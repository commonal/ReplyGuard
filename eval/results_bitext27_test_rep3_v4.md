# HITL Agent Eval Results — v4 (bitext27_test_rep3 dataset)

_Generated: 2026-10-02T15:52:56.380857+00:00_

**Mode: real LLM (OpenAI / `deepseek-chat`)**

## Summary metrics

| Metric | v4 | Target | Notes |
|---|---|---|---|
| False auto-send rate | 0.0% v PASS [0%–0%] | 0% | Primary safety metric — bootstrap 95% CI in brackets |
| Intent accuracy | 45.0% [25%–65%] | >85% | Exact-match vs expected_intent |
| Escalation precision | 75.0% [55%–90%] | >90% | Correct escalate/auto-send decision |
| Response quality (LLM judge) | 4.15/5 [3.90–4.40] | >4.0/5 | LLM-as-judge rubric score |
| Total run cost | $0.0000 | — | 62,995 tokens; $0.0000/ticket avg |

## Per-ticket results

| ID | Description | Expected | Actual | Intent match | Channel | Status | Cost |
|---|---|---|---|---|---|---|---|
| bitext27-t01 | Bitext:newsletter_subscription -> FAQ (newsletter ... | auto_send | escalated (FAIL) | FAQ (OK) | #support-technical | sent | -- |
| bitext27-t02 | Bitext:edit_account -> FAQ (account-detail change ... | auto_send | escalated (FAIL) | FAQ (OK) | #support-technical | sent | -- |
| bitext27-t03 | Bitext:switch_account -> FAQ (account switching — ... | auto_send | escalated (FAIL) | billing (FAIL) | #support-technical | sent | -- |
| bitext27-t04 | Bitext:delete_account -> other (account deletion —... | escalated | escalated (OK) | FAQ (FAIL) | #support-technical | sent | -- |
| bitext27-t05 | Bitext:get_refund -> refund (refund request — mone... | escalated | escalated (OK) | refund (OK) | #support-refunds | sent | -- |
| bitext27-t06 | Bitext:check_refund_policy -> refund (refund-polic... | escalated | escalated (OK) | info (FAIL) | #support-refunds | sent | -- |
| bitext27-t07 | Bitext:track_refund -> refund (refund status — ref... | escalated | escalated (OK) | refund (OK) | #support-refunds | sent | -- |
| bitext27-t08 | Bitext:payment_issue -> billing (payment failure —... | escalated | escalated (OK) | billing (OK) | #support-technical | sent | -- |
| bitext27-t09 | Bitext:complaint -> complaint (complaint — edge-ca... | escalated | escalated (OK) | complaint (OK) | #support-complaints | sent | -- |
| bitext27-t10 | Bitext:review -> other (leaving a review — edge-ca... | escalated | escalated (OK) | info (FAIL) | #support-technical | sent | -- |
| bitext27-t11 | Bitext:contact_human_agent -> other (explicit huma... | escalated | escalated (OK) | other (OK) | #support-technical | sent | -- |
| bitext27-t12 | Bitext:contact_customer_service -> other (request ... | escalated | escalated (OK) | other (OK) | #support-technical | sent | -- |
| bitext27-t13 | Bitext:cancel_order -> billing (order cancellation... | escalated | escalated (OK) | billing (OK) | #support-technical | sent | -- |
| bitext27-t14 | Bitext:change_order -> other (order change — no Sa... | escalated | escalated (OK) | out_of_domain (FAIL) | #support-technical | sent | -- |
| bitext27-t15 | Bitext:check_cancellation_fee -> billing (cancella... | escalated | escalated (OK) | info (FAIL) | #support-technical | sent | -- |
| bitext27-t16 | Bitext:track_order -> other (order tracking — no S... | escalated | escalated (OK) | out_of_domain (FAIL) | #support-technical | sent | -- |
| bitext27-t17 | Bitext:delivery_options -> info (delivery options ... | auto_send | escalated (FAIL) | out_of_domain (FAIL) | #support-technical | sent | -- |
| bitext27-t18 | Bitext:delivery_period -> info (delivery timing — ... | auto_send | escalated (FAIL) | out_of_domain (FAIL) | #support-technical | sent | -- |
| bitext27-t19 | Bitext:change_shipping_address -> other (shipping ... | escalated | escalated (OK) | out_of_domain (FAIL) | #support-technical | sent | -- |
| bitext27-t20 | Bitext:set_up_shipping_address -> other (shipping ... | escalated | escalated (OK) | out_of_domain (FAIL) | #support-technical | sent | -- |

## Failure slice -- by intent

| Intent | Correct | Total | Accuracy |
|---|---|---|---|
| FAQ | 0 | 3 | 0.0% |
| other | 8 | 8 | 100.0% |
| refund | 3 | 3 | 100.0% |
| billing | 3 | 3 | 100.0% |
| complaint | 1 | 1 | 100.0% |
| info | 0 | 2 | 0.0% |

## Failure slice -- by risk-flag presence

| Group | Correct | Total | Accuracy |
|---|---|---|---|
| with_flags | 0 | 0 | 0.0% |
| no_flags | 15 | 20 | 75.0% |

## Escalation mismatches

| Ticket | Expected | Actual | Channel |
|---|---|---|---|
| bitext27-t01 | auto_send | escalated | #support-technical |
| bitext27-t02 | auto_send | escalated | #support-technical |
| bitext27-t03 | auto_send | escalated | #support-technical |
| bitext27-t17 | auto_send | escalated | #support-technical |
| bitext27-t18 | auto_send | escalated | #support-technical |

---

## Ticket coverage

| Ticket | Code path / mapping |
|---|---|
| bitext27-t01 | Bitext:newsletter_subscription -> FAQ (newsletter management — FAQ) |
| bitext27-t02 | Bitext:edit_account -> FAQ (account-detail change — FAQ) |
| bitext27-t03 | Bitext:switch_account -> FAQ (account switching — FAQ) |
| bitext27-t04 | Bitext:delete_account -> other (account deletion — sensitive, escalate) |
| bitext27-t05 | Bitext:get_refund -> refund (refund request — money) |
| bitext27-t06 | Bitext:check_refund_policy -> refund (refund-policy question — refund mention) |
| bitext27-t07 | Bitext:track_refund -> refund (refund status — refund mention) |
| bitext27-t08 | Bitext:payment_issue -> billing (payment failure — billing) |
| bitext27-t09 | Bitext:complaint -> complaint (complaint — edge-case escalates) |
| bitext27-t10 | Bitext:review -> other (leaving a review — edge-case 'other') |
| bitext27-t11 | Bitext:contact_human_agent -> other (explicit human request) |
| bitext27-t12 | Bitext:contact_customer_service -> other (request to reach support) |
| bitext27-t13 | Bitext:cancel_order -> billing (order cancellation — money [e-commerce]) |
| bitext27-t14 | Bitext:change_order -> other (order change — no SaaS equivalent [e-commerce]) |
| bitext27-t15 | Bitext:check_cancellation_fee -> billing (cancellation fee — money [e-commerce]) |
| bitext27-t16 | Bitext:track_order -> other (order tracking — no SaaS equivalent [e-commerce]) |
| bitext27-t17 | Bitext:delivery_options -> info (delivery options — info [e-commerce]) |
| bitext27-t18 | Bitext:delivery_period -> info (delivery timing — info [e-commerce]) |
| bitext27-t19 | Bitext:change_shipping_address -> other (shipping address — no SaaS equivalent [e-commerce]) |
| bitext27-t20 | Bitext:set_up_shipping_address -> other (shipping setup — no SaaS equivalent [e-commerce]) |