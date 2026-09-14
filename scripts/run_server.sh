#!/usr/bin/env bash
# One-shot launcher for the HITL support agent on a GFW-restricted network.
# Exports the proxy vars (Slack/Gmail/IMAP/SMTP need them — the SDKs don't
# read system proxy env by default) and boots `python -m src.server`.
#
# Usage: bash scripts/run_server.sh
set -euo pipefail
cd "$(dirname "$0")/.."

# Proxies (Clash/FlClash): HTTP for Slack API + SMTP, SOCKS5 for IMAP.
export HTTPS_PROXY="http://127.0.0.1:7890"
export HTTP_PROXY="http://127.0.0.1:7890"
export SOCKS_PROXY="socks5://127.0.0.1:7891"

# DeepSeek official (OpenAI-compatible). LLM_PROVIDER=openai switches
# src/llm.py away from OpenRouter.
export LLM_PROVIDER="openai"
# OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL are read from .env via pydantic-settings.

# Keep logs quiet — LangSmith tracing is optional and 401s otherwise flood stderr.
export LANGSMITH_TRACING="${LANGSMITH_TRACING:-false}"

exec .venv/Scripts/python.exe -m src.server
