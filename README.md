# afuture

`afuture` 是面向国内商品期货的 Python 交易工程，覆盖数据校验、策略研究、历史回放、影子运行和受控实盘。项目按单用户、单账户设计，重点保证交易时序、账户记账、风险控制、状态恢复和研究证据可核验，不追求多账户平台或分布式架构。

系统支持两类互斥策略：

- **跨期价差策略（Calendar / Auto）**：交易同一品种不同到期月份之间的相对价差；
- **方向组合策略（Directional）**：用已完成交易日的数据生成品种方向和目标仓位，下一交易日再选择具体合约和手数。

策略只产生交易意图或目标仓位。只有 Broker（统一订单和成交接口）返回的真实或模拟成交事件可以改变持仓、现金和权益。

## 当前能力

| 能力 | 状态 | 边界 |
| --- | --- | --- |
| 固定跨期组合回放 | 已实现并测试 | 使用确定性模拟 Broker；不等同于真实成交 |
| 自动选择跨期组合 | 已实现并测试 | 只使用决策时已经可见的合约目录、到期信息和流动性数据 |
| 方向组合离线研究 | 已实现并有固定证据 | 历史结果不是未来收益承诺 |
| Stress-90 可选 runtime wiring | 已实现并测试 | 必须显式选择、bootstrap 和绑定 identity；不等于真实资金许可 |
| Shadow 影子运行 | 已实现 | 使用实时 CTP 行情和合约信息，本地模拟订单，绝不向柜台报单 |
| CTP 期货柜台实盘基础链 | 已实现 | 真实资金前仍需完成多日 Shadow、测试柜台和生产检查表 |
| 状态、审计与恢复 | 已实现 | 损坏状态不会自动回退；恢复必须人工核验并重新对账 |

固定 Stress-90 候选现在可以通过 `directional.policy = "stress90"` 显式接入 Shadow、测试柜台和 CTP runtime；普通 `execution_aligned` 仍保留原有行为。代码 wiring、历史候选验证、Shadow、测试柜台、极小真钱和扩大风险是六个不同阶段，前一阶段不能自动授权后一阶段。

Stress-90 账户连续性默认使用 `account_continuity_mode = "strict"`，保持现有官方结算/交易时段证据的 fail-closed 门。个人专用、账户独占且运行期间无人工交易和出入金时，可显式选择 `operator_managed`，通过 `stress90-operator-roll-forward` 记录带 checksum 的操作者信任连续性凭证；它不是交易所或 Broker 的官方结算/session 见证，成功后仍保持 `HALTED`、kill switch 开启，并要求重新运行 `status`、`doctor` 和签发新的技术 permit。任何外部账户活动都必须先走 `stress90-account-rebase`。

## 策略

### 跨期价差

固定模式直接配置近月、远月合约。Auto 模式从当时已经挂牌的合约中选择同品种、相邻月份且满足到期距离、行情同步和流动性要求的组合。

策略跟踪两腿价差相对历史均值的偏离程度，在偏离足够大且预期收益能够覆盖成本时开仓，价差回归、触发止损或持有超时时退出。双腿订单、部分成交、撤单和回滚由执行层统一处理，策略不能直接修改持仓。

### 方向组合

方向组合根据已完成的价格数据生成品种目标。生产链使用预先固定的信号组合，不在运行期间搜索参数；交易日 D 的数据最早只能影响下一交易日 D+1。

系统在 D+1 根据上一完整交易日的成交量、持仓量、挂牌和到期信息选择具体合约，再把目标权重转换成整数手数。转换过程同时受保证金、可用资金、总敞口和单合约手数限制。减仓必须先成交，随后才允许增加风险。

生产配置必须在 `execution_aligned` 和 `stress90` 中显式二选一。前者使用 0.25 风险缩放；后者使用固定 Base→60m Price×OI→20/3/15bp cost gate→survivor reallocation 候选，并在整数 lots 层分别应用 25% completed-path drawdown reserve freeze 和 HHI concentration freeze。两个 freeze 只阻止新风险；最终硬风险权限仍属于 `RiskManager`。

## 架构

核心数据流是：

