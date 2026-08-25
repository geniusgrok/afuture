# Directional Production Reality Closure Design

日期：2026-08-22

## 目标

把已经在 selection-biased specific-contract / next-open 历史口径下达到 100% 年化目标的 execution-aligned directional 策略，收口为更接近真实生产语义、可持续验证且适合个人长期维护的实现。

本轮不增加新的 Alpha family、不扩大 template pool、不提高 2.0x gross notional 上限，也不通过继续观察现有 OOS 调参来提高历史收益。核心目标是消除 research/live 语义差异，并让未来 Shadow/小资金结果可以自动回答“历史 Alpha 被真实执行吞掉了多少”。

## 已确认不需要改的内容

- Broker 继续是订单、成交、账户和持仓唯一真相。
- Directional 成交已经复用 TradingEngine `_apply_expected_trade()` 写入 `RuntimeState.positions`；重启对账能够逐合约比较本地持仓与 CTP 完整持仓，因此不新增第二套 directional position/state owner。
- 现有 account-exclusive 配置边界保留：directional 不能与 static pairs / Auto 同账户同时运行。
- 原 Calendar Spread / Auto 路径的经济行为不因本轮 directional 收口而改变。

## 1. Previous-day contract activity snapshot

### 问题

L4 使用交易日 t 的最终 OI/volume 选择合约，并让该具体合约承担 t→t+1 的收益；这等价于“上一完整交易日 activity 决定下一交易日交易合约”。当前 Live 却在 rebalance 时使用当前 Tick 的当日累计 volume/OI 重新选择，存在时点漂移。

### 设计

增加一个轻量、原子持久化的 `DirectionalActivityStore`：

- 按 CTP `Tick.trading_day` 聚合每个允许合约当日最后可见的 `volume`、`open_interest`、timestamp；
- 当观察到 trading_day 从 D 切换到 D+1 时，把 D 的最终 activity 冻结为 `completed_snapshot`；
- `completed_snapshot` 持久化到独立 JSON sidecar，默认与 directional state 同目录；
- 重启后直接恢复最近一个 completed snapshot；
- 生产选合约只能使用 completed snapshot，不使用当前交易日尚未完成的累计 volume；
- delivery/listing 过滤仍使用当前计划交易日；排序仍为 OI → volume → expiry → symbol；
- 第一次全新启动且没有 completed snapshot 时：允许管理/减持已有风险，但禁止新增 directional 风险，直到观察到一个完整 trading day。

该 sidecar 只拥有 contract activity 证据，不拥有账户/仓位/订单状态，因此不会形成第二状态机。

## 2. Signal-day freshness 与 fail-closed

### 问题

当前 `signal_max_age_hours=96` 主要用于容纳周末，但正常工作日也可能把漏掉一个完整交易日的数据误判为 fresh。

### 设计

信号新鲜度改为“交易日证据优先，小时上限兜底”：

- `required_signal_day = completed_activity_snapshot.trading_day`；
- execution-aligned OHLC history 的最新日期必须 `>= required_signal_day`；
- 小时 `signal_max_age_hours` 仍保留，作为未来 timestamp/长期停更的第二道门；
- provider 刷新失败时，如果本地缓存已经覆盖 `required_signal_day`，允许使用缓存；否则返回明确的 `signal_unavailable_risk_off`。

如果 signal 不足而账户已有 directional risk，`DirectionalTradingEngine` 进入 `REDUCE_ONLY` 并调用现有 flatten 生命周期；如果账户已经无风险，则 fail-closed 拒绝新开仓但不制造无意义 Kill Switch。

## 3. Reduction-first reconciliation

### 问题

当前代码在构造 position/rebalance plan 之前就要求所有非零 target product 都存在可交易具体合约，因此一个新增目标品种缺合约可能阻塞其它本应执行的减仓。

### 设计

rebalance 顺序改为：

1. 读取真实 broker positions；
2. 计算有效 target weights；
3. 用 completed activity snapshot 选择可交易具体合约；
4. 对 target=0、方向反转、超额仓位、需要换月且新合约已可用的旧风险先生成 reductions；
5. 对“target 非零但当前没有可用新合约”的产品：禁止新增/换月，不把这个缺失扩散成全组合 reject；若已有同产品仓位则保持其当前净手数作为临时 frozen target；
6. 只在 reductions 全部结算后，下一周期才提交 openings。

这样任何新增风险不可用都不能阻塞确定性的风险下降。

## 4. Directional execution-quality evidence

### 问题

现有 `ExecutionQualityRecorder` 的核心 round-trip schema 面向 pair spread，无法直接回答 directional 的 target tracking、turnover、单合约滑点和实际成本。

