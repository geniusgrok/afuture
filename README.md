# afuture

`afuture` 是面向个人使用的国内商品期货自动交易程序，保留两条**账户互斥**的正式交易链：

1. **Calendar Spread / Auto**：同品种相邻月份跨期套利；
2. **Execution-Aligned Directional Portfolio**：冻结 50 品种、96-template 的方向组合。

项目只保留一套账户/订单/成交/持仓真相：Broker/CTP。Directional 不建立第二账户状态机，继续复用 `RiskManager`、Kill Switch、`REDUCE_ONLY`、状态持久化、启动对账、Shadow 和执行质量证据。

## 当前历史证据

必须区分研究口径与生产机械口径；两者都不是未来收益保证。

### Production-mechanics proxy（当前生产语义）

固定区间 `2024-08-21 ~ 2026-08-20`，使用 specific-contract 日线、integer lots、上一完整交易日 activity 选约、当前账户硬门和 5bp Base 成本。最终冻结 L3：

| 指标 | Base 5bp / 12% margin proxy | Stress 15bp / 15% margin proxy |
|---|---:|---:|
| 年化收益 | **108.8461%** | **0.9249%** |
| 累计收益 | **311.4052%** | **1.7840%** |
| 最大回撤 | **17.8010%** | **5.8553%** |
| Sharpe | **2.0812** | 0.2246 |
| 活跃交易日 | **478 / 484** | **14 / 484** |
| 最终权益（初始 500,000） | **2,057,025.78** | 508,919.91 |
| daily circuit days | 4 | 0 |
| margin reject days | 0 | 6 |
| realized gross 峰值 | **1.998253x** | 1.856519x |
| 最终状态 | **未 HALT** | **HALT** |

Base 同时通过本轮四个硬门：年化收益 `>=100%`、最大回撤 `<=30%`、实际 realized gross `<=2.0x`、全区间不永久 HALT。

但 **Stress 没有通过**：15bp + 15% margin proxy 下很早触发保证金硬门并 HALT。因此不能把 Base 的 108.8461% 描述为“已经证明稳健”或“真实账户可保证获得”。

最终 L3 证据：workflow run `32617588179`，artifact `production-return-l3-d112697f6da929a702f9b88869a41aed47b29e52`，artifact id `9487448673`，SHA-256 `a56a65593fd83d9addf6b542b67eaf986c11f8a1d8fc48902ae348df14606173`。

### Float-notional specific-contract L4（研究层）

原冻结研究证据仍保留：

| 指标 | Base 5bp | Stress 15bp |
|---|---:|---:|
| 年化收益 | **107.4623%** | **58.1372%** |
| 累计收益 | **306.1855%** | **141.1415%** |
| 最大回撤 | **27.4097%** | **32.9554%** |
| Sharpe | **1.6874** | **1.1525** |
| gross target 上限 | **2.0x** | **2.0x** |

该研究路径存在明确 selection bias；此前已观察的 Final OOS 也不是 pristine holdout。当前 108.8461% production-mechanics Base 同样来自已经反复研究过的历史区间，不能作为独立泛化证明。

详细证据：

- [`docs/return-target-100-evidence.md`](docs/return-target-100-evidence.md)
- [`docs/directional-production-mechanics-evidence.md`](docs/directional-production-mechanics-evidence.md)

## Directional 冻结生产语义

当前正式候选不通过预先给所有目标打折来“留 headroom”。正常目标保持原始 gross（策略自身 `<=2.0x`），风险只允许向下收紧：

- Universe：冻结 50 个成熟中国商品期货品种；
- template pool：冻结 96；
- meta lookback：**11**；
- meta rebalance：**3**；
- active templates：**3**；
- meta score：`0.25 × annualized + 1.0 × Sharpe`，且只使用已完成历史；
- directional 单合约上限：**35 手**；
- gross target 上限：**2.0x**；
- completed-return governor：两日样本波动 `>=3%`，或最近一个已完成账户日收益 `<=-2%`，下一目标缩至 **25%**；否则保持 **100%**；
- governor 只读取已完成交易日账户收益，当前交易日 PnL 不参与本次目标；
- realized-gross guard：Broker/行情真值显示实际 marked gross `>2.0x` 时，只生成 reduction-only FAK；不能安全计算或执行时 fail-closed；
- 目标等于 2.0x 时**不预先 haircut**，实际超限后才由硬 guard 收缩。

### 数据与执行流