```text
行情、合约目录和账户快照
→ 数据完整性与时间校验
→ 策略意图或目标仓位
→ 账户与组合风控
→ 订单计划和成交价格
→ Broker（模拟、Shadow 或 CTP）
→ 成交驱动的持仓与资金记账
→ 状态、审计、告警和报告
```

主要边界：

| 边界 | 职责 |
| --- | --- |
| 领域与配置 | 统一订单、成交、持仓、账户、合约和风险数据结构 |
| 策略与研究 | 生成信号、候选或目标，不拥有账户真相 |
| 风控与执行 | 决定是否允许交易、允许多少手，以及如何生成订单 |
| Broker 适配 | 对接模拟环境、Shadow 和 CTP，统一外部事件语义 |
| 记账与状态 | 只根据成交更新持仓、现金、盈亏、保证金和重启证据 |
| 运维与报告 | 提供预检、审计、告警和执行质量，不拥有交易权限 |

依赖方向、事件顺序和完整模块地图见 [`docs/architecture.md`](docs/architecture.md)。

## 安全模型

运行状态只有三种：

- `RUNNING`：安全检查通过，可以正常开仓和减仓；
- `REDUCE_ONLY`：只能减仓或退出，不能增加风险；
- `HALTED`：硬停机，需要人工核验、恢复和重新对账。

核心约束：

- 未成交、撤单和拒单不改变持仓；部分成交只按实际数量记账；
- 本地持仓必须与柜台的合约、交易所、今昨仓和多空方向一致；
- 日亏损、总回撤、保证金、可用资金、总敞口和单合约手数都有硬限制；
- 状态文件的格式、版本、序号和校验和不可信时停止加载和覆盖；
- 数据缺失、过期、时间错位或含有非法数值时拒绝增加风险；
- 风险收缩、强制减仓和停机不能被策略或执行优化绕过。

配置示例中的方向组合上限为：总名义敞口不超过账户权益的 2 倍、保证金占比不超过 35%、可用资金不低于权益的 25%、单日亏损达到 5% 时停止新增风险、总回撤达到 30% 时硬停机、单合约不超过 35 手。实际运行以所用配置和 `RiskManager` 校验结果为准。

术语和公式见 [`docs/glossary.md`](docs/glossary.md)，更完整的会计与风控不变量见 [`docs/architecture.md`](docs/architecture.md)。

## 安装

需要 Python 3.10 或更高版本。

```bash
python -m venv .venv

# Linux / macOS
. .venv/bin/activate

# Windows PowerShell
.\.venv\Scripts\Activate.ps1

python -m pip install -e ".[dev]" -c constraints/core-dev.txt
```

安装 CTP 与 AKShare 支持：

```bash
python -m pip install -e ".[live,dev]" \
  -c constraints/core-dev.txt -c constraints/live.txt
```

两个 `constraints` 文件固定直接依赖版本，但不能证明 CTP 原生组件一定适配目标交易机。实盘前必须在目标系统验证 Python、CTP 柜台和原生安装包的兼容性。

## 配置

配置使用 TOML。选择最接近用途的示例并复制后修改：

- `config/afuture.example.toml`：固定跨期组合回放；
- `config/afuture.auto-replay.example.toml`：自动选择组合的回放；
- `config/afuture.live.example.toml`：跨期策略 CTP 配置；
- `config/afuture.directional-live.example.toml`：普通 `execution_aligned` 方向组合 CTP 配置；
- `config/afuture.directional-stress90-live.example.toml`：Stress-90 production-wiring/commissioning 示例，不授予 activation。

全部字段的类型、默认值、单位和约束见 [`docs/configuration.md`](docs/configuration.md)。

主要配置段：

| 配置段 | 内容 |
| --- | --- |
| `[system]` | 运行模式和初始资金 |
| `[ctp]` | CTP 交易、行情地址和环境 |
| `[directional]` / `[auto]` | 互斥的策略配置 |
| `[risk]` | 资金、回撤、敞口、手数和订单限制 |
| `[execution]` | 订单类型、滑点、延迟、冲击和合约参数 |
| `[paths]` | 状态、日志、报告、审计和告警路径 |
| `[[contracts]]` / `[[pairs]]` | 固定回放使用的合约、费率和组合 |

