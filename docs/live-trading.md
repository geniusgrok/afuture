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

### 3.2 隔离测试柜台的结算格式采集

在确认目标是零真钱测试柜台、完成首次账户绑定并明确批准该次连接后，可使用：

```bash
afuture ctp-settlement-capture --config /etc/afuture/test.toml \
  --trading-day 20260921 --output /private/evidence/settlement-20260921.json \
  --confirm-test-connection
```

该命令拒绝 production 环境、缺少明确 AccountID/CurrencyID、已有输出文件和非规范绝对路径。
它先取得同一账户/runtime 的互斥锁，再连接、等待新账户/完整持仓快照，查询指定交易日的
`SettlementInfo`；不下单、不撤单、不推进账户状态、不签发交易许可。原生登录可能执行柜台
结算确认，因此“零报单”不等于“零柜台写操作”，不能连接性质未确认的环境。

输出以私有权限保存请求 ID、账户/交易日、SettlementID、分片序号、查询结束标志、
依赖版本和摘要。重复相同分片只保留一次；冲突、缺片、错身份、迟到的已完成回报和非法
文本均拒绝。`vnpy_ctp==6.7.11.4` 把每片 Content 转成 Unicode，采集不声称保留原生
GBK 字节，也不声称已验证首片序号原点。其
`financial_continuity_verified=false` 不能被改成结算/资金连续性已验证。
只有核对实际柜台格式、字段语义与完整性后，才能建立版本化的确定性结算解析合同。

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

Stress-90 还检查 seed/policy/OI/intent 的 schema、sequence、checksum 和 identity，展示 bootstrap through day、last target、input digests、各层 decision digest、HHI/prior median、两个 freeze、completed account wealth/HWM/drawdown、target/current lots、gross、tracking error、policy/data gap 和 remaining blockers。

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

Stress-90 不使用上述 0.25 target scaling。它保留同一 adaptive soft margin envelope，但使用 1x raw candidate 做 margin-aware integer sizing，随后依次应用 completed account path 的 25% drawdown-reserve freeze 和 raw candidate HHI freeze。两个 freeze 只冻结 entry/同向 add；reduction、exit、reversal 和 same-product roll 通过。`RiskManager` 仍拥有 margin、available、gross、daily circuit 和 HALT 的最终权限。

### 8.0 实时执行时钟和完整查询边界

跨期和 Auto 生产路径在计算信号及每条腿真正发送前，以当前时钟重新检查报价新鲜度，
限速使用单调经过时间。不能用两条同样滞后的行情相互证明新鲜，也不能用未来信号时间
清空实盘限速窗口；历史回放仍显式使用事件时钟。第一条腿成交后第二条腿报价过期时，
拒绝继续加风险，不拿陈旧价格盲目平仓，保留真实单腿暴露并进入原停机/对账链。

原始账户、持仓查询在发送前登记 request ID、账户和柜台交易日；收到同一请求的结束
标志并校验全部行后，才交给 SDK 转换并发布快照。未完成查询不更新新鲜度；重复相同行
去重、冲突行和跨请求/跨交易日回报拒绝。SDK 静默丢失未知合约、错误方向/数量或跨交易所
同名合约合并均拒绝，不能把不完整持仓解释为空仓。当前持仓模型不支持净持仓/套保语义，
发现这些行时停止核验而不是折算成投机仓。

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

Stress-90 每个 target day 另记录 Base/OI/cost/survivor、HHI/prior median、两个 freeze、raw/margin-fitted/drawdown-frozen/HHI-frozen/final lots、reduction/opening plan 和 daily decision digest；计划/成交记录包含 expected open、planned/actual price、one-way cost、p95 cost、partial/reject、latency 和 model/actual turnover。

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

Live 启动先取得账户/runtime 锁，在构造 order-capable Broker 前检查 `process_run.json`。未清洁退出默认失效技术许可并保持 HALTED/kill switch；唯一自动续接例外是 runbook 规定的“许可已消费、原状态尚未提交”且全量证据仍一致的同次提交。损坏或异账户证据、缺失的已有账户状态保持只读阻断，不创建资金基线或自动采用 `.prev`；重复启动不得干扰活动实例。既有事故恢复仍须完成安全核验和所需授权；systemd 自动重启本身不构成 activation authority。

### 单实例启动和异常重启证据

