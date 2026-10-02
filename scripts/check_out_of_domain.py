"""Does the classifier separate out-of-domain requests from in-domain ones?

Run:  python scripts/check_out_of_domain.py     (LIVE LLM)

Run this after touching `CLASSIFY_SYSTEM` or the out-of-domain gate. The failure
it guards against is not "the classifier is unsure" but "the classifier is
confident and wrong": ACME sells software subscriptions, so shipping and
order-tracking questions are out of scope, and the classifier used to label them
`info` — factually true, they do ask for information — while `info` is in
AUTO_SEND_SAFE_INTENTS. `bitext27-t16` ("checking order status") auto-sent on
every run because of it.

Two halves, and both matter:

- Out-of-domain cases must be labelled `out_of_domain` AND escalate through
  Gate 1, at whatever confidence the model reports. Escalating only because the
  intent is absent from the safe set is not enough; that path depends on
  `intent_confidence`, and a confident mistake is exactly the leak.
- In-domain cases must keep auto-sending. A gate that escalates everything is
  "safe" and useless.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env", override=True)

from src.llm import classify_intent  # noqa: E402
from src.policy import (  # noqa: E402
    OUT_OF_DOMAIN_INTENT,
    gate_one_policy_risk,
    should_auto_send,
)

#: (label, message, expected label, expected to escalate?)
CASES: list[tuple[str, str, str, bool]] = [
    # --- out of scope for a SaaS product ---
    ("out: order tracking", "checking order status", OUT_OF_DOMAIN_INTENT, True),
    ("out: delivery options", "could you help me check what delivery methods you offer?", OUT_OF_DOMAIN_INTENT, True),
    ("out: delivery timing", "need help seeing when my package is going to arrive", OUT_OF_DOMAIN_INTENT, True),
    ("out: place an order", "I have to order something, can I get some help?", OUT_OF_DOMAIN_INTENT, True),
    ("out: shipping address", "give me information about a delivery address modification", OUT_OF_DOMAIN_INTENT, True),
    # --- in scope, and these must stay auto-sendable ---
    ("in: support hours", "wat are ur support hours on weekends?", "info", False),
    ("in: change email", "how do i change the email adress on my acount?", "FAQ", False),
    ("in: export report", "cant find the buton to export my report to csv", "basic_technical", False),
    ("in: double charge", "i think i got charged twice for last month, can u check", "billing", True),
    ("in: refund request", "I want a $200 refund — the laptop arrived damaged.", "refund", True),
]


async def main() -> int:
    print("=" * 90)
    print("out-of-domain separation — live classifier + Gate 1")
    print("=" * 90)
    print()
    print(f"{'case':<24} {'expected':<16} {'got':<16} {'conf':>5}  {'esc':>5}  verdict")
    print("-" * 90)

    failures: list[str] = []
    for label, message, expected_intent, expected_escalate in CASES:
        result = await classify_intent(message)
        state = {
            "ticket_id": "probe",
            "thread_id": "probe",
            "customer_message": message,
            "intent": result.intent,
            "intent_confidence": result.intent_confidence,
            "sentiment": result.sentiment,
            "risk_flags": list(result.risk_flags),
            "risk_level": result.risk_level,
            "policy_matches": [],
            "draft_confidence": 1.0,  # neutralise Gate 2: test the label, not the draft
        }
        escalates = gate_one_policy_risk(state) or not should_auto_send(state)

        label_ok = result.intent == expected_intent
        escalate_ok = escalates == expected_escalate
        ok = label_ok and escalate_ok
        if not ok:
            failures.append(label)
        print(
            f"{label:<24} {expected_intent:<16} {result.intent:<16} "
            f"{result.intent_confidence:>5.2f}  {str(escalates):>5}  "
            f"{'OK' if ok else 'MISMATCH'}"
        )

    print("-" * 90)
    print()
    if failures:
        print(f"{len(failures)} mismatch(es): {failures}")
        print()
        print("If the 'out:' cases came back as info, CLASSIFY_SYSTEM's rule that")
        print("out_of_domain beats info is not landing — check the label definition")
        print("and the two shipping/order few-shot examples.")
        print("If the 'in:' cases escalate, the gate is too broad and is costing")
        print("human reviews on questions the system can answer.")
        return 1
    print("Out-of-domain requests are separated and escalated; in-domain questions")
    print("still auto-send. Gate 1 escalates on the label, not on confidence.")
    return 0


raise SystemExit(asyncio.run(main()))