实盘凭证只从环境变量读取：`AFUTURE_CTP_USER`、`AFUTURE_CTP_PASSWORD`、`AFUTURE_CTP_BROKER`。Stress-90 柜台连接还必须设置预期 `AFUTURE_CTP_ACCOUNT_ID`、`AFUTURE_CTP_CURRENCY_ID`；柜台提供时同时设置 `AFUTURE_CTP_INVESTOR_ID`、`AFUTURE_CTP_INVEST_UNIT_ID`。真实报单还必须设置：

```text
AFUTURE_LIVE_ACK=I_UNDERSTAND_FUTURES_RISK
```

## 常用命令

```bash
# 校验配置和查看本地状态
afuture validate --config config/afuture.example.toml
afuture status --config config/afuture.directional-live.example.toml

# 历史回放、候选扫描和研究验收
afuture replay --config config/afuture.example.toml --data examples/sample_ticks.csv
afuture scan --config config/afuture.example.toml --data examples/research_ticks.csv
afuture accept --config config/afuture.example.toml --data examples/research_ticks.csv --pair m_calendar
afuture accept-auto --config config/afuture.auto-replay.example.toml --data examples/research_ticks.csv
afuture data-check --config config/afuture.auto-replay.example.toml --data examples/research_ticks.csv

# CTP 无报单预检、影子运行和实盘
afuture doctor --config config/afuture.directional-live.example.toml --confirm-live
afuture shadow --config config/afuture.directional-live.example.toml --duration-seconds 3600
afuture live --config config/afuture.directional-live.example.toml --confirm-live

# Stress-90 固定 bootstrap、无订单 OHLC 准备和显式 lifecycle（先阅读 runbook）
afuture stress90-bootstrap --config config/afuture.directional-stress90-live.example.toml --runtime-dir runtime --through YYYYMMDD
afuture directional-ohlc-refresh --config config/afuture.directional-stress90-live.example.toml --current-trading-day YYYYMMDD
afuture stress90-oi-collect --help
afuture stress90-prepare-decision --help
afuture stress90-capacity-report --help
afuture stress90-registry-init --help
afuture stress90-settlement-roll-forward --help
afuture stress90-operator-roll-forward --help
afuture stress90-activate --help
afuture stress90-account-rebase --help
afuture stress90-order-journal-rollover --help
afuture stress90-crash-fill-recover --help
afuture directional-policy-migrate --help
afuture stress90-oi-compare --help

# 人工恢复和执行质量
afuture recover-state --help
afuture quality-report --config config/afuture.directional-live.example.toml --output runtime/execution_quality_report.json
```

`stress90-settlement-roll-forward` 当前仍是显式失败关闭入口：目标柜台尚未提供可证明上一完整交易日全部资金流的权威最终见证，因此确认参数和 operator reason 都不能使它推进状态。详见运行手册的结算资金闭合门。

`directional-policy-migrate` 是 Stress-90 policy lifecycle transaction：只在已验证、
`HALTED`、kill-switched、flat 且 reconciled 的 runtime 上切换到 `execution_aligned`
policy，并保留 retirement provenance。该命令不修改 state、registry、nonce ledger 或 TOML schema。

`status` 只读本地状态和运行环境，不需要 CTP 凭证。`doctor` 连接 CTP 获取新的账户、持仓、活动委托、合约目录、报价和合约参数，但不会发送订单。Stress-90 的精确 bootstrap→status→doctor→Shadow→测试柜台→极小真钱顺序见 [`docs/stress90-live-runbook.md`](docs/stress90-live-runbook.md)。

## 测试

```bash
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python -m mypy afuture
python -m compileall -q afuture
```

完整历史压力矩阵成本较高，只在策略、数据时间规则、成交和成本假设、仓位构造或共享会计基础设施发生实质变化时运行。普通文档和行为不变的工程修改不重复运行昂贵回测。

## 离线研究证据

当前方向组合研究候选的历史代号是 `Stress-90`。其中“90”表示该轮研究预先设定的压力情景年化收益目标不低于 90%，不是 90 个基点成本、90% 保证金或实盘风险等级。它继承的前一候选代号为 `Stress-80`。

