# 故障排查

本手册只给出安全诊断和恢复顺序。任何会改变持仓、解除停机或采用本地状态的操作，都必须先以 CTP 账户、持仓、活动委托和成交回报为真相重新核对。状态含义见 [`glossary.md`](glossary.md)，正常启动流程见 [`live-trading.md`](live-trading.md)。

## 1. 通用排查顺序

1. 停止新的 `live` 或 `shadow` 进程，确认没有重复实例；
2. 执行只读 `afuture status --config <配置>`；
3. 保存错误输出、日志末尾、状态文件和审计记录，不先删除或覆盖；
4. 需要柜台事实时执行无报单 `afuture doctor --config <配置> --confirm-live`；
5. 核对账户、完整持仓、活动委托、交易日、合约参数和最近成交；
6. 只有根因清楚且对账通过后，才决定恢复、继续只减仓或保持停机。

不要通过删除状态文件、修改校验和、清空停机原因、降低风险阈值或重复启动进程来“恢复”。

## 2. 常见症状

| 症状 | 常见原因 | 安全处理 |
| --- | --- | --- |
| `status` 返回 2 或 `state_integrity` 失败 | 状态 JSON 截断、非 UTF-8、版本/序号/校验和错误或重复持仓 | 保留 current 和 `.prev`；确认磁盘和进程状态；不要自动回退。需要重建时走人工 `recover-state`。 |
| `status` 报路径不可写或磁盘不足 | 权限变化、目录不存在、磁盘接近耗尽 | 停止实盘进程，释放空间或修复目标目录权限；再次运行 `status`。不要把运行文件临时改到不持久的目录。 |
| `doctor` 报交易日或快照不一致 | CTP 尚未推送完整快照、夜盘交易日映射异常、断线重连未完成 | 等待新的账户和完整持仓事件；核对柜台交易日。不能用本机自然日期强行替换。 |
| `doctor` 报活动委托 | 上次进程退出前委托未结束，或账户存在外部委托 | 在柜台确认委托来源和状态；必要时人工撤单并等待最终回报，再重新预检。 |
| `doctor` 报持仓对账失败 | 今昨仓、多空、合约或交易所不一致，或有手工/其他程序交易 | 以柜台完整持仓为准查明差异。未解释差异前保持 `HALTED`，不要只比较净仓。 |
| 合约参数或目录检查失败 | 乘数、最小变动价位、保证金、手续费缺失或与静态配置冲突 | 从柜台和结算资料核对；不要猜测保证金或沿用过期静态参数开仓。 |
| 方向组合没有流动性快照 | 新部署、没有跨过完整交易日、状态文件缺失或快照身份不一致 | 连续运行 Shadow 至少一个完整柜台交易日；核对品种、合约、交易所和到期日覆盖。 |
| `directional_activity.json` schema/checksum 失败 | 文件来自旧版裸 completed 格式、被手工修改、截断或含无效 identity/数值 | 保存诊断副本；不要补写 checksum。移走不兼容文件后连续 Shadow 一个完整柜台交易日，重建 `completed` + `in_progress` envelope。 |
| Directional OHLC cache integrity 失败 | `directional_ohlc_cache.json` 被截断/篡改，schema、品种、日期、shape、正有限值、content digest 或 envelope checksum 不符 | 停止相关 runtime，保留 `status` 输出并把损坏文件明确移动到只读诊断位置；不要重签名或从研究文件拼接。确认 provider 已修复后，在原路径确实不存在 cache 的状态下启动一次，让首次可信结果 bootstrap 新文件，再运行 `status`/`doctor`。只修复 provider 不会覆盖仍留在原路径的损坏 cache。 |
| provider 历史修订被拒绝 | 新数据与已验证缓存的重叠开盘/收盘值不同 | 同时保留 cache、provider 原始响应、digest 和日期范围，向数据源确认修订原因。运行时只可继续使用仍覆盖 required day 的旧缓存，不能自动接受修订。 |
| 方向信号被判为过期 | 价格历史/已验证缓存未覆盖快照交易日、数据提供方停更、时间戳在未来 | 核对 `status` 的 cache latest date/digest 和 `doctor` required date；恢复数据后重新预检。不能用向前填充或修改系统时间绕过。 |
| 行情被判为 stale 或两腿不同步 | CTP 断线、单腿停更、时间戳时区错误或跨腿延迟过大 | 检查行情连接和两腿最新时间；恢复完整新行情前不增加风险。 |
| CTP live extra 无法导入或只在开发机通过 | `vnpy_ctp` 原生扩展与目标机 OS/CPU/Python ABI 不匹配，或目标机未安装 `.[live]` | 在最终部署机的同一虚拟环境重新安装并执行 import、无报单 doctor、Shadow 和重连验证；测试替身通过不能替代。 |
| 关键事件积压持续上升 | callback 消费不足、目标机阻塞，或单轮 100 条上限下关键事件产生率持续过高 | 读取 `delivery_counters()`，区分 `critical_backlog` 与正常 `ticks_coalesced`；停止新增风险并定位消费延迟。不要把 Tick 合并误判为成交丢失。 |
| 进入 `REDUCE_ONLY` | 单日熔断、实际总敞口超限、数据/执行证据不足但仍有风险 | 只允许系统或人工安全减仓；等待成交确认并重新读取真实持仓，不能手工改状态为 `RUNNING`。 |
| 进入 `HALTED` | 总回撤、保证金、可用资金、非正权益、对账、状态或基础设施硬失败 | 保持停机，定位首个硬失败。交易日切换不会自动清除这些原因。 |
| 保证金开仓被拒绝 | 目标超过保证金/可用资金门，缺少每手保证金，或保守缓冲后无容量 | 核对账户权益、各方向保证金率、价格、乘数和缓冲。接受缩量或不开仓，不降低硬门追求目标收益。 |
| 重复报单或成交疑虑 | 活动委托状态延迟、重连重复事件、外部程序共同交易 | 立即停止新增风险，核对 order ID、trade ID、活动委托和完整成交；不要通过重启赌重复事件会消失。 |
| 审计或告警文件写入失败 | 磁盘、权限、Webhook 或轮转异常 | 本地审计不可写属于运行风险；先修复持久化。Webhook 失败不能影响本地事实记录。 |

