# Stress-90 预热、身份与维护

从仓库根目录执行命令。`CONFIG` 为从 `config/afuture.directional-stress90-live.example.toml` 复制的私有配置，`RUNTIME` 与其 canonical state 目录一致；大写参数须替换。凭证变量与配置限制见[配置](configuration.md)，目标机/现场顺序见[CTP 运行](live-trading.md)。本页描述软件门，不声明真实柜台或无人值守已经认证。

## 固定预热

五份原始输入须从既有获授权的完整原档取得，文件名及 SHA-256 精确匹配；普通下载/刷新不能再造这组冻结字节。它们是当前预热依赖，不是实时账户来源。

| 文件 | SHA-256 |
| --- | --- |
| `broad_daily_universe.csv` | `c1d46bc113a79bd2e000bf5d66ee53c59337eda750b2d3b1409e40f3f4833d0f` |
| `return_target_specific_contracts.csv` | `f8b3f4232cb1bc9eaed65a4401dff6bed121faee064252727202874ad7d53c64` |
| `execution_aligned_weights.csv` | `250a55c1c18ecd3754f489136515275eec3a48d505fd41026d68ab43924e08c1` |
| `prior_two_year_broad_60m.csv` | `3351aa3ae8dec0cf0e9b9a64026181ac8ff2cc22858c442821fe3c94767036b1` |
| `two_year_broad_60m.csv` | `5faf112bb69dd5ddf48ed34419e2046b1c6bdd651b595ca46a46804d8317a27b` |

```bash
afuture stress90-bootstrap --config CONFIG --runtime-dir RUNTIME --through YYYYMMDD
```

须看到 `historical_candidate_parity=true`、规定精度内 batch/incremental 一致，以及 seed/policy/OI 创建成功。候选 SHA 为 `8e38dbf6441b561dd1728df08665b94b15cc3358823257505c2fcb9d63f09f28`；固定 Base 权威为 SHA-pinned weights，当前 UNKNOWN-aware Base 不冒充旧归档反推 parity，官方 profile 的 `base_max_abs_error=null`。

这组 SHA 有精确豁免清单：daily/specific 的 AP/CF 在 2024-01-30、2024-03-01 及 AP 在 2024-05-10 五项；weights 缺 2022-09-21 target；TA OI 缺 2024-05-20..2024-08-20 的 66 source sessions。清单参与 seed digest，仅同完整 manifest 有效，任何差异失败。UNKNOWN OI 不支持 entry/add/reversal，UNKNOWN close 不支持 entry/add；减仓退出继续。live OHLC 从最后缺口后的完整 session 起，seed 不带历史账户收益。

bootstrap 使用 owner store 的 create-only、artifact locks 和 token 验证；OI 为最终 commit point。孤儿 sidecar、损坏、并发替换、写入/`fsync` 歧义或 cleanup 失败保留证据，不能靠重试覆盖。缺输入、SHA/parity 失败或原档不可得应报阻断，不换数据、改 hash、伪造 seed。

## Bundle 与部署身份

从从未绑定账户的干净 bootstrap runtime 制作 production bundle，再安装到空目标：

```bash
afuture stress90-bundle-create --config CONFIG --runtime-dir CLEAN_BOOTSTRAP --output BUNDLE
afuture stress90-bundle-verify --bundle BUNDLE
afuture stress90-bundle-install --config CONFIG --bundle BUNDLE --runtime-dir RUNTIME
afuture deployment-seal --config CONFIG --bundle BUNDLE --runtime-dir RUNTIME
afuture deployment-verify --config CONFIG --runtime-dir RUNTIME
```

bundle 检查固定输入、候选/parity、成员 allowlist、大小、身份和语义，拒绝路径穿越、重复/链接/未知成员、截断及额外字节；fixture 不可标为 production-ready。安装 staging 验证后原子发布，不覆盖运行目录。

seal 绑定源码、配置、constraints、Python/OS/CPU/executable、原生 CTP、runtime、registry、bundle/policy/seed/products/risk overlay。漂移阻断 `doctor`/`shadow`/`live`；重新 seal 失效旧技术 permit。它不代替目标机认证。

## Registry、activation 与新数据

仅没有任何 durable evidence 的 pristine 机器路径可初始化 registry：

