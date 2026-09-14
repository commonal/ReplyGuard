# HITL 客户支持 Agent——架构

> 一个生产风格的客户支持系统，将 LLM 推理、确定性策略执行、人工审批工作流和持久化执行结合起来。系统使用真实 Gmail 收发、Slack 多频道审批，以及三个按能力隔离的 MCP Server。

## 系统分层

运行时分为七层，每层只承担一种职责，代码目录也与这种划分相呼应。

| # | 层 | 职责 | 代码位置 |
|---|---|---|---|
| 1 | **接入层 Ingestion** | 接收客户邮件并发出回复邮件 | `src/email_listener.py`（IMAP）+ MCP Email Write（SMTP） |
| 2 | **编排层 Orchestration** | 安排节点顺序、持久化状态、从崩溃中恢复 | `src/graph.py`（LangGraph + SQLite checkpointer） |
| 3 | **智能层 Intelligence** | 调用 LLM：分类、起草、总结变化 | `src/llm.py` + `src/nodes.py` |
| 4 | **策略层 Policy** | 两道门控、频道选择、知识库检索 | `src/policy.py` + `src/slack_router.py` + 通过 MCP Read 访问 ACME KB |
| 5 | **HITL 层** | Slack 通知、interrupt、操作处理器、编辑模态框 | `src/server.py` + `src/slack_handler.py` + MCP Slack Write |
| 6 | **执行层 Execution** | 组装负载、幂等发送、记录审计日志 | `src/nodes.py` 中的 Finalize / Send Email / Audit |
| 7 | **可观测层 Observability** | Trace、成本跟踪、指标 | `src/llm.py` 中的 LangSmith 装饰器 + `src/metrics.py` 中的 Prometheus |

替换任意一层都应当是配置变更，而不是重写系统。例如，生产部署可以用 SES 替换 Gmail/IMAP、用 Salesforce 替换模拟 CRM、用公司的真实策略文档替换 ACME 语料，而图逻辑保持不变。

## 端到端流程（30 秒总览）

```mermaid
flowchart TD
    S0[客户发送邮件到<br/>support@yourcompany.com]:::email
    S1[1. Agent 通过 IMAP 读取]:::blue
    S2[2. 分类并补充上下文<br/>CRM + ACME 策略]:::blue
    S3[3. 生成回复草稿]:::blue
    S4{4. 可以安全发送吗？}:::yellow
    S5[5. 通过 SMTP 自动发送]:::blue
    S6[6. 按意图、客户等级和风险<br/>选择 Slack 频道]:::slack
    S7[7. 团队在对应 Slack 频道<br/>批准或编辑]:::orange
    S8[8. 将回复发给客户<br/>并归入原邮件线程]:::email
    S9[9. 审计 + LangSmith trace]:::green

    S0 --> S1 --> S2 --> S3 --> S4
    S4 -->|是| S5
    S4 -->|否：退款、愤怒或不确定| S6
    S6 --> S7
    S7 --> S8
    S5 --> S8
    S8 --> S9

    classDef email fill:#fff3bf,stroke:#f59e0b,stroke-width:2px,color:#000
    classDef blue fill:#a5d8ff,stroke:#2563eb,stroke-width:2px,color:#000
    classDef yellow fill:#fff3bf,stroke:#f59e0b,stroke-width:2px,color:#000
    classDef slack fill:#d0bfff,stroke:#8b5cf6,stroke-width:2px,color:#000
    classDef orange fill:#ffd8a8,stroke:#d97706,stroke-width:2px,color:#000
    classDef green fill:#c3fae8,stroke:#15803d,stroke-width:2px,color:#000
```

> 为了便于阅读，第 4 步把两个独立检查（策略风险和置信度）合并成一个框；下面的详细流程会将它们拆开。

## 详细流程

