"""Bounded context revalidation.

`revalidate_context -> summarize_changes -> interrupt_gate` re-pauses for a
fresh decision whenever the context changed during a long pause. Without a
bound, a context that keeps drifting re-runs the three MCP reads and the delta
LLM call forever. These tests pin the counter and the routing that stops it,
and pin that the counter only advances on rounds that actually found a change.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.llm import ContextDelta
from src.nodes import (
    revalidate_context_node,
    route_after_revalidate,
    summarize_changes_node,
)

MAX_REVALIDATIONS = 3


@pytest.fixture(autouse=True)
def _fixed_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_REVALIDATIONS", str(MAX_REVALIDATIONS))


# --------------------------------------------------------------------------
# route_after_revalidate
# --------------------------------------------------------------------------


def test_stable_context_ends_the_cycle() -> None:
    """No change means send: this is the normal termination."""
    assert route_after_revalidate({"_revalidate_changed": False, "revalidation_count": 0}) == "finalize"


def test_changed_context_re_prompts_below_the_limit() -> None:
    state = {"_revalidate_changed": True, "revalidation_count": MAX_REVALIDATIONS - 1}
    assert route_after_revalidate(state) == "summarize_changes"


def test_changed_context_at_the_limit_goes_to_manual_queue() -> None:
    state = {"_revalidate_changed": True, "revalidation_count": MAX_REVALIDATIONS}
    assert route_after_revalidate(state) == "manual_queue"


def test_changed_context_above_the_limit_stays_bounded() -> None:
    state = {"_revalidate_changed": True, "revalidation_count": MAX_REVALIDATIONS + 5}
    assert route_after_revalidate(state) == "manual_queue"


def test_missing_counter_is_treated_as_zero() -> None:
    """Old checkpoints have no revalidation_count; the first changed round
    must still be allowed to re-prompt."""
    assert route_after_revalidate({"_revalidate_changed": True}) == "summarize_changes"


def test_limit_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_REVALIDATIONS", "1")
    assert route_after_revalidate({"_revalidate_changed": True, "revalidation_count": 1}) == "manual_queue"
    assert route_after_revalidate({"_revalidate_changed": True, "revalidation_count": 0}) == "summarize_changes"


# --------------------------------------------------------------------------
# revalidate_context_node — the counter only advances on real changes
# --------------------------------------------------------------------------


def _fake_client() -> Any:
    profile = AsyncMock()
    profile.model_dump = lambda: {"tier": "Pro"}
    history: list[Any] = []
    kb = AsyncMock()
    kb.model_dump = lambda: {"sections": ["1.1"]}

    client = AsyncMock()
    client.read.get_crm_profile = AsyncMock(return_value=profile)
    client.read.get_customer_history = AsyncMock(return_value=history)
    client.read.get_kb_article = AsyncMock(return_value=kb)
    return client


async def test_unchanged_context_does_not_consume_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stable context ends the cycle by itself, so it must not burn a round."""
    from src import nodes

    monkeypatch.setattr(nodes, "_client", _fake_client)
    monkeypatch.setattr(nodes, "_customer_email_from_audit", lambda state: "a@b.c")
    monkeypatch.setattr(nodes, "_hash_context", lambda payload: "SAME-HASH")

    state = {"ticket_id": "t1", "customer_message": "hi", "context_hash": "SAME-HASH"}
    update = await revalidate_context_node(state)

    assert update["_revalidate_changed"] is False
    assert update["revalidation_count"] == 0
    assert route_after_revalidate({**state, **update}) == "finalize"


async def test_changed_context_consumes_one_round(monkeypatch: pytest.MonkeyPatch) -> None:
    from src import nodes

    monkeypatch.setattr(nodes, "_client", _fake_client)
    monkeypatch.setattr(nodes, "_customer_email_from_audit", lambda state: "a@b.c")
    monkeypatch.setattr(nodes, "_hash_context", lambda payload: "NEW-HASH")

    state = {"ticket_id": "t1", "customer_message": "hi", "context_hash": "OLD-HASH"}
    update = await revalidate_context_node(state)

    assert update["_revalidate_changed"] is True
    assert update["revalidation_count"] == 1


async def test_counter_accumulates_across_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    from src import nodes

    monkeypatch.setattr(nodes, "_client", _fake_client)
    monkeypatch.setattr(nodes, "_customer_email_from_audit", lambda state: "a@b.c")
    monkeypatch.setattr(nodes, "_hash_context", lambda payload: f"HASH-{id(object())}")

    state: dict[str, Any] = {
        "ticket_id": "t1",
        "customer_message": "hi",
        "context_hash": "OLD",
        "revalidation_count": 0,
    }
    for expected in (1, 2, 3):
        update = await revalidate_context_node(state)
        state.update(update)
        state["context_hash"] = "OLD"  # force a change every round
        assert update["revalidation_count"] == expected

    assert route_after_revalidate(state) == "manual_queue"


# --------------------------------------------------------------------------
# summarize_changes_node — must refresh the elapsed baseline and the SLA
# --------------------------------------------------------------------------


async def test_summarize_refreshes_baseline_and_sla(monkeypatch: pytest.MonkeyPatch) -> None:
    from src import nodes

    monkeypatch.setattr(
        nodes,
        "summarize_context_changes",
        AsyncMock(return_value=ContextDelta(has_changes=True, summary="tier changed")),
    )
    monkeypatch.setenv("SLA_DEADLINE_HOURS", "24")

    stale = (datetime.now(UTC) - timedelta(hours=30)).isoformat()
    state = {
        "ticket_id": "t1",
        "approval_status": "approved",
        "approval_requested_at": stale,
        "sla_deadline": datetime.now(UTC) - timedelta(hours=6),
        "audit_log": [],
    }
    update = await summarize_changes_node(state)

    # Re-prompt: back to pending, with the clock restarted.
    assert update["approval_status"] == "pending"
    assert update["approval_requested_at"] != stale
    assert datetime.fromisoformat(update["approval_requested_at"]) > datetime.now(UTC) - timedelta(minutes=1)
    assert update["sla_deadline"] > datetime.now(UTC) + timedelta(hours=23)


async def test_summarize_audit_entry_is_appended(monkeypatch: pytest.MonkeyPatch) -> None:
    from src import nodes

    monkeypatch.setattr(
        nodes,
        "summarize_context_changes",
        AsyncMock(return_value=ContextDelta(has_changes=True, summary="kb changed")),
    )
    state = {"ticket_id": "t1", "approval_status": "approved", "audit_log": []}
    update = await summarize_changes_node(state)

    entries = update["audit_log"]
    assert len(entries) == 1
    assert entries[0]["node"] == "summarize_changes"
    assert entries[0]["ts"]  # usable as a fallback baseline