| 情景 | 单边交易成本假设 | 保证金比例假设 |
| --- | ---: | ---: |
| 标准情景 | 5 个基点，即 0.05% | 12% |
| 压力情景 | 15 个基点，即 0.15% | 15% |

固定历史窗口的主要结果：

| 情景 | 年化收益 | 最大回撤幅度 | 最高总敞口 | 保证金拒绝 | 硬停机 |
| --- | ---: | ---: | ---: | ---: | --- |
| 标准情景汇总窗口 | 156.881655% | 15.708467% | 1.983123 倍权益 | 0 | 否 |
| 压力情景汇总窗口 | 112.100053% | 14.567214% | 1.670510 倍权益 | 0 | 否 |

“汇总窗口”覆盖训练、验证和样本外区段，因此不是与它们完全独立的最终留出集。这些结果来自固定输入的离线账户模拟，不代表未来收益，也不证明真实成交质量。完整输入摘要、分段结果、成本、换手和防过拟合约束见 [`docs/stress90-final-evidence.md`](docs/stress90-final-evidence.md)。

112.100053% 不是未来收益承诺。96-template pool 在已观察历史上存在 selection bias；候选固定后新发生的数据才是真正 forward evidence。live 改用 CTP raw 60m 数据后，必须通过多日 Shadow 解释它与历史 vendor 数据在 first/last、volume、dominant contract 和 flow 上的差异。

## 局限性

- 目前是单用户、单账户工程，不支持多账户并发、分布式高可用或远程管理平台；
- 历史回放和离线账户模拟无法完整复现排队、断线、柜台限流和极端行情成交；
- CTP 原生组件、真实手续费、保证金和合约规则需要在目标账户和交易机再次核验；
- Stress-90 runtime wiring 已实现，但尚不能用代码或离线高收益替代目标机 CTP、Shadow、测试柜台和小资金上线验证；
- 状态备份只保留一个上一版本证据，不会自动恢复或绕过柜台对账；
- 项目没有承诺稳定的第三方插件接口，内部模块可能随正确性要求调整。

## 扩展原则与下一步

新增策略应只输出信号或目标仓位，并复用统一风控、执行、Broker 和记账链。新增 Broker 或数据源必须先转换成统一领域模型，并通过数值、时间和身份校验。新增风控规则不能绕过现有硬限制。

下一阶段优先收集真实执行证据，而不是继续扩大同一历史数据上的参数搜索：

1. 连续多个交易日运行 CTP Shadow；
2. 在测试柜台验证报单、部分成交、撤单、重连和交易日切换；
3. 用真实账户费率、保证金和执行质量校准模拟假设；
4. 使用新发生数据做前向验证；
5. 全部通过后，再从极小资金开始逐步扩大风险。

当前没有必要引入数据库、消息队列、微服务或第二套账户状态机。

## 文档入口

- [`docs/glossary.md`](docs/glossary.md)：术语、缩写和公式；
- [`docs/architecture.md`](docs/architecture.md)：模块边界、依赖方向和事件顺序；
- [`docs/strategies.md`](docs/strategies.md)：当前策略规则、决策时点和实盘边界；
- [`docs/configuration.md`](docs/configuration.md)：全部配置字段、默认值、单位和约束；
- [`docs/data-formats.md`](docs/data-formats.md)：Tick CSV 字段、类型和数据质量要求；
- [`docs/data-and-backtest.md`](docs/data-and-backtest.md)：数据时点、回放假设和研究窗口；
- [`docs/live-trading.md`](docs/live-trading.md)：CTP、Shadow、停机和恢复；
- [`docs/troubleshooting.md`](docs/troubleshooting.md)：常见故障、诊断顺序和安全恢复；
- [`docs/production-checklist.md`](docs/production-checklist.md)：真实资金上线检查表；
- [`docs/stress90-live-productionization.md`](docs/stress90-live-productionization.md)：Stress-90 当前代码 wiring、状态/data/执行边界；
- [`docs/stress90-live-runbook.md`](docs/stress90-live-runbook.md)：Stress-90 从 bootstrap 到极小真钱的操作顺序；
- [`docs/stress90-final-evidence.md`](docs/stress90-final-evidence.md)：当前离线压力研究证据；
- [`docs/documentation-index.md`](docs/documentation-index.md)：全部 Markdown 的权威分类和历史记录入口。

