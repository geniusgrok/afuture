# Industrial Refactoring Validation Report — 2026-08-25

## Executive Summary

本次治理严格继承远端 `main` 的 `482455dc57bc6a134f45232e290b4a49c3f7073d`（PR #25）以及其祖先 Stress-80 checkpoint `b4207abb50aca1e39d5ebba3affc04765857251a`（PR #24），没有回退策略、参数、风险阈值或研究证据。

最终候选解决了会产生不可能持仓、伪造 CTP 经济语义、覆盖损坏状态、研究数据隐式选真值、非法账户先停机而不减仓、重复成交二次入账以及跨交易日首笔成交错桶等真实问题。改造保留现有 Calendar 与 Directional runtime facade，没有为了目录或复杂度指标进行框架化拆分。独立终审确认无剩余 P0/P1；最终本地工程门、481 项测试、CLI/replay smoke、Stress-80 与 Stress-90 冻结矩阵全部通过。

## Architecture Changes

- 保留 `TradingEngine`、`DirectionalTradingEngine`、acceptance simulator 和 CLI 的现有外观。事件顺序与经济循环高度内聚，盲目拆分的风险高于收益。
- 新增纯函数边界 `directional_data_validation.py`，集中校验 daily index、唯一键、有限值和窗口，不让 provider、runtime 与 evaluator 各自解释数据完整性。
- `StateStore` 的 load/save 共用经过 checksum、schema、sequence 和 payload 验证的 decoder；原子替换失败只清理明确创建的临时文件，不覆盖不可信目标。
- CTP adapter 在基础设施边界精确转换支持的 enum、整数数量和有限经济值。协议未知值不会进入 Order、Trade、Position 或 Account 领域对象。
- Broker fill 仍是唯一持仓真相。引擎新增有界、持久化、按交易日分域的成交身份集合，并把换日、去重、ownership、持仓更新和质量归因固定为可测试顺序。
- 依赖审计未发现 package import cycle；没有引入 Repository/Service/Factory/DI 层，也没有改变 live runtime 对 Stress-80/90 离线研究模块的隔离。

主链路保持为：行情/完成日数据进入 provider 或 Broker；策略/政策产生意图；RiskManager 和 portfolio gates 决定是否允许新增风险；executor 形成委托；Broker 回报成交；PositionBook、账户/风险状态、质量与 StateStore 消费成交真相；replay/acceptance 再形成绩效、stress 和审计报告。

## Correctness Fixes

下表中的修复都会改变原先的错误或不可信路径；每项均先由失败用例或确定性复现证明。对合法冻结输入的矩阵影响均为零。

| 失败场景 | 被保护的不变量 | 主要回归测试 | 修复后的行为 | 冻结矩阵影响 |
| --- | --- | --- | --- | --- |
| SHFE/INE 指定 `CLOSE_TODAY`/`CLOSE_YESTERDAY` 可把请求 bucket 减成负数 | close bucket 不得跨桶借量；失败前不得修改仓位 | `test_bucket_specific_close_rejects_insufficient_bucket_without_mutation` | 成交应用前检查对应今/昨、多/空 bucket；不足时显式拒绝 | 无变化 |
| 无效成交价格、非整数手数、空 identity 或非法初始均价进入 PositionBook | 数量、价格、identity 和 valuation 必须可审计 | `test_trade_rejects_invalid_*`、`test_position_book_rejects_invalid_identity_or_valuation` | Trade/ContractPosition 在任何持仓副作用前验证 | 无变化 |
| 损坏 JSON、checksum/schema/sequence/payload 可在下次 save 时被覆盖 | 不可信恢复状态不得被推进或替换 | `test_save_refuses_to_replace_*`、`test_load_rejects_*` | load/save fail-closed；原文件保持；只迁移合法 legacy state | 无变化 |
| 相同日期或合约 observation 被排序/`keep=last` 隐式选为真值 | 历史输入唯一、单调、有限；切分前不得污染 | `test_daily_index_rejects_invalid_ordering`、`test_unique_keys_rejects_duplicate_contract_observation` | provider/evaluator 在特征与窗口构建前拒绝歧义输入 | 无变化 |
| 完成 activity 过旧却掩盖更新的 required signal day | 决策日只能使用满足当前 completed-day 要求的活动 | `test_stale_completed_activity_cannot_hide_newer_completed_signal_day` | stale activity 显式阻塞，不再错误放行 | 无变化 |
| proxy 在当日开盘大涨后未先更新 high watermark，或已有 margin breach 仍先正常 rebalance | drawdown/margin 风险先于新增风险，reduction-first | `test_proxy_open_equity_updates_high_watermark_before_intraday_drawdown`、`test_proxy_existing_margin_breach_reduces_before_normal_rebalance` | 先标记账户风险并减仓/停机，再考虑 rebalance | 无变化 |

