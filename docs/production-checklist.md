# 生产上线检查表

本表用于判断系统是否具备投入真实资金的证据，不等同于“代码可以启动”。术语和风险状态定义见 [`glossary.md`](glossary.md)。

历史回放、离线账户模拟和持续集成只能证明代码及固定输入下的行为，不能替代真实盘口、测试柜台和新发生数据。以下未完成项全部通过前，不应扩大真实资金。

## A. 已完成的离线机械验证

- [x] 连续合约换月跳空不计入收益。
- [x] D 到 D+1 的收益来自 D 日已经选定的同一具体合约。
- [x] 距到期不足 20 天的合约不能新增风险。
- [x] 目标和实际总敞口均受 2 倍权益硬限制。
- [x] 账户模拟使用整数手数、固定合约乘数、手续费、保证金、可用资金、单日亏损和权益高水位。
- [x] 减仓先于开仓，成交驱动持仓和资金记账。
- [x] 标准与压力情景的账户模拟均未触发保证金拒绝或硬停机。
- [x] 样本外区间已经被观察，不再标记为纯净样本外。
- [x] 历史 evidence 如实保留当时 `production_wiring=false`，没有倒写成当时已接入。
- [x] 历史 evaluator 和组装矩阵显式固定 `historical_research_only`，并拒绝 live、扩大风险或 prospective 授权；`passed` 不等于 activation。
- [x] 完整输入、分段结果和失败路线已保存在 [`stress90-final-evidence.md`](stress90-final-evidence.md)。
- [ ] 在目标 workspace 用五个固定输入重新完成 Base、batch/incremental 逐日 parity，并得到 candidate SHA `8e38dbf...9f28`。
- [ ] 新发生、此前未参与任何选择/调参的未来数据持续验证。
- [ ] 未来显著恶化时优先降低或关闭风险，不在同一历史上无限调参。

## B. 当前生产风险门认知

- [x] 单日亏损 5% 是同一柜台交易日内的熔断，不会自动取消其他风险门。
- [x] 总回撤 30% 仍然触发需要人工处理的硬停机。
- [x] 保证金不超过 35%、可用资金不低于 25% 仍是硬限制。
- [x] 正常目标保证金份额最高约 30%，完整交易日冲击只会进一步收缩；它不替代或放宽 35%/25% 硬限制。
- [x] 目标总敞口和实际总敞口都不超过权益的 2 倍。
- [x] 单合约最多 35 手。
- [x] 只有实际总敞口超限才进入只减仓；保证金目标和总敞口保护分别约束资金占用和名义风险。
- [x] 没有为恢复历史收益而放宽单日亏损、总回撤、保证金、现金或杠杆限制。
- [ ] 未来调整风险阈值前先取得新的 Shadow、测试柜台和小资金证据。

### B.1 Stress-90 当前代码 wiring（不构成 activation）

- [x] `directional.policy` 在生产配置中必须显式为 `execution_aligned` 或 `stress90`。
- [x] `stress90` 使用独立 manager/runtime adapter；旧 `execution_aligned` state 不会静默迁移成 Stress-90。
- [x] 普通模式保留 0.25 target scaling；Stress-90 使用 1x raw candidate，不被 `DirectionalRiskScaledPolicy` 重复包装。
- [x] 研究 batch、bootstrap incremental 和 live runtime 共用同一纯候选 primitives；live 不导入 `tools/` 或 acceptance CLI。
- [x] policy definition、固定历史 candidate 和 daily decision 使用三个不同 digest。
- [x] prepared decision 在第一张订单前 exactly once 落盘；重启复用同一 decision/intent。
- [x] 25% completed-path reserve 与 raw candidate HHI freeze 独立记录，均只冻结新风险。
- [x] raw CTP 60m evidence 在 Tick coalescing 前采集，callback 不做阻塞式 IO。
- [x] OHLC provider 被移出订单路径；live 只读 validated cache。
- [x] `status`、`doctor`、Shadow quality、activation/rebase 和 vendor comparator 已接线。
- [x] 当前 wiring 的实现边界已写入 [`stress90-live-productionization.md`](stress90-live-productionization.md)。
- [ ] 目标机 CTP ABI、真实 callback 顺序、实盘前置和账户环境已验证。
- [ ] 连续多日 Stress-90 Shadow 已通过。
- [ ] 测试柜台已通过。
- [ ] 极小真钱已通过。

