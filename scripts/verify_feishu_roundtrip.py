"""Offline check of the Feishu approval round-trip.

Run:  python scripts/verify_feishu_roundtrip.py     (no credentials needed)

Builds the real graph approval card, converts it through the real MCP Feishu
converter, then feeds a realistic Feishu card-action callback through the real
handler payload parser, proving the thread_id survives the round trip.

That round trip is the step which can fail silently in production: if the
thread_id is lost anywhere in the chain, clicking Approve does nothing and only
a log warning is emitted. `tests/test_feishu_adapter.py` covers the pieces;
this covers them wired together, and is the one you want after touching the
card builder or the converter.

No network, no credentials, no message sent.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "mcp_server"))

TICKET = "ticket-verify1234abcd"
KB_QUOTE = "Refunds are processed within 7 business days of approval."
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(name)


# ------------------------------------------------------------- 1. build card
from src.nodes import _build_approval_blocks  # noqa: E402

print("=" * 74)
print("STEP 1 — graph builds the approval card")
print("=" * 74)

state = {
    "ticket_id": TICKET,
    "thread_id": TICKET,
    "intent": "refund",
    "intent_confidence": 0.91,
    "draft_confidence": 0.42,
    "sentiment": "angry",
    "risk_flags": ["financial", "angry_customer"],
    "policy_matches": ["4.2.1"],
    "customer_tier": "Enterprise",
    "customer_message": (
        "From: [EMAIL_1]\nSubject: 退款申请\n\n我上个月的年费套餐买错了版本，想申请退款。"
    ),
    "original_draft": "您好，已收到您的退款申请。",
    "final_draft": "您好，已收到您的退款申请。",
    "audit_log": [],
    "risk_level": "financial",
}

blocks = _build_approval_blocks(state, KB_QUOTE)
check("card built", bool(blocks), f"{len(blocks)} blocks")
json.dumps(blocks)
check("card is JSON-serialisable", True)

actions = [b for b in blocks if b.get("type") == "actions"]
check("card has exactly one actions block", len(actions) == 1, f"got {len(actions)}")
buttons = actions[0].get("elements", []) if actions else []
check("actions block has 3 buttons", len(buttons) == 3, f"got {len(buttons)}")
check(
    "actions block_id == ticket_id",
    bool(actions) and actions[0].get("block_id") == TICKET,
    f"block_id={actions[0].get('block_id') if actions else None!r}",
)
check(
    "button action_ids are approve/edit/reject",
    [b.get("action_id") for b in buttons] == ["approve_button", "edit_button", "reject_button"],
    f"{[b.get('action_id') for b in buttons]!r}",
)

# ------------------------------------------------------------ 2. conversion
print()
print("=" * 74)
print("STEP 2 — MCP Feishu server converts to an interactive card")
print("=" * 74)

from support_feishu_write import _slack_blocks_to_feishu_card  # noqa: E402

card = _slack_blocks_to_feishu_card(blocks)
check("card converted", isinstance(card, dict))
check("converted card is JSON-serialisable", isinstance(json.dumps(card, ensure_ascii=False), str))

form = next((el for el in card.get("elements", []) if el.get("tag") == "form"), None)
check("converted card contains a form", form is not None)

converted_buttons = [e for e in (form or {}).get("elements", []) if e.get("tag") == "button"]
check("converted card has 3 buttons", len(converted_buttons) == 3, f"got {len(converted_buttons)}")

form_inputs = [e for e in (form or {}).get("elements", []) if e.get("tag") == "input"]
check(
    "form exposes edited_draft + reject_reason inputs",
    [i.get("name") for i in form_inputs] == ["edited_draft", "reject_reason"],
    f"{[i.get('name') for i in form_inputs]!r}",
)

for b in converted_buttons:
    v = b.get("value")
    check(
        f"button {b.get('name')!r} carries thread_id",
        isinstance(v, dict) and v.get("thread_id") == TICKET,
        f"value={v!r}",
    )

# ------------------------------------------------------- 3. callback shape
print()
print("=" * 74)
print("STEP 3 — Feishu card-action callback: can we recover thread_id?")
print("=" * 74)

from src.config import settings  # noqa: E402
from src.feishu_handler import _resume_payload, handle_feishu_event  # noqa: E402

by_name = {b["name"]: b for b in converted_buttons}


def callback(btn: dict, form_values: dict | None = None, op: str = "ou_test_operator") -> dict:
    action: dict = {"value": btn["value"], "name": btn["name"]}
    if form_values:
        action["form_value"] = form_values
    return {
        "schema": "2.0",
        "header": {"event_type": "card.action.trigger", "token": settings.feishu_verification_token},
        "event": {"operator": {"operator_id": {"open_id": op}}, "action": action},
    }


tid, payload = _resume_payload(callback(by_name["approve"]))
check("approve: thread_id recovered", tid == TICKET, f"got {tid!r}")
check("approve: action == approve", payload.get("action") == "approve", f"{payload!r}")
check("approve: approver_id captured", payload.get("approver_id") == "ou_test_operator", f"{payload!r}")

tid_r, payload_r = _resume_payload(
    callback(by_name["reject"], {"reject_reason": "tone is too apologetic"}, op="ou_admin")
)
check("reject: thread_id recovered", tid_r == TICKET, f"got {tid_r!r}")
check("reject: action == reject", payload_r.get("action") == "reject", f"{payload_r!r}")
check("reject: reason carried", payload_r.get("reason") == "tone is too apologetic", f"{payload_r!r}")

tid_e, payload_e = _resume_payload(
    callback(by_name["edit"], {"edited_draft": "Rewritten reply text."})
)
check("edit: thread_id recovered", tid_e == TICKET, f"got {tid_e!r}")
check("edit: action == edit", payload_e.get("action") == "edit", f"{payload_e!r}")
check("edit: edited_draft carried", payload_e.get("edited_draft") == "Rewritten reply text.", f"{payload_e!r}")

# --------------------------------------------------------- 4. handler/security
print()
print("=" * 74)
print("STEP 4 — handler path and verification-token security")
print("=" * 74)


async def _run() -> None:
    ch = await handle_feishu_event(
        {"type": "url_verification", "challenge": "chal-abc", "token": settings.feishu_verification_token}
    )
    check("url_verification returns challenge", ch.get("challenge") == "chal-abc", f"{ch!r}")

    try:
        await handle_feishu_event({"type": "url_verification", "challenge": "x", "token": "wrong"})
        check("bad verification token rejected", False, "no exception raised")
    except PermissionError:
        check("bad verification token rejected", True)

    missing = callback(by_name["approve"])
    missing["event"]["action"]["value"] = {"action": "approve"}
    res = await handle_feishu_event(missing)
    check(
        "missing thread_id -> error toast (no silent resume)",
        res.get("toast", {}).get("type") == "error",
        f"{res!r}",
    )

    unknown = callback(by_name["approve"])
    unknown["event"]["action"]["value"] = {"action": "explode", "thread_id": TICKET}
    res2 = await handle_feishu_event(unknown)
    check("unknown action -> error toast", res2.get("toast", {}).get("type") == "error", f"{res2!r}")


asyncio.run(_run())

# ------------------------------------------------------------------- summary
print()
print("=" * 74)
if failures:
    print(f"RESULT: {len(failures)} CHECK(S) FAILED")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("RESULT: ALL CHECKS PASSED — Feishu approval round-trip is wired correctly.")
print("(offline: no live card sent, no credentials used)")
