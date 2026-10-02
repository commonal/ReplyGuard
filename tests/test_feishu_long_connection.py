"""Long-connection Feishu approval: SDK payload -> resume payload.

The long-connection path must produce exactly the resume payload the webhook
path produces, because both delegate to ``src.feishu_handler._resume_payload``.
These tests pin that contract using real ``lark_oapi`` callback model objects,
so a change in the SDK shape or the adapter is caught here.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.config import settings
from src.feishu_handler import _resume_payload
from src.feishu_ws import sdk_payload_to_body

lark = pytest.importorskip("lark_oapi")

from lark_oapi.event.callback.model.p2_card_action_trigger import (  # noqa: E402
    P2CardActionTrigger,
)

TICKET = "ticket-lconn0001"


def _event(
    name: str,
    value: dict[str, Any] | None = None,
    form_value: dict[str, Any] | None = None,
    open_id: str = "ou_approver",
) -> P2CardActionTrigger:
    """Build a P2CardActionTrigger the way the SDK delivers one.

    Nested fields are passed as dicts because the generated models unmarshal
    through ``lark_oapi.core.construct.parse``, which expects a Mapping for a
    typed sub-object.
    """
    action: dict[str, Any] = {
        "name": name,
        "tag": "button",
        "value": value if value is not None else {"action": name, "thread_id": TICKET},
    }
    if form_value:
        action["form_value"] = form_value

    return P2CardActionTrigger(
        {
            "event": {
                "operator": {"open_id": open_id, "user_id": "", "union_id": ""},
                "token": settings.feishu_verification_token,
                "action": action,
            }
        }
    )


def test_operator_object_is_parsed_by_the_sdk() -> None:
    """Guard the assumption that the SDK hands us real model objects."""
    event = _event("approve")
    assert event.event is not None
    assert event.event.operator is not None
    assert event.event.operator.open_id == "ou_approver"
    assert event.event.action is not None
    assert event.event.action.value == {"action": "approve", "thread_id": TICKET}


def test_operator_and_thread_id_survive_the_conversion() -> None:
    body = sdk_payload_to_body(_event("approve"))
    assert body["event"]["operator"]["operator_id"]["open_id"] == "ou_approver"
    thread_id, resume = _resume_payload(body)
    assert thread_id == TICKET
    assert resume == {"action": "approve", "approver_id": "ou_approver"}


def test_verification_token_is_carried_into_the_header() -> None:
    body = sdk_payload_to_body(_event("approve"))
    assert body["header"]["token"] == settings.feishu_verification_token


def test_edit_action_carries_the_edited_draft() -> None:
    body = sdk_payload_to_body(
        _event(
            "edit",
            value={"action": "edit", "thread_id": TICKET},
            form_value={"edited_draft": "Rewritten reply."},
        )
    )
    thread_id, resume = _resume_payload(body)
    assert thread_id == TICKET
    assert resume["action"] == "edit"
    assert resume["edited_draft"] == "Rewritten reply."


def test_reject_action_carries_the_reason() -> None:
    body = sdk_payload_to_body(
        _event(
            "reject",
            value={"action": "reject", "thread_id": TICKET},
            form_value={"reject_reason": "tone is wrong"},
        )
    )
    thread_id, resume = _resume_payload(body)
    assert thread_id == TICKET
    assert resume["action"] == "reject"
    assert resume["reason"] == "tone is wrong"


def test_ticket_id_key_is_accepted_as_well_as_thread_id() -> None:
    body = sdk_payload_to_body(
        _event("approve", value={"action": "approve", "ticket_id": TICKET})
    )
    thread_id, _ = _resume_payload(body)
    assert thread_id == TICKET


def test_missing_thread_id_is_reported_not_silently_dropped() -> None:
    """A click with no ticket reference must not look like a successful resume."""
    body = sdk_payload_to_body(_event("approve", value={"action": "approve"}))
    thread_id, resume = _resume_payload(body)
    assert thread_id == ""
    assert resume["action"] == "approve"


def test_absent_form_value_does_not_break_parsing() -> None:
    body = sdk_payload_to_body(_event("approve"))
    assert "form_value" not in body["event"]["action"]
    thread_id, resume = _resume_payload(body)
    assert thread_id == TICKET
    assert "edited_draft" not in resume


def test_missing_action_object_is_tolerated() -> None:
    """A malformed callback must not raise out of the adapter."""
    event = P2CardActionTrigger({"event": {"operator": {"open_id": "ou_x"}}})
    body = sdk_payload_to_body(event)
    thread_id, resume = _resume_payload(body)
    assert thread_id == ""
    assert resume["approver_id"] == "ou_x"
    assert resume["action"] == ""  # unrecognised by the caller, which returns a toast


def test_unregistered_loop_refuses_to_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an app loop the resume must be refused, not scheduled nowhere."""
    import src.graph_runner as graph_runner
    from src import feishu_ws

    called: list[Any] = []

    async def _fake_resume(*args: Any, **kwargs: Any) -> None:  # pragma: no cover
        called.append(args)

    monkeypatch.setattr(graph_runner, "resume", _fake_resume)
    monkeypatch.setattr(feishu_ws, "_app_loop", None)

    # Returns without scheduling: there is no loop to run the coroutine on.
    assert feishu_ws._schedule_resume(TICKET, {"action": "approve"}) is None
    assert called == []
