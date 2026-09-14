# 工作原理——端到端产品流程

> 一个生产风格的客户支持系统，将 LLM 推理、确定性策略执行、人工审批工作流和持久化执行结合起来。系统使用真实邮件收发、真实的多频道 Slack 审批、虚构的 ACME SaaS Co 策略知识库，以及三个按能力隔离的 MCP Server。

这是项目的标准叙事文档：面试时可以打开本文讲解，也可以把其中部分内容放进 README，或在演示中按本文走完整流程。`spec.md` 是构建规格，`docs/architecture.md` 包含架构图、状态结构和 LangSmith 标签表，而本文负责讲清楚系统如何工作。

运行时分为七层（表格见 `docs/architecture.md`）：**接入层**（IMAP/SMTP）、**编排层**（LangGraph + SQLite checkpointer）、**智能层**（通过 OpenRouter 调用 LLM）、**策略层**（两道门控路由 + ACME 知识库检索）、**HITL 层**（Slack 通知 + interrupt + 操作处理器）、**执行层**（Finalize + Send + Audit）、**可观测层**（LangSmith trace + 成本跟踪）。下面的每一步都对应其中一层；这些层名也与代码目录结构相呼应。

---

## 准备工作（首次处理工单前完成一次）

你有一个启用了 IMAP 和应用专用密码的 Gmail 账户 `support@yourcompany.com`。Slack 工作区中为本项目配置了三个频道：`#support-refunds`、`#support-technical`、`#support-complaints`，团队成员都已加入这些频道，Agent 也已启动。

（规格中描述了六个频道，还包括 `#support-billing`、`#support-enterprise`、`#support-legal`，并采用四级优先路由。为了把构建时间控制在 6～10 小时，实际实现缩减为三个频道。参见文末“从规格中删减的内容”和 `src/slack_router.py` 的 docstring。以后若决定加入这些频道，只需修改配置，无需重写工作流。）

---

## 第 1 步——客户发送邮件

假设客户名叫 Jamie。她在手机上打开 Gmail，写道：

> *收件人：support@yourcompany.com*  
> *主题：请退款*  
> *我想申请 200 美元退款——你们的衬衫不合身。*

她点击发送。邮件经过 Gmail 服务器，进入 `support@yourcompany.com` 的收件箱。**Jamie 开始等待。**从她的视角看不到任何内部流程，只看到手机回到了收件箱。

## 第 2 步——Email Listener 收到邮件（IDLE 约 1 秒，轮询降级约 30 秒）

后台监听器 `src/email_listener.py` 通过 **IMAP IDLE** 连接 `support@yourcompany.com`。新邮件到达后，Gmail 通常会在一秒内推送通知。如果 IDLE 断开或不可用，监听器会降级为每 30 秒轮询一次。大规模生产环境一般会使用 SES、SendGrid Parse 或 Postmark 等基于 webhook 的入站邮件服务，在避免 IDLE 长连接开销的同时实现亚秒级延迟。

监听器解析出：

- `from`：`jamie@example.com`
- `subject`：`Refund please`
- `body`：`I want a $200 refund...`
- `email_thread_id`：RFC-822 Message-ID，第 14 步中需要用它将回复归入原邮件线程

然后生成新的 `ticket_id`（同时也是 LangGraph 的 `thread_id`）和唯一的 `send_idempotency_key`，再把工单送入 LangGraph 工作流。

## 第 3 步——PII 脱敏

在任何 LLM 看到 Jamie 的文本之前，正则中间件会把邮箱、信用卡号、电话号码替换为 `[EMAIL_1]` 之类的令牌，原值保存在状态中。从这里到第 14 步，LLM 只能看到脱敏后的文本。

## 第 4 步——意图分类

通过 OpenRouter 调用 DeepSeek V3，询问：“客户想做什么？判断有多大把握？情绪如何？是否有风险标记？风险等级是什么？”

返回：`intent=refund, intent_confidence=0.88, sentiment=neutral, risk_flags=["refund"], risk_level=financial`。

## 第 5 步——补充上下文

Agent 调用 **MCP Read Server**，并行执行三个请求：

- `get_crm_profile(jamie@example.com)` → `customer_tier=Standard, contract_value=$240/yr, joined=2024-08, billing=current`
- `get_customer_history(jamie@example.com)` → 两张历史工单，均已解决
- `get_kb_article("refund $200")` → 原样返回 ACME Policy 4.2.1：“100～500 美元退款需要一级客服批准。”