生产入口先取得账户/runtime 互斥锁，再读取或推进 process-run、状态和许可。重复启动
只读失败，不能把正在运行的实例标成异常重启、改写其停机状态或消费其许可。
锁覆盖引擎构造到退出收尾，构造失败也释放。已有未清洁进程记录但账户状态缺失时，
不得创建新空账户状态；账户、部署或 runtime 身份变化、损坏的进程证据也不自动收编。
这些修复不等于跨日自动授权或资金连续性闭环，既有事故恢复门仍然有效。


### 运行日历与当前报单时间

非历史模式的 live/Shadow 引擎在 `runtime_factory` 装配带版本、来源、日期覆盖、完整开休市日期和 SHA-256 的 `afuture/runtime_calendar.json`，由现有 RiskManager 裁决，普通双腿与方向性每笔报单、失衡减仓和已成交腿的减仓回滚均不能绕过。没有新增账户事实源。当前 bundle 覆盖 2025-12-31 至 2026-12-31、现有 50 个 Stress-90 品种及普通路径的原油 SC；它不将 SC 加入策略品种池。未知合约/交易所、未覆盖日期、损坏日历、实际柜台交易日冲突均拒绝，而不是回退到周一至周五。

SHFE/INE 时段采用已核对的交易所时间表；DCE/CZCE 品种时段沿用仓库 2026-08-25 固定资料及其交易所来源，没有冒称本次重新核验了这两个交易所的每份业务细则。年度休市使用各交易所 2026 年通知，DCE/CZCE 原通知的公开转载路径在 source 中明确记录。当前没有公告增量下载器；新增或变更的正式时段、年度覆盖扩展必须进入获批源码/日历版本后重新验证，不能在运行中猜测。部署 seal 的 tracked production source digest 覆盖该 JSON，改动日历会使旧部署身份不匹配。

夜盘自然日期与柜台交易日分开：周五夜盘和周六凌晨可属于周一交易日；节前取消的夜盘不恢复；日盘休息区间不触发行情陈旧告警。全量未知日期仍会阻断并告警。心跳对行情年龄使用相同休市过滤，但不会用休市清除账户、身份或硬风险错误。Runtime 在闭市/集合竞价期间不消耗尚未形成的方向性首次入场意图；逐产品入场窗口、D→D+1 信号和风险参数没有放宽。

RB/HC/BU 的实际夜盘在 23:00 结束，与原冻结研究表的 01:00 不同；运行时限制单独纠正，冻结研究表、政策摘要和既有研究结果不倒写。生产每笔方向性开仓/减仓重新检查当前行情、柜台健康、实际交易日；减仓还检查 Broker 当前今昨仓、方向和数量。事件时间只能用于显式历史回放，生产限速用单调时间。异常中已成交的持仓和订单身份不会因为后续拒单而抹除。

### 持久关键通知

配置 `AFUTURE_ALERT_WEBHOOK` 后，CLI 与无柜台能力的 watchdog 复用 `paths.alerts` 同目录的 `.outbox.sqlite3`。事件先写入受限权限、目标地址摘要绑定的 SQLite spool，再由通知线程进行有超时的 HTTPS 发送。不会跟随重定向泄漏事件到未批准地址。已有 spool 的身份、结构或完整性不匹配时保留原件并拒绝使用，不自动转换、清空或重建；此类部署切换应按通知事故接管，不属于正常每日操作。默认未解决事件上限 2048 条，单事件上限 16 KiB，数据库上限 32 MiB；HTTP 已接收的收据保留 32 天用于覆盖自然月，仍受数据库总量硬界约束，不能为腾空间删除未确认事件；重复告警合并计数。

进程中断保留 pending/inflight/failed；重新领取使用同一 event_id 和 Idempotency-Key，语义是至少一次送达，接收端应去重。默认每事件最多 6 次尝试，退避有界；耗尽保留 failed 并在日志/心跳/watchdog 中明确升级。队列满、无法持久化或源损坏不静默删除旧事件；失败计数可见，损坏 spool 不自动重建。修复通知端后应按受控事故流程处置 failed 原件，不能删除数据库掩盖失败。

心跳 `alert_delivery` 与 watchdog 显示 pending/failed 数和最后 HTTP 接收时间。2xx 只证明通知端接受请求，不证明人已阅读。交易进程和同机 watchdog 都不能证明整机断电后通知送达；独立接收端/主机失联监测仍必须在获批环境中实测，目前没有已完成的现场证据。
