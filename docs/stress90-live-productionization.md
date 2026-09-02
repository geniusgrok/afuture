# Stress-90 实盘生产化边界

本文描述当前代码中 `directional.policy = "stress90"` 的正式接线、状态身份、数据证据和安全边界。固定历史研究事实仍以 [`stress90-final-evidence.md`](stress90-final-evidence.md) 为准；该证据保留当时 `production_wiring=false` 的历史事实，不能被本文倒写。

## 1. 当前结论

Stress-90 已成为一个显式、可选且与普通 `execution_aligned` 隔离的 Directional runtime policy。代码接线完成只表示下列机械路径已经实现并由自动化测试保护：

```text
固定 Base 96-template 权重
→ 60m Price×OI 方向变化确认
→ 20/3/15bp 固定成本门
→ survivor reallocation
→ raw candidate HHI
→ exactly-once prepared decision
→ live contract/margin-aware integer sizing
→ 25% completed-path drawdown-reserve freeze
→ HHI concentration freeze
→ reduction-first Broker execution
```

它不表示已经取得真实资金许可。上线证据按六个互不替代的阶段管理：

1. 固定历史候选验证；
2. 代码 wiring 和自动化验证；
3. 多日 CTP Shadow；
4. 测试柜台订单生命周期；
5. 极小真实资金；
6. 扩大风险的人工许可。

阶段 2 不能自动勾选阶段 3–6。目标机 CTP ABI、真实费率与保证金、FAK 部分成交、拒单、断线重连、极小真钱和扩大资金都属于外部现场门。

## 2. 两种 Directional policy

生产 `system.mode = "live"` 时必须显式配置 `directional.policy`：

| policy | manager | 策略风险响应 | current 边界 |
| --- | --- | --- | --- |
| `execution_aligned` | `ExecutionAlignedDirectionalPortfolioManager` | 保留 `0.25` target-weight scaling | 当前配置/运行时都必须显式选择该 policy |
| `stress90` | `Stress90DirectionalPortfolioManager` | 使用 `1x` raw candidate，再在整数 lots 层分别应用两个 freeze | 不接受旧 policy state 的静默迁移 |

manager 通过明确的 `policy_risk_response_mode` capability 告诉引擎如何响应风险。Stress-90 不靠类名或 `isinstance` 特判，因此不会再被 `DirectionalRiskScaledPolicy` 重复缩放。两种模式仍共享 adaptive soft margin envelope 和最终权威 `RiskManager`。

policy 切换不是普通配置热更新。首次绑定 Stress-90 identity 必须使用 `stress90-activate`，且同时满足 `HALTED`、kill switch、Broker 与本地空仓、无活动委托、完成对账、seed/state identity 一致和双重强确认。绑定后仍保持 `HALTED`；后续 `doctor`、Shadow 和现场门不能被该命令绕过。

## 3. 单一候选核心

`afuture/directional_stress90_policy.py` 是研究 batch wrapper、bootstrap replay 和 live incremental runtime 共用的无副作用核心。live runtime 不导入 `tools/`、`directional_acceptance`、CLI 文件读取入口或研究 entrypoint。

不可变 `Stress90PolicyDefinition` 固定：

- policy id/version 和 definition digest；
- 50 品种、96 templates、meta `11/3/3`；
- OI 支持品种 `A,C,EG,I,M,P,PP,TA,Y`；
- completed lookback `20`、benefit horizon `3`、单边 hurdle `15bp`；
- Base/Stress endpoint `5bp/15bp`；
- gross cap `2x`；
- HHI expanding-median 规则；
- `30%-5%=25%` drawdown reserve；
- 固定历史 candidate SHA-256；
- 不可放宽的 hard-risk envelope。

三个 digest 不能混用：

- `policy_definition_digest` 标识定义和常量；
- `historical_candidate_weight_sha256` 标识固定历史完整候选矩阵；
- `daily_decision_digest` 标识某一 target day 的输入、各层目标和 post-state。

`step_stress90_candidate(...)` 只接受显式 prior state、权威 target trading day、Base 权重、已完成 close history 和已完成 OI flow。它不读取 Broker、账户、网络、文件、本机日期或当前未完成 PnL；同一规范输入产生相同字节 digest。每层都校验有限值、50 品种 manifest、support、方向和 gross。

## 4. 候选经济行为

60m Price×OI 对九个支持品种按当日最后持仓量、总成交量、合约代码稳定排序选 dominant contract。只有 `last_hold > first_hold` 且价格方向非零才产生 `-1/+1`；合法 `0` 与 missing/incomplete 是不同状态。entry、同向加仓和反转进入新方向需要确认；未确认反转归零；减仓与退出通过；其余 41 品种不变。

