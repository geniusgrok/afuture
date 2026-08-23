# 生产上线检查表

这是**真实资金门**，不是代码完成清单。历史回测、production-mechanics proxy 和 GitHub CI 都不能代替真实 L1、测试柜台和未来未见数据。

当前代码级状态：previous-day activity、trading-day signal gate、stale activity fail-closed、reduction-first、margin-aware target sizing、daily circuit、completed-return governor、realized-gross hard guard、gross-position flatten、restart reconciliation、directional execution-quality 和 production-mechanics acceptance 均已实现。

当前经济证据：

- Float L4 Base：**107.4623% 年化 / 27.4097% 最大回撤 / target gross≤2x**，selection-biased；
- Production L3 Base：**109.0636% 年化 / 15.8529% 最大回撤 / actual gross peak 1.998253x / no permanent halt**；
- Production L3 Stress：**28.9559% 年化 / 28.1152% 最大回撤 / actual gross peak 1.668769x / 0 margin rejects / no permanent halt**。

因此 **Base 历史 production-mechanics ≥100% 已满足，Stress 的结构性 margin HALT 已修复，但 Stress 80% 收益目标未满足**。继续真实资金前仍必须完成本清单。

## A. 历史与账户机械证据

- [x] specific-contract 不使用 continuous roll jump 作为收益。
- [x] t→t+1 收益来自 t 日已选择同一具体合约。
- [x] 20 天交割黑窗。
- [x] signal target gross ≤2x。
- [x] Float Base 5bp 年化 107.4623%。
- [x] `pristine_final_oos=false` 与历史选择偏差明确记录。
- [x] Production L3 使用 frozen multipliers / integer lots / max contract volume 35。
- [x] Production L3 使用 margin/cash/daily-loss/high-watermark hard gates。
- [x] Production L3 使用 causal completed-return governor：-2% daily loss / 3% two-day sample vol → 25%。
- [x] Production L3 使用 actual realized-gross hard ceiling 2.0x。
- [x] Production L3 使用 adaptive margin-aware target sizing：无历史/平静基线约 30% equity，completed shock 高于 3% 时只继续收缩，35% hard margin gate 不变。
- [x] Production L3 输出 turnover attribution，并验证每个 bucket 求和等于总 turnover。
- [x] completed-activity 选约保留仍 eligible 的 incumbent；challenger 只有 OI 和 volume 同时更高才切换。
- [x] margin-fitted 同方向 `+1 lot` 增仓可在 incumbent 仍满足 soft margin / 2x gross 时保持不动；减仓与风险动作不被抑制。
- [x] **Production Base 年化 ≥100%：109.0636%。**
- [x] **Production Base 最大回撤 ≤30%：15.8529%。**
- [x] **Production Base actual gross ≤2x：1.998253x。**
- [x] **Production Base full_recent 不永久 HALT。**
- [x] **Production Stress full_recent 不永久 HALT：474/484 active days，0 margin rejects。**
- [x] Stress 未达到 80% 的结果被保留：28.9559% 年化。
- [ ] 新发生、此前未参与任何选择/调参的未来数据持续验证。
- [ ] 未来显著恶化时优先降低/关闭风险，不在同一历史上无限追参。

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

## D. Previous-day activity snapshot

- [ ] CTP catalog 覆盖冻结 50 品种。
- [ ] 连续观察至少一个完整 trading day，生成 `directional_activity.json`。
- [ ] snapshot volume/OI 等于上一完整交易日最后可见 activity。
- [ ] 次交易日当前累计 volume/OI 不会改变已冻结主力。
- [ ] listing/expiry/20 天过滤正确。
- [ ] incumbent eligibility + challenger OI/volume 双维 dominance 与离线重建一致；无 incumbent 时 OI → volume → expiry → symbol 排序一致。
- [ ] 新部署无 completed snapshot 时不会新增风险。
- [ ] 重启后能恢复最近 completed snapshot。
- [ ] completed activity 比最新完整 signal day 陈旧时 fail-closed。

## E. Signal / meta / governor

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

## F. Margin-aware sizing / reduction-first / gross guard

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

## G. Daily circuit / hard halt

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

- [ ] `afuture doctor` 登录、account、complete position、catalog、metadata 全部通过。
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

## J. Restart / state truth

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

107.4623% Float、109.0636% Production Base、28.9559% Production Stress 都是**已观察历史结果**。Stress 已不再早期 HALT，但没有达到 80%；这些数字均不是未来年度收益承诺。


## O. 本轮执行效率实验处置

- [x] product replacement persistence 已实现并跑固定 L3；因 Base 降至约 58.41% 而拒绝并回退。
- [x] cost-aware meta hysteresis 已实现并跑固定 L3；因 Base 约 58.32%、Stress -13.03%、DD 超 30% 且 HALT 而拒绝并回退。
- [x] same-direction weight resize hysteresis 已实现并跑固定 L3；未通过 promotion gate，已回退。
- [x] 最终晋级版本保持 Base ≥100%、Stress DD≤30%、no-HALT、gross≤2x、0 margin rejects。
- [ ] Stress 80% 仍是研究方向，不作为放宽硬门或重复拟合同一历史的理由。

## PR #15 net-alpha 研究收口门

- [x] Stress PnL / turnover / capacity attribution 与现金对账闭合；
- [x] entry/exit future labels 与 causal features 分离；
- [x] 未发现跨 prior1/prior2/train/validation/OOS 稳定负贡献的可因果 entry/exit cohort；
- [x] net-edge candidate 在 prior2 反向失效，未进入生产；
- [x] 新 Alpha families 未通过 15bp 多窗口门，未做失败策略混合；
- [x] shock-derived margin candidate 固定 Stress 仅 4.7970% 年化且 permanent HALT，拒绝；
- [x] 研究失败没有通过改 leverage、成本、margin proxy、风险硬门、数据时点或样本窗口包装成成功；
- [x] 最终生产经济行为保持 PR #14 基线；只保留行为中性审计/离线诊断与负证据。