## Risk / Trading Fixes

| 失败场景 | 被保护的不变量 | 主要回归测试 | 修复后的行为 | 冻结矩阵影响 |
| --- | --- | --- | --- | --- |
| 未知 CTP direction/offset/type/status 被默认成 sell/close/limit/rejected | 不得把未知协议值伪造成有效经济事件 | `test_ctp_*_rejects_unknown_protocol_value*` | 只接受显式 enum 映射；未知值产生有上下文的 `broker_error` | 无变化 |
| tick/account/metadata/position 含 NaN、inf、负费率、非整数数量或非法均价 | 风控与会计只接收有限且单位有效的真值 | `test_tick_validation_rejects_*`、`test_account_risk_rejects_*`、CTP snapshot regressions | adapter/model/risk gate 在状态修改前 fail-closed | 无变化 |
| 非法 account event 在验证前推进交易日/高水位，或 CTP 转换错误被归类为通用 broker failure | 无效账户不得污染 state；有 Directional 风险时必须先减仓 | `test_account_event_is_validated_before_runtime_state_mutation`、`test_ctp_invalid_account_event_reduces_directional_risk_before_halt` | CTP 发出 `account_error`；Directional 进入 `REDUCE_ONLY` 并尝试 flatten；无风险才停机 | 无变化 |
| 同一 trade callback 在进程内或重启后再次应用，且重复回报可能再次完成质量周期 | 一个 broker fill identity 最多产生一次持仓、PnL 和质量副作用 | `test_ctp_trade_callback_is_idempotent_within_session`、`test_duplicate_trade_callback_is_ignored_*` | CTP 与 engine 双层有界去重；engine identity 持久化；重复事件在 ownership 和持仓副作用前返回 | 无变化 |
| 新交易日首笔 trade 早于 account event 到达，被记到旧日后在 persist 中换日，导致 today/yesterday 错桶且 ID 被清掉 | 成交必须属于 broker 权威交易日；换日先于 fill；重启仍幂等 | `test_new_day_trade_rolls_state_before_fill_and_remains_idempotent` | 首笔新日成交先换日再应用；换日仅淘汰旧日 ID；账户事件前后与重启 replay 均不重复 | 无变化 |
| 合法 duck-typed Broker 没有显式 `get_trading_day()`，新保护逻辑会误停机 | 既有 Broker runtime contract 保持兼容 | `test_directional_engine_records_broker_order_and_trade_callbacks_after_position_truth` | 优先 broker method；缺失时回退到已验证的 `AccountSnapshot.trading_day` | 无变化 |

## Code Quality

- 核心模型补齐 `Tick`、`Trade`、`ContractPosition`、`AccountSnapshot` 的明确验证契约，减少跨模块 `dict[str, Any]` 和隐式 `None` 语义。
- CTP 动态对象的 `Any` 被限制在 adapter；领域层接收已规范化 dataclass/enum。
- alert sink 失败现在写入日志且不阻塞其它 sink；持久化时账户刷新失败也不再静默吞掉。
- 全仓执行 Ruff import/format 治理并建立 MyPy 基线；修复具体 Optional、动态 enum、变量复用和类型推断问题，没有为了 strictness 添加无意义 wrapper。
- CI 将 lint、format、type、compile、full test、CLI、replay 与 directional production smoke 分开；昂贵真实数据矩阵继续保持手动门。
- 大模块只按职责和依赖审计，不以 LOC 为硬门。没有发现值得承担迁移风险的 God-object 重写，也没有新增循环依赖。

## Testing

基线为 `323 passed`。最终候选本地证据：

| Gate | 结果 |
| --- | --- |
| `ruff check .` | 通过 |
| `ruff format --check .` | 184 files formatted |
| `mypy afuture` | 74 source files，无问题 |
| `compileall -q afuture` | 通过 |
| `pytest -q` | **481 passed in 35.71s**（最终 logging-only hygiene 修改后重跑） |
| documentation consistency | 45 Markdown files；4 tests passed |
| `pip check` | 无损坏依赖 |
| wheel build | `afuture-0.2.0-py3-none-any.whl` 构建成功 |
| config validation | replay、Calendar live、Directional live、Auto replay 全部通过 |
| replay smoke | fixed/Auto 均 4 trades、空仓、margin 0 |
| research/acceptance CLI 与 production mechanics smoke | 通过 |

新增保护重点覆盖 position bucket、state corruption/atomicity、CTP conversion、non-finite inputs、duplicate fill/restart、cross-day fill、account reduction-first、causal data uniqueness 和文档一致性。最终代码终审另行运行相关 66 项测试，确认无剩余 P0/P1。