成本门只读取 target day 之前已经完成的 close：20 个日收益的算术和、3 日 benefit、严格高于单边 15bp，并再 lag 一个 target session。它只阻止 entry 和同向加仓。

survivor reallocation 不创造 support、不改变方向、不超过 OI-confirmed gross 或 `2x`。第一目标最小化相对 OI-confirmed 目标的 L1 tracking error，第二目标最小化相对上一 applied target 的 L1 turnover，并使用固定比例 tie-break。

HHI 在 survivor product weights 上按绝对权重归一化计算。先与 strictly-prior finite HHI history 的中位数比较，再追加当前 HHI；历史为空不触发，当前 HHI `<=` prior median 时触发。只要完整 target 成功生成，不论有没有可交易合约、订单或成交，HHI 都 exactly once 推进。HHI 不读取实际持仓、成交或账户 PnL。

25% reserve 只读取已经完成的柜台交易日账户收益，以复利 wealth 和 completed high-watermark 的充分统计量判断。当前未完成日 PnL 不进入该路径。两个 freeze 都只冻结新风险和同向加仓；减仓、退出、反转和同品种换月继续通过。

## 5. Exactly-once 状态

Stress-90 使用四份彼此分责的带 schema/sequence/checksum 文件：

| 文件 | 内容 |
| --- | --- |
| `stress90_bootstrap_seed.json` | 固定输入 manifest、policy identity、最后三层 candidate state、完整 prior HHI 和历史摘要 |
| `stress90_policy_state.json` | last completed target、prepared decision、三层 state、HHI、completed account wealth/HWM 和最近两日 adaptive returns |
| `stress90_oi_evidence.json` | in-progress 与 completed raw 60m/session evidence、覆盖集合和缺失集合 |
| `stress90_execution_intent.json` | 已持久的每日整数目标和 reduction/opening plan identity |
| `stress90_ctp_orders.json` + archive/epoch manifest | 第一张 official send 前的 durable order authorization、完整 fill economics、跨容量/账户 epoch 的不可变冷链 |
| `stress90_crash_fill_recovery.json` | 仅在 `HALTED` + kill switch 下采用已授权崩溃成交的独立 prepared/committed checkpoint、完整 session order/trade evidence 与认证 compact nonce root |

另外，通用 `state.json` 保存运行状态、Broker position mirror 和明确的 policy activation marker。每次成功替换先保留验证过的 `.prev` 作为人工证据；当前文件损坏时绝不自动回退。

Stress-90 不使用通用 `recover-state` 猜测或采用崩溃成交。专用
`stress90-crash-fill-recover` 先取得精确 account/runtime lease，拒绝 prepared lifecycle
transaction，再在 Broker critical-ingress fence 内复查完整本 session order/trade evidence、
account epoch 与 account-specific registry receipt、无 active/unknown order/trade 和 Broker/local
一致性。随后 durable 顺序固定为 registry 全局 nonce acknowledgement、TradingDayEvidence
精确 roll-forward、recovery prepared、generic state CAS、recovery committed；
两处崩溃均只允许同一 nonce、相同语义 evidence 和相同 state target 的 exact retry。machine-wide
registry sequence/checksum 只作审计快照；授权绑定账户自己的 payload digest、revision、last
operation 和 receipt digest。全 machine nonce 永久性由 current registry schema-3
Patricia-Merkle root 与不可变 content-addressed node/receipt 保证；current initialization 直接创建
认证 nonce ledger。旧 registry/migration artifact 不是可升级输入，必须失败关闭、归档并以 current
bootstrap/initialization 重建。80% 容量告警、1,000,000 receipt 硬上限均失败关闭且 receipt 永不淘汰。
命令不报单、不撤单，
成功后仍为 `HALTED` 且 kill switch 开启。
首次后续 lifecycle 将 recovery marker 与自身 operation nonce 一起转换成 consumed marker，并在
generic CAS 后、lifecycle coordinator commit 前向 recovery store 追加 durable consumption receipt；
该参与者的 crash retry 必须精确完成，后续 lifecycle 只保留并验证 receipt，不再要求 generic
state 永远停在最初 recovery target。

跨日 account path 仍有一个独立外部 blocker：当前代码不能证明上一完整柜台交易日的最终资金流。
锁定的 `vnpy_ctp 6.7.11.4` 虽然暴露 request-bound `QrySettlementInfo` 和
`QryTransferSerial`，但前者只把结算单资金内容作为无结构 `Content` 文本返回，后者只查询银期
转账流水，且请求没有交易日或保留范围边界。二者都不能排除柜台人工调账、非银期资金变化或
历史保留不完整。D+1 的 `PreBalance` 与当日累计 `Deposit/Withdraw=0` 也不能证明 D 日最后一次
本地 snapshot 后没有资金变化，operator assertion 不能替代柜台证据。

