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
- [x] 当前离线研究候选没有自动接入实盘。
- [x] 完整输入、分段结果和失败路线已保存在 [`stress90-final-evidence.md`](stress90-final-evidence.md)。
- [ ] 新发生、此前未参与任何选择/调参的未来数据持续验证。
- [ ] 未来显著恶化时优先降低或关闭风险，不在同一历史上无限调参。

## B. 当前生产风险门认知

- [x] daily loss 5% 是同 trading-day circuit，而不是自动取消风险门。
- [x] total drawdown 30% 仍是 hard/manual halt。
- [x] max margin 35% / min available 25% 仍是 hard gate。
- [x] adaptive soft target share 的平静上限为 30%，completed shock 只可进一步收缩；它不替代或放宽 35%/25% hard gates。
- [x] target gross 与 actual gross 都有 2.0x 硬边界。
- [x] 单合约 max volume = 35。
- [x] actual gross 超限才 reduction-only；margin target 与 gross guard 分别约束保证金和名义风险。
- [x] 没有为恢复历史数字放宽 daily-loss / DD / margin / cash / leverage。
- [ ] 任何未来风险阈值调整先有 Shadow/test/small-capital 新证据。

## C. 配置与账户隔离

- [ ] 使用冻结 50 品种 Universe，不为追近期收益随意删改。
- [ ] `directional.max_gross_leverage <= 2.0`。
- [ ] `directional.max_contract_volume == 35` 或更低。
- [ ] directional 与 static pairs / Auto 不同时启用。
- [ ] 同一账户没有手工/其它程序交易破坏 account-exclusive 假设。
- [ ] 目标交易所和品种权限已开通。
- [ ] `rebalance_window` 经过测试柜台验证。
- [ ] margin / available / daily-loss / DD 已按真实承受能力确认。

## D. 上一完整交易日的流动性快照

- [ ] CTP catalog 覆盖冻结 50 品种。
- [ ] 连续观察至少一个完整 trading day，生成 `directional_activity.json`。
- [ ] snapshot volume/OI 等于上一完整交易日最后可见 activity。
- [ ] `doctor` 复用正式 activity selector，activity 的 symbol/product/exchange 与 catalog identity 一致，且 50 个配置 product 均有通过 expiry/volume/OI 门的覆盖。
- [ ] 次交易日当前累计 volume/OI 不会改变已冻结主力。
- [ ] listing/expiry/20 天过滤正确。
- [ ] incumbent eligibility + challenger OI/volume 双维 dominance 与离线重建一致；无 incumbent 时 OI → volume → expiry → symbol 排序一致。
- [ ] 新部署无 completed snapshot 时不会新增风险。
- [ ] 重启后能恢复最近 completed snapshot。
- [ ] completed activity 比最新完整 signal day 陈旧时 fail-closed。

## E. 信号日期、策略状态和仓位收缩

- [ ] 50 品种 continuous OHLC 在 Shadow 中连续多日成功。
- [ ] 最新 OHLC 覆盖 `completed_activity_snapshot.trading_day`。
- [ ] 周末/节假日按 required trading day 语义处理。
- [ ] 普通交易日漏完整 bar 时即使小时数未超限也拒绝。
- [ ] provider 临时失败但缓存已覆盖 required day 时可继续。
- [ ] required signal 缺失且账户为空时不新增风险。
- [ ] required signal/activity 缺失且有风险时进入 `REDUCE_ONLY`。
- [ ] meta 仍使用冻结 96 templates / lookback 11 / rebalance 3 / active 3。
- [ ] meta 的 15bp evidence 只做生存门；存活模板按 Base score 排名，不在实盘期间动态重拟合历史参数。
- [ ] completed daily returns 只在 trading day 完成后入 state。
- [ ] 当前 session PnL 不会前视进入 governor。
- [ ] -2% loss / 3% two-day volatility 的 25% defensive scaling 在 Shadow 可解释。

## F. 保证金约束、先减后开和总敞口限制

