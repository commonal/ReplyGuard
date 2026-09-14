"""Two-gate routing logic for the HITL customer support agent.
严格控制自动发送（auto‑send），保证错误自动发送率为 0%

业务背景：客服 AI 不是所有消息都可以自动回复；高风险、低置信度的工单必须交给人工接管（escalate 升级 / 人工介入）；只有同时过两道门控 + 安全意图，才允许 AI 自动发送回复。

1. **Gate1 策略风险检查**：只要命中任意风险条件，直接升级人工，不走后续逻辑；会修改状态里的风险标记、风险等级。

2. Gate1 放行后才执行 **Gate2 置信度检查**：意图置信度、草稿回复置信度双阈值校验。

3. `should_auto_send`：组合两个门控 + 安全意图白名单，最终输出是否允许 AI 自动发消息。


Gate 1 — Policy Risk Check (gate_one_policy_risk):
  Escalate if ANY of the following is true:
    - Message contains a refund/money mention (keyword + dollar-sign regex)
    - intent == refund
    - sentiment == angry
    - Edge-case intent: complaint | other (documented choice; these are
      open-ended, hard to draft for safely, so we always escalate)
    - policy_matches already populated by Enrich Context (KB hit)

  Returns:
    bool — True if escalation required, False if safe to continue to Gate 2.

  Side effects on state (appended/deduped, never overwritten):
    - risk_flags: adds "angry" / "money_mention" / "refund_intent" /
                  "edge_case_intent" / "policy_match" as applicable
    - risk_level: set to "financial" on money/refund triggers; "compliance"
                  on policy_match; leaves "none" if no risk.
    - policy_matches: NOT modified here — that's Enrich Context's job; Gate 1
                      reads it as an input signal only.

Gate 2 — Confidence Check (gate_two_confidence):
  Only called when Gate 1 returns False.
  Escalate if intent_confidence < 0.85 OR draft_confidence < 0.85.
  Missing/default confidence (0.0) is treated as low → escalate (conservative).

should_auto_send:
  Returns True only when:
    - gate_one_policy_risk is False (no risk)
    - gate_two_confidence is False (both confidences >= 0.85)
    - intent in {FAQ, info, basic_technical}   ← CLAUDE.md verbatim contract

Primary safety metric: false_auto_send_rate = 0%

Pure function — no I/O, no LLM, no DB. Tests run instantly.
"""

from __future__ import annotations

import re

from src.state import AgentState

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Confidence threshold for both intent and draft scores 置信度阈值，意图分类置信度、生成草稿置信度都需要≥0.85
CONFIDENCE_THRESHOLD: float = 0.85

# Auto-send is only safe for these low-stakes, well-understood intents.
# Verbatim from CLAUDE.md "Two-gate routing" section. 只有 FAQ 问答、普通信息查询、基础技术问题才允许自动回复
AUTO_SEND_SAFE_INTENTS: frozenset[str] = frozenset({"FAQ", "info", "basic_technical"})

# Edge-case intents that always escalate via Gate 1.
# Rationale: "complaint" is open-ended and emotionally charged; "other" is
# a classifier catch-all that means we don't actually know the intent.
# Both are riskier to auto-draft for than any of the mapped intents. 边缘意图：`complaint`投诉、`other`无法识别的兜底意图，**Gate1 直接触发人工升级**
EDGE_CASE_INTENTS: frozenset[str] = frozenset({"complaint", "other"})

# Intents that always escalate regardless of message content.
# "refund" is the canonical financial-risk intent. 金融风险意图：退款，直接升级人工
FINANCIAL_INTENTS: frozenset[str] = frozenset({"refund"})