```bash
AFUTURE_ACCOUNT_RUNTIME_REGISTRY_ACK=INITIALIZE_AFUTURE_MACHINE_ACCOUNT_REGISTRY afuture stress90-registry-init --config CONFIG --confirm-initialize --operator-reason REASON
```

已有 registry/lineage/nonce/receipt 缺失或损坏是事故，不能删 marker、换路径或新 nonce 重建。永久 nonce ledger 达 800000 告警、1000000 硬上限；原件及 authenticated path nodes 不删除。

首次 activation 或 overlay 重绑定前须 HALTED、kill switch、Broker/local 空仓、无 active/unknown 委托、fresh snapshots 和 reconcile，旧 intent 已完成或退役。每个新 lifecycle 操作用新 64 位十六进制 `OP_ID`，只有完全同一 prepared 操作重试复用。

```bash
AFUTURE_STRESS90_ACTIVATION_ACK=I_CONFIRM_STRESS90_POLICY_ACTIVATION afuture stress90-activate --config CONFIG --confirm-live --confirm-activation --operation-id OP_ID --operator-reason REASON
afuture status --config CONFIG
afuture directional-ohlc-refresh --config CONFIG --current-trading-day YYYYMMDD
afuture stress90-oi-collect --config CONFIG --confirm-live
afuture stress90-prepare-decision --config CONFIG --confirm-live
```

activation 只绑定身份，结果仍 HALTED/kill switch。OI collector 为零报单 sidecar，完整权威柜台日才能形成 coverage；`--once` 仅一个批次安全检查。prepare-decision 在 HALTED 逐日补齐同源数据并保存 exactly-once decision，不跳 target gap。

OHLC refresh 的日期须来自实际 CTP evidence，路径固定为 canonical runtime 下 cache/TDE；取得同账户 lease，重验 account/epoch/registry receipt/lifecycle 后才调用 provider。provider 追加不能删改缓存日期或值，缺 required day 不开仓。`.pending` witness、symlink/identity/CAS 或持久化歧义不能人工清除。

## Doctor、容量和技术许可

```bash
afuture doctor --config CONFIG --confirm-live
afuture stress90-capacity-report --config CONFIG --confirm-live --output CAPACITY_JSON
```

须核对 fresh Broker/session、policy/seed/overlay、target continuity、50 品种 OHLC、九品种 OI coverage/activity、全部整数 stages、reduction/opening、cost/margin/quotes。Doctor 对必需合约不受 `--metadata-limit` 截断，`orders_sent=0`；任一 P0 失败返回 2。capacity report 也零报单/零撤单，缺 margin 或全零 commission 不当零成本，整数归零不是提高 scale 的理由。

内部 P0 全过且人工已复核时才签技术 permit：

```bash
AFUTURE_STRESS90_ACTIVATION_PERMIT_ACK=I_CONFIRM_STRESS90_TECHNICAL_ACTIVATION afuture doctor --config CONFIG --confirm-live --issue-stress90-permit
```

permit 是一次 HALTED→RUNNING 技术转换，不能证明 Shadow、测试柜台或真钱门。它先在 lease 内消费再写 state；两步间崩溃仅在相邻完整 checksum 链、原 generic state 与全部新证据精确一致时补齐同次提交。状态、证据、身份变化或人工/硬停机使此窄续接失效，不使用 `.prev` 或再次授权。

## 隔离 Shadow 与来源比较

令 `SHADOW_RUNTIME` 为 `CONFIG` 的 state 目录下 `shadow/`，五输入、seed/policy/OI/intent/activity/cache 全在该独立目录，不借用 live account path。单独 bootstrap；base CONFIG 用于 activate/doctor/shadow 和带 `--shadow-account` 的 seal/verify/prepare-session。独立 state 指向 SHADOW_RUNTIME 的配置仅给 OHLC sidecar，避免再次追加 shadow。激活必填新的 `--operation-id`，production doctor 前先完成同角色 seal/verify。

