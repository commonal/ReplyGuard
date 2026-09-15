"""MCP APPROVAL WRITE Server — capability-isolated to Feishu Open Platform.

The graph deliberately keeps the old ``slack`` client attribute and tool names
for checkpoint/test compatibility.  When ``APPROVAL_PROVIDER=feishu`` this
server is the implementation behind that interface:

* ``post_approval_request`` sends an interactive Feishu card;
* ``update_message`` replaces the same card after approve/reject/send;
* the card contains a small form, so Edit and Reject can return form values in
  the normal Feishu card callback payload.

This process has no email, CRM, or LLM capability.  Credentials are read only
from environment variables and are never included in tool results or logs.

Run standalone (stdio)::

    python mcp_server/support_feishu_write.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, stream=sys.stderr)


FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_RECEIVE_ID_TYPE = os.environ.get("FEISHU_RECEIVE_ID_TYPE", "chat_id")
FEISHU_RECEIVE_ID = os.environ.get("FEISHU_RECEIVE_ID") or os.environ.get(
    "FEISHU_CHAT_ID", ""
)
FEISHU_API_BASE_URL = os.environ.get(
    "FEISHU_API_BASE_URL", "https://open.feishu.cn/open-apis"
).rstrip("/")

_tenant_token = ""
_tenant_token_expires_at = 0.0
_tenant_token_lock = asyncio.Lock()


def _json_payload(raw: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Feishu API returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Feishu API returned a non-object response")
    return parsed


def _request_json_sync(
    method: str,
    url: str,
    payload: dict[str, Any],
    access_token: str = "",
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=20) as response:  # noqa: S310 - fixed API base URL
            raw = response.read()
    except HTTPError as exc:
        detail = "request failed"
        try:
            response = _json_payload(exc.read())
            detail = str(response.get("msg") or detail)
        except RuntimeError:
            pass
        raise RuntimeError(f"Feishu API HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Feishu API network error: {exc.reason}") from exc
    return _json_payload(raw)


async def _request_json(
    method: str,
    path: str,
    payload: dict[str, Any],
    access_token: str = "",
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    url = f"{FEISHU_API_BASE_URL}/{path.lstrip('/')}"
    if query:
        url = f"{url}?{urlencode(query)}"
    return await asyncio.to_thread(
        _request_json_sync,
        method,
        url,
        payload,
        access_token,
    )


async def _get_tenant_access_token() -> str:
    """Fetch and briefly cache the app's tenant access token."""
    global _tenant_token, _tenant_token_expires_at
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        raise RuntimeError(
            "FEISHU_APP_ID and FEISHU_APP_SECRET must be set in environment."
        )

    async with _tenant_token_lock:
        if _tenant_token and time.monotonic() < _tenant_token_expires_at:
            return _tenant_token
        response = await _request_json(
            "POST",
            "/auth/v3/tenant_access_token/internal",
            {"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        )
        if response.get("code") != 0:
            raise RuntimeError(f"Feishu token request failed: {response.get('msg', 'unknown error')}")
        token = str(response.get("tenant_access_token") or "")
        if not token:
            raise RuntimeError("Feishu token response did not contain tenant_access_token")
        _tenant_token = token
        # Feishu normally returns 7,200 seconds. Keep a one-minute margin.
        expire = int(response.get("expire") or 7200)
        _tenant_token_expires_at = time.monotonic() + max(60, expire - 60)
        return _tenant_token


def _text_from_slack_block(block: dict[str, Any]) -> str:
    text = block.get("text")
    if isinstance(text, dict):
        return str(text.get("text") or text.get("content") or "")
    return str(text or "")


def _draft_from_blocks(blocks: list[dict[str, Any]]) -> str:
    marker = "*Draft reply*"
    for block in blocks:
        text = _text_from_slack_block(block)
        if marker not in text:
            continue
        draft = text.split(marker, 1)[1].lstrip("\n")
        if draft.startswith("```"):
            draft = draft[3:]
        if draft.endswith("```"):
            draft = draft[:-3]
        return draft.strip("\n")
    return ""


def _ticket_id_from_blocks(blocks: list[dict[str, Any]]) -> str:
    for block in blocks:
        for element in block.get("elements") or []:
            value = element.get("value")
            if isinstance(value, dict):
                candidate = value.get("thread_id") or value.get("ticket_id")
            else:
                candidate = value
            if isinstance(candidate, str) and candidate.startswith("ticket-"):
                return candidate
    return ""


def _label_from_slack_button(button: dict[str, Any]) -> str:
    text = button.get("text") or {}
    if isinstance(text, dict):
        return str(text.get("text") or text.get("content") or "操作")
    return str(text or "操作")


def _action_name(button: dict[str, Any]) -> str:
    action_id = str(button.get("action_id") or button.get("name") or "")
    if action_id.endswith("_button"):
        action_id = action_id[: -len("_button")]
    return action_id or "action"


def _feishu_button(button: dict[str, Any]) -> dict[str, Any]:
    action = _action_name(button)
    raw_value = button.get("value")
    thread_id = ""
    if isinstance(raw_value, dict):
        thread_id = str(raw_value.get("thread_id") or raw_value.get("ticket_id") or "")
    else:
        thread_id = str(raw_value or "")
    value = {
        "action": action,
        "thread_id": thread_id,
        "ticket_id": thread_id,
    }
    kind = str(button.get("style") or "default")
    if kind not in {"primary", "danger", "default"}:
        kind = "default"
    return {
        "tag": "button",
        "name": action,
        "action_type": "form_submit",
        "text": {"tag": "lark_md", "content": _label_from_slack_button(button)},
        "type": kind,
        "value": value,
    }


def _form_element(buttons: list[dict[str, Any]], draft: str) -> dict[str, Any]:
    """Build an old-card-compatible form used by test-tenant callbacks."""
    elements: list[dict[str, Any]] = [
        {
            "tag": "input",
            "name": "edited_draft",
            "label": {"tag": "plain_text", "content": "回复草稿（可直接修改）"},
            "placeholder": {
                "tag": "plain_text",
                "content": "请输入要发送给客户的回复",
            },
            "value": draft,
            # The legacy Feishu input component rejects the 6000-character
            # limit used by the old Slack modal. Keep the editable draft
            # within the legacy card component's accepted range.
            "max_length": 1000,
        },
        {
            "tag": "input",
            "name": "reject_reason",
            "label": {"tag": "plain_text", "content": "拒绝原因（可选）"},
            "placeholder": {
                "tag": "plain_text",
                "content": "如果拒绝，请告诉 Agent 需要怎样修改",
            },
            "max_length": 1000,
        },
    ]
    elements.extend(_feishu_button(button) for button in buttons)
    return {"tag": "form", "name": "approval_form", "elements": elements}


def _slack_blocks_to_feishu_card(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert the graph's stable Block Kit-shaped payload to a Feishu card."""
    header = "HITL 客服工单"
    elements: list[dict[str, Any]] = []
    buttons: list[dict[str, Any]] = []
    draft = _draft_from_blocks(blocks)
    ticket_id = _ticket_id_from_blocks(blocks)

    for block in blocks:
        block_type = block.get("type")
        if block_type == "header":
            header = _text_from_slack_block(block) or header
            continue
        if block_type == "actions":
            buttons.extend(
                element for element in block.get("elements") or [] if isinstance(element, dict)
            )
            continue
        if block_type != "section":
            continue
        fields = block.get("fields") or []
        if fields:
            chunks = []
            for field in fields:
                text = _text_from_slack_block(field)
                chunks.append(text.replace("*", "**", 2) if text else "")
            content = "\n\n".join(chunk for chunk in chunks if chunk)
        else:
            content = _text_from_slack_block(block)
        if content:
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": content}})

    if buttons and ticket_id:
        elements.append(_form_element(buttons, draft))
    elif buttons:
        elements.append(
            {
                "tag": "action",
                "actions": [_feishu_button(button) for button in buttons],
            }
        )

    if not elements:
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": "审批消息已更新。"}}
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": header[:80]},
        },
        "elements": elements,
    }


def _card_content(blocks: list[dict[str, Any]]) -> str:
    return json.dumps(_slack_blocks_to_feishu_card(blocks), ensure_ascii=False, separators=(",", ":"))


def _resolve_receive_id(channel: str) -> str:
    receive_id = (channel or FEISHU_RECEIVE_ID).strip()
    # A route generated for legacy Slack can still be used in a one-chat Feishu
    # setup; prefer the explicit Feishu ID instead of sending to a '#channel'.
    if receive_id.startswith("#") and FEISHU_RECEIVE_ID:
        receive_id = FEISHU_RECEIVE_ID
    if not receive_id:
        raise RuntimeError(
            "FEISHU_RECEIVE_ID (or a configured approval channel) must be set."
        )
    return receive_id


def _check_success(response: dict[str, Any], operation: str) -> dict[str, Any]:
    if response.get("code") != 0:
        raise RuntimeError(f"Feishu {operation} failed: {response.get('msg', 'unknown error')}")
    data = response.get("data")
    return data if isinstance(data, dict) else {}


mcp = FastMCP(
    "support-feishu-write",
    instructions=(
        "APPROVAL WRITE server backed by Feishu Open Platform. Sends and updates "
        "interactive approval cards. Has no ability to send email or read CRM/KB data."
    ),
)


@mcp.tool()
async def post_approval_request(
    channel: str,
    blocks: list[dict[str, Any]],
    text: str = "New support ticket requires approval",
) -> dict[str, Any]:
    """Send an interactive approval card to a Feishu chat."""
    del text  # The interactive card itself is the notification payload.
    token = await _get_tenant_access_token()
    receive_id = _resolve_receive_id(channel)
    response = await _request_json(
        "POST",
        "/im/v1/messages",
        {
            "receive_id": receive_id,
            "msg_type": "interactive",
            "content": _card_content(blocks),
        },
        access_token=token,
        query={"receive_id_type": FEISHU_RECEIVE_ID_TYPE},
    )
    data = _check_success(response, "post_approval_request")
    message_id = str(data.get("message_id") or data.get("open_message_id") or "")
    if not message_id:
        raise RuntimeError("Feishu post response did not contain message_id")
    logger.info("post_approval_request: posted to receive_id=%s message_id=%s", receive_id, message_id)
    return {
        # The legacy field is an opaque message pointer; Feishu uses om_... IDs
        # instead of Slack timestamps.
        "slack_message_ts": message_id,
        "channel": receive_id,
        "ok": True,
    }


@mcp.tool()
async def update_message(
    channel: str,
    ts: str,
    blocks: list[dict[str, Any]],
    text: str = "Message updated",
) -> dict[str, Any]:
    """Replace a previously sent Feishu approval card in place."""
    del text  # Feishu card updates use the structured card content.
    token = await _get_tenant_access_token()
    message_id = (ts or "").strip()
    if not message_id:
        raise RuntimeError("Feishu message_id is required for update_message")
    response = await _request_json(
        "PATCH",
        f"/im/v1/messages/{message_id}",
        {"content": _card_content(blocks)},
        access_token=token,
    )
    data = _check_success(response, "update_message")
    return {
        "ok": True,
        "ts": str(data.get("message_id") or message_id),
        "channel": channel or FEISHU_RECEIVE_ID,
    }


@mcp.tool()
async def open_edit_modal(
    trigger_id: str,
    ticket_id: str,
    current_draft: str,
) -> dict[str, Any]:
    """Compatibility no-op; Feishu editing is handled by the card form."""
    del trigger_id, ticket_id, current_draft
    return {"ok": False, "view_id": ""}


if __name__ == "__main__":
    mcp.run(transport="stdio")