```mermaid
flowchart TD
    Inbox([support@yourcompany.com<br/>Gmail 收件箱]):::email
    Inbox --> Listener[Email Listener<br/>优先使用 IMAP IDLE<br/>降级时约每 30 秒轮询]:::node
    Listener --> PII[PII 脱敏<br/>中间件]:::middleware
    PII --> Classify[意图分类<br/>intent + sentiment + risk_flags + risk_level]:::node
    Classify --> Enrich[补充上下文<br/>CRM 档案 + 历史 + ACME KB]:::node
    Enrich --> Draft[生成回复草稿]:::node
    Draft --> Policy{策略风险检查<br/>退款、愤怒或命中 ACME 策略？}:::decision

    Policy -->|检测到风险| Router{频道路由<br/>当前实现两级优先级<br/>1. angry > 2. intent}:::decision
    Policy -->|无风险| Confidence{置信度检查<br/>两个置信度都 >= 0.85？}:::decision
    Confidence -->|低于阈值| Router
    Confidence -->|高于阈值| AutoSendMarker[auto_send_marker<br/>在状态中标记，供审计使用]:::node
    AutoSendMarker --> Finalize

    Router -->|angry| ChCmp[#support-complaints]:::slack
    Router -->|intent=refund| ChRef[#support-refunds]:::slack
    Router -->|其他意图| ChTech[#support-technical<br/>兜底频道]:::slack

    ChCmp --> SlackPost
    ChRef --> SlackPost
    ChTech --> SlackPost
    SlackPost[Slack 通知<br/>发送 Block Kit 消息<br/>保存 slack_message_ts<br/>此时还不 interrupt]:::ui

    SlackPost --> Interrupt[Interrupt Gate<br/>独立节点，只执行 interrupt&#40;&#41;<br/>checkpointer 在 super-step 保存状态<br/>通过 webhook 恢复]:::hitl

    Interrupt -->|webhook 签名通过<br/>Command resume| Action{人工操作？}:::decision
    Action -->|拒绝并说明原因| RejectCheck{rejection_count >= 3？}:::decision
    Action -->|批准或编辑| Elapsed{审批延迟 > 15 分钟？}:::decision

    RejectCheck -->|是| ManualQueue[人工队列<br/>在频道发布最终状态<br/>并通过邮件通知客户]:::terminal
    RejectCheck -->|否，count++<br/>保留 rejection_reason| Draft

    Elapsed -->|否| Finalize
    Elapsed -->|是| Revalidate{重新验证上下文<br/>比较 context_hash}:::decision
    Revalidate -->|hash 未变化| Finalize
    Revalidate -->|hash 已变化| Summarize[总结变化<br/>计算差异]:::node
    Summarize -->|update_message<br/>在同一消息发布差异<br/>再次 interrupt 等待决定| Interrupt

    Finalize[Finalize Action<br/>恢复 PII + 组装负载<br/>+ In-Reply-To 和 References<br/>+ Subject: Re: ... 以维持线程]:::node
    Finalize --> SendEmail[发送邮件<br/>Gmail SMTP<br/>应用层幂等<br/>已有 sent_message_id 时跳过]:::node

    SendEmail -. SMTP .-> CustInbox([客户收件箱<br/>回复归入原线程]):::email
    SendEmail --> Audit[只追加审计日志<br/>+ 关闭 LangSmith trace]:::terminal
    Audit --> End([结束]):::terminal

    Enrich -. 读 .-> MCPRead[(MCP Read Server<br/>get_crm_profile<br/>get_customer_history<br/>get_kb_article)]:::mcpread
    SendEmail -. 写 .-> MCPEmail[(MCP Email Write<br/>通过 Gmail SMTP 发送)]:::mcpemail
    SlackPost -. 写 .-> MCPSlack[(MCP Slack Write<br/>post_approval_request<br/>update_message<br/>views.open 编辑模态框)]:::mcpslack
    Summarize -. 写 .-> MCPSlack
    ManualQueue -. 写 .-> MCPSlack

    classDef email fill:#fff3bf,stroke:#f59e0b,stroke-width:2px,color:#000
    classDef middleware fill:#d0bfff,stroke:#8b5cf6,stroke-width:2px,color:#000
    classDef node fill:#a5d8ff,stroke:#2563eb,stroke-width:2px,color:#000
    classDef decision fill:#fff3bf,stroke:#f59e0b,stroke-width:2px,color:#000
    classDef hitl fill:#ffc9c9,stroke:#dc2626,stroke-width:2px,color:#000
    classDef slack fill:#e9d5ff,stroke:#7e22ce,stroke-width:2px,color:#000
    classDef ui fill:#ffd8a8,stroke:#d97706,stroke-width:2px,color:#000
    classDef mcpread fill:#99e9f2,stroke:#0891b2,stroke-width:2px,color:#000
    classDef mcpemail fill:#fcc2d7,stroke:#be185d,stroke-width:2px,color:#000
    classDef mcpslack fill:#fde68a,stroke:#a16207,stroke-width:2px,color:#000
    classDef terminal fill:#c3fae8,stroke:#15803d,stroke-width:2px,color:#000
```

