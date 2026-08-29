# afuture — ChatGPT Project Brief

> 本文件只保存供 ChatGPT Work、Codex 和工程协作者按需加载的长期稳定事实。动态分支、SHA、PR、任务进度和验证结果必须记录在 GitHub Issue 或 PR 中，不得写入本文件。

## 项目目标与定位

`afuture` 是面向国内商品期货的 Python 交易工程，覆盖数据校验、策略研究、历史回放、Shadow 运行和受控实盘。项目按单用户、单账户设计，重点保证交易时序、账户记账、风险控制、状态恢复和研究证据可核验；它不是多账户平台、分布式交易平台或远程管理服务。

系统支持两条账户互斥的正式运行链：

- 跨期价差策略（固定组合或自动选择相邻月份合约）；
- 方向组合策略（使用已完成交易日数据生成品种目标，再选择具体合约和整数手数）。

项目定位、能力边界和常用入口以 [`README.md`](../README.md) 为准；术语、模块和事件顺序以 [`docs/architecture.md`](../docs/architecture.md) 及 [`docs/glossary.md`](../docs/glossary.md) 为准。

## 核心架构与模块边界

稳定依赖方向：统一数据模型和配置 → 纯计算与策略规则 → 策略、风控和订单计划 → 运行时编排 → Broker、持久化、命令行和报告。

| 边界 | 长期职责 |
| --- | --- |
| 策略与研究 | 生成信号、候选、意图或目标；不拥有账户真相，不直接修改持仓。 |
| `RiskManager` 与执行层 | 执行账户硬限制、只减仓和停机约束；风险收缩层只能降低目标。 |
| Broker / CTP | 提供订单、成交、账户、柜台持仓、活动委托、合约和交易日的外部权威事件。 |
| `PositionBook` | 仅根据 Broker 成交维护本地持仓镜像。 |
| `StateStore` | 保存带 schema/version、正序号和校验和的重启证据；不得自动采用损坏状态或 `.prev`。 |
| Directional activity、OHLC、Stress-90 OI sidecar | 保存市场输入证据；不拥有账户、持仓或 Broker 真相。 |
| `TradingEngine` | 编排事件顺序、对账、持久化和运行观测。 |
| 审计、告警和报告 | 只负责观测，不拥有下单或最终风控权限。 |

完整边界和数据流见 [`docs/architecture.md`](../docs/architecture.md)。

## 关键数据与状态 Owner

| 事实或状态 | 权威 Owner |
| --- | --- |
| 外部订单、成交、账户、柜台持仓、活动委托、合约和权威交易日 | Broker / CTP |
| 本地持仓镜像 | `PositionBook`，且只能由成交推进 |
| 账户硬限制、`REDUCE_ONLY`、`HALTED` | `RiskManager` |
| 运行编排和对账顺序 | `TradingEngine` |
| 重启证据 | `StateStore` 当前版本化状态；`.prev` 只供人工检查 |
| 当前项目契约 | README、架构、策略、配置、数据、实盘和生产检查文档 |
| 当前任务现场 | 最新 GitHub Issue、PR 和实时只读 Git/GitHub 核验；实时核验高于旧评论、旧交接或聊天摘要 |

文档权威性分类见 [`docs/documentation-index.md`](../docs/documentation-index.md)。历史记录、研究证据和开发计划不得覆盖当前权威文档。

## 不可改变的业务约束

- 策略只能产生意图或目标；只有 Broker 成交事件可以改变持仓、现金、盈亏和权益。
- 未成交、撤单和拒单不改变账户；部分成交只按实际数量记账；成交必须按稳定身份去重。
- 反转和风险增加遵守 reduction-first：先完成减仓，再允许新增风险。
- D 日完整数据最早只能影响 D+1；不得使用未完成交易日、未来数据或本机日期猜测交易日。
- 生产 Directional 必须显式选择 `execution_aligned` 或 `stress90`。前者保留 0.25 target scaling；Stress-90 使用 1x raw candidate，不得重复套用 0.25 缩放。
- Stress-90 drawdown reserve 与 HHI freeze 只阻止新增风险；最终硬风险权限仍属于 `RiskManager`。
- 风险状态只有 `RUNNING`、`REDUCE_ONLY`、`HALTED`；策略和执行优化不得绕过风险收缩、强制减仓或停机。
- 方向组合示例硬限制：目标和实际总敞口不超过权益 2 倍、保证金不超过 35%、可用资金不低于权益 25%、单日亏损达到 5% 时停止新增风险、总回撤达到 30% 时硬停机、单合约不超过 35 手。实际运行仍以配置和 `RiskManager` 为准。
- Shadow 只在本地模拟订单，绝不向柜台报单。
- 代码 wiring、离线验证、Shadow、测试柜台、极小真钱和风险扩大是相互独立的阶段；前一阶段不能自动授权后一阶段。
- 状态、schema、sequence、checksum、identity、日期或数值不可信时必须失败关闭。
- 历史模拟和离线研究不是未来收益承诺，不能替代目标机 CTP、Shadow、测试柜台和小资金验证。

