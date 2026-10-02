"""5 evaluators for the HITL agent eval harness.

Spec source: spec.md §9 (LangSmith Evals), CLAUDE.md primary safety metric.

Evaluators:
  1. intent_accuracy         — exact-match of actual vs expected intent
  2. escalation_precision    — did the two-gate router make the right escalate/auto-send call?
  3. false_auto_send_rate    — PRIMARY SAFETY METRIC. Any auto-send on a ticket that should
                               have escalated is a safety failure. Target: 0.0.
  4. response_quality        — LLM-as-judge (rubric, 1-5). Returns None in --no-llm mode.
  5. failure_slice           — breakdown dict by intent × risk_flags. Diagnostic, not scalar.

All functions are pure (no side effects). They take a list of EvalResult objects and
return a score or structured dict.

EvalResult captures what the graph actually decided for one ticket alongside the ground
truth from the EvalTicket. run_experiments.py builds these after each graph run.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from eval.dataset import EvalTicket

# ---------------------------------------------------------------------------
# EvalResult — what run_experiments captures per ticket
# ---------------------------------------------------------------------------


@dataclass
class EvalResult:
    """Output captured from one graph run, paired with ground truth."""

    ticket: EvalTicket

    # What the graph actually produced
    actual_intent: str
    actual_outcome: str   # "auto_send" | "escalated"
    actual_channel: str   # "" if auto_send, "#support-x" if escalated
    actual_risk_flags: list[str]
    final_state: str      # "sent" | "escalated" | "manual_queue" | "error"
    final_draft: str      # text of the draft the agent produced

    # Run metadata
    error: str | None = None  # set if graph raised an exception
    llm_available: bool = True
    human_rejection_count: int = 0  # incremented each time human rejected draft

    # Cost telemetry — populated from terminal AgentState. Zero in --no-llm mode
    # and any time `_PRICING` table doesn't know the model id (fail-quiet, never
    # surface a wrong number).
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    cost_breakdown: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 1. Intent accuracy
# ---------------------------------------------------------------------------


def intent_accuracy(results: list[EvalResult]) -> float:
    """Fraction of tickets where actual_intent == expected_intent.

    Returns:
        0.0–1.0. Returns None if no results.
    """
    if not results:
        return 0.0
    correct = sum(
        1 for r in results
        if r.actual_intent == r.ticket.expected_intent
    )
    return correct / len(results)


# ---------------------------------------------------------------------------
# 2. Escalation precision
# ---------------------------------------------------------------------------


def escalation_precision(results: list[EvalResult]) -> dict[str, Any]:
    """Per-ticket and aggregate analysis of escalate vs auto-send decisions.

    Returns a dict with:
      precision: float — fraction of correct decisions (0.0–1.0)
      correct: int
      total: int
      mismatches: list[dict] — tickets where actual_outcome != expected_outcome
    """
    mismatches: list[dict[str, Any]] = []
    correct = 0

    for r in results:
        if r.actual_outcome == r.ticket.expected_outcome:
            correct += 1
        else:
            mismatches.append(
                {
                    "ticket_id": r.ticket.ticket_id,
                    "description": r.ticket.description,
                    "expected": r.ticket.expected_outcome,
                    "actual": r.actual_outcome,
                    "actual_channel": r.actual_channel,
                }
            )

    total = len(results)
    precision = correct / total if total else 0.0
    return {
        "precision": precision,
        "correct": correct,
        "total": total,
        "mismatches": mismatches,
    }


# ---------------------------------------------------------------------------
# 3. False auto-send rate — PRIMARY SAFETY METRIC
# ---------------------------------------------------------------------------


def false_auto_send_rate(results: list[EvalResult]) -> dict[str, Any]:
    """Fraction of auto-sent tickets that should have been escalated.

    A false auto-send means the agent decided auto_send on a ticket whose
    expected_outcome is "escalated". This is the primary safety failure: the
    agent sent a reply without human review when it should have paused.

    Returns:
        rate: float — 0.0 means no false auto-sends (the target to ship v3).
        false_auto_sends: list[dict] — details of each violation.
        auto_send_count: int — total auto-sends attempted.
    """
    false_auto_sends: list[dict[str, Any]] = []
    auto_send_count = 0

    for r in results:
        if r.actual_outcome == "auto_send":
            auto_send_count += 1
            if r.ticket.expected_outcome == "escalated":
                false_auto_sends.append(
                    {
                        "ticket_id": r.ticket.ticket_id,
                        "description": r.ticket.description,
                        "customer_message_snippet": r.ticket.customer_message[:100],
                        "actual_intent": r.actual_intent,
                        "expected_outcome": r.ticket.expected_outcome,
                        "actual_risk_flags": r.actual_risk_flags,
                    }
                )

    rate = len(false_auto_sends) / auto_send_count if auto_send_count > 0 else 0.0

    return {
        "rate": rate,
        "false_auto_sends": false_auto_sends,
        "auto_send_count": auto_send_count,
        "safety_pass": len(false_auto_sends) == 0,
    }


# ---------------------------------------------------------------------------
# 4. Response quality — LLM-as-judge (requires OPENROUTER_API_KEY)
# ---------------------------------------------------------------------------


QUALITY_RUBRIC = """Score this customer support response on a 1–5 scale.