## C. 配置与账户隔离

- [ ] 使用固定的 50 品种范围，不为追逐近期收益随意删改。
- [ ] 生产配置显式包含 `directional.policy = "stress90"` 和 `directional.account_exclusive = true`。
- [ ] `directional.account_continuity_mode` 已明确选择；默认和通用生产建议为 `strict`。选择 `operator_managed` 时已确认这是个人专用、单账户、账户独占的操作者信任模型，而非官方结算/session 证据。
- [ ] `directional.max_gross_leverage <= 2.0`。
- [ ] `directional.max_contract_volume == 35` 或更低。
- [ ] 方向组合不与固定跨期组合或 Auto 同时启用。
- [ ] 同一账户没有手工或其他程序交易，保持账户独占假设。
- [ ] 若使用 `operator_managed`，每次跨日 receipt 的 account/epoch/runtime、registry/TDE、完整 session ownership、journal、持仓对账、Deposit/Withdraw 和四项 no-external-activity assertion 均有效；`orders_sent=0`、`cancels_sent=0`。
- [ ] 若账户发生人工交易、外部委托、入金、出金或无法解释的资金变化，已先执行 `stress90-account-rebase`，没有把外部资金变化计入策略收益。
- [ ] operator roll-forward 后 runtime 仍为 `HALTED` 且 kill switch 开启，已重新运行 `status`/`doctor` 并签发新 technical permit；外部 activation blockers 仍单独保留。
- [ ] 目标交易所和品种权限已开通。
- [ ] `rebalance_window` 经过测试柜台验证。
- [ ] Stress-90 不用宽泛 `rebalance_window` 追单；每个产品只在固定 session manifest 的首个 entry window 增加风险。
- [ ] 保证金、可用资金、单日亏损和总回撤限制已按真实承受能力确认。

## D. 上一完整交易日的流动性快照

- [ ] CTP 合约目录覆盖固定的 50 个品种。
- [ ] 连续观察至少一个完整柜台交易日，生成 `directional_activity.json`。
- [ ] 快照中的成交量和持仓量等于上一完整交易日最后可见值。
- [ ] `doctor` 复用正式选约器；合约、品种和交易所身份与 CTP 目录一致，且 50 个配置品种都有通过到期日、成交量和持仓量门槛的合约。
- [ ] 次交易日当前累计成交量和持仓量不会改变已冻结主力。
- [ ] 挂牌日、到期日和距到期 20 天过滤正确。
- [ ] 已持有合约的保留条件和挑战合约的成交量/持仓量双重优势与离线重建一致；没有已持有合约时按持仓量、成交量、到期日和合约代码依次排序。
- [ ] 新部署没有上一完整交易日快照时不会新增风险。
- [ ] 日内重启后能恢复最近成功批次 checkpoint 的 `in_progress` 和最近一份 `completed`；交易日切换和正常停机会强制落盘。
- [ ] 旧版无 schema/checksum 的裸 activity 文件已保留诊断副本并通过完整交易日 Shadow 重建，没有手工伪造迁移。
- [ ] 流动性快照落后于最新完整信号交易日时拒绝增加风险。

## E. 信号日期、策略状态和仓位收缩

