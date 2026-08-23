# 100% 年化收益目标：最终历史证据

日期：2026-08-23

## 1. 最终结论

`afuture` 的冻结 Execution-Aligned Directional Portfolio 当前有两个层级的历史结果：

- **研究层 Float L4**：selection-biased specific-contract / next-open float-notional 最近两年 Base 年化 **107.4623%**、Stress 15bp 年化 **58.1372%**；
- **生产机械层 L3**：当前 integer lots、账户硬门、daily circuit、completed-return governor、margin-aware sizing 和 realized-gross hard guard 下，Base 年化 **109.0636%**、Stress 年化 **28.9559%**；两者全区间均未永久 HALT。

准确表述是：

> **固定历史 Base production-mechanics 已达到 100% 年化验收门；Stress 已修复此前的结构性 margin HALT，但年化只有 28.9559%，没有达到 80%。这些结果都不是未来收益保证或独立泛化证明。**

## 2. 原 Float L4

固定区间：`2024-08-21 ~ 2026-08-20`。

| 指标 | Base 5bp | Stress 15bp |
|---|---:|---:|
| 年化收益 | **107.4623%** | **58.1372%** |
| 累计收益 | **306.1855%** | **141.1415%** |
| 最大回撤 | **27.4097%** | **32.9554%** |
| Sharpe | **1.6874** | **1.1525** |
| gross target 上限 | **2.0x** | **2.0x** |

该研究路径存在明确 selection bias；Final OOS 已被研究过程观察，`pristine_final_oos=false`。

## 3. 当前冻结策略

- Universe：50 个成熟中国商品期货品种；
- template pool：固定 96；
- family：breakout、time-series momentum、momentum、moving average、reversal、acceleration；
- meta lookback：**11**；
- meta rebalance：**3**；
- active templates：**3**；
- Base meta score：`0.25 × annualized + 1.0 × Sharpe`；
- Cost robustness：模板的已完成 continuous intraday evidence 必须在 5bp Base 与 15bp Stress 两个成本端点都为正；通过生存门后仍按 Base score 排名；
- 不使用 5bp/15bp 50/50 score，不按 template rebalance 周期删减 96-template pool；
- 产品 signal 只使用前一完整交易日及此前历史；
- gross target：`<=2.0x`；
- directional 单合约上限：35 手。

`ExecutionAlignedAggressivePolicy` 仍是唯一正式 directional signal policy。

## 4. Production risk semantics

### Margin-aware target sizing

此前 Stress 的核心机械冲突是：`15% margin proxy × 1.25 buffer × 2.0x gross = 37.5%`，高于 35% hard margin gate。新版本不放宽 hard gate，而是在目标手数生成阶段按 margin evidence 只向下缩手数。

Soft target margin share：

```text
hard_share = min(max_margin_ratio, 1 - min_available_ratio)
soft_target_share = max(0, hard_share - max_daily_loss_ratio)
```

当前配置得到：

```text
35% - 5% = 30% equity target margin budget
```

Live 使用 Broker side-specific margin rates；历史 acceptance 由于没有逐日 Broker margin truth，仍使用明确标注的 Base 12% / Stress 15% proxy。正常 target 经过 soft sizing 后，开仓仍必须再次通过原有 35% margin / 25% available `RiskManager` hard gate。

### Causal completed-return governor

只使用已完成账户日收益，不读当前 session PnL：

```text
最近 completed daily return <= -2%
OR 最近两日 sample volatility >= 3%
    => next target scale = 25%
else
    => next target scale = 100%
```

### Daily-loss circuit

`max_daily_loss_ratio=5%` 保持不变：触发后当日 flatten、当日禁止新增风险；后续 CTP trading day 只有在 Broker ready、无 active order、已平风险、metadata、账户风险和启动对账均通过时才恢复 RUNNING。

`max_total_drawdown_ratio=30%`、`max_margin_ratio=35%`、`min_available_ratio=25%`、非正权益、metadata/对账异常仍是 hard/manual halt。

### Realized-gross hard guard

- signal target `<=2.0x`；
- actual marked gross `>2.0x`：只提交 reduction-only FAK；
- 无法安全计算或执行：fail-closed；
- exactly-at-limit 的 gross target 不做固定预 haircut；
- 同一合约同时有多/空毛仓时，flatten 分别关闭两侧。

## 5. 最终 Production L3

### 可重复证据

```text
fixed input artifact id = 9473260618
workflow run             = 32634296589
PR head                  = 921bd8b4820a8c1efa9c804a8ea1a1b56c2d188f
PR merge ref             = 1ec387433ee5011e44bc214e4e4c83a28e72c93c
artifact id              = 9491959916
artifact                 = stress-80-l3-1ec387433ee5011e44bc214e4e4c83a28e72c93c
SHA-256                  = e531f2874cbc26c3a54ff561e074f59b86ac887a1f509e33effd6908eaa3144d
```

### 统一参数

```text
initial capital             = 500000
Base cost                   = 5bp one-way
Stress cost                 = 15bp one-way
Base margin proxy           = 12%
Stress margin proxy         = 15%
margin buffer               = 1.25
max margin ratio            = 35%
min available ratio         = 25%
calm soft target share      = 30% (completed shock can contract it)
daily loss limit            = 5%
total drawdown limit        = 30%
max contract volume         = 35
target gross cap            = 2.0x
realized gross hard ceiling = 2.0x
parameter_search            = false
margin_is_historical_truth  = false
```

### full_recent 结果

| 指标 | Base | Stress |
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
| actual gross 峰值 | **1.998253x** | **1.668769x** |
| first divergence | `daily loss limit reached` | `daily loss limit reached` |
| halted | **false** | **false** |

Base 仍通过：年化 `>=100%`、最大回撤 `<=30%`、实际 gross `<=2x`、不永久 HALT。Stress 现在也不永久 HALT，且 15% margin proxy 下没有 opening margin reject。

## 6. 为什么没有继续追 Stress 80%

本轮明确尝试过成本鲁棒性方向，但固定历史给出了负证据：

- 5bp/15bp score 直接 50/50：Base 年化降到约 **94.59%**；
- 进一步只允许 rebalance `>=5` 的 57 templates：Base 年化降到约 **69.78%**；
- 最终选择恢复全部 96 templates，以 Stress 只做生存门，Base 收益恢复到 **109.0636%**。

最终 Stress 28.9559% 已经是真正运行 474/484 天的账户结果，不再是此前“很早 HALT 后把少量收益摊到两年”的 0.9249%。但它仍远低于 80%。继续在同一已经反复观察的两年历史上调 meta 权重、删模板、改 governor 或 margin headroom，主要增加过拟合风险，不足以证明未来实盘更好，因此到此停止。

## 7. 泛化与真实资金限制

历史仍存在 selection bias 和 regime dependence，且缺少多年完整：

- bid/ask/depth；
- queue position；
- partial fill / reject；
- CTP/交易所流控；
- 逐日真实 Broker margin schedule；
- 实际结算手续费；
- gross guard/reduction 的真实成交时延与冲击。

因此真实资金仍必须经过多日 Shadow、测试柜台、极小真实仓位，以及最重要的——**新发生、此前没有参与任何选择和调参的未来数据**。

完整 mechanics 说明见 [`directional-production-mechanics-evidence.md`](directional-production-mechanics-evidence.md)。

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