- [ ] live target 使用 Broker side-specific margin rate、mid、multiplier、buffer 计算逐手 margin。
- [ ] 当前 35%/25%/5% 配置下平静 target margin share 约为 30%，completed shock 高于 3% 时能因果收缩。
- [ ] 缺少正的 margin evidence 时 fail-closed，不用猜测 margin 开仓。
- [ ] margin fitter 不会增加任一 requested lot。
- [ ] margin-fitted openings 仍经过 `RiskManager.check_open_orders()` 35%/25% hard gates。
- [ ] target=0、反转、超额风险能先 reduction。
- [ ] 某个新目标无 eligible contract 时不会阻塞其它产品减仓。
- [ ] 不可用产品已有仓位不加仓、不换月。
- [ ] reductions 全部由 Broker 确认后，下一 cycle 才 opening。
- [ ] active order 存在时 rebalance/gross guard 不重复发单。
- [ ] actual marked gross `>2.0x` 时 gross guard 产生 reduction-only FAK。
- [ ] gross guard 完成后实际 gross `<=2.0x`。
- [ ] gross guard 无法安全计算/执行时 fail-closed。
- [ ] 同一合约同时有 long/short 毛仓时 flatten 两边都能平，不因 net=0 漏风险。
- [ ] reduction FAK 未成交/partial 后下一 cycle 以 Broker 真实持仓重算。

## G. 单日风控熔断和硬停机

- [ ] 5% daily-loss 触发当日 flatten 并禁止重新加风险。
- [ ] 同一 trading day 不自动恢复。
- [ ] 下一 CTP trading day 只有 Broker ready / no active orders / flat risk / metadata / account / reconcile 全通过才恢复。
- [ ] total DD / margin / cash / nonpositive equity 不走 daily-circuit 自动恢复。
- [ ] Kill Switch 不会被跨日逻辑错误清除。

## H. Shadow

- [ ] `afuture shadow --config config/afuture.directional-live.example.toml` 连续运行多个真实交易日。
- [ ] Shadow 真实读取 CTP catalog/tick/trading day/metadata。
- [ ] Shadow 不调用真实 CTP `send_order()`。
- [ ] 每日 signal day / activity day / selected contract / raw target / margin-fitted target 可解释。
- [ ] modeled per-lot margin 与 Broker metadata 差异可解释。
- [ ] target gross vs actual gross 差异可解释。
- [ ] gross guard 触发/完成/剩余 gross 可解释。
- [ ] depth 足以覆盖计划手数。
- [ ] margin / available / daily-loss / DD 状态可解释。
- [ ] planned vs realized turnover/slippage/commission/tracking 有稳定记录。
- [ ] 历史 proxy 与真实 margin schedule 的差异得到重点复核。

## I. Doctor / 测试柜台

- [ ] `afuture status` 当前 state、previous evidence、路径和磁盘检查全部通过。
- [ ] `afuture doctor --confirm-live` 的 fresh snapshot、account/trading day、margin/available/daily-loss/drawdown、active orders、catalog、metadata、Kill Switch/runtime mode、persisted gates、position reconciliation 和 activity 检查全部通过，且 `orders_sent=0`。
- [ ] 单方向 FAK 开仓；对手一档深度覆盖整笔手数时 opening 使用 best opposite，否则回退 legacy aggressive tick。
- [ ] FAK 未成交、partial、reject；depth-aware opening 不得改变 reduction aggressive 价格、订单数量或 hard-risk authority。
- [ ] 平仓与平今/平昨。
- [ ] 多产品 order-rate。
- [ ] 换月 reduction 完成前不新增风险。
- [ ] side-specific margin-aware sizing 与柜台冻结保证金一致或差异可解释。
- [ ] realized gross guard 真正通过 CTP 产生减仓并回到限额内。
- [ ] hedged gross-position flatten 正确。
- [ ] daily circuit 次交易日恢复正确。
- [ ] 未知 order/trade/position drift 触发安全停机。
- [ ] 断线、行情陈旧、快照陈旧状态正确。
- [ ] `REDUCE_ONLY` 只减仓。
- [ ] query/order 流控不过载。
- [ ] Broker margin/commission 与 metadata/结算单一致。

