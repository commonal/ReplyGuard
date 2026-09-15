"""IMAP email listener — turns inbound enterprise email into LangGraph entry points.
IMAP 邮件监听器，负责实时监听企业邮箱收件箱，并将每封新邮件转化为 AI 客服系统（LangGraph）的初始工单
Two modes (architecture.md "Detailed flow"):
  - **Preferred:** IMAP IDLE — the mail provider pushes a notification when a
    new message arrives. Implemented via `aioimaplib`.
  - **Fallback:** poll every `IMAP_POLL_INTERVAL_SEC` seconds if IDLE drops
    or is unsupported on the connection.

Each new email becomes an `AgentState` and is fed into the compiled
LangGraph via `graph_runner.start_ticket(...)`.
"""

from __future__ import annotations

import asyncio
import contextlib
import email as stdlib_email
import logging
import re
import ssl
import uuid
from email.header import decode_header
from typing import Any

import aioimaplib

from src.config import settings
from src.state import initial_state

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Email parsing helpers 
# ---------------------------------------------------------------------------


def _unseen_search_criteria() -> tuple[str, ...]:
    """Return UNSEEN search criteria, optionally narrowed for a test run."""
    subject = settings.imap_subject_filter.strip()
    if not subject:
        return ("UNSEEN",)
    escaped = subject.replace("\\", "\\\\").replace('"', '\\"')
    return ("UNSEEN", "SUBJECT", f'"{escaped}"')


async def _message_subject(
    client: aioimaplib.IMAP4_SSL, msg_id: bytes, subject_filter: str
) -> str:
    """Read only the Subject header for an optional test-only local guard."""
    if not subject_filter:
        return subject_filter
    typ, data = await client.fetch(
        msg_id.decode(), "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])"
    )
    if typ != "OK" or not data:
        return ""
    for item in data:
        if not isinstance(item, (bytes, bytearray)) or b":" not in item:
            continue
        try:
            header = stdlib_email.message_from_bytes(bytes(item))
        except (TypeError, ValueError):
            continue
        subject = _decode_header(header.get("Subject"))
        if subject:
            return subject
    return ""


