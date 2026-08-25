# 数据、回放与研究

## 1. 数据职责

| 数据 | 用途 | 不可替代的边界 |
| --- | --- | --- |
| Tick / CTP L1 | 回放、fresh quote、depth、limit、交易日与执行 | 不能从日线伪造 queue、partial 或 reject |
| CTP catalog / metadata | listing、expiry、multiplier、tick、margin、fee | 缺失可信字段时 live opening fail-closed |
| continuous OHLC | Directional 产品 signal 与 meta history | continuous roll jump 不算可交易 PnL |
| concrete-contract OHLC/OI/volume | point-in-time 选约、roll-safe next-open 与 account proxy | 必须使用当时已挂牌合约和冻结 multiplier |
| Broker account / position / fill | live cash、equity、margin、持仓与成交真相 | 研究 target 或 request 不能替代 fill truth |

公开历史不包含多年完整 bid/ask/depth/queue/partial/reject、真实订单流控、逐日 Broker margin schedule、实际结算费率和 market impact。固定 5bp/15bp、12%/15% margin proxy 是可比较的研究假设，不是精确 CTP 历史重放。

## 2. 输入完整性

所有进入 causal daily 研究或 acceptance 的 frame 必须满足：

- `DatetimeIndex` 可解析、唯一且严格单调；
- 所需列存在；
- required product/symbol/date key 唯一；
- 数值列不含 NaN/inf；
- 必需价格严格为正，cost bps 非负；
- 缺失值只能出现在调用者明确允许的稀疏区域；
- duplicate date 不允许通过排序或 keep-last 隐式选择真相。

Tick 数据额外校验 timestamp、bid/ask/mid、depth、volume、open interest、limit 与 trading day。原始顺序需要用于 data-check 的 duplicate/out-of-order 诊断；回放才在验证后排序。

## 3. 因果时间规则

```text
完整交易日 D 的 OHLC
→ D+1 产品目标

完整交易日 D 的 concrete-contract volume/OI
→ D+1 具体合约

D 已选具体合约的 D close → D+1 open
+ D+1 新目标的 open → close
- D+1 open turnover cost
→ D+1 account return
```

禁止：

- 用 D+1 未完成 volume/OI 改写 D 已冻结选约；
- 用当前 session PnL 决定当前目标；
- 把 continuous adjusted price 的换月跳空记为交易收益；
- 用未来 label、MFE/MAE 或 candidate outcome 作为 production feature；
- 在 appended future data 后改变过去 target/state label。

## 4. Trading day、session 与 timezone

生产 Tick 必须带 timezone；中国夜盘自然日与 trading day 不能混用。`DirectionalActivityTracker` 只在 `Tick.trading_day` 推进时冻结前一完整日 snapshot。周末、节假日与夜盘依赖 trading-day evidence，不用自然日小时差猜测“最新”。

`required_signal_day = completed_activity_snapshot.trading_day`。OHLC 必须覆盖 required day，然后 `signal_max_age_hours` 才处理未来 timestamp 或长时间停更。第一次启动没有 completed snapshot 时不新增 Directional 风险。

## 5. 回放与撮合

`afuture replay` 使用与运行时相同的 strategy、risk、execution、Broker event 和 accounting 语义。默认 SimBroker 是确定性回归模型；opt-in realistic L1 模式只有在输入真实 point-in-time L1/tick 时，才模拟 depth haircut、partial FAK、latency、size/depth impact 与 unfilled quantity。

回放保护以下不变量：

- request 不更新 position；fill 才更新；
- commission 与 slippage 在 cash/equity 中只记一次；
- close volume、offset 与今昨 bucket 先校验后修改；
- reversal 先平后开；
- reduction 未确认前不 opening；
- active order、reject、cancel、partial、retry 不产生重复 fill；
- HALT/REDUCE_ONLY 期间不增加风险。

## 6. 研究窗口

Stress-80/90 matrix 的每一行都是从 frozen initial capital、flat position 和显式 pre-window causal state 开始的独立账户模拟。Train、validation、OOS 与 prior windows 用于不同阶段检验；`full_recent` 是覆盖这些阶段的汇总视图，因此会重叠，不能称为 pristine holdout。

任何被模板选择、候选筛选、promotion 或人工判断观察过的 OOS 都必须记录 `pristine=false`。研究报告应同时保存窗口日期、成本、margin proxy、initial capital、input digest、candidate digest、gross peak、reject 和 HALT，而不是只保存年化收益。

## 7. 当前 Stress-90 checkpoint

继承基线：

- Stress-80 merge `b4207abb50aca1e39d5ebba3affc04765857251a`（PR #24）；
- Stress-90 merge `482455dc57bc6a134f45232e290b4a49c3f7073d`（PR #25）；
- fixed candidate weight SHA256 `8e38dbf6441b561dd1728df08665b94b15cc3358823257505c2fcb9d63f09f28`。

| Window | Annualized | Max DD | Gross peak | HALT | Rejects |
| --- | ---: | ---: | ---: | --- | ---: |
| Base full_recent | 156.881655% | 15.708467% | 1.983123x | false | 0 |
| Stress train | 28.891985% | 13.657897% | 1.648285x | false | 0 |
| Stress validation | 512.267292% | 11.783634% | 1.626864x | false | 0 |
| Stress OOS | 102.808956% | 17.632605% | 1.649642x | false | 0 |
| Stress full_recent | 112.100053% | 14.567214% | 1.670510x | false | 0 |
| Stress prior1 | 12.141524% | 23.228982% | 1.677099x | false | 0 |
| Stress prior2 | 8.578529% | 18.354608% | 1.648349x | false | 0 |

PR #25 run `32798895640` 的 Python 3.10/3.13 CI 通过。完整输入摘要、成本、turnover、net alpha 与 bounded-search 约束见 [`stress90-final-evidence.md`](stress90-final-evidence.md)。该 checkpoint 是离线研究晋级，不会自动接入 live runtime。

## 8. 何时重跑昂贵验证

普通命名、文档、类型和行为中性重构只运行局部/模块测试。只有以下变化需要重新执行完整 frozen backtest / Stress / acceptance matrix：

- Alpha、target construction、position sizing 或 risk response；
- leverage、gross、margin、cash、drawdown、cost 或 fill assumption；
- contract selection、roll、timestamp、session、calendar 或 split definition；
- shared accounting/execution infrastructure 的行为修改；
- final validation 暴露实质问题并修复。

更高信息价值的下一步是新发生数据、多交易日 CTP Shadow、真实 margin/fee、planned-vs-realized turnover/slippage/tracking、测试柜台订单生命周期与极小资金，而不是在相同已观察历史上继续调参。
