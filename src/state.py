"""AgentState TypedDict — full schema per spec.md §5.

The graph's single source of truth for ticket state. Every node reads and writes
through this dict; the SQLite checkpointer persists the whole structure at each
super-step boundary.

Field grouping mirrors spec.md §5 verbatim. Do not reorder without updating spec.
"""

from datetime import datetime
from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    # ---- Identity ----
    ticket_id: str
    thread_id: str  # equals ticket_id (stable resume pointer for the checkpointer)
    graph_version: str  # "v3"

    # ---- Input ----
    customer_message: str
    customer_history: list[dict[str, Any]]
    context_hash: str  # hash of retrieved_context for stale-check during long pauses 检索出来知识库上下文的哈希；长时间暂停后恢复会话，对比 hash 判断知识库上下文是否过期失效

    # ---- Classification ---- 退款 / 技术故障 / 账单问题 / 投诉 / FAQ / 其他
    intent: str  # refund | technical | billing | complaint | FAQ | other
    intent_confidence: float
    sentiment: str  # angry | neutral | positive 客户情绪
    risk_flags: list[str] #风险标记列表，标记当前工单存在哪些风险

    # ---- Drafting ----
    original_draft: str
    final_draft: str
    draft_confidence: float

    # ---- Customer-tier-aware routing inputs ---- 客户等级 + 风险 + 政策
    customer_tier: str  # Free | SMB | Enterprise (from CRM)
    risk_level: str  # none | financial | legal | compliance
    policy_matches: list[str]  # e.g. ["ACME 4.2.1", "ACME 7.1"]

    # ---- Approval ---- 人工审批
    requires_approval: bool
    approval_status: str  # pending | approved | edited | rejected | expired | cancelled | superseded
    approver_id: str  # Slack user id
    approval_timestamp: str
    sla_deadline: datetime #超时审批直接走过期分支

    # ---- Real I/O channels ---- 真实对外通信通道
    email_thread_id: str  # original RFC-822 Message-ID — drives In-Reply-To threading
    slack_channel: str  # e.g. "#support-refunds"
    slack_message_ts: str  # used to update_message in place AND resume on the right msg

    # ---- Idempotency on send (app-layer, not SMTP-layer) ---- 发送幂等控制
    send_idempotency_key: str  # set once at entry — lookup key for dedupe #工单入口就生成一次，用于去重；同一个 key 不会重复发送
    sent_message_id: str | None  # presence == "already sent" — Send node skips if set #如果不为 None，代表邮件已经成功发出；发送节点看到该字段直接跳过发送逻辑，防止重复发邮件

    # ---- Execution ---- 发送执行状态
    send_status: str  # pending | in_flight | sent | failed_retryable | failed_manual

    # ---- Loop guards ---- 循环保护，防止死循环
    human_rejection_count: int  # >= MAX_HUMAN_REJECTIONS routes to manual_queue
    rejection_reason: str | None  # captured from Slack reject modal; carried into next Draft
    send_retry_count: int  # >= MAX_SEND_RETRIES routes to failed_manual

    #工单外部状态：打开 / 客户已经关闭工单 / 工单被新工单替代；恢复会话的时候，如果工单已经关闭，Agent 直接终止，不继续处理。
    # ---- Long-pause edge cases ----
    ticket_external_status: str  # open | closed_by_customer | superseded

    # ---- Terminal ----
    final_state: str  # sent | rejected | expired | cancelled | superseded | failed_manual

    # ---- Audit + observability ----
    audit_log: list[dict[str, Any]]  # APPEND-ONLY — never mutate prior entries
    cost_breakdown: dict[str, float]  # {"classify": 0.0001, "draft": 0.0008, ...}
    tokens_breakdown: dict[str, int]  # {"classify": 230, "draft": 1240, ...}
    # `total_tokens` / `total_cost_usd` are derived at read time from the dicts
    # above. LangGraph rebuilds state from node-return partial dicts, so primitive
    # scalar mutations on the live AgentState don't survive across super-steps;
    # dict mutations do (the dict is shared by reference). Keep the scalar fields
    # for back-compat — populate only via final-snapshot computation in callers
    # that need a single number (e.g. eval/run_experiments.py).
    total_tokens: int
    total_cost_usd: float
    trace_url: str

    # ---- v4 multi-agent transient fields (Drafter↔Critic loop) ----用于【起草 Agent ↔ 批评 Agent】循环迭代
    # Underscore-prefix marks them as transient — not part of v3 contract.
    # Only used inside the drafter sub-graph; cleared on exit. 子图结束必须清空**。
    _critic_iteration: int        # 0..MAX_CRITIC_ITERATIONS-1
    _critic_verdict: str          # accept | revise
    _critic_feedback: str         # short, actionable string
    _critic_severity: float       # [0, 1]


# Default factory — every required key has a sensible zero value so
# nodes don't need to defend against missing keys.
def initial_state(
    ticket_id: str,
    customer_message: str,
    email_thread_id: str,
    send_idempotency_key: str,
) -> AgentState:
    """Build a fresh AgentState for a new ticket. thread_id == ticket_id."""
    return AgentState(
        ticket_id=ticket_id,
        thread_id=ticket_id,
        graph_version="v3",
        customer_message=customer_message,
        customer_history=[],
        context_hash="",
        intent="",
        intent_confidence=0.0,
        sentiment="",
        risk_flags=[],
        original_draft="",
        final_draft="",
        draft_confidence=0.0,
        customer_tier="",
        risk_level="none",
        policy_matches=[],
        requires_approval=False,
        approval_status="",
        approver_id="",
        approval_timestamp="",
        email_thread_id=email_thread_id,
        slack_channel="",
        slack_message_ts="",
        send_idempotency_key=send_idempotency_key,
        sent_message_id=None,
        send_status="pending",
        human_rejection_count=0,
        rejection_reason=None,
        send_retry_count=0,
        ticket_external_status="open",
        final_state="",
        audit_log=[],
        cost_breakdown={},
        tokens_breakdown={},
        total_tokens=0,
        total_cost_usd=0.0,
        trace_url="",
    )
