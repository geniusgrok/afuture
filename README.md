# afuture

`afuture` 是面向个人使用的国内商品期货自动交易程序，保留两条**账户互斥**的正式交易链：

1. **Calendar Spread / Auto**：同品种相邻月份跨期套利；
2. **Execution-Aligned Directional Portfolio**：冻结 50 品种、96-template 的方向组合。

项目只保留一套账户/订单/成交/持仓真相：Broker/CTP。Directional 不建立第二账户状态机，继续复用 `RiskManager`、Kill Switch、`REDUCE_ONLY`、状态持久化、启动对账、Shadow 和执行质量证据。

## 当前历史证据

必须区分研究口径与生产机械口径；两者都不是未来收益保证。

### Production-mechanics proxy（当前生产语义）

固定区间 `2024-08-21 ~ 2026-08-20`，使用 specific-contract 日线、integer lots、上一完整交易日 activity 选约、当前账户硬门、causal completed-return governor、margin-aware target sizing 和 realized-gross hard guard。最终固定 L3：

| 指标 | Base 5bp / 12% margin proxy | Stress 15bp / 15% margin proxy |
|---|---:|---:|
| 年化收益 | **108.8461%** | **20.4057%** |
| 累计收益 | **311.4052%** | **42.8545%** |
| 最大回撤 | **17.8010%** | **27.9925%** |
| Sharpe | **2.0812** | **0.7466** |
| 活跃交易日 | **478 / 484** | **472 / 484** |
| 最终权益（初始 500,000） | **2,057,025.78** | **714,272.26** |
| daily circuit days | 4 | 4 |
| defensive risk days | 78 | 70 |
| margin reject days | 0 | **0** |
| realized gross 峰值 | **1.998253x** | **1.684784x** |
| 最终状态 | **未 HALT** | **未 HALT** |

上一版本的 Stress 只有 `0.9249%` 年化、14/484 个活跃日并因 margin hard gate 永久 HALT。当前版本通过 margin-aware target sizing 消除了这个结构性失败：Stress 在完整区间持续运行，但在更高成本和更高保证金假设下年化仍只有 **20.4057%**，远未达到 80%。因此准确结论是：**Base 固定历史生产机械门通过，Stress 生存性显著改善，但 80% Stress 收益目标未被证明。**

最终 L3 证据：workflow run `32624688557`，PR merge ref `7664851987a59c2d87d1084376ffbd9649863b2c`，artifact `stress-robustness-l3-7664851987a59c2d87d1084376ffbd9649863b2c`，artifact id `9489421243`，SHA-256 `6a9abb9eb15a542eda2683200bbf5f001613dccd9546bde11f85dc4f0aa6add7`。固定输入 artifact id 仍为 `9473260618`。

### Float-notional specific-contract L4（研究层）

原冻结研究证据仍保留：

| 指标 | Base 5bp | Stress 15bp |
|---|---:|---:|
| 年化收益 | **107.4623%** | **58.1372%** |
| 累计收益 | **306.1855%** | **141.1415%** |
| 最大回撤 | **27.4097%** | **32.9554%** |
| Sharpe | **1.6874** | **1.1525** |
| gross target 上限 | **2.0x** | **2.0x** |

该研究路径存在明确 selection bias；此前已观察的 Final OOS 也不是 pristine holdout。Production L3 同样使用已经反复研究过的历史，因此不能把任何历史年化当成未来真实账户收益保证。

详细证据：

- [`docs/return-target-100-evidence.md`](docs/return-target-100-evidence.md)
- [`docs/directional-production-mechanics-evidence.md`](docs/directional-production-mechanics-evidence.md)

## Directional 冻结生产语义

- Universe：冻结 50 个成熟中国商品期货品种；
- template pool：冻结 96；
- meta lookback：**11**；
- meta rebalance：**3**；
- active templates：**3**；
- meta 基础分数：`0.25 × annualized + 1.0 × Sharpe`；
- meta cost robustness：模板必须在已完成历史的 **5bp Base 与 15bp Stress** 两个成本端点都保持正 trailing evidence；通过 Stress 生存门后仍按 **Base score** 排名，不做 50/50 收益优化，也不删除高频模板；
- 产品 signal：只使用前一完整交易日及此前历史；
- gross target：`<=2.0x`；
- directional 单合约上限：35 手。

### Margin-aware target sizing

Signal 可以输出不超过 2.0x 的目标，但执行层不会把正常目标故意放在账户 hard margin 边界上。

生产环境使用 Broker `ContractSpec` 的多/空保证金率、当前行情、合约乘数和 `margin_estimate_buffer` 估算每手 margin；历史 acceptance 使用明确标注的 12%/15% proxy。Soft sizing budget 为：

```text
hard margin share = min(max_margin_ratio, 1 - min_available_ratio)
soft target share = max(0, hard margin share - max_daily_loss_ratio)
```

当前 35% margin / 25% available / 5% daily-loss 配置下，正常目标 margin budget 为 **30% equity**。这不是新的 hard gate，也不改变 35% margin 上限；它只是保留 5 个百分点的 mark-to-market headroom。最终开仓仍必须再次通过现有 `RiskManager.check_open_orders()`，35%/25% hard gates 始终具有最终否决权。

### 其他风险语义

- completed-return governor：最近已完成账户日收益 `<=-2%`，或最近两日样本波动 `>=3%`，下一目标缩至 **25%**；否则保持 **100%**；当前 session PnL 不参与本次目标；
- `max_daily_loss_ratio=5%`：同交易日 circuit breaker；触发后 flatten，当日不再新增风险，后续 CTP trading day 在 Broker/订单/仓位/metadata/账户风险/启动对账全部通过后可恢复；
- `max_total_drawdown_ratio=30%`、`max_margin_ratio=35%`、`min_available_ratio=25%`、非正权益、metadata/对账异常仍是 hard/manual halt；
- realized-gross guard：Broker/行情真值显示实际 marked gross `>2.0x` 时，只生成 reduction-only FAK；无法安全计算或执行时 fail-closed；
- 同一合约同时存在多仓和空仓时，flatten 按毛仓分别平仓，不能因 `net_volume=0` 把真实风险误判为 flat。

### 数据与执行流

```text
Sina/AKShare 连续 OHLC（signal/meta）
        ↓
ExecutionAlignedAggressivePolicy
96-template / Base-rank + Stress-survival causal meta
        ↓
截至完整交易日 D 的产品权重
        ↓
completed-return governor（仅可降风险）
        ↓
CTP completed activity 选 D+1 concrete contracts
        ↓
fresh quote / depth / limit / live metadata
        ↓
integer target lots
        ↓
margin-aware soft sizing（当前 30% equity target margin budget）
        ↓
reductions → Broker 确认 → openings
        ↓
RiskManager hard gates → FAK → Broker
        ↓
每个 tick 检查 realized gross；>2x 只减仓
```

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

Base production-mechanics 已在固定历史证据上超过 100%，Stress 已从早期 margin HALT 修复为完整运行，但 **20.4057% Stress 年化不是 80%，更不是未来真实收益承诺**。真实资金仍必须依次完成：

1. 多交易日 CTP Shadow；
2. previous-day activity snapshot 与实际主力切换抽查；
3. modeled vs realized turnover/slippage/commission；
4. 实际 Broker margin 与 proxy/soft target 的差异；
5. 测试柜台 FAK、partial、reject、断线、平今/平昨、换月、margin sizing 和 gross guard；
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
