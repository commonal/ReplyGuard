<!-- 草稿说明（发帖前删除本段）：
     这是给上游仓库 Ranith36963/hitl-support-agent 的 issue 草稿，纯反馈、不带代码改动。
     标题 + 正文均为英文（仓库语言）。确认无误后告诉我，我走 7890 代理帮你提交；
     如果你想自己发，直接复制标题和分隔线以下的内容到 GitHub New Issue 即可。
     观察基于 main @ d970975（当前 origin/main HEAD）。-->

# Title

**Re-approval after "Context changed" always re-enters `revalidate_context` — elapsed baseline is never refreshed**

---

## Summary

When an approval pause exceeds `REVALIDATE_THRESHOLD_MIN` (default 15 min), the graph goes
`revalidate_context → summarize_changes → interrupt_gate` and re-pauses for a second decision.
On that second decision, `route_after_action` still computes elapsed time from the **first**
`slack_notification` audit entry, so the >15 min condition is always true and every
re-approval cycle re-enters `revalidate_context` — even if the human replies seconds after
the "Context changed" update.

## Details

- `route_after_action` (`src/nodes.py`) finds the notification time with a
  `for … break` over `audit_log`, i.e. it always takes the **first** `slack_notification`
  entry's `ts`.
- `summarize_changes_node` updates the Slack message via `chat.update`
  (which preserves the original message `ts`), resets `approval_status` to `pending`,
  but appends **no new timing baseline** to the audit log and does not refresh `sla_deadline`.
- Timeline of the problem case:
  1. Approval request posted at T0
  2. Human approves at T0 + 20 min → elapsed 20 min > 15 min → `revalidate_context` ✓ (correct)
  3. Context changed → `summarize_changes` re-pauses at T0 + 20 min + ε
  4. Human approves again at T0 + 20 min + 10 s → elapsed is still computed as ~20 min
     → `revalidate_context` runs again ✗ (should be ~10 s since the re-prompt)

Consequences:

- If the revalidated context is stable, the loop terminates (routes to `finalize`), but the
  3 MCP reads + context hash run on **every** cycle, plus an extra LLM delta step whenever
  the context actually changed. If the context keeps drifting, the loop persists.
- `sla_deadline` (24 h SLA) is set once at the first notification and never refreshed across
  re-decision rounds, so the effective SLA window silently shrinks with each round.

I believe this is a latent bug rather than intended behavior — the re-pause exists so the
human can make a fresh decision on updated context, and that fresh decision should be
measured from the re-prompt, not from the original notification.

## Suggested minimal fix

- In `summarize_changes_node`, record a fresh elapsed baseline — either append a new audit
  entry representing the updated approval request, or add an explicit
  `approval_baseline_timestamp` state field (similar in spirit to `sent_message_id`).
- In `route_after_action`, read the **latest** baseline instead of the first audit entry
  (also more robust if other nodes ever write into the audit log).
- Optionally refresh `sla_deadline` at the same point so each re-prompt gets a full SLA window.

I'm happy to send a small PR with a regression test if you agree with the direction —
noting that `tests/` currently has no coverage for the `route_after_action` elapsed branch,
so the fix would add the first one.

## Environment

- Observed on `main` @ `d970975`
- Relevant code: `src/nodes.py` — `slack_notification_node`, `route_after_action`,
  `revalidate_context_node`, `summarize_changes_node`