- [ ] 50 个品种的连续合约开高低收数据在 Shadow 中连续多日成功更新。
- [ ] 最新完整价格历史覆盖 `completed_activity_snapshot.trading_day`。
- [ ] 周末和节假日按所需柜台交易日处理。
- [ ] 普通交易日缺少完整日线时，即使未超过小时上限也拒绝增加风险。
- [ ] `directional_ohlc_cache.json` 的 schema、固定 50 品种顺序、共享日期索引、开盘/收盘 shape、正有限值、content digest 和 envelope checksum 全部通过。
- [ ] provider 的开盘/收盘索引在转换前均为无时区自然日午夜且完全一致；没有 aware/naive 混用、非午夜时间或越过权威当前交易日的行。
- [ ] provider 的全部允许行都已验证为正有限值并可无损表示为 `float64`；首次保存、重启和等值 dtype 变化使用相同规范表示。
- [ ] 新 provider 历史保留已验证缓存的每个日期，开盘/收盘值完全一致；删日期或静默修订不会覆盖 last-known-good。
- [ ] 数据提供方临时失败但已验证缓存仍覆盖所需 completed activity day 时可以继续。
- [ ] 缓存缺失、损坏或不覆盖 required day 时，空账户拒绝新增风险，有风险账户进入收缩路径。
- [ ] 所需信号缺失且账户为空时不新增风险。
- [ ] 所需信号或流动性快照缺失且仍有风险时进入 `REDUCE_ONLY`。
- [ ] 元组合仍使用固定的 96 个模板、11 日回看、每 3 日选择最多 3 个模板。
- [ ] 15 个基点压力证据只作为生存门；存活模板按标准成本评分排序，实盘期间不重新拟合历史参数。
- [ ] 完整日收益只在柜台交易日结束后写入状态。
- [ ] 当前交易时段尚未完成的盈亏不会提前进入风险收缩判断。
- [ ] 完整日亏损达到 2% 或两日波动达到 3% 时缩小到 25% 的规则在 Shadow 中可解释。

### E.1 Stress-90 候选与 exactly-once state

- [ ] `stress90_bootstrap_seed.json` 的五份 source SHA、policy digest、50/9 品种 manifest 和 through day 全部通过。
- [ ] `stress90_policy_state.json` 的 schema、sequence、checksum、seed identity 和 `.prev` 证据全部通过；current 损坏时绝不回退。
- [ ] 每个 target day 的 Base、OI、cost、survivor 和 daily decision digest 可重算且一致。
- [ ] OI 支持品种严格为 `A,C,EG,I,M,P,PP,TA,Y`，其余 41 品种保持 Base target。
- [ ] missing/incomplete OI 与合法 `flow=0` 在 status、doctor 和 state 中可区分。
- [ ] 成本门严格使用 completed `20/3/15bp`，只阻止 entry 和同向 add。
- [ ] survivor reallocation 不创造 support、不改方向、不超过原 gross/2x，并满足 L1/turnover/tie-break 顺序。
- [ ] HHI 使用 raw survivor product weights，先比较 strictly-prior median 再追加；同日重复运行不会重复追加。
- [ ] state 已有 prepared decision 时，重启和重复 `run_once()` 复用相同 digest，不重新推进 target。
- [ ] seed 到 current target 之间没有跳过中间 target day；gap 时空仓拒绝新增风险、有仓 `REDUCE_ONLY`。
- [ ] historical seed 没有继承回测账户收益；live wealth/HWM 从真实 inception 开始。

## F. 保证金约束、先减后开和总敞口限制

- [ ] 实盘目标使用 Broker 返回的多空保证金率、中间价、合约乘数和缓冲计算逐手保证金。
- [ ] 当前 35%/25%/5% 配置下，平静期目标保证金份额约为 30%；完整日冲击高于 3% 时只会因果收缩。
- [ ] 缺少正的保证金证据时拒绝开仓，不猜测保证金率。
- [ ] 整数手数拟合不会增加任一请求手数。
- [ ] 缩量后的开仓单仍经过 `RiskManager.check_open_orders()` 的 35%/25% 硬限制。
- [ ] 目标为零、反转和超额风险都先减仓。
- [ ] 某个新目标没有合格合约时，不会阻塞其他产品必要减仓。
- [ ] 不可用产品的已有仓位不加仓、不换月。
- [ ] 所有减仓都由 Broker 确认后，下一轮才允许开仓。
- [ ] 存在活动委托时，调仓和总敞口保护不重复发单。
- [ ] 按市价标记的实际总敞口超过 2 倍权益时，只生成减仓 FAK。
- [ ] 总敞口减仓完成后，实际总敞口不超过 2 倍权益。
- [ ] 无法安全计算或执行总敞口减仓时拒绝增加风险。
- [ ] 同一合约同时存在多头和空头毛仓时分别平仓，不因净仓为零漏掉风险。
- [ ] 减仓 FAK 未成交或部分成交后，下一轮以 Broker 真实持仓重新计算。