`customer_tier` 被写入状态，并在第 8 步参与频道路由。Agent 还会根据全部检索结果计算 `context_hash`；如果人工审批耗时较长，第 12 步会用到它。

和图中的其他步骤一样，本步骤会写入 LangSmith trace。每个节点调用、LLM token 和 MCP 调用都会归入该工单的一条 trace，并带上标签，以便按失败切片分析。详见 `docs/architecture.md` 中的 LangSmith 标签表。

## 第 6 步——生成回复草稿

LLM 获得完整上下文，并收到提示：“以 ACME Policy 4.2.1 为依据，为 Jamie 写一封回复。”返回 `original_draft` 和 `draft_confidence=0.92`。

## 第 7 步——策略风险检查（Gate 1）

确定性规则检查检索到的策略和 `risk_flags`。ACME Policy 4.2.1 明确规定：“100～500 美元退款需要一级客服批准。”因此**检测到风险**，并写入 `policy_matches=["ACME 4.2.1"]`。此时完全跳过 Gate 2，因为策略已经明确要求人工审批，无需再检查模型置信度。

→ 路由到 Channel Router。

> 如果这是一条简单 FAQ，例如“如何重置密码？”，Gate 1 和 Gate 2 都会通过，流程会直接跳到第 13 步，不需要人工参与。**这就是自动发送路径：大约 3 秒、零人工。**下面继续介绍人工审批路径。

## 第 8 步——Channel Router 选择正确的 Slack 频道

当前实现有两级优先级，按首次匹配生效：

1. **客户是否愤怒？**否，当前情绪为 neutral；如果是，则进入 `#support-complaints`。
2. **按意图路由？**是，`intent=refund`，因此进入 **`#support-refunds`**。

将 `slack_channel="#support-refunds"` 保存到状态。

> 规格中的完整四级路由为 `legal/compliance > Enterprise+risk > angry > intent`，目前按 `src/slack_router.py` 的 docstring 延后实现。在当前版本中，既不是 `refund`、也不是 `angry` 的请求都会进入兜底频道 `#support-technical`。

## 第 9 步——发送 Slack 通知（必须发生在 interrupt 之前）

Agent 调用 **MCP Slack Write Server** 的 `post_approval_request`。`#support-refunds` 中出现一条 Block Kit 消息：

```text
🟡 ticket-4421 · 退款 $200

客户：jamie@example.com（Standard，每年 $240，两张历史工单 ✅）
意图：refund (0.88)   情绪：neutral

暂停原因：
  • policy_match: ACME 4.2.1
  • 提到了金额：$200

依据（来自知识库）：
  “根据 ACME 4.2.1，100～500 美元退款需要一级客服批准”

[回复草稿 ▼]
[批准]   [编辑]   [拒绝]
```

Slack 返回消息时间戳，系统把它保存为 `slack_message_ts`。**发送 Slack 消息是一个独立节点，这里没有 `interrupt()`。**

## 第 10 步——Interrupt Gate（只负责暂停的独立节点）

工作流在另一个节点中调用 `interrupt()`，该节点不做任何其他事情。根据**实现规则 1**，重复发送 Slack 等副作用,必须位于另一个节点，避免恢复执行时重复触发。

> **本项目遵守两条 LangGraph 规则。它们不是可选建议；违反后都可能产生不明显的静默故障：**
>
> **规则 1——**`interrupt()` 必须独占一个节点。LangGraph 从 `interrupt()` 恢复时，包含该调用的节点会从头重新执行，因此 interrupt 之前的代码会在每次恢复时重跑。如果该节点包含副作用，就会重复执行。正确模式是把副作用放到其他能够确保每次恢复只运行一次的节点中。
>
> **规则 2——**不要用 `try/except` 包裹 `interrupt()`。`interrupt()` 的工作原理是抛出一个由 LangGraph 运行时捕获的特殊异常。宽泛的 `try/except` 会直接吞掉它，使图卡住，或者完全跳过暂停。错误处理应放在其他节点中，或者不应该包裹interrupt。

SQLite checkpointer 已经在上一个 super-step 边界保存状态。**执行会真正暂停。**即使服务器此时崩溃，重启后 Jamie 的工单仍然存在：状态、草稿和 Slack 消息都保持不变，消息按钮仍然有效，因为 webhook 可以通过 `slack_message_ts` 找到需要恢复的流程。

