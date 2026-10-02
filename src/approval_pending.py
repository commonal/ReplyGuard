"""Durable index of tickets currently waiting on a human approval.

Why this exists
---------------
A ticket paused at `interrupt_gate` lives only inside the LangGraph
checkpoint. Nothing iterates those checkpoints, so before this module there
was no way to answer "which tickets have been waiting longer than their SLA?"
— `sla_deadline` was written into state and never read, so the documented
"24h with no human response -> Manual Queue" path was unreachable.

This is a small side index, not a second source of truth: the checkpoint still
owns the ticket, and every row here is a pointer plus a deadline. Losing the
file degrades to the old behaviour (no timeout escalation) rather than
corrupting a ticket, so every failure path logs and continues.

Symmetric with `src/pii.py`'s sidecar: one SQLite file, one connection,
best-effort writes.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

_CONN: sqlite3.Connection | None = None
_INITED = False
_VAULT_LOCK = threading.Lock()

_DEFAULT_PATH = "data/approval_pending.sqlite"


def _db_path() -> str:
    return os.environ.get("APPROVAL_PENDING_DB_PATH", _DEFAULT_PATH)


def _conn() -> sqlite3.Connection | None:
    """Open (once) the pending-approval index. None disables the feature."""
    global _CONN, _INITED
    if _INITED:
        return _CONN

    path = _db_path()
    _INITED = True
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pending_approval ("
            "thread_id TEXT PRIMARY KEY, "
            "ticket_id TEXT, "
            "channel TEXT, "
            "message_ts TEXT, "
            "deadline TEXT, "
            "requested_at TEXT"
            ")"
        )
        conn.commit()
        _CONN = conn
        log.info("Pending-approval index opened at %s", path)
    except Exception as exc:  # noqa: BLE001 — a disk problem must not break a ticket
        log.warning(
            "Pending-approval index at %s failed to open: %s — SLA escalation disabled",
            path,
            exc,
        )
        _CONN = None
    return _CONN


def _reset_for_tests() -> None:
    """Drop the cached connection so a test can point at its own file."""
    global _CONN, _INITED
    close()
    _CONN = None
    _INITED = False


def _iso(value: datetime) -> str:
    """Serialise as UTC, reading a naive datetime the same way _parse does."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def remember(
    *,
    thread_id: str,
    ticket_id: str = "",
    channel: str = "",
    message_ts: str = "",
    deadline: datetime | str | None = None,
    requested_at: datetime | str | None = None,
) -> None:
    """Upsert the ticket as waiting on a human.

    Called on every approval prompt, including re-prompts, so the deadline
    tracks the card the human is actually looking at.
    """
    if not thread_id:
        return
    deadline_dt = _parse(deadline)
    requested_dt = _parse(requested_at) or datetime.now(UTC)
    conn = _conn()
    if conn is None:
        return
    try:
        with _VAULT_LOCK:
            conn.execute(
                "INSERT INTO pending_approval "
                "(thread_id, ticket_id, channel, message_ts, deadline, requested_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(thread_id) DO UPDATE SET "
                "ticket_id=excluded.ticket_id, channel=excluded.channel, "
                "message_ts=excluded.message_ts, deadline=excluded.deadline, "
                "requested_at=excluded.requested_at",
                (
                    thread_id,
                    ticket_id,
                    channel,
                    message_ts,
                    _iso(deadline_dt) if deadline_dt else "",
                    _iso(requested_dt),
                ),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("Pending-approval remember failed for %s: %s", thread_id, exc)


def forget(thread_id: str) -> None:
    """Drop the row once the ticket is no longer waiting (any terminal path)."""
    if not thread_id:
        return
    conn = _conn()
    if conn is None:
        return
    try:
        with _VAULT_LOCK:
            conn.execute("DELETE FROM pending_approval WHERE thread_id = ?", (thread_id,))
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("Pending-approval forget failed for %s: %s", thread_id, exc)


def all_pending() -> list[dict[str, str]]:
    conn = _conn()
    if conn is None:
        return []
    try:
        with _VAULT_LOCK:
            rows = conn.execute(
                "SELECT thread_id, ticket_id, channel, message_ts, deadline, requested_at "
                "FROM pending_approval"
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("Pending-approval read failed: %s", exc)
        return []
    keys = ("thread_id", "ticket_id", "channel", "message_ts", "deadline", "requested_at")
    return [dict(zip(keys, row, strict=True)) for row in rows]


def expired(now: datetime | None = None) -> list[dict[str, str]]:
    """Rows whose deadline has passed. Rows with no deadline are skipped."""
    moment = now or datetime.now(UTC)
    out: list[dict[str, str]] = []
    for row in all_pending():
        deadline = _parse(row.get("deadline"))
        if deadline is not None and deadline <= moment:
            out.append(row)
    return out


def close() -> None:
    global _CONN
    conn = _CONN
    _CONN = None
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            log.debug("Pending-approval index close failed", exc_info=True)


__all__ = [
    "_reset_for_tests",
    "all_pending",
    "close",
    "expired",
    "forget",
    "remember",
]
