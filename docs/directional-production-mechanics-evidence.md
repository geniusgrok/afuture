# Directional Production-Mechanics 最终证据

日期：2026-08-23

## 1. 结论

冻结的 Execution-Aligned Directional Portfolio 当前有两层必须区分的历史证据：

1. **Float-notional specific-contract L4**：`2024-08-21 ~ 2026-08-20` Base 5bp 年化 **107.4623%**，Stress 15bp 年化 **58.1372%**；存在明确 selection bias；
2. **当前 production-mechanics L3**：同区间 Base 年化 **109.0636%**、最大回撤 **15.8529%**；Stress 年化 **28.9559%**、最大回撤 **28.1152%**。两者全区间均未永久 HALT。

本轮解决的是此前 Stress 的结构性保证金失败，而不是通过放宽账户硬门制造收益。上一版 Stress 仅 14/484 个活跃日、年化 0.9249% 并因 margin hard gate HALT；当前版本在相同 15bp 成本、15% margin proxy 和全部原硬门下有 **474/484 个活跃日、0 margin reject、无永久 HALT**。

但 **28.9559% 仍远低于 80%**。同一历史已经被多轮观察，因此停止继续针对该区间扫参数或删模板以追逐 80%。固定历史结果也不能外推成真实账户未来收益保证。

## 2. 固定证据与可重复性

最终 L3 沿用同一原始输入，没有重新抓取或替换数据：

```text
input artifact id = 9473260618
workflow run       = 32634296589
PR head            = 921bd8b4820a8c1efa9c804a8ea1a1b56c2d188f
PR merge ref       = 1ec387433ee5011e44bc214e4e4c83a28e72c93c
artifact id        = 9491959916
artifact           = stress-80-l3-1ec387433ee5011e44bc214e4e4c83a28e72c93c
SHA-256            = e531f2874cbc26c3a54ff561e074f59b86ac887a1f509e33effd6908eaa3144d
```

固定参数：

```text
initial capital             = 500000
Base cost                   = 5bp one-way
Stress cost                 = 15bp one-way
Base margin proxy           = 12%
Stress margin proxy         = 15%
margin estimate buffer      = 1.25
max margin ratio            = 35%
min available ratio         = 25%
daily loss limit            = 5%
total drawdown limit        = 30%
max contract volume         = 35
target gross cap            = 2.0x
realized gross hard ceiling = 2.0x
parameter_search            = false
margin_is_historical_truth  = false
```

每个报告窗口独立以 500,000 / flat 开始；signal/meta 使用同一冻结历史，不按报告窗口重新拟合。

## 3. 当前生产机械

### 3.1 冻结 signal/meta

- Universe：50 品种；
- template pool：固定 96，不新增模板，也不通过 rebalance 周期删除模板；
- `META_LOOKBACK=11`；
- `META_REBALANCE=3`；
- `META_COUNT=3`；
- 基础 score：`0.25 × annualized + 1.0 × Sharpe`；
- Meta cost robustness：同一个模板必须在已完成 continuous intraday proxy 的 5bp Base 与 15bp Stress 两个端点均保持正 trailing evidence；通过 Stress 生存门后仍按 Base score 排名；
- signal/meta 均只使用已完成历史；
- signal gross target `<=2.0x`。

这不是 5bp/15bp 的 50/50 收益优化。此前试验表明等权 robust score 和只保留慢 rebalance 模板都会明显损伤 Base，因此未进入最终候选。

### 3.2 Margin-aware target sizing

历史 proxy 和 live production 都在正常目标构建阶段留出 margin headroom，但账户 hard gate 完全不变。

Soft target share：

```text
hard_share = min(max_margin_ratio, 1 - min_available_ratio)
conservative = max(0, hard_share - max_daily_loss_ratio)
shock = clamp(max(3%, abs(latest completed return), two-day sample volatility), 3%, 5%)
soft_target_margin_share = min(conservative, conservative * (1 - max(0, shock - 3%)))
```

当前配置中 `hard_share=35%`，无历史或平静 completed-return evidence 的 `conservative=30%`；当已完成收益绝对值或两日样本波动高于 3% 时，soft target 只会进一步收缩，永远不会向 35% hard margin gate 扩张。

含义：正常目标不再故意贴着 35% hard margin boundary，并在已完成账户证据变差时主动增加 headroom。**35% hard margin gate 没有变成 40%，也没有被绕过。**

- Live：使用 Broker `ContractSpec` 的多/空保证金率、当前 tick mid、合约乘数和 `margin_estimate_buffer` 逐手估算；
- Acceptance：历史 Broker margin schedule 不可得，因此仍明确使用 12%/15% proxy；
- Integer fitter 只会减少请求手数，不会放大任何目标；缺少正的 margin evidence 时 fail-closed；
- 最终 openings 仍必须再次通过现有 `RiskManager.check_open_orders()` 的 35% margin / 25% available hard gates。

### 3.3 Daily circuit / hard halt

`max_daily_loss_ratio=5%` 是同交易日 circuit breaker：触发后 flatten，当日禁止新增风险；后续 CTP trading day 只有在 Broker ready、无活动订单、已平仓、metadata、账户风险和启动对账全部通过后才恢复 RUNNING。

以下仍是 hard/manual halt：

- `max_total_drawdown_ratio=30%`；
- `max_margin_ratio=35%`；
- `min_available_ratio=25%`；
- 非正 equity；
- metadata / reconciliation / 基础设施异常。

### 3.4 Causal completed-return governor

只使用已完成账户日收益：

```text
最近 completed daily return <= -2%
OR 最近两日 sample volatility >= 3%
    => next target scale = 25%
else
    => next target scale = 100%
```

