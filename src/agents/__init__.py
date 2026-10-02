"""Multi-agent layer (v4) — Researcher, Drafter, Critic.

Each agent is a compiled LangGraph sub-graph slotted into the parent graph
in place of an existing v3 node. Hard invariants preserved per
docs/v4_multiagent.md.

Activation: MULTIAGENT_ENABLED defaults to 1 (v4 multi-agent); set 0 for the v3 baseline.
"""