> **为什么顺序如此重要：**`interrupt()` 一旦触发，执行立即暂停，该节点后面的代码不会运行。如果把 Channel Router 和 Slack Notification 放在 interrupt 后面，它们永远不会执行，工作流会在没有发出任何 Slack 通知的情况下永久暂停。正确顺序永远是：Slack post → Interrupt Gate → resume。

**Jamie 仍在手机前等待，并不知道后台发生了什么。**

## 第 11 步——Sarah 在 Slack 中操作

值班客服 Sarah 在 `#support-refunds` 中看到消息。“暂停原因”面板和 ACME 策略原文让她可以在约 10 秒内作出决定，存在三条子路径：

> **11a. 批准。**Sarah 点击 Approve。Slack 向 FastAPI 服务 `src/slack_handler.py` 发送 webhook。处理器：
>
> 1. 读取 `X-Slack-Request-Timestamp` 和原始请求体；
> 2. 计算 `HMAC-SHA256(signing_secret, "v0:" + timestamp + ":" + body)`，并与 `X-Slack-Signature` 做常量时间比较；
> 3. 签名不匹配或时间戳超过 5 分钟时返回 401，以防重放攻击。
>
> 验证通过后调用 `Command(resume="approve")`，工作流从 Interrupt Gate 醒来。Slack 通过 `update_message` 原地更新为：“✅ @sarah 已批准 · 22 秒”。

> **11b. 编辑。**Sarah 点击 Edit。Slack Write Server 使用 `views.open` 弹出 Slack 模态框，不跳转到其他网页。草稿已预填，她修改一句话、补充道歉后保存。`original_draft` 和 `final_draft` 都会写入状态。随后调用 `Command(resume="edit")`，Slack 更新为：“✏️ @sarah 已编辑并批准 · 47 秒”。

> **11c. 拒绝。**Sarah 点击 Reject，系统弹出小型模态框询问“为什么？（可选）”。她填写“语气太正式，请更友好一些”，内容保存为 `rejection_reason`。Agent 检查 `human_rejection_count`：少于 3 次时，**同一个 LangGraph thread** 重新进入 Draft 节点，不会启动新线程。`thread_id` 和此前所有状态均保留，只增加 `rejection_reason` 并执行 `human_rejection_count++`。Draft 节点把拒绝原因作为附加上下文用于重新生成。新草稿作为原 Slack 消息的线程回复发送，使团队可以直接看到完整审计历史。达到 3 次后，工单进入 **Manual Queue**，频道中显示“🚦 已拒绝 3 次——转人工队列”，系统通过邮件通知客户，之后由 Sarah 的团队全程人工处理。

在 Jamie 的示例中，假设 Sarah 点击了**批准**。

## 第 12 步——检查审批耗时

审批是否超过 15 分钟？Sarah 只用了 22 秒，所以**没有**。跳过重新验证，直接进入 Finalize。

> **为什么是 15 分钟？**这是可调的工程决策，不是神奇常数。15 分钟以内，客户状态发生重大变化的概率较低；超过 15 分钟，则更可能出现 CRM 更新，例如订阅变更、新工单或计费事件。阈值通过环境变量 `REVALIDATE_THRESHOLD_MIN` 配置，并可按租户调整。

> **慢路径：**如果 Sarah 外出吃饭，两小时后才批准，Agent 会再次调用 MCP Read Server，重新计算并比较 hash。如果 Jamie 的账户状态在此期间发生变化，例如升级为 Enterprise，**Summarize Changes** 节点会生成差异，并通过同一 Slack 线程上的 `update_message` 提示：“⚠️ 上下文已变化，请重新确认。”工作流再次 interrupt，等待 Sarah 基于新信息重新决定。只有上下文未变化时才继续执行。

## 第 13 步——Finalize Action

这是纯组装步骤，还没有不可逆副作用：

- 恢复 PII：将 `[EMAIL_1]` 替换为真实邮箱；
- 组装邮件负载，并包含**三个线程关联字段**。Gmail 会同时使用它们，缺少任何一个都可能偶发破坏邮件线程：
  - `In-Reply-To: <original Message-ID>`
  - `References: <original Message-ID>`，并拼接此前的线程 ID
  - `Subject: Re: Refund please`，主题必须以 `Re: ` 开头才能匹配
- 附加幂等键。

## 第 14 步——发送邮件（整张图中唯一不可逆的步骤）

Agent 调用 **MCP Email Write Server** 的 `send_email`。

**应用层幂等：**SMTP 本身不会去重。每次调用前：

