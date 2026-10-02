# HANDOFF — where this project stands

> Written 2026-10-02 at the end of a long working session. Read this first, then
> `CLAUDE.md` (the project's own entry point) and `eval/METHODOLOGY.md` (how to
> read any number in `eval/`).
>
> This file is about **state and open threads**, not architecture. Architecture
> lives in `docs/architecture.md`; the product narrative in `HOW_IT_WORKS.md`.

## State

- `main` is level with `origin/main`. Working tree clean.
- **300 tests pass** (`pytest -q`).
- Verified end to end against live services, not just unit-tested: IMAP intake →
  classifier → drafter → Feishu approval card → WebSocket callback → approve
  click → graph resume → SMTP send. Real mail, real card, real click.
- The Feishu channel uses a **WebSocket long connection**, so it needs no public
  URL and no tunnel. Select 「使用长连接接收回调」 in the Feishu console for it to
  work. A leftover cloudflared quick-tunnel URL in that console will silently
  break clicks — quick tunnels change hostname on every restart.

## Recent work (most recent first)

Read the commit messages; they carry the reasoning, and that is deliberate.

| Commit | What |
|---|---|
| `docs(eval): record the noise floor measurement and the rubric attempt` | The measurement discipline below, plus a drafter confidence rubric whose benefit is **not proven** |
| `fix(policy): escalate out-of-domain requests, at any confidence` | The safety fix that worked |
| `feat(eval): add a reply-language eval set and score it in code` | `--dataset language` |
| `feat(feishu): show the customer's message on the approval card` | The approver can finally see what was asked |
| `feat(i18n): reply in the customer's language, defaulting to Chinese` | Language contract in all three drafting prompts |
| `feat(hitl): implement SLA escalation and the Manual Queue customer notice` | Spec'd but never implemented |
| `fix(hitl): refresh the approval elapsed baseline and bound revalidation` | Baseline refresh + `MAX_REVALIDATIONS` |

## Measurement discipline — read before changing anything

**`false_auto_send_rate` is unusable when few tickets auto-send.** It is
`dangerous auto-sends / total auto-sends`; on the bitext27 test split only 0-4
tickets auto-send, so **one decision flip moves it 25-50 points**. Measured
directly, 3 repetitions of the same code: v3 read 25% / 50% / 67%.

Consequences already observed here:

- A v3 baseline reported 0% (PASS) and the next run of nearly the same code
  reported 25-67%. The 0% was luck, not safety.
- Making v3 *more* conservative cut its auto-sends and pushed the rate **up**,
  because the numerator held while the denominator halved.

**So: report absolute counts, run `--reps 3`, and never compare two runs on a
rate whose denominator is below ~20.** A `--reps 3` run writes a noise summary
alongside the per-rep files.

Use `--reps 3` before believing any change to the gates or the prompts.

## Open threads

### 1. v4 now auto-sends nothing (biggest open question)

On bitext27 test with the current model, **v4 sends 0 of 20 automatically** and
has 0 dangerous auto-sends across 3 repetitions. v3 sends 1-3 and leaks on 2 of
3 runs, on different tickets each time.

That is a real safety/throughput trade, and the honest framing is **"v4 buys
zero leaks with zero throughput"** — not "v4 is better". It will be asked in an
interview. The routes to *both* safety and throughput:

- **Classifier self-consistency.** Classify twice (different temperature) and
  escalate on disagreement. The v4 Critic reviews the *draft*, so it cannot see a
  wrong *intent label* — `bitext27_findings.md` calls this the architectural
  ceiling. A second opinion on the label is the direct fix.
- **Derive risk flags from the intent, not only from keywords.** Gate 1 scans a
  money/legal regex, so v3's historical leaks all had empty `risk_flags` — they
  were labelled `info`/`FAQ` and no keyword matched.
- **Widen the eval sets.** Most of the above is unresolvable at N=20; a 10-point
  difference cannot be seen.

### 2. The drafter confidence rubric is unproven

Gate 2 compares `draft_confidence` to a hard 0.85, and the drafter prompt
originally never said what the number meant. A banded rubric was added; it moved
the curated draft median 0.60 → 0.72 (still below 0.85) and curated v4
escalation precision 80% → 90%, against a single pre-change reading of 80%. **That
is inside the noise.** Kept because it is mechanically justified and regresses
nothing — not because a win was measured. Re-examine if the sets grow.

Note the threshold is **model-sensitive**: archived results show 2/10 curated
tickets auto-sending under one model and 0/10 under another, from the same code.
`scripts/check_confidence_calibration.py` measures this in one command.

### 3. v3's residual leaks are intentionally left

v3 leaks on 2 of 3 runs. This is being **kept as the control arm** — v4's value
is only demonstrable against a version that fails. Do not "fix" v3 without
discussing it first; it is a deliberate contrast, not an oversight.

### 4. Housekeeping not done

- Delete the empty `-BiGRSA` repo on GitHub (browser).
- `Underloop` default branch is `codex/rename-underloop`; should be `main`
  (browser).
- `adviserplan.md` and `study.md` are tracked and public. They are working notes
  (one says "delegated build plan, reviewed by advisor model"), not product docs.
  `git rm --cached` was considered and not done.
- `spec.md` §Manual Queue and `docs/architecture.md` were brought in line with the
  code; `DRAFT-issue-revalidate-baseline.md` still holds an upstream-facing writeup
  of the baseline bug, now marked fixed locally.

## Scripts

`scripts/` follows the existing `verify_gmail.py` / `verify_slack.py` convention.

| Script | Credentials | What it answers |
|---|---|---|
| `verify_feishu_roundtrip.py` | none | Does thread_id survive card → converter → callback? The silent-failure check |
| `check_out_of_domain.py` | LLM | Is out-of-domain separated from in-domain, and do in-domain questions still auto-send? |
| `check_confidence_calibration.py` | LLM | Are the two confidence distributions anywhere near the 0.85 threshold? |
| `check_reply_language.py` | LLM | Which language does each draft path reply in? |
| `send_live_test_card.py` | Feishu | **Sends a real card.** Rendering can only be checked by eye |

## Working notes

- The project lives at `E:\Agent\8\hitl-support-agent`; `data/` holds the SQLite
  checkpointer, the PII vault sidecar and the pending-approval index, all
  gitignored. `.env` holds live credentials and is not tracked — keep it that way.
- Several real messages were sent to the Feishu test chat while verifying. The
  chat is a one-member group, so nothing leaked, but the history is noisy.