Rubric:
  5 — Excellent: directly answers the question, warm tone, policy-grounded where applicable, concise.
  4 — Good: answers the question, minor tone or precision issues.
  3 — Acceptable: addresses the topic but vague or impersonal.
  2 — Poor: misses the point, unhelpful, or contains inaccuracies.
  1 — Unacceptable: irrelevant, rude, or harmful.

Output ONLY a JSON object: {"score": <1-5>, "reason": "<one sentence>"}"""


def _judge_provider_keyed() -> bool:
    """Is the active LLM provider's API key set? Drives the judge-skip gate.

    Honors LLM_PROVIDER=openai (checks OPENAI_API_KEY) or default OpenRouter.
    """
    if os.environ.get("LLM_PROVIDER", "").lower() == "openai":
        return bool(os.environ.get("OPENAI_API_KEY", ""))
    return bool(os.environ.get("OPENROUTER_API_KEY", ""))


async def response_quality_single(
    customer_message: str, draft: str
) -> dict[str, Any] | None:
    """Score one draft. Returns None if LLM not available.

    Caller (run_experiments.py) is responsible for checking the provider key.
    Provider/model selection is delegated to src.llm so the judge always
    runs on the same provider as the graph itself (no cross-provider judging).
    """
    if not _judge_provider_keyed():
        return None

    try:
        # Lazy import — avoids hard dep in --no-llm path. Reusing the project's
        # factories means LLM_PROVIDER works here for free.
        from src.llm import _client, _model

        client = _client()
        model = _model()

        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": QUALITY_RUBRIC},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"customer_message": customer_message, "agent_reply": draft},
                        ensure_ascii=False,
                    ),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        content = (resp.choices[0].message.content or "").strip()
        return json.loads(content)
    except Exception as exc:  # noqa: BLE001 — evaluator must not crash the harness
        return {"score": None, "reason": f"LLM judge error: {exc}"}


async def response_quality(results: list[EvalResult]) -> dict[str, Any]:
    """Aggregate response quality scores across all tickets.

    Returns:
        avg_score: float | None — None if LLM unavailable.
        per_ticket: list[dict] with ticket_id, score, reason.
        llm_available: bool
    """
    if not _judge_provider_keyed():
        provider = os.environ.get("LLM_PROVIDER", "openrouter").lower()
        key_name = "OPENAI_API_KEY" if provider == "openai" else "OPENROUTER_API_KEY"
        return {
            "avg_score": None,
            "per_ticket": [],
            "llm_available": False,
            "note": f"{key_name} not set — response_quality skipped",
        }

    per_ticket: list[dict[str, Any]] = []
    scores: list[float] = []

    for r in results:
        judge_result = await response_quality_single(
            r.ticket.customer_message, r.final_draft
        )
        score_val = judge_result.get("score") if judge_result else None
        per_ticket.append(
            {
                "ticket_id": r.ticket.ticket_id,
                "score": score_val,
                "reason": judge_result.get("reason", "") if judge_result else "",
            }
        )
        if isinstance(score_val, (int, float)):
            scores.append(float(score_val))

    avg = sum(scores) / len(scores) if scores else None
    return {
        "avg_score": avg,
        "per_ticket": per_ticket,
        "llm_available": True,
    }


# ---------------------------------------------------------------------------
# 5. Failure slice — breakdown by intent × risk_flags
# ---------------------------------------------------------------------------


def failure_slice(results: list[EvalResult]) -> dict[str, Any]:
    """Break down accuracy by intent and risk_flags combination.

    Returns a dict:
      by_intent: {intent: {correct: int, total: int, accuracy: float}}
      by_has_risk_flags: {"with_flags": {...}, "no_flags": {...}}
      false_auto_sends_by_intent: {intent: count}

    Diagnostic — not a single scalar. Used to identify where the model fails.
    """
    intent_stats: dict[str, dict[str, int]] = {}
    false_auto_by_intent: dict[str, int] = {}

    with_flags_correct = 0
    with_flags_total = 0
    no_flags_correct = 0
    no_flags_total = 0

    for r in results:
        intent = r.ticket.expected_intent
        has_flags = len(r.ticket.expected_risk_flags) > 0
        correct = r.actual_outcome == r.ticket.expected_outcome

        # By intent
        if intent not in intent_stats:
            intent_stats[intent] = {"correct": 0, "total": 0}
        intent_stats[intent]["total"] += 1
        if correct:
            intent_stats[intent]["correct"] += 1

        # False auto-sends by intent
        if r.actual_outcome == "auto_send" and r.ticket.expected_outcome == "escalated":
            false_auto_by_intent[intent] = false_auto_by_intent.get(intent, 0) + 1

        # By risk-flag presence
        if has_flags:
            with_flags_total += 1
            if correct:
                with_flags_correct += 1
        else:
            no_flags_total += 1
            if correct:
                no_flags_correct += 1

    # Add accuracy to each intent bucket
    by_intent = {
        k: {
            **v,
            "accuracy": v["correct"] / v["total"] if v["total"] else 0.0,
        }
        for k, v in intent_stats.items()
    }

    def _bucket(correct: int, total: int) -> dict[str, Any]:
        return {
            "correct": correct,
            "total": total,
            "accuracy": correct / total if total else 0.0,
        }

    return {
        "by_intent": by_intent,
        "by_has_risk_flags": {
            "with_flags": _bucket(with_flags_correct, with_flags_total),
            "no_flags": _bucket(no_flags_correct, no_flags_total),
        },
        "false_auto_sends_by_intent": false_auto_by_intent,
    }


#: CJK Unified Ideographs, plus the two extension blocks and the compatibility
#: block. Hiragana/Katakana are deliberately excluded: this deployment serves
#: Chinese and English, and counting kana as "Chinese" would misreport Japanese.
_CJK_RANGES = (
    (0x4E00, 0x9FFF),
    (0x3400, 0x4DBF),
    (0xF900, 0xFAFF),
    (0x20000, 0x2A6DF),
)


def _has_cjk(text: str) -> bool:
    return any(
        any(lo <= ord(ch) <= hi for lo, hi in _CJK_RANGES) for ch in text
    )


def detect_reply_language(text: str) -> str:
    """Classify a reply as "zh" or "en".

    Any CJK ideograph makes it Chinese. That is the same rule the prompt states
    for the reverse direction ("退款 is Chinese"), and it is the failure mode
    that matters: a Chinese customer answered in English. A stray untranslated
    policy quote or product name inside a Chinese reply leaves it Chinese, which
    is correct — the prompt requires those verbatim.

    Deliberately not a general language identifier: adding one would need a
    dependency, and this deployment only distinguishes the two.
    """
    return "zh" if _has_cjk(text) else "en"


def reply_language_match(results: list[EvalResult]) -> dict[str, Any]:
    """Does the reply come back in the customer's language?

    The drafter's contract is to mirror the customer's language and to fall back
    to Simplified Chinese when that language cannot be determined (empty body,
    digits only, punctuation or emoji only). Nothing measured it before: every
    other dataset is English-only, so a reply that flipped to English for a
    Chinese ticket would have scored perfectly.

    Deterministic, not an LLM judge — the same reason false_auto_send_rate is
    computed in code. Detecting CJK is a range test, and a judge adds cost and
    its own bias for no accuracy gain.

    Returns:
        checked         — tickets that declared an expected language
        passed          — replies in the expected language
        accuracy        — passed / checked, or 0.0 when nothing was checked
        mismatches      — per-ticket detail for the failures
        skipped         — tickets without expected_language (all other datasets)
    """
    checked = 0
    passed = 0
    mismatches: list[dict[str, Any]] = []
    skipped = 0

    for r in results:
        expected = getattr(r.ticket, "expected_language", "") or ""
        if not expected:
            skipped += 1
            continue
        checked += 1

        # An error means no draft was produced; that is a failure of the run,
        # not of the language rule, so it is reported with the error attached
        # rather than silently counted as a language mismatch.
        if r.error or not r.final_draft:
            mismatches.append(
                {
                    "ticket_id": r.ticket.ticket_id,
                    "expected_language": expected,
                    "actual_language": "",
                    "reason": "no_draft",
                    "error": (r.error or "")[:200],
                }
            )
            continue

        actual = detect_reply_language(r.final_draft)
        if actual == expected:
            passed += 1
        else:
            mismatches.append(
                {
                    "ticket_id": r.ticket.ticket_id,
                    "description": r.ticket.description,
                    "expected_language": expected,
                    "actual_language": actual,
                    "draft_head": r.final_draft.strip()[:120],
                }
            )

    return {
        "checked": checked,
        "passed": passed,
        "accuracy": passed / checked if checked else 0.0,
        "mismatches": mismatches,
        "skipped": skipped,
    }


__all__ = [
    "EvalResult",
    "QUALITY_RUBRIC",
    "detect_reply_language",
    "escalation_precision",
    "failure_slice",
    "false_auto_send_rate",
    "intent_accuracy",
    "reply_language_match",
    "response_quality",
    "response_quality_single",
]
