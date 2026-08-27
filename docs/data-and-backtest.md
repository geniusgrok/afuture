# 数据、回放与研究

本文说明数据在何时可见、如何进入回放，以及研究区间如何隔离。Tick CSV 的精确字段定义见 [`data-formats.md`](data-formats.md)，术语和指标定义见 [`glossary.md`](glossary.md)。

## 1. 数据职责

| 数据 | 用途 | 不能替代的证据 |
| --- | --- | --- |
| Tick / CTP 一档行情 | 回放、最新报价、盘口深度、涨跌停、交易日和订单执行 | 日线不能伪造排队、部分成交或拒单 |
| CTP 合约目录和参数 | 挂牌、到期、乘数、最小变动价位、保证金和手续费 | 缺失可信字段时不能开仓 |
| 连续合约开高低收 | 方向组合的品种信号历史 | 换月跳空不能计入可交易盈亏 |
| 具体合约价格、持仓量和成交量 | 当时可见的选约、换月和次日开盘账户模拟 | 必须使用当时已挂牌合约和固定乘数 |
| completed 60m Price×OI evidence | Stress-90 九品种方向变化确认；保存 expected/observed/missing 合约 coverage | 普通合并后 Tick、日线或缺失合约不能伪造完整 flow |
| Broker 账户、持仓和成交 | 实盘现金、权益、保证金、持仓和成交真相 | 研究目标或订单请求不能替代真实成交 |

公开历史数据通常不包含多年完整买卖盘口、排队、部分成交、柜台拒单、订单限流、逐日保证金和实际结算费率。固定成本和保证金比例只是为了可比较的研究假设，不是精确的 CTP 历史重放。

## 2. 输入完整性

进入日频研究或验收的表格必须满足：

- 时间索引可解析、唯一并严格递增；
- 必需列完整；
- 品种、合约和日期的组合键唯一；
- 数值列不含 NaN 或无穷值；
- 必需价格严格为正，交易成本不能为负；
- 缺失值只能出现在调用者明确允许的稀疏区域；
- 重复日期不能通过排序或“保留最后一条”隐式选择真相。

Tick 数据还要校验时间戳、买卖价、中间价、盘口数量、成交量、持仓量、涨跌停和交易日。数据质量检查保留原始顺序以发现重复和乱序；回放只能在校验通过后排序。

## 3. 时间因果规则

```text
完整交易日 D 的品种价格
→ D+1 品种目标

完整交易日 D 的具体合约成交量和持仓量
→ D+1 具体合约

D 日已选合约的收盘到 D+1 开盘盈亏
+ D+1 新目标的开盘到收盘盈亏
- D+1 开盘成交成本
→ D+1 账户收益
```

禁止：

- 用 D+1 尚未完成的成交量或持仓量改写 D 日已经冻结的选约；
- 用当前交易日尚未完成的盈亏决定当前目标；
- 把连续合约换月跳空记为交易收益；
- 用未来标签、未来最大有利/不利波动或候选结果作为生产特征；
- 追加未来数据后改变过去的目标或状态标签。

## 4. 交易日、时段和时区

生产 Tick 必须包含时区。中国期货夜盘的自然日期和柜台交易日不能混用。已启动的 CTP adapter 只接受交易 API 的 `getTradingDay()` 结果；网关、交易 API、getter 或合法 `YYYYMMDD` 值缺失时失败关闭，不能退回本机自然日期或旧缓存日期。

Stress-90 的 target trading day 同样只接受当前 CTP `getTradingDay()`。上一 activity/signal/OI day 必须由已经完成并持久化的柜台交易日证据确定；周末、法定节假日、临时休市和 missed target day 不能用 `pandas.BDay` 或人工猜测跳过。seed 与当前首个 live target 之间若有 gap，必须携带完整 manifest 逐日 replay。

`DirectionalActivityTracker` 每次有实质变化的最新观察都原子写入带 schema 和 checksum 的 `directional_activity.json`，同时保存 `completed` 和 `in_progress`。重启会恢复进行中观察，只在权威 `Tick.trading_day` 推进时冻结前一完整交易日。旧版无 envelope/checksum、只有 completed 的活动文件不自动迁移；必须保留诊断副本并重新观察一个完整柜台交易日。周末、节假日和夜盘都依赖柜台交易日证据，不能根据自然日小时差猜测数据是否最新。

方向组合要求价格历史覆盖流动性快照对应的交易日。第一次启动尚未形成完整快照时，系统不增加方向风险。

生产连续合约历史在任何时区/日期归一化前，就必须使用无时区、自然日午夜、唯一递增且开盘/收盘完全一致的索引；每个正有限数值必须可无损表示为 `float64`。runtime 先校验 provider 返回的全部允许行：不得晚于权威 CTP 当前交易日；没有 tracker 时不得晚于本地计划日。允许存在尚未完成的当前交易日行，但只把 required completed day 及以前的规范值原子写入 `directional_ohlc_cache.json`，首次调用和重启都使用重新解码的同一 `float64` 表示。文件使用一个共享日期向量、行优先开盘/收盘矩阵、内容 SHA-256 和整个 envelope SHA-256；它只保存市场输入证据。新的 provider 结果只有在与已验证缓存的全部重叠规范值逐值不变时才可替换缓存。重叠历史被静默修订、缓存被篡改、schema/品种/形状不符，或缓存没有所需完整交易日时，都不接受该输入；provider 中断只能回退到仍满足同一所需交易日/新鲜度契约的已验证缓存。

