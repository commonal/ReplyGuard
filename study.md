第 1 步 · 项目到底解决什么问题（30 min）
读 README.md — 一句话定位：一个"人在环内（HITL）"的客服 agent，邮件进来 → AI 起草 → 高风险/低置信度时停在 Slack 等人审批 → 人才授权发。
读 CLAUDE.md 你已经看过的「What we're building / Graph node order / Two-gate routing」三节，这是全局地图。

第 2 步 · 端到端故事线（1 h）
读 HOW_IT_WORKS.md — 它用"Jamie 的一封邮件"串起整条链路（v3 单 agent 版），最易入门。
然后看 HOW_IT_WORKS.md 末尾「How v4 changes this story」—— 知道 v4 把"单 agent"拆成了 Researcher + Drafter↔Critic 三个角色。

第 3 步 · 架构与状态（1.5 h）
读 docs/architecture.md — 有图、有节点顺序、有 env 变量表、有可观测性说明。重点看「Graph node order」，和你 CLAUDE.md 里那张图对照。
读 spec.md §5 — 完整 AgentState 字段表（ticket_id / risk_level / draft_confidence / approval_status / audit_log …）。这是所有节点读写的数据契约，读懂 state 就懂了一半。

第 4 步 · 核心代码（最重要，2–3 h）
按数据流顺序读 src/：

state.py — AgentState 类型定义
llm.py — LLM 调用（你刚配的 DeepSeek 走这里，LLM_PROVIDER=openai 分支） 费用计算，模型调用
policy.py — 两道门 + should_auto_send，你刚问的安全逻辑全在这（policy.py:188） 严格控制自动发送（auto‑send），保证错误自动发送率为 0%
nodes.py — 各节点函数（分类/起草/发 Slack 卡片/interrupt）
graph.py — 节点怎么连成图、interrupt 在哪
email_listener.py — 你刚看的 IMAP 轮询（已懂）
slack_handler.py — Slack 审批按钮回调

第 5 步 · v4 多 agent（1.5 h）
读 docs/v4_multiagent.md — v4 架构锁定 + 硬不变量
读 src/agents/ 下 researcher.py / drafter.py / critic.py — 三个角色怎么协作


第 6 步 · 安全与评测（按需）
docs/threat_model.md — 能力隔离（Read 不能发、Email 不能 Slack 等）、PII 进出
eval/ 下的 results_*.md + discussion.md — v3 vs v4 的实测对比（为什么默认翻到 v4）