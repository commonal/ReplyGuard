"""Send a real approval card to FEISHU_RECEIVE_ID, for eyeballing the render.

Run:  python scripts/send_live_test_card.py --kind short
      python scripts/send_live_test_card.py --kind long
      python scripts/send_live_test_card.py --kind all

SENDS A REAL MESSAGE to the chat in FEISHU_RECEIVE_ID. It sends no email and
touches no graph — it builds the card from a synthetic state and posts it through
the real MCP Feishu tool.

Why this is not a test: Feishu card rendering (quoting, line wrapping, the
truncation note, the form inputs, button styles) cannot be asserted in a unit
test. `tests/test_approval_card_customer_message.py` proves the block structure;
only a human looking at a phone can confirm it reads correctly.

Use `--kind long` to exercise the truncation path: the customer message is cut at
CUSTOMER_MESSAGE_CARD_LIMIT with a note naming the ticket. Confirm the note is
NOT inside the quote block — a quoted line reads as the customer's own words, and
attributing our footnote to them misleads the approver.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "mcp_server"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env", override=True)

import support_feishu_write as fw  # noqa: E402
from src.nodes import _build_approval_blocks  # noqa: E402

KB_QUOTE = (
    "4.2.1 Refunds Under $100: Support agents may approve refunds under $100 "
    "without additional authorisation."
)

_BASE = {
    "intent": "refund",
    "intent_confidence": 0.92,
    "draft_confidence": 0.48,
    "sentiment": "neutral",
    "risk_flags": ["financial"],
    "policy_matches": ["4.2.1"],
    "customer_tier": "Enterprise",
    "original_draft": "您好，已收到您的退款申请，正在为您处理。",
    "final_draft": "您好，已收到您的退款申请，正在为您处理。",
    "audit_log": [],
    "risk_level": "financial",
}

SHORT_MESSAGE = (
    "From: [EMAIL_1]\nSubject: 退款申请\n\n"
    "你好，我上个月购买了年费套餐，但是买错了版本，想申请退款。\n\n"
    "订单号是 ACME-2026-0912，金额 80 美元，在 100 美元以内。\n"
    "麻烦帮我处理一下，谢谢！"
)

# Deliberately much longer than CUSTOMER_MESSAGE_CARD_LIMIT so the cut is visible.
LONG_MESSAGE = (
    "From: [EMAIL_1]\nSubject: 很长的工单\n\n"
    + "\n".join(f"这是第 {i} 段说明，客户详细描述了问题的来龙去脉。" for i in range(1, 60))
)

KINDS = {
    "short": ("ticket-card-demo1", SHORT_MESSAGE, "customer message, normal length"),
    "long": ("ticket-card-demo2", LONG_MESSAGE, "customer message, over the card limit"),
    "empty": ("ticket-card-demo3", "", "no customer message (section must be absent)"),
}


async def _send(kind: str) -> None:
    ticket_id, message, note = KINDS[kind]
    state = {**_BASE, "ticket_id": ticket_id, "thread_id": ticket_id, "customer_message": message}

    blocks = _build_approval_blocks(state, KB_QUOTE)
    has_customer = any(
        "Customer message" in str(b.get("text", {}).get("text", "")) for b in blocks
    )
    print(f"[{kind}] {note}")
    print(f"    blocks={len(blocks)}  customer section present={has_customer}")

    result = await fw.post_approval_request(channel="", blocks=blocks, text=f"card probe: {kind}")
    print(f"    ok={result.get('ok')}  message_id={result.get('slack_message_ts')}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=[*KINDS, "all"], default="all")
    args = parser.parse_args()

    if not os.environ.get("FEISHU_APP_ID") or not os.environ.get("FEISHU_RECEIVE_ID"):
        print("ERROR: FEISHU_APP_ID / FEISHU_RECEIVE_ID not set — nothing to send to.")
        return 1

    kinds = list(KINDS) if args.kind == "all" else [args.kind]
    print("=" * 74)
    print(f"sending {len(kinds)} real card(s) to FEISHU_RECEIVE_ID")
    print("=" * 74)
    for kind in kinds:
        await _send(kind)
        print()
    print("Check Feishu: the customer text should appear above the draft reply, and")
    print("the truncation note (long card) should sit OUTSIDE the quote block.")
    return 0


raise SystemExit(asyncio.run(main()))
