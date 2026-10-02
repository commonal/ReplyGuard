"""SLA escalation for unanswered approval requests.

Before this, `sla_deadline` was written into state and never read: there was no
timer and no sweep, so a ticket paused at `interrupt_gate` waited forever and
the documented "24h -> Manual Queue" path was unreachable. These tests cover the
pending index and the watchdog that closes that gap.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from src import approval_pending, sla_watchdog


@pytest.fixture(autouse=True)
def _isolated_index(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("APPROVAL_PENDING_DB_PATH", str(tmp_path / "pending.sqlite"))
    approval_pending._reset_for_tests()
    yield
    approval_pending._reset_for_tests()


def _ago(**kw: float) -> datetime:
    delta = timedelta(**kw)
    return datetime.now(UTC) - delta


# --------------------------------------------------------------------------
# The index
# --------------------------------------------------------------------------


def test_remember_then_list() -> None:
    approval_pending.remember(
        thread_id="t1",
        ticket_id="ticket-1",
        channel="oc_x",
        message_ts="om_1",
        deadline=datetime.now(UTC) + timedelta(hours=24),
        requested_at=datetime.now(UTC),
    )
    rows = approval_pending.all_pending()
    assert len(rows) == 1
    assert rows[0]["thread_id"] == "t1"
    assert rows[0]["ticket_id"] == "ticket-1"
    assert rows[0]["channel"] == "oc_x"


def test_remember_is_an_upsert_not_a_duplicate() -> None:
    """Re-prompts call remember again; that must refresh, not accumulate."""
    approval_pending.remember(thread_id="t1", ticket_id="ticket-1", deadline=_ago(hours=1))
    approval_pending.remember(
        thread_id="t1", ticket_id="ticket-1", deadline=datetime.now(UTC) + timedelta(hours=24)
    )
    rows = approval_pending.all_pending()
    assert len(rows) == 1
    assert approval_pending.expired() == []  # refreshed deadline is in the future


def test_forget_removes_the_row() -> None:
    approval_pending.remember(thread_id="t1", deadline=_ago(hours=1))
    assert len(approval_pending.all_pending()) == 1
    approval_pending.forget("t1")
    assert approval_pending.all_pending() == []


def test_forget_unknown_thread_is_a_no_op() -> None:
    approval_pending.forget("never-seen")
    assert approval_pending.all_pending() == []


def test_expired_selects_only_past_deadlines() -> None:
    approval_pending.remember(thread_id="past", deadline=_ago(minutes=1))
    approval_pending.remember(thread_id="future", deadline=datetime.now(UTC) + timedelta(hours=1))
    overdue = approval_pending.expired()
    assert [r["thread_id"] for r in overdue] == ["past"]


def test_row_without_deadline_is_never_expired() -> None:
    approval_pending.remember(thread_id="nodeadline")
    assert approval_pending.expired() == []


def test_blank_thread_id_is_ignored() -> None:
    approval_pending.remember(thread_id="", ticket_id="ticket-1")
    assert approval_pending.all_pending() == []


def test_deadline_may_be_a_naive_datetime() -> None:
    """A naive datetime is read as UTC, matching how it is stored."""
    naive_past = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None)
    approval_pending.remember(thread_id="naive", deadline=naive_past)
    assert [r["thread_id"] for r in approval_pending.expired()] == ["naive"]


# --------------------------------------------------------------------------
# The watchdog
# --------------------------------------------------------------------------


async def test_sweep_resumes_each_overdue_ticket_with_expire() -> None:
    approval_pending.remember(thread_id="late-1", ticket_id="ticket-1", deadline=_ago(minutes=5))
    approval_pending.remember(thread_id="late-2", ticket_id="ticket-2", deadline=_ago(minutes=1))
    approval_pending.remember(thread_id="fine", deadline=datetime.now(UTC) + timedelta(hours=1))

    calls: list[tuple[str, dict[str, Any]]] = []

    async def _resume(thread_id: str, value: dict[str, Any]) -> None:
        calls.append((thread_id, value))

    count = await sla_watchdog.sweep_once(_resume)

    assert count == 2
    assert sorted(t for t, _ in calls) == ["late-1", "late-2"]
    assert all(v == {"action": "expire"} for _, v in calls)


async def test_sweep_does_nothing_when_nothing_is_overdue() -> None:
    approval_pending.remember(thread_id="fine", deadline=datetime.now(UTC) + timedelta(hours=1))

    async def _resume(thread_id: str, value: dict[str, Any]) -> None:  # pragma: no cover
        raise AssertionError("should not resume a ticket inside its SLA window")

    assert await sla_watchdog.sweep_once(_resume) == 0


async def test_failed_resume_does_not_stop_the_sweep() -> None:
    """One unresumable thread must not block the others."""
    approval_pending.remember(thread_id="bad", deadline=_ago(minutes=5))
    approval_pending.remember(thread_id="good", deadline=_ago(minutes=5))

    seen: list[str] = []

    async def _resume(thread_id: str, value: dict[str, Any]) -> None:
        seen.append(thread_id)
        if thread_id == "bad":
            raise RuntimeError("checkpoint gone")

    count = await sla_watchdog.sweep_once(_resume)

    assert count == 2
    assert sorted(seen) == ["bad", "good"]
    # Both are dropped so the sweep does not retry an unresumable row forever.
    assert approval_pending.all_pending() == []


async def test_sweep_drops_rows_it_resumed() -> None:
    approval_pending.remember(thread_id="late", deadline=_ago(minutes=5))

    async def _resume(thread_id: str, value: dict[str, Any]) -> None:
        pass

    await sla_watchdog.sweep_once(_resume)
    assert approval_pending.all_pending() == []


async def test_run_forever_survives_a_failing_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop must keep going after an unexpected sweep error."""
    monkeypatch.setenv("SLA_SWEEP_INTERVAL_SEC", "1")
    calls = {"n": 0}

    async def _boom(resume: Any) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        raise asyncio.CancelledError

    monkeypatch.setattr(sla_watchdog, "sweep_once", _boom)

    async def _resume(thread_id: str, value: dict[str, Any]) -> None:  # pragma: no cover
        pass

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(sla_watchdog.run_forever(_resume), timeout=10)

    assert calls["n"] == 2  # survived the first failure and swept again


async def test_run_forever_exits_on_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLA_SWEEP_INTERVAL_SEC", "1")

    async def _resume(thread_id: str, value: dict[str, Any]) -> None:  # pragma: no cover
        pass

    task = asyncio.create_task(sla_watchdog.run_forever(_resume))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
