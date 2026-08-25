# 实盘、Shadow、停机与恢复

## 1. 正式模式与证据边界

`afuture` 支持两种账户互斥模式：Calendar Spread / Auto 与 Execution-Aligned Directional。两者共用 Broker、`RiskManager`、Kill Switch、`REDUCE_ONLY`、StateStore、启动对账和审计链。

Directional 必须区分 live wiring 与离线 checkpoint：PR #25 Stress-90 的 Base 156.881655% / Stress 112.100053% 是固定输入 production-research proxy，全部矩阵行无 HALT、0 margin rejects，但**没有接入 live runtime**。Live 仍使用本文件描述的 Execution-Aligned Directional runtime、Broker/RiskManager 权限和账户 hard gates。历史结果不能作为真实资金收益承诺，也不能跳过本运行手册。

## 2. 推荐上线顺序

```text
固定历史 L3/L4
→ 多交易日 CTP Shadow
→ CTP doctor
→ 测试柜台 FAK/partial/reject/reconnect/margin sizing/gross guard
→ 极小真实仓位
→ execution-quality / 结算单核对
→ 新发生未来数据
→ 再决定是否扩大风险
```

不再围绕同一两年历史继续追更高收益数字。

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

## 3.1 本地状态与 CTP 预检

先运行完全本地、只读的状态检查：

```bash
afuture status --config config/afuture.directional-live.example.toml
```

它不会初始化日志、连接 CTP 或修改文件，也不要求注入 CTP 用户名、密码和 Broker ID。报告包含当前 checksum state、显式 `state.json.prev`、Kill Switch/runtime mode、持仓摘要、audit/alert 大小、路径可写性和最少 100 MiB 磁盘余量。当前 state 不可信时返回 2；`.prev` 只用于人工诊断，不能自动恢复或绕过 `recover-state`。

设置 `AFUTURE_LIVE_ACK` 后运行无报单 CTP gate：

```bash
afuture doctor --config config/afuture.directional-live.example.toml --confirm-live
```

`doctor` 等待新的账户事件和完整持仓快照，随后检查账户数值、broker/account trading day、margin/available/daily-loss/drawdown 限制、活动委托、catalog、抽样 live metadata、本地期望持仓对账、Kill Switch、runtime mode、上次持久化安全门和 Directional completed activity。Directional activity 复用正式选约的 product/exchange、expiry、volume/OI 门；activity 的 symbol、product、exchange 必须与 catalog 同一条 identity 完全一致，且配置内每个 product 都必须有合格 catalog/activity 覆盖。任一无关、身份冲突或低流动性快照都不能冒充 ready。JSON 中任一 `checks[].passed=false` 都使进程返回 2；报告中的 `orders_sent` 固定为 0。Directional 新部署必须先观察一个完整 trading day 生成 activity evidence，预检才会通过。

## 4. Directional 冻结配置

使用 `config/afuture.directional-live.example.toml` 作为 test/Shadow 起点：

- 冻结 50 品种 / 96 templates；
- meta lookback 11 / rebalance 3 / active 3；
- meta：5bp Base 与 15bp Stress evidence 都存活后按 Base score 排名；
- gross target 上限 2.0x；
- directional 单合约上限 35 手；
- 20 天 expiry filter；
- `20:55-09:10` 跨午夜 rebalance window；
- max margin 35%；
- min available 25%；
- daily loss 5%；
- total drawdown 30%；
- fresh quote / depth / limit / order-rate 硬门。

## 5. Margin-aware target sizing

Signal gross 仍可到 2.0x，但目标手数在开仓前先使用 Broker live `ContractSpec` 做 margin feasibility sizing：

```text
hard_share = min(max_margin_ratio, 1 - min_available_ratio)
conservative = max(0, hard_share - max_daily_loss_ratio)
shock = clamp(max(3%, abs(latest completed return), two-day sample volatility), 3%, 5%)
soft_target_share = min(conservative, conservative * (1 - max(0, shock - 3%)))
```

当前配置的无历史/平静基线为 `35% - 5% = 30% equity`；completed shock 高于 3% 时 soft target 进一步收缩。

这不是把 hard gate 改成 30%。语义是：

1. 正常 target 不主动贴着 35% hard margin boundary；
2. 多头使用 `margin_rate_long`，空头使用 `margin_rate_short`；
3. 结合当前 mid、multiplier、`margin_estimate_buffer=1.25` 计算逐手 margin；
4. integer fitter 只向下缩手数，缺少可信 margin evidence 时 fail-closed；
5. 生成 openings 后，原 `RiskManager.check_open_orders()` 仍重新检查 35% max margin 和 25% min available，并拥有最终否决权。
6. margin fitting 后若只是同方向 `+1 lot` 增仓，且当前持仓本身仍在 soft margin 与 2x gross 内，可保持 incumbent lot；减仓、反转、换月、daily circuit 与 gross guard 不受该 no-trade 规则抑制。

Shadow/test 必须对比 modeled target margin 与 Broker 实际冻结保证金；真实逐品种/逐日 margin 与历史 12%/15% proxy 不同是预期情况。

## 6. Previous-day activity snapshot

生产不在开盘后用当前交易日累计 OI/volume 重新挑主力。

`DirectionalActivityTracker` 按 CTP `Tick.trading_day` 记录每个允许合约最后可见 activity。trading day 从 D 推进时：

```text
D 日最终 volume/OI
→ DirectionalActivitySnapshot
→ 原子保存 directional_activity.json
→ D+1 选约只读 completed snapshot
```

