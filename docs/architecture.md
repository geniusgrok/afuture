# 架构与账户边界

固定/自动跨期与方向组合互斥地使用同一套账户、执行和风控基础设施。

| 组件 | 职责 |
| --- | --- |
| Broker / CTP | 外部订单、成交、账户与柜台持仓真相 |
| `PositionBook` | 成交驱动的本地持仓镜像 |
| `RiskManager` | 账户硬限制、`REDUCE_ONLY` 和 `HALTED` |
| `StateStore` | 已校验的重启证据 |
| Activity / OHLC / OI sidecars | 市场输入证据，不能拥有账户或下单权限 |
| `TradingEngine` | 事件、对账、持久化与运行编排 |

依赖由模型/配置、纯计算、策略/风控和运行编排，进入 Broker/持久化/CLI。VeighNa 动态类型限制在 CTP 适配边界，内部使用统一模型。策略只产生意图；审计、告警和报告只观测。

## 数据到订单

跨期链从已挂牌目录选择相邻月份，在同步两腿行情上计算可成交价差，经账户检查后生成双腿订单。候选退役不会删除已有持仓，已有风险须继续管理直到 Broker 确认退出。

方向链使用完整 D 日价格产生 D+1 品种目标；上一完整交易日成交量和持仓量决定具体合约。目标按权益、价格、乘数和保证金拟合为整数手数，只允许向下缩减。

```text
验证输入 → 保存 prepared decision → 读取 Broker truth
→ 计算并保存 execution intent → 先减仓 → 等待确认
→ 重读持仓 → 检查每笔开仓 → 成交记账 → 状态 checkpoint
```

普通方向策略使用 `execution_aligned`；Stress-90 使用独立 manager 和同一纯候选核心：Base→九品种 completed 60m Price×OI→20/3/15bp cost gate→survivor reallocation→raw HHI。它使用 1x raw candidate，completed-path 25% reserve 与 HHI 只冻结新增风险，不重复应用普通模式的 0.25 缩放。decision、HHI 与 candidate state 不随订单或重试重复推进。

## 成交与回调顺序

- 订单请求、撤单或拒单不改变持仓；部分成交只按实际数量记账。
- 引擎与 CTP 以 `(trading_day, exchange, trade_id)` 去重；启动前注入持久化身份。缺 exchange 的旧身份冲突须停机对账。
- 反转先平旧方向；SHFE/INE 平今、平昨不能互相借量。
- 合约、交易所、方向、开平、状态、数量和单位须精确；非法枚举、非整数数量、NaN/无穷值拒绝，不能猜测。
- 持仓镜像和快照串行化；交易日只能向前推进。延迟旧日 account event 不能改写今昨仓。

order/trade/position/account/error 使用关键 FIFO；尚未投递的 Tick 按合约与交易所合并，每轮有界投递并优先关键事件。`delivery_counters()` 显示投递和积压。Stress-90 raw observer 在合并前观察有效 Tick，以权威交易日和 session manifest 聚合；activity/OI 在批次、日切或正常停机时 checkpoint。崩溃不声称恢复未落盘 Tick。

CTP 当前交易日来自 TD API `getTradingDay()`，缺失或非法即失败关闭，不使用本机日期。运行日历由 `runtime_calendar.json` 提供版本、来源、完整开休市覆盖和摘要；未知日期/合约、身份冲突或损坏日历拒绝每笔报单。

## 风险与开仓边界

`RUNNING` 遇可恢复风险进入 `REDUCE_ONLY`；硬风险或人工停机进入 `HALTED`。解除停机必须处理原因、重新对账并满足相应许可。风险响应只能缩减目标，无法绕过最终 `RiskManager`。

Stress-90 首次入场只允许预定义首窗。首窗外纯换月仅限真实 CTP、已持久认证的同日/epoch/decision/overlay 意图：旧腿全平，新腿累计认证成交等于当前持仓，无新腿 CLOSE、未知或活动委托，累计数量及保守名义价值不超初始授权。其余 entry/add/reversal 不获得该例外；Sim/Shadow 保持首窗限制。该校验覆盖已可见事件，不保证尚未收到的未来迟到回报不存在。

## 状态、身份与恢复

状态、registry、seed、policy、OI、intent、permit、order journal 和 lifecycle transaction 分别验证精确 schema、正 sequence、checksum、parent chain 与账户/部署/runtime 身份。推进依赖原子持久写、锁和 CAS；registry 的多 binding 保护账户切换、退役身份及 Shadow/live 隔离，不能当重复账户状态删除。

通用 state 的 `.prev` 仅是事故证据，不自动提升。专用 store 按其 schema 校验 predecessor。损坏当前文件不能被覆盖或拼接修复；普通 `recover-state` 仍保持停机且明确拒绝 Stress-90，后者仅走专用恢复/核验备份。OHLC cache 仅保存市场数据，provider 追加必须保留所有已验证日期及规范数值；缺 required day 或静默修订阻止新风险。

Stress-90 seed 不继承历史账户收益。prepared decision 与 execution intent 可用于同次崩溃续接；order journal 在 official send 前持久化授权和最坏成交容量，terminal entries 进入 immutable archive。容量/账户切换须显式 HALTED epoch 事务，跨 epoch 保留订单身份防重。详细操作见[Stress-90 手册](stress90-live-runbook.md)。

## 观测与支持范围

`status` 只读本地；`doctor` 连接新柜台快照但 `orders_sent=0`；capacity report、Shadow 和报告不能签发真实资金许可。Python ≥3.10、受约束依赖与 Windows core/replay 支持保留；CTP live 只针对经验证的 POSIX 目标机。原生 ABI、实际回调、结算和整机失联通知须由现场证据证明，通用 CI 无法认证。