Stress-90 live 的外部 OHLC provider 只由 `directional-ohlc-refresh` 这一无订单权限的准备进程调用。`run_once()`、`on_tick()`、rebalance 和 Broker callbacks 只读 validated cache，不同步访问网络。cache 不完整时空仓拒绝 openings，有仓进入 `REDUCE_ONLY`；不得以更旧 cache 继续增加风险。

raw CTP 60m observer 位于 Tick 转换成功后、manager Tick coalescing 前。每个有效 raw Tick 进入有界聚合器，但 critical order/trade/account/position/error 仍保持 FIFO 优先。聚合按 CTP `trading_day` 和固定 session manifest 处理夜盘跨午夜、60m 边界、累计 volume 非负增量、reset、duplicate/out-of-order/late Tick、重启和 rollover。九品种中任一 required contract coverage 不完整时，对应 target input 为 missing/incomplete，而不是 `flow=0`。

## 5. 回放和撮合

`afuture replay` 复用运行时的策略、风控、执行、Broker 事件和记账规则。默认 `SimBroker` 是确定性回归模型。

只有输入真实、当时可见的一档行情时，增强模拟才会使用盘口深度折扣、部分 FAK 成交、延迟、订单数量冲击和未成交数量。没有这些输入时，系统不会伪造“真实 L1”精度。

回放保护以下规则：

- 订单请求不更新持仓，成交才更新；
- 手续费和滑点只在现金和权益中记录一次；
- 平仓数量、开平标记和今昨仓先校验后修改；
- 反转先平仓再开仓；
- 减仓未确认前不允许开仓；
- 活动委托、拒单、撤单、部分成交和重试不产生重复成交；
- 只减仓和硬停机期间不增加风险。

实盘 CTP 回调的投递与回放的简单事件列表不同：order、trade、position、account 和 error 进入关键 FIFO；Tick 按 `(symbol, exchange)` 只保留尚未投递的最新一条。每次 `poll_events()` 默认最多返回 100 条并先清空关键事件，因此 Tick 洪峰不能把成交或账户事件排在无限旧 Tick 之后。`delivery_counters()` 提供 `critical_enqueued`、`ticks_received`、`ticks_coalesced`、两类 delivered 和两类 backlog，用于判断合并率和积压；合并只减少尚未消费的旧 Tick，不改变成交真相。

## 6. 研究窗口

压力验证矩阵的每一行都从固定初始资金、空仓和显式的窗口前状态开始，拥有独立账户模拟：

- 训练区间用于形成或选择候选；
- 验证区间用于比较候选；
- 样本外区间用于检验未参与形成过程的数据；
- 前序区间检查不同历史阶段；
- 汇总窗口覆盖上述近期区段，因此会与子区间重叠。

样本外区间一旦被模板选择、候选筛选或人工判断观察，就必须记录为不再纯净。研究报告不能只保存年化收益，还要保存窗口日期、成本、保证金假设、初始资金、输入摘要、候选摘要、最高总敞口、风控拒绝和停机状态。

## 7. 当前离线压力研究证据

当前方向组合候选的历史代号是 `Stress-90`。数字表示该轮预先设定的压力情景年化收益目标，不表示交易成本、保证金比例或实盘风险等级。

| 情景 | 单边成本 | 保证金比例 | 汇总窗口年化收益 | 最大回撤幅度 | 最高总敞口 | 保证金拒绝 | 硬停机 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 标准情景 | 5 个基点 | 12% | 156.881655% | 15.708467% | 1.983123 倍权益 | 0 | 否 |
| 压力情景 | 15 个基点 | 15% | 112.100053% | 14.567214% | 1.670510 倍权益 | 0 | 否 |

这些数字只描述固定历史输入下的离线账户模拟。完整分段结果、输入摘要、成本、换手和防过拟合约束见 [`stress90-final-evidence.md`](stress90-final-evidence.md)。该文档如实保留当时 `production_wiring=false`；当前代码后来把同一固定候选接成显式可选 runtime policy，见 [`stress90-live-productionization.md`](stress90-live-productionization.md)。两项证据不能倒推，也不能替代现场 activation。

当前 batch evaluator、bootstrap replay 和 live incremental transition 共用同一生产纯核心，验收必须逐日、逐产品比较 Base/OI/cost/survivor，而不能只比较最终年化收益。五个固定输入可用时还必须重得 candidate SHA `8e38dbf6441b561dd1728df08665b94b15cc3358823257505c2fcb9d63f09f28`；输入缺失或 SHA 不符时报告 blocker，不得更换数据后声称精确复现。

`112.100053%` 不是未来收益承诺。96-template pool 在已观察历史上存在 selection bias；候选固定后新发生的数据才是真正 forward evidence。live 使用 CTP raw 60m 后，必须通过 Shadow comparator 解释它与历史 vendor 数据在 first/last、volume、dominant 和 flow 上的差异。

## 8. 何时重跑昂贵验证

普通命名、文档、类型和行为不变的重构只运行受影响测试。以下变化才需要重新执行完整回测、压力矩阵和验收矩阵：

- 策略信号、目标构造、仓位大小或风险响应；
- 杠杆、总敞口、保证金、现金、回撤、成本或成交假设；
- 合约选择、换月、时间戳、交易时段、交易日历或窗口划分；
- 共享记账和执行基础设施的行为；
- 完整验证发现实质问题并完成修复。

更高价值的下一步是新发生数据、多日 CTP Shadow、真实手续费和保证金、计划与实际成交差异、测试柜台订单生命周期和极小资金，而不是继续调整同一历史数据。
