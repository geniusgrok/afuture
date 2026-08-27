# 配置参考

本文是 TOML 配置的当前权威说明。示例文件用于起步，本文解释字段含义、默认值、单位和关键约束；运行前仍应执行 `afuture validate --config <配置文件>`。

比例均使用小数，例如 `0.35` 表示 35%。时间窗口使用中国期货本地时间 `HH:MM-HH:MM`，可以跨午夜。没有显式注明“必填”的字段使用代码默认值。

## 1. 运行模式与柜台

| 字段 | 默认值 | 含义与约束 |
| --- | --- | --- |
| `system.mode` | `replay` | `replay` 为历史回放，`live` 为连接 CTP；Shadow 也使用 `live` 配置，但不会向柜台报单。 |
| `system.initial_capital` | `500000` | 回放或本地模拟初始资金，单位为人民币元，必须大于 0。实盘账户资金以 Broker 快照为准。 |
| `ctp.environment` | `test` | `test` 或 `production`；只在 `system.mode=live` 时使用。 |
| `ctp.td_address` | 实盘必填 | CTP 交易前置地址。 |
| `ctp.md_address` | 实盘必填 | CTP 行情前置地址。 |

CTP 凭证不进入 TOML：

| 环境变量 | 是否必需 | 用途 |
| --- | --- | --- |
| `AFUTURE_CTP_USER` | CTP 连接必需 | 用户代码。 |
| `AFUTURE_CTP_PASSWORD` | CTP 连接必需 | 密码。 |
| `AFUTURE_CTP_BROKER` | CTP 连接必需 | 经纪公司代码。 |
| `AFUTURE_CTP_APP_ID` | 柜台要求时必需 | CTP App ID。 |
| `AFUTURE_CTP_AUTH_CODE` | 柜台要求时必需 | CTP 认证码。 |
| `AFUTURE_CTP_ACCOUNT_ID` | Stress-90 柜台连接必需 | 预期 AccountID；只用于精确身份校验和不可逆摘要，不写入日志。 |
| `AFUTURE_CTP_CURRENCY_ID` | Stress-90 柜台连接必需 | 预期 CurrencyID（通常为 `CNY`）；必须与原始账户响应精确一致。 |
| `AFUTURE_CTP_INVESTOR_ID` | 柜台提供时填写 | 预期 InvestorID；原始响应含此字段时必须精确一致。 |
| `AFUTURE_CTP_INVEST_UNIT_ID` | 柜台提供时填写 | 预期 InvestUnitID；原始响应含此字段时必须精确一致。 |
| `AFUTURE_LIVE_ACK` | 生产报单必需 | 必须为 `I_UNDERSTAND_FUTURES_RISK`，并同时传入 `--confirm-live`。 |
| `AFUTURE_RECOVERY_ACK` | 人工恢复必需 | 必须为 `I_VERIFIED_CTP_POSITIONS`，并同时传入 `--confirm-adopt-state`。 |
| `AFUTURE_STRESS90_ACTIVATION_ACK` | Stress-90 首次 identity 绑定必需 | 必须为 `I_CONFIRM_STRESS90_POLICY_ACTIVATION`，并同时传入 `--confirm-activation`。 |
| `AFUTURE_STRESS90_REBASE_ACK` | Stress-90 账户路径 rebase 必需 | 必须为 `RESET_STRESS90_ACCOUNT_PATH`，并同时传入 `--confirm-rebase`。 |
| `AFUTURE_STRESS90_ORDER_EPOCH_ACK` | Stress-90 CTP order journal 容量 epoch 封存必需 | 必须为 `I_CONFIRM_STRESS90_CTP_ORDER_JOURNAL_EPOCH_ROLLOVER`，并同时传入 `--confirm-rollover`。 |
| `AFUTURE_OPERATOR_CONTINUITY_ACK` | Stress-90 `operator_managed` 跨日必需 | 必须为 `I_CONFIRM_EXCLUSIVE_ACCOUNT_AND_NO_EXTERNAL_ACTIVITY`，并同时传入 `--confirm-operator-continuity`。 |