```text
Sina/AKShare 连续 OHLC（signal/meta）
        ↓
ExecutionAlignedAggressivePolicy
冻结 96-template / causal meta
        ↓
截至完整交易日 D 的产品权重
        ↓
completed-return governor（仅可降风险）

CTP Tick.trading_day
        ↓
DirectionalActivityTracker
冻结 D 日具体合约最终 OI/volume
        ↓
D+1 concrete contract selection
        ↓
fresh quote / depth / limit / metadata
        ↓
integer target lots，单合约 <=35，target gross <=2x
        ↓
reductions → Broker 确认 → 后续 cycle openings
        ↓
RiskManager → FAK → Broker
        ↓
每个 tick 检查 realized gross；>2x 只减仓
```

关键边界：

- D+1 尚未完成的 activity 不能改变 D 日冻结选约；
- signal 必须覆盖 completed activity day，缓存只能在已覆盖 required day 时兜底；
- stale/missing required evidence 且已有风险时 fail-closed；
- 新目标不可用不能阻塞确定性 reduction；
- Broker trade callback 是成交真相，策略不自行假定成交；
- 同一合约出现多空毛仓时，flatten 按毛持仓分别平多/平空，不能因净仓为 0 误判为无风险。

## 风险与恢复

生产硬门没有为追求历史收益而放宽：

- `max_daily_loss_ratio = 5%`；
- `max_total_drawdown_ratio = 30%`；
- `max_margin_ratio = 35%`；
- `min_available_ratio = 25%`；
- gross target / realized gross 上限 = `2.0x`；
- directional 单合约上限 = `35`。

日亏损 5% 是**同交易日 circuit breaker**：触发后 flatten 并禁止当日重新承担风险；只有进入后续 CTP trading day，且 Broker ready、无活动订单、仓位已平、metadata、账户风险和启动对账全部通过时，才恢复 RUNNING。

总回撤、保证金、可用资金、非正权益、metadata/对账/基础设施错误仍是 hard/manual halt，不因 daily circuit 自动恢复。

## Directional execution quality

同一个 `ExecutionQualityRecorder` 同时记录 pair 和 directional：

- `directional_rebalance`：signal/activity day、target lots、reductions/openings、planned turnover；
- `directional_fill`：expected/fill price、multiplier、slippage、commission；
- `directional_cycle`：realized turnover、tracking error、完成延迟、partial/rejected count。

quality 只做观测，不拥有账户或策略权限。

## Calendar Spread / Auto

原套利路径保持：

```text
CTP Catalog / Tick
→ AutoPairManager
→ CalendarSpreadStrategy
→ PortfolioRisk + RiskManager
→ PairExecutor
→ Broker
```

支持 point-in-time catalog、front-3/adjacent months、activity/sync/stationarity/half-life/Net Edge、动态风险预算、FAK 双腿、partial rollback、managed/open-eligible 分离、bounded warm history 和非阻塞 metadata。

## 安装与常用命令

研究/测试：

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
```

CTP + AKShare：

```bash
python -m pip install -e ".[live,dev]"
```

```bash
# Calendar / Auto
afuture validate --config config/afuture.auto-replay.example.toml
afuture replay --config config/afuture.auto-replay.example.toml --data examples/auto_sample_ticks.csv
afuture accept-auto --config config/afuture.auto-replay.example.toml --data examples/research_ticks.csv

# Directional
afuture validate --config config/afuture.directional-live.example.toml
afuture shadow --config config/afuture.directional-live.example.toml --duration-seconds 3600
afuture doctor --config config/afuture.directional-live.example.toml
afuture live --config config/afuture.directional-live.example.toml --confirm-live

# Execution quality
afuture quality-report --config config/afuture.directional-live.example.toml --output runtime/execution_quality_report.json
```

真实凭证只从环境变量读取；实盘还要求：

```text
AFUTURE_LIVE_ACK=I_UNDERSTAND_FUTURES_RISK
```

## 真实资金门

Base production-mechanics 已在固定历史证据上超过 100%，但这**不是**真实账户收益承诺，也没有消除 selection bias、Stress 失败和历史微观结构缺失。真实资金仍必须依次完成：

1. 多交易日 CTP Shadow；
2. previous-day activity snapshot 与实际主力切换抽查；
3. modeled vs realized turnover/slippage/commission；
4. 实际 margin/risk-off 与 proxy 差异；
5. 测试柜台 FAK、partial、reject、断线、平今/平昨、换月和 gross guard；
6. 极小真实仓位；
7. 新发生、此前未参与任何选择或调参的未来数据。

详见 [`docs/production-checklist.md`](docs/production-checklist.md)。

## 验证层级

```text
L1  局部因果/风控/手数/quality 单测
L2  directional runtime + restart smoke
L3  frozen production-mechanics economic gate
L4  specific-contract / roll-safe / execution-aware research evidence
Final  Python 3.10/3.13 主 CI + repository review
```

昂贵经济证据只在策略公式、生产机械、合约选择、执行时点、成本或数据方法发生实质变化时重跑；文档和无行为清理不重复经济回放。
