# 架构与数据流

## 1. 总体原则

`afuture` 支持两种**账户互斥**的正式模式：

- `calendar / auto`：同品种相邻月份跨期套利；
- `directional`：冻结的 50 品种 execution-aligned 方向组合。

两种模式共用唯一的 Broker/TradingEngine 账户真相、`RiskManager`、Kill Switch、`REDUCE_ONLY`、StateStore、Shadow 和审计链。Directional 不创建第二套账户、订单、成交或持仓状态机。

## 2. Calendar / Auto

```text
CTP Catalog / Tick
→ AutoPairManager
→ activity / sync / stationarity / half-life / Net Edge
→ CalendarSpreadStrategy
→ PortfolioRisk + RiskManager
→ PairExecutor
→ Broker
```

Auto 只负责候选和开仓资格；已有仓位失去候选资格后继续 managed，但不再 open-eligible，直至退出。

## 3. Directional

```text
连续 OHLC
→ ExecutionAlignedAggressivePolicy
   96-template / causal meta
→ 冻结产品权重，target gross <=2x
→ completed-return governor（只可缩风险）

CTP Tick.trading_day
→ DirectionalActivityTracker
→ trading day 切换时冻结前一日最终 OI/volume
→ DirectionalActivityStore
→ D+1 concrete contract selection

Broker positions + D+1 fresh quotes
→ integer target lots，单合约 <=35
→ reduction-first rebalance
→ RiskManager
→ FAK
→ Broker
→ 每 tick realized-gross guard（actual gross >2x 只减仓）
```

### 冻结经济参数

- Universe：50 品种，代码字母序；
- template pool：96；
- family：breakout / tsmom / momentum / moving-average / reversal / acceleration；
- meta lookback = **11**；
- meta rebalance = **3**；
- active templates = **3**；
- meta score = `0.25 × annualized + 1.0 × Sharpe`，只使用已完成 continuous `open→close` evidence；
- gross target ≤2.0x；
- max contract volume = 35。

`execution_aligned_policy.py` 是唯一正式 signal/meta policy；`directional.py` 保留配置、合约/手数/rebalance/gross-reduction 原语。

## 4. 因果时间边界

Directional 同时冻结：

```text
完整交易日 D 的 OHLC
→ D+1 产品目标

完整交易日 D 的具体合约最终 OI/volume
→ D+1 具体合约
```

D+1 尚未完成的 volume/OI 不能改变 D 已冻结选约。持久化 activity snapshot 比已确认完成的 signal day 更旧时 fail-closed。

completed-return governor 也遵守同一因果边界：当前 session PnL 不参与当前目标，只保存交易日结束后的账户日收益供下一目标使用。

## 5. Signal freshness

```text
required_signal_day = completed_activity_snapshot.trading_day
latest OHLC day >= required_signal_day
```

然后才使用 `signal_max_age_hours` 处理未来 timestamp / 长时间停更。

- provider 失败但 cache 已覆盖 required day：可继续；
- required day 缺失且已有 risk：`risk_off → REDUCE_ONLY`；
- required day 缺失且账户为空：拒绝新增；
- activity snapshot stale：fail-closed；
- 新启动无 completed snapshot：禁止新增风险。

## 6. Reduction-first 与毛仓 flatten

```text
Broker 当前持仓
→ target=0 / 反转 / 超额 / 换月 reductions
→ reducing FAK
→ Broker 回报
→ 下一 cycle 重读真实持仓
→ 无 reductions 才允许 openings
```

新目标暂无 eligible contract/fresh quote 时，不新增、不换月；其它确定性 reductions 继续。

异常情况下同一合约可能同时有多仓和空仓。安全 flatten 必须按 `long_total` / `short_total` 毛仓分别平仓，不能因 `net_volume=0` 误判为 flat。

## 7. 两层风险收缩

### Completed-return governor

```text
latest completed daily return <= -2%
OR two-day sample volatility >= 3%
→ next target = 25%
else
→ next target = 100%
```

它永远不能增加原冻结目标。

### Realized gross guard

不对正常目标预先乘固定 headroom。目标本身仍必须 `<=2x`；实际 Broker/mark gross 超过 `2x` 时，manager 只生成 reduction-only FAK。无法安全分类/计算/执行时 fail-closed。

因此 target leverage 与 actual marked leverage 是两个不同层级：前者由 signal gate 约束，后者由运行时 hard guard 约束。

## 8. Risk state 与 daily circuit

统一状态机不变。Directional 的 5% daily-loss 是同交易日 circuit：

```text
RUNNING
→ daily-loss breach
→ REDUCE_ONLY / flatten
→ 当日保持 HALTED/circuit marker
→ 后续 CTP trading day 完成安全检查
→ RUNNING
```

恢复要求 Broker ready、无 active order、无 residual directional risk、metadata verified、账户风险通过、启动对账通过。

总回撤、margin、available cash、非正 equity、metadata/对账/基础设施异常仍是 hard/manual halt，不能走 daily-circuit 自动恢复。

## 9. Broker/StateStore 真相与重启

Directional trade callback 先由基础 TradingEngine 更新统一 expected positions；quality callback 只观察。

重启时：

```text
RuntimeState.positions
↔ Broker complete position snapshot
```

逐合约今昨、多空完全一致才 reconciled；任何 mismatch fail-closed。

## 10. Execution quality

同一 `ExecutionQualityRecorder` 记录：

- pair：candidate / decision / round-trip；
- directional：rebalance / fill / cycle。

Directional 汇总 realized turnover、commission、median/p95 slippage、tracking error、completion latency、partial/rejected count。真实 fill 只来自 Broker `Trade` callback。

## 11. 经济证据分层

1. **Float-notional specific-contract L4**：Base 5bp 年化 107.4623%、最大回撤 27.4097%，selection-biased；
2. **Production-mechanics L3**：当前生产机械 Base 5bp 年化 **108.8461%**、最大回撤 **17.8010%**、actual gross peak **1.998253x**、未永久 HALT；Stress 年化 **0.9249%** 且因 margin hard gate HALT。

Base production acceptance 已通过，但 Stress 失败和历史选择偏差仍然存在。两个层级都不能替代真实 CTP 新数据。

历史逐日 Broker margin 不可得，因此 Base/Stress 仍是显式 12%/15% margin proxy × 1.25 buffer，不声称是柜台历史真值。

详细证据：[`directional-production-mechanics-evidence.md`](directional-production-mechanics-evidence.md)。

## 12. Shadow 与后续边界

Shadow 市场侧来自真实 CTP catalog/tick/trading day/metadata，账户侧来自本地 SimBroker。必须重点观察：actual gross、gross-guard reductions、margin、daily circuit、realized cost、tracking、partial/reject 和恢复行为。

当前不需要数据库、消息队列、Web 服务、微服务或第二账户状态机。后续新增价值应来自**未来新数据和真实执行证据**，而不是继续扩大同一历史上的参数空间。
