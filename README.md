# afuture

`afuture` 是面向国内商品期货研究、回放、Shadow 和受控实盘演进的 Python 工程。系统保留两条账户互斥的交易链：

- Calendar Spread / Auto：同品种相邻月份跨期套利；
- Execution-Aligned Directional：冻结 50 品种、96-template 的方向组合实盘链。

两条链共用一套 Broker、订单、成交、持仓、资金、风险状态和持久化真相。策略只产生意图；Broker 成交回报才允许改变持仓。项目已经具备完整回放、状态恢复、CTP 适配、风控、审计和自动测试，但历史 proxy 不能替代真实 L1、测试柜台与未来未见数据，不能据此直接放大真实资金。

## 当前基线与证据边界

当前工程基线继承 [PR #26](https://github.com/ychenracing/afuture/pull/26) 的工业重构 merge `34fd0210b914b847ed10ecadead53b62015a32af`。研究 checkpoint 与工程基线分开记录：当前 production-research checkpoint 仍是 [PR #25](https://github.com/ychenracing/afuture/pull/25) 的 Stress-90 merge `482455dc57bc6a134f45232e290b4a49c3f7073d`，并继续包含 [PR #24](https://github.com/ychenracing/afuture/pull/24) Stress-80 checkpoint `b4207abb50aca1e39d5ebba3affc04765857251a`。

Stress-90 是固定输入、离线 production-mechanics evaluator 的验证结果，**没有接入 live runtime**。PR #25 的 Python 3.10/3.13 主 CI 已在 run `32798895640` 通过。

| 固定窗口指标 | Stress-80 | Stress-90 |
| --- | ---: | ---: |
| Base full_recent 年化 | 128.119261% | 156.881655% |
| Stress full_recent 年化 | 80.067891% | 112.100053% |
| Stress 最大回撤 | 29.727688% | 14.567214% |
| Stress gross peak | 1.683773x | 1.670510x |
| Stress margin rejects | 0 | 0 |
| Stress HALT | false | false |
| Stress net alpha / turnover | 30.990722 bps | 78.274862 bps |

这些都是已观察历史结果，不是未来收益承诺。权威结果、输入摘要和防过拟合边界见 [`docs/stress90-final-evidence.md`](docs/stress90-final-evidence.md)；旧研究结果只应通过 [`docs/documentation-index.md`](docs/documentation-index.md) 作为历史或证据记录阅读。

## 主链路

### Calendar / Auto

```text
CTP catalog / Tick
→ AutoPairManager 候选与开仓资格
→ CalendarSpreadStrategy 信号
→ PortfolioRisk + RiskManager
→ PairExecutor（双腿、FAK、回滚）
→ Broker / fill events
→ PositionBook + cash/equity/margin
→ StateStore / audit / report
```

### Directional live runtime

```text
已完成 continuous OHLC + completed activity snapshot
→ ExecutionAlignedAggressivePolicy 产品权重
→ D+1 concrete contract selection
→ integer target lots + margin-aware downward fitting
→ reductions first；Broker 确认后才允许 openings
→ RiskManager hard gates → FAK → Broker
→ fill-driven position/accounting
→ realized-gross guard / circuit / HALT
→ state / audit / execution quality
```

Stress-80/90 研究模块建立在同一 deterministic account evaluator 上，但不会绕过 live `RiskManager`，也不会自动成为实盘策略。完整边界见 [`docs/architecture.md`](docs/architecture.md)。

## 安装

需要 Python 3.10 或更高版本。

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]" -c constraints/core-dev.txt
```

连接 CTP 与 AKShare：

```bash
python -m pip install -e ".[live,dev]" \
  -c constraints/core-dev.txt -c constraints/live.txt
```

`pyproject.toml` 声明支持范围；`constraints/core-dev.txt` 固定 Python 3.10–3.13 的 core/dev 直接依赖，`constraints/live.txt` 固定 CTP/AKShare 直接依赖。CTP 含平台原生 wheel，目标交易机仍必须单独验证安装和柜台兼容性，不能把跨平台 constraints 当成二进制可用证明。

## 配置

配置使用 TOML。先复制最接近用途的示例，再修改副本：

- `config/afuture.example.toml`：固定 pair 回放；
- `config/afuture.auto-replay.example.toml`：Auto 回放；
- `config/afuture.live.example.toml`：Calendar / Auto CTP；
- `config/afuture.directional-live.example.toml`：Directional CTP。

关键 section：

| Section | 作用 |
| --- | --- |
| `[system]` | mode、initial capital |
| `[ctp]` | 非敏感 CTP 前置地址与环境 |
| `[directional]` / `[auto]` | 互斥策略配置 |
| `[risk]` | margin、available、daily loss、drawdown、gross/手数与订单门 |
| `[execution]` | FAK、滑点、legging、latency、impact、metadata |
| `[paths]` | state、log、report、journal、alert |
| `[[contracts]]` / `[[pairs]]` | 固定回放的合约、费率与 pair |

实盘凭证不写入仓库，只从环境变量读取：`AFUTURE_CTP_USER`、`AFUTURE_CTP_PASSWORD`、`AFUTURE_CTP_BROKER`。真实报单还要求：

```text
AFUTURE_LIVE_ACK=I_UNDERSTAND_FUTURES_RISK
```

所有字段、单位与约束以 `afuture/config.py`、`afuture/risk.py` 和示例配置为事实来源；生产使用前必须执行 `validate` 和 `doctor`。

## 运行

```bash
# 配置与 CLI
afuture --help
afuture validate --config config/afuture.example.toml

