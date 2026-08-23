# 数据、回放与研究

## 1. 数据职责

### Calendar / Auto

生产与回放以带时区 Tick 为基础；Auto 还使用 limit、volume、open_interest、point-in-time catalog 和合约 expiry/listing。

### Directional

Directional 分离三类证据：

1. **continuous OHLC**：产品 signal 和已完成 intraday meta evidence；
2. **concrete-contract daily OHLC/OI/volume**：验证真实换月、next-open 收益和 production-mechanics；
3. **CTP L1 / account / fill / live ContractSpec**：未来 Shadow/实盘执行和真实 margin 证据。

公开历史没有多年完整 bid/ask/depth/queue/partial/reject，也没有逐日 Broker margin schedule，因此日线研究和 production proxy 都不能替代真实执行。

## 2. 因果规则

正式 directional 研究/生产遵守：

- t 日未完成信息不能决定已经发生的 t 日收益；
- continuous roll jump 不计入可交易 Alpha；
- historical universe 只能包含当时已挂牌合约；
- t→t+1 收益来自 t 日已选择的**同一具体合约**；
- D+1 具体合约选择使用 D 的最终 OI/volume，不使用 D+1 尚未完成 activity；
- completed activity snapshot 不能落后于已确认完成的 signal trading day；
- meta 的 Base/Stress cost evidence 均只读取已完成历史；
- completed-return governor 只读取已完成账户交易日收益；
- Final OOS 被任何选择过程观察过后必须标记 non-pristine。

Float L4 执行分解：

```text
截至 t 收盘历史 → t+1 target weights
old weights × (t close → t+1 open)
+ new weights × (t+1 open → close)
- t+1 open turnover cost
```

## 3. Previous-day activity evidence

生产 `DirectionalActivityTracker` 按 `Tick.trading_day` 聚合合约最后可见 volume/OI；只有 trading day 从 D 推进时，才冻结 D 为 `DirectionalActivitySnapshot`。

下一交易日 selector 读取 completed snapshot；listing/expiry 按计划交易日过滤；默认排序仍为 OI → volume → expiry → symbol；若已有持仓合约仍 eligible，则只有 challenger 在 completed-day OI 与 volume 两维都严格更高时才切换。当前 tick 只负责 fresh quote、depth、limit、价格、margin sizing 和下单。

第一次启动没有 completed snapshot 时不新增 directional 风险；snapshot 落后于已确认完整 OHLC day 时 fail-closed。

## 4. Signal trading-day gate

`required_signal_day = completed_activity_snapshot.trading_day`。OHLC 最新日期必须覆盖 required day；`signal_max_age_hours` 只做第二层长时间停更门。

## 5. Float-notional specific-contract L4

冻结策略：

- 50 products；
- 96-template pool；
- breakout / tsmom / momentum / moving-average / reversal / acceleration；
- meta lookback=**11**；
- meta rebalance=**3**；
- active templates=3；
- Base score=`0.25 × annualized + 1.0 × Sharpe`；
- target gross ≤2x。

原官方 float artifact `2024-08-21 ~ 2026-08-20`：

| 指标 | Base 5bp | Stress 15bp |
|---|---:|---:|
| 年化收益 | **107.4623%** | **58.1372%** |
| 累计收益 | **306.1855%** | **141.1415%** |
| 最大回撤 | **27.4097%** | **32.9554%** |
| Sharpe | **1.6874** | **1.1525** |

此前 Final OOS 已被选择流程观察，因此 `selection_bias_acknowledged=true`、`pristine_final_oos=false`。

## 6. 当前 production meta

当前 `ExecutionAlignedAggressivePolicy` 没有扩大模板池，也没有只保留慢 rebalance 模板。

每个模板分别生成已完成 continuous open→close 的：

- Base 5bp stream；
- Stress 15bp stream。

Trailing Base 和 Stress evidence 都必须为正；通过该 Stress 生存门后按 Base score 排名。这一设计用于淘汰连 15bp 成本都无法存活的模板，但避免 5bp/15bp 50/50 score 直接替代 Base Alpha 目标。

本轮固定历史曾验证并拒绝：

- 5bp/15bp score 50/50：Production Base 年化降到约 94.59%；
- 强制只允许 template rebalance `>=5`：Base 年化降到约 69.78%。

最终版本恢复全部 96 templates，Base 经济结果恢复到 109.0636%。

## 7. Production-mechanics L3

Production L3 使用当前正式 signal/meta，并加入：

