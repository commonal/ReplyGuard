"""Feishu approval-card adapter tests without network credentials."""

from __future__ import annotations

import asyncio
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
async def test_encrypted_callback_fails_with_setup_hint() -> None:
    with pytest.raises(RuntimeError, match="FEISHU_ENCRYPT_KEY"):
        await feishu_handler.handle_feishu_event({"encrypt": "ciphertext"})


async def _noop_resume(thread_id: str, payload: dict[str, Any]) -> None:
    del thread_id, payload
