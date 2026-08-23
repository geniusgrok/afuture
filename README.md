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
| 年化收益 | **109.0636%** | **28.9559%** |
| 累计收益 | **312.2285%** | **62.9735%** |
| 最大回撤 | **15.8529%** | **28.1152%** |
| Sharpe | **2.0976** | **0.9604** |
| 活跃交易日 | **478 / 484** | **474 / 484** |
| 最终权益（初始 500,000） | **2,061,142.43** | **814,867.56** |
| daily circuit days | 3 | 2 |
| defensive risk days | 77 | 70 |
| margin reject days | 0 | **0** |
| realized gross 峰值 | **1.998253x** | **1.668769x** |
| 最终状态 | **未 HALT** | **未 HALT** |

更早的 Stress 只有 `0.9249%` 年化、14/484 个活跃日并因 margin hard gate 永久 HALT；PR #13 已先修复到 `20.4057%` / 472 active days。本轮 execution-efficiency 收口进一步把最终 Stress 提升到 **28.9559%** / 474 active days，同时 Base 从 PR #13 的 108.8461% 提升到 **109.0636%**。当前版本通过 margin-aware target sizing 消除了这个结构性失败：Stress 在完整区间持续运行，但在更高成本和更高保证金假设下年化仍只有 **28.9559%**，远未达到 80%。因此准确结论是：**Base 固定历史生产机械门通过，Stress 生存性显著改善，但 80% Stress 收益目标未被证明。**

最终 L3 证据：workflow run `32634296589`，PR merge ref `1ec387433ee5011e44bc214e4e4c83a28e72c93c`，artifact `stress-80-l3-1ec387433ee5011e44bc214e4e4c83a28e72c93c`，artifact id `9491959916`，SHA-256 `e531f2874cbc26c3a54ff561e074f59b86ac887a1f509e33effd6908eaa3144d`。固定输入 artifact id 仍为 `9473260618`。

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

还必须区分 lineage：上述 **58.1372%** 是较早的归档 Float 权重路径；PR #14 当前冻结权重在同一 roll-safe next-open 口径下的 Float 15bp 年化为 **109.3145%**（0bp 毛收益年化 **236.4127%**）。因此不能把归档 58.1372% 与当前 Production 28.9559% 做逐项可加的 mechanics 分解，更不能对已经包含 15bp 的 58.1372% 再扣一次交易成本。

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
hard_share = min(max_margin_ratio, 1 - min_available_ratio)
conservative = max(0, hard_share - max_daily_loss_ratio)
shock = clamp(max(3%, abs(latest completed return), two-day sample volatility), 3%, 5%)
soft_share = min(conservative, conservative * (1 - max(0, shock - 3%)))
```

当前 35% margin / 25% available / 5% daily-loss 配置下，无历史或平静 completed-return evidence 的正常目标 margin budget 为 **30% equity**；已完成收益绝对值或两日样本波动高于 3% 时只会进一步收缩，永远不会把 soft target 扩张到 35% hard gate。这不是新的 hard gate，也不改变 35% margin 上限；它只是保留 5 个百分点的 mark-to-market headroom。最终开仓仍必须再次通过现有 `RiskManager.check_open_orders()`，35%/25% hard gates 始终具有最终否决权。

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
CTP completed activity 选 D+1 concrete contracts（eligible incumbent 默认保留；challenger 必须同时在 OI 和 volume 上更高才换月）
        ↓
fresh quote / depth / limit / live metadata
        ↓
integer target lots
        ↓
adaptive margin-aware soft sizing（平静基线 30%，completed shock 只可继续收缩）
        ↓
同方向 +1 lot 低价值增仓可保持 incumbent；任何减仓/反转/风险动作不受抑制
        ↓
reductions → Broker 确认 → openings
        ↓
opening pricing：对手一档深度覆盖整笔手数时用 best opposite；否则保留原 aggressive tick
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

quality 只做观测，不拥有账户或策略权限。Directional reduction FAK 始终保留原 aggressive price；depth-aware 只作用于正常 opening，不改变手数、订单数、FAK 类型或 hard-risk 权限。

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

Base production-mechanics 已在固定历史证据上超过 100%，Stress 已从早期 margin HALT 修复为完整运行，但 **28.9559% Stress 年化不是 80%，更不是未来真实收益承诺**。真实资金仍必须依次完成：

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


## Execution-efficiency 最终处置（2026-08-23）

本轮不是把所有“降换手”想法都塞进生产。固定 L3 逐项决定晋级：

- **保留**：turnover attribution；eligible-incumbent contract-roll hysteresis；同方向 `+1 lot` 低价值增仓抑制；completed-return 驱动的 margin soft-envelope 收缩。
- **拒绝并回退**：product replacement persistence（Base 约 58.41%、Stress 约 22.55%）；cost-aware meta hysteresis（Base 约 58.32%、Stress -13.03%、DD 超 30% 且 HALT）；same-direction weight resize hysteresis（未通过 L3 promotion gate）。
- 更早已拒绝：Base/Stress 50/50 meta score（Base 约 94.59%）和只保留慢 rebalance templates（Base 约 69.78%）。

最终 Stress 从 `20.4057%` 提升到 **28.9559%**，但仍没有达到 80%。高频 entry/exit 中包含真实 Alpha，不能把 turnover 本身当作错误并无限压低；继续在同一两年历史上追到 80% 会把研究目标变成 selection fitting。

## Net-alpha efficiency 研究收口（PR #15）

PR #14 之后没有继续在同一两年窗口追求打印 80%。本轮先加入**行为中性的生产事件/PnL/容量审计**，再用该证据检查 entry/exit、net-edge、新 Alpha family 与 margin capacity：

- Stress gross signal PnL `700,245.00`，其中 long `460,515.00`、short `239,730.00`；
- Stress turnover `256,918,290.00`，15bp 成本 `385,377.435`，约消耗 gross signal PnL 的 **55.03%**；
- entry+exit 占 Stress turnover **73.92%**，但 causal cohort 在 prior1/prior2/train/validation/OOS 上没有稳定负贡献，不能作为 churn 机械删除；
- 单一、预声明的 completed-history net-edge gate 在 prior2 发生明显反向分离，未进入生产；
- slow/confirmed trend、cross-sectional momentum、breakout confirmation、point-in-time carry/carry+trend、strength-ranked trend 均未跨窗口通过 15bp 门；
- shock-derived 33.04% calm soft-margin research candidate 在固定 Stress screen 仅 **4.7970% 年化**且 permanent HALT，明确拒绝；35%/25%/5%/30% 等硬门完全未变。

因此 PR #15 **不改变 Alpha、风险权限、leverage、成本或 margin 假设**。最终 Production Before/After 经济指标保持完全一致：Base 109.0636%，Stress 28.9559%。进入主线的是审计能力与可复现负证据，而不是一个为了历史收益而新增的生产规则。完整过程证据见 [`docs/directional-net-alpha-efficiency-evidence.md`](docs/directional-net-alpha-efficiency-evidence.md)；最终报告见 [`docs/directional-net-alpha-efficiency-final-report.md`](docs/directional-net-alpha-efficiency-final-report.md)。