1. 检查状态中是否已有 `sent_message_id`；
2. 如果有，则跳过发送，返回缓存的 `sent_message_id`，确保恢复或重跑工作流时不会重复发信；
3. 如果没有，则调用 SMTP，并保存返回的 `sent_message_id`。

`send_idempotency_key` 用于查找，状态中的字段充当锁。Send 节点是整张图里唯一发生不可逆副作用的地方。此前的步骤都可以撤销或重跑：可以重新思考、重新生成草稿。这就是代码中“幂等发送”的实际含义。

出现短暂故障（例如网络抖动）时，`send_retry_count` 增加，最多重试三次；相同幂等键可以防止重复发送。三次后仍失败，则进入 `failed_manual` → Manual Queue，并在频道中通知。

状态变化为：`send_status: pending → in_flight → sent`。

## 第 15 步——最后一次更新 Slack 消息

Agent 调用 Slack Write Server 的 `update_message`，`#support-refunds` 中的原消息变为：

```text
✅ ticket-4421 · @sarah 已批准 · 22 秒
📤 已于 14:05:30 回复 jamie@example.com
```

团队在日常工作的 Slack 中就能看到完整审计信息。

## 第 16 步——结束审计日志和 LangSmith trace

系统向审计日志追加一行：工单 ID、意图、置信度、原草稿、最终草稿、批准人（`@sarah`）、Slack 频道及消息链接、决策耗时（22 秒）、成本（$0.0034）、token 数（892）和 trace URL。

LangSmith trace 结束时带有这些标签：`outcome=escalated, human_edited=false, slack_channel=#support-refunds, final_state=sent, intent=refund, risk_flags=refund, confidence_bucket=gte_0.85`。

## 第 17 步——Jamie 收到回复

Jamie 的手机发出通知。Gmail 中出现一封新邮件，并且**归在她原来的“Refund please”邮件线程下**：

> *发件人：support@yourcompany.com*  
> *主题：Re: Refund please*  
> *Hi Jamie，很抱歉这件衬衫不合身。我们已经发起 200 美元退款，款项将在五个工作日内退回你的银行卡……*

**从她点击发送到收到回复的总耗时：**采用轮询降级时约 55 秒（30 秒 IMAP 轮询 + Sarah 决策的 22 秒 + 其他处理耗时）；采用 IMAP IDLE 时约 25 秒。

她不知道 Agent 生成过草稿、Router 选择过频道、系统引用过 ACME 4.2.1，也不知道人工做过审批。她只感受到：一条退款请求得到了快速、准确、具有人类服务质量的回复，而且邮件线程显示正确，就像来自真正的客服团队。

---

## 同样的流程，但 Jamie 非常愤怒

假设 Jamie 写的是：“这已经是第三次了。我非常愤怒。马上把钱退给我，否则我就联系律师。”

第 1～7 步的结构不变。分类器返回 `sentiment=angry (0.94), risk_flags=["refund","angry","money_mention"], risk_level=financial`。

**Channel Router** 按优先级判断：

1. **是否愤怒？是。**频道设为 **`#support-complaints`**。

即使它同时也是退款请求，仍然由 *angry 优先*。在当前三个频道的实现中，带有强烈情绪的升级请求进入 `#support-complaints`，由经验更丰富的客服先缓和情绪，再处理具体问题。

Slack 消息会进入 `#support-complaints`，仍然展示风险标记、情绪、客户历史、草稿和策略引用，但该频道由高级支持人员关注。他们能立刻看到律师相关表述，并选择批准谨慎的回复，或拒绝草稿，让 Agent 以更平和的语气重新生成；修订时 Critic 会倾向保守。

Jamie 最终仍然通过真实邮件收到归入原线程的回复。内部路由对她不可见，但这恰恰是作品集 Demo 与真实产品之间的区别。

> 规格中的四级路由会因为“律师”一词把请求送到 `#support-legal`。该路由目前延后实现，详见 `src/slack_router.py` 的 docstring。恢复它只需增加常量，并在 angry 判断前增加优先级检查，不需要修改图结构。

---

## 失败场景——出错时系统如何处理