### 设计

在同一个 JSONL recorder 中增加三类事件，不建新服务：

- `directional_rebalance`：cycle id、signal day、activity day、target gross、target lots、planned reductions/openings、planned turnover notional、reason；
- `directional_fill`：cycle id、order id、product/symbol、offset/side、expected price、fill price、volume、multiplier、slippage bps、commission；
- `directional_cycle`：本轮目标 notional、实际成交 notional、realized turnover、commission、median/p95 slippage、target tracking error、completion latency、partial/rejected count。

`quality-report` 在保留原 pair 字段兼容性的同时增加 `directional` 子汇总。Directional manager 在提交订单时注册 expected execution metadata，TradingEngine 的 trade/order callback 按 `reference.startswith("directional:")` 回填，不创建第二成交状态真相。

## 5. Production-mechanics acceptance

### 目的

新增一个明确命名为 `production_mechanics_proxy` 的 acceptance，而不是把现有 107.46% notional-weight L4 重新命名成“实盘收益”。

### 数据与语义

输入继续使用已经完成的 continuous OHLC + concrete-contract daily data。策略权重完全复用冻结 execution-aligned policy。

新增回测必须逐日模拟：

- previous-day activity 选下一交易日 concrete contract；
- prior lots 的 previous-close → current-open PnL；
- current open 执行 reductions/roll/openings；
- `build_target_lots()` 相同的 equity × weight / (price × multiplier) 整数向下取整；
- `max_contract_volume`；
- target gross ≤2.0x；
- 保证金/available-cash 门；
- base/stress cost；
- 日内 open→close PnL；
- day-start equity / high-watermark drawdown。

### ContractSpec 代理边界

过去数年的逐日真实经纪商 margin/commission metadata 不存在，因此 acceptance 不伪造“精确历史柜台参数”。仓库维护一份冻结的产品 multiplier 表，并显式使用统一保守 `margin_rate_proxy` 与 bps cost；报告必须写出这些假设。

该 acceptance 的作用是验证整数手数、乘数、账户权益、margin/cash gate、drawdown gate 会如何改变经济曲线；它不能替代未来真实 CTP L1 Shadow。

### Risk policy

本轮先按当前正式 directional 风险配置运行 proxy。若 current production risk gate 导致策略历史上远早于研究曲线永久停机，报告必须把这一差异作为失败证据；只有基于清晰、可解释的风险语义修正才允许调整配置，不为了恢复 100% 数字而静默放宽风控。

## 6. 代码精简

最终 production policy 只有 `ExecutionAlignedAggressivePolicy`。

- `directional.py` 只保留 DirectionalConfig、contract/activity selection、target-lot/rebalance primitives；
- 删除旧 `FrozenAggressivePolicy` 及其旧 32-template 中间策略常量/理论流；
- `execution_aligned_policy.py` 自包含最终 96-template signal/meta primitives；
- `directional_runtime.py` 只承担通用 execution lifecycle，不再内置旧 Sina close-only provider/default policy；
- `execution_aligned_runtime.py` 唯一拥有 production continuous OHLC provider 和最终 policy wiring。

研究 tools 只保留可重现最终证据或当前 acceptance 所必需的文件；失败/被替代的中间 return-target evaluator 若无依赖则删除，历史结论保留在 Markdown。

## 7. 验证策略

严格遵守 AGENTS.md：

- L1：activity store/selector、signal day、reduction-first、quality schema、integer mechanics 单测；
- L2：directional manager/engine deterministic smoke，包括 fill、restart reconcile、REDUCE_ONLY；
- L3：复用固定 artifact 做 production-mechanics proxy，不重新搜索策略参数；
- L4：只有 final behavior candidate 稳定后运行一次完整 specific-contract acceptance；
- 最终：完整 `pytest`、compileall、Python 3.10/3.13 CI、directional config validate + smoke。

任何文档/注释/格式修改不重复昂贵 L4。

## 验收标准

- research/live contract selection 都使用 previous completed trading day activity；
- signal freshness 不能因 96h 周末容忍而漏掉正常交易日；
- 缺失新增目标绝不阻塞其它 reduction；
- stale/missing required signal + existing directional risk 会自动进入 REDUCE_ONLY；
- restart reconcile 继续只有 RuntimeState/Broker 一个仓位真相；
- directional quality-report 能输出实际 turnover/slippage/commission/tracking；
- production-mechanics proxy 明确报告 integer lots/multiplier/margin/drawdown 与旧 float-notional L4 的差异；
- 最终 main 不再保留可误用的旧 directional 中间 production policy；
- README、architecture、data/backtest、live、production checklist 与最终代码/证据完全一致。