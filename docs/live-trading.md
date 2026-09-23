# 实盘、影子运行、停机与恢复

本文是 CTP 正常运行手册。Shadow（影子运行）使用实时 CTP 信息，但订单、成交、持仓和资金全部由本地模拟 Broker 维护，绝不向柜台发送委托。异常诊断见 [`troubleshooting.md`](troubleshooting.md)，术语和公式见 [`glossary.md`](glossary.md)。

## 1. 适用范围

`afuture` 支持两种账户互斥模式：

- 跨期价差：固定组合或自动选择相邻月份合约；
- 方向组合：根据已完成数据生成品种目标。

两种模式共用 Broker、账户风控、停机开关、只减仓状态、状态存储、启动对账和审计链。

固定 Stress-90 候选已经成为显式可选的 Directional runtime policy；必须配置 `directional.policy = "stress90"`、完成固定 bootstrap 并绑定 policy identity。该 wiring 不等于真实资金 activation，离线高收益和 CI 不能替代真实行情、测试柜台、未来数据和小资金验证。

## 2. 推荐上线顺序

```text
固定历史回放和压力验证
→ Stress-90 固定输入 bootstrap、identity activation 和无报单 doctor
→ 连续多个交易日 CTP Shadow
→ CTP 无报单预检
→ 测试柜台验证订单、部分成交、拒单、重连和保证金
→ 极小真实仓位
→ 核对执行质量和结算单
→ 用新发生数据复核
→ 再决定是否扩大风险
```

不能通过继续调整同一历史数据来替代这些步骤。

## 3. 凭证和实盘确认

CTP 凭证只从环境变量读取：

```text
AFUTURE_CTP_USER
AFUTURE_CTP_PASSWORD
AFUTURE_CTP_BROKER
AFUTURE_CTP_APP_ID
AFUTURE_CTP_AUTH_CODE
AFUTURE_CTP_ACCOUNT_ID
AFUTURE_CTP_CURRENCY_ID
AFUTURE_CTP_INVESTOR_ID       # 柜台提供时
AFUTURE_CTP_INVEST_UNIT_ID    # 柜台提供时
```

Stress-90 的柜台连接必须至少显式配置预期 `AccountID` 和 `CurrencyID`。账户原始响应、VeighNa `AccountData`、权威 CTP 交易日和当次查询证据未形成唯一精确匹配时，账户身份保持未验证并触发 `broker_error`；不得据此新增风险。

真实生产还必须设置：

```text
AFUTURE_LIVE_ACK=I_UNDERSTAND_FUTURES_RISK
```

并在命令行显式传入 `--confirm-live`。

### 3.1 原生 CTP 目标机门

`vnpy_ctp` 含与操作系统、CPU 和 Python ABI 相关的原生扩展。普通 CI 和非目标开发机只验证 adapter 逻辑与测试替身，不能证明目标机能导入原生模块、登录实际前置或按真实顺序收到回调。部署前必须在最终目标机的同一 Python 环境安装 `.[live]`，至少完成 import、`status`、无报单 `doctor`、连续 Shadow、断线重连和完整订单生命周期验证；未完成时不能把 CI 绿色当成 CTP 可用证据。

## 4. 启动前检查

### 4.1 本地只读状态

```bash
afuture status --config config/afuture.directional-live.example.toml
```

`status` 不初始化日志、不连接 CTP、不修改文件，也不需要 CTP 用户名、密码和 Broker ID。它检查：

- 当前状态文件和上一版本证据的完整性；
- Directional OHLC 缓存的 schema、按配置顺序的完整品种 manifest、内容 digest、envelope checksum、最新日期和行数；
- 停机开关、运行状态和持仓摘要；
- 审计、告警文件大小；
- 运行路径是否可写、可访问；
- 磁盘是否至少剩余 100 MiB。

Stress-90 还检查 seed/policy/OI/intent 的 schema、sequence、checksum 和 identity，展示 bootstrap through day、last target、input digests、各层 decision digest、HHI/prior median、回撤预留状态、completed account wealth/HWM/drawdown、target/current lots、gross、tracking error、policy/data gap 和 remaining blockers。

当前状态损坏时命令返回 2。`state.json.prev` 只供人工诊断，不能自动恢复，也不能绕过 `recover-state`。

### 4.2 CTP 无报单预检

