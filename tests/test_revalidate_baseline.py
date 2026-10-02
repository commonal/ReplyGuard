"""Regression tests for the approval elapsed-time baseline.

`route_after_action` decides whether a long approval pause needs a context
revalidation before sending. It measures the human's pause from the approval
prompt that was actually answered.

The defect these tests pin: the baseline used to be the FIRST
`slack_notification` entry in the audit log, and `summarize_changes` never
recorded a new one. So once a pause exceeded the threshold, the condition
stayed true forever and every re-approval re-entered `revalidate_context` —
even a reply seconds after the "Context changed" re-prompt.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from src.nodes import _latest_approval_prompt_ts, route_after_action

THRESHOLD = 15


def _iso(minutes_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()


def _state(
    *,
    clicked_minutes_ago: float,
    approval_requested_at: str | None = None,
    audit_log: list[dict[str, Any]] | None = None,
    status: str = "approve",
) -> dict[str, Any]:
    return {
        "ticket_id": "ticket-baseline01",
        "approval_status": status,
        "approval_timestamp": _iso(clicked_minutes_ago),
        **({"approval_requested_at": approval_requested_at} if approval_requested_at else {}),
        "audit_log": audit_log or [],
    }


@pytest.fixture(autouse=True)
def _fixed_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVALIDATE_THRESHOLD_MIN", str(THRESHOLD))


# --------------------------------------------------------------------------
# The core regression: a fast second decision must not re-revalidate.
# --------------------------------------------------------------------------


def test_fast_re_approval_after_re_prompt_goes_straight_to_finalize() -> None:
    """The bug: this returned 'revalidate_context' because the baseline was
    still the original notification, 20 minutes old."""
    state = _state(
        clicked_minutes_ago=0,          # human clicks almost immediately
        approval_requested_at=_iso(0),  # re-prompt was just posted
        audit_log=[
            # original notification, 20 minutes ago
            {"node": "slack_notification", "ts": _iso(20)},
            # context changed and the graph re-prompted a moment ago
            {"node": "summarize_changes", "ts": _iso(0)},
        ],
    )
    assert route_after_action(state) == "finalize"


def test_slow_second_decision_still_revalidates() -> None:
    """The guard must not be defeated: a genuinely slow second answer still
    triggers revalidation."""
    state = _state(
        clicked_minutes_ago=0,
        approval_requested_at=_iso(20),
        audit_log=[{"node": "slack_notification", "ts": _iso(40)}],
    )
    assert route_after_action(state) == "revalidate_context"


def test_first_decision_over_threshold_revalidates() -> None:
    """Unchanged behaviour for the first decision."""
    state = _state(
        clicked_minutes_ago=0,
        approval_requested_at=_iso(20),
        audit_log=[{"node": "slack_notification", "ts": _iso(20)}],
    )
    assert route_after_action(state) == "revalidate_context"


def test_first_decision_under_threshold_finalizes() -> None:
    state = _state(
        clicked_minutes_ago=0,
        approval_requested_at=_iso(5),
        audit_log=[{"node": "slack_notification", "ts": _iso(5)}],
    )
    assert route_after_action(state) == "finalize"


def test_just_under_threshold_finalizes() -> None:
    """A pause just under the threshold does not revalidate.

    The comparison is strictly greater-than, so a hair under the threshold
    finalizes. An exact-equality test would be a race: building the two
    timestamps takes a few milliseconds, which is enough to tip the
    difference just over the line.
    """
    state = _state(
        clicked_minutes_ago=0,
        approval_requested_at=_iso(THRESHOLD - 0.1),
        audit_log=[{"node": "slack_notification", "ts": _iso(THRESHOLD - 0.1)}],
    )
    assert route_after_action(state) == "finalize"


def test_just_over_threshold_revalidates() -> None:
    """A hair over the threshold does revalidate — the other side of the line."""
    state = _state(
        clicked_minutes_ago=0,
        approval_requested_at=_iso(THRESHOLD + 0.1),
        audit_log=[{"node": "slack_notification", "ts": _iso(THRESHOLD + 0.1)}],
    )
    assert route_after_action(state) == "revalidate_context"


# --------------------------------------------------------------------------
# Fallback for tickets checkpointed before approval_requested_at existed.
# --------------------------------------------------------------------------


def test_fallback_uses_the_latest_prompt_not_the_first() -> None:
    """Old checkpoints have no approval_requested_at. The fallback must still
    take the newest prompt; taking the first was the defect."""
    state = _state(
        clicked_minutes_ago=0,
        audit_log=[
            {"node": "slack_notification", "ts": _iso(45)},
            {"node": "revalidate_context", "ts": _iso(30)},
            {"node": "summarize_changes", "ts": _iso(1)},
        ],
    )
    assert _latest_approval_prompt_ts(state) == state["audit_log"][-1]["ts"]
    assert route_after_action(state) == "finalize"


def test_fallback_ignores_unrelated_audit_entries() -> None:
    state = _state(
        clicked_minutes_ago=0,
        audit_log=[
            {"node": "classify_intent", "ts": _iso(60)},
            {"node": "slack_notification", "ts": _iso(3)},
            {"node": "revalidate_context", "ts": _iso(1)},
        ],
    )
    assert _latest_approval_prompt_ts(state) == state["audit_log"][1]["ts"]


def test_explicit_field_wins_over_audit_log() -> None:
    """Once present, the explicit field is authoritative — the audit log may
    hold many older prompts."""
    state = _state(
        clicked_minutes_ago=0,
        approval_requested_at=_iso(1),
        audit_log=[{"node": "slack_notification", "ts": _iso(99)}],
    )
    assert route_after_action(state) == "finalize"


# --------------------------------------------------------------------------
# Other branches.
# --------------------------------------------------------------------------


def test_reject_short_circuits_before_any_timing() -> None:
    state = _state(clicked_minutes_ago=0, status="reject")
    assert route_after_action(state) == "reject_increment"


def test_missing_baseline_finalizes_rather_than_blocking() -> None:
    """Nothing to measure from: fail open, as before."""
    state = _state(clicked_minutes_ago=0, audit_log=[])
    assert _latest_approval_prompt_ts(state) is None
    assert route_after_action(state) == "finalize"


def test_missing_click_timestamp_finalizes() -> None:
    state = {
        "approval_status": "approve",
        "approval_requested_at": _iso(30),
        "audit_log": [{"node": "slack_notification", "ts": _iso(30)}],
    }
    assert route_after_action(state) == "finalize"


def test_unparsable_timestamp_finalizes() -> None:
    state = _state(
        clicked_minutes_ago=0,
        approval_requested_at="not-a-timestamp",
        audit_log=[{"node": "slack_notification", "ts": _iso(30)}],
    )
    assert route_after_action(state) == "finalize"
