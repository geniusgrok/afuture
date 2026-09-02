# ChatGPT Project Brief

> 本文件只保存长期稳定、仓库级的信息。当前任务、临时分支、SHA、测试状态和执行进度应保存在当前 Pull Request 正文中。

## 1. Project

- 项目名称：afuture
- GitHub 仓库：`ychenracing/afuture`
- 默认分支：`main`
- 系统定位：面向国内商品期货的 Python 交易工程，覆盖数据校验、策略研究、历史回放、Shadow 运行和受控实盘。
- 项目最终目标：在单用户、单账户边界内，使交易时序、账户记账、风险控制、状态恢复和研究证据可核验。

## 2. Purpose and Non-Goals

afuture 支持两条账户互斥的正式运行链：跨期价差策略，以及基于已完成交易日数据的方向组合策略。策略只生成意图或目标，执行层在 Broker 和账户硬限制下形成订单并按成交记账。

长期非目标：

- 多账户、多租户或分布式交易平台；
- 远程管理服务或第二套账户状态机；
- 让离线研究、历史收益、Shadow 或测试柜台自动授权真实资金交易；
- 让研究工具直接进入 live 订单路径。

### Current-only 兼容性边界

afuture 只支持当前代码、API/CLI、TOML 配置、runtime state、account/runtime registry、nonce ledger 和当前研究输入契约。旧版本的代码/API/CLI、配置字段、schema-less 或旧 schema state、旧 registry、migration artifact、historical compatibility adapter 都不是受支持的输入；解析必须 fail closed，不能猜测、默认补齐、过滤未知字段、静默 fallback 或自动转换。

旧 runtime 应完整归档为 incident/provenance evidence，再在新的路径执行明确的 current bootstrap 或 initialization，并重新按 Broker/CTP 真相 reconciliation；不维护 migration path。这项边界不适用于当前交易安全：Broker/CTP 成交真相、exactly-once、CTP order journal、crash-fill recovery、leases/locks、CAS/sequence/checksum/lineage、identity/nonce、kill switch、`HALTED`/`REDUCE_ONLY`、Shadow 隔离、reconciliation、backup/restore、Stress-90 fail-closed 与 current schema `.prev` 都是必须保留的当前机制。

## 3. Architecture and Module Boundaries

稳定依赖方向：统一数据模型和配置 → 纯计算与策略规则 → 策略、风控和订单计划 → 运行时编排 → Broker、持久化、命令行和报告。

- 策略与研究生成信号、候选、意图或目标；不拥有账户真相，不直接修改持仓。
- `RiskManager` 与执行层执行账户硬限制、只减仓和停机约束；风险收缩层只能降低目标。
- Broker / CTP 提供订单、成交、账户、柜台持仓、活动委托、合约和交易日的外部权威事件。
- `PositionBook` 仅根据 Broker 成交维护本地持仓镜像。
- `StateStore` 保存带 schema/version、正序号和校验和的重启证据；不得自动采用损坏状态或 `.prev`。
- account/runtime registry 的多 binding 仍是 current lifecycle safety：它承载 account switch/rebase、Shadow/live 隔离、retired identity 与 crash recovery；不得仅以单经济账户为由压缩为一条 binding。
- Directional activity、OHLC 和 Stress-90 OI sidecar 保存市场输入证据；不拥有账户、持仓或 Broker 真相。
- `TradingEngine` 编排事件顺序、对账、持久化和运行观测。
- 审计、告警和报告只负责观测，不拥有下单或最终风控权限。

权威 Owner：

- 外部订单、成交、账户、柜台持仓、活动委托、合约和交易日：Broker / CTP。
- 本地持仓镜像：`PositionBook`，且只能由成交推进。
- 账户硬限制、`REDUCE_ONLY` 和 `HALTED`：`RiskManager`。
- 运行编排和对账顺序：`TradingEngine`。
- 重启证据：`StateStore` 当前版本化状态；`.prev` 仅供人工检查。
- 配置和策略边界：`config/`、对应代码及第 5 节的权威文档。

不得建立可与上述 Owner 竞争的第二状态源、第二账户状态机或第二最终风控 Owner。完整数据流见 `docs/architecture.md`。

## 4. Non-Negotiable Constraints

- 策略只能产生意图或目标；只有 Broker 成交事件可以改变持仓、现金、盈亏和权益。
- 未成交、撤单和拒单不改变账户；部分成交只按实际数量记账；成交必须按稳定身份去重。
- 反转和风险增加遵守 reduction-first：先完成减仓，再允许新增风险。
- D 日完整数据最早只能影响 D+1；不得使用未完成交易日、未来数据或本机日期猜测交易日。
- 生产 Directional 必须显式选择 `execution_aligned` 或 `stress90`；前者保留 0.25 target scaling，Stress-90 使用 1x raw candidate，不得重复套用 0.25 缩放。
- Stress-90 drawdown reserve 与 HHI freeze 只阻止新增风险；最终硬风险权限仍属于 `RiskManager`。
- 风险状态只有 `RUNNING`、`REDUCE_ONLY`、`HALTED`；策略和执行优化不得绕过风险收缩、强制减仓或停机。
- Shadow 只在本地模拟订单，绝不向柜台报单。
- 代码 wiring、离线验证、Shadow、测试柜台、极小真钱和风险扩大是相互独立的阶段。
- 状态、schema、sequence、checksum、identity、日期或数值不可信时必须失败关闭。
- 历史模拟和离线研究不是未来收益承诺，不能替代目标机 CTP、Shadow、测试柜台和小资金验证。

