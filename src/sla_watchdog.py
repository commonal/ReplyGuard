"""SLA watchdog — escalates approval requests nobody answered.

`docs/architecture.md` and `spec.md` both promise that a ticket with no human
response by `sla_deadline` auto-escalates to Manual Queue. Nothing implemented
that: `sla_deadline` was written into state and never read, and no timer or
sweep existed, so a paused ticket waited forever.

This task closes that gap. It sweeps `src.approval_pending` for rows whose
deadline has passed and resumes each one with `{"action": "expire"}`, which
`route_after_action` routes to `manual_queue`.

Resuming (rather than writing state directly) is required: the ticket is parked
inside `interrupt()`, so the graph can only advance through a resume.

Shaped after `src.email_listener.listen_forever`: a plain loop with a sleep,
started as a background task from the server lifespan.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable

from src import approval_pending

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 60


def _interval_sec() -> int:
    try:
        return max(1, int(os.environ.get("SLA_SWEEP_INTERVAL_SEC", str(DEFAULT_INTERVAL_SEC))))
    except ValueError:
        return DEFAULT_INTERVAL_SEC


async def sweep_once(resume: Callable[[str, dict[str, Any]], Awaitable[None]]) -> int:
    """Expire every overdue pending approval. Returns how many were resumed."""
    overdue = approval_pending.expired()
    for row in overdue:
        thread_id = row.get("thread_id") or ""
        if not thread_id:
            continue
        log.warning(
            "SLA expired for ticket %s (requested %s, deadline %s) — escalating to manual queue",
            row.get("ticket_id") or thread_id,
            row.get("requested_at") or "?",
            row.get("deadline") or "?",
        )
        try:
            await resume(thread_id, {"action": "expire"})
        except Exception as exc:  # noqa: BLE001 — one bad ticket must not stop the sweep
            log.exception("SLA escalation failed for %s: %s", thread_id, exc)
        finally:
            # Drop the row either way. On success the ticket has moved on; on
            # failure retrying forever would keep an unresumable row alive with
            # no way out.
            approval_pending.forget(thread_id)
    return len(overdue)


async def run_forever(resume: Callable[[str, dict[str, Any]], Awaitable[None]]) -> None:
    """Sweep on an interval until cancelled.

    The first sweep waits one interval: a ticket cannot be overdue at startup
    unless the process was down past its deadline, and those are caught by the
    next tick anyway.
    """
    log.info("SLA watchdog started (interval %ss)", _interval_sec())
    while True:
        try:
            await asyncio.sleep(_interval_sec())
        except asyncio.CancelledError:
            log.info("SLA watchdog stopped")
            raise
        try:
            await sweep_once(resume)
        except asyncio.CancelledError:
            log.info("SLA watchdog stopped")
            raise
        except Exception:  # noqa: BLE001 — the loop must survive a bad sweep
            log.exception("SLA sweep failed; continuing")

__all__ = ["run_forever", "sweep_once"]