```bash
afuture stress90-bootstrap --config CONFIG --runtime-dir SHADOW_RUNTIME --through YYYYMMDD
afuture deployment-seal --config CONFIG --bundle BUNDLE --runtime-dir SHADOW_RUNTIME --shadow-account
afuture deployment-verify --config CONFIG --runtime-dir SHADOW_RUNTIME --shadow-account
AFUTURE_STRESS90_ACTIVATION_ACK=I_CONFIRM_STRESS90_POLICY_ACTIVATION afuture stress90-activate --config CONFIG --runtime-dir SHADOW_RUNTIME --shadow-account --confirm-live --confirm-activation --operation-id OP_ID --operator-reason REASON
AFUTURE_STRESS90_ACTIVATION_PERMIT_ACK=I_CONFIRM_STRESS90_TECHNICAL_ACTIVATION afuture doctor --config CONFIG --shadow-account --confirm-live --issue-stress90-permit
afuture shadow --config CONFIG --confirm-live --duration-seconds 3600
afuture quality-report --config CONFIG --shadow --output QUALITY_JSON
afuture stress90-oi-compare --config CONFIG --runtime-dir SHADOW_RUNTIME --trading-day YYYYMMDD --vendor VERIFIED_VENDOR_JSON --output COMPARISON_JSON
```

多日观察 first/last、volume、dominant、flow、HHI/freezes、整数目标及计划/成交质量。comparator 无 Broker/credentials/订单权限；未解释 flow 差异保持 `activation_blocked=true`。Sim/Shadow 不拥有真实 CTP 后续纯换月凭据。

## 换月与日终边界

entry/add/reversal 限各产品固定首窗，错过不追开；减仓仍须过实时行情、交易日、今昨仓、数量及最终风控。先减后开不变。

真实 CTP 的纯同品种同方向换月可在后续真实 session 完成原 durable intent：同日/epoch/decision/overlay，旧腿全平，初始新腿为空且当前只有今仓；journal 与终态订单完整认证，累计新腿 OPEN 数量等于当前持仓，无 CLOSE/active/unknown/未落盘成交。`max(累计 OPEN 成交名义, 当前持仓 mark 名义)+整笔 opening 上界` 不超初始 incumbent 替代预算，数量不超原授权。BUY 用实际 limit，SELL 用可信涨停上界；超额整笔拒绝不拆单。Sim/Shadow 仍首窗。关键边界重验已可见事件，不能保证未来迟到回报不存在。

正常日终使用 `stress90_day_end` 技术暂停、原阶段授权和 lifecycle WAL；它阻止新风险，提交期间阻止写单，不能清人工/硬风险 kill switch。最终资金、session、市场和旧日风险均须认证；更正阻断续行。当前真实 CTP 未装配认证最终结算提供者，`stress90-settlement-roll-forward` 在启动 gateway 或写 state 前失败关闭，确认参数不能替代来源。

D+1 PreBalance、当前 Deposit/Withdraw、TransferSerial、opaque SettlementInfo、operator assertion 均不能证明完整 D 日最终资金；必须有绑定账户/币种/day/SettlementID/request 的 final、完整记录，文本另需正式 grammar、真实 fixtures 与独立台账。资金差异不能计策略收益。

严格非相邻 session ledger 当前亦未认证。OI v4 的独立验证接口不等于生产来源；标准 runtime 不装配，周末/假期保持关闭。getTradingDay、长连接、静态日历、OHLC 端点或两个日期不能替代完整官方会话链。离线注入只证明消费者/恢复机械，未证明真实一个月资金与会话连续性。

## 可选 operator-managed 跨日

默认 `strict` 不变。仅个人独占、无手工/外部订单或成交、无入出金的账户可显式配置 `account_continuity_mode="operator_managed"`。HALTED/kill switch 下核对 fresh account、完整 positions/orders/trades、journal、registry/TDE、市场/policy 后：

```bash
AFUTURE_OPERATOR_CONTINUITY_ACK=I_CONFIRM_EXCLUSIVE_ACCOUNT_AND_NO_EXTERNAL_ACTIVITY afuture stress90-operator-roll-forward --config CONFIG --confirm-live --confirm-operator-continuity --operation-id OP_ID --operator-reason REASON
```

不报单/撤单，未知、漂移、资金活动、未终态或不齐数据均拒绝；该命令不支持 `--runtime-dir`/`--shadow-account`。成功仍 HALTED/kill switch、metadata 未验证且需新 Doctor permit。receipt 为 `operator_trust`，不提升官方 funding/session 或 `financial_continuity_verified`；不能冒充结算认证。外部活动先停机查明，再走必要 rebase。

## 账户、policy 与 journal 维护

全部维护遵循 HALTED/kill switch、无未知/活动委托、fresh snapshot/reconcile、同账户/runtime lease 与精确 WAL/CAS 身份；账户重基、迁移和 epoch 封存另须 Broker/local flat。确认值见下表；示例只列命令，执行前在受控进程环境设置相应变量。

