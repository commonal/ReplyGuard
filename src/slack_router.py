"""Approval destination router — picks the target channel from AgentState.

Scope (adviserplan.md §Phase 2 — Track C):
  3 channels for the 6-10h build:
    #support-refunds    — refund intent
    #support-technical  — technical intent + all catch-all cases
    #support-complaints — angry sentiment (highest priority)

  Deferred (config-only addition in production):
    #support-legal, #support-enterprise, #support-billing

Priority order (CLAUDE.md "Channel router priority"):
  1. sentiment == angry → #support-complaints
  2. intent == refund   → #support-refunds
  3. intent == technical / basic_technical / info → #support-technical
  4. everything else (billing, complaint, FAQ, other, empty) → #support-technical (catch-all)

Rules are lexicographic / priority-ordered, NOT fuzzy. First match wins.

Pure function — no I/O, no LLM, no DB. Tests run instantly. The returned
destination is a Feishu receive ID when the Feishu provider is configured, or
the legacy Slack channel name when the fallback provider is selected.
Side effect: writes `slack_channel` to state (the node calling this should
persist that write back into the LangGraph state update).
"""

from __future__ import annotations

from src.config import settings
from src.state import AgentState

# ---------------------------------------------------------------------------
# Channel constants (single source of truth — change here to add/remove)
# ---------------------------------------------------------------------------

CHANNEL_COMPLAINTS = "#support-complaints"
CHANNEL_REFUNDS = "#support-refunds"
CHANNEL_TECHNICAL = "#support-technical"

# Intent → channel mapping (consulted only when sentiment != angry)
_INTENT_MAP: dict[str, str] = {
    "refund": CHANNEL_REFUNDS,
    "technical": CHANNEL_TECHNICAL,
    "basic_technical": CHANNEL_TECHNICAL,
    "info": CHANNEL_TECHNICAL,
    # catch-all entries for intents without a dedicated channel yet
    "billing": CHANNEL_TECHNICAL,
    "complaint": CHANNEL_TECHNICAL,
    "FAQ": CHANNEL_TECHNICAL,
    "other": CHANNEL_TECHNICAL,
}

_DEFAULT_CHANNEL = CHANNEL_TECHNICAL


def route_channel(state: AgentState) -> str:
    """Return the configured approval destination and write it to state.

    Priority:
      1. sentiment == angry  → #support-complaints  (highest)
      2. intent map          → channel per _INTENT_MAP
      3. default             → #support-technical   (catch-all)

    Args:
        state: The current AgentState. Mutated in-place to set `slack_channel`.

    Returns:
        A Feishu receive ID (for example ``oc_...``) or a legacy Slack
        channel string (for example ``#support-complaints``).
    """
    sentiment: str = state.get("sentiment", "") or ""
    intent: str = state.get("intent", "") or ""

    # Priority 1 — angry sentiment overrides everything
    destinations = settings.channel_set
    if sentiment == "angry":
        channel = destinations["complaints"]
    else:
        # Priority 2 — intent-based routing with catch-all fallback
        logical_channel = _INTENT_MAP.get(intent, _DEFAULT_CHANNEL)
        if logical_channel == CHANNEL_REFUNDS:
            channel = destinations["refunds"]
        elif logical_channel == CHANNEL_COMPLAINTS:
            channel = destinations["complaints"]
        else:
            channel = destinations["technical"]

    # Write back into state so the graph can persist it
    state["slack_channel"] = channel
    return channel
