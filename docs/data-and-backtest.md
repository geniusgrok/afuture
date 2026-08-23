# 数据、回放与研究

## 1. 数据职责

### Calendar / Auto

生产与回放以带时区 Tick 为基础；Auto 还使用 limit、volume、open_interest、point-in-time catalog 和合约 expiry/listing。

### Directional

Directional 分离三类证据：

1. **continuous OHLC**：产品 signal 和已完成 intraday meta evidence；
2. **concrete-contract daily OHLC/OI/volume**：验证真实换月、next-open 收益和 production-mechanics；
3. **CTP L1 / account / fill**：未来 Shadow/实盘执行证据。

公开历史没有多年完整 bid/ask/depth/queue/partial/reject，因此日线研究和 production proxy 都不能替代真实执行。

## 2. 因果规则

正式 directional 研究/生产遵守：

- t 日未完成信息不能决定已经发生的 t 日收益；
- continuous roll jump 不计入可交易 Alpha；
- historical universe 只能包含当时已挂牌合约；
- t→t+1 收益来自 t 日已选择的**同一具体合约**；
- D+1 具体合约选择使用 D 的最终 OI/volume，不使用 D+1 尚未完成 activity；
- completed activity snapshot 不能落后于已确认完成的 signal trading day；
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

下一交易日：

- selector 读取 completed snapshot；
- listing/expiry 按计划交易日过滤；
- OI → volume → expiry → symbol 排序；
- 当前 tick 只负责 fresh quote、spread、depth、limit、价格和下单。

第一次启动没有 completed snapshot 时不新增 directional 风险；snapshot 落后于已确认完整 OHLC day 时 fail-closed。

## 4. Signal trading-day gate

`required_signal_day = completed_activity_snapshot.trading_day`。OHLC 最新日期必须覆盖 required day；`signal_max_age_hours` 只做第二层长时间停更门。

这避免周末小时容忍掩盖普通交易日漏 bar，也防止重启后的陈旧 activity snapshot 被继续使用。

## 5. 历史研究结论

### corrected M/OI calendar

```text
prior-forward             4 trades, -1.958R
final OOS                  2 trades, +0.296R
recent two years           5 trades, +1.028R
neighbor stability         0 / 16
2% risk proxy annualized   ≈ 1.07%
```

旧同品种 M/OI 研究仍未通过高收益门。

### Broad directional research

约 50 个成熟商品期货连续合约用于 family/候选发现。连续合约 close-to-close / roll 语义不能直接作为可执行 production 收益。

## 6. Float-notional specific-contract L4

固定原始数据：

```text
products                  = 50
candidate contract calls  = 3000
usable concrete contracts = 2540
specific daily rows       ≈ 495086
missing next returns      = 0 on final products
```

冻结策略：

- 96-template pool；
- breakout / tsmom / momentum / moving-average / reversal / acceleration；
- meta lookback=**11**；
- meta rebalance=**3**；
- active templates=3；
- meta score=`0.25 × annualized + 1.0 × Sharpe`；
- Base 5bp；Stress 15bp；Extreme 30bp；
- target gross ≤2x。

原官方 float artifact `2024-08-21 ~ 2026-08-20`：

| 指标 | Base 5bp | Stress 15bp |
|---|---:|---:|
| 年化收益 | **107.4623%** | **58.1372%** |
| 累计收益 | **306.1855%** | **141.1415%** |
| 最大回撤 | **27.4097%** | **32.9554%** |
| Sharpe | **1.6874** | **1.1525** |

此前 Final OOS 已被选择流程观察，所以：

```text
selection_bias_acknowledged = true
pristine_final_oos          = false
```

## 7. 当前 Production-mechanics L3

Production L3 使用当前正式 signal/meta，而不是另建研究路径，并加入：

1. previous-completed-day activity 选 next-day concrete contract；
2. prior lots 的 previous-close → current-open PnL；
3. reduction-first；
4. frozen multiplier；
5. integer lot floor；
6. `max_contract_volume=35`；
7. margin / available cash；
8. 5% daily-loss circuit；
9. 30% high-watermark total DD hard halt；
10. completed-return governor：-2% completed daily loss 或 3% two-day sample vol → 25%，否则 100%；
11. target gross `<=2x`；
12. actual realized gross `>2x` 后 reduction-only hard guard；
13. gross guard reduction cost 计入账户；
14. 每个报告窗口独立从 `500000` / flat 开始。

历史 Broker 每日真实 margin schedule 不存在，因此：

```text
Base margin proxy   = 12% × 1.25 buffer
Stress margin proxy = 15% × 1.25 buffer
max margin ratio    = 35%
min available       = 25%
daily loss          = 5%
total drawdown      = 30%
```

### 最近两年最终结果

| 指标 | Base | Stress |
|---|---:|---:|
| 年化 | **108.8461%** | **0.9249%** |
| 累计 | **311.4052%** | **1.7840%** |
| 最大回撤 | **17.8010%** | **5.8553%** |
| Sharpe | **2.0812** | 0.2246 |
| 活跃交易日 | **478 / 484** | **14 / 484** |
| 最终权益 | **2,057,025.78** | 508,919.91 |
| daily circuit days | 4 | 0 |
| defensive days | 78 | 2 |
| margin reject days | 0 | 6 |
| actual gross peak | **1.998253x** | 1.856519x |
| halted | **false** | **true** |

Base 的固定 L3 同时通过：年化 `>=100%`、最大回撤 `<=30%`、actual gross `<=2x`、不永久 HALT。

Stress 没通过：更高成本/保证金代理下很早被 margin hard gate 终止。较小的 Stress 最大回撤主要来自早停机，不能解释为“更稳”。

最终证据：run `32617588179`，artifact id `9487448673`。

详细证据见 [`directional-production-mechanics-evidence.md`](directional-production-mechanics-evidence.md)。

## 8. 结果不能怎样解释

Float Base 107.4623% 与 Production Base 108.8461% 都来自已经反复观察的历史。Production 的较高历史收益不能解释为“真实机械天然提高 Alpha”；它来自 integer lots、daily circuit、governor、gross guard 和账户路径共同作用，并且这些机械本身也是在同一历史上收口的。

同样，Stress 0.9249% + halt 不能被隐藏。当前最准确结论是：**Base historical production acceptance passes；Stress robustness does not。**

## 9. Proxy 仍非精确 CTP 历史重放

仍缺：

- 历史完整 bid/ask/depth；
- queue position；
- partial fill / reject；
- 真实订单流控；
- 逐日真实 Broker margin；
- 实际结算手续费；
- reduction 成交后下一 cycle opening 的精确分钟/秒价格；
- gross guard 的真实延迟与冲击成本。

日线 proxy 只能用同日 open/close 近似阶段执行，所以真正的可兑现 Alpha 最终仍需 directional quality + Shadow + 测试柜台 + 小资金回答。

## 10. 验证层级

```text
L1  activity/signal/lot/risk/quality 因果单测
L2  manager/engine/restart smoke
L3  frozen production-mechanics economic gate
L4  specific-contract roll-safe research evidence
Final Python 3.10/3.13 CI + review
```

达到固定 Base acceptance 后停止在同一历史上扩大搜索。后续最有信息价值的是：

1. 新发生、此前未参与选择的交易日；
2. 多交易日 CTP Shadow；
3. planned vs realized turnover/slippage/commission/tracking；
4. 实际 margin / gross guard / daily circuit；
5. 测试柜台 FAK/partial/reject/reconnect；
6. 极小真实仓位；
7. 若可获得，可靠历史 L1。