```bash
afuture doctor --config config/afuture.directional-live.example.toml --confirm-live
```

`doctor` 连接 CTP 并等待新的账户事件和完整持仓快照，但没有报单入口。它检查：

- 登录、交易日和快照是否最新；
- 账户数值、保证金、可用资金、单日亏损和总回撤；
- 活动委托；
- 合约目录、乘数、最小变动价位、保证金和手续费；
- 本地预期持仓与柜台持仓；
- 停机开关、运行状态和持久化安全门；
- 方向组合需要的上一完整交易日流动性证据；
- 已验证 Directional OHLC 缓存是否精确覆盖该完整交易日。

Stress-90 还会核验九品种 OI contract coverage、target-day continuity、policy/seed identity，预览每层 weights 与整数 lots，并用 live 合约参数估算开仓、平昨、平今、1 tick、bid/ask 和 depth 成本。任一必需项不可信时 `stress90_ready=false`；`orders_sent` 始终为 0。

任一检查失败都返回 2，输出中的 `orders_sent` 必须为 0。方向组合新部署要先观察一个完整交易日，形成流动性快照后才可能通过。

## 5. 方向组合示例配置

`config/afuture.directional-live.example.toml` 是普通 `execution_aligned` 起点；`config/afuture.directional-stress90-live.example.toml` 是 Stress-90 commissioning 起点。两者都不授予实盘许可：

| 项目 | 当前示例值 |
| --- | ---: |
| 配置品种数 | 50 |
| 预先固定的信号组合数 | 96 |
| 目标和实际总敞口上限 | 2.0 倍权益 |
| 单合约手数上限 | 35 |
| 距到期日最短天数 | 20 |
| 保证金占权益上限 | 35% |
| 可用资金占权益下限 | 25% |
| 单日亏损限制 | 5% |
| 总回撤限制 | 30% |

此外还启用行情新鲜度、盘口深度、涨跌停距离和订单频率限制。示例值不是对所有账户都安全，必须按真实资金、合约和承受能力确认。

## 6. 保证金约束下的目标手数

品种目标总敞口可以达到 2 倍权益，但开仓前必须使用 CTP 返回的合约参数计算每手保证金，并把目标只向下缩减：

```text
硬保证金份额 = min(最大保证金比例, 1 - 最小可用资金比例)
保守份额 = max(0, 硬保证金份额 - 单日亏损限制)
近期冲击 = 3% 到 5% 之间的：
           max(3%, 最近完整日收益绝对值, 两日收益样本波动)
正常目标份额 = min(保守份额,
                   保守份额 × (1 - max(0, 近期冲击 - 3%)))
```

当前示例配置在历史不足或市场平静时以约 30% 权益为正常目标；近期冲击高于 3% 时只会继续收缩。

执行规则：

1. 多头使用多头保证金率，空头使用空头保证金率；
2. 使用当前中间价、合约乘数和 `margin_estimate_buffer=1.25` 计算每手保证金；
3. 整数手数拟合只能减少目标，缺少可信保证金数据时拒绝开仓；
4. 生成开仓单后，账户风控再次检查 35% 保证金和 25% 可用资金限制，并拥有最终否决权；
5. 减仓、反转、换月、单日熔断和总敞口减仓不受减少换手规则抑制。

Shadow 和测试柜台必须比较模型保证金与柜台实际冻结保证金。历史固定比例与真实逐品种、逐日保证金不同是预期现象。

## 7. 上一完整交易日的流动性快照

系统不能在开盘后使用当前交易日尚未完成的累计成交量和持仓量重新选择主力合约。

`DirectionalActivityTracker` 按柜台交易日记录每个允许合约最后可见的成交量和持仓量。Tick 先更新内存；每轮有界 Broker 事件批次后，所有变化合并成一次带 schema/checksum 的原子 checkpoint。交易日切换和正常停机前也强制落盘；意外崩溃后只恢复最近成功 checkpoint，不声称恢复未落盘 Tick。这避免每个 Tick 都整文件 `fsync` 阻塞成交和账户事件。交易日从 D 推进时：

```text
D 日最终成交量和持仓量
→ 保存为 directional_activity.json
→ D+1 选约只读这份完整快照
```

D+1 实时行情仍用于价格、盘口、涨跌停、保证金和下单，但不能改变 D 日已经冻结的流动性证据。