| 故障 | 系统行为 |
|---|---|
| 服务器在暂停期间崩溃 | LangGraph SQLite checkpoint 保存在最近一个 super-step。重启后 Slack 按钮仍可使用；webhook 通过 `slack_message_ts` 在 Interrupt Gate 恢复，状态被完整还原。 |
| SMTP 短暂失败 | `send_retry_count++`，使用相同 `send_idempotency_key` 最多重试三次，并通过状态中的 `sent_message_id` 做应用层检查。三次后进入 `failed_manual` → Manual Queue，并通知 Slack。 |
| 一小时内无人响应 | Agent 再次提醒频道：“⏰ 仍在等待——已通知备用频道。”到 `sla_deadline`（24 小时）仍无响应时，自动转入 Manual Queue。 |
| 客户在暂停期间追发邮件 | `ticket_external_status` 变为 `superseded`，旧草稿被丢弃。Slack 更新：“⚠️ 客户已回复——当前工单被替代，请查看 ticket-XXXX。”追发内容作为新工单进入。 |
| 客户从外部取消 | `ticket_external_status` 变为 `cancelled`。Slack 显示：“🚫 客户已取消——关闭工单。”不发送邮件。 |
| 客户邮件包含 Prompt Injection | Read MCP Server 没有 `send_email` 和 `post_slack`。即使检索阶段受到越狱攻击，在显式执行 Send 或 Slack Write 节点之前，Agent 也没有访问任何 I/O 频道的路径。能力隔离限制了故障影响范围。 |
| Slack webhook 签名不匹配 | FastAPI 返回 401，不恢复工作流，并记录为安全事件。 |
| Slack 时间戳超过五分钟 | 作为重放攻击防护，返回 401。 |
| 人工连续拒绝三次 | 自动进入 Manual Queue。Slack 显示：“🚦 已拒绝 3 次——转人工队列。”并通过邮件通知客户。 |
| LangSmith 不可用 | Agent 继续运行；trace 在本地缓冲，LangSmith 恢复后重放。可观测性故障不影响用户流程。 |
| LLM 被限流 | 退避后重试一次；第二次仍失败则按低置信度处理，升级给人工。 |
| Hash 未变化，但人工延迟超过 24 小时 | SLA 仍然过期，进入 Manual Queue。时间规则优先于陈旧性检查。 |

---

## 哪些是真实组件，哪些是模拟组件

| 层 | 真实 | 模拟 |
|---|---|---|
| **I/O 渠道** | Gmail IMAP IDLE（收件）+ SMTP（发件），真实 Slack 多频道路由 | — |
| **LLM / 可观测性** | OpenRouter（DeepSeek）、LangSmith tracing | — |
| **编排** | LangGraph + SQLite checkpointer | — |
| **工具** | 三个按能力拆分的 MCP Server：Read / Email Write / Slack Write | — |
| **评测数据** | 10 张人工整理的工单，每条代码路径一张（`eval/dataset.py`）；外部 Bitext 基准评测在原文此处标为推迟到 v4.1 | — |
| **客户数据库** | — | `data/customers_seed.json`，结构仿 Salesforce |
| **CRM 档案** | — | 模拟的 `get_crm_profile` 返回结构化数据 |
| **策略语料库** | — | `data/acme_policies.md`，虚构的 ACME SaaS Co |
| **工单系统** | — | 邮件直接进入 LangGraph，没有 Zendesk |

任何一层替换成生产系统都应是 MCP 配置变更，而不是重写整张图。

---

## 如何证明系统确实有效（评测）

项目通过 LangSmith 对 **10 张人工整理的工单**运行五种评估器。每张工单对应一条代码路径，并覆盖一个不同的图分支，见 `eval/dataset.py`。之后也运行了一次包含 10 张真实 Bitext 工单的评测，见 `eval/bitext_findings.md`；它只覆盖 Bitext 27 个意图中的 10 个，更大规模的外部评测仍属于未来工作。

1. **意图准确率**：与标签做精确匹配。
2. **回复质量**：按照 rubric 使用 LLM-as-judge 评分。
3. **升级精确率**：两道门控是否正确地把请求交给人工。
4. **`false_auto_send_rate`**：主要安全指标，目标为 0%。真正关键的问题是：Agent 是否曾经把本应升级的退款、愤怒或策略敏感回复自动发给客户？
5. **失败切片分析**：按照 `intent × risk_flags × confidence_bucket` 分解准确率，从而定位 v1 → v2 → v3 的改进发生在哪里。

每条 LangSmith trace 都带有 `graph_version`、`intent`、`outcome`、`human_edited`、`final_state`、`risk_flags`、`confidence_bucket` 等标签。完整标签表见 `docs/architecture.md`。

---

## 用一句话概括整个系统

