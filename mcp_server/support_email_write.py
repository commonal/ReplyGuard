"""MCP EMAIL WRITE Server — capability-isolated to enterprise SMTP only.

Implements spec.md §8 "MCP EMAIL WRITE Server":
  - send_email(to, subject, body, in_reply_to, idempotency_key, references)
    → sends via Tencent/NetEase enterprise SMTP; app-layer idempotent.

Critical invariants (spec §6 Send Email, CLAUDE.md):
  1. App-layer idempotency: before SMTP call, check mcp_server/.email_idem.db for
     the idempotency_key. If already sent, return cached sent_message_id.
   2. Three threading headers (kept for provider-independent threading):
       In-Reply-To: <original_message_id>
       References: <original_message_id>
       Subject: Re: <original subject>
   3. ZERO approval-channel code. Cannot post to Feishu or Slack under any circumstance.

Env vars read (from .env.example):
  EMAIL_USER           — sender address (e.g. support@example.com)
  EMAIL_APP_PASSWORD   — enterprise-mail client authorization code
  EMAIL_SMTP_HOST      — defaults to Tencent enterprise SMTP
  EMAIL_SMTP_PORT      — 465 (implicit TLS) or 587 (STARTTLS)

  GMAIL_* names remain accepted as a backwards-compatible alias for old
  deployments and tests; they are not required for a new setup.

Run standalone (stdio):
    python mcp_server/support_email_write.py
"""

from __future__ import annotations

import email.utils
import logging
import os
import pathlib
import sys
import uuid
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any

import aiosqlite
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, stream=sys.stderr)

# ---------------------------------------------------------------------------
# Config — read from env, never hardcoded
# ---------------------------------------------------------------------------

EMAIL_PROVIDER: str = os.environ.get("EMAIL_PROVIDER", "tencent").strip().lower()
_SMTP_DEFAULTS: dict[str, tuple[str, int]] = {
    "tencent": ("smtp.exmail.qq.com", 465),
    "netease": ("smtphz.qiye.163.com", 465),
}
_default_smtp_host, _default_smtp_port = _SMTP_DEFAULTS.get(
    EMAIL_PROVIDER, _SMTP_DEFAULTS["tencent"]
)

# Keep the old constant names because existing tests and downstream users may
# monkeypatch them. Their values now come from the generic EMAIL_* settings.
GMAIL_USER: str = os.environ.get("EMAIL_USER") or os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD: str = os.environ.get("EMAIL_APP_PASSWORD") or os.environ.get(
    "GMAIL_APP_PASSWORD", ""
)
GMAIL_SMTP_HOST: str = (
    os.environ.get("EMAIL_SMTP_HOST")
    or os.environ.get("GMAIL_SMTP_HOST")
    or _default_smtp_host
)
GMAIL_SMTP_PORT: int = int(
    os.environ.get("EMAIL_SMTP_PORT")
    or os.environ.get("GMAIL_SMTP_PORT")
    or str(_default_smtp_port)
)
GMAIL_SMTP_SECURITY: str = (
    os.environ.get("EMAIL_SMTP_SECURITY")
    or os.environ.get("GMAIL_SMTP_SECURITY")
    or ("ssl" if GMAIL_SMTP_PORT == 465 else "starttls")
).strip().lower()

# ---------------------------------------------------------------------------
# Idempotency DB — tiny SQLite table keyed by idempotency_key
# ---------------------------------------------------------------------------
_REPO_ROOT = pathlib.Path(__file__).parent.parent
_IDEM_DB_PATH = pathlib.Path(__file__).parent / ".email_idem.db"


