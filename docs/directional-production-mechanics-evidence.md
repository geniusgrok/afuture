# Directional Production-Mechanics 最终证据

日期：2026-08-23

## 1. 结论

冻结的 Execution-Aligned Directional Portfolio 目前有两层必须区分的历史证据：

1. **Float-notional specific-contract L4**：`2024-08-21 ~ 2026-08-20` Base 5bp 年化 **107.4623%**，但存在明确 selection bias；
2. **当前 production-mechanics L3**：同区间 Base 5bp 年化 **108.8461%**、最大回撤 **17.8010%**、realized gross 峰值 **1.998253x**，全区间**未永久 HALT**。

因此，本轮“production-mechanics Base 年化 >=100%，最大回撤 <=30%，实际 gross <=2x，且不永久 HALT”的固定历史验收已通过。

但该结论不能外推成真实账户收益保证：15bp + 15% margin proxy 的 Stress 仅年化 **0.9249%**，并因保证金硬门 HALT；此外用于设计和验证的两年历史已经被反复观察，不是 pristine holdout。

本轮没有放宽：

- `max_daily_loss_ratio=5%`；
- `max_total_drawdown_ratio=30%`；
- `max_margin_ratio=35%`；
- `min_available_ratio=25%`；
- gross target / realized gross hard ceiling `2.0x`。

## 2. 固定证据与可重复性

没有重新抓取历史数据。最终 L3 使用：

- specific-contract 原始数据 artifact id：`9473260618`；
- `broad_daily_universe.csv`：同一固定 artifact 中的连续日线信号证据；
- 冻结 50 品种、96-template 的当前 `ExecutionAlignedAggressivePolicy`；
- 区间：`2024-08-21 ~ 2026-08-20`；
- 当前生产机械：integer lots、上一完整交易日 activity 选约、合约乘数、账户门、daily circuit、causal governor、realized gross guard；
- 参数搜索：`false`；
- 历史 Broker margin 真值：`false`。

最终验收：

```text
workflow run = 32617588179
PR head      = 4522abe69e66f6bfc5329ee54a66b6d0c4ee59e4
PR merge SHA = d112697f6da929a702f9b88869a41aed47b29e52
artifact id  = 9487448673
artifact     = production-return-l3-d112697f6da929a702f9b88869a41aed47b29e52
SHA-256      = a56a65593fd83d9addf6b542b67eaf986c11f8a1d8fc48902ae348df14606173
```

Artifact 包含：

- `current_production_weights.csv`；
- `directional_production_mechanics_report.json`；
- `directional_production_base_daily.csv`；
- `directional_production_stress_daily.csv`。

## 3. 最终生产机械

### 冻结 signal/meta

- Universe：50 品种；
- template pool：96；
- `META_LOOKBACK=11`；
- `META_REBALANCE=3`；
- `META_COUNT=3`；
- score：`0.25 × annualized + 1.0 × Sharpe`；
- signal/meta 只使用已完成历史；
- gross target `<=2.0x`。

### 手数与账户门

- 初始资金：500,000；
- integer lot floor；
- directional 单合约上限：35 手；
- Base cost：5bp one-way；
- Stress cost：15bp one-way；
- Base margin proxy：12% × `1.25` buffer；
- Stress margin proxy：15% × `1.25` buffer；
- `max_margin_ratio=35%`；
- `min_available_ratio=25%`；
- `max_daily_loss_ratio=5%`；
- `max_total_drawdown_ratio=30%`。

### Daily circuit

5% 日亏损不是永久关闭整个历史：

- 当日触发后 flatten；
- 当日禁止重新新增风险；
- 只有后续 CTP trading day 且 Broker ready、无活动订单、已平风险、metadata、账户风险和启动对账全部通过才恢复 RUNNING；
- total drawdown、margin、available cash、非正 equity 仍是 hard/manual halt。

### Causal governor

未来目标只由**已完成账户日收益**决定：

- 最近一个 completed daily return `<= -2%` → `0.25x`；
- 或最近两日样本波动 `>=3%` → `0.25x`；
- 否则 `1.0x`；
- governor 永远不能放大原始冻结策略目标。

### Realized gross hard guard

最终实现**不预先把所有目标乘 0.95**。原因是固定 headroom 会通过 integer-lot/复利路径显著破坏收益，且不是实际硬门的直接定义。

正式语义是：

