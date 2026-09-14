# ReplyGuard｜人机协同客服 Agent

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](pyproject.toml)
[![测试](https://img.shields.io/badge/tests-159%2F159-brightgreen)](#测试)

ReplyGuard 是一个面向企业邮箱的 AI 客服 Agent。它可以读取客户邮件、生成回复草稿，并在退款、投诉、愤怒客户或策略不明确等高风险场景下暂停，将审批卡片发送到飞书测试企业，由人工决定批准、修改或拒绝后再继续处理。

项目基于 LangGraph，支持腾讯企业邮箱和网易企业邮箱，默认使用飞书作为人工审批渠道。核心安全目标是：false_auto_send_rate = 0%，即不允许高风险邮件被系统误自动发送。

## 核心流程

![ReplyGuard 端到端流程](docs/hitl-flow.png)

1. 从配置的企业邮箱通过 IMAP 接收客户邮件。
2. 在进入模型前对邮箱、姓名等个人信息进行脱敏。
3. 识别客户意图，读取模拟 CRM 和企业政策知识库，并生成回复草稿。
4. 依次经过策略风险检查和置信度检查两道门。
5. 安全且置信度足够高的请求，自动通过 SMTP 回复客户。
6. 其他请求暂停在持久化检查点，并向飞书发送审批卡片。
7. 人工点击“批准 / 修改 / 拒绝”后，流程从暂停位置恢复，完成发送或进入人工处理队列。

客户只会收到企业邮箱中的正常回复，不会看到 Agent 或飞书内部审批过程。

## 演示与截图

- [端到端演示：邮件 → 飞书审批 → 邮件回复](https://www.loom.com/share/1dcea3327a774699a705acf79eaab9d4)
- [可观测性演示：Grafana 与 LangSmith](https://www.loom.com/share/c1d9a80faf3f453aa3447f525d34ff28)

![飞书审批卡片](docs/screenshots/slack-approval-card.png)

![Grafana 监控面板](docs/screenshots/grafana-dashboard.png)

## 主要设计

- **两道安全门**：先做策略风险检查，再做置信度检查，避免用一个模糊路由器决定是否自动发送。
- **人工审批可恢复**：使用 LangGraph interrupt() 和 SQLite 检查点，服务重启后仍可从暂停位置继续。
- **工具能力隔离**：三个 MCP 服务分别负责读取信息、发送邮件和发送审批卡片，读取工具没有发送能力。
- **发送幂等**：应用层记录幂等键，避免流程重试导致同一封邮件重复发送。
- **隐私保护**：入口脱敏，最终发送前恢复必要信息；审计记录不保存原始个人信息。
- **有界重试**：拒绝次数、模型修订次数和 SMTP 重试次数均有上限，避免无限循环。
- **飞书回调校验**：校验验证令牌，快速响应回调，再恢复对应的工作流。

详细说明：

- [中文端到端流程](HOW_IT_WORKS.zh-CN.md)
- [中文架构说明](docs/architecture.zh-CN.md)
- [架构与状态流转](docs/architecture.md)
- [威胁模型](docs/threat_model.md)
- [v4 多智能体设计](docs/v4_multiagent.md)
- [评估方法](eval/METHODOLOGY.md)

## 当前状态

- v3 单 Agent 流程和 v4 多 Agent 流程均保留，可通过环境变量切换。
- v4 包含 Researcher、Drafter 和 Critic 三类 Agent，并使用有界修订循环。
- 当前测试：**159 / 159 通过**。
- 27 类 Bitext 宽度评估已经暴露出分类器在部分非目标场景下仍可能误判，不能把当前版本当作无需人工监督的生产系统。

切换版本：

```powershell
$env:MULTIAGENT_ENABLED = "1"  # v4 多 Agent，默认
python -m src.server

$env:MULTIAGENT_ENABLED = "0"  # v3 单 Agent，对照版本
python -m src.server
```

## 快速开始

### 1. 创建环境并安装依赖

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e ".[dev]"
```

### 2. 配置服务

在 .env 中填写模型、企业邮箱和飞书应用配置。不要把真实密钥提交到 GitHub。

```dotenv
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=你的模型密钥

# 腾讯企业邮箱；改成 netease 可切换为网易企业邮箱
EMAIL_PROVIDER=tencent
EMAIL_USER=support@yourcompany.com
EMAIL_APP_PASSWORD=邮箱客户端授权码

APPROVAL_PROVIDER=feishu
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=你的飞书应用密钥
FEISHU_RECEIVE_ID_TYPE=chat_id
FEISHU_RECEIVE_ID=oc_xxx
FEISHU_VERIFICATION_TOKEN=飞书回调验证令牌
```

腾讯和网易的服务器地址、端口已经写入 .env.example。邮箱要开启 IMAP/SMTP，并使用客户端授权码，不要直接使用网页登录密码。飞书应用需要启用机器人，并授予发送消息和接收事件所需权限。

### 3. 运行测试

```powershell
pytest
python -m eval.run_experiments --no-llm
```

### 4. 启动服务

```powershell
python -m src.server
```

启动后，服务会监听企业邮箱；飞书回调地址为：

```text
POST /feishu/events
```

真实飞书事件订阅通常需要可访问的 HTTPS 地址。仅在本机测试时，可以使用安全的内网穿透或测试环境网关，并注意不要暴露邮箱密钥和个人信息。

## 费用说明

本项目代码采用 MIT 许可证，本身可以免费使用。实际运行是否产生费用，取决于外部服务的选择：

- 企业邮箱：取决于腾讯企业邮箱或网易企业邮箱的版本和账号数量。
- 大模型：取决于 OpenRouter、OpenAI 或其他模型服务的调用量；本地无模型密钥时只能运行不调用模型的测试。
- 飞书：测试企业和应用能力以飞书当前规则为准。
- LangSmith、服务器和 HTTPS 内网穿透：只在启用对应能力时产生潜在费用。

## 项目结构

```text
src/                 主流程、邮箱、飞书回调、策略和可观测性
src/agents/          v4 Researcher、Drafter、Critic
mcp_server/          读取、邮件发送、飞书审批三个 MCP 服务
data/                政策、客户种子数据和评估数据
eval/                离线评估、对抗评估和结果分析
tests/               单元测试与端到端集成测试
docs/                架构、威胁模型和中文流程文档
deploy/              Prometheus 与 Grafana 配置
```

## 重要说明

“ReplyGuard｜人机协同客服 Agent”是项目对外展示名称。为保证现有导入路径、历史记录和部署脚本继续可用，仓库目录、Python 包和部分兼容字段仍保留 hitl-support-agent 或旧版 Slack 命名；这些属于内部兼容标识，不影响默认使用腾讯/网易企业邮箱和飞书。

完整构建约束、失败模式和安全边界请先阅读 spec.md、HOW_IT_WORKS.zh-CN.md 和 docs/threat_model.md。
