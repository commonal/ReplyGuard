"""Reply-language contract for the drafter and critic.

The deployment serving Chinese-speaking customers had no language rule in any
drafting prompt. The prompts and the policy corpus are all English, so the
model took its cue from the prompt: a customer who wrote only "退款" got an
English reply, and a mixed Chinese/English message drifted to English.

Measured against the live model before the fix (5 inbound variants x 2 draft
paths):

    inbound            v4 drafter   v3 draft_response
    Chinese            CN           CN
    English            EN           EN
    Mixed CN+EN        EN  <- wrong EN  <- wrong
    Short Chinese      EN  <- wrong EN  <- wrong
    Short English      EN           EN

After the rule, all five mirror the customer's language on both paths.

These tests pin the contract in the prompts so it cannot be quietly dropped.
They assert on prompt text, not model output: the behaviour itself was verified
against the live API, and a test that calls it would need a key and be flaky.
"""

from __future__ import annotations

import pytest

from src.agents.base import load_prompt
from src.llm import DRAFT_SYSTEM

DRAFTER = load_prompt("drafter_system")
CRITIC = load_prompt("critic_system")

#: Every drafting prompt the graph can reach: v4 reads the markdown file, v3
#: carries an inline constant in src/llm.py.
DRAFT_PROMPTS = {
    "v4 drafter_system.md": DRAFTER,
    "v3 llm.DRAFT_SYSTEM": DRAFT_SYSTEM,
}


# --------------------------------------------------------------------------
# Both draft paths carry the rule
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(DRAFT_PROMPTS))
def test_draft_prompt_asks_for_the_customers_language(name: str) -> None:
    prompt = DRAFT_PROMPTS[name]
    assert "SAME language as the customer" in prompt


@pytest.mark.parametrize("name", sorted(DRAFT_PROMPTS))
def test_draft_prompt_does_not_let_short_messages_default_away(name: str) -> None:
    """The regression the first attempt caused: treating "short" as "unknown"
    sent a one-word English message ("refund") to the Chinese fallback."""
    prompt = DRAFT_PROMPTS[name]
    assert "however short" in prompt
    assert '"refund" is English' in prompt
    assert '"退款" is Chinese' in prompt


@pytest.mark.parametrize("name", sorted(DRAFT_PROMPTS))
def test_draft_prompt_names_the_english_pull_explicitly(name: str) -> None:
    """The prompt, policy quotes and JSON keys are English; the rule has to say
    that they must not drag the reply into English."""
    prompt = DRAFT_PROMPTS[name]
    assert "policy quotes" in prompt
    assert "must not pull the reply into English" in prompt


@pytest.mark.parametrize("name", sorted(DRAFT_PROMPTS))
def test_draft_prompt_bounds_the_chinese_fallback(name: str) -> None:
    prompt = DRAFT_PROMPTS[name]
    assert "Simplified Chinese" in prompt
    assert "cannot" in prompt and "be determined" in prompt
    # The fallback must be for contentless messages, not merely short ones.
    assert "emoji only" in prompt


@pytest.mark.parametrize("name", sorted(DRAFT_PROMPTS))
def test_draft_prompt_routes_mixed_input_to_chinese(name: str) -> None:
    prompt = DRAFT_PROMPTS[name]
    assert "Mixed Chinese and English" in prompt


@pytest.mark.parametrize("name", sorted(DRAFT_PROMPTS))
def test_draft_prompt_protects_placeholders_from_translation(name: str) -> None:
    """Restoring PII, or matching a policy quote, depends on the tokens coming
    back byte-identical."""
    prompt = DRAFT_PROMPTS[name]
    assert "[EMAIL_1]" in prompt
    assert "do not translate" in prompt.lower() or "Do not translate" in prompt


# --------------------------------------------------------------------------
# The critic must not fight the drafter
# --------------------------------------------------------------------------


def test_critic_treats_a_language_mismatch_as_a_defect() -> None:
    assert "DIFFERENT language from the customer" in CRITIC
    assert '"revise", not "accept"' in CRITIC


def test_critic_applies_the_same_short_message_rule() -> None:
    assert '"refund" is English' in CRITIC
    assert '"退款" is Chinese' in CRITIC


def test_critic_does_not_reject_a_reply_for_being_chinese() -> None:
    """Without this, the Critic's English tone rubric penalises valid Chinese
    drafts and the loop keeps rewriting them."""
    assert "draft's own language" in CRITIC
    assert "not mark a reply wrong merely for being written" in CRITIC


def test_critic_ignores_untranslated_policy_quotes_and_tokens() -> None:
    assert "policy quotes or PII tokens" in CRITIC


# --------------------------------------------------------------------------
# The rule is in the prompt, where it belongs
# --------------------------------------------------------------------------


def test_the_rule_is_not_only_in_the_critic() -> None:
    """A Critic rule alone would flag bad drafts without fixing them: the
    drafter is what actually decides the language."""
    for name, prompt in DRAFT_PROMPTS.items():
        assert "Language" in prompt, name