## G. 单日风控熔断和硬停机

- [ ] 单日亏损达到 5% 时当日退出风险并禁止重新加仓。
- [ ] 同一柜台交易日不会自动恢复。
- [ ] 下一柜台交易日只有 Broker 就绪、没有活动委托、风险已清空、合约参数、账户限制和持仓对账全部通过才恢复。
- [ ] 总回撤、保证金、可用资金或非正权益问题不走单日熔断的自动恢复路径。
- [ ] Kill Switch 不会被跨日逻辑错误清除。

## H. Shadow

- [ ] `afuture shadow --config config/afuture.directional-live.example.toml` 连续运行多个真实交易日。
- [ ] Shadow 真实读取 CTP 合约目录、行情、柜台交易日和合约参数。
- [ ] Shadow 不调用真实 CTP `send_order()`。
- [ ] 每日信号日、流动性日、所选合约、原始目标和保证金缩量目标可解释。
- [ ] 模型逐手保证金与 Broker 合约参数的差异可解释。
- [ ] 目标总敞口和实际总敞口的差异可解释。
- [ ] 总敞口保护的触发、完成和剩余风险可解释。
- [ ] depth 足以覆盖计划手数。
- [ ] 保证金、可用资金、单日亏损和总回撤状态可解释。
- [ ] 计划与实际换手、滑点、手续费和目标偏差有稳定记录。
- [ ] 历史固定保证金假设与真实逐日保证金表的差异得到重点复核。
- [ ] Stress-90 Shadow 已在 `runtime/shadow/` 单独 bootstrap/activate，policy/OI/intent/account state 不与 live 共用。
- [ ] raw evidence expected/observed/missing contract coverage 覆盖九个 OI 品种的所有 eligible futures contracts。
- [ ] 夜盘跨午夜、60m boundary、volume reset、duplicate/out-of-order/late Tick、rollover 和重启证据可解释。
- [ ] CTP/vendor comparator 的 first open、last close、first/last hold、volume、dominant 和 flow 逐日可解释；没有 unexplained flow difference。
- [ ] 每日 quality 含 Base/OI/cost/survivor、HHI/prior median、两个 freeze、全部 integer stages 和 reduction/opening plan。

## I. Doctor / 测试柜台