## 关键设计点

- **发送 Slack 消息必须发生在 interrupt 之前。**`interrupt()` 触发后执行立即暂停，该节点后面的代码不会运行。正确顺序是 `Channel Router → Slack Notification（发送消息并保存时间戳）→ Interrupt Gate（只调用 interrupt()）`。颠倒顺序会导致工作流永久暂停，却没有任何 Slack 消息。
- **策略门和置信度门彼此独立。**即使模型对退款请求置信度很高，也必须升级。顺序很重要：先检查策略，再检查置信度；先用更便宜的检查快速失败。
- **三个 MCP Server 按能力拆分。**Read（CRM + KB）不能发送消息；Email Write 不能发 Slack；Slack Write 不能发邮件。即使检索阶段受到 Prompt Injection，也无法访问两个 I/O 渠道，影响范围被 Server 边界限制。
- **频道路由按优先级执行，不是模糊判断。**当前版本是三个频道上的 `angry > by-intent`：`#support-complaints`、`#support-refunds`、兜底的 `#support-technical`。规格还在这两级之前定义了 `legal/compliance > Enterprise+risk`，根据 `adviserplan.md` 的范围约束暂缓实现；以后只需配置即可加入。
- **发送幂等属于应用层，而不是协议层。**SMTP 不会自动去重。Send 节点检查状态中的 `sent_message_id`，若已有值则跳过。`send_idempotency_key` 是查找键，状态字段是锁。这才是代码中“幂等发送”的含义。
- **Finalize 与 Send 分离。**Finalize 只负责组合数据，包括恢复 PII、组装 payload 和邮件线程头；Send 才执行不可逆的 SMTP 调用。分离两者使部分执行后的重启更安全。
- **15 分钟重新验证阈值是工程选择，不是神奇常数。**15 分钟以内客户状态通常不会显著变化；超过 15 分钟则更可能出现 CRM 更新。该值可通过环境变量按租户调整。如果长时间暂停后 `context_hash` 发生变化，`Summarize Changes` 会在同一 Slack 线程中发布差异，而不是静默重写草稿，使审批人基于完整新信息重新决定。
- **拒绝路径会记录原因并限制循环次数。**点击 Reject 后弹出“为什么？（可选）”模态框，原因保存为 `rejection_reason` 并作为下一次 Draft 的附加上下文。拒绝三次后进入 Manual Queue。

## 实现规则（LangGraph 特有）

下面两条规则可以避免 LangGraph HITL 中最常见的问题。违反后都可能产生难以察觉的静默故障。

**规则 1——`interrupt()` 必须位于独立节点。**DB 写入、MCP 调用和审计记录都不能与它共享节点。从 interrupt 恢复时，该节点会从头重启，因此 interrupt 前的代码会再次执行并重复产生副作用。副作用应放在其他能够确保每次恢复只运行一次的节点中。

**规则 2——不要用 `try/except` 包裹 `interrupt()`。**`interrupt()` 通过抛出一个由 LangGraph 运行时捕获的特殊异常实现暂停。宽泛的 `try/except` 会吞掉异常，使图卡住或完全跳过暂停。错误处理应放在其他节点。

## 路由规则

系统采用顺序执行的两道门，而不是一个模糊 Router。

**Gate 1——策略风险检查。**只要满足任意条件就升级：退款或金额表述、愤怒情绪、边界意图、明确命中策略（取消、计费争议、账户恢复、法律问题）。

**Gate 2——置信度检查。**只有 Gate 1 通过后才执行。如果 `intent_confidence < 0.85` 或 `draft_confidence < 0.85`，则升级。

只有当**两道门都通过**，并且 `intent in {FAQ, info, basic_technical}` 时，系统才自动发送。**主要安全指标：`false_auto_send_rate = 0%`。**

## 审批界面

目标是让人工在约 10 秒内完成决策，而不是两分钟。Slack 消息结构为：

1. 客户消息和邮件线程历史；
2. **暂停原因**：展示触发了哪一道门、命中了哪条策略；
3. 检测出的意图和置信度；
4. 可编辑的回复草稿；
5. 三个操作：Approve、Edit & Approve、Reject。

