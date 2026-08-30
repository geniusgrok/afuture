# 术语与公式

本页定义当前权威文档中反复出现的交易、研究和运行术语。代码标识符和历史研究代号保持原样，中文解释是其在 `afuture` 中的实际含义。

## 交易与运行

| 术语 | 含义 |
| --- | --- |
| Calendar Spread / 跨期价差 | 同一商品、不同到期月份合约之间的价格差。策略交易相对价差，不是直接预测单个合约绝对价格。 |
| Auto | 从决策时已经可见的合约目录中，自动选择满足挂牌、到期、相邻月份、行情同步和流动性条件的跨期组合。 |
| Directional / 方向组合 | 为多个商品生成做多、做空或空仓目标，再映射到具体合约和整数手数。 |
| CTP | 国内期货柜台接口。`afuture` 通过它接收行情、账户、持仓、订单和成交事件并发送实盘委托。 |
| Shadow / 影子运行 | 使用实时 CTP 行情、合约和交易日信息运行策略，但订单、成交、持仓和资金由本地模拟 Broker 维护，不调用 CTP 报单接口。 |
| Broker | 订单和成交真相的统一边界，可以是确定性模拟、Shadow 或真实 CTP 适配器。 |
| fill / 成交 | Broker 确认实际成交的事件。只有 fill 可以改变持仓、现金、手续费和盈亏。 |
| FAK | 立即成交当前可成交部分，剩余数量自动撤销的限价单。 |
| OHLC / 开高低收 | 一个时间区间的开盘价、最高价、最低价和收盘价。 |
| OI / 持仓量 | 市场尚未平仓的合约数量；这里用于流动性判断和具体合约选择。 |
| 60m Price×OI flow | 对一个完整柜台交易日的具体合约，比较 first 60m open、last 60m close 和 first/last hold；只有持仓量上升且价格方向非零才得到 `-1/+1`。合法 `0` 与 missing/incomplete 不同。 |
| target trading day | 某份产品目标被应用的权威柜台交易日，只能来自 CTP `getTradingDay()`；本机自然日和通用工作日推算不是 live 权威。 |
| activity day | 已完整结束并用于下一 target day 选具体合约的柜台交易日。 |
| L1 / 一档行情 | 当前最优买价、卖价及其可见数量，不包含完整委托队列。 |
| D / D+1 | D 是已经完整结束的交易日；D+1 是其后的下一个交易日。D 日数据最早只能影响 D+1 的决策。 |
| point-in-time / 当时可见 | 决策只能使用该时点已经发布或已经完成的数据，不能使用后来补充的合约、行情或结果。 |

## 账户与风险

| 术语 | 含义 |
| --- | --- |
| notional / 名义价值 | 合约价格 × 合约乘数 × 手数。它不是实际支付的保证金。 |
| gross exposure / 总名义敞口 | 所有多空头寸名义价值绝对值之和。总敞口 2 倍表示该值不超过账户权益的 2 倍。 |
| net exposure / 净敞口 | 带方向的名义价值之和，用于表示组合整体偏多或偏空。 |
| margin / 保证金 | 柜台或模拟账户为当前持仓冻结的资金。保证金比例为保证金除以账户权益。 |
| available / 可用资金 | 扣除保证金等占用后可继续使用的账户资金。 |
| realized / unrealized PnL | 已实现盈亏来自已经平仓的成交；未实现盈亏来自仍持有仓位按当前价格计算的浮动盈亏。 |
| turnover / 换手金额 | 一段时间内成交名义价值的累计值，用于衡量交易频率和成本暴露。 |
| `RUNNING` | 安全检查通过，允许正常开仓、减仓和退出。 |
| `REDUCE_ONLY` | 只能减仓或退出，不能增加任何风险敞口。 |
| `HALTED` | 硬停机状态。恢复前必须人工核验、处理原因并完成柜台对账。 |
| daily circuit / 单日风控熔断 | 同一交易日亏损达到限制后，停止增加风险并收缩仓位。 |
| fail-closed / 失败关闭 | 数据、状态或外部事件不完整、不可信时，默认拒绝增加风险，而不是猜测后继续运行。 |
| margin reject / 保证金拒绝 | 目标仓位或开仓请求会突破保证金或可用资金限制，因此被风控拒绝。 |
| Kill Switch / 停机开关 | 人工或系统设置的持久化停机标记，未完成核验和恢复前不能重新交易。 |
| drawdown reserve / 回撤预留 | Stress-90 的软防御阈值：30% hard total drawdown 减 5% daily loss，等于 25%。只用 completed account wealth/high-watermark，并只冻结新风险。 |
| freeze new risk / 冻结新风险 | 阻止新开仓和同方向加仓，但允许减仓、退出、反转进入新方向前的旧风险退出，以及同品种换月。它不是 0.25 target scaling，也不能覆盖硬风控。 |
| policy activation identity | 通用 runtime state 中绑定的 policy id、definition digest、products digest 和 bootstrap seed digest；不一致时失败关闭。 |
| account rebase | 在 `HALTED`、空仓、无活动委托、fresh account snapshot、reconcile 和强确认后重置 live account soft-path sufficient statistics；不重算候选或 HHI。 |

