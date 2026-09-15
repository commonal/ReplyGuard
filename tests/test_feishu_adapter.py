"""Feishu approval-card adapter tests without network credentials."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import pytest

from mcp_server import support_feishu_write as feishu_write
from src import feishu_handler


def _blocks() -> list[dict[str, Any]]:
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🟡 ticket-feishu-001 · refund"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": "*Tier*\nEnterprise"},
                {"type": "mrkdwn", "text": "*Intent*\nrefund (0.92)"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Draft reply*\n```您好，退款已受理。```"},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "action_id": "approve_button",
                    "value": "ticket-feishu-001",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Edit"},
                    "action_id": "edit_button",
                    "value": "ticket-feishu-001",
                },
                {
                    "type": "button",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "action_id": "reject_button",
                    "value": "ticket-feishu-001",
                },
            ],
        },
    ]


def test_block_kit_payload_becomes_feishu_form_card() -> None:
    card = feishu_write._slack_blocks_to_feishu_card(_blocks())

    assert card["header"]["title"]["content"].startswith("🟡 ticket-feishu-001")
    form = card["elements"][-1]
    assert form["tag"] == "form"
    assert {item.get("name") for item in form["elements"][-3:]} == {
        "approve",
        "edit",
        "reject",
    }
    assert form["elements"][0]["name"] == "edited_draft"
    assert form["elements"][0]["value"] == "您好，退款已受理。"


def test_feishu_receive_id_falls_back_to_configured_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feishu_write, "FEISHU_RECEIVE_ID", "oc_test_chat")
    assert feishu_write._resolve_receive_id("#support-refunds") == "oc_test_chat"


@pytest.mark.asyncio
async def test_url_verification_returns_challenge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feishu_handler.settings, "feishu_verification_token", "verify-me")

    result = await feishu_handler.handle_feishu_event(
        {"type": "url_verification", "token": "verify-me", "challenge": "challenge-1"}
    )

    assert result == {"challenge": "challenge-1"}


def test_legacy_card_action_http_callback_is_not_rejected_by_app_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old Feishu card callback has a message token, not app token."""
    from fastapi.testclient import TestClient

    from src import server

    async def fake_handle(_body: dict[str, Any]) -> dict[str, Any]:
        return {"toast": {"type": "success", "content": "已提交，系统处理中"}}

    monkeypatch.setattr(server.settings, "approval_provider", "feishu")
    monkeypatch.setattr(server.settings, "feishu_verification_token", "verify-me")
    monkeypatch.setattr(server.settings, "feishu_legacy_card_callback_compat", True)
    monkeypatch.setattr(server, "handle_feishu_event", fake_handle)

    payload = {
        "event": {
            "token": "c-message-update-token",
            "operator": {"operator_id": {"open_id": "ou_reviewer"}},
            "action": {
                "name": "approve",
                "value": {"action": "approve", "thread_id": "ticket-feishu-003"},
            },
        }
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    signature = hashlib.sha1(b"1700000000nonce-1verify-me" + body).hexdigest()
    response = TestClient(server.app).post(
        "/feishu/events",
        content=body,
        headers={
            "content-type": "application/json",
            "X-Lark-Request-Timestamp": "1700000000",
            "X-Lark-Request-Nonce": "nonce-1",
            "X-Lark-Signature": signature,
        },
    )

    assert response.status_code == 200
    assert response.json()["toast"]["type"] == "success"


def test_legacy_top_level_card_action_http_callback_is_not_rejected_by_app_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Some old Feishu form-card callbacks put action at the top level."""
    from fastapi.testclient import TestClient

    from src import server

    async def fake_handle(_body: dict[str, Any]) -> dict[str, Any]:
        return {"toast": {"type": "success", "content": "已提交，系统处理中"}}

    monkeypatch.setattr(server.settings, "approval_provider", "feishu")
    monkeypatch.setattr(server.settings, "feishu_verification_token", "verify-me")
    monkeypatch.setattr(server.settings, "feishu_legacy_card_callback_compat", True)
    monkeypatch.setattr(server, "handle_feishu_event", fake_handle)

    payload = {
        "token": "c-message-update-token",
        "open_id": "ou_reviewer",
        "action": {
            "name": "approve",
            "value": {"action": "approve", "thread_id": "ticket-feishu-004"},
        },
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    signature = hashlib.sha1(b"1700000000nonce-1verify-me" + body).hexdigest()
    response = TestClient(server.app).post(
        "/feishu/events",
        content=body,
        headers={
            "content-type": "application/json",
            "X-Lark-Request-Timestamp": "1700000000",
            "X-Lark-Request-Nonce": "nonce-1",
            "X-Lark-Signature": signature,
        },
    )

    assert response.status_code == 200
    assert response.json()["toast"]["type"] == "success"


@pytest.mark.asyncio
async def test_card_action_resumes_graph_without_blocking_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resumed: list[tuple[str, dict[str, Any]]] = []

    async def fake_resume(thread_id: str, payload: dict[str, Any]) -> None:
        resumed.append((thread_id, payload))

    monkeypatch.setattr(feishu_handler, "_resume_graph", fake_resume)
    result = await feishu_handler.handle_feishu_event(
        {
            "event": {
                "operator": {"operator_id": {"open_id": "ou_reviewer"}},
                "action": {
                    "name": "edit",
                    "value": {"action": "edit", "thread_id": "ticket-feishu-001"},
                    "form_value": {"edited_draft": "修改后的回复", "reject_reason": ""},
                },
            }
        }
    )
    await asyncio.sleep(0)

    assert result["toast"]["type"] == "success"
    assert resumed == [
        (
            "ticket-feishu-001",
            {
                "action": "edit",
                "approver_id": "ou_reviewer",
                "edited_draft": "修改后的回复",
            },
        )
    ]


@pytest.mark.asyncio
async def test_card_message_token_is_not_confused_with_app_verification_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(feishu_handler.settings, "feishu_verification_token", "verify-me")
    monkeypatch.setattr(feishu_handler, "_resume_graph", _noop_resume)

    result = await feishu_handler.handle_feishu_event(
        {
            "header": {"token": "verify-me"},
            "event": {
                "token": "c-message-update-token",
                "action": {
                    "name": "approve",
                    "value": {"action": "approve", "thread_id": "ticket-feishu-002"},
                },
            },
        }
    )
    await asyncio.sleep(0)

    assert result["toast"]["type"] == "success"


@pytest.mark.asyncio
async def test_top_level_legacy_card_action_uses_top_level_operator_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resumed: list[tuple[str, dict[str, Any]]] = []

    async def fake_resume(thread_id: str, payload: dict[str, Any]) -> None:
        resumed.append((thread_id, payload))

    monkeypatch.setattr(feishu_handler, "_resume_graph", fake_resume)
    result = await feishu_handler.handle_feishu_event(
        {
            "token": "c-message-update-token",
            "open_id": "ou_top_level_reviewer",
            "action": {
                "name": "approve",
                "value": {"action": "approve", "thread_id": "ticket-feishu-004"},
            },
        }
    )
    await asyncio.sleep(0)

    assert result["toast"]["type"] == "success"
    assert resumed == [
        (
            "ticket-feishu-004",
            {"action": "approve", "approver_id": "ou_top_level_reviewer"},
        )
    ]


@pytest.mark.asyncio
async def test_encrypted_callback_fails_with_setup_hint() -> None:
    with pytest.raises(RuntimeError, match="FEISHU_ENCRYPT_KEY"):
        await feishu_handler.handle_feishu_event({"encrypt": "ciphertext"})


async def _noop_resume(thread_id: str, payload: dict[str, Any]) -> None:
    del thread_id, payload