async def _ensure_idem_schema() -> None:
    """Create idempotency table if it doesn't exist yet."""
    async with aiosqlite.connect(_IDEM_DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_emails (
                idempotency_key TEXT PRIMARY KEY,
                sent_message_id TEXT NOT NULL,
                sent_at         TEXT NOT NULL
            )
            """
        )
        await db.commit()


# Sentinel value stored in `sent_message_id` between reservation and SMTP
# completion. Concurrent callers seeing this value know a peer is mid-send
# and must NOT call SMTP themselves.
_RESERVED_SENTINEL = "__RESERVED__"


async def _lookup_sent(idempotency_key: str) -> str | None:
    """Return sent_message_id if already sent, else None.

    May return the `__RESERVED__` sentinel — caller is responsible for
    distinguishing "completed" from "in-flight" results.
    """
    async with aiosqlite.connect(_IDEM_DB_PATH) as db:
        async with db.execute(
            "SELECT sent_message_id FROM sent_emails WHERE idempotency_key = ?",
            (idempotency_key,),
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else None


async def _try_reserve(idempotency_key: str) -> bool:
    """Atomic reserve-or-bail.

    Returns True if THIS caller acquired the reservation (proceeds to SMTP).
    Returns False if the row already existed (caller is a duplicate or
    racing peer — must NOT call SMTP).

    The check-and-insert is a single SQL statement that SQLite executes
    atomically: `INSERT OR IGNORE` is a constraint-fail-tolerant insert,
    and `cursor.rowcount` reflects whether the row was newly inserted.
    No window where two concurrent callers can both pass.
    """
    async with aiosqlite.connect(_IDEM_DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT OR IGNORE INTO sent_emails (idempotency_key, sent_message_id, sent_at)
            VALUES (?, ?, ?)
            """,
            (
                idempotency_key,
                _RESERVED_SENTINEL,
                datetime.now(UTC).isoformat(),
            ),
        )
        await db.commit()
        return cursor.rowcount == 1  # 1 = we inserted, 0 = key existed


async def _commit_sent(idempotency_key: str, sent_message_id: str) -> None:
    """Replace the reservation sentinel with the real sent Message-ID.

    Called after SMTP succeeds. UPDATE is unconditional because we only
    reach this path when `_try_reserve` returned True — the row exists
    and holds our reservation.
    """
    async with aiosqlite.connect(_IDEM_DB_PATH) as db:
        await db.execute(
            "UPDATE sent_emails SET sent_message_id = ? WHERE idempotency_key = ?",
            (sent_message_id, idempotency_key),
        )
        await db.commit()


async def _release_reservation(idempotency_key: str) -> None:
    """Drop a reservation row so retries after SMTP failure can re-attempt.

    Called when SMTP raises before completion. Without this, a transient
    SMTP failure would permanently block retry of the same idempotency_key.
    """
    async with aiosqlite.connect(_IDEM_DB_PATH) as db:
        await db.execute(
            "DELETE FROM sent_emails WHERE idempotency_key = ? AND sent_message_id = ?",
            (idempotency_key, _RESERVED_SENTINEL),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# SMTP helper — uses aiosmtplib (async-first per CLAUDE.md)
# ---------------------------------------------------------------------------

def _build_message(
    to: str,
    subject: str,
    body: str,
    in_reply_to: str,
    references: str,
    message_id: str,
) -> EmailMessage:
    """Assemble an RFC-822 EmailMessage with the three required threading headers."""
    msg = EmailMessage()
    msg["From"] = GMAIL_USER
    msg["To"] = to
    msg["Date"] = email.utils.formatdate(localtime=False)
    msg["Message-ID"] = f"<{message_id}>"

    # Keep the conventional reply subject for all enterprise mail providers.
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    msg["Subject"] = subject

    # Three threading headers — ALL required (spec §6 + CLAUDE.md invariants)
    if in_reply_to:
        # Normalise: wrap in angle brackets if missing
        if not in_reply_to.startswith("<"):
            in_reply_to = f"<{in_reply_to}>"
        msg["In-Reply-To"] = in_reply_to

        # References = existing references chain + in_reply_to
        ref_chain = references.strip() if references else ""
        if in_reply_to not in ref_chain:
            ref_chain = (ref_chain + " " + in_reply_to).strip()
        msg["References"] = ref_chain

    msg.set_content(body)
    return msg


async def _smtp_send(msg: EmailMessage) -> str:
    """Send *msg* via aiosmtplib and return the sent Message-ID.

    The security mode is inferred from EMAIL_SMTP_SECURITY or the configured
    port, so Tencent/NetEase port 465 uses implicit TLS while port 587 uses
    STARTTLS. A configured proxy tunnel is still supported for environments
    where direct outbound TCP is unavailable.
    """
    import aiosmtplib  # local import — zero approval-channel code in this module

    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        raise RuntimeError(
            "EMAIL_USER and EMAIL_APP_PASSWORD must be set in environment. "
            "See .env.example."
        )

    tls_kwargs: dict[str, Any]
    if GMAIL_SMTP_SECURITY in {"ssl", "tls", "implicit_tls"} or GMAIL_SMTP_PORT == 465:
        tls_kwargs = {"use_tls": True, "validate_certs": True}
    else:
        tls_kwargs = {"start_tls": True, "validate_certs": True}

    # Opt-in proxy tunnel (no-op when HTTPS_PROXY is unset → direct connect).
    try:
        from src.proxy_tunnel import smtp_tunnel_socket
        sock = smtp_tunnel_socket(GMAIL_SMTP_HOST, dest_port=GMAIL_SMTP_PORT)
        # The tunnel is a plain TCP pipe; aiosmtplib performs the configured
        # TLS handshake on top of it.
        await aiosmtplib.send(
            msg,
            hostname=GMAIL_SMTP_HOST,
            sock=sock,
            username=GMAIL_USER,
            password=GMAIL_APP_PASSWORD,
            **tls_kwargs,
        )
    except RuntimeError:
        # No proxy configured → use the configured direct host/port.
        await aiosmtplib.send(
            msg,
            hostname=GMAIL_SMTP_HOST,
            port=GMAIL_SMTP_PORT,
            username=GMAIL_USER,
            password=GMAIL_APP_PASSWORD,
            **tls_kwargs,
        )
    # Message-ID was set before send; return it as the stable sent_message_id
    return str(msg["Message-ID"])


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "support-email-write",
    instructions=(
        "EMAIL WRITE server. Sends customer reply emails via enterprise SMTP. "
        "App-layer idempotent — will not double-send for the same idempotency_key. "
        "Has no ability to read CRM data or post to the approval channel."
    ),
)