- [ ] `afuture status` 当前 state、previous evidence、路径和磁盘检查全部通过。
- [ ] `afuture doctor --confirm-live` 的新快照、账户交易日、保证金、可用资金、单日亏损、总回撤、活动委托、合约目录、合约参数、停机开关、运行状态、持久化安全门、持仓对账和流动性检查全部通过，且 `orders_sent=0`。
- [ ] `status` 显示 Directional OHLC cache 的 ordered products manifest、verified digest/latest date/shape，`doctor` 显示 required activity day 已覆盖；两个命令都不抓取外部 OHLC。
- [ ] 最终目标机的 `vnpy_ctp` 原生扩展在同一 Python/OS/CPU ABI 下可导入，并完成真实前置登录、无报单 doctor、Shadow 与断线重连；通用 CI 替身不作为该项证据。
- [ ] Tick 洪峰下关键 FIFO 仍优先投递；`delivery_counters()` 的 critical backlog 不持续增长，tick coalescing 与默认 100 条 poll 上限符合容量预期。
- [ ] 单方向 FAK 开仓：对手一档深度覆盖整笔手数时使用当前最优对手价，否则使用原有保守主动价。
- [ ] 覆盖 FAK 未成交、部分成交和拒单；基于深度的开仓优化不得改变减仓价格、订单数量或硬风控权限。
- [ ] 平仓与平今/平昨。
- [ ] 多产品同时报单仍满足频率限制。
- [ ] 换月减仓完成前不新增风险。
- [ ] 区分多空保证金率的目标手数与柜台冻结保证金一致，或差异可解释。
- [ ] 实际总敞口保护真正通过 CTP 减仓并回到限额内。
- [ ] 同一合约多空并存时能够分别退出毛仓。
- [ ] 单日熔断在下一柜台交易日的恢复条件正确。
- [ ] 未知委托、成交或持仓漂移触发安全停机。
- [ ] 断线、行情陈旧、快照陈旧状态正确。
- [ ] `REDUCE_ONLY` 只减仓。
- [ ] 查询和报单频率不会使柜台过载。
- [ ] Broker 保证金和手续费与合约参数及结算单一致。
- [ ] Doctor 显示 Stress-90 policy/seed/state identity、target continuity、OHLC/OI/activity alignment、九品种 coverage 和完整 integer preview，且 `stress90_ready=true`。
- [ ] Doctor 逐计划合约估算开仓、平昨、平今、1 tick、spread 和 depth；确定性最低成本显著高于 15bp 的合约保持 activation blocker。
- [ ] 真实成本不兼容时使用独立命名的新矩阵重跑，没有动态修改固定 Stress-90 cost hurdle。

## J. 重启和状态真相

- [ ] 每次第二次及后续 state save 产生 `<state>.prev`，内容是上一份通过 checksum 的 envelope。
- [ ] 损坏 current state 时程序返回失败，绝不自动采用 `.prev`。
- [ ] 当前状态非 UTF-8、包含重复 `(symbol, exchange)` 持仓身份，或柜台与本地持仓交易所不一致时拒绝继续运行；同代码跨交易所不会碰撞。
- [ ] 成交幂等键使用 `(trading_day, exchange, trade_id)` 并在 CTP 启动前恢复；旧 `(trading_day, trade_id)` 命中时显式停机对账，不静默吞掉跨交易所同 ID 新成交。
- [ ] CTP 当前交易日确实来自交易 API `getTradingDay()`；缺少 gateway/td_api/getter/合法值时失败关闭，不回退本机自然日。
- [ ] 延迟的较旧 account event 不会让交易日倒退或重分今/昨仓；状态保持不变并失败关闭。
- [ ] audit/alert JSONL 到达 20 MiB 后在完整记录边界轮转，最多保留 14 份备份。

- [ ] 正常退出前 StateStore 已保存最新 expected positions。
- [ ] 重启后 RuntimeState 与 Broker 完整持仓一致时 reconciled。
- [ ] 任一合约今昨仓或多空方向不一致时拒绝继续运行。
- [ ] 最近完整日收益和单日熔断标记能正确跨重启恢复。
- [ ] directional 没有第二份独立策略仓位可与 Broker 漂移。
- [ ] Kill Switch 只有在全部安全条件确认后解除。
- [ ] Stress-90 prepared decision 后、第一张订单后和部分成交后三类崩溃均复用同一 decision，并以 Broker 持仓继续收敛。
- [ ] policy switch 只在 `HALTED`、Broker/local 空仓、无活动委托、reconcile 和强确认后完成，且完成后仍保持 kill switch。
- [ ] 充值、出金或更换账户只通过带唯一 `--operation-id` 的 `stress90-account-rebase`；同账户有 verified nonzero cash flow，rebase 有 operator reason/audit 且完成后仍 `HALTED`。
- [ ] activation/rebase/migrate 在 account lease 内完整审计 current/archive/sealed CTP order journal；任一损坏或 pending cleanup 都是 P0。
- [ ] journal 达到 identity 容量时只通过 `stress90-order-journal-rollover` 封存；命令要求 HALTED、空仓、无活动委托、reconcile、强确认，且跨 epoch identity 不可重用。

