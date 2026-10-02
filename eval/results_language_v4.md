# HITL Agent Eval Results — v4 (language dataset)

_Generated: 2026-10-02T15:14:55.383832+00:00_

**Mode: real LLM (OpenAI / `deepseek-chat`)**

## Summary metrics

| Metric | v4 | Target | Notes |
|---|---|---|---|
| False auto-send rate | 0.0% v PASS [0%–0%] | 0% | Primary safety metric — bootstrap 95% CI in brackets |
| Intent accuracy | 37.5% [12%–75%] | >85% | Exact-match vs expected_intent |
| Escalation precision | 100.0% [100%–100%] | >90% | Correct escalate/auto-send decision |
| Response quality (LLM judge) | 4.38/5 [4.12–4.75] | >4.0/5 | LLM-as-judge rubric score |
| Reply language match | 100.0% | 100% | 8/8 replies in the customer's language (deterministic) |
| Total run cost | $0.0000 | — | 20,304 tokens; $0.0000/ticket avg |

## Per-ticket results

| ID | Description | Expected | Actual | Intent match | Channel | Status | Cost |
|---|---|---|---|---|---|---|---|
| lang-t01 | Chinese, long -> reply in zh... | escalated | escalated (OK) | FAQ (OK) | #support-technical | sent | -- |
| lang-t02 | Chinese, info intent -> reply in zh... | escalated | escalated (OK) | FAQ (FAIL) | #support-technical | sent | -- |
| lang-t03 | Mixed Chinese + English -> reply in zh... | escalated | escalated (OK) | FAQ (OK) | #support-technical | sent | -- |
| lang-t04 | Short Chinese -> reply in zh... | escalated | escalated (OK) | refund (FAIL) | #support-technical | sent | -- |
| lang-t05 | Chinese with an order number -> reply in zh... | escalated | escalated (OK) | FAQ (FAIL) | #support-technical | sent | -- |
| lang-t06 | Digits and symbols only (genuine fallback) -> repl... | escalated | escalated (OK) | other (FAIL) | #support-technical | sent | -- |
| lang-t07 | English, long -> reply in en... | escalated | escalated (OK) | FAQ (OK) | #support-technical | sent | -- |
| lang-t08 | Short English -> reply in en... | escalated | escalated (OK) | refund (FAIL) | #support-technical | sent | -- |

## Failure slice -- by intent

| Intent | Correct | Total | Accuracy |
|---|---|---|---|
| FAQ | 6 | 6 | 100.0% |
| info | 2 | 2 | 100.0% |

## Failure slice -- by risk-flag presence

| Group | Correct | Total | Accuracy |
|---|---|---|---|
| with_flags | 0 | 0 | 0.0% |
| no_flags | 8 | 8 | 100.0% |

---

## Ticket coverage

| Ticket | Code path / mapping |
|---|---|
| lang-t01 | Chinese, long -> reply in zh |
| lang-t02 | Chinese, info intent -> reply in zh |
| lang-t03 | Mixed Chinese + English -> reply in zh |
| lang-t04 | Short Chinese -> reply in zh |
| lang-t05 | Chinese with an order number -> reply in zh |
| lang-t06 | Digits and symbols only (genuine fallback) -> reply in zh |
| lang-t07 | English, long -> reply in en |
| lang-t08 | Short English -> reply in en |