Governor 永远只能降低原始 signal 风险。

### 3.5 Realized-gross hard guard

- signal target 自身 `<=2.0x`；
- Broker/行情真值的 actual marked gross `>2.0x` 时，只产生 reduction-only FAK；
- 无法安全计算或执行时 fail-closed；
- exactly-at-limit 的 target 不做固定 gross haircut；
- 同一合约同时存在多/空毛仓时，flatten 按毛持仓分别关闭。

Margin-aware sizing 与 realized-gross guard 是不同约束：前者控制预计账户保证金，后者控制实际名义 gross。

## 4. 最终最近两年结果

区间：`2024-08-21 ~ 2026-08-20`，484 个报告交易日。

| 指标 | Base 5bp / 12% margin proxy | Stress 15bp / 15% margin proxy |
|---|---:|---:|
| 年化收益 | **109.0636%** | **28.9559%** |
| 累计收益 | **312.2285%** | **62.9735%** |
| 最大回撤 | **15.8529%** | **28.1152%** |
| 年化波动 | 38.5851% | 31.5242% |
| Sharpe | **2.0976** | **0.9604** |
| 活跃交易日 | **478 / 484** | **474 / 484** |
| 最终权益 | **2,061,142.43** | **814,867.56** |
| daily circuit days | 3 | 2 |
| defensive risk days | 77 | 70 |
| margin reject days | 0 | **0** |
| realized gross 峰值 | **1.998253x** | **1.668769x** |
| first divergence | `daily loss limit reached` | `daily loss limit reached` |
| 最终 HALT | **false** | **false** |

Base 与上一冻结版本的主要经济结果完全恢复；Stress 则从“早期 margin halt”变为完整运行。Stress gross 明显低于 2x 是 15% margin proxy × 1.25 buffer 与 30% soft target margin budget 的自然结果，而不是扩大杠杆。

## 5. Stress 80% 目标为何未继续追

当前固定历史 Stress 年化为 28.9559%，没有达到 80%。本轮在不扫新模板/参数的前提下已逐项评估执行效率机制；仍不能通过以下方式继续在同一两年历史上追数：

- template pool；
- meta 权重；
- rebalance 周期；
- margin headroom；
- governor 阈值；

直到得到 80%，会显著增加 selection bias，并不能增加未来实盘信息。

本轮已经验证并拒绝两类看似合理、但证据为负的改法：

1. 5bp/15bp meta score 直接 50/50 混合：Base 年化降至约 94.59%；
2. 强制只允许 rebalance `>=5` 的 57 个模板：Base 年化进一步降至约 69.78%。

因此最终版本保留全部 96 个模板，只把 15bp evidence 当作生存门；同时把 P0 margin-aware sizing 保留下来，因为它直接消除了 Stress 的结构性 margin reject / halt。

## 6. 泛化风险仍未解决

原 Base 独立窗口仍显示明显 regime dependence：train、validation、selection/OOS、prior regimes 表现差异很大；历史区间也已经参与多轮设计。Production proxy 还缺少多年历史完整 L1 bid/ask/depth、queue position、partial fill/reject、CTP 流控、真实逐日 Broker margin schedule、真实结算手续费和真实 market impact。

因此：

- Base `109.0636%` 不是未来年化保证；
- Stress `28.9559%` 也不是未来下限；
- 下一阶段最有信息价值的是 CTP Shadow、测试柜台、极小真实仓位和真正新发生的未见数据，而不是继续优化同一固定历史。

## 11. 2026-08-23 Execution-efficiency 最终晋级

相对进入本轮前的 `main` commit `b6b2cdca0f04193c10e14f8b3ad61902d6e36769`：

- Base 年化：108.8461% → **109.0636%**（+0.2175 个百分点）；最大回撤 17.8010% → **15.8529%**；
- Stress 年化：20.4057% → **28.9559%**（+8.5502 个百分点）；最大回撤 27.9925% → **28.1152%**；
- Stress active days：472 → **474 / 484**；margin rejects 仍为 **0**；no permanent HALT；
- hard gates 保持 2x gross / 35% margin / 25% available / 5% daily loss / 30% DD / 35 lots。

最终固定证据：workflow run `32634296589`，PR head `921bd8b4820a8c1efa9c804a8ea1a1b56c2d188f`，PR merge ref `1ec387433ee5011e44bc214e4e4c83a28e72c93c`，artifact id `9491959916`，artifact `stress-80-l3-1ec387433ee5011e44bc214e4e4c83a28e72c93c`，SHA-256 `e531f2874cbc26c3a54ff561e074f59b86ac887a1f509e33effd6908eaa3144d`。

Stress turnover attribution（notional）：entry/exit `189,920,240`、resize `51,722,155`、reversal `11,314,145`、roll `2,521,010`、daily circuit `1,440,740`，总计 `256,918,290`；所有 bucket 与总 turnover 精确闭合。

最终**保留**：turnover attribution、completed-day contract-roll hysteresis、same-sign `+1 lot` increase no-trade、completed-return shock adaptive margin contraction。最终**拒绝并回退**：product replacement persistence、cost-aware meta hysteresis、same-direction weight resize hysteresis。前两者固定 L3 分别把 Base 压至约 58.41% 与 58.32%；meta hysteresis 还令 Stress 为 -13.03%、DD 超 30% 并 HALT。

因此 28.9559% 仍不是 80%。本轮证据反而证明高换手中有相当部分是有效 Alpha 迁移，不能无限压低 turnover；在同一已反复观察历史上继续调整门槛直到得到 80% 会增加 selection bias，而不是提高实盘可信度。