已有持仓合约只要仍然符合挂牌、到期和流动性条件就优先保留。只有新合约的持仓量和成交量都严格更高时才换月。快照中的合约、品种和交易所必须与合约目录一致。

新部署没有完整快照时不新增风险；重启后的快照落后于已确认的价格历史时同样拒绝新增风险。

`directional_activity.json` 必须通过 envelope、checksum、identity 和数值校验。校验失败时保存原文件用于诊断，移走无效文件后用 Shadow 重新观察一个完整柜台交易日，直到 `status` 和 `doctor` 都通过。

## 8. 信号缓存、新鲜度和仓位收缩

方向组合的价格历史必须覆盖流动性快照对应的交易日。通过验证的 provider 数据以紧凑 JSON 原子写到 state 同目录的 `directional_ohlc_cache.json`：manifest 固定 schema 和配置品种顺序，content 使用一个共享日期索引以及开盘/收盘矩阵，并同时保存 content SHA-256 与整个 envelope SHA-256。缓存只属于市场输入证据，不保存策略目标、账户、订单、成交或持仓。

刷新时先验证现有缓存。新的 provider 历史必须在任何转换前使用无时区自然日午夜索引，开盘/收盘日期完全相同且唯一递增，全部正有限值可无损规范成 `float64`；不同输入 dtype 表示同一数值时不会被误判为修订。provider 必须保留已验证缓存的所有日期，且每个规范值完全一致；删掉最旧日、中间日或最新已缓存日都不是合法滚动窗口。provider 可带权威当前交易日的未完成行，但任何更远日期都失败关闭，当前行也必须通过数值检查；缓存只落盘 required completed day 及以前的重新解码表示。只有首次没有缓存，或现有日期集合与数值都未变时，provider 才是向后追加的新权威并原子替换缓存。provider 中断、返回非法数据或静默改写历史时，只能使用已经验证且覆盖 `completed_activity_snapshot.trading_day` 的旧缓存；缓存缺失、过期、被篡改、schema/shape/品种不符时失败关闭。长期停更或时间戳落在未来也失败关闭，不向前填充、不自动采用修订值。

预先固定的信号组合必须同时通过标准成本和压力成本验证，运行期间不能重新拟合历史参数。

普通 `execution_aligned` 引擎只使用已经完成交易日的账户收益决定下一目标：

```text
最近完整日收益 <= -2%
或者两日收益样本波动 >= 3%
→ 下一目标缩小到原来的 25%

否则
→ 保持 100%
```

当前交易日尚未完成的盈亏不能影响当前目标。该规则只能降低风险。

Stress-90 不使用上述 0.25 target scaling。它保留同一 adaptive soft margin envelope，但使用 1x raw candidate 做 margin-aware integer sizing，随后应用 completed account path 的 25% drawdown-reserve freeze。HHI 只用于观测，不否决开仓；回撤预留仍只冻结 entry/同向 add，reduction、exit、reversal 和 same-product roll 通过。`RiskManager` 仍拥有 margin、available、gross、daily circuit 和 HALT 的最终权限。

## 8.1 CTP 事件投递观测

CTP 的 order、trade、position、account 和 error 回调进入关键 FIFO；Tick 按 `(symbol, exchange)` 合并成尚未投递的最新值。每轮默认最多投递 100 条并优先关键 FIFO，避免 Tick 洪峰饿死成交与账户真相。运维诊断可读取 `delivery_counters()` 的 enqueued/received/coalesced/delivered/backlog 计数；`ticks_coalesced` 上升表示旧的未消费 Tick 被更新值替换，不代表成交丢失。持续增长的 `critical_backlog` 必须视为运行容量问题，停止扩大风险并在目标机定位回调/消费延迟。

Stress-90 的 raw 60m observer 位于 Tick 转换成功后、合并前，因此 manager Tick 可以被 coalesce，而 Price×OI evidence 仍看到每个有效 raw Tick。callback 不做磁盘或网络 IO；证据只在一批 Broker events 完成后 checkpoint。它按 CTP `trading_day`、固定 session manifest 和 expected contract universe 记录 first open、last close、first/last hold、累计 volume 增量及 coverage。missing/incomplete 与合法 `flow=0` 始终分开。

## 9. 先减仓、后开仓