| 命令 | 确认变量=值 | 额外参数 |
| --- | --- | --- |
| `stress90-account-rebase` | `AFUTURE_STRESS90_REBASE_ACK=RESET_STRESS90_ACCOUNT_PATH` | `--confirm-rebase` |
| `directional-policy-migrate` | `AFUTURE_DIRECTIONAL_POLICY_MIGRATION_ACK=I_CONFIRM_DIRECTIONAL_POLICY_MIGRATION` | `--to execution_aligned --confirm-migration` |
| `stress90-order-journal-rollover` | `AFUTURE_STRESS90_ORDER_EPOCH_ACK=I_CONFIRM_STRESS90_CTP_ORDER_JOURNAL_EPOCH_ROLLOVER` | `--confirm-rollover` |
| `stress90-crash-fill-recover` | `AFUTURE_STRESS90_CRASH_FILL_RECOVERY_ACK=I_CONFIRM_AUTHORIZED_STRESS90_CRASH_FILL_RECOVERY` | `--confirm-recovery` |

```bash
afuture stress90-account-rebase --config CONFIG --confirm-live --confirm-rebase --operation-id OP_ID --operator-reason REASON
afuture directional-policy-migrate --config CONFIG --to execution_aligned --confirm-live --confirm-migration --operation-id OP_ID --operator-reason REASON
afuture stress90-order-journal-rollover --config CONFIG --confirm-live --confirm-rollover --operation-id OP_ID --operator-reason REASON
afuture stress90-crash-fill-recover --config CONFIG --confirm-live --confirm-recovery --operation-id OP_ID --operator-reason REASON
```

- 同账户 rebase 须已认证非零外部资金流，调整资金基线，不能用零流清回撤/日损；新账户用 verified settlement 建基线。candidate/HHI/seed 不重算。
- policy schema 6 的 `completed_account_reserve_triggered` 永久锁存；rebase、epoch、重启或净值恢复均不清除。缺历史证据的旧 schema 保留并阻断，无自动升级/seed 重建。
- policy migration 仅认证 Stress-90→`execution_aligned`；返回 Stress-90 须精确 migrated marker，并运行 `stress90-activate`，同时带 `--confirm-activation --confirm-rebase` 与 activation/rebase 两个 ACK。overlay 变更也需 activation，不能热改旧订单解释。
- journal 20000 order/10000 fill identity 是硬界。epoch 封存全审 cold chain、跨 epoch 防重；不删 archive/manifest。中断保留 pending，用同 OP_ID 重试；成功仍 HALTED 且旧 permit 失效。
- crash-fill 只接纳完整当前 session 查询认证的已授权成交，critical fence 重验后 checkpoint；不发单/撤单，不清停机。prepared lifecycle 存在即拒绝。state/checkpoint 中断用原 OP_ID；current/lineage 缺失不提升 `.prev`。
- rebase/migrate 支持 `--runtime-dir ... --shadow-account`；crash-fill/journal rollover 不支持 `--shadow-account`。未知能力先读对应 `--help`，不得落到临时模拟账户。

## 备份、恢复与每日操作

HALTED/kill switch 且无 prepared lifecycle 时，备份显式账户证据 allowlist，排除配置、凭证、日志与报告：

```bash
afuture backup-runtime --config CONFIG --output PRIVATE_BACKUP
afuture verify-backup --backup PRIVATE_BACKUP
afuture restore-runtime --backup PRIVATE_BACKUP --runtime-dir RUNTIME --account-registry-path REGISTRY --registry-staging-path EMPTY_STAGE
```

验证使用现有各 store loaders 与跨文件身份，不以 tar 可解压或单 hash 为准。恢复仅空 runtime/安全 registry staging，须匹配原 canonical 身份；不连接 Broker、不发单/撤单，结果 HALTED/kill switch、旧 permit 失效。

随后 `status→deployment-verify→prepare-session→必要维护→doctor→capacity→新技术 permit`，资料不足保持停止。实盘/扩大风险另须现场授权。lineage、nonce、current/`.prev`、archive、`.pending` 或持久化歧义一律保全原件和备份，不删目录、手写 checksum、跳日期或自动重建。事故见[排障](troubleshooting.md)。