def _decode_header(raw: str | bytes | None) -> str:
    #解码邮件主题、发件人编码
    if raw is None:
        return ""
    # `email.message.Message.get()` may return an email.header.Header object
    # under the default compat32 policy. Normalise non-bytes through str()
    # before handing the value to decode_header().
    value = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    parts = decode_header(value)
    out = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            out.append(chunk.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


def _extract_body(msg: stdlib_email.message.Message) -> str:
    # `get_payload(decode=True)` is typed `Message | bytes | Any | None` by
    # stdlib stubs. With decode=True it really only returns bytes-or-None;
    # cast / isinstance-narrow before calling `.decode`.
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                payload = part.get_payload(decode=True) or b""
                if not isinstance(payload, bytes):
                    continue
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
        return ""
    payload = msg.get_payload(decode=True) or b""
    if not isinstance(payload, bytes):
        return ""
    charset = msg.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


_ENVELOPE_RE = re.compile(r"<([^>]+)>|([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})")


def _extract_envelope_from(msg: stdlib_email.message.Message) -> str:
    """Return the SMTP envelope-from address from `Return-Path`.

    Most MTAs populate `Return-Path` with the actual SMTP
    envelope-from. This is the trustworthy sender — the RFC-822 `From:`
    header is content the sender controls and is spoofable.

    Falls back to the empty string if Return-Path is missing or malformed,
    in which case the caller should treat the recipient as unknown.
    """
    #优先读`Return‑Path`，因为邮件头部的from 是可以编造的
    raw = msg.get("Return-Path") or msg.get("X-Original-Sender") or ""
    if not raw:
        return ""
    match = _ENVELOPE_RE.search(raw)
    if not match:
        return ""
    return (match.group(1) or match.group(2) or "").strip() #返回真实邮箱


def parse_email_to_ticket(raw_bytes: bytes) -> dict[str, Any]:
    """Parse raw RFC-822 bytes into a ticket dict ready to seed AgentState.
    输入原始 RFC‑822 邮件二进制字节，输出工单 dict
    `envelope_from` is the trustworthy recipient address. `from` is the
    spoofable header — kept for LLM context only, NEVER used as recipient.
        {
    "envelope_from": "真实信封发件人",
    "from": "邮件头From（可伪造，仅用于LLM展示）",
    "subject": "邮件主题",
    "body": "邮件正文",
    "email_thread_id": "Message‑ID，邮件线程ID，用于会话追踪"
    }
    """
    msg = stdlib_email.message_from_bytes(raw_bytes)
    return {
        "envelope_from": _extract_envelope_from(msg),
        "from": _decode_header(msg.get("From")),
        "subject": _decode_header(msg.get("Subject")),
        "body": _extract_body(msg),
        "email_thread_id": msg.get("Message-ID", ""),
    }


def ticket_to_initial_state(ticket: dict[str, Any]) -> Any:
    """Build the initial AgentState from a parsed ticket dict.
    为这封邮件生成唯一的 ticket_id，并构建 LangGraph 需要的初始状态
    Stores envelope_from in the ephemeral PII vault (NOT in audit_log)
    so Send Email can retrieve the trustworthy recipient address without
    leaking it to LangSmith / SQLite checkpoints.
    """
    from src.pii import store_envelope_from

    ticket_id = "ticket-" + uuid.uuid4().hex[:12]
    envelope = ticket.get("envelope_from", "")
    if envelope:
        store_envelope_from(ticket_id, envelope) #把真实发件人存入独立 PII 保险箱；

    # customer_message: the LLM sees the spoofable From for context, but
    # the agent NEVER uses it as the recipient — Send Email reads the
    # envelope-from from the vault.
    return ticket_id, initial_state(
        ticket_id=ticket_id,
        customer_message=f"From: {ticket['from']}\nSubject: {ticket['subject']}\n\n{ticket['body']}",
        email_thread_id=ticket["email_thread_id"], #用于识别同一个邮件会话
        send_idempotency_key="idem-" + uuid.uuid4().hex,
    )


# ---------------------------------------------------------------------------
# IMAP loop — IDLE first, polls as fallback. 顶层无限循环
# ---------------------------------------------------------------------------


async def listen_forever(on_ticket: Any) -> None:  # on_ticket: async callable(ticket_id, state)
    """Main loop. `on_ticket` receives every new email as (ticket_id, AgentState)."""
    settings.require_secrets("email_user", "email_app_password")

    while True:
        try:
            await _idle_loop(on_ticket)
        except Exception as exc:  # broad: connection errors and network blips; fall back to polling
            log.warning("IMAP IDLE failed (%s) — falling back to poll", exc)
            try:
                await _poll_loop(on_ticket)
            except Exception:
                #如果轮询也崩了，sleep 配置间隔后重新外层循环重试
                log.exception("Poll loop crashed; sleeping before retry")
                await asyncio.sleep(settings.imap_poll_interval_sec)


async def _connect() -> aioimaplib.IMAP4_SSL:
    # In blocked networks (e.g. GFW) aioimaplib's direct connect fails with
    # an SSL EOF because it does NOT read proxy env vars. When a SOCKS5 proxy
    # is configured (SOCKS_PROXY / socks5:// in HTTPS_PROXY) we tunnel the
    # TCP connection and hand the raw socket to aioimaplib's transport.
    try:
        from src.proxy_tunnel import imap_tunnel_socket

        loop = asyncio.get_running_loop()
        raw = await imap_tunnel_socket(settings.email_imap_host, settings.email_imap_port)
        client = aioimaplib.IMAP4(
            host=settings.email_imap_host,
            port=settings.email_imap_port,
            loop=loop,
        )
        ctx = ssl.create_default_context()
        transport, _proto = await loop.create_connection(
            lambda: client.protocol,
            sock=raw,
            ssl=ctx,
            server_hostname=settings.email_imap_host,
        )
        # aioimaplib awaits wait_hello via the protocol's state machine; the
        # create_client path in __init__ already scheduled a task we replaced.
    except RuntimeError:
        # No SOCKS5 proxy → use the default direct SSL connect.
        client = aioimaplib.IMAP4_SSL(
            host=settings.email_imap_host,
            port=settings.email_imap_port,
        )
    await client.wait_hello_from_server()
    await client.login(settings.email_user, settings.email_app_password)
    await client.select("INBOX")
    return client


async def _idle_loop(on_ticket: Any) -> None:
    client = await _connect()
    try:
        # Fetch any UNSEEN already in the mailbox first. 一次性消费未读邮件
        await _fetch_unseen(client, on_ticket)

        while True:
            #后台运行的异步idle会话任务
            idle_task = await client.idle_start(timeout=29 * 60)  # < common 30min ceiling
            await client.wait_server_push() #等待服务端推送事件
            client.idle_done() #收到推送事件之后，客户端必须发送 `DONE` 命令结束 IDLE 状态，之后才能执行 SEARCH / FETCH 这类普通 IMAP 指令
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(idle_task, timeout=10) #回收idle，十秒后如果没有成功
            await _fetch_unseen(client, on_ticket)
    finally:
        with contextlib.suppress(Exception): #`contextlib.suppress(Exception)`：吃掉 logout 阶段所有异常
            await client.logout()


async def _poll_loop(on_ticket: Any) -> None:
    while True:
        client = await _connect()
        try:
            await _fetch_unseen(client, on_ticket)
        finally:
            with contextlib.suppress(Exception):
                await client.logout()
        await asyncio.sleep(settings.imap_poll_interval_sec)


async def _fetch_unseen(client: aioimaplib.IMAP4_SSL, on_ticket: Any) -> None:
    #IMAP 协议：`SEARCH`成功时，`data[0]` 是空格分隔的邮件数字编号字符串，例如 `b'12 13 14'`；没有未读邮件时 `data[0]` 是空字节串 `b''`。
    typ, data = await client.search(*_unseen_search_criteria())   #命令响应状态字符串，响应载荷
    if typ != "OK" or not data or not data[0]:
        return
    ids = data[0].split() #拿到所有未读邮件编号
    subject_filter = settings.imap_subject_filter.strip()
    max_messages = max(0, settings.imap_max_messages)
    processed_messages = 0
    for msg_id in ids:
        if subject_filter:
            subject = await _message_subject(client, msg_id, subject_filter)
            if subject_filter not in subject:
                continue
        typ, msg_data = await client.fetch(msg_id.decode(), "(RFC822)")
        if typ != "OK" or not msg_data:
            continue
        # aioimaplib returns a list with the body bytes at index 1
        for item in msg_data:
            if isinstance(item, (bytes, bytearray)) and len(item) > 100:
                ticket = parse_email_to_ticket(bytes(item))
                ticket_id, state = ticket_to_initial_state(ticket)
                log.info("Inbound email -> ticket %s", ticket_id)
                try:
                    await on_ticket(ticket_id, state) #这里是同步的
                except Exception:
                    # A poison message must not be re-read on every IMAP
                    # reconnect. The failed ticket is logged with its stable
                    # ticket_id; operators can resend it after fixing the
                    # downstream failure.
                    log.exception(
                        "Ticket %s failed; marking message %s as Seen to avoid duplicate processing",
                        ticket_id,
                        msg_id.decode(),
                    )
                finally:
                    # Mark as Seen after the processing attempt so a callback
                    # or model failure cannot create duplicate tickets.
                    await client.store(msg_id.decode(), "+FLAGS", "(\\Seen)")
                processed_messages += 1
                if max_messages and processed_messages >= max_messages:
                    break
                break