每个行情事件都根据柜台真实持仓重新计算总名义敞口：

1. 目标和实际总敞口都不得超过账户权益的 2 倍；
2. 实际总敞口超限时只发送减仓 FAK；
3. 已有减仓委托未结束时不重复发送；
4. 无法安全计算或执行且账户仍有风险时进入只减仓状态；
5. 某个新目标缺少合格合约或最新行情时，只禁止该产品新增风险，不能阻塞其他必要减仓；
6. 所有减仓经 Broker 确认后，下一轮才允许开仓。

同一合约同时存在多头和空头毛仓时，系统分别生成平仓单，不能因为净仓为零而漏掉风险。

正常开仓只有在对手一档深度覆盖整笔数量时才使用当前最优对手价，否则使用更保守的价格。这个优化不改变强制减仓价格和风险权限。

## 10. Shadow 影子运行

```bash
afuture shadow --config config/afuture.directional-live.example.toml --duration-seconds 3600
```

Shadow 使用真实 CTP 合约目录、行情、交易日和合约参数，以及正式策略和风控逻辑；账户、订单、成交和持仓由本地 `SimBroker` 维护。

Stress-90 Shadow 使用 `runtime/shadow/` 下单独 bootstrap、activation 和持久 state，不能复用 live account path。准确命令见 [`stress90-live-runbook.md`](stress90-live-runbook.md)。Shadow 与 live 共用 raw CTP evidence 链；vendor comparator 没有 Broker 或订单权限，任何未解释 flow 差异都阻断 activation。

至少记录并复核：

- 目标总敞口、保证金缩减后的目标和实际总敞口；
- 模型每手保证金与 CTP 合约参数；
- 目标手数与实际手数；
- 保证金比例、可用资金、单日亏损和高水位回撤；
- 计划与实际换手；
- 滑点中位数和 95 分位数、手续费、部分成交和拒单；
- 主力合约切换是否符合上一完整交易日的流动性证据。

## 11. 单日熔断、硬停机和恢复

单日亏损达到 5% 时：

```text
正常运行
→ 进入只减仓并退出风险
→ 当日不重新开仓
→ 下一柜台交易日重新完成安全检查
→ 恢复正常运行
```

总回撤、保证金、可用资金、非正权益、合约参数、持仓对账或基础设施异常属于硬停机或人工处理路径，不能在下一交易日自动恢复。

恢复正常运行前必须满足：

- Broker 已就绪；
- 没有活动委托；
- 没有未处理的方向风险；
- 合约参数已经验证；
- 账户限制全部通过；
- 启动对账通过。

`recover-state` 只是人工恢复入口，不能跳过停机原因和柜台对账。它会在 CTP 启动前恢复可证明交易所的成交 identity；任何 identity 缺少 exchange 时拒绝采纳，必须先人工完成柜台对账。


### 11.1 Stress-90 账户连续性模式

Stress-90 默认 `directional.account_continuity_mode = "strict"`。未配置该字段时行为与原有严格模式完全一致：缺少 prior-day final funding/settlement witness 或权威 nonadjacent session ledger 时继续 fail closed，现有 `stress90-settlement-roll-forward` 的含义不变。

`operator_managed` 只面向个人专用、单账户、`account_exclusive=true` 的 live Stress-90 runtime。它记录本地 `stress90_operator_continuity.json` receipt，把当前 CTP 交易日、账户/epoch/runtime、registry/TDE、完整 session ownership、order journal、Broker/local 持仓对账、Deposit/Withdraw、OHLC/activity/OI/policy 对齐和操作者的“无人工交易、无外部委托、无入金、无出金”声明绑定在同一 checksum 链中。该 receipt 的 authority 是 `operator_trust`，**不是**交易所/Broker 官方结算见证或官方 session ledger；`external_activation_gates_completed` 始终保持 `false`。

跨日必须显式执行 `afuture stress90-operator-roll-forward`，并同时提供 `--confirm-live`、`--confirm-operator-continuity`、唯一 64-hex `--operation-id`、`--operator-reason` 和 `AFUTURE_OPERATOR_CONTINUITY_ACK=I_CONFIRM_EXCLUSIVE_ACCOUNT_AND_NO_EXTERNAL_ACTIVITY`。命令只在 `HALTED`、kill switch 开启、fresh account/positions/session ownership 完整、活动委托为零、journal 无未终态/unknown、Broker/local 持仓一致且当前 Deposit/Withdraw 都为零时推进；它不报单也不撤单。周五到周一等自然日间隔可以由 operator receipt 明确确认，但不能跳过已经存在的中间 OHLC/OI session 数据，也不使用本机日期、`BDay` 或静态节假日日历猜测交易日。