`stress90-activate`、`stress90-account-rebase`、`directional-policy-migrate` 和
`stress90-order-journal-rollover` 还要求 operator 生成的唯一 64 位十六进制
`--operation-id`。只有同一 prepared 事务的精确崩溃重试才能复用该值；新的资金流、账户切换、policy 迁移或 journal epoch 必须使用新值。

## 2. 账户和交易风险

| 字段 | 默认值 | 单位和作用 |
| --- | ---: | --- |
| `risk.max_margin_ratio` | `0.35` | 保证金占权益上限；超过时拒绝增加风险。 |
| `risk.max_daily_loss_ratio` | `0.01` | 当前柜台交易日相对日初权益的亏损限制。达到阈值即拒绝。 |
| `risk.max_total_drawdown_ratio` | `0.08` | 相对权益高水位的总回撤限制。达到阈值即拒绝。 |
| `risk.max_open_pairs` | `3` | 同时持有的跨期组合数上限。 |
| `risk.max_contract_volume` | `10` | 风控层允许的单合约总手数上限。方向组合还受自身同名限制。 |
| `risk.max_quote_age_seconds` | `10.0` | 行情相对决策时点的最大允许延迟，秒。 |
| `risk.max_leg_skew_seconds` | `2.0` | 跨期两腿时间戳最大差，秒。 |
| `risk.expiry_blackout_days` | `5` | 距到期日不超过该自然日数时禁止新增风险。 |
| `risk.min_available_ratio` | `0.50` | 可用资金占权益下限。 |
| `risk.margin_estimate_buffer` | `1.20` | 每手保证金估计乘数，不得小于 1。 |
| `risk.max_orders_per_minute` | `20` | 普通报单的一分钟频率上限。 |
| `risk.min_depth_multiple` | `2.0` | 对手一档数量至少应为计划手数的倍数，不得小于 1。 |
| `risk.max_bid_ask_ticks` | `4.0` | 买卖价差最大跳数。 |
| `risk.limit_distance_ticks` | `3.0` | 开仓价距涨跌停至少保留的跳数；0 表示只要求不越界。 |
| `risk.risk_budget_ratio` | `0.002` | 单个跨期组合的波动风险预算占权益比例。 |
| `risk.risk_sigma_multiplier` | `2.0` | 风险定仓使用的标准差倍数，不得小于 1。 |
| `risk.open_cooldown_minutes` | `0` | 每个交易时段开始后禁止新开仓的分钟数。 |
| `risk.close_blackout_minutes` | `0` | 每个交易时段结束前禁止新开仓的分钟数；不阻止必要平仓。 |

五个比例限制都必须位于 `(0, 1)`：`max_margin_ratio`、`max_daily_loss_ratio`、`max_total_drawdown_ratio`、`min_available_ratio` 和 `risk_budget_ratio`。方向组合示例使用的 35% 保证金、25% 可用资金、5% 单日亏损和 30% 总回撤不是全局默认值，而是该示例显式覆盖后的结果。

## 3. 执行与模拟

| 字段 | 默认值 | 单位和作用 |
| --- | ---: | --- |
| `execution.slippage_ticks` | `1` | 回放成交价额外滑点，跳。 |
| `execution.aggressive_ticks` | `1` | 限价单相对最优对手价的主动偏移，跳。 |
| `execution.auto_flatten_imbalance` | `true` | 跨期两腿不平衡时是否自动平掉已成交腿。 |
| `execution.legging_timeout_seconds` | `2.0` | 两腿成交等待超时，秒。 |
| `execution.conservative_simulation` | `false` | 是否启用盘口深度、延迟、部分成交和冲击模型。 |
| `execution.latency_ticks` | `0` | 保守模拟等待的行情事件数。 |
| `execution.market_impact_ticks` | `0` | 保守模拟的额外价格冲击，跳。 |
| `execution.require_live_metadata` | 回放为 `false`，实盘为 `true` | 是否必须用柜台元数据核对乘数、最小变动价位、保证金和手续费。 |
| `execution.metadata_timeout_seconds` | `10.0` | 等待柜台合约参数的最长时间，秒，必须大于 0。 |

