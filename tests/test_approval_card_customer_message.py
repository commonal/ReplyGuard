"""The approval card must show what the customer actually wrote.

An approver's job is "is this draft a good answer to the customer's question?".
The card showed the ticket header, tier/intent, the recipient address, the pause
reasons, the KB quote, the draft, and the buttons — but never the customer's
message, so the reviewer had to leave the card and find the original mail. The
human-in-the-loop step could not be performed from the card.

The message shown is the redacted one from state (pii_redact_node replaced real
addresses with [EMAIL_n]); the reviewer needs the content, and the real address
is already on the card in "Reply will go to" so spoofing stays detectable.
"""

from __future__ import annotations

from typing import Any

from src.nodes import (
    _CUSTOMER_MESSAGE_CARD_LIMIT,
    _build_approval_blocks,
    _customer_message_block,
    _customer_message_body,
    _truncate_for_card,
)

KB = "4.2.1 Refunds Under $100: agents may approve refunds under $100."


def _state(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ticket_id": "ticket-card001",
        "thread_id": "ticket-card001",
        "intent": "refund",
        "intent_confidence": 0.92,
        "draft_confidence": 0.55,
        "sentiment": "neutral",
        "risk_flags": ["financial"],
        "policy_matches": ["4.2.1"],
        "customer_tier": "Enterprise",
        "original_draft": "您好，退款已受理。",
        "final_draft": "您好，退款已受理。",
        "audit_log": [],
        # The shape email_listener produces, after pii_redact_node.
        "customer_message": (
            "From: [EMAIL_1]\nSubject: Refund please\n\n"
            "你好，我上周买错了套餐，想申请退款，订单号是 12345。"
        ),
    }
    base.update(over)
    return base


def _sections(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [b for b in blocks if b.get("type") == "section"]


def _section_text(blocks: list[dict[str, Any]]) -> str:
    return "\n".join(b.get("text", {}).get("text", "") for b in _sections(blocks))


# --------------------------------------------------------------------------
# Header stripping
# --------------------------------------------------------------------------


def test_body_excludes_the_listener_header_lines() -> None:
    """From/Subject are redundant: the header carries the intent and the
    "Reply will go to" field carries the address."""
    body = _customer_message_body(_state())
    assert body == "你好，我上周买错了套餐，想申请退款，订单号是 12345。"
    assert "From:" not in body
    assert "Subject:" not in body


def test_body_keeps_a_paragraph_break_in_the_middle() -> None:
    state = _state(
        customer_message="From: a@b.c\nSubject: Two parts\n\nFirst paragraph.\n\nSecond paragraph."
    )
    assert _customer_message_body(state) == "First paragraph.\n\nSecond paragraph."


def test_body_untouched_when_there_is_no_header_block() -> None:
    state = _state(customer_message="Just a plain body with no headers at all.")
    assert _customer_message_body(state) == "Just a plain body with no headers at all."


def test_leading_blank_line_is_not_treated_as_a_header_block() -> None:
    state = _state(customer_message="\n\nBody after a blank line.")
    assert _customer_message_body(state) == "Body after a blank line."


def test_body_of_an_empty_message_is_empty() -> None:
    assert _customer_message_body(_state(customer_message="")) == ""
    assert _customer_message_body({}) == ""


# --------------------------------------------------------------------------
# Truncation
# --------------------------------------------------------------------------


def test_short_text_is_not_cut() -> None:
    text, cut = _truncate_for_card("short")
    assert text == "short"
    assert cut is False


def test_long_text_is_cut_and_reported() -> None:
    text, cut = _truncate_for_card("x" * (_CUSTOMER_MESSAGE_CARD_LIMIT + 500))
    assert cut is True
    assert len(text) <= _CUSTOMER_MESSAGE_CARD_LIMIT


def test_cut_prefers_a_line_boundary() -> None:
    lines = [f"line {i}" for i in range(400)]
    text, cut = _truncate_for_card("\n".join(lines))
    assert cut is True
    assert not text.endswith("lin")  # did not stop mid-word
    assert "\n" in text


def test_truncated_card_says_so_and_points_at_the_ticket() -> None:
    state = _state(customer_message="long line\n" * 300)
    block = _customer_message_block(state, "ticket-card001")
    assert "截断" in block
    assert "ticket-card001" in block


def test_truncation_note_is_not_quoted() -> None:
    """A quoted line reads as the customer's own words. Attributing our
    footnote to the customer would mislead the approver about what they sent."""
    state = _state(customer_message="long line\n" * 300)
    block = _customer_message_block(state, "ticket-card001")
    for line in block.splitlines():
        if "截断" in line:
            assert not line.startswith(">"), f"note is quoted: {line!r}"


def test_quoted_body_lines_still_use_the_quote_marker() -> None:
    """The note changing must not have dropped quoting from the actual body."""
    block = _customer_message_block(_state(), "t")
    assert "> 你好，我上周买错了套餐" in block


# --------------------------------------------------------------------------
# The block itself
# --------------------------------------------------------------------------


def test_customer_message_section_is_present_and_quoted() -> None:
    block = _customer_message_block(_state(), "ticket-card001")
    assert block.startswith("*Customer message*")
    assert "> 你好，我上周买错了套餐" in block


def test_blank_lines_stay_inside_the_quote() -> None:
    state = _state(customer_message="From: a@b.c\nSubject: s\n\none\n\ntwo")
    block = _customer_message_block(state, "t")
    assert "> one" in block
    assert "> two" in block
    assert ">\n" in block  # the gap is a quoted empty line, not a broken quote


def test_empty_message_produces_no_block() -> None:
    assert _customer_message_block(_state(customer_message=""), "t") == ""


def test_card_includes_the_customer_message() -> None:
    blocks = _build_approval_blocks(_state(), KB)
    text = _section_text(blocks)
    assert "*Customer message*" in text
    assert "想申请退款" in text


def test_card_omits_the_section_when_there_is_no_message() -> None:
    blocks = _build_approval_blocks(_state(customer_message=""), KB)
    assert "*Customer message*" not in _section_text(blocks)


def test_customer_message_comes_before_the_draft() -> None:
    """The card should read question-then-answer."""
    text = _section_text(_build_approval_blocks(_state(), KB))
    assert text.index("*Customer message*") < text.index("*Draft reply*")


def test_section_carries_a_distinct_block_id() -> None:
    """Slack rejects duplicate block_ids; the actions block owns the bare
    ticket_id."""
    blocks = _build_approval_blocks(_state(), KB)
    ids = [b.get("block_id") for b in blocks if b.get("block_id")]
    assert len(ids) == len(set(ids))
    assert "ticket-card001-customer" in ids
    assert "ticket-card001" in ids


def test_redacted_tokens_are_shown_not_the_real_address() -> None:
    """The body comes from redacted state, so placeholders appear as-is and the
    real address never lands in the quoted text. The reviewer judges content;
    the address is already on the card in "Reply will go to"."""
    body = _customer_message_body(_state())
    assert "[EMAIL_1]" not in body  # it is in the header, which is stripped
    assert "想申请退款" in body


def test_real_address_never_appears_in_the_body() -> None:
    """Even if a real address survived redaction into the header, the stripped
    header is what would have carried it — and the body is what we render."""
    state = _state(
        customer_message="From: alice@example.com\nSubject: s\n\n我的订单有问题"
    )
    block = _customer_message_block(state, "t")
    assert "alice@example.com" not in block
    assert "我的订单有问题" in block
