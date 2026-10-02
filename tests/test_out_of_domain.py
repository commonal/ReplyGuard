"""Out-of-domain requests must escalate, whatever the confidence.

The failure this pins: ACME sells software subscriptions, so shipping, delivery
and order-tracking questions are out of scope. The classifier mapped them to
"info" — factually true, they do ask for information — and "info" is in
AUTO_SEND_SAFE_INTENTS. So a confidently mislabelled out-of-domain question
auto-sent a reply drafted from a policy corpus that never mentions shipping.

`bitext27-t16` ("checking order status") reproduced this on every run:
auto-send, expected escalated, `risk_flags` empty so Gate 1 did not fire either.
Escalation by "not in the safe set" is not enough on its own — that relies on
intent_confidence, and this was a confident mistake. The gate has to fire on the
label itself.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.policy import (
    AUTO_SEND_SAFE_INTENTS,
    EDGE_CASE_INTENTS,
    OUT_OF_DOMAIN_INTENT,
    gate_one_policy_risk,
    gate_two_confidence,
    should_auto_send,
)


def _state(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ticket_id": "t",
        "intent": "info",
        "intent_confidence": 0.95,
        "draft_confidence": 0.95,
        "sentiment": "neutral",
        # Deliberately no money or legal keyword: Gate 1 must fire on the label.
        "customer_message": "checking order status",
        "risk_flags": [],
        "risk_level": "none",
        "policy_matches": [],
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------
# The label itself is the signal
# --------------------------------------------------------------------------


def test_out_of_domain_escalates_with_high_confidence() -> None:
    """The regression: high confidence used to be enough to auto-send."""
    state = _state(intent=OUT_OF_DOMAIN_INTENT, intent_confidence=0.99)
    assert gate_one_policy_risk(state) is True
    assert should_auto_send(state) is False


def test_out_of_domain_survives_every_other_check_passing() -> None:
    """No keyword, no policy match, no angry sentiment, both scores high — the
    label alone must stop it."""
    state = _state(
        intent=OUT_OF_DOMAIN_INTENT,
        intent_confidence=1.0,
        draft_confidence=1.0,
        sentiment="positive",
        customer_message="what delivery options do you have",
        policy_matches=[],
    )
    assert gate_two_confidence(state) is False
    assert gate_one_policy_risk(state) is True
    assert should_auto_send(state) is False


def test_out_of_domain_flag_is_recorded() -> None:
    """The audit trail has to name why the ticket was held."""
    state = _state(intent=OUT_OF_DOMAIN_INTENT)
    gate_one_policy_risk(state)
    assert "out_of_domain" in state["risk_flags"]


def test_out_of_domain_does_not_claim_financial_risk() -> None:
    """Being out of scope is not a money problem; mislabelling the risk level
    would send the reviewer looking for a charge that does not exist."""
    state = _state(intent=OUT_OF_DOMAIN_INTENT)
    gate_one_policy_risk(state)
    assert state["risk_level"] == "none"


def test_out_of_domain_is_not_in_the_auto_send_safe_set() -> None:
    assert OUT_OF_DOMAIN_INTENT not in AUTO_SEND_SAFE_INTENTS


def test_out_of_domain_is_kept_out_of_edge_case_intents() -> None:
    """It is handled by its own check; adding it to EDGE_CASE_INTENTS would
    force risk_level="financial" on a non-financial ticket."""
    assert OUT_OF_DOMAIN_INTENT not in EDGE_CASE_INTENTS


# --------------------------------------------------------------------------
# No collateral: genuinely in-domain tickets are unaffected
# --------------------------------------------------------------------------


def test_in_domain_info_still_auto_sends() -> None:
    """The fix must not turn a benign factual question into a human review."""
    state = _state(intent="info", customer_message="what are your support hours?")
    assert gate_one_policy_risk(state) is False
    assert should_auto_send(state) is True


def test_in_domain_faq_still_auto_sends() -> None:
    state = _state(
        intent="FAQ",
        customer_message="how do i change the email address on my account?",
    )
    assert should_auto_send(state) is True


def test_out_of_domain_flag_does_not_leak_into_the_next_ticket() -> None:
    """_append_flag mutates the list it is given; each ticket carries its own."""
    first = _state(intent=OUT_OF_DOMAIN_INTENT)
    second = _state(intent="info", customer_message="what are your support hours?")
    gate_one_policy_risk(first)
    gate_one_policy_risk(second)
    assert first["risk_flags"] == ["out_of_domain"]
    assert second["risk_flags"] == []


# --------------------------------------------------------------------------
# The classifier prompt carries the label and the rule
# --------------------------------------------------------------------------


def _classify_prompt() -> str:
    from src.llm import CLASSIFY_SYSTEM

    return CLASSIFY_SYSTEM


def test_classifier_lists_the_new_label() -> None:
    assert "out_of_domain" in _classify_prompt()


def test_classifier_defines_scarcity_of_scope() -> None:
    prompt = _classify_prompt()
    for term in ("shipping", "delivery", "order tracking"):
        assert term in prompt, term


def test_classifier_rule_beats_info() -> None:
    """Without this the model keeps picking "info" for shipping questions."""
    assert '"out_of_domain" beats "info"' in _classify_prompt()


def test_classifier_says_not_to_soften_confidence() -> None:
    assert "confidently" in _classify_prompt()


def test_classification_result_description_lists_the_label() -> None:
    """The field description is part of the model-facing contract."""
    from src.llm import ClassificationResult

    description = ClassificationResult.model_fields["intent"].description or ""
    assert "out_of_domain" in description
