"""The reply-language evaluator and its dataset.

`reply_language_match` is the only thing that measures whether a reply comes
back in the customer's language. Before it existed, every dataset was
English-only, so a Chinese ticket answered in English scored perfectly on every
metric the harness had.

Deterministic by design — same reason `false_auto_send_rate` is: detecting CJK
is a range test, and an LLM judge would add cost and its own bias for no
accuracy gain.
"""

from __future__ import annotations

from typing import Any

import pytest

from eval.dataset import EvalTicket
from eval.evaluators import (
    EvalResult,
    detect_reply_language,
    reply_language_match,
)
from eval.language_dataset import LANGUAGE_TICKETS


def _ticket(ticket_id: str, expected_language: str = "") -> EvalTicket:
    return EvalTicket(
        ticket_id=ticket_id,
        description="probe",
        customer_message="From: a@b.c\nSubject: s\n\nbody",
        customer_email="a@b.c",
        expected_intent="FAQ",
        expected_outcome="escalated",
        expected_channel="#support-technical",
        expected_risk_flags=[],
        canned_classification={
            "intent": "FAQ",
            "intent_confidence": 0.9,
            "sentiment": "neutral",
            "risk_flags": [],
            "risk_level": "none",
        },
        canned_draft={"draft": "x", "draft_confidence": 0.9},
        expected_language=expected_language,
    )


def _result(
    ticket_id: str,
    final_draft: str,
    expected_language: str = "",
    error: str | None = None,
) -> EvalResult:
    return EvalResult(
        ticket=_ticket(ticket_id, expected_language),
        actual_intent="FAQ",
        actual_outcome="escalated",
        actual_channel="#support-technical",
        actual_risk_flags=[],
        final_state="sent",
        final_draft=final_draft,
        error=error,
    )


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("您好，退款已受理。", "zh"),
        ("Hi there, your refund is processed.", "en"),
        ("退款", "zh"),
        ("refund", "en"),
        ("[EMAIL_1] processed", "en"),
        ("", "en"),
        # An untranslated policy quote inside a Chinese reply leaves it Chinese,
        # which is what the prompt requires (quotes stay verbatim).
        ("您好，per ACME policy 4.2.1 we approved your refund.", "zh"),
        # Mixed input is Chinese by contract.
        ("您好 Hello 混排", "zh"),
    ],
)
def test_detection(text: str, expected: str) -> None:
    assert detect_reply_language(text) == expected


def test_emoji_only_is_not_chinese_detection_but_fallback() -> None:
    """Detection reports what the text is; the Chinese fallback is the prompt's
    job, not the detector's. Emoji carry no script."""
    assert detect_reply_language("???!!!") == "en"
    assert detect_reply_language("💸💔") == "en"


def test_kana_is_not_counted_as_chinese() -> None:
    """This deployment serves Chinese and English; counting kana as Chinese
    would misreport a Japanese reply as a pass."""
    assert detect_reply_language("こんにちは") == "en"


# --------------------------------------------------------------------------
# The metric
# --------------------------------------------------------------------------


def test_matching_replies_pass() -> None:
    report = reply_language_match(
        [
            _result("t1", "您好，已受理。", "zh"),
            _result("t2", "Hi, done.", "en"),
        ]
    )
    assert report["checked"] == 2
    assert report["passed"] == 2
    assert report["accuracy"] == 1.0
    assert report["mismatches"] == []


def test_chinese_ticket_answered_in_english_is_a_mismatch() -> None:
    """The exact failure the set exists to catch."""
    report = reply_language_match([_result("t1", "Hi, your refund is processed.", "zh")])
    assert report["passed"] == 0
    assert report["accuracy"] == 0.0
    assert report["mismatches"][0]["ticket_id"] == "t1"
    assert report["mismatches"][0]["expected_language"] == "zh"
    assert report["mismatches"][0]["actual_language"] == "en"


def test_english_ticket_answered_in_chinese_is_a_mismatch() -> None:
    report = reply_language_match([_result("t1", "您好，已处理。", "en")])
    assert report["mismatches"][0]["actual_language"] == "zh"


def test_tickets_without_an_expected_language_are_skipped() -> None:
    """Every other dataset leaves the field empty; they must be untouched."""
    report = reply_language_match(
        [
            _result("t1", "Hi.", ""),
            _result("t2", "您好。", ""),
            _result("t3", "Hi.", "en"),
        ]
    )
    assert report["checked"] == 1
    assert report["passed"] == 1
    assert report["skipped"] == 2


def test_nothing_checked_reports_zero_accuracy_not_a_pass() -> None:
    report = reply_language_match([_result("t1", "Hi.", "")])
    assert report["checked"] == 0
    assert report["accuracy"] == 0.0


def test_missing_draft_is_reported_as_no_draft() -> None:
    """An error or empty draft is a run failure, not a language mismatch; it is
    labelled so the two are not conflated."""
    report = reply_language_match([_result("t1", "", "zh", error="boom")])
    assert report["passed"] == 0
    assert report["mismatches"][0]["reason"] == "no_draft"


def test_empty_result_list_is_safe() -> None:
    report = reply_language_match([])
    assert report["checked"] == 0
    assert report["accuracy"] == 0.0


def test_mismatch_detail_is_enough_to_diagnose() -> None:
    report = reply_language_match([_result("t1", "Hi there, all done.", "zh")])
    detail = report["mismatches"][0]
    assert detail["description"] == "probe"
    assert "Hi there" in detail["draft_head"]


# --------------------------------------------------------------------------
# The dataset itself
# --------------------------------------------------------------------------


def test_dataset_ids_are_unique() -> None:
    ids = [t.ticket_id for t in LANGUAGE_TICKETS]
    assert len(ids) == len(set(ids))


def test_every_dataset_ticket_declares_a_language() -> None:
    for ticket in LANGUAGE_TICKETS:
        assert ticket.expected_language in {"zh", "en"}, ticket.ticket_id


def test_dataset_covers_both_languages_and_the_mixed_case() -> None:
    langs = {t.expected_language for t in LANGUAGE_TICKETS}
    assert langs == {"zh", "en"}
    descriptions = " ".join(t.description for t in LANGUAGE_TICKETS)
    assert "Mixed Chinese + English" in descriptions
    assert "Short English" in descriptions
    assert "Short Chinese" in descriptions


def test_dataset_does_not_claim_auto_send() -> None:
    """The first version declared auto_send for every ticket and the live run
    escalated all eight: on the v4 path the draft score does not clear Gate 2.
    This set measures language, not routing — routing is measured by the curated
    and Bitext sets."""
    for ticket in LANGUAGE_TICKETS:
        assert ticket.expected_outcome == "escalated", ticket.ticket_id


def test_dataset_intents_are_auto_send_safe() -> None:
    """So the run needs no approval channel or SMTP when the score does clear."""
    from src.policy import AUTO_SEND_SAFE_INTENTS

    for ticket in LANGUAGE_TICKETS:
        assert ticket.expected_intent in AUTO_SEND_SAFE_INTENTS, ticket.ticket_id


def test_canned_drafts_match_their_declared_language() -> None:
    """A canned draft in the wrong language would make a --no-llm run lie."""
    for ticket in LANGUAGE_TICKETS:
        draft: Any = ticket.canned_draft["draft"]
        assert detect_reply_language(draft) == ticket.expected_language, ticket.ticket_id