这些参数不能替代真实成交证据。`slippage_ticks` 和 `market_impact_ticks` 只影响模拟；真实成交价始终来自 Broker 成交回报。

## 4. 运行文件和告警

| 字段 | 默认值 | 内容 |
| --- | --- | --- |
| `paths.state` | `runtime/state.json` | 带版本、序号和校验和的运行状态。 |
| `paths.log` | `runtime/afuture.log` | 人类可读日志。 |
| `paths.report` | `runtime/report.json` | 账户或运行报告。 |
| `paths.journal` | `runtime/audit.jsonl` | 追加式审计事件。 |
| `paths.alert` | `runtime/alerts.jsonl` | 本地告警事件。 |
| `alert.webhook` | 空 | 可选告警 Webhook；不要在 URL 中嵌入不必要的敏感参数。 |

相对路径以启动命令的当前目录为基准。生产环境应确保目录可写、磁盘空间充足且日志轮转正常。Directional 启用时还会在 `paths.state` 同一目录派生 `directional_activity.json` 和 `directional_ohlc_cache.json`。Stress-90 另派生 `stress90_bootstrap_seed.json`、`stress90_policy_state.json`、`stress90_oi_evidence.json` 和 `stress90_execution_intent.json`。这些文件各自带 schema/sequence/checksum；损坏当前文件时不得自动采用 `.prev`。

## 5. 固定跨期组合

每个 `[[contracts]]` 同时提供交易参数；带有 `product`、`expiry` 或 `listing` 时还可作为历史 Auto 回放的当时可见合约目录。

| 字段 | 默认值 | 含义与约束 |
| --- | --- | --- |
| `contracts.symbol` | 必填 | 合约代码，不能为空且不能重复。 |
| `contracts.exchange` | 必填 | 交易所代码，加载时转为大写。 |
| `contracts.multiplier` | 必填 | 合约乘数，必须大于 0。 |
| `contracts.price_tick` | 必填 | 最小变动价位，必须大于 0。 |
| `contracts.margin_rate_long` | 必填 | 多头保证金比例，必须位于 `(0, 1)`。 |
| `contracts.margin_rate_short` | 必填 | 空头保证金比例，必须位于 `(0, 1)`。 |
| `contracts.product` | 从合约代码推断 | 品种代码；Auto 历史回放建议显式填写。 |
| `contracts.expiry` | 空 | 到期日，ISO `YYYY-MM-DD`；Auto 回放必须提供。 |
| `contracts.listing` | 空 | 挂牌日，ISO `YYYY-MM-DD`；只用于历史时点目录重建。 |

手续费可以按手、按成交额或同时配置：

| 字段 | 默认值 | 含义 |
| --- | ---: | --- |
| `contracts.fee.open_fixed` | `0.0` | 每手开仓固定费用，人民币元。 |
| `contracts.fee.open_rate` | `0.0` | 开仓成交额费率。 |
| `contracts.fee.close_fixed` | `0.0` | 每手普通平仓固定费用。 |
| `contracts.fee.close_rate` | `0.0` | 普通平仓成交额费率。 |
| `contracts.fee.close_today_fixed` | `0.0` | 每手平今固定费用。 |
| `contracts.fee.close_today_rate` | `0.0` | 平今成交额费率。 |

每个 `[[pairs]]` 描述一个同品种的近月/远月组合：