# 回放与研究
afuture replay --config config/afuture.example.toml --data examples/sample_ticks.csv
afuture scan --config config/afuture.example.toml --data examples/research_ticks.csv
afuture accept --config config/afuture.example.toml --data examples/research_ticks.csv --pair m_calendar
afuture accept-auto --config config/afuture.auto-replay.example.toml --data examples/research_ticks.csv
afuture data-check --config config/afuture.auto-replay.example.toml --data examples/research_ticks.csv

# 本地只读状态；不初始化日志、不连接 CTP
afuture status --config config/afuture.directional-live.example.toml

# CTP 无报单预检与 Shadow（还需要 AFUTURE_LIVE_ACK）
afuture doctor --config config/afuture.directional-live.example.toml --confirm-live
afuture shadow --config config/afuture.directional-live.example.toml --duration-seconds 3600

# 实盘入口；必须先完成 production checklist
afuture live --config config/afuture.directional-live.example.toml --confirm-live

# 人工恢复入口先阅读帮助；不得绕过对账与 Kill Switch
afuture recover-state --help

# 执行质量
afuture quality-report --config config/afuture.directional-live.example.toml --output runtime/execution_quality_report.json
```

`status` 只读当前 state、显式 `.prev` 证据、运行文件大小和磁盘/路径条件；当前 state 损坏时返回 2，但绝不自动采用 `.prev`。`doctor` 在 fresh CTP snapshot 后逐项检查账户、活动委托、元数据、持仓对账、Kill Switch、runtime mode、持久化安全门和 Directional completed activity；任一安全门失败返回 2，且不会报单。

`recover-state` 是人工核验后的 fail-closed 恢复入口，不是绕过对账或 Kill Switch 的快捷方式。运行手册见 [`docs/live-trading.md`](docs/live-trading.md)。

## 测试与静态门禁

```bash
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python -m mypy afuture
python -m compileall -q afuture
```

昂贵的真实数据、Stress matrix 和 acceptance matrix 保持为手动 GitHub workflows；只有策略、生产机械、数据方法、执行时点或成本假设发生实质变化时才重跑。

## 风险与会计不变量

- 未知 CTP direction/offset/type/status 拒绝转换，不猜默认经济语义；
- live tick、账户、合约元数据或持仓快照含 NaN/inf、非整数手数、空标识或无效均价时 fail-closed；
- reject、cancel 或未成交订单不得修改持仓；partial fill 只按实际成交更新；
- trade callback 以 `trading_day:trade_id` 去重；首个新交易日成交先触发换日再入账，持久化集合换日时只淘汰旧日 ID，重连/重启不得重复入账；
- SHFE/INE `CLOSE_TODAY` 与 `CLOSE_YESTERDAY` 不能跨 bucket 借量；
- reductions 在 openings 前完成；`REDUCE_ONLY`、daily circuit 和 HALT 不得增加风险；
- cash、realized/unrealized PnL、commission、margin、gross/net exposure 使用明确 multiplier 和单位；
- state checksum/schema/sequence、持仓数量/均价或去重历史不可信时禁止 load，也禁止覆盖原文件；每次成功推进前把上一份已验证 envelope 原样保存为 `.prev`，但运行时永不自动回退；
- audit/alert JSONL 在完整记录边界按 20 MiB 轮转，保留 14 份备份；轮转失败不允许静默丢证据；
- Directional hard authority 保持 target/realized gross `<=2x`、margin `<=35%`、available `>=25%`、daily-loss `5%`、total drawdown `30%`、单合约 `<=35` 手；
- 研究日索引重复、非单调、NaN/inf、非正必需价格均 fail-closed；
- train/validation/OOS 独立模拟；`full_recent` 是汇总窗口，会与这些子窗口重叠，不能被描述成独立 holdout。

## 模块地图

| 模块 | 职责 |
| --- | --- |
| `models.py`, `config.py` | 领域记录、枚举、配置与校验 |
| `strategy.py`, `auto.py`, `scanner.py` | Calendar / Auto 信号与候选 |
| `directional_*` | Directional policy、activity、目标、研究 evaluator |
| `risk.py`, `portfolio_risk.py` | 账户硬门与组合风险 |
| `execution.py`, `directional_execution.py` | 订单计划、双腿与价格选择 |
| `broker/` | Sim / Shadow / CTP 适配 |
| `position.py`, `state.py` | 持仓不变量、持久化完整性与显式 previous evidence |
| `engine.py`, `directional_engine.py` | 事件顺序、状态机与运行编排 |
| `research.py`, `directional_acceptance.py` | Walk-forward、Stress、deterministic account proxy |
| `quality.py`, `journal.py`, `jsonl.py`, `report.py` | 执行证据、有界审计与报告 |
| `operations.py`, `cli.py`, `runtime_factory.py` | 运维预检、命令入口与 runtime 组装 |

## 文档

- [`docs/documentation-index.md`](docs/documentation-index.md)：全部 Markdown 的权威分类；
- [`docs/architecture.md`](docs/architecture.md)：模块边界、依赖方向、交易与研究主链路；
- [`docs/data-and-backtest.md`](docs/data-and-backtest.md)：数据时点、回放、窗口与 proxy 限制；
- [`docs/live-trading.md`](docs/live-trading.md)：Shadow、CTP、停机和恢复；
- [`docs/production-checklist.md`](docs/production-checklist.md)：真实资金门；
- [`docs/stress90-final-evidence.md`](docs/stress90-final-evidence.md)：当前 production research checkpoint；
- [`docs/refactoring/architecture-audit-20260825.md`](docs/refactoring/architecture-audit-20260825.md)：工业重构审计与严重度记录。
- [`docs/archive/evidence/`](docs/archive/evidence/)：已过期或被替代的研究证据；不描述当前系统行为。
- [`docs/archive/development/`](docs/archive/development/)：历史设计与实施记录；不作为运行契约。
