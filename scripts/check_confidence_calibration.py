"""Is Gate 2's 0.85 threshold calibrated to what the models actually emit?

Run:  python scripts/check_confidence_calibration.py   (LIVE LLM)

Gate 2 escalates whenever `intent_confidence` OR `draft_confidence` is below
0.85. Measuring the curated set live showed intent scores clustering at 0.90-0.96
while draft scores sat at 0.55-0.60 — so Gate 2 was firing on the draft score
almost always, and the auto-send path was being decided by a number the prompt
never defined.

This reproduces that diagnostic: print both distributions and how many tickets
clear each threshold, so a gate that reads "0.85" but behaves as "reject nearly
everything" is visible as such.

Re-run it whenever the drafter prompt, the model, or CONFIDENCE_THRESHOLD
changes. A threshold tuned against one model's output distribution does not
transfer: archived results show 2/10 curated tickets auto-sending under one
model and 0/10 under another, from the same code.

Offline by design — it calls the classifier and the drafter directly rather than
through the graph, so it needs no MCP router, no checkpointer and no Feishu.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env", override=True)

from eval.dataset import EVAL_TICKETS  # noqa: E402
from src.llm import classify_intent, draft_response  # noqa: E402
from src.policy import (  # noqa: E402
    CONFIDENCE_THRESHOLD as TH,
    gate_one_policy_risk,
    gate_two_confidence,
)

#: The two curated tickets the dataset expects to auto-send.
AUTO_SEND_EXPECTED = ("eval-t01", "eval-t07")


async def _draft_v4(message: str, state: dict) -> tuple[str, float]:
    from src.agents.drafter import drafter_node

    update = await drafter_node(state)
    return (
        update.get("original_draft") or update.get("final_draft") or "",
        float(update.get("draft_confidence", 0.0)),
    )


async def _draft_v3(message: str, state: dict) -> tuple[str, float]:
    result = await draft_response(
        message,
        state.get("intent", "other"),
        {"tier": state.get("customer_tier", "Free")},
        state.get("customer_history") or [],
        state.get("policy_matches") or [],
    )
    return result.draft, float(result.draft_confidence)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="probe every curated ticket, not just the two")
    parser.add_argument("--v3", action="store_true", help="use the v3 single-agent draft path")
    args = parser.parse_args()

    draft_fn = _draft_v3 if args.v3 else _draft_v4
    tickets = EVAL_TICKETS if args.all else [t for t in EVAL_TICKETS if t.ticket_id in AUTO_SEND_EXPECTED]

    print("=" * 84)
    print(f"draft path: {'v3 draft_response' if args.v3 else 'v4 drafter_node'}   threshold={TH}")
    print("=" * 84)
    print()
    print(f"{'ticket':<10} {'intent':<18} {'ic':>5} {'dc':>5}  gate1  gate2   decision")
    print("-" * 84)

    ics: list[float] = []
    dcs: list[float] = []
    for t in tickets:
        c = await classify_intent(t.customer_message)
        state = {
            "ticket_id": t.ticket_id,
            "thread_id": t.ticket_id,
            "customer_message": t.customer_message,
            "intent": c.intent,
            "intent_confidence": c.intent_confidence,
            "sentiment": c.sentiment,
            "risk_flags": list(c.risk_flags),
            "risk_level": c.risk_level,
            "customer_tier": "Free",
            "customer_history": [],
            "policy_matches": [],
            "audit_log": [],
            "original_draft": "",
            "final_draft": "",
            "draft_confidence": 0.0,
        }
        draft, dc = await draft_fn(t.customer_message, state)
        state["draft_confidence"] = dc
        state["final_draft"] = draft

        g1 = gate_one_policy_risk(state)
        g2 = gate_two_confidence(state)
        auto = (not g1) and (not g2)
        ics.append(c.intent_confidence)
        dcs.append(dc)
        print(
            f"{t.ticket_id:<10} {c.intent:<18} {c.intent_confidence:>5.2f} {dc:>5.2f}  "
            f"{str(g1):<5}  {str(g2):<5}  {'auto_send' if auto else 'escalate'}"
        )
        if t.ticket_id in AUTO_SEND_EXPECTED and not auto:
            why = []
            if g1:
                why.append("gate1_policy_risk")
            if g2:
                why.append(f"gate2_confidence(ic={c.intent_confidence:.2f}, dc={dc:.2f})")
            print(f"{'':<10} -> dataset expects auto_send; blocked by {', '.join(why)}")

    print("-" * 84)

    def _line(name: str, xs: list[float]) -> str:
        return (
            f"{name:<18} min={min(xs):.2f}  median={statistics.median(xs):.2f}  max={max(xs):.2f}   "
            f"clear >= {TH}: {sum(1 for x in xs if x >= TH)}/{len(xs)}"
        )

    print(_line("intent_confidence", ics))
    print(_line("draft_confidence", dcs))
    print()
    if sum(1 for x in dcs if x >= TH) < len(dcs) / 2:
        print("NOTE: most drafts sit below the threshold. On this evidence Gate 2 is not a")
        print("'low confidence' filter — it rejects nearly every ticket, so the auto-send")
        print("path is decided by a signal the drafter prompt has to define explicitly.")
    return 0


raise SystemExit(asyncio.run(main()))