D+1 当前 Tick 仍用于 fresh quote、bid/ask、depth、limit、价格、margin sizing 和下单，但不能改变 D 已冻结 activity evidence。已有持仓合约若仍 eligible，会继续作为 incumbent；只有另一个合约在 D 日 completed OI **和** volume 两项都严格更高时才换月，expiry/listing/activity 失效则立即按确定性排名切换。

新部署无 completed snapshot 时不新增风险；重启 snapshot 若落后于已确认完整 OHLC day，也 fail-closed。

## 7. Signal-day freshness 与 meta

```text
required_signal_day = completed activity day
continuous OHLC latest day >= required_signal_day
```

`signal_max_age_hours` 只做第二层长期停更/未来 timestamp 保护。

Meta 只使用已完成 continuous open→close evidence：模板必须同时在 5bp Base 和 15bp Stress 成本端点存活，然后按 Base score 排名。Stress 是 robustness filter，不是用已观察历史做 50/50 收益最大化。

## 8. Completed-return governor

Directional engine 只保存**已完成交易日账户收益**：

```text
latest completed daily return <= -2%
OR two-day sample volatility >= 3%
→ next target scale = 25%
else
→ 100%
```

当前交易日 PnL 不参与当前目标。Governor 只能降风险。

## 9. Realized-gross hard guard

1. signal target 必须 `<=2.0x`；
2. manager 读取 Broker 真实仓位、实时 quote、contract multiplier 和 account equity；
3. 每个 tick 检查 marked gross；
4. actual gross `>2.0x` 时只发送 reduction-only FAK；
5. active reduction order 未结算时不重复发送；
6. 无法安全计算/执行且仍有风险时进入 fail-closed `REDUCE_ONLY`。

Margin sizing 与 gross guard 不能互相替代：一个约束保证金需求，一个约束实际名义风险。

## 10. 合约不可用与 reduction-first

某个新目标产品没有 eligible contract/fresh quote 时不整体阻塞组合减仓：

1. 读取 Broker positions；
2. 计算目标；
3. target=0、反转、超额风险等 reductions 先执行；
4. 缺失新目标只禁止对应产品新增/换月；
5. reductions 经 Broker 确认后的下一 cycle 才允许 openings。
6. 正常 opening 只有在当前对手一档显示深度覆盖整笔 requested volume 时，FAK limit 使用 best opposite quote；否则保持原 aggressive tick。该规则不用于 reduction。

同一合约如果同时存在多空毛仓，flatten 按 long/short 毛仓分别生成平仓单，不能因为净仓为 0 判断“已经 flat”。

## 11. Shadow

```bash
afuture shadow --config config/afuture.directional-live.example.toml --duration-seconds 3600
```

Directional Shadow 使用真实 CTP catalog/tick/trading day/metadata 和正式 signal/activity/risk 逻辑；账户/订单/成交/持仓由本地 SimBroker 维护，不调用真实 CTP `send_order()`。

必须重点记录：

- signal gross / margin-fitted target / actual gross；
- modeled per-lot margin vs Broker metadata；
- margin sizing 缩手事件及剩余 headroom；
- gross guard 触发及 reduction 后 actual gross；
- target lots vs actual lots；
- margin ratio / available ratio；
- daily loss / high-watermark drawdown；
- completed-return governor scale；
- daily circuit 与次日恢复；
- planned vs realized turnover；
- median/p95 slippage、commission、partial/reject；
- 主力切换是否与 previous-day activity 一致。

## 12. Doctor

```bash
afuture doctor --config config/afuture.directional-live.example.toml --confirm-live
```

Doctor 只检查登录、account/position snapshot、catalog、multiplier、price tick、margin、commission metadata，不包含真实报单入口。

## 13. Daily circuit 与 hard halt

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

以下仍是 hard/manual halt：total drawdown、margin ratio、available cash、nonpositive equity、metadata/reconciliation/infrastructure failure。

## 14. 启动对账与 execution quality

Directional 不持久化第二份策略仓位。重启必须以 Broker 完整 account/positions 为真相并与 StateStore 对账；任何 mismatch 都 fail-closed。

`ExecutionQualityRecorder` 的 directional 证据包括：

- rebalance：signal/activity day、target、planned turnover；
- fill：Broker callback 的 fill/slippage/commission；
- cycle：realized turnover、tracking error、latency、partial/reject。

持续检查 `quality-report.directional`。

## 15. Production research 的边界

当前固定历史 checkpoint：

```text
Base full_recent    156.881655% annualized / 15.708467% DD / no HALT
Stress full_recent  112.100053% annualized / 14.567214% DD / no HALT
```

Stress-90 已通过冻结 promotion gate，但仍是离线 proxy，不是未来收益下限，也不是 live activation。

仍缺多年历史真实 L1 bid/ask/depth/queue、partial/reject、CTP 流控、逐日 Broker margin schedule、真实结算手续费和 market impact。因此下一步应取得真实 Shadow/test/small-capital/new-data 证据，而不是继续拟合同一历史。

## 16. Research checkpoint 对实盘行为的影响

**PR #25 没有改变 live economic behavior。** Stress-90 的 causal leadership response、60m candidate 与 research CSV artifacts 没有连接到 live runtime。实盘仍以 Broker/CTP 为唯一账户、订单、成交、持仓真相；reduction-first、5% daily circuit、hard/manual halt、35% margin、25% available、30% total DD、35 lots、target/realized gross <=2x 全部不变。
