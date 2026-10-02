"""Manual Queue customer acknowledgement.

spec.md §Manual Queue requires that the customer is told their ticket is being
handled by a human. The node only updated the chat card, so a customer whose
ticket escalated saw silence — the worst possible outcome of giving up on
automation.

The notice is a fixed template (no LLM) and is skipped when the send itself
failed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from src import approval_pending
from src.nodes import manual_queue_node


@pytest.fixture(autouse=True)
def _isolated_index(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("APPROVAL_PENDING_DB_PATH", str(tmp_path / "pending.sqlite"))
    approval_pending._reset_for_tests()
    yield
    approval_pending._reset_for_tests()


class _Sent:
    sent_message_id = "om_ack_1"
    was_duplicate = False
    status = "sent"


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    email: str = "customer@example.com",
    send: Any = None,
    cleared: list[str] | None = None,
) -> Any:
    from src import nodes

    client = AsyncMock()
    client.email.send = send if send is not None else AsyncMock(return_value=_Sent())
    monkeypatch.setattr(nodes, "_client", lambda: client)
    monkeypatch.setattr(nodes, "_customer_email_from_audit", lambda state: email)
    if cleared is not None:
        monkeypatch.setattr(nodes, "_pii_clear_ticket", lambda tid: cleared.append(tid))
    return client


def _state(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ticket_id": "ticket-mq1",
        "thread_id": "ticket-mq1",
        "send_idempotency_key": "idem-mq1",
        "human_rejection_count": 3,
        "send_status": "sent",
        "audit_log": [],
        "customer_message": "From: a@b.c\nSubject: Refund please\n\nI want my money back.",
        "email_thread_id": "<orig@example.com>",
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------
# Recipient resolution happens before the PII vault is dropped
# --------------------------------------------------------------------------


async def test_recipient_is_resolved_before_pii_is_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clearing first would lose the real address, which lives in the vault."""
    order: list[str] = []
    from src import nodes

    client = AsyncMock()
    client.email.send = AsyncMock(side_effect=lambda params: order.append(f"send:{params.to}") or _Sent())

    monkeypatch.setattr(nodes, "_client", lambda: client)
    monkeypatch.setattr(
        nodes,
        "_customer_email_from_audit",
        lambda state: order.append("resolve") or "customer@example.com",
    )
    monkeypatch.setattr(nodes, "_pii_clear_ticket", lambda tid: order.append(f"clear:{tid}"))

    await manual_queue_node(_state())

    assert order.index("resolve") < order.index("clear:ticket-mq1")
    assert "send:customer@example.com" in order


# --------------------------------------------------------------------------
# The notice itself
# --------------------------------------------------------------------------


async def test_customer_is_notified_on_rejection_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _patch(monkeypatch)
    update = await manual_queue_node(_state())

    assert update["final_state"] == "rejected"
    params = client.email.send.call_args.args[0]
    assert params.to == "customer@example.com"
    assert params.subject == "Re: Refund please"
    assert "support team" in params.body
    assert params.in_reply_to == "<orig@example.com>"
    assert update["audit_log"][-1]["customer_notified"] is True


async def test_customer_is_notified_on_sla_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SLA path: no rejection, no send failure — just nobody answered."""
    client = _patch(monkeypatch)
    update = await manual_queue_node(_state(human_rejection_count=0, send_status="pending"))

    assert update["final_state"] == "expired"
    assert client.email.send.await_count == 1
    assert update["audit_log"][-1]["customer_notified"] is True


async def test_notice_uses_its_own_idempotency_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """It must not collide with a real reply attempt on the same ticket."""
    client = _patch(monkeypatch)
    await manual_queue_node(_state())
    params = client.email.send.call_args.args[0]
    assert params.idempotency_key == "idem-mq1:manual-queue-ack"
    assert params.idempotency_key != "idem-mq1"


async def test_notice_falls_back_to_ticket_id_without_a_send_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _patch(monkeypatch)
    await manual_queue_node(_state(send_idempotency_key=""))
    params = client.email.send.call_args.args[0]
    assert params.idempotency_key == "ticket-mq1:manual-queue-ack"


# --------------------------------------------------------------------------
# When NOT to notify
# --------------------------------------------------------------------------


async def test_failed_send_does_not_notify(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken send may have delivered partial mail; do not stack on it."""
    client = _patch(monkeypatch)
    update = await manual_queue_node(_state(send_status="failed_manual"))

    assert update["final_state"] == "failed_manual"
    assert client.email.send.await_count == 0
    assert update["audit_log"][-1]["customer_notified"] is False


async def test_unknown_recipient_does_not_notify(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _patch(monkeypatch, email="unknown@example.com")
    update = await manual_queue_node(_state())

    assert client.email.send.await_count == 0
    assert update["audit_log"][-1]["customer_notified"] is False


async def test_empty_recipient_does_not_notify(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _patch(monkeypatch, email="")
    await manual_queue_node(_state())
    assert client.email.send.await_count == 0


async def test_a_failing_notice_does_not_change_the_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The outcome is decided first; a courtesy mail must never alter it."""
    _patch(monkeypatch, send=AsyncMock(side_effect=OSError("smtp down")))
    update = await manual_queue_node(_state())

    assert update["final_state"] == "rejected"
    assert update["audit_log"][-1]["customer_notified"] is False


# --------------------------------------------------------------------------
# Housekeeping
# --------------------------------------------------------------------------


async def test_terminal_path_forgets_the_pending_row(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime, timedelta

    approval_pending.remember(
        thread_id="ticket-mq1",
        deadline=datetime.now(UTC) - timedelta(minutes=1),
    )
    _patch(monkeypatch)
    await manual_queue_node(_state())

    assert approval_pending.all_pending() == []


async def test_pii_is_cleared_on_the_terminal_path(monkeypatch: pytest.MonkeyPatch) -> None:
    cleared: list[str] = []
    _patch(monkeypatch, cleared=cleared)
    await manual_queue_node(_state())
    assert cleared == ["ticket-mq1"]