成功后 generic/policy state 仍通过 recoverable lifecycle transaction/CAS 原子推进，runtime 仍为 `HALTED`、kill switch 仍开启、`metadata_verified=false`。此前的 activation permit 被失效，必须再次执行 `status`、`doctor` 并签发新的 technical permit。任何人工交易、外部委托、入金、出金或无法解释的资金变化都不能算策略收益；必须先用 `stress90-account-rebase` 建立新的账户 epoch，然后才能重新建立 operator continuity。

## 12. 启动对账与执行质量

方向组合不持久化第二份策略持仓。重启时以 Broker 完整账户和持仓为真相，与 `StateStore` 中的预期状态核对；任何差异都失败关闭。

`ExecutionQualityRecorder` 记录：

- 调仓：信号日期、流动性日期、目标和计划换手；
- 成交：实际价格、滑点、手续费、延迟、部分成交和拒单；
- 周期：实际换手、目标偏差和剩余风险。

Stress-90 每个 target day 另记录 Base/OI/cost/survivor、HHI/prior median、回撤预留状态、raw/margin-fitted/drawdown-frozen/HHI-observed/final lots、reduction/opening plan 和 daily decision digest；计划/成交记录包含 expected open、planned/actual price、one-way cost、p95 cost、partial/reject、latency 和 model/actual turnover。

使用 `afuture quality-report` 持续汇总这些证据，并与结算单核对。

## 13. 离线研究与实盘边界

当前离线压力研究结果来自固定历史输入和确定性账户模拟。它没有覆盖多年完整盘口排队、柜台限流、断线、逐日保证金、实际结算费率和极端行情冲击。后续代码已经完成可选 runtime wiring，但这不会改变历史证据中当时 `production_wiring=false` 的事实，也不会自动补齐现场证据。

完整离线结果见 [`stress90-final-evidence.md`](stress90-final-evidence.md)，当前代码边界见 [`stress90-live-productionization.md`](stress90-live-productionization.md)，准确操作顺序见 [`stress90-live-runbook.md`](stress90-live-runbook.md)。`112.100053%` 不是未来收益承诺，96-template pool 存在已观察历史 selection bias；真实资金上线条件以 [`production-checklist.md`](production-checklist.md) 为准。

## Stress-90 risk-overlay change control

Never edit Stress-90 live risk fields under a RUNNING process. A configured/bound risk-overlay digest mismatch blocks live startup and invalidates the meaning of an old technical permit. To rebind, stop in `HALTED` with kill switch enabled, require Broker/local flatness, zero active orders and a fresh reconcile, then run the existing `stress90-activate` command with a new operation id and activation confirmation. The lifecycle coordinator changes only the overlay marker for this case; account epoch, strategy returns and historical candidate identity are not reset.

Before first money, run `stress90-capacity-report`. It performs no order or cancel calls and does not checkpoint generic/policy/intent state. Missing real margin or commission evidence is a hard failure. Representation warnings never loosen hard risk limits or automatically increase scale.

## 当前生产启动顺序

生产 Stress-90 只使用以下顺序：

```text
deployment-verify
→ prepare-session
→ [仅在报告要求时] operator roll-forward / account rebase
→ doctor
→ capacity review
→ issue fresh activation permit
→ live
→ watchdog
→ HALTED backup/recovery
```

`prepare-session` 和 `watchdog` 都不能报单、撤单、修改交易 state、签发 permit 或自动解除 HALTED。`prepare-session` 可以连接只读 CTP/Doctor Broker 取得 fresh facts；`watchdog` 完全不需要 CTP 凭证且不会连接 Broker。

Live 启动在构造 order-capable Broker 前检查 `process_run.json`。上一进程没有 clean receipt、receipt 损坏或链不确定时，启动必须 fail closed 到 `HALTED` + kill switch，并失效现有 permit；operator 重新完成盘前检查、Doctor 和新的 permit 后才允许再次尝试。systemd 的自动 restart 不构成 activation authority。
