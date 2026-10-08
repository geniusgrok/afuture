# 排障与恢复

账户、持仓、委托和成交以柜台事实为准。故障先停止新增风险，保存原件，再核对原因；不能用重启、删除状态、手写 checksum、调低阈值或改本机日期绕过。正常流程见[CTP 运行](live-trading.md)，Stress-90 维护见[手册](stress90-live-runbook.md)。

## 检查顺序

1. 确认实例与账户/runtime 锁，避免重复启动干扰已有进程。
2. `afuture status --config CONFIG` 只读本地，保留退出码及首个失败项。
3. 保存 current/`.prev`/lineage/lock/pending、日志、审计、输入原件及外部备份；不先移动、覆盖或删除。
4. 需要新柜台事实时，在获连接许可的环境运行 `afuture doctor --config CONFIG --confirm-live`。
5. 核对 fresh account、完整持仓/活动委托/成交、权威交易日和参数；只比较净仓不够。
6. 处理根因后再次对账，按具体停机原因及许可决定继续只减仓、恢复或停机。

## 常见阻断

| 失败 | 处理 |
| --- | --- |
| state/schema/sequence/checksum/identity/lineage 错误 | 保全 current 和全部 sidecars；不自动提升 `.prev`、新建空账户或重建 marker。Stress-90 使用专用 recovery/WAL 或同身份已验证备份。 |
| activity/OHLC/OI 缺失、坏 digest 或 required day 不齐 | 保留原件/来源响应，查采集完整性和持久化；不开新风险。Stress-90 不得按普通缓存流程移除后自动重建。 |
| provider 修订重叠历史 | 保存新响应与旧 cache/digests，查明修订；仅仍覆盖 required completed day 的合格旧缓存可回退。不能 ffill、删失败日期或重签。 |
| CTP 交易日、快照或今昨仓不一致 | 重新取得 TD API 交易日及完整查询，检查夜盘/重连；本机日期、旧缓存不是权威。 |
| active/unknown 委托、未解释成交或持仓漂移 | 保持 HALTED，核对来源/终态和 trade/order identity；必要撤单须获授权并等最终回报。不能靠重启赌结果。 |
| margin/commission/metadata/quote 缺失或成本过高 | 查真实参数、乘数、盘口和实际冻结；接受缩量/不开仓，不猜手续费为零或扩大风险弥补。 |
| critical backlog 持续上升 | 读 `delivery_counters()`、定位消费/磁盘延迟；正常 Tick coalescing 不是成交丢失。停止新增风险。 |
| REDUCE_ONLY | 等待减仓确认并重读 Broker；不手改 RUNNING。 |
| HALTED / hard kill | 查首个硬失败，交易日推进或净值恢复不能自动解锁。 |
| 路径/磁盘/审计不可写 | 修复持久存储和权限，不换到临时目录。持久化/通知失败不能阻止已决定的必要风险退出。 |
| 原生 live import/login 失败 | 最终目标机按 constraints 安装并验 ABI；开发机替身/CI 不代替。 |

## 普通状态恢复

`recover-state` 仅用于普通策略路径，明确拒绝 Stress-90；不能用它替代 durable order/intent、crash-fill 或 lifecycle recovery。

柜台事实已独立核验且 local state 无法可信加载时，普通路径可按授权运行：

```bash
AFUTURE_RECOVERY_ACK=I_VERIFIED_CTP_POSITIONS afuture recover-state --config CONFIG --confirm-live --confirm-adopt-state
```

它检查参数与活动委托；发现活动委托会尝试撤单并继续停机，不直接采用。重建后仍保留 kill switch，须再 `status`/`doctor` 做第二次对账。`.prev` 是事故证据，不复制为 current 后直接交易。

## Stress-90 专用事故

- reserve schema 6 的触发锁存不因 rebase/epoch/净值恢复清除；旧 schema 缺历史证据持续阻断。
- prepared lifecycle、crash-fill checkpoint、已消费 permit 或 journal epoch 中断，须同 OP_ID、同参数/账户证据精确重试；换 nonce、abort、删 artifact 不构成恢复。
- registry/nonce/lineage 缺失、orphan sidecar、cache `.pending` 或写入/`fsync` 歧义保全全部原件和核验过的备份，不重新初始化。
- `operator_managed` receipt、account/epoch/runtime、registry/TDE 链不匹配保持 HALTED。外部订单/成交、资金活动或无法解释权益使 operator continuity 失效，先查明，再执行必要 account rebase。
- `stress90_risk_overlay_identity` 失配，即使 scale 更小，也须原 intent 完成/退役、HALTED/kill、flat/reconciled 后重新 `stress90-activate`；不改 bound digest。
- capacity exit 2 查看 `hard_safety_failures`、`risk_manager_preview` 及缺失参数；全零 commission 不等于无费，整数零目标可为安全结果。
- 严格最终结算/非相邻 session provider 尚未认证，不能用 Deposit/Withdraw=0、PreBalance 或操作者确认填证据。operator trust 不提升官方 verified 字段。

## 重连、部署与备份

断线后重取权威交易日、fresh account、完整 positions、委托最终状态、遗漏成交、合约参数及新行情。事实未齐保持收缩/停机；多次失败用柜台客户端查明，不连续重启制造未知结果。

`deployment-verify` 的 changes 表示源码/config/constraints/native/runtime/registry/bundle/overlay 漂移。保持停机，确认合法变化和账户证据后才重新 seal，旧 permit 失效。

`verify-backup` 拒绝成员/尺寸/checksum/语义/身份问题时保留失败备份，不覆盖运行目录、不拼 `.prev`。`restore-runtime` 只允许空目标和同 canonical registry/runtime 身份；不为了成功而清空正在使用的目录。恢复仍 HALTED/kill，按 `status→deployment-verify→prepare-session→doctor→新 permit` 复核。

## Heartbeat 与异常退出

watchdog 非零时看具体 missing/corrupt/stale、broker/queue、identity、HALTED 或 unclean check；restart/rebase/permit 不是通用修复。heartbeat 的 symlink/非普通文件、写入/fsync 失败是路径事故，不能当解除风控的理由。

未 clean shutdown 的 process receipt 默认使 live 在 gateway 启动/真实连接前以 75 阻断、失效旧 permit；损坏或丢 current 但有 `.prev` 亦阻断。完整证据支持的同次 activation 或认证日终续接只适用于手册窄条件。不要用 systemd restart loop 或删 receipt 绕过。

## 保留什么

保留命令/退出码、发生时间与柜台交易日、源码/config/input 摘要、状态和 sidecars、完整查询及 order/trade IDs、provider 原响应/日期/digest、delivery counters、日志/审计/告警和备份。私有资料不带入公共报告，密码/AuthCode/webhook 密钥不输出。

柜台持仓/成交/结算不一致、未知订单、无法解释资金、规则不可信或审计不足时保持停机并与柜台确认；不能把未评估写成没有风险。现场复核清单见[上线检查](production-checklist.md)。