## J. 重启和状态真相

- [ ] 每次第二次及后续 state save 产生 `<state>.prev`，内容是上一份通过 checksum 的 envelope。
- [ ] 损坏 current state 时程序返回失败，绝不自动采用 `.prev`。
- [ ] current state 非 UTF-8、重复 position symbol，或柜台/本地 position exchange 不一致时 fail-closed。
- [ ] audit/alert JSONL 到达 20 MiB 后在完整记录边界轮转，最多保留 14 份备份。

- [ ] 正常退出前 StateStore 已保存最新 expected positions。
- [ ] 重启后 RuntimeState 与 Broker 完整持仓一致时 reconciled。
- [ ] 任一合约今昨/多空不一致时 fail-closed。
- [ ] recent completed returns / daily circuit marker 跨重启正确。
- [ ] directional 没有第二份独立策略仓位可与 Broker 漂移。
- [ ] Kill Switch 只有在全部安全条件确认后解除。

## K. Account sizing

- [ ] 用真实 equity、multiplier、当前价格和 side-specific margin 复核 integer target lots。
- [ ] 小资金不会因整数手数长期把组合压成极少数产品。
- [ ] 35 手 cap 不产生不可接受 tracking error。
- [ ] 实际 margin 缩量后的 gross 可解释。
- [ ] 30% soft target margin 与真实账户波动之间仍有足够 headroom。
- [ ] 账户规模不足时降低风险/复杂度，不放宽硬门。
- [ ] 不通过 leverage >2x 追历史收益。

## L. Directional execution quality

- [ ] `directional_rebalance` 持续记录 signal/activity day、target、planned turnover。
- [ ] `directional_fill` 只来自 Broker trade callback。
- [ ] expected vs fill price 的 slippage bps 正常。
- [ ] 真实 commission 与结算单抽样一致。
- [ ] `directional_cycle` 有 realized turnover、tracking error、latency、partial/reject。
- [ ] `quality-report.directional` 每日可读。
- [ ] realized turnover 没有系统性显著高于模型假设。
- [ ] p95 slippage 没有吞掉大部分可兑现 Alpha。

## M. 极小真实资金

- [ ] 从 1 手或最小合理风险开始。
- [ ] 测试规模即使完全损失也不影响整体资金安全。
- [ ] 连续多个交易日无 order/state/position 事故。
- [ ] 主力切换与换月正确。
- [ ] margin sizing / gross guard / daily circuit / hard halt 都按预期执行。
- [ ] 实际成本与 Base/Stress 差异可解释。
- [ ] 实际回撤符合账户风险预算。

## N. 扩大风险前

只有以下全部满足才考虑扩大：

- [ ] 新发生未来数据没有持续推翻 Alpha。
- [ ] Shadow 稳定。
- [ ] 测试柜台稳定。
- [ ] 极小真实仓位稳定。
- [ ] execution quality 可接受。
- [ ] integer/multiplier/margin 后组合没有严重漂移。
- [ ] 实盘回撤符合风险预算。
- [ ] 已重新评估 15bp/高 margin Stress 与真实执行差异。

离线研究指标不能改变实盘风险权限，也不是未来收益承诺。

## O. 离线研究和实盘启用边界

- [x] 离线压力研究没有放宽 2 倍总敞口、35% 保证金、25% 可用资金、5% 单日亏损、30% 总回撤或 35 手限制。
- [x] 失败路线保留在证据文档，没有通过事后调整参数挽救。
- [x] 当前离线研究候选没有接入实盘。
- [ ] 若把离线候选接入实盘，必须有独立设计，并验证启动预热、重启、陈旧数据、交易时段、换月和线上线下一致性。
- [ ] 不以离线年化通过为由跳过 Shadow、测试柜台、小资金或未来新数据。
