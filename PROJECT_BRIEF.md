# afuture 项目简报

> 本文件只保存长期稳定的项目事实和边界。当前任务、分支、SHA、工作区状态和测试结果统一放在 `TASK_STATE.md`；当前任务验收标准统一放在 `ACCEPTANCE.md`。

## 项目名称与最终目标

- 项目：`ychenracing/afuture`。
- 最终目标：构建面向国内商品期货的可核验 Python 交易工程，覆盖数据校验、策略研究、历史回放、Shadow 运行和受控实盘，并保证交易时序、账户记账、风险控制、状态恢复及研究证据可验证。
- 系统定位：单用户、单账户工程；支持互斥的跨期价差策略与方向组合策略；不定位为多账户平台、分布式交易平台或远程管理服务。

## 关键架构与模块边界

稳定依赖方向：统一数据模型和配置 → 无外部副作用的计算与策略规则 → 策略、风控和订单计划 → 运行时编排 → Broker、持久化、命令行和报告。

| 模块或边界 | 权限与职责 |
| --- | --- |
| `afuture/` 领域模型与配置 | 定义订单、成交、持仓、账户、合约、风险和配置语义。 |
| 策略与研究 | 生成信号、候选、交易意图或目标仓位；不拥有账户真相，不直接修改持仓。 |
| `RiskManager` 与执行层 | 校验账户硬限制、只减仓和停机状态，生成受约束订单计划；所有风险收缩层只能降低目标。 |
| Broker / CTP 适配层 | 提供订单、成交、账户和柜台持仓的权威外部事件；进入系统后转换为统一领域模型。 |
| `PositionBook` | 仅根据 Broker 成交维护本地持仓镜像。 |
| `StateStore` | 保存带版本、正序号和校验和的重启证据；损坏状态不得自动回退或覆盖。 |
| Directional activity、OHLC、Stress-90 OI sidecar | 保存带 schema、digest/checksum 的市场输入证据；不拥有账户或 Broker 真相。 |
| `TradingEngine` | 编排事件顺序、对账、持久化和运行观测。 |
| 运维、审计、告警和报告 | 负责预检与观测，不拥有交易权限或最终风控权限。 |

详细模块地图、事件顺序和依赖边界的权威来源是 `docs/architecture.md`。

## 不可改变的业务约束

- 两条正式运行链账户互斥：跨期价差或方向组合，不能同时拥有同一账户的策略权限。
- 策略只能产生意图或目标；只有 Broker 返回的真实或模拟成交事件可以改变持仓、现金、盈亏和权益。
- 未成交、撤单和拒单不改变账户；部分成交只按实际数量记账；成交身份必须可去重。
- 反转和风险增加必须遵守 reduction-first：先完成减仓，再允许新增风险。
- 方向组合使用已完成交易日数据：D 日数据最早只能影响 D+1；不得使用未完成交易日或未来信息。
- 生产 Directional 必须显式选择 `execution_aligned` 或 `stress90`。前者保留 0.25 target scaling；Stress-90 使用固定候选链和 1x raw candidate，不得再次套用 0.25 缩放。
- Stress-90 的 drawdown reserve 与 HHI freeze 只阻止新增风险；最终硬风险权限仍属于 `RiskManager`。
- 风险状态只有 `RUNNING`、`REDUCE_ONLY`、`HALTED`；策略和执行优化不得绕过风险收缩、强制减仓或停机。
- 方向组合示例硬限制：目标和实际总敞口不超过权益的 2 倍、保证金不超过 35%、可用资金不低于权益的 25%、单日亏损达到 5% 时停止新增风险、总回撤达到 30% 时硬停机、单合约不超过 35 手。实际运行仍以所用配置与 `RiskManager` 校验为准。
- Shadow 使用实时 CTP 行情和合约信息但只在本地模拟订单，绝不向柜台报单。
- Stress-90 的代码 wiring、历史候选验证、Shadow、测试柜台、极小真钱和扩大风险是六个不同阶段；前一阶段不能自动授权后一阶段。
- 真实报单必须满足配置、对账、目标机验证和显式风险确认；凭证只从环境变量读取，不得写入仓库。
- 状态、schema、sequence、checksum、policy digest、身份、日期或数值不可信时必须失败关闭；`.prev` 只供人工检查，运行时不得自动回退。
- 历史模拟和离线研究结果不是未来收益承诺，也不能替代新发生数据、目标机 CTP、Shadow、测试柜台和小资金验证。