## K. 账户规模与整数手数

- [ ] 用真实权益、合约乘数、当前价格和多空保证金率复核目标整数手数。
- [ ] 小资金不会因整数手数长期把组合压成极少数产品。
- [ ] 35 手上限不会产生不可接受的目标偏差。
- [ ] 实际保证金缩量后的总敞口可解释。
- [ ] 30% 正常目标保证金与真实账户波动之间仍有足够安全余量。
- [ ] 账户规模不足时降低风险/复杂度，不放宽硬门。
- [ ] 不通过超过 2 倍杠杆追逐历史收益。

## L. 方向组合执行质量

- [ ] `directional_rebalance` 持续记录信号日、流动性日、目标和计划换手。
- [ ] `directional_fill` 只来自 Broker trade callback。
- [ ] 预期价格与成交价格之间的滑点基点正常。
- [ ] 真实手续费与结算单抽样一致。
- [ ] `directional_cycle` 记录实际换手、目标偏差、延迟、部分成交和拒单。
- [ ] `quality-report.directional` 每日可读。
- [ ] 实际换手没有系统性显著高于模型假设。
- [ ] 滑点 95 分位数没有吞掉大部分可兑现收益。
- [ ] expected open、planned order price、actual fill、one-way realized cost、p95 cost、completion latency 和 actual/model turnover 都可审计。

## M. 极小真实资金

- [ ] 从 1 手或最小合理风险开始。
- [ ] 测试规模即使完全损失也不影响整体资金安全。
- [ ] 连续多个交易日无 order/state/position 事故。
- [ ] 主力切换与换月正确。
- [ ] 保证金定仓、总敞口保护、单日熔断和硬停机都按预期执行。
- [ ] 实际成本与标准/压力情景的差异可解释。
- [ ] 实际回撤符合账户风险预算。

## N. 扩大风险前

只有以下全部满足才考虑扩大：

- [ ] 新发生未来数据没有持续推翻 Alpha。
- [ ] Shadow 稳定。
- [ ] 测试柜台稳定。
- [ ] 极小真实仓位稳定。
- [ ] execution quality 可接受。
- [ ] 经过整数手数、合约乘数和保证金约束后，实际组合没有严重偏离目标。
- [ ] 实盘回撤符合风险预算。
- [ ] 已重新评估单边 15 个基点、高保证金压力情景与真实执行的差异。

离线研究指标不能改变实盘风险权限，也不是未来收益承诺。

## O. 离线研究和实盘启用边界

- [x] 离线压力研究没有放宽 2 倍总敞口、35% 保证金、25% 可用资金、5% 单日亏损、30% 总回撤或 35 手限制。
- [x] 失败路线保留在证据文档，没有通过事后调整参数挽救。
- [x] 当前代码已通过独立设计把候选接成显式可选 runtime policy；历史 evidence 仍保留当时未接线的事实。
- [x] 代码测试覆盖启动预热、重启、陈旧数据、交易时段、换月、exactly-once 和线上/离线共享 primitives。
- [ ] 固定五份输入在部署 workspace 可用并完成完整 Stress-90/Stress-80 矩阵复现。
- [ ] 多日 CTP Shadow 解释历史 vendor 与 live 60m 数据源差异。
- [ ] 不以离线年化通过为由跳过 Shadow、测试柜台、小资金或未来新数据。

以下外部证据项不得因代码或 CI 绿色而勾选：目标机 CTP ABI、多日 Shadow、测试柜台、真实手续费、真实保证金、FAK 部分成交、真实断线重连、极小真钱和扩大资金。

`112.100053%` 不是未来收益承诺。96-template pool 存在已观察历史 selection bias；当前新发生数据才是真正 forward evidence。准确 commissioning 命令见 [`stress90-live-runbook.md`](stress90-live-runbook.md)。