- signal target 自身必须 `<=2.0x`；
- Broker/行情真值计算的实际 marked gross 在运行中 `>2.0x` 时，只产生 reduction-only FAK；
- reduction 无法安全计算或执行时 fail-closed；
- exactly-at-limit 的目标不预先 haircut；
- acceptance proxy 同样对 close mark 后的 actual gross 做 reduction-only guard，并计入 reduction cost。

## 4. 最近两年最终结果

区间：`2024-08-21 ~ 2026-08-20`，484 个报告交易日。

| 指标 | Base 5bp / 12% margin proxy | Stress 15bp / 15% margin proxy |
|---|---:|---:|
| 年化收益 | **108.8461%** | **0.9249%** |
| 累计收益 | **311.4052%** | **1.7840%** |
| 最大回撤 | **17.8010%** | **5.8553%** |
| 年化波动 | 38.8943% | 4.5535% |
| Sharpe | **2.0812** | 0.2246 |
| 活跃交易日 | **478 / 484** | **14 / 484** |
| 最终权益 | **2,057,025.78** | 508,919.91 |
| daily circuit days | 4 | 0 |
| defensive risk days | 78 | 2 |
| margin reject days | 0 | 6 |
| realized gross 峰值 | **1.998253x** | 1.856519x |
| first divergence | `daily loss limit reached` | `combined margin ratio would exceed limit` |
| 最终 HALT | **false** | **true** |

### Base 验收

固定 L3 直接断言并通过：

```text
annualized_return >= 1.00
max_drawdown >= -0.30
max_realized_gross_notional_ratio <= 2.00
halted == false
daily_circuit_days > 0
```

这证明 108.8461% 不是靠绕过 daily-loss gate 得到：Base 实际发生了 4 个 daily circuit，之后按因果恢复；同时 78 日进入 completed-return defensive scaling。

### Stress 的含义

Stress 很差，但不能被隐藏：其主要失败来自更高成本和更高 margin proxy 下的保证金硬门。它不是“108.8461% 的低收益版本”，而是一个很早被硬风险门终止的独立账户实验。

所以当前结论只能是：**Base 历史 production-mechanics 目标通过；Stress robustness 未通过。**

## 5. 独立窗口

这些窗口都独立从 500,000 / flat 开始，不能拼接成一条账户曲线。

Base：

| 窗口 | 年化收益 | 最大回撤 | 最终 HALT |
|---|---:|---:|---|
| train | 27.0579% | 15.9454% | false |
| validation | 225.7700% | 13.2886% | false |
| selection_full | 57.0719% | 17.8010% | false |
| OOS（已被观察） | 55.9159% | 13.4475% | false |
| prior1 | -28.9394% | 30.0493% | true |
| prior2 | -28.7962% | 30.0915% | true |

“当前 OOS”已经参与过此前研究判断，不能重新命名成 pristine OOS。prior1/prior2 的失败也说明策略具有明显 regime dependence，108.8461% 不是均匀稳定地产生。

## 6. 与 float-notional L4 的关系

原研究层 fixed evidence：

| 指标 | Float Base 5bp | Production Base 5bp |
|---|---:|---:|
| 年化收益 | 107.4623% | **108.8461%** |
| 累计收益 | 306.1855% | **311.4052%** |
| 最大回撤 | 27.4097% | **17.8010%** |
| gross | target <=2.0x | actual peak **1.998253x** |

这不是“production 必然优于 float”的一般结论。路径差异来自 integer lots、daily circuit、completed-return governor、actual gross guard、成本与账户状态机的组合，且最终机械本身是在同一历史上经过研究收口得到的。

## 7. 仍然不是精确实盘重放

Production proxy 仍缺多年历史完整：

- L1 bid/ask/depth；
- queue position；
- partial fill / reject；
- CTP/交易所流控；
- 历史逐日 Broker margin schedule；
- 实际结算手续费；
- reduction FAK 成交后下一 cycle opening 的真实分钟/秒级价格；
- 实际盘中 gross guard 的成交时延与冲击成本。

因此该结果不能替代 CTP Shadow、测试柜台和极小真实资金。

## 8. 对生产决策的含义

当前应该冻结的判断是：

- 不再为“历史 100%”继续扩大 Alpha/template/参数搜索；
- Base production-mechanics 已达到目标；
- Stress robustness 仍明显不足；
- 风控硬门没有为收益目标放宽；
- 下一阶段最有信息价值的是新的、未参与历史选择的真实执行证据：Shadow/test/small-capital 的 realized turnover、slippage、commission、margin、daily circuit、gross guard 和恢复行为。

任何未来风险阈值变化都应基于新增真实账户证据，而不是继续追逐同一两年历史的更高数字。