| 字段 | 默认值 | 含义与约束 |
| --- | --- | --- |
| `pairs.pair_id` | 必填 | 组合唯一标识。 |
| `pairs.near_symbol` | 必填 | 近月合约。 |
| `pairs.far_symbol` | 必填 | 远月合约，必须与近月属于同一品种且不能相同。 |
| `pairs.exchange` | 必填 | 必须与两腿合约参数一致。 |
| `pairs.volume` | 必填 | 最大允许手数，必须大于 0；实际手数仍会被风险预算缩小。 |
| `pairs.lookback` | `60` | 统计窗口样本数，至少 2。 |
| `pairs.entry_z` | `2.0` | 开仓偏离阈值。 |
| `pairs.exit_z` | `0.5` | 回归退出阈值；必须满足 `0 <= exit_z < entry_z < stop_z`。 |
| `pairs.stop_z` | `4.0` | 紧急退出阈值。 |
| `pairs.sample_seconds` | `0` | 最小采样间隔，秒；0 表示每个合格同步事件均可采样。 |
| `pairs.expiry_near` | 空 | 近月到期日；固定组合实盘必填。 |
| `pairs.expiry_far` | 空 | 远月到期日；固定组合实盘必填且晚于近月。 |
| `pairs.max_holding_samples` | `120` | 最大持有样本数；0 表示不按持有期退出。 |
| `pairs.structural_mean_shift_z` | `3.0` | 绝对价差均值结构变化阈值，必须大于 0。 |
| `pairs.structural_vol_ratio` | `2.5` | 当前波动相对入场波动阈值，必须大于 1。 |
| `pairs.min_net_edge` | `0.0` | 扣除手续费、滑点和缓冲后的最小人民币净边际。 |
| `pairs.legging_buffer` | `0.0` | 两腿不同步成交的额外人民币成本缓冲。 |
| `pairs.risk_group` | 空 | 风险分组；Auto 使用品种代码。 |
| `pairs.session_windows` | 空 | 允许开仓的交易时段列表；固定组合实盘必填。 |
| `pairs.signal_transform` | `spread` | `spread` 使用绝对价差；`log_ratio` 使用价格对数比。 |
| `pairs.confirm_entry` | `false` | 是否要求极端偏离后回撤确认才开仓。 |
| `pairs.confirmation_retrace_z` | `0.0` | 开启确认时要求的最小回撤幅度，Z 值。 |
| `pairs.min_confirmed_entry_z` | `0.0` | 确认后仍需保留的最小偏离，必须位于 `(0, entry_z)`。 |
| `pairs.entry_trend_window` | `6` | 入场趋势斜率窗口，至少 2 个样本。 |
| `pairs.max_entry_z_slope` | `999.0` | 允许的入场 Z 值绝对斜率上限。 |
| `pairs.min_stationarity_score` | `0.0` | 最小均值回归稳定度，范围 `[0, 1]`。 |
| `pairs.max_half_life` | `999.0` | 最大估计半衰期，样本数，必须大于 0。 |
| `pairs.daily_sample_window` | 空 | 每个交易日允许取样的单一窗口；开始时刻必须早于结束时刻。 |

同一合约不能被多个固定组合重复使用。确认入场、稳定度和半衰期字段也由 Auto 复制到动态组合。