## 研究与指标

| 术语 | 含义 |
| --- | --- |
| bp / bps | 基点，1 bp = 0.01%。15 bp = 0.15%。 |
| mean-reversion heuristic / 均值回复启发式（分数） | 对滚动信号窗口的简单 AR(1) 变化回归中负斜率派生的 `[0, 1]` 分数，并与估计半衰期一起作为开仓/候选门。它不是平稳性检验，也不是 ADF、KPSS、Phillips-Perron、Engle-Granger 或 Johansen 检验；不构成统计显著性证明，也不保证未来均值回复。配置兼容字段名 `min_stationarity_score` 保持不变。 |
| Base / 标准情景 | 当前固定研究中使用单边 5 bp 成本和 12% 保证金比例假设的账户模拟。 |
| Stress / 压力情景 | 当前固定研究中使用单边 15 bp 成本和 15% 保证金比例假设的账户模拟。 |
| `Stress-80` / `Stress-90` | 历史研究代号，数字表示该轮预先设定的压力情景年化收益目标，不表示成本、保证金或风险等级。 |
| deterministic account proxy / 确定性账户模拟 | 对相同输入总是产生相同结果的离线账户模型，用于验证持仓、费用、保证金和风险机械；它不是实际柜台。 |
| train / 训练区间 | 用于选择或校准候选的历史区间。 |
| validation / 验证区间 | 用于比较候选、但不应直接拟合参数的后续区间。 |
| OOS / 样本外区间 | 在候选形成后用于检验泛化能力的区间。一旦被反复观察并用于选择，它就不再是纯净样本外。 |
| `full_recent` / 汇总窗口 | 覆盖近期训练、验证和样本外区段的整体视图，因此与这些子区间重叠，不是独立留出集。 |
| gross peak / 最高总敞口 | 模拟期间“总名义敞口 ÷ 账户权益”的最大值。 |
| net alpha / 净收益贡献 | 扣除交易成本后的累计交易收益；这里的 alpha 是研究记账名称，不代表已证明的市场超额收益。 |
| net alpha / turnover | 净收益贡献除以换手金额，通常用基点表示，用于衡量每单位交易量留下的净收益。 |
| HHI / 集中度指数 | 各品种目标权重绝对值占比的平方和；数值越高，组合风险越集中。 |
| survivor reallocation | 成本门决定 support 后，在不创造 support、不改方向、不增加 gross 的前提下恢复 OI-confirmed aggregate gross；先最小化 candidate L1 tracking error，再最小化相对上一 applied target 的 L1 turnover。 |
| `policy_definition_digest` | Stress-90 固定定义和常量的规范摘要，不包含未来每日目标。 |
| `historical_candidate_weight_sha256` | 固定历史完整 candidate weight path 的摘要；不是 policy definition，也不是未来每日 decision identity。 |
| `daily_decision_digest` | 一个 target day 的规范输入、各层 product weights 和 post-state 摘要；相同输入/state 必须一致。 |
| prepared decision | 在第一张订单之前原子持久化的 exactly-once 每日候选决定。重启复用它，不再次推进 HHI 或候选 state。 |
| L1–L4 验证级别 | 工程验证范围：L1 为局部测试，L2 为模块测试，L3 为相关历史情景，L4 为完整验收矩阵。它与“一档行情 L1”是不同语境。 |

## 使用规则

- 当前权威文档第一次使用缩写、历史代号或不直观符号时，应同时给出中文含义或链接到本页。
- 证据文档可以保留原始字段名、文件名和研究代号，但不能把历史候选描述成当前实盘行为。
- 同一个术语不得在策略、风控、回放和报告中使用不同单位或不同经济含义。
