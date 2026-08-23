# 实盘、Shadow、停机与恢复

## 1. 正式模式与证据边界

`afuture` 支持两种账户互斥模式：Calendar Spread / Auto 与 Execution-Aligned Directional。两者共用 Broker、`RiskManager`、Kill Switch、`REDUCE_ONLY`、StateStore、启动对账和审计链。

Directional 的历史证据必须分层：

- float-notional L4：selection-biased Base 年化 **107.4623%**；
- 当前 production-mechanics L3：Base 年化 **108.8461%**、最大回撤 **17.8010%**、actual gross 峰值 **1.998253x**、未永久 HALT；
- Stress：年化仅 **0.9249%**，因 margin hard gate HALT。

所以代码级 Base 历史门已经达到 100%，但**真实资金仍未因此自动获批**。历史已被反复观察，Stress 也明显失败；真正的下一道门是 Shadow / 测试柜台 / 极小资金 / 新发生未来数据。

## 2. 推荐上线顺序

```text
固定历史 L3/L4
→ 多交易日 CTP Shadow
→ CTP doctor
→ 测试柜台 FAK/partial/reject/reconnect/gross guard
→ 极小真实仓位
→ execution-quality / 结算单核对
→ 新发生未来数据
→ 再决定是否扩大风险
```

不再围绕同一两年历史继续追更高 Base 数字。

## 3. 凭证

CTP 凭证只从环境变量读取：

```text
AFUTURE_CTP_USER
AFUTURE_CTP_PASSWORD
AFUTURE_CTP_BROKER
AFUTURE_CTP_APP_ID
AFUTURE_CTP_AUTH_CODE
```

真实生产还要求：

```text
AFUTURE_LIVE_ACK=I_UNDERSTAND_FUTURES_RISK
```

以及 `--confirm-live`。

## 4. Directional 冻结配置

使用 `config/afuture.directional-live.example.toml` 作为 test/Shadow 起点：

- 冻结 50 品种 / 96 templates；
- meta lookback 11 / rebalance 3 / active 3；
- gross target 上限 2.0x；
- directional 单合约上限 35 手；
- 20 天 expiry filter；
- `20:55-09:10` 跨午夜 rebalance window；
- max margin 35%；
- min available 25%；
- daily loss 5%；
- total drawdown 30%；
- fresh quote / depth / limit / order-rate 硬门。

2.0x 是 signal target hard cap，同时还有独立的 actual-gross hard guard；不是承诺账户会持续保持 2.0x 风险。

## 5. Previous-day activity snapshot

生产不在开盘后用当前交易日累计 OI/volume 重新挑主力。

`DirectionalActivityTracker` 按 CTP `Tick.trading_day` 记录每个允许合约最后可见 activity。trading day 从 D 推进时：

```text
D 日最终 volume/OI
→ DirectionalActivitySnapshot
→ 原子保存 directional_activity.json
→ D+1 选约只读 completed snapshot
```

D+1 当前 Tick 仍用于 fresh quote、bid/ask、depth、limit、价格和下单，但不能改变 D 已冻结主力。

新部署无 completed snapshot 时不新增风险；重启 snapshot 若落后于已确认完整 OHLC day，也 fail-closed。

## 6. Signal-day freshness

```text
required_signal_day = completed activity day
continuous OHLC latest day >= required_signal_day
```

`signal_max_age_hours` 只做第二层长期停更/未来 timestamp 保护。

- provider 临时失败但缓存已覆盖 required day：允许缓存；
- required day 缺失且账户为空：拒绝新增；
- required day 缺失且已有 risk：`risk_off → REDUCE_ONLY → flatten`；
- completed activity 明显落后：fail-closed。

## 7. Completed-return governor

Directional engine 只保存**已完成交易日账户收益**：

```text
latest completed daily return <= -2%
OR two-day sample volatility >= 3%
→ next target scale = 25%
else
→ 100%
```

当前交易日 PnL 不参与当前目标。Governor 只能降风险，不能放大冻结 signal。

## 8. Realized-gross hard guard

正式实现不对所有正常目标预先乘固定 headroom：

1. signal target 必须 `<=2.0x`；
2. manager 读取 Broker 真实仓位、实时 quote、contract multiplier 和 account equity；
3. 每个 tick 检查 marked gross；
4. actual gross `>2.0x` 时只发送 reduction-only FAK；
5. active reduction order 未结算时不重复发送；
6. 无法安全计算/执行且仍有风险时进入 fail-closed `REDUCE_ONLY`。

Shadow/test 必须专门验证 gross guard 的触发频率、成交延迟、冲击和 guard 后实际 gross。