## 6. Auto 自动选择跨期组合

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `auto.enabled` | `false` | 启用动态选组。不能与 Directional 同账户启用；当前引擎允许与固定跨期组合并存。 |
| `auto.products` | `m, rb, TA, c, p` | 允许品种；`*` 表示全部已验证目录品种。 |
| `auto.exchanges` | `DCE, SHFE, CZCE` | 允许交易所。 |
| `auto.max_active_pairs` | `2` | 同时激活的组合总数。 |
| `auto.max_pairs_per_product` | `1` | 每个品种的组合数上限。 |
| `auto.max_contracts_per_product` | `3` | 每个品种用于构造相邻月份组合的最早到期合约数，至少 2。 |
| `auto.min_days_to_expiry` | `20` | 合约距到期日的最小自然日数。 |
| `auto.scan_interval_seconds` | `30.0` | 重新扫描候选的最小间隔，秒。 |
| `auto.max_sync_seconds` | `2.0` | 两腿样本最大时间差，秒。 |
| `auto.lookback` | `60` | 统计窗口样本数。 |
| `auto.entry_z` | `2.0` | 开仓阈值。 |
| `auto.exit_z` | `0.5` | 回归退出阈值。 |
| `auto.stop_z` | `4.0` | 紧急退出阈值。 |
| `auto.max_pair_volume` | `3` | 动态组合最大允许手数。 |
| `auto.sample_seconds` | `60` | 最小采样间隔，秒。 |
| `auto.max_holding_samples` | `120` | 最大持有样本数。 |
| `auto.structural_mean_shift_z` | `3.0` | 均值结构变化阈值。 |
| `auto.structural_vol_ratio` | `2.5` | 波动结构变化倍率。 |
| `auto.legging_buffer` | `0.0` | 两腿成交成本缓冲，人民币元。 |
| `auto.signal_transform` | `spread` | `spread` 或 `log_ratio`。 |
| `auto.confirm_entry` | `false` | 是否要求回撤确认。 |
| `auto.confirmation_retrace_z` | `0.0` | 确认回撤幅度。 |
| `auto.min_confirmed_entry_z` | `0.0` | 确认后最小残余偏离。 |
| `auto.entry_trend_window` | `6` | Z 值斜率窗口。 |
| `auto.max_entry_z_slope` | `999.0` | 入场 Z 值绝对斜率上限。 |
| `auto.daily_sample_window` | 空 | 每日采样窗口。 |
| `auto.min_volume` | `5000.0` | 两腿当前累计成交量下限。 |
| `auto.min_open_interest` | `10000.0` | 两腿持仓量下限。 |
| `auto.min_liquidity_score` | `0.5` | 流动性评分下限，范围 `[0, 1]`。 |
| `auto.min_stationarity_score` | `0.02` | 均值回归稳定度下限，范围 `[0, 1]`。 |
| `auto.max_half_life` | `120.0` | 最大半衰期，样本数。 |
| `auto.min_net_edge` | `0.0` | 净边际必须严格高于此人民币金额。 |
| `auto.slippage_ticks` | `1` | 候选净边际估算使用的单腿滑点，跳。 |
| `auto.metadata_timeout_seconds` | `10.0` | 等待动态合约参数的最长时间，秒。 |
| `auto.session_windows` | 日盘三个窗口 | 允许开仓时段；Auto 实盘必须显式配置。 |

## 7. Directional 方向组合

| 字段 | 默认值 | 含义与约束 |
| --- | --- | --- |
| `directional.enabled` | `false` | 启用方向组合；启用后不能同时配置固定组合或 Auto。 |
| `directional.policy` | 回放兼容为 `execution_aligned` | 只允许 `execution_aligned` 或 `stress90`。`system.mode=live` 时必须显式填写，不能由默认值决定生产经济行为。 |
| `directional.products` | 空 | 允许品种，启用时不能为空。 |
| `directional.exchanges` | `DCE, CZCE, SHFE, INE` | 允许交易所。 |
| `directional.max_gross_leverage` | `2.0` | 目标总名义敞口上限，范围 `(0, 2]`。 |
| `directional.min_days_to_expiry` | `20` | 选约时距到期日的最小自然日数。 |
| `directional.min_volume` | `1000.0` | 上一完整交易日成交量下限。 |
| `directional.min_open_interest` | `5000.0` | 上一完整交易日持仓量下限。 |
| `directional.max_contract_volume` | `35` | 方向组合目标的单合约手数上限。最终还取该值与 `risk.max_contract_volume` 的更严格者。 |
| `directional.rebalance_window` | `21:00-21:10` | 允许产生方向调仓的中国期货本地时间窗口，可跨午夜。 |
| `directional.signal_max_age_hours` | `36.0` | 信号时间戳的第二层最长年龄，小时；交易日对齐仍是主要新鲜度判断。 |
| `directional.account_exclusive` | `true` | 必须保持 `true`；同一账户不能混入手工或其他程序持仓。 |
| `directional.account_continuity_mode` | `strict` | `strict` 保持官方结算/session 证据门；`operator_managed` 仅允许 `system.mode=live`、Stress-90、directional enabled 且账户独占，用操作者信任凭证推进跨日，但不伪造 Broker 验证字段。 |