策略依据来自 LLM 生成草稿时使用的同一个 `retrieved_context`，并按原文引用。这是可解释 AI 与“请相信我”的区别。如果 `Revalidate` 检测到暂停期间上下文发生变化，消息会增加差异面板，例如 `account_status: Active → Pending`，人工据此重新决定。

## 失败场景——出错时系统如何处理

每种失败都有明确处理路径，不会静默忽略。

| 故障 | 系统行为 |
|---|---|
| 服务器在暂停期间崩溃 | LangGraph SQLite checkpoint 保存在最近一个 super-step。重启后 Slack 按钮仍可使用；webhook 通过 `slack_message_ts` 在 Interrupt Gate 恢复，状态被完整还原。 |
| SMTP 短暂失败 | `send_retry_count++`，使用同一个 `send_idempotency_key` 最多重试三次。之后进入 `failed_manual` → Manual Queue，并通知 Slack。 |
| 一小时内无人响应 | Agent 再次提醒频道：“⏰ 仍在等待——已通知备用频道。”到 `sla_deadline`（24 小时）仍无响应时，自动转入 Manual Queue。 |
| 客户在暂停期间追发邮件 | `ticket_external_status` 变为 `superseded`，旧草稿被丢弃。Slack 更新：“⚠️ 客户已回复——当前工单被替代，请查看 ticket-XXXX。”追发内容作为新工单进入。 |
| 客户从外部取消工单 | `ticket_external_status` 变为 `cancelled`。Slack 更新：“🚫 客户已取消——关闭工单。”不发送邮件。 |
| 客户邮件包含 Prompt Injection | Read MCP 没有 `send_email` 和 `post_slack`。即使检索阶段受到越狱攻击，在显式 Send / Slack Write 节点前也没有访问 I/O 渠道的路径。能力隔离限制影响范围。 |
| Slack webhook 签名不匹配 | FastAPI 返回 401，不恢复工作流，并记录为安全事件。 |
| Slack 时间戳超过五分钟 | 作为重放攻击防护，返回 401。 |
| 人工连续拒绝三次 | 自动进入 Manual Queue，Slack 显示：“🚦 已拒绝 3 次——转人工队列。”并通过邮件通知客户。 |
| LangSmith 不可用 | Agent 继续运行；trace 在本地缓冲，LangSmith 恢复后重放。可观测性故障不影响用户流程。 |
| LLM 被限流或超时 | 退避后重试一次；第二次仍失败则按低置信度处理，升级给人工。 |
| Hash 未变化，但人工延迟超过 24 小时 | SLA 仍然过期，进入 Manual Queue。时间规则优先于陈旧性检查。 |

## 架构图与代码的对应关系

| 架构图区域 | 文件 |
|---|---|
| Email Listener（IMAP IDLE / poll） | `src/email_listener.py` |
| PII 脱敏与恢复中间件 | `src/pii.py` |
| Classify、Enrich、Draft、Summarize Changes、Finalize、Audit 节点 | `src/nodes.py` |
| 策略与置信度路由、拒绝次数保护 | `src/policy.py` |
| 带优先级覆盖的 Channel Router | `src/slack_router.py` |
| Interrupt 和 checkpointer 接线 | `src/graph.py` |
| FastAPI、Slack webhook HMAC 校验、编辑模态框 | `src/server.py` + `src/slack_handler.py` |
| MCP **Read** Server（CRM + KB） | `mcp_server/support_read.py` |
| MCP **Email Write** Server（Gmail SMTP，幂等） | `mcp_server/support_email_write.py` |
| MCP **Slack Write** Server（post / update / views.open） | `mcp_server/support_slack_write.py` |
| MCP Client Router | `src/mcp_client.py` |
| LLM Client + LangSmith tracing 装饰器 | `src/llm.py` |
| Prometheus 指标 + `@timed_node` 装饰器 | `src/metrics.py` |
| v4 Agent（Researcher、Drafter、Critic） | `src/agents/*.py` |
| ACME SaaS Co 虚构策略语料库 | `data/acme_policies.md` |
| 模拟客户数据库（仿 Salesforce 结构） | `data/customers_seed.json` |
| 重启和恢复集成测试 | `tests/test_resume.py` |