### Stress-90 commissioning risk overlay

Stress-90 live/Shadow production can set `directional.live_risk_scale` in `(0, 1]`. The default `1.0` preserves the historical production target; the scale is applied only after the immutable Base/OI/cost/survivor/HHI/drawdown decision and before integer lots and margin fitting. It never scales exits, reductions, hard-risk flattening, crash recovery or cancellation. Production state binds a separate canonical risk-overlay digest; changing any bound risk field fails closed until the existing `stress90-activate` lifecycle is run while HALTED, kill-switched, flat, reconciled and free of active orders.

`afuture stress90-capacity-report --config ... --confirm-live --output ...` is a zero-order, zero-cancel diagnostic that reuses Doctor contract selection, live metadata/cost evidence, the shared Stress-90 lot planner and RiskManager preview. It is capacity evidence, not capital activation approval.

## 生产盘前与进程托管

Stress-90 的当前生产操作顺序只有一条：`deployment-verify` → `prepare-session` → 必要时人工 `stress90-operator-roll-forward` 或 `stress90-account-rebase` → `doctor` → `stress90-capacity-report` 人工复核 → 人工签发新的 activation permit → `live` → `watchdog` → 仅在 `HALTED` 且 kill switch 开启时执行 backup/recovery。

`prepare-session` 是一次性、只读柜台准备命令。它验证 deployment seal、本地状态和 artifact，取得权威 CTP trading day、fresh account、完整持仓、活动委托、catalog、metadata/quote/margin/commission，并复用 Doctor P0、continuity/rebase 与 capacity 逻辑输出 canonical JSON；其 Broker 能力层禁止报单和撤单，永不签发 permit、永不自动 roll-forward/rebase、永不把 runtime 切到 `RUNNING`。可选 `--refresh-ohlc` 仍在 engine 外复用现有 OHLC refresh，不把外部 provider 接入订单路径。

Shadow/Live 的正常主循环按 `execution.heartbeat_interval_seconds`（默认 5 秒，允许 1–60 秒）原子更新 `paths.heartbeat`。外部 `afuture watchdog --once` 只读 heartbeat、deployment 和本地 checksummed state，不连接 CTP、不持有 order-capable lease，也不会 kill/restart、平仓、解除 HALTED 或修改 permit/state。

Python `>=3.10` 是当前仓库、工具链、constraints、CI 和文档共同支持的 baseline，因而保留
Python 3.10/3.13 matrix 与 `tomli` fallback；实际 production Python 版本仍须在目标机验证。Windows
仍是 core/replay CLI 与配置校验的当前支持环境，CI 保留最小 Windows smoke；Stress-90 live 继续只在
POSIX target 上 fail closed，Windows smoke 不代表 CTP ABI 或真实报单可用。registry 的多 binding
表示仍承担 account switch/rebase、Shadow/live isolation、retired identity 和 crash recovery；本轮不将
其错误地压缩为单条 binding。

Live 使用 `runtime/process_run.json` 的 current/.prev receipt 建立异常重启围栏。任何没有 clean shutdown receipt、current 损坏或 current 缺失但 `.prev` 存在的窗口都在构造 order-capable Broker 前 fail closed：失效已签发 permit、保持/转换 `HALTED`、开启 kill switch，并以专用退出码 75 阻止 systemd 重启循环。`deploy/systemd/` 提供只含通用机器路径的 live/watchdog 模板；operator 必须在机器私有 EnvironmentFile 中提供本机运行参数，模板不内置 live confirmation、activation、roll-forward 或 rebase。

### 隔离测试柜台证据采集

`ctp-settlement-capture` 可在明确批准的零真钱测试环境采集 request-bound 结算文本分片；
不下单、不推进资金基线、不签发许可。输出明确区分查询完成与财务连续性证明，参见
[`docs/live-trading.md`](docs/live-trading.md#32-隔离测试柜台的结算格式采集)。
无人值守跨日、目标机验证和真实一个月模拟的未通过项以
[`docs/production-checklist.md`](docs/production-checklist.md#p-无人值守交付与证据等级) 为准。