`stress90` 还强制以下配置身份：

- `products` 必须与固定 50 品种 manifest 完全一致；
- manager 使用 1x candidate 和 freeze-only risk response，不能再套普通模式的 0.25 target scaling；
- `max_gross_leverage <= 2.0`、`max_contract_volume <= 35`；
- `[risk]` 不得宽于 margin 35%、available 25%、daily loss 5%、total drawdown 30%、margin buffer 1.25 和 35 手；
- `account_exclusive = true`；运行期间禁止手工交易、其他策略、充值和出金；
- `account_continuity_mode` 默认 `strict`；`operator_managed` 只适用于个人专用、单账户、账户独占运行。其 receipt 明确标记为 `operator_trust`，不能替代官方结算见证或官方 session ledger；
- `operator_managed` 期间一旦发生人工交易、外部委托、入金或出金，必须保持停机并先执行现有 `stress90-account-rebase`；跨日成功后仍需新的 Doctor 技术 permit；
- `rebalance_window` 只保留旧 manager 的外层兼容校验，实际新风险 entry 使用不可变的产品/交易所 first-session manifest；错过首个窗口当日不追单。

更严格 commissioning 值可以减少风险，但会偏离固定历史矩阵。`status`/`doctor` 会显示该差异；不得把更严格配置下的 live/Shadow 结果表述为历史 Stress-90 的精确复现。

## 8. 配置选择与修改纪律

- 回放从 `config/afuture.example.toml` 或 `config/afuture.auto-replay.example.toml` 开始；CTP 测试从对应 live 示例复制，不直接编辑仓库示例。
- Stress-90 从 `config/afuture.directional-stress90-live.example.toml` 复制；该示例只表达 wiring 和 hard envelope，不构成 activation 许可。
- 修改手续费、合约乘数、保证金、滑点、杠杆、风控阈值、信号参数或交易窗口会改变经济行为，应重新运行受影响回测和验收。
- 路径、日志和无行为影响的说明性修改只需运行配置、文档和相关运维测试。
- 所有可执行配置节和手续费子项都拒绝未知键；布尔值必须是真正的 TOML boolean，整数不能经有损浮点转换，所有经济数值必须有限。`afuture validate` 失败时应修正配置，不得通过字符串强转、`nan` 或忽略拼写错误继续运行。

策略语义见 [`strategies.md`](strategies.md)，输入字段见 [`data-formats.md`](data-formats.md)，通用上线操作见 [`live-trading.md`](live-trading.md)，Stress-90 lifecycle 见 [`stress90-live-runbook.md`](stress90-live-runbook.md)。

## Stress-90 live risk scale and overlay identity

`directional.live_risk_scale` defaults to `1.0` and must be finite with `0 < value <= 1`. Only the Stress-90 live/Shadow production lot path consumes it; replay, bootstrap, historical candidate generation and `execution_aligned` ignore it. The production risk-overlay digest includes the scale, directional gross/contract/session-entry limits and all `RiskConfig` fields. A digest change is an identity change, not a hot reload: live/status/Doctor fail closed until explicit HALTED activation/reactivation rebinds it.

The checked-in Stress-90 live example is intentionally a personal commissioning starting point (`live_risk_scale=0.05`, one-lot contract caps, 10% margin, 80% available cash, 1% daily loss and 5% total drawdown). These values are not Alpha parameters or permanent policy requirements. Recalibrate them from real one-lot stress loss, live margin/commission and account equity before committing more capital.