1. previous-completed-day activity 选 next-day concrete contract；
2. prior lots previous-close → current-open PnL；
3. reduction-first；
4. frozen multiplier；
5. integer lot floor；
6. `max_contract_volume=35`；
7. margin-aware target sizing；
8. margin / available hard gates；
9. 5% daily-loss circuit；
10. 30% high-watermark total DD hard halt；
11. completed-return governor：-2% completed daily loss 或 3% two-day sample vol → 25%；
12. signal target gross `<=2x`；
13. actual realized gross `>2x` 后 reduction-only hard guard；
14. gross guard reduction cost 计入账户；
15. 每个报告窗口独立从 `500000` / flat 开始。

### Margin proxy 与 soft target

历史 Broker 每日真实 margin schedule 不存在，因此：

```text
Base margin proxy   = 12% × 1.25 buffer
Stress margin proxy = 15% × 1.25 buffer
max margin ratio    = 35% hard gate
min available       = 25% hard gate
daily loss          = 5%
soft target margin  = min(35%, 75%) - 5% = 30% equity
total drawdown      = 30%
```

Soft target 30% 只用于正常 target lot fitting；账户 hard margin 仍为 35%。Live 不使用统一 12%/15% proxy，而使用 Broker side-specific margin metadata。

### 最近两年最终结果

| 指标 | Base | Stress |
|---|---:|---:|
| 年化 | **109.0636%** | **28.9559%** |
| 累计 | **312.2285%** | **62.9735%** |
| 最大回撤 | **15.8529%** | **28.1152%** |
| Sharpe | **2.0976** | **0.9604** |
| 活跃交易日 | **478 / 484** | **474 / 484** |
| 最终权益 | **2,061,142.43** | **814,867.56** |
| daily circuit days | 3 | 2 |
| defensive days | 77 | 70 |
| margin reject days | 0 | **0** |
| actual gross peak | **1.998253x** | **1.668769x** |
| halted | **false** | **false** |

最终证据：run `32634296589`，artifact id `9491959916`，SHA-256 `e531f2874cbc26c3a54ff561e074f59b86ac887a1f509e33effd6908eaa3144d`。

上一 production Stress 为 0.9249% / 14 active days / margin HALT；因此当前 28.9559% 的主要意义是**Stress 已成为完整运行的账户实验**，而不是早停后的摊薄年化。但它仍没有达到 80%。

## 8. 结果不能怎样解释

Float Base 107.4623%、Production Base 109.0636%、Production Stress 28.9559% 都来自已观察历史。

不能据此声称：

- 实盘未来 Base 必然年化 >100%；
- Stress 28.9559% 是未来收益下限；
- 当前 margin proxy 等于历史真实 Broker margin；
- 继续在相同历史上调参到 Stress 80% 会提高真实泛化能力。

当前准确结论是：**Base historical production acceptance passes；Stress mechanical survivability now passes；Stress 80% return target does not。**

## 9. Proxy 仍非精确 CTP 历史重放

仍缺历史完整 bid/ask/depth、queue position、partial fill/reject、真实订单流控、逐日 Broker margin、实际结算手续费和真实 market impact。

日线 proxy 只能近似阶段执行，所以真正的可兑现 Alpha 最终仍需 directional quality + Shadow + 测试柜台 + 小资金回答。

## 10. 验证层级

```text
L1  activity/signal/lot/risk/quality 因果单测
L2  manager/engine/restart smoke
L3  frozen production-mechanics economic gate
L4  specific-contract roll-safe research evidence
Final Python 3.10/3.13 CI + review
```

最终 L3 稳定后停止在同一历史上扩大搜索。后续最有信息价值的是：新发生交易日、多日 CTP Shadow、planned vs realized turnover/slippage/commission、实际 Broker margin、测试柜台 FAK/partial/reject/reconnect 和极小真实仓位。


## 11. Execution-efficiency 负证据与最终晋级

固定输入 `9473260618` 上，本轮最终晋级候选把 Production Stress 从 **20.4057%** 提升到 **28.9559%**，Base 从 **108.8461%** 提升到 **109.0636%**。晋级来源是执行机械而不是新模板搜索：roll hysteresis、one-lot increase no-trade、adaptive margin contraction 与 turnover attribution。

三类更激进的 signal 层换手抑制被固定 L3 否决并回退：product replacement persistence（Base 约 58.41%）、cost-aware meta hysteresis（Stress -13.03% 且 HALT）、same-direction weight resize hysteresis（未通过 promotion gate）。这说明 entry/exit turnover 中包含重要 Alpha；不能按“换手越低越好”继续拟合。