## 5. Authoritative Sources

- 项目定位与能力边界：`README.md`
- 工程约定：`AGENTS.md`
- 文档权威性索引：`docs/documentation-index.md`
- 架构、模块和 Owner：`docs/architecture.md`
- 策略与时序：`docs/strategies.md`
- 配置：`docs/configuration.md`
- 数据与回测：`docs/data-and-backtest.md`
- 实盘、恢复和生产门：`docs/live-trading.md`、`docs/production-checklist.md`
- Stress-90 运维边界：`docs/stress90-live-runbook.md`
- 包、依赖和工具配置：`pyproject.toml`、`constraints/core-dev.txt`、`constraints/live.txt`
- 生产账户、订单、成交和交易日真相：Broker / CTP 返回的权威事件
- 版本和发布：Git 标签、GitHub Releases 及仓库发布 workflow（如适用）

## 6. Standard Commands

最低 Python 版本为 3.10。以下命令由 `pyproject.toml`、`README.md` 和 `.github/workflows/ci.yml` 支持。

Python `>=3.10`、`tomli` fallback、Python 3.10/3.13 matrix 和 Windows core/replay/config-validation
smoke 均是当前仓库契约，不能仅为清理而移除。真实生产 Python 版本超出仓库事实，仍是部署前
UNKNOWN；Windows smoke 不承诺 Stress-90 live 或 CTP native ABI 可用，后者只在目标 POSIX 机器上
验证并 fail closed。

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

常用非实盘入口：

```bash
afuture validate --config config/afuture.example.toml
afuture replay --config config/afuture.example.toml --data examples/sample_ticks.csv
afuture scan --config config/afuture.example.toml --data examples/research_ticks.csv
afuture data-check --config config/afuture.auto-replay.example.toml --data examples/research_ticks.csv
```

CTP、Shadow 和 live 操作必须按相应 runbook 与 production checklist 执行；示例命令不构成真钱授权。

## 7. Important Paths

- `afuture/`：核心包、引擎、策略、风控、状态和 Broker 适配。
- `afuture/broker/`：模拟、Shadow 与 CTP Broker 边界。
- `config/`：回放、Directional、Stress-90 和 live 配置示例。
- `constraints/`：工程与 live 依赖约束。
- `tests/`：单元、集成、回归、安全和生产机制验证。
- `tools/`：离线研究与工程工具；不得进入 live 订单路径。
- `docs/`：权威文档、研究证据、运维手册和历史归档。
- `deploy/systemd/`：通用托管模板。
- `.github/workflows/ci.yml`：push/PR 的标准工程门。
- `.github/workflows/research-*.yml`：昂贵研究与历史验证入口。
- `AGENTS.md`：渐进式、按影响范围验证的工程约定。

## 8. CI and Acceptance Entry Points

- `.github/workflows/ci.yml` 承担 Python 3.10/3.13 测试、lint、format、mypy、compileall、配置验证和关键 smoke。
- `.github/workflows/research-*.yml` 承担昂贵研究和历史验证，不因纯文档或元数据改动默认运行。
- 本地验证按 `AGENTS.md` 使用渐进式、影响范围驱动的策略；最终候选满足受影响范围的完整门。
- Definition of Done：验收条件逐项满足；实际运行的命令和 workflow 有准确结果；未运行检查明确标记；没有未解决的正确性、安全、数据完整性或阻断审查问题。

## 9. Prohibited Actions

- 不得绕过 Broker 成交、`PositionBook` 和 `RiskManager`。
- 不得引入第二套账户状态机，或让 sidecar、缓存和报告成为账户真相。
- 不得用本机日期、旧交易日或猜测值替代权威 CTP trading day。
- 不得自动恢复损坏状态、自动采用 `.prev`、覆盖损坏原件或静默迁移不兼容状态。
- 不得将 Shadow、bootstrap、doctor、capacity report、历史收益或 production wiring 解释为真钱许可。
- 不得在仓库、模板、配置、日志或治理文档中保存账户凭证。
- 不得擅自改写 Git 历史或 force push。
- 不得丢弃未知或未提交工作，也不得覆盖无关改动。
- 不得把计划执行写成已验证完成。
- 不得根据旧聊天猜测当前分支、SHA、PR 或 CI 状态。

## 10. Context Loading Protocol

1. 新开发任务可以直接使用自然语言提出，不要求预先填写固定 Prompt。
2. 开始任务时先读取本文件。
3. 搜索与任务相关的开放 PR、分支和 Issue。
4. 如果存在匹配工作，从现有现场原地继续。
5. 当前动态任务状态默认维护在 Pull Request 正文。
6. 不强制普通单 PR 任务创建 Issue。
7. 优先读取目标代码、直接调用者、相关测试和直接相关配置。
8. 只有证据不足、状态冲突或影响范围扩大时才扩大读取。
9. 不默认加载完整仓库、完整聊天、完整日志或全部 GitHub Actions 历史。
10. 长对话交接使用 `conversation-continuity-guard`，但 GitHub 当前现场仍是状态权威来源。

## 11. References

- `README.md`
- `AGENTS.md`
- `docs/documentation-index.md`
- `docs/architecture.md`
- `docs/strategies.md`
- `docs/configuration.md`
- `docs/data-and-backtest.md`
- `docs/live-trading.md`
- `docs/stress90-live-runbook.md`
- `docs/production-checklist.md`
- `pyproject.toml`
- `.github/workflows/ci.yml`
