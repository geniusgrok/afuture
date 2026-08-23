# 100% 年化收益目标：最终历史证据

日期：2026-08-23

## 1. 最终结论

`afuture` 的冻结 Execution-Aligned Directional Portfolio 目前有两个不同层级的历史结果：

- **研究层 Float L4**：selection-biased specific-contract / next-open float-notional 最近两年 Base 年化 **107.4623%**；
- **生产机械层 L3**：当前 integer lots、账户硬门、daily circuit、completed-return governor 和 realized-gross hard guard 下，最近两年 Base 年化 **108.8461%**、最大回撤 **17.8010%**、实际 gross 峰值 **1.998253x**，全区间未永久 HALT。

所以当前准确表述是：

> **固定历史 Base production-mechanics 已达到 100% 年化验收门，但这不是未来收益保证，也不是独立泛化证明。**

Stress 仍未通过：15bp 成本 + 15% margin proxy 下年化只有 **0.9249%**，并因保证金硬门 HALT。该失败必须与 Base 结果一起保留。

## 2. 原 Float L4

固定区间：`2024-08-21 ~ 2026-08-20`。

| 指标 | Base 5bp | Stress 15bp |
|---|---:|---:|
| 年化收益 | **107.4623%** | **58.1372%** |
| 累计收益 | **306.1855%** | **141.1415%** |
| 最大回撤 | **27.4097%** | **32.9554%** |
| Sharpe | **1.6874** | **1.1525** |
| gross target 上限 | **2.0x** | **2.0x** |

Extreme 30bp 的历史结果仍明显更差。此前标记为 Final OOS 的 `2026-02-21 ~ 2026-08-20` 也已经被研究流程观察，因此 `pristine_final_oos=false`。

## 3. 当前冻结策略

- Universe：50 个成熟中国商品期货品种；
- template pool：固定 96；
- family：breakout、time-series momentum、momentum、moving average、reversal、acceleration；
- meta lookback：**11**；
- meta rebalance：**3**；
- active templates：**3**；
- meta score：`0.25 × annualized + 1.0 × Sharpe`；
- meta evidence：已完成 continuous `open→close` intraday proxy；
- 产品 signal：只使用前一完整交易日及此前历史；
- gross target：`<=2.0x`；
- directional 单合约上限：35 手。

`ExecutionAlignedAggressivePolicy` 是唯一正式 directional signal policy。

## 4. Production risk semantics

### Causal completed-return governor

只使用已完成账户日收益，不读当前 session PnL：

```text
最近 completed daily return <= -2%
OR 最近两日 sample volatility >= 3%
    => next target scale = 25%
else
    => next target scale = 100%
```

Governor 只允许减少风险，不能把原 signal 放大到 1.0x 以上。

### Daily-loss circuit

`max_daily_loss_ratio=5%` 保持不变。触发后：

- 当日 flatten；
- 当日不再新增风险；
- 后续 CTP trading day 只有在 Broker ready、无 active order、已平仓、metadata、账户风险和启动对账均通过时才恢复 RUNNING。

`max_total_drawdown_ratio=30%`、`max_margin_ratio=35%`、`min_available_ratio=25%`、非正权益、metadata/对账异常仍是 hard/manual halt。

### Realized-gross hard guard

不再用固定 0.95 headroom 对所有正常目标预先 haircut。生产执行保持 signal target `<=2.0x`，然后用 Broker/行情真值持续检查实际 marked gross：

- actual gross `<=2.0x`：不干预；
- actual gross `>2.0x`：只提交 reduction-only FAK；
- 无法安全计算或执行：fail-closed；
- 目标 exactly 2.0x 不预先削减。

同一合约出现同时多仓和空仓时，flatten 按毛仓分别平仓，不能因 `net_volume=0` 把真实风险误判为 flat。

## 5. 最终 Production L3

### 可重复证据

```text
workflow run = 32617588179
PR head      = 4522abe69e66f6bfc5329ee54a66b6d0c4ee59e4
PR merge SHA = d112697f6da929a702f9b88869a41aed47b29e52
artifact id  = 9487448673
artifact     = production-return-l3-d112697f6da929a702f9b88869a41aed47b29e52
SHA-256      = a56a65593fd83d9addf6b542b67eaf986c11f8a1d8fc48902ae348df14606173
```

固定原始 specific-contract 数据 artifact id：`9473260618`。

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
daily loss limit            = 5%
total drawdown limit        = 30%
max contract volume         = 35
target gross cap            = 2.0x
realized gross hard ceiling = 2.0x
parameter_search            = false
margin_is_historical_truth  = false
```

每个报告窗口独立以 500,000 / flat 开始；signal/meta 始终来自同一冻结历史，不按窗口重新拟合。

### full_recent 结果

| 指标 | Base | Stress |
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
| actual gross 峰值 | **1.998253x** | 1.856519x |
| first divergence | `daily loss limit reached` | `combined margin ratio would exceed limit` |
| halted | **false** | **true** |

L3 的 Base gate 直接检查并通过：

```text
annualized_return >= 100%
max_drawdown <= 30%
max_realized_gross_notional_ratio <= 2.0x
halted == false
daily_circuit_days > 0
```

Base 的 4 次 daily circuit 说明策略不是通过删除日亏损门来恢复收益；78 个 defensive days 说明 completed-return governor 也确实发生了风险缩放。

## 6. 泛化与 regime 风险

Base 的分窗口结果并不均匀：

- train 年化约 **27.06%**；
- validation 年化约 **225.77%**；
- selection_full 年化约 **57.07%**；
- 已观察 OOS 年化约 **55.92%**；
- prior1 年化约 **-28.94%**，并触及 30% 总回撤硬门；
- prior2 年化约 **-28.80%**，并触及 30% 总回撤硬门。

这表明策略仍有明显 regime dependence。整个 `2024-08-21 ~ 2026-08-20` 也已经被用于多轮分析和生产机械收口，因此 108.8461% 不能当成未见样本的预期年化。

## 7. 为什么没有继续追更高历史数字

达到 Base acceptance 后继续在同一历史上搜索，会增加过拟合风险而不是增加真实信息。当前不再：

- 提高 leverage >2x；
- 放宽 5% daily-loss；
- 放宽 30% total DD；
- 放宽 35% margin / 25% cash reserve；
- 扩大 template pool；
- 围绕同一两年历史继续扫 governor/cap 参数。

Stress 失败也说明下一步的主要信息缺口已经不是“怎么把 Base 回测再抬高”，而是真实执行和保证金约束。

## 8. 仍不能由历史证明的内容

缺少多年历史完整：

- bid/ask/depth；
- queue position；
- partial fill / reject；
- CTP/交易所流控；
- 逐日真实 Broker margin；
- 真实结算手续费；
- reduction 完成后下一 cycle opening 的真实时间价格；
- gross guard 的真实成交时延和市场冲击。

因此真实资金仍必须经过多日 Shadow、测试柜台、极小真实仓位，以及最重要的——**新发生、此前没有参与任何选择和调参的未来数据**。

完整 production-mechanics 说明见 [`directional-production-mechanics-evidence.md`](directional-production-mechanics-evidence.md)。