## Stress-90 commissioning overlay and account capacity

- [ ] `status` and `doctor` show identical configured and bound risk-overlay digests.
- [ ] `directional.live_risk_scale` is an intentional commissioning value; no value above `1` is accepted.
- [ ] Any risk-overlay change was rebound only from `HALTED`, kill-switch=true, Broker/local flat, zero active orders and fresh reconciliation.
- [ ] A prior same-day execution intent was not reinterpreted under a new overlay.
- [ ] `stress90-capacity-report` returned `orders_sent=0` and `cancels_sent=0` using real CTP margin/commission evidence.
- [ ] One-lot notional, buffered long/short margin, one-tick/spread cost and 15bp compatibility were reviewed for every selected non-zero product.
- [ ] Integer zeroing, margin/funding clipping, HHI/drawdown freezes, product HHI, largest product share and tracking error are acceptable for commissioning.
- [ ] The commissioning example was adjusted from measured one-lot stress and actual account equity; no warning was used to relax hard risk.

## 部署来源与恢复验收

- [ ] production bootstrap bundle 由官方固定摘要与固定 candidate 生成，`stress90-bundle-verify` 明确通过；测试 fixture 没有进入生产路径。
- [ ] bundle 安装目标为空，安装后 seed/policy/OI/OHLC/activity identity 与 manifest 完全一致，未生成账户绑定、permit 或订单事实。
- [ ] 最终目标机、最终虚拟环境、最终 `vnpy_ctp` 已就绪后执行 `deployment-seal`；`deployment-verify` 对 Git HEAD、tracked source digest、配置、constraints、Python/OS/CPU/executable、runtime、registry、bundle、risk overlay 和 native module 均无漂移。
- [ ] production `status` 能显示 deployment 诊断；deployment 不匹配时 `doctor`/`shadow`/`live` 失败关闭，不能依赖旧 permit 继续运行。
- [ ] 至少完成一次 `HALTED` + `kill_switch=true` 的 `backup-runtime` 与独立 `verify-backup`；备份成员只有 allowlist 权威证据，不含配置、日志、报告、告警和原始凭证。
- [ ] 至少完成一次离线恢复演练：目标 runtime 为空、registry staging 独立、恢复过程零 Broker writes，恢复结果保持 `HALTED` + kill switch，旧 technical permit 失效。
- [ ] 恢复演练后按 `status` → `deployment-verify` → 无报单 `doctor` → fresh permit 完成重新准入；任何身份漂移都先停机解释，不手工复制 `.prev` 或修改 checksum。

## 盘前、托管与 Watchdog

- [ ] `deployment-verify` 与已安装 bundle/seed/policy/deployment identity 一致。
- [ ] `prepare-session --confirm-live --output <path>` 返回 0，JSON 明确 `orders_sent=0`、`cancels_sent=0`，continuity/rebase、alignment、Doctor P0 与 capacity 结果已人工复核。
- [ ] 如 preflight 要求 continuity roll-forward 或 account rebase，只执行报告列出的人工命令；完成后重新跑 `prepare-session` 和 `doctor`。
- [ ] capacity warnings 已人工确认，随后才签发新的 activation permit；不得由 systemd 或 watchdog 自动签发。
- [ ] `paths.heartbeat` 位于本机受保护目录，`heartbeat_interval_seconds` 为 1–60 秒，watchdog timer 能以无 CTP 凭证身份运行。
- [ ] `deploy/systemd/afuture-live.service` 未启用 `PrivateTmp=true`，机器私有 EnvironmentFile 不进入仓库；退出码 75 位于 `RestartPreventExitStatus`。
- [ ] `process_run.json` current/.prev 完整；若出现 unclean restart fence，保持 HALTED/kill switch，不通过重启循环绕过。
- [ ] 备份只在 HALTED 且 kill switch=true 时执行；备份失败不应停止或控制交易进程。