因此 `stress90-settlement-roll-forward` 在取得以下外部证据前始终失败关闭：一个不可变、响应完整
且具有明确 finality 语义的结构化记录，必须绑定账户、币种、completed day、settlement identity、
request/generation，并给出包含非银期/人工调整在内的最终 `Deposit` 与 `Withdraw` 总额。若目标
柜台只能给结算单文本，必须先取得柜台文档化的稳定 grammar、真实目标机 fixture，并与独立完整
资金台账对账；未知格式、sequence gap、无 `bIsLast`、超时、错误或任一 identity 不一致都继续
阻断。该 blocker 不允许 FakeBroker/provider/parser fallback，也不允许把未闭合资金流记为策略收益。

非相邻自然日的 session continuity 是另一项明确的外部 activation blocker。对锁定的
`vnpy_ctp 6.7.11.4` 生成 ABI 逐项审计后，行情 API 只有登录、订阅和当前 Tick，交易 API 的
`getTradingDay()` 也只返回当前前置交易日；request/callback 列表没有 trading calendar、holiday
calendar 或可证明完整历史 session chain 的查询。`QryUserSession` 只返回登录会话，
`QryExchange` 只返回交易所标识/名称/属性，`OnRtnInstrumentStatus` 只是没有 `TradingDay` 的当前
状态推送，均不能证明周末/节假日之间没有遗漏 target session。

因此 raw CTP `ObservedTradingDayTransition` 只允许相邻自然日；聚合器、持久化 decoder 和消费端
都会拒绝非相邻 source/target。Sina OHLC 行、静态品种时段 manifest、两个 endpoint day、长连接、
operator assertion、`pandas.BDay`、本机日历或猜测的节假日都不能替代 session ledger。
`status`/`doctor` 必须持续列出 `authoritative_nonadjacent_session_ledger`，其诊断为
`not_available_from_pinned_vnpy_ctp_6_7_11_4`；周五到周一及任何节假日 gap 保持失败关闭。

只有取得真实目标柜台或交易所发布的不可变 official ledger 后，才可另行评审 ingestion：证据至少
要绑定 issuer/exchange、适用产品/session scope、有效区间、完整且有序的 official trading-day
chain、版本/发布 identity、内容 digest 或签名、finality/completeness 语义，以及产生证据的
request/generation（若经柜台查询）。未知版本、覆盖边界不清、漏日、重复、乱序、响应不完整或
任一身份不一致都必须继续阻断。本轮不预造没有真实 fixture 和 provenance 的 ledger schema。

一个 target day 只能推进一次。prepared decision 必须在第一张订单前原子落盘；持久化失败则 `orders_sent=0`。决策已保存但未下单、第一张订单后崩溃或部分成交后崩溃，重启都复用同一 decision/intent，以 Broker 最新持仓继续向同一目标收敛，不重复追加 HHI，也不重新推进候选状态。policy/schema/manifest/seed/digest 不一致、交易日倒退或 target gap 均失败关闭。

历史 bootstrap seed 不继承回测账户收益。live account soft path 从真实账户 inception 开始；充值、出金或换账户只能在全套生命周期门通过后以唯一 `--operation-id` 执行 `stress90-account-rebase`。同一账户只接受 verified nonzero cash-flow adjustment，不能用零资金流重置 hard daily/HWM；换账户才创建新 account epoch。运行中不允许手工交易、其他策略、充值或出金，也不允许把资金流自动当作策略收益。

每次 activation、rebase 和 policy migration 都在 account-exclusive lease 内对 current order journal、所有 archive segment 和全部 sealed epochs 做完整冷链审计；损坏或 pending cleanup 会在任何 lifecycle 写入前失败关闭。容量达到硬上限时只能使用 `stress90-order-journal-rollover`，且必须 `HALTED`、Broker/local 空仓、无活动委托、reconcile 完成和双重强确认。封存不丢弃旧 order/fill identity，跨 epoch 重用仍被拒绝。

## 6. Raw CTP 60m evidence

raw evidence observer 位于 CTP Tick 转换成功后、`_enqueue_tick()` 合并前。每个有效 raw Tick 进入有界内存聚合器；manager 可继续合并普通 Tick，而 order/trade/position/account/error 的 critical FIFO 优先级不变。callback 只做有界内存更新，不做磁盘、网络或长计算；完成一个 broker event batch 后才 checkpoint。

聚合器以 CTP `trading_day` 归属交易日，不按本机自然日分组。session manifest 固定交易所/品种有效时段和 60m 边界，并处理夜盘跨午夜、累计 volume 非负增量、volume reset、duplicate、out-of-order、late Tick、重启、rollover、多合约和不完整 session。