策略细节以 [`docs/strategies.md`](../docs/strategies.md) 为准，配置和生产安全边界以 [`docs/configuration.md`](../docs/configuration.md)、[`docs/live-trading.md`](../docs/live-trading.md)、[`docs/stress90-live-runbook.md`](../docs/stress90-live-runbook.md) 和 [`docs/production-checklist.md`](../docs/production-checklist.md) 为准。

## 标准安装、构建与验证命令

最低 Python 版本为 3.10。依赖和工具配置以 [`pyproject.toml`](../pyproject.toml)、[`constraints/core-dev.txt`](../constraints/core-dev.txt) 和 [`constraints/live.txt`](../constraints/live.txt) 为准。

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]" -c constraints/core-dev.txt

python -m pip check
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python -m mypy afuture
python -m compileall -q afuture
```

常用非实盘运行入口：

```bash
afuture validate --config config/afuture.example.toml
afuture replay --config config/afuture.example.toml --data examples/sample_ticks.csv
afuture scan --config config/afuture.example.toml --data examples/research_ticks.csv
afuture accept --config config/afuture.example.toml --data examples/research_ticks.csv --pair m_calendar
afuture accept-auto --config config/afuture.auto-replay.example.toml --data examples/research_ticks.csv
afuture data-check --config config/afuture.auto-replay.example.toml --data examples/research_ticks.csv
```

CTP、Shadow 和 live 操作必须按当前 runbook 与 production checklist 执行；示例命令不构成真钱授权。

## 重要目录与文件

| 路径 | 用途 |
| --- | --- |
| `afuture/` | 核心包、引擎、策略、风控、状态和 Broker 适配 |
| `afuture/broker/` | 模拟、Shadow 与 CTP Broker 边界 |
| `config/` | 回放、Directional、Stress-90 和 live 配置示例 |
| `constraints/` | 工程与 live 直接依赖约束 |
| `tests/` | 单元、集成、回归、安全和生产机制验证 |
| `tools/` | 离线研究与工程工具；不得进入 live 订单路径 |
| `docs/` | 当前权威文档、研究证据、运维手册和历史归档 |
| `deploy/systemd/` | 通用托管模板；不得内置凭证、activation 或人工确认 |
| `.github/workflows/ci.yml` | push/PR 的标准工程门 |
| `.github/workflows/research-*.yml` | 昂贵研究与历史验证入口；按影响面手动运行 |
| `AGENTS.md` | 渐进式、按影响范围验证的工程约定 |

## GitHub Actions 与验收入口

- 标准 PR 工程门：`.github/workflows/ci.yml`，包含 Python 3.10/3.13 测试、lint、format、mypy、compileall、配置验证和关键 smoke。
- `research-*.yml` 属于昂贵研究工作流，不因纯文档或元数据改动默认运行。
- 验收证据必须记录在关联 Issue 和 PR：命令或 workflow、准确结果、运行环境、未运行检查和原因。
- Issue 正文只保留最新有效状态；重要历史 checkpoint 放在 Issue 评论；不得复制完整聊天记录。
- 合并前必须满足 PR 模板中的验收、测试、CI、阻断 review 和 Issue 刷新门；未运行的检查不得写成通过。

## 明确禁止事项

- 禁止策略、研究工具、CLI、报告或运维绕过 Broker 成交、`PositionBook` 和 `RiskManager`。
- 禁止引入第二套账户状态机，或让 sidecar、缓存和报告成为账户真相。
- 禁止用本机日期、旧交易日或猜测值替代权威 CTP trading day。
- 禁止自动恢复损坏状态、自动采用 `.prev`、覆盖损坏原件或静默迁移不兼容状态。
- 禁止将 Shadow、bootstrap、doctor、capacity report、历史收益或 production wiring 解释为真钱许可。
- 禁止在仓库、模板、配置、日志或治理文档中保存账户凭证。
- 禁止仅为抽象引入数据库、消息队列、Web 服务、微服务或分布式架构。
- 禁止为了节省上下文而降低必要推理、跳过验收、模糊数值/AND 条件或省略风险。
- 未经明确授权，禁止 `reset`、`clean`、`rebase`、force push、丢弃工作、删除工作树/分支或改写历史。

## 权威协作入口

- 项目说明：[`README.md`](../README.md)
- 工程约定：[`AGENTS.md`](../AGENTS.md)
- 文档索引：[`docs/documentation-index.md`](../docs/documentation-index.md)
- 架构与 owner：[`docs/architecture.md`](../docs/architecture.md)
- 策略与时序：[`docs/strategies.md`](../docs/strategies.md)
- 配置：[`docs/configuration.md`](../docs/configuration.md)
- 数据与回测：[`docs/data-and-backtest.md`](../docs/data-and-backtest.md)
- 实盘与恢复：[`docs/live-trading.md`](../docs/live-trading.md)
- Stress-90 运行手册：[`docs/stress90-live-runbook.md`](../docs/stress90-live-runbook.md)
- 生产检查表：[`docs/production-checklist.md`](../docs/production-checklist.md)
