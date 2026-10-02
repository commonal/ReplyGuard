"""What language does the drafter actually reply in?

Run:  python scripts/check_reply_language.py     (LIVE LLM — costs a few calls)

The drafter's contract is to mirror the customer's language and to fall back to
Simplified Chinese only when that language cannot be determined. Every other eval
dataset is English-only, so before the `language` set existed nothing caught a
Chinese ticket answered in English — that failure scored perfectly on intent
accuracy, escalation precision, false-auto-send rate and the quality rubric.

Two things this script does that `eval/run_experiments.py --dataset language`
cannot:

- It runs BOTH draft paths side by side. The v4 path reads
  `data/prompts/drafter_system.md`; the v3 path carries an inline `DRAFT_SYSTEM`
  in `src/llm.py`. A rule added to one and not the other is invisible to a
  single-path run.
- It prints the draft, so you can see *which* language came back rather than
  trusting a boolean.

`--reps` matters: a one-word message carries a weak language signal, and
`lang-t08` ("refund") answered in Chinese in roughly 1 of 5 full-graph runs
before the prompt carried an explicit short-message clause. A single pass does
not characterise these cases.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env", override=True)

from src.agents.drafter import drafter_node  # noqa: E402
from src.llm import draft_response  # noqa: E402
from eval.evaluators import detect_reply_language  # noqa: E402

POLICY = [
    "4.2.1 Refunds Under $100: Support agents may approve refunds under $100 "
    "without additional authorisation.",
    "7.2 Response Time SLA: Enterprise tickets receive an initial response "
    "within 4 business hours.",
]

#: (label, inbound body, expected reply language)
CASES: list[tuple[str, str, str]] = [
    ("Chinese, long", "你好，我上周购买的年费订阅想要退款，金额是 80 美元，请帮我处理一下，谢谢。", "zh"),
    ("English, long", "Hi, I would like a refund for my annual subscription. The amount is $80.", "en"),
    ("Mixed Chinese + English", "Hello 你好, I want a refund 退款 for my subscription 订阅 please 谢谢.", "zh"),
    ("Short Chinese", "退款", "zh"),
    ("Short English", "refund", "en"),
    ("Symbols only (genuine fallback)", "???!!!", "zh"),
]


def _state(body: str) -> dict:
    return {
        "ticket_id": "ticket-langprobe",
        "thread_id": "ticket-langprobe",
        "customer_message": f"From: customer@example.com\nSubject: probe\n\n{body}",
        "intent": "FAQ",
        "intent_confidence": 0.95,
        "customer_tier": "Enterprise",
        "customer_history": [],
        "policy_matches": POLICY,
        "audit_log": [],
        "draft_confidence": 0.0,
        "original_draft": "",
        "final_draft": "",
    }


async def _v4(body: str) -> str:
    update = await drafter_node(_state(body))
    return update.get("original_draft") or update.get("final_draft") or ""


async def _v3(body: str) -> str:
    result = await draft_response(
        _state(body)["customer_message"], "FAQ", {"tier": "Enterprise"}, [], POLICY
    )
    return getattr(result, "draft", "") or ""


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reps", type=int, default=1, help="repetitions per case")
    args = parser.parse_args()

    print("=" * 78)
    print(f"reply language per draft path ({args.reps} rep(s) each)")
    print("=" * 78)

    mismatches = 0
    for label, body, expected in CASES:
        got: list[str] = []
        head = ""
        for _ in range(args.reps):
            for fn in (_v4, _v3):
                draft = await fn(body)
                got.append(detect_reply_language(draft))
                if not head:
                    head = draft.strip().splitlines()[0][:58] if draft.strip() else "(empty)"
        ok = all(g == expected for g in got)
        mismatches += 0 if ok else 1
        mark = "OK  " if ok else "MISS"
        print(f"  [{mark}] {label:<32} expected={expected}  got={got}")
        print(f"         {head}")

    print()
    print("=" * 78)
    if mismatches:
        print(f"{mismatches} case(s) did not match. Expected [zh] for Chinese, mixed and")
        print("symbols-only; [en] for English, including a one-word English message.")
        return 1
    print("All cases mirror the customer's language on both draft paths.")
    return 0


raise SystemExit(asyncio.run(main()))
