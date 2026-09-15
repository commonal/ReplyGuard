"""Feishu card callback handler — resumes the LangGraph approval gate.

Feishu sends card interactions as HTTP POST requests. The handler performs only
the fast callback work on the request path: it validates the verification token,
extracts the action/form values, schedules the graph resume, and returns a
toast response. The graph itself may take much longer than Feishu's callback
deadline, so it is never awaited before the acknowledgement is returned.

The callback contract intentionally matches ``src.slack_handler``:

* Approve -> ``{"action": "approve", "approver_id": ...}``
* Edit -> ``{"action": "edit", "edited_draft": ..., "approver_id": ...}``
* Reject -> ``{"action": "reject", "reason": ..., "approver_id": ...}``

URL verification uses the application verification token. The old form-card
callback protocol has a message update token in its JSON body and authenticates
the request with Feishu's ``X-Lark-*`` SHA-1 signature headers, so the HTTP
route has a test-tenant opt-in compatibility switch for that protocol.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from collections.abc import Mapping
from typing import Any

from src.config import settings

log = logging.getLogger(__name__)


def _token_from_body(body: dict[str, Any]) -> str:
    header = body.get("header") or {}
    if isinstance(header, dict) and header.get("token"):
        return str(header["token"])
    # Do not use event.token here: for card actions it is the message update
    # token (often c-...), not the app verification token.
    if body.get("token"):
        return str(body["token"])
    return ""


def verify_feishu_verification_token(
    body: dict[str, Any], verification_token: str | None = None
) -> bool:
    """Validate the token when one is configured; allow local dev without it."""
    expected = verification_token if verification_token is not None else settings.feishu_verification_token
    if not expected:
        return True
    actual = _token_from_body(body)
    return bool(actual) and actual == expected


def verify_feishu_card_signature(
    headers: Mapping[str, str], body: bytes, verification_token: str | None = None
) -> bool:
    """Verify the HTTP signature used by legacy message-card callbacks.

    The legacy ``card.action.trigger_v1`` protocol does not put the
    application Verification Token in the JSON action payload. Feishu signs
    the raw request body with the timestamp, nonce, and Verification Token
    using SHA-1 instead. The raw bytes must be used; parsing and re-serializing
    JSON would change the signature input.
    """
    expected = (
        verification_token
        if verification_token is not None
        else settings.feishu_verification_token
    )
    if not expected:
        return True
    timestamp = headers.get("X-Lark-Request-Timestamp", "")
    nonce = headers.get("X-Lark-Request-Nonce", "")
    signature = headers.get("X-Lark-Signature", "")
    if not timestamp or not nonce or not signature:
        return False
    signing_input = (timestamp + nonce + expected).encode("utf-8") + body
    calculated = hashlib.sha1(signing_input).hexdigest()
    return hmac.compare_digest(calculated, signature)


def _action_node(body: dict[str, Any]) -> dict[str, Any]:
    event = body.get("event")
    if isinstance(event, dict):
        action = event.get("action")
        if isinstance(action, dict):
            return action
    action = body.get("action")
    return action if isinstance(action, dict) else {}


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _operator_id(body: dict[str, Any]) -> str:
    event = body.get("event") or {}
    operator = event.get("operator") if isinstance(event, dict) else None
    if not isinstance(operator, dict):
        operator = {}
    operator_id = operator.get("operator_id")
    if isinstance(operator_id, dict):
        return str(
            operator_id.get("open_id")
            or operator_id.get("user_id")
            or operator_id.get("union_id")
            or ""
        )
    return str(
        operator.get("open_id")
        or operator.get("user_id")
        or operator.get("union_id")
        or event.get("operator_id", "")
        or body.get("open_id", "")
        or body.get("user_id", "")
    )


def _form_values(action: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    for candidate in (
        action.get("form_value"),
        action.get("form_values"),
        (body.get("event") or {}).get("form_value") if isinstance(body.get("event"), dict) else None,
    ):
        values = _as_mapping(candidate)
        if values:
            return values
    return {}


def _resume_payload(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    action = _action_node(body)
    raw_value = action.get("value")
    value = _as_mapping(raw_value)
    action_name = str(
        value.get("action")
        or value.get("decision")
        or action.get("name")
        or action.get("action_type")
        or ""
    ).lower()
    action_name = action_name.removesuffix("_button")

    thread_id = str(
        value.get("thread_id")
        or value.get("ticket_id")
        or (raw_value if isinstance(raw_value, str) else "")
        or ""
    )
    form = _form_values(action, body)
    payload: dict[str, Any] = {
        "action": action_name,
        "approver_id": _operator_id(body),
    }
    if action_name == "edit":
        payload["edited_draft"] = str(
            form.get("edited_draft") or form.get("draft") or form.get("reply") or ""
        )
    elif action_name == "reject":
        payload["reason"] = str(form.get("reject_reason") or form.get("reason") or "")
    return thread_id, payload


async def _resume_graph(thread_id: str, resume_value: dict[str, Any]) -> None:
    if not thread_id:
        return
    from src.graph_runner import resume as runner_resume

    await runner_resume(thread_id, resume_value)


async def handle_feishu_event(body: dict[str, Any]) -> dict[str, Any]:
    """Handle URL verification or a card action and return Feishu's response."""
    if body.get("encrypt"):
        raise RuntimeError(
            "Encrypted Feishu callbacks are not enabled in this local adapter; "
            "leave FEISHU_ENCRYPT_KEY empty for the test-tenant setup."
        )

    if body.get("type") == "url_verification":
        if not verify_feishu_verification_token(body):
            raise PermissionError("invalid Feishu verification token")
        return {"challenge": str(body.get("challenge") or "")}

    thread_id, resume_value = _resume_payload(body)
    if resume_value["action"] not in {"approve", "edit", "reject"}:
        return {"toast": {"type": "error", "content": "无法识别审批操作"}}
    if not thread_id:
        log.warning("Feishu card action missing thread_id; resume skipped")
        return {"toast": {"type": "error", "content": "工单编号缺失，未执行操作"}}

    # Feishu expects a quick response. The graph resume is durable and may
    # include revalidation, LLM calls, SMTP, and a final card update.
    asyncio.create_task(_resume_graph(thread_id, resume_value))
    return {"toast": {"type": "success", "content": "已提交，系统处理中"}}


__all__ = [
    "handle_feishu_event",
    "verify_feishu_card_signature",
    "verify_feishu_verification_token",
]