Jamie 发送邮件 → IMAP 收到邮件 → Agent 分类并生成草稿 → 简单 FAQ 自动发送，其他请求则**按优先级进入正确的 Slack 频道 → 持久化暂停 → 人工点击批准、编辑或拒绝 → 恢复执行** → SMTP 发送归入原线程的回复 → Jamie 的手机收到通知。客户不会看到 Agent 或 Slack。

---

## v4 如何改变这个流程

> 上面的 Jamie 退款流程描述的是 v3 模式。v4 在保留第 7 步之后全部流程的前提下，引入三个专门 Agent。本节说明 `MULTIAGENT_ENABLED=1` 时，第 5 步（补充上下文）到第 7 步（策略风险检查）之间发生了什么变化。

### 完全不变的部分

- 第 1～4 步：接入、PII 脱敏、意图分类，完全相同；
- 第 7～17 步：门控、频道路由、Slack 通知、interrupt、resume、finalize、send、audit，完全相同；
- 硬性不变量：确定性 PII 处理、`false_auto_send_rate=0%`、`interrupt_gate` 隔离、幂等发送、只追加审计日志、MCP 能力隔离。

### 第 5 步变为 Researcher Agent

Jamie 的退款工单进入原来的 `enrich_context_node` 位置。在 v4 中，这里变为一个已编译的子图——**Researcher Agent**，在 MCP Read 工具上执行 ReAct 循环。退款意图会触发完整检索：Researcher 依次调用 `get_kb_article`、`get_crm_profile`、`get_customer_history`，然后以与 v3 相同的结构返回补充结果。

对 Jamie 来说，其功能与 v3 的确定性节点相同；差异会出现在“如何重置密码？”之类的简单 FAQ 中，此时 Researcher 只查询知识库，跳过 CRM。审计日志新增 `researcher_agent` 条目，其中记录 `tools_called=["get_kb_article","get_crm_profile","get_customer_history"]`。结果与 v3 一致，但 Agent 自主选择工具的过程可以在 trace 中看到。

### 第 6 步变为 Drafter ↔ Critic 循环

原本的一次 LLM 调用变为紧凑的双 Agent 子图。**Drafter Agent** 先生成 v1 草稿和 `draft_confidence`，结构与 v3 相同。随后 **Critic Agent** 读取草稿以及 Researcher 检索到的策略原文，输出 `accept` 或 `revise`，同时给出严重程度和反馈。

如果结果为 `accept`，循环结束，当前草稿原样进入 Gate 1。如果结果为 `revise`，且当前仍是第 0 或第 1 次迭代，Drafter 会把 Critic 反馈加入 prompt 后重写，Critic 再检查新草稿。

循环受 `MAX_CRITIC_ITERATIONS = 3` 限制：最多调用 Drafter 三次、最多修订两次，之后无论 Critic 给出什么结果都会退出，因此**不会无限循环**。Critic 严重程度按 `draft_confidence *= (1 - severity * 0.5)` 影响草稿置信度，因此 Critic 只能降低置信度，不能提高。如果 Critic 返回无法解析的 JSON，系统安全失败：默认结果为 `revise`、严重程度为 0.5，并升级给人工，而不是静默放行。

### 真实评测中的表现

项目让 10 张工单分别经过 v3 和 v4 的真实 LLM 调用。两个模式下的 **`false_auto_send_rate` 都保持为 0%**，确定性安全契约没有被破坏。一张工单 `eval-t07` 原本是高置信度信息咨询，在 v3 中自动发送，在 v4 中被升级，因为 Critic 把 `draft_confidence` 降到了 Gate 2 的 0.85 阈值以下。

因此，v4 以小幅降低升级精确率为代价，为每份草稿增加一次 Critic 检查，同时没有削弱确定性安全契约。

### v4 没有改变什么

Jamie 的体验与 v3 完全相同。她的手机仍然收到真实邮件回复，回复仍归在原来的“Refund please”线程下，看起来仍像真正客服团队发出的邮件。内部在 Slack 之前的生成过程从两个确定性节点变成了三 Agent 管线（Researcher → Drafter ↔ Critic），但 Slack 通知、人工审批、邮件线程头、幂等发送和审计日志均保持不变。

### 相关链接

- 架构约束与不变量：`docs/v4_multiagent.md`
- 原始评测产物：`eval/results_v4_live.json` 与 `eval/results_v3_live.json`
- 代码：`src/agents/{researcher,drafter,critic}.py`