# Keyword / phrase regex that signals a money-related request.
# Scans customer_message body (case-insensitive). 正则表达式，**扫描用户原始消息文本**，大小写不敏感，匹配金钱相关关键词：美元金额、refund、charge、money back、billing、dispute、cancellation、account recovery。只要用户消息命中，判定金融风险。
_MONEY_RE = re.compile(
    r"\$\s*\d+"           # dollar amount: $99, $ 250
    r"|\brefund\b"         # the word "refund"
    r"|\bcharge[ds]?\b"    # charged / charge
    r"|\bmoney back\b"     # "money back"
    r"|\bbillin[g]\b"      # billing disputes
    r"|\bdispute\b"        # dispute
    r"|\bcancell?ation\b"  # cancellation
    r"|\baccount recovery\b",  # account recovery
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers 内部工具 Helper 函数
# ---------------------------------------------------------------------------
#给状态`risk_flags`追加风险标记，**去重，不会重复添加**。
def _append_flag(state: AgentState, flag: str) -> None:
    """Add *flag* to state['risk_flags'] if not already present."""
    flags: list[str] = state.get("risk_flags") or []
    if flag not in flags:
        flags.append(flag)
    state["risk_flags"] = flags


def _set_risk_level(state: AgentState, level: str) -> None:
    """Set risk_level only if it would be an upgrade (none → financial → compliance → legal)."""
    order = {"none": 0, "financial": 1, "compliance": 2, "legal": 3}
    current = state.get("risk_level") or "none"
    if order.get(level, 0) > order.get(current, 0):
        state["risk_level"] = level


# ---------------------------------------------------------------------------
# Gate 1 — Policy Risk Check
# ---------------------------------------------------------------------------

def gate_one_policy_risk(state: AgentState) -> bool:
    """Evaluate policy risk for the current ticket state.

    Mutates state: appends to risk_flags; upgrades risk_level.
    返回 `True` = 需要升级人工；`False` = 风险通过，可以进入 Gate2
⚠️这个函数会**修改 state**：追加 risk_flags，升级 risk_level。
触发任意一条条件，就标记 escalate=True。
    Returns:
        True  — risk detected, skip Gate 2, route to Interrupt Gate.
        False — no risk detected, proceed to Gate 2.
    """
    escalate = False

    # --- Check 1: Angry sentiment ---
    sentiment: str = state.get("sentiment", "") or ""
    if sentiment == "angry":
        _append_flag(state, "angry")
        _set_risk_level(state, "financial")
        escalate = True

    # --- Check 2: Money / refund mention in message text ---
    message: str = state.get("customer_message", "") or ""
    if _MONEY_RE.search(message):
        _append_flag(state, "money_mention")
        _set_risk_level(state, "financial")
        escalate = True

    # --- Check 3: Financial intent (refund) ---
    intent: str = state.get("intent", "") or ""
    if intent in FINANCIAL_INTENTS:
        _append_flag(state, "refund_intent")
        _set_risk_level(state, "financial")
        escalate = True

    # --- Check 4: Edge-case intent ---
    if intent in EDGE_CASE_INTENTS:
        _append_flag(state, "edge_case_intent")
        _set_risk_level(state, "financial")
        escalate = True

    # --- Check 5: Pre-loaded policy matches from Enrich Context ---
    policy_matches: list[str] = state.get("policy_matches") or []
    if policy_matches:
        _append_flag(state, "policy_match")
        _set_risk_level(state, "compliance")
        escalate = True

    # --- Check 6: Classifier-supplied legal / compliance risk_flags ---
    # If the classifier (or upstream enrichment) tagged the ticket as legal or
    # compliance — e.g. classifier read "lawyer", "FTC", "GDPR", "subpoena" —
    # respect that signal as a Gate 1 trip. Without this, a ticket whose only
    # risk signal is the classifier's `risk_flags` array would slip into
    # Gate 2 and could auto-send on a high-confidence draft. The threat model
    # row A2 names this as the gate's purpose; this check makes it real.
    existing_flags: list[str] = state.get("risk_flags") or []
    if "legal" in existing_flags:
        _set_risk_level(state, "legal")
        escalate = True
    if "compliance" in existing_flags:
        _set_risk_level(state, "compliance")
        escalate = True

    return escalate


# ---------------------------------------------------------------------------
# Gate 2 — Confidence Check
# ---------------------------------------------------------------------------

def gate_two_confidence(state: AgentState) -> bool:
    """Evaluate confidence scores. Only called when Gate 1 returned False.

    Returns:
        True  — confidence below threshold, escalate to Interrupt Gate.
        False — both scores acceptable, proceed to auto-send path.
    """
    intent_conf: float = state.get("intent_confidence") or 0.0
    draft_conf: float = state.get("draft_confidence") or 0.0
    return intent_conf < CONFIDENCE_THRESHOLD or draft_conf < CONFIDENCE_THRESHOLD


# ---------------------------------------------------------------------------
# Combined auto-send decision
# ---------------------------------------------------------------------------

def should_auto_send(state: AgentState) -> bool:
    """Return True only when ALL three conditions hold:

    1. Gate 1 passes (no policy risk).
    2. Gate 2 passes (both confidences >= 0.85).
    3. intent is in the safe auto-send set {FAQ, info, basic_technical}.

    Gate 2 is only evaluated if Gate 1 passes (sequential, not parallel).
    This preserves the gate ordering contract and keeps Gate 1 as the faster,
    cheaper check that short-circuits the rest.
    """
    intent: str = state.get("intent", "") or ""

    # Gate 1 — if risk detected, stop immediately
    if gate_one_policy_risk(state):
        return False

    # Gate 2 — only runs here if Gate 1 was clean
    if gate_two_confidence(state):
        return False

    # Safe-intent check — even high-confidence billing tickets must escalate
    return intent in AUTO_SEND_SAFE_INTENTS