@mcp.tool()
async def send_email(
    to: str,
    subject: str,
    body: str,
    in_reply_to: str,
    idempotency_key: str,
    references: str = "",
) -> dict[str, Any]:
    """Send a reply email via configured enterprise SMTP, idempotently.

    Args:
        to:               Recipient email address.
        subject:          Email subject. If it doesn't start with 'Re: ', the
                           server prepends it for normal reply threading.
        body:             Plain-text body of the reply.
        in_reply_to:      RFC-822 Message-ID of the customer's original email
                          (used for In-Reply-To AND References headers).
        idempotency_key:  Stable key set once at ticket entry. If this key has
                          been sent before, returns the cached sent_message_id
                          without calling SMTP again.
        references:       Optional existing References header chain to extend.

    Returns: dict with keys:
        sent_message_id (str)  — RFC-822 Message-ID of the sent email.
        was_duplicate (bool)   — True if idempotency guard fired (no SMTP call).
        status (str)           — "sent" | "duplicate_skipped".
    """
    await _ensure_idem_schema()

    # --- Atomic reserve-or-bail (closes audit finding H2) ---
    # `_try_reserve` is a single INSERT OR IGNORE — atomic at the SQLite
    # level. Concurrent callers cannot both pass; exactly one wins. Replaces
    # the previous lookup→SMTP→record pattern that had a check-then-act
    # window where two retries with the same idempotency_key could both
    # call SMTP and the customer would receive duplicate replies.
    we_won_reservation = await _try_reserve(idempotency_key)
    if not we_won_reservation:
        # Row already exists — either a completed prior send OR a
        # concurrent peer mid-send.
        existing = await _lookup_sent(idempotency_key)
        if existing == _RESERVED_SENTINEL:
            # Concurrent send in flight on a peer process. Do NOT call
            # SMTP. Surface a clear status so LangGraph's retry logic
            # can wait and re-poll rather than racing.
            logger.warning(
                "send_email: concurrent in-flight reservation for key=%s",
                idempotency_key,
            )
            return {
                "sent_message_id": "",
                "was_duplicate": True,
                "status": "concurrent_in_flight",
            }
        logger.info(
            "send_email: duplicate detected for key=%s; returning cached id=%s",
            idempotency_key,
            existing,
        )
        return {
            "sent_message_id": existing or "",
            "was_duplicate": True,
            "status": "duplicate_skipped",
        }

    # --- We hold the reservation. Build + send + commit. ---
    new_message_id = f"{uuid.uuid4()}@{GMAIL_SMTP_HOST}"
    msg = _build_message(
        to=to,
        subject=subject,
        body=body,
        in_reply_to=in_reply_to,
        references=references,
        message_id=new_message_id,
    )

    try:
        sent_id = await _smtp_send(msg)
    except Exception:
        # Release the reservation so the LangGraph send-retry path can
        # try again with the same idempotency_key. Without this, a
        # transient SMTP failure permanently blocks retry.
        await _release_reservation(idempotency_key)
        raise

    logger.info("send_email: sent to=%s message_id=%s", to, sent_id)
    await _commit_sent(idempotency_key, sent_id)

    return {
        "sent_message_id": sent_id,
        "was_duplicate": False,
        "status": "sent",
    }


if __name__ == "__main__":
    # _ensure_idem_schema() runs on first send_email call; no need to pre-run here.
    mcp.run(transport="stdio")