## Documentation

- README 现在是项目入口，说明两条运行链、安装/配置/命令、实盘边界、风险会计不变量和文档导航。
- `architecture.md`、`data-and-backtest.md`、`live-trading.md` 与 `production-checklist.md` 已按真实代码纠正执行、因果、CTP、HALT/recovery 和研究 checkpoint 语义。
- `documentation-index.md` 将每个 Markdown 精确分类为 current authority、validated evidence、historical/superseded 或 development record。
- 旧实验文档保留唯一研究证据，但顶部明确标为 historical；PR #25 Stress-90 是当前 production-research checkpoint，不等于 live activation。
- `tools/check_documentation.py` 检查本地链接、反引号路径、分类唯一性和历史标记；复杂注释只解释因果、单位、状态顺序和 fail-closed 理由。

## Behavioral Compatibility

未改变 alpha、portfolio construction、position sizing、2x gross、35% margin、25% available reserve、5% daily loss、30% total drawdown、35-lot cap、commission、slippage、stress scenario 或 train/validation/OOS 定义。Stress-80 与 Stress-90 evaluator 的 candidate weight SHA-256 均保持：

`8e38dbf6441b561dd1728df08665b94b15cc3358823257505c2fcb9d63f09f28`

行为变化只发生在原先错误或不可信的路径：非法输入现在 fail-closed；bucket 不足不再产生负仓；duplicate fill 不再二次入账；跨日成交按正确 bucket 记账；存在 Directional 风险时，非法账户路径先减仓而不是直接 HALT。冻结合法数据的经济结果与 checkpoint 一致，证明工程治理没有偷偷优化回测结果。

## Backtest / Stress Comparison

固定输入来自 PR #24/#25 workflow artifacts。ZIP 只解压到 ignored `runtime/`，未提交生成数据。

| Artifact ID | 内容 | ZIP SHA-256 |
| ---: | --- | --- |
| `9473260618` | broad daily 与 specific contracts | `ab4321a9cf8b9a61a0f9d435a6fb6bba7dd63986043b429c12c0ff47fd15389c` |
| `9491959916` | execution-aligned weights/mechanics | `e531f2874cbc26c3a54ff561e074f59b86ac887a1f509e33effd6908eaa3144d` |
| `9516473115` | prior 60m bars | `d0adcfb348c5ae89ddb488474c088d74a2402a2426c0e84fee7877dda5bd2a4e` |
| `9516472727` | recent 60m bars | `68ff2200eed8326741ce76f1cff46379c4e1dbc263297f4602bd84422e95e421` |
| `9532997165` | Stress-80 final summary | `7667ae671025bde9827436d29fe6e97e618650ca3e9526544420d67b39f43b1a` |

Stress-80 对照（修改前 checkpoint 与修改后复现一致）：

| Metric | Checkpoint | Final candidate |
| --- | ---: | ---: |
| Base full_recent annualized | 128.119261% | 128.119261% |
| Stress full_recent annualized | 80.067891% | 80.067891% |
| Stress max drawdown | 29.727688% | 29.727688% |
| Stress train / validation / OOS | 10.177351% / 511.519900% / 44.102450% | 10.177351% / 511.519900% / 44.102450% |
| Stress gross peak | 1.683773x | 1.683773x |
| Stress margin rejects / HALT | 0 / false | 0 / false |
| Stress turnover | 337,934,465 | 337,934,465 |
| Stress net alpha / turnover | 30.990722 bps | 30.990722 bps |

当前 Stress-90 checkpoint 也被完整复现：Base annualized 156.881655%、Stress annualized 112.100053%、Stress max drawdown 14.567214%、train/validation/OOS 为 28.891985% / 512.267292% / 102.808956%、gross peak 1.670510x、0 margin rejects、无 HALT、turnover 206,838,150、net alpha/turnover 78.274862 bps。Stress-80 与 Stress-90 promotion gate 均为 `passed=true`、`reasons=[]`。

## Known Limitations

- Stress-90 仍是离线 production-research checkpoint，没有接入 live 策略 wiring，也不能代替未来未见数据验证。
- 历史日线/60m proxy 不能模拟真实 L1 排队、部分成交、柜台拒单和真实保证金细节；上线前仍需 CTP Shadow、测试柜台和小资金验证。
- CTP account margin 在网关字段不足时仍使用 `balance - available` proxy；实盘 runbook 要求对券商账户、持仓和成交回报持续 reconciliation。
- 成交去重历史是每交易日最多 10,000 个 identity 的有界集合。该上限避免 state 无限增长；超过日内容量前必须评估并提高边界，不能依赖“永不重放”的假设。