## 3. CTP 断线与重连

断线后不要假定本地的最后状态仍然最新。原生 CTP 当前交易日必须重新来自交易 API `getTradingDay()`；缺少 gateway、td_api、getter 或合法 `YYYYMMDD` 时保持失败关闭，不能使用本机自然日期或上次缓存值。重连必须重新取得：

- 当前柜台交易日；
- 新的账户快照；
- 完整持仓快照，而不是增量片段；
- 活动委托及其最终状态；
- 断线期间可能发生的成交；
- 当前合约参数和最新行情。

上述事实没有全部到齐时，系统应保持只减仓或停机。多次重连仍失败时停止进程，使用柜台客户端人工核对，不连续重启制造更多未知订单状态。

## 4. 人工状态恢复

只有本地状态无法可信加载、且柜台事实已经独立核验时才使用：

```bash
AFUTURE_RECOVERY_ACK=I_VERIFIED_CTP_POSITIONS \
afuture recover-state \
  --config config/afuture.directional-live.example.toml \
  --confirm-live \
  --confirm-adopt-state
```

恢复过程会检查合约参数和活动委托；发现活动委托时会尝试撤单并保持停机，不会直接采用状态。成功重建本地预期持仓后，停机开关仍然保留。随后必须再次运行 `status` 和 `doctor`，完成第二次独立对账，再按停机原因决定是否恢复运行。

`.prev` 只是上一份通过校验的诊断证据，不是自动恢复源。不能把它复制覆盖 current 后直接实盘。


### 4.1 Stress-90 operator-managed 连续性故障

`status`/`doctor` 会把严格外部门与 operator trust receipt 分开显示。`operator_managed` 下若报告 receipt 缺失、checksum/`.prev`/lineage 损坏、account/epoch/runtime 不匹配、registry/TDE 不再绑定、需要 roll-forward、需要 rebase 或需要新 permit，保持 `HALTED`，不要删除 artifact、复制 `.prev`、手改 checksum 或修改本机日期。

若 CTP 当前 Deposit/Withdraw 非零、出现人工/外部订单或成交、Broker/local 持仓漂移、unknown order/trade 或无法解释的权益变化，operator continuity 不再成立。先用柜台事实查明原因；发生合法外部资金/账户活动时走 `stress90-account-rebase`，然后重新建立 continuity。`stress90-operator-roll-forward` 本身绝不发送订单或撤单；成功后也不会解除 kill switch，必须重新运行 `status`、`doctor` 并签发新 technical permit。

## 5. 日志和证据

定位问题至少保留：

- 错误命令、退出码和完整错误文本；
- 发生时间、柜台交易日和运行模式；
- 状态文件及 `.prev`、`directional_activity.json`、`directional_ohlc_cache.json` 的副本；
- OHLC cache 的 content digest、latest date、required date 和 provider 原始响应日期范围；
- `delivery_counters()` 快照；
- 对应的日志、审计和告警片段；
- CTP 账户、完整持仓、活动委托和成交查询结果；
- 使用的配置文件摘要，但不包含密码、认证码或完整 Webhook 密钥。

先寻找时间最早的根因，后续大量拒单和停机日志通常只是同一故障的结果。不要在多个位置重复打印同一异常，也不要删除能解释账户状态变化的原始证据。

## 6. 何时不能自行恢复

出现以下任一情况时保持停机，并通过柜台客户端或期货公司确认：

- 柜台持仓、成交和结算单彼此不一致；
- 无法确认是否存在活动或已成交委托；
- 保证金、手续费、乘数或交易规则来源不可信；
- 本地与柜台差异无法解释；
- 状态损坏同时缺少完整审计证据；
- 交易所、柜台或网络持续异常；
- 账户权益或可用资金出现无法解释的变化。

生产上线前的逐项证据要求见 [`production-checklist.md`](production-checklist.md)。

## Stress-90 risk overlay mismatch or capacity failure

If status/Doctor reports `stress90_risk_overlay_identity` failed, do not delete state or edit the bound digest. Stop the runtime, keep it HALTED/kill-switched, finish or retire any current execution intent under its original digest, verify Broker/local flatness and zero active orders, reconcile, then run the explicit `stress90-activate` lifecycle to bind the new overlay. A smaller scale is still an identity change because old orders must retain their original interpretation.

If `stress90-capacity-report` exits `2`, inspect `hard_safety_failures`, `risk_manager_preview`, missing contract capacity/cost evidence and clipped products. Missing margin or all-zero commission evidence is not treated as zero cost. Integer zeroing can be a valid safe target; it is not a reason to enlarge scale automatically.