## 可调阈值（环境变量）

| 环境变量 | 默认值 | 控制内容 |
|---|---:|---|
| `REVALIDATE_THRESHOLD_MIN` | 15 | 审批超过多少分钟后，发送前重新验证上下文 |
| `MAX_HUMAN_REJECTIONS` | 3 | 多少次拒绝后，将重新生成循环转入 Manual Queue |
| `MAX_SEND_RETRIES` | 3 | 短暂故障重试上限，之后进入 `failed_manual` |
| `SLA_DEADLINE_HOURS` | 24 | 人工沉默多少小时后，SLA 过期并进入 Manual Queue |
| `IMAP_POLL_INTERVAL_SEC` | 30 | IMAP IDLE 不可用时的轮询间隔 |
| `MULTIAGENT_ENABLED` | 1 | `1` 启用 v4（Researcher + Drafter↔Critic），`0` 运行 v3 单 Agent |
| `HOST` | `127.0.0.1` | FastAPI 绑定地址，默认仅本机。生产或容器部署必须设置 `HOST=0.0.0.0`，见 `docs/threat_model.md` A5 行。 |
| `PORT` | `8000` | FastAPI 绑定端口。 |

完整状态结构见 [`spec.md §5`](../spec.md)，完整环境变量列表见 [`.env.example`](../.env.example)。

## 可观测性

项目接入了两层可观测能力：

- **LangSmith**：`src/llm.py` 中每次 LLM 调用都通过 `@traceable` 记录。失败切片标签 `graph_version`、`intent`、`outcome`、`risk_flags`、`confidence_bucket` 已在 `_ls_metadata` 中确定作用域，但**尚未真正写入 run metadata**；完成接线仍是一个后续提交。
- **Prometheus**：`src/metrics.py` 暴露六组指标：`hitl_tickets_total`、`hitl_node_errors_total`、`hitl_llm_tokens_total`、`hitl_node_latency_seconds`、`hitl_ticket_e2e_seconds`、`hitl_llm_latency_seconds`。FastAPI 挂载 `/metrics`；Grafana 仪表盘位于 `deploy/grafana/dashboards/hitl-overview.json`。Docker Compose 每 15 秒采集一次。

## v4 多 Agent 修订

v4 把 v3 中两个推理负担较重的节点提升为专门 Agent：**Researcher** 替代 `enrich_context_node`，**Drafter ⇄ Critic** 替代 `draft_response_node`，外层图保持不变。15 节点父图拓扑、全部安全门、频道 Router、interrupt/resume 协议和只追加审计语义均与 v3 相同。

通过 `MULTIAGENT_ENABLED=1` 启用 v4；自 2026-05-23 起它是默认值。关闭开关后，v3 保持原样运行。

完整子图接线、硬性不变量和评估目标见 [`docs/v4_multiagent.md`](./v4_multiagent.md)。

## 未来工作

- **把人工编辑作为黄金数据集：**持续积累 `(original_draft, final_draft)` 对，形成高信号微调语料。每周按意图计算编辑距离，并反馈到 prompt 迭代。
- **生产环境改用 Postgres：**SQLite checkpointer 只支持单写者；Postgres 可以支持并发 Agent 和跨地域副本。
- **基于 webhook 的入站邮件：**使用 SES / SendGrid Inbound Parse 替代 IMAP IDLE，在大规模下避免 keepalive 开销，并实现亚秒级延迟。
- **在线评测：**当前为离线评测；增加采样层，对线上 LangSmith trace 评分，使回归在生产中被及时发现，而不是事后发现。
- **多模型路由：**Gate 1 使用较便宜的分类模型（Haiku 级别），草稿使用较强模型（Sonnet 级别），并在完成后量化成本下降。

## 参考资料

- [`spec.md`](../spec.md)：构建规格、完整状态结构（§5）、实现规则（§6.5）
- [`HOW_IT_WORKS.md`](../HOW_IT_WORKS.md)：使用 Jamie 示例讲解的端到端产品叙事
- [`docs/v4_multiagent.md`](./v4_multiagent.md)：v4 规格修订和硬性不变量
- [`docs/threat_model.md`](./threat_model.md)：STRIDE 风格威胁模型
- [`eval/METHODOLOGY.md`](../eval/METHODOLOGY.md)：评测方法、数据集和统计严谨性