## 9. 合约不可用与 reduction-first

某个新目标产品没有 eligible contract/fresh quote 时不整体阻塞组合减仓：

1. 读取 Broker positions；
2. 计算目标；
3. target=0、反转、超额风险等 reductions 先执行；
4. 缺失新目标只禁止对应产品新增/换月；
5. reductions 经 Broker 确认后的下一 cycle 才允许 openings。

同一合约如果同时存在多空毛仓，flatten 按 long/short 毛仓分别生成平仓单，不能因为净仓为 0 判断“已经 flat”。

## 10. Shadow

```bash
afuture shadow --config config/afuture.directional-live.example.toml --duration-seconds 3600
```

Directional Shadow 使用真实 CTP catalog/tick/trading day/metadata 和正式 signal/activity/risk 逻辑；账户/订单/成交/持仓由本地 SimBroker 维护，不调用真实 CTP `send_order()`。

### Shadow 必须重点回答

- target gross vs actual gross；
- gross guard 触发及 reduction 后 actual gross；
- target lots vs actual lots；
- margin ratio / available ratio；
- daily loss / high-watermark drawdown；
- completed-return governor scale；
- daily circuit 与次日恢复；
- planned vs realized turnover；
- median/p95 slippage；
- commission；
- partial/reject；
- 主力切换是否与 previous-day activity 一致。

Stress 历史在 margin proxy 下很早 HALT，因此真实 margin 是 Shadow/test 阶段的最高优先级变量之一。

## 11. Doctor

```bash
afuture doctor --config config/afuture.directional-live.example.toml
```

Doctor 只检查登录、account/position snapshot、catalog、multiplier、price tick、margin、commission metadata，不包含真实报单入口。

## 12. Daily circuit 与 hard halt

5% daily-loss 是同 trading-day circuit：

```text
RUNNING
→ daily-loss breach
→ REDUCE_ONLY / flatten
→ 当日不重新承担风险
→ 后续 CTP trading day 安全检查
→ RUNNING
```

恢复必须满足 Broker ready、无 active order、无残余 directional risk、metadata verified、账户 hard gates 通过、startup reconciliation 通过。

以下仍是 hard/manual halt：

- total drawdown；
- margin ratio；
- available cash；
- nonpositive equity；
- metadata / reconciliation / infrastructure failure。

不能把这些错误当作 daily circuit 自动清除。

## 13. 启动对账

Directional 不持久化第二份策略仓位。重启：

1. Broker ready；
2. fresh account / complete positions；
3. 处理 active orders；
4. `RuntimeState.positions` 与 Broker 完整持仓逐合约比较；
5. metadata/account gates；
6. 完全一致才恢复。

任何 mismatch 都 fail-closed。

## 14. Execution quality

下单时 manager 只注册 expected metadata；真实 callback 到达后：

- 基础 TradingEngine 先更新统一 position truth；
- quality 再记录 realized fill/slippage/commission；
- cycle 汇总 realized turnover、tracking error、latency、partial/reject。

持续检查 `quality-report.directional`。

## 15. Production L3 的边界

最终 Base 历史 L3：108.8461% / 17.8010% DD / 1.998253x actual gross / no permanent halt。

仍缺多年历史真实：

- L1 bid/ask/depth/queue；
- partial/reject；
- CTP/交易所流控；
- 逐日 Broker margin schedule；
- 真实结算手续费；
- reduction 后下一 cycle opening 的分钟/秒价格；
- gross guard 的真实成交时延与市场冲击。

因此 **108.8461% 不是未来真实收益预测**；Stress 的 0.9249% + margin halt 也说明执行与保证金鲁棒性仍未证明。

## 16. 风险参数调整原则

不要因为历史 Base 已超过 100% 就扩大 leverage，也不要因为 Stress 失败就直接放宽 margin/cash/drawdown。任何阈值调整必须来自：

- 实际账户风险承受能力；
- 多日 Shadow；
- 测试柜台真实 margin/fee/fill；
- 极小资金 realized drawdown；
- 新发生未见数据。

目标是提高**可兑现净收益/风险比**，不是继续拟合同一历史。

## 17. 测试柜台必须验证

- FAK 开/平；
- partial / reject；
- 平今/平昨；
- 夜盘跨 trading day activity freeze；
- 断线重连；
- metadata/query/order rate；
- 多合约 reduction-first；
- realized gross guard；
- daily circuit 次日恢复；
- signal/activity risk-off → REDUCE_ONLY；
- hedged gross-position flatten；
- restart reconcile；
- 实际手续费和 margin。

这些通过后再进入极小真实仓位。