九个 OI 品种必须订阅当日 eligible futures contract 全集，并过滤期权、组合和非法合约。completed evidence 同时保存 expected、observed 和 missing contract sets；未观察到潜在更高 OI 合约时，产品保持 incomplete，不能把 missing 变成 `flow=0`。

可选 `stress90-oi-compare` 是无 Broker、无订单权限的离线 comparator。它比较 first open、last close、first/last hold、total volume、dominant symbol 和 final flow；无法解释的 flow 差异是 activation blocker。

## 7. 数据与执行时序

Stress-90 runtime 的 target day 只取 `CtpBroker.get_trading_day()`。本机日期、UTC 日期、`pandas.BDay` 或猜测的节假日都不是权威。seed 到当前首个 target day 之间必须按完整 evidence manifest 逐日 backfill，不能跳日。

订单路径不进行同步外部抓取。`directional-ohlc-refresh` 是独立、无订单权限的数据准备命令；runtime 只读通过 digest/checksum、append-only overlap 和 required completed day 校验的 OHLC cache。provider 失败但 cache 完整时可继续使用；cache 不足时空仓拒绝新增风险，有仓进入 `REDUCE_ONLY`。

完整调仓顺序是：

1. 验证 CTP current trading day 和上一完整 activity day；
2. 验证 OHLC、60m OI 和 activity coverage；
3. 生成 Base、OI、cost、survivor 和 raw candidate HHI；
4. 先比较 prior HHI median，再推进 HHI，并原子保存 prepared decision；
5. 读取 Broker 账户、持仓、quotes 和 live contract specs；
6. 依上一完整 activity 选择具体合约并做 margin-aware integer sizing；
7. 应用 adaptive margin envelope、completed-path drawdown reserve freeze、再应用 HHI freeze；
8. 生成 reduction-first plan；Broker 确认 reductions 后重新读取 truth，并再次运行 `RiskManager.check_open_orders()`；
9. 仅在当前产品首个固定 entry window 内允许 openings；错过窗口不追单，但 target state 仍推进；
10. 所有持仓变化只来自 Broker trade callback。

HHI 始终使用第 3 步 raw candidate product weights，不使用整数手数、账户结果或 risk governor 输出。硬 `RiskManager` 保持最终否决权。

## 8. 失败关闭矩阵

| 异常 | 空仓 | 有持仓 |
| --- | --- | --- |
| policy state 缺失/损坏或 identity 不一致 | `HALTED` / 拒绝启动 | `HALTED`，人工对账 |
| target-day gap | 拒绝新增风险 | `REDUCE_ONLY` |
| OHLC、60m OI 或 activity required day 缺失/陈旧 | 拒绝新增风险 | `REDUCE_ONLY` |
| CTP trading day 非法或倒退 | `HALTED` | `HALTED` |
| live metadata 缺失 | 拒绝新增风险 | `REDUCE_ONLY` 或现有硬门要求的 `HALTED` |
| active orders 未结 | wait | wait |
| Broker/local position drift、unknown order/trade | `HALTED` | `HALTED` |
| provider 失败但 verified cache 完整 | 使用 cache | 使用 cache |
| provider 失败且 cache 不完整 | 拒绝 | `REDUCE_ONLY` |

系统不猜测数据、不自动采用 `.prev`、不自动跳过 target day、不清除 kill switch，也不会在异常路径增加风险。

## 9. 硬风险与历史可比性

Stress-90 不放宽 target/realized gross `2x`、margin `35%`、available `25%`、daily loss `5%`、total drawdown `30%`、单合约 `35` 手或 margin estimate buffer `1.25`。commissioning 可以更严格，但 `status`/`doctor` 必须显示与固定历史 envelope 的差异；更严格配置的结果不能声称为历史矩阵的精确复现。

Doctor 以 live specs 估算开仓、平昨、平今、1 tick、bid/ask spread 和 depth。若确定性费用加最低合理滑点已显著超过单边 15bp，该合约成为 blocker；不能动态提高候选成本门来悄悄改变 Stress-90，必须以真实成本创建独立命名的新压力矩阵。

## 10. 证据解释

历史 Stress full_recent 年化 `112.100053%` 不是未来收益承诺。96-template pool 在已观察历史上存在 selection bias；训练、validation、OOS 和 full_recent 也不是四个彼此独立的最终留出集。真正的 forward evidence 来自本候选固定后新发生的数据。

CTP raw 60m 与历史 vendor 数据源具有不同采集、修订和 session 语义。必须在 Shadow 中解释 first/last/volume/dominant/flow 差异，不能因代码输出相同字段就宣称数据源迁移成功。

准确操作顺序、强确认环境变量和现场门见 [`stress90-live-runbook.md`](stress90-live-runbook.md)。
