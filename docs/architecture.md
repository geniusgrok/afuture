# 架构与数据流

## 1. 系统边界

`afuture` 有两条账户互斥的正式运行链：Calendar / Auto 与 Execution-Aligned Directional。它们共享唯一的账户和执行真相：

- Broker/CTP：订单、成交、账户与柜台持仓；
- `PositionBook`：对 Broker fill 的本地确定性镜像；
- `RiskManager`：账户 hard gate、`REDUCE_ONLY`、daily circuit 与 HALT；
- `StateStore`：带 schema、sequence、checksum 的重启证据；
- TradingEngine：事件顺序、对账、持久化与可观测性编排。

策略、研究 evaluator 和 CLI 都不能直接赋值真实持仓，也不能绕过 Broker 成交与 RiskManager 权限。

## 2. 依赖方向与职责

稳定依赖方向是：

```text
models / config
→ pure calculations and policies
→ strategy / risk / execution
→ runtime orchestration
→ broker / persistence / CLI / reporting
```

当前包没有已知 import cycle。动态 VeighNa 类型只停留在 `broker/ctp.py` 适配边界，转换后使用 `models.py` 中的 enum/dataclass。没有为了单一实现引入 Repository/Factory/Service 层；TradingEngine 虽然职责较重，但 event、fill、risk、state 的顺序高度耦合，当前以测试保护的显式编排优先于高风险拆分。

## 3. Calendar / Auto 主链路

```text
CTP catalog / Tick
→ AutoPairManager
   point-in-time catalog、front-3、adjacent、activity/sync/liquidity
→ CalendarSpreadStrategy
   signal history、entry/exit/stop/confirmation
→ PortfolioRisk + RiskManager
→ PairExecutor
   reductions/openings、双腿 FAK、partial rollback
→ Broker
→ fill event
→ PositionBook / account / state / quality
```

Auto 只拥有候选与 open-eligible 状态。一个 pair 失去候选资格后，既有仓位仍受 managed 逻辑约束直至退出，不能因扫描结果消失而成为无人管理风险。

## 4. Directional live 主链路

```text
已完成产品 OHLC
→ ExecutionAlignedAggressivePolicy
→ 产品目标权重（target gross <=2x）

已完成交易日 D 的 CTP volume/OI snapshot
→ D+1 concrete contract selection

Broker positions + fresh D+1 quotes + live ContractSpec
→ integer target lots（单合约 <=35）
→ margin-aware downward fitting
→ deterministic reductions
→ Broker 确认 reductions 后再 openings
→ RiskManager hard gates
→ FAK → Broker fill truth
→ realized-gross guard / circuit / HALT
→ state / audit / execution quality
```

`DirectionalPortfolioManager` 不维护第二套账户。signal provider 失败时，只有已缓存历史覆盖 required signal day 才可继续；缺失且已有风险时转入风险收缩，账户为空时拒绝新增。

## 5. Offline production-research evaluator

`DirectionalProductionAcceptance` 是 deterministic account proxy，负责从冻结目标和历史具体合约数据模拟：

- previous-close → current-open 既有头寸 PnL；
- reduction-first 订单序列；
- integer lots、multiplier、commission、turnover；
- cash、realized/unrealized PnL、equity；
- margin、available、gross exposure、reject、daily circuit、HALT；
- train、validation、OOS、prior 与 full_recent 结果行。

PR #25 Stress-90 在该离线 evaluator 上增加 causal expanding-median HHI leadership freeze，并继承 Stress-80 的 9-product 60m Price × OI confirmation、cost eligibility、survivor reallocation 与 25% drawdown reserve。它没有修改 live runtime wiring。

```text
frozen historical inputs
→ causal 60m signal / target construction
→ Stress-80 target and reserve mechanics
→ Stress-90 leadership response
→ independent account simulation per result row
→ promotion matrix / evidence
```

当前研究 checkpoint 是 `main` merge `482455dc57bc6a134f45232e290b4a49c3f7073d`（PR #25）。Stress full_recent 年化 112.100053%、最大回撤 14.567214%、gross peak 1.670510x、0 margin rejects、无 HALT；这些数字只描述固定历史 proxy。详见 [`stress90-final-evidence.md`](stress90-final-evidence.md)。

## 6. 时间与窗口不变量

- D 日 completed OHLC 只决定 D+1 及以后目标；
- D 日最终 volume/OI 只决定 D+1 concrete contract；
- 当前 session PnL 不进入当前目标的 completed-return governor；
- continuous roll jump 不计入可交易收益；
- t→t+1 收益来自 t 日已经选择的同一具体合约；
- validation/OOS simulation 不继承同矩阵其它结果行的账户状态；
- `full_recent` 是覆盖 train/validation/OOS 等区段的汇总窗口，**不是**与它们不重叠的 holdout；
- 一旦 OOS 被选择流程观察，文档必须标记 non-pristine。

## 7. Execution 与 accounting 不变量

- 提交 request 不改变持仓；只有 Broker trade/fill event 改变；
- reject/cancel 不改变 position、cash 或 realized PnL；
- partial fill 只按实际成交量记账，retry 不能重复成交；
- reversal 是 close 后 open，不是绕过 execution 的净仓赋值；
- SHFE/INE close-today 与 close-yesterday 独立校验，不能跨 bucket 借量；
- commission/slippage、notional、margin、gross/net exposure 使用明确 multiplier 与单位；
- 无法识别的 CTP direction/offset/type/status 产生 `broker_error`，不映射成猜测的经济事件。

## 8. 风险状态机

```text
RUNNING
→ soft/runtime risk condition
→ REDUCE_ONLY
→ reductions / flatten
→ 完整安全检查
→ RUNNING

RUNNING or REDUCE_ONLY
→ hard/manual condition
→ HALTED
→ 人工核验、对账和恢复门
→ RUNNING
```

Directional hard authority 保持 target/realized gross `<=2x`、margin `<=35%`、available `>=25%`、daily loss `5%`、total drawdown `30%`、单合约 `<=35` 手。Daily-loss 是同 trading-day circuit；total drawdown、margin、cash、non-positive equity、metadata/对账异常属于 hard/manual 路径。任何风险收缩层都只能降低目标。

## 9. State 与恢复

```text
Broker account + complete positions + active orders
↔ RuntimeState expected positions / event IDs / risk markers
```

启动只有在今昨、多空、合约与关键状态完全一致时 reconciled。State envelope 的 JSON、schema、positive sequence、checksum 或 payload 不可信时：

- `load` fail-closed；
- `save` 不允许把损坏目标覆盖成 sequence 1；
- 原文件保持不变，供人工诊断；
- `recover-state` 仍保持 Kill Switch，不能直接恢复交易。

## 10. 可观测性

- `AuditJournal`：signal、order、fill、risk、recovery 的 JSONL 证据；
- `AlertManager`：本地与 webhook 广播；单 sink 故障不阻止风险动作，但记录脱敏 warning；
- `ExecutionQualityRecorder`：pair round trip 与 directional rebalance/fill/cycle；
- report：account、position、performance、margin 和质量摘要。

告警、quality 和 report 都是观测层，不拥有下单或风险权限。

## 11. 明确非目标

当前不需要数据库、消息队列、Web 服务、微服务或第二账户状态机。研究 artifacts 不直接成为 live 数据源。后续架构变化必须由已复现的正确性、容量或维护问题驱动；真实收益与执行可信度的下一批高价值证据来自未来数据、CTP Shadow、测试柜台和小资金，而不是继续扩大同一历史上的参数空间。