## 权威数据源与状态 Owner

| 事实 | 权威来源或 Owner |
| --- | --- |
| 外部订单、成交、账户、柜台持仓、活动委托、合约与权威交易日 | Broker / CTP。 |
| 本地持仓镜像 | `PositionBook`，且只能由 Broker 成交推进。 |
| 账户硬限制、只减仓和停机 | `RiskManager`。 |
| 策略运行编排和对账顺序 | `TradingEngine`。 |
| 重启证据 | `StateStore` 的版本化、带序号和校验和状态；不自动采用 `.prev`。 |
| 当前策略、配置、架构和运行边界 | `README.md`、`docs/architecture.md`、`docs/strategies.md`、`docs/configuration.md`、`docs/live-trading.md`、`docs/stress90-live-productionization.md`、`docs/stress90-live-runbook.md`、`docs/production-checklist.md`。 |
| 文档权威性分类 | `docs/documentation-index.md`。历史记录和开发计划不得覆盖当前权威文档。 |
| 当前任务现场 | 最新只读 Git 核验与 `TASK_STATE.md`；现场核验结果高于旧聊天、旧摘要和旧交接。 |

## 标准安装、构建、测试、Lint 与运行命令

最低 Python 版本为 3.10。直接依赖基线由 `constraints/core-dev.txt` 固定；可选 live 依赖由 `constraints/live.txt` 固定，且 CTP 原生组件必须在目标交易机另行验证。

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
afuture accept --config config/afuture.example.toml --data examples/research_ticks.csv --pair m_calendar
afuture accept-auto --config config/afuture.auto-replay.example.toml --data examples/research_ticks.csv
afuture data-check --config config/afuture.auto-replay.example.toml --data examples/research_ticks.csv
```

CTP、Shadow 和 live 操作必须先阅读对应 runbook 与 production checklist；不得把示例命令视为真钱授权。

## 重要目录与文件索引

| 路径 | 用途 |
| --- | --- |
| `afuture/` | 核心包、引擎、策略、风控、状态和 Broker 适配。 |
| `afuture/broker/` | 模拟、Shadow、CTP 等 Broker 边界。 |
| `afuture/health/` | 健康检查与运行观测。 |
| `config/` | 回放、Directional、Stress-90 和 live 配置示例。 |
| `constraints/` | 工程与 live 直接依赖版本约束。 |
| `tests/` | 单元、集成、回归、安全和生产机制验证。 |
| `tools/` | 离线研究与工程工具；不得进入 live 订单路径。 |
| `docs/` | 当前权威文档、研究证据、运维手册和历史归档。 |
| `deploy/systemd/` | 通用进程托管模板；不得内置账户凭证或 activation。 |
| `.github/workflows/ci.yml` | PR 与 push 的标准工程门；昂贵研究矩阵由手动 workflow 管理。 |
| `README.md` | 项目定位、能力、命令与边界入口。 |
| `AGENTS.md` | 渐进式、按影响范围验证的开发约定。 |
| `PROJECT_BRIEF.md` | 本文件：长期稳定事实。 |
| `TASK_STATE.md` | 最新有效任务现场；更新时替换过期值。 |
| `ACCEPTANCE.md` | 当前任务的稳定验收标准与语义覆盖。 |

## 明确禁止事项

- 禁止策略、研究工具、CLI、运维或报告直接修改持仓、账户真相或绕过 Broker 成交与 `RiskManager`。
- 禁止引入第二套账户状态机，或让 sidecar、缓存、报告成为账户真相。
- 禁止用本机自然日期、旧交易日或猜测值替代权威 CTP trading day。
- 禁止把未完成数据、未来数据、连续合约换月跳空或被污染的样本外结果当成可交易收益证据。
- 禁止自动恢复损坏状态、自动采用 `.prev`、覆盖损坏原件或静默迁移不兼容状态。
- 禁止将 Shadow、bootstrap、doctor、capacity report、历史高收益或 production wiring 解释为真实资金许可。
- 禁止在仓库、配置示例、systemd 模板、日志或治理文件中保存账户凭证。
- 禁止仅为抽象而引入数据库、消息队列、Web 服务、微服务、Repository/Factory/Service 层或分布式架构。
- 禁止为了节省时间或上下文而降低必要推理、跳过验收、模糊数字/AND 条件或省略风险。
- 未经明确授权，禁止 `reset`、`clean`、`rebase`、force push、丢弃工作、删除工作树/分支或改写历史。
