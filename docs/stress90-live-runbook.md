# Stress-90 运行手册

本文给出从固定 bootstrap 到 Shadow、测试柜台和极小真钱的操作顺序。所有命令假设从仓库根目录运行，示例配置为 `config/afuture.directional-stress90-live.example.toml`。先复制示例到目标机的私有运维目录并填写前置地址；不要把凭证、地址或账户身份提交到 Git。

## 1. 不可跳过的阶段门

```text
固定输入 bootstrap/parity
→ 本地 status
→ CTP 空仓 activation
→ order-incapable OHLC refresh
→ doctor（orders_sent=0）
→ 独立持久 Shadow
→ vendor/CTP 60m comparator
→ 多日 Shadow review
→ 测试柜台
→ 极小真钱
→ 扩大风险人工审批
```

任一步失败都停在当前阶段。代码、CI 或历史 `112.100053%` 不能替代后续现场证据。

## 2. 目标机准备

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[live]" -c constraints/core-dev.txt -c constraints/live.txt
python -c "import vnpy_ctp"

cp config/afuture.directional-stress90-live.example.toml \
  /secure/path/afuture.directional-stress90.toml

afuture validate --config /secure/path/afuture.directional-stress90.toml
```

确认配置明确包含：

```toml
[directional]
enabled = true
policy = "stress90"
account_exclusive = true
account_continuity_mode = "strict"  # 默认；个人独占账户才考虑 operator_managed
```

并保持固定 50 品种。commissioning 风险可以更严格，不能更宽；任何差异都要保留在审计中，且不能声称精确复现固定历史矩阵。

CTP 凭证只设置在受控进程环境：

```bash
export AFUTURE_CTP_USER='...'
export AFUTURE_CTP_PASSWORD='...'
export AFUTURE_CTP_BROKER='...'
export AFUTURE_CTP_APP_ID='...'
export AFUTURE_CTP_AUTH_CODE='...'
export AFUTURE_CTP_ACCOUNT_ID='...'
export AFUTURE_CTP_CURRENCY_ID='CNY'
# 柜台原始账户响应提供相应身份时再填写：
export AFUTURE_CTP_INVESTOR_ID='...'
export AFUTURE_CTP_INVEST_UNIT_ID='...'
export AFUTURE_LIVE_ACK=I_UNDERSTAND_FUTURES_RISK
```

不要把这些值粘贴到日志、issue、PR、文档或命令历史共享记录。

## 3. 固定历史 bootstrap

把五份固定字节输入放入 live runtime directory；文件名必须完全一致：

```text
runtime/broad_daily_universe.csv
runtime/return_target_specific_contracts.csv
runtime/execution_aligned_weights.csv
runtime/prior_two_year_broad_60m.csv
runtime/two_year_broad_60m.csv
```

先对照 [`stress90-final-evidence.md`](stress90-final-evidence.md) 和命令输出核验每份 SHA-256，再执行：

```bash
afuture stress90-bootstrap \
  --config /secure/path/afuture.directional-stress90.toml \
  --runtime-dir runtime \
  --through YYYYMMDD
```

通过条件：

- `historical_candidate_parity=true`；
- candidate SHA-256 为 `8e38dbf6441b561dd1728df08665b94b15cc3358823257505c2fcb9d63f09f28`；
- 固定归档以 SHA-pin 的 `execution_aligned_weights.csv` 作为 Base 权威输入；由于当前
  Base 已加入 UNKNOWN-aware 安全语义，不再把新实现对旧归档的反推伪装成历史 parity，
  官方 profile 的 `base_max_abs_error` 为 `null`；
- batch/incremental 最大差在实现规定的机器精度范围内；
- `stress90_bootstrap_seed.json` 与 `stress90_policy_state.json` 成功创建。

这组固定字节含已审计的精确缺口：broad daily 与 specific-contract daily 各有相同的
5 个 date/product 缺口，weights 缺 `2022-09-21` target，TA 60m OI 缺
`2024-05-20..2024-08-20` 的 66 个 source sessions。代码只对这组完整 SHA manifest
和这份精确清单豁免；多一个、少一个或位置变化均失败。缺口清单写入 seed 并参与
`seed_digest`。未知 OI 不得支持 entry、加仓或反转；未知 close 不得支持 entry 或同向
加仓；二者都不阻止减仓或退出。live OHLC cache 从最后缺口后的首个完整 session 开始。

若任一文件不存在、SHA 不匹配、缺口清单变化或 candidate parity 失败，立即停止。
禁止下载另一版本数据、修改摘要或生成伪 seed。seed 只继承候选 state 和完整 prior HHI，
不继承历史回测账户收益。

## 4. 首次 live identity activation

先只读检查：

```bash
afuture status --config /secure/path/afuture.directional-stress90.toml
```

首次 activation 必须确认柜台和本地都空仓、没有活动委托、当前状态为 `HALTED` 且 kill switch 开启，并完成 fresh snapshot/reconcile。设置一次性强确认后运行：

```bash
export AFUTURE_STRESS90_ACTIVATION_ACK=I_CONFIRM_STRESS90_POLICY_ACTIVATION
STRESS90_OPERATION_ID="$(openssl rand -hex 32)"

afuture stress90-activate \
  --config /secure/path/afuture.directional-stress90.toml \
  --confirm-live \
  --confirm-activation \
  --operation-id "$STRESS90_OPERATION_ID" \
  --operator-reason 'initial Stress-90 commissioning after verified bootstrap'

unset AFUTURE_STRESS90_ACTIVATION_ACK STRESS90_OPERATION_ID
```

成功结果仍必须是 `runtime_mode=HALTED`、`kill_switch=true`。这一步只绑定 policy/definition/products/seed identity，不授予报单许可。

## 5. 准备 completed OHLC cache

从正在运行的 CTP 会话取得当前权威交易日 `YYYYMMDD`，再在无订单权限的独立进程刷新：

```bash
afuture directional-ohlc-refresh \
  --config /secure/path/afuture.directional-stress90.toml \
  --current-trading-day YYYYMMDD
```

该命令可以访问外部 provider，但 live `run_once()`、Tick 和 Broker callback 只读已验证 cache，不同步访问网络。provider 修订重叠历史、缺少 required completed day 或 cache checksum 不可信时不得继续开仓。

刷新命令只接受配置中 state 所在 canonical runtime 下的固定文件名
`directional_ohlc_cache.json` 和 `ctp_trading_day_evidence.json`。它取得与 lifecycle
相同的 account/runtime lease 后，重新读取 policy、trading-day evidence、machine registry
和 lifecycle coordinator；任一 account、epoch、runtime、account-specific receipt 不一致，或
coordinator 仍为 `prepared`，都必须在构造 provider 前失败。registry 的 machine-wide
sequence/checksum 只作审计快照；另一账户推进 registry 不会废除本账户由 binding payload、
account revision、last operation 和 receipt digest 固定的证据。registry schema 3 以认证
Patricia-Merkle root 和不可变 receipt 为每次 bind/advance/switch/acknowledgement/transfer/recovery
持久化 operation kind 和精确参数；nonce 全 machine、全账户、全 operation kind 永久唯一，
只有 kind 与全部参数都相同的重试可以复用。

cache artifact lock 从 final-path 检查一直持有到 provider load 和 durable commit。首次创建使用
`O_EXCL|O_NOFOLLOW`，更新只写入已验证身份的 `O_NOFOLLOW` regular-file fd。写前会 durable
创建 `.pending` witness；truncate、write、file/directory `fsync` 或 witness cleanup 的歧义失败
都会保留 witness，后续 load/save/refresh 在 provider side effect 前持续失败关闭。不得手工删除
`.pending`；必须按事故流程保全并核验 cache、witness、目录和外部备份。

## 6. 无报单 status 和 doctor

```bash
afuture status --config /secure/path/afuture.directional-stress90.toml

afuture doctor \
  --config /secure/path/afuture.directional-stress90.toml \
  --confirm-live \
  --metadata-limit 4
```

Stress-90 doctor 会忽略 `metadata-limit` 对必需计划合约的截断，收集所有 activity-selected Stress 合约的 bounded quote/spec evidence。输出必须保持 `orders_sent=0`，并核验：

- policy、seed、state 和 manifest identity；
- CTP current day 与 target-day continuity；
- OHLC、60m OI、activity 对齐和九品种 coverage；
- 50 品种 Base OHLC；
- 合约目录、quotes、live margin/commission；
- raw/margin-fitted/drawdown-frozen/HHI-frozen/final lots；
- reductions/openings、gross、tracking error 和集中度；
- 开仓、平昨、平今、1 tick、spread/depth 的 15bp 成本门；
- 首次 activation 的空仓、无活动委托和 reconcile；
- 外部现场门仍为未验证。

任一 P0 失败时 `stress90_ready=false`。doctor 返回 2 是阻断证据，不得通过删状态、改本机日期、把 missing OI 当 0 或使用 `.prev` 绕过。

当且仅当内部 P0 全部通过时，使用额外强确认签发一次性技术启动许可：

```bash
export AFUTURE_STRESS90_ACTIVATION_PERMIT_ACK=I_CONFIRM_STRESS90_TECHNICAL_ACTIVATION

afuture doctor \
  --config /secure/path/afuture.directional-stress90.toml \
  --confirm-live \
  --issue-stress90-permit

unset AFUTURE_STRESS90_ACTIVATION_PERMIT_ACK
```

该 permit 只允许一次 `HALTED → RUNNING` 技术状态转换，不能代表多日 Shadow、测试柜台、极小真钱或扩大风险门已通过。permit 在账户 lease 内先被消费、再保存 RUNNING；两步之间崩溃时状态保持 `HALTED` 且 permit 已消费，必须重新运行 Doctor 签发，禁止自动复用。


### 6.1 可选：个人独占账户的 operator-managed 跨日

默认 `strict` 不变，仍要求原有 authoritative settlement/session 证据。只有该账户完全由本进程独占、运行期间不进行人工交易/外部委托/入金/出金时，才可在私有生产配置中显式设置：

```toml
[directional]
account_continuity_mode = "operator_managed"
```

跨日时先保持 `HALTED` 和 kill switch，不启动 live 交易循环。确认 CTP fresh account、完整持仓、活动委托、当前 session order/trade、journal、registry、TradingDayEvidence、OHLC/activity/OI/policy 均一致后：

```bash
export AFUTURE_OPERATOR_CONTINUITY_ACK=I_CONFIRM_EXCLUSIVE_ACCOUNT_AND_NO_EXTERNAL_ACTIVITY
OPERATION_ID="$(openssl rand -hex 32)"

afuture stress90-operator-roll-forward \
  --config /secure/path/afuture.directional-stress90.toml \
  --confirm-live \
  --confirm-operator-continuity \
  --operation-id "$OPERATION_ID" \
  --operator-reason 'exclusive account continuity; no manual trade/order/deposit/withdrawal'

unset AFUTURE_OPERATOR_CONTINUITY_ACK OPERATION_ID
```

该命令不报单、不撤单；Deposit/Withdraw 非零、活动委托、unknown order/trade、journal 未终态、持仓漂移、source/data day 不齐或 prepared transaction 不匹配都会失败关闭。自然日间隔大于 1（例如周五到周一）只记录为 operator trust；不把本机日期、`BDay`、静态节假日日历或 OHLC 端点本身当作官方 session ledger。

成功后仍必须看到 `HALTED`、`kill_switch=true`、`metadata_verified=false`，并依次重新运行 `status`、无报单 `doctor`、再签发新的 technical permit。receipt 不能把 `external_activation_gates_completed` 变成 true。若发生任何外部账户活动，先执行 `stress90-account-rebase`；同账户 rebase 会推进 account epoch，后续 operator receipt 只可在 registry/TDE 已证明该新 epoch 的前提下重新锚定，旧 receipt 不能跨 epoch 授权。

## 7. 独立持久 Shadow

Stress-90 Shadow 使用 `runtime/shadow/` 下独立且持久的 account/policy/OI/intent state，不能复用 live account soft path。先把同一组已经核验的五个固定输入复制到该目录，再单独 bootstrap 和 activation：

```bash
mkdir -p runtime/shadow
cp runtime/broad_daily_universe.csv runtime/shadow/
cp runtime/return_target_specific_contracts.csv runtime/shadow/
cp runtime/execution_aligned_weights.csv runtime/shadow/
cp runtime/prior_two_year_broad_60m.csv runtime/shadow/
cp runtime/two_year_broad_60m.csv runtime/shadow/

afuture stress90-bootstrap \
  --config /secure/path/afuture.directional-stress90.toml \
  --runtime-dir runtime/shadow \
  --through YYYYMMDD

# 使用 state_path 指向 runtime/shadow 的独立 Shadow 配置；固定写入该 runtime 的 cache。
afuture directional-ohlc-refresh \
  --config /secure/path/afuture.directional-stress90-shadow.toml \
  --current-trading-day YYYYMMDD \
  --cache runtime/shadow/directional_ohlc_cache.json

export AFUTURE_STRESS90_ACTIVATION_ACK=I_CONFIRM_STRESS90_POLICY_ACTIVATION

afuture stress90-activate \
  --config /secure/path/afuture.directional-stress90.toml \
  --runtime-dir runtime/shadow \
  --shadow-account \
  --confirm-live \
  --confirm-activation \
  --operator-reason 'initialize isolated persistent Stress-90 Shadow state'

unset AFUTURE_STRESS90_ACTIVATION_ACK

export AFUTURE_STRESS90_ACTIVATION_PERMIT_ACK=I_CONFIRM_STRESS90_TECHNICAL_ACTIVATION

afuture doctor \
  --config /secure/path/afuture.directional-stress90.toml \
  --confirm-live \
  --shadow-account \
  --issue-stress90-permit

unset AFUTURE_STRESS90_ACTIVATION_PERMIT_ACK

afuture shadow \
  --config /secure/path/afuture.directional-stress90.toml \
  --confirm-live \
  --duration-seconds 3600
```

重复运行多个完整柜台交易日，覆盖夜盘、跨午夜、rollover、正常停机和至少一次受控重启。Shadow 使用真实 CTP raw evidence observer 和 live manager，但订单只进入 `ShadowBroker`/本地模拟，绝不调用 CTP 报单。

每个新柜台交易日前都必须由无订单权限的 sidecar 更新上述 Shadow cache；禁止让 Shadow 读取 live 目录的 cache，或因 Shadow cache 不完整而退回同步 provider 请求。

汇总执行质量：

```bash
afuture quality-report \
  --config /secure/path/afuture.directional-stress90.toml \
  --shadow \
  --output runtime/shadow_execution_quality_report.json
```

每日复核 Base/OI/cost/survivor、HHI/prior median、两个 freeze、全部整数 stages、reduction/opening plan、expected open、planned/fill price、slippage、commission、p95 one-way cost、partial fill、reject、latency 和 model/actual turnover。

## 8. CTP 与 vendor 60m 同源比较

把批准且包含完整 source manifest 的 vendor JSON 放到只读位置。比较命令不加载 CTP credentials、不创建 Broker，也没有报单对象：

```bash
afuture stress90-oi-compare \
  --config /secure/path/afuture.directional-stress90.toml \
  --runtime-dir runtime/shadow \
  --trading-day YYYYMMDD \
  --vendor /verified/vendor/stress90_oi_YYYYMMDD.json \
  --output runtime/stress90_oi_comparison_YYYYMMDD.json
```

逐产品解释 first open、last close、first/last hold、total volume、dominant symbol 和 flow。任何 unexplained flow difference 都保持 `activation_blocked=true`。

## 9. 测试柜台

只有多日 Shadow、数据 comparator 和 doctor 均通过后，才在专用测试柜台重复以下现场用例：

- entry、同向 add、reduction、exit、confirmed/unconfirmed reversal 和 same-product roll；
- reduction 成交确认前绝无 opening；
- FAK partial fill、reject、撤单、重复回报和 unknown event；
- callback Tick flood 下 critical FIFO；
- 夜盘跨午夜、trading-day rollover、断线重连和进程重启；
- prepared decision 后崩溃、首单后崩溃和部分成交后崩溃；
- Broker/local drift 时 `HALTED`；
- OHLC/OI/activity 缺失时空仓 0 opening、有仓 `REDUCE_ONLY`；
- 真实冻结保证金、手续费和成交成本与 model audit 的差异。

测试柜台结果、柜台环境、日期、合约、订单/成交 identity 和 operator 必须写入不可变外部验收记录。不要用单元测试替代该记录。

## 10. 极小真实资金

极小真钱只在独立人工审批后进行。审批前再次执行 `status`、`doctor`，确认：

- 目标机 ABI、前置、重连和 callback 顺序已验证；
- 当前账户独占，Broker/local 空仓且无活动委托；
- 完整日 OI/coverage、OHLC/activity 和 target continuity 无 gap；
- `authoritative_nonadjacent_session_ledger` 不再是外部 blocker；若当前交易日与上一日非相邻，
  必须先有真实 official ledger，不能人工确认；
- 实际 commission、margin、tick/slippage 与 15bp 历史假设兼容；
- Shadow/test counter 中无未解释 flow、partial/reject 或 tracking discrepancy；
- 当日处于每个产品首个允许 entry window；
- commissioning limits 足够保守且未放宽 hard envelope。

真实命令必须由获批 operator 在现场执行：

```bash
afuture live \
  --config /secure/path/afuture.directional-stress90.toml \
  --confirm-live
```

错过首个 entry window 时当日不得追开，但 reductions 和硬风险退出继续允许。每轮 reductions 后必须等待 Broker 成交、重新读账户/持仓并重新运行 `RiskManager`，才可 opening。

## 11. 结算与上一交易日资金闭合门

`stress90-settlement-roll-forward` 当前不可用，即使提供 `--confirm-roll-forward`、强确认环境变量、
合法 operation id 和 operator reason，也会在构造 Broker 或写入任何 state/lifecycle artifact 前
失败关闭。不要循环重试或修改本地状态来绕过；运行 `status`/`doctor` 时
`prior_day_final_funding_settlement_witness` 必须继续显示为未验证的外部 activation blocker。

当前锁定的 CTP Python ABI 只有以下相关原语：

- `QryTradingAccount`：D+1 `PreBalance`、`SettlementID` 和 D+1 当前日累计
  `Deposit/Withdraw`；它不能证明 D 日最后一次 snapshot 后没有资金变化；
- `QryTransferSerial`：request-bound 银期转账流水，但请求没有 completed-day/retention 边界，
  也不能排除非银期或柜台人工资金调整；
- `QrySettlementInfo`：带 account/day/settlement/sequence 的结算单分片，但资金内容是无结构
  `Content` 文本，当前没有经目标柜台批准的稳定解析契约。

只有目标柜台提供不可变、响应完整且具有明确 finality 的结构化最终记录，绑定账户、币种、
completed day、SettlementID、request/generation，并给出包括非银期/人工调整在内的最终
`Deposit` 与 `Withdraw`，才能重新评审该命令。若只能提供结算单文本，还必须有柜台文档化 grammar、
真实目标机 fixtures 和独立完整资金台账对账。未知格式、sequence gap、缺少 `bIsLast`、查询
超时/错误、identity 不一致、未知 transfer code/status 或保留范围不完整都必须保持阻断。

严禁用 operator 猜测、D+1 当日 `Deposit/Withdraw=0`、`PreBalance` 差额、单独
`TransferSerial` 或 FakeBroker/provider/parser fallback 替代见证；无法闭合的资金变化不得计入
策略收益。

### 11.1 非相邻自然日 session continuity 门

`status` 和 `doctor` 必须列出外部 blocker
`authoritative_nonadjacent_session_ledger=not_available_from_pinned_vnpy_ctp_6_7_11_4`。
当前锁定 ABI 的 `getTradingDay()` 只有当前日；`QryUserSession` 是登录会话，`QryExchange` 只有
交易所元数据，`OnRtnInstrumentStatus` 是没有 `TradingDay` 的当前状态推送。它们都不是历史
trading-session ledger。raw MD generation 即使没有断开，也只能为相邻自然日记录
`ObservedTradingDayTransition`；持久化 decoder 和 account-day consumer 会拒绝周五到周一或
节假日 gap。

以下内容一律不是可接受证据：`pandas.BDay` 或其他 business-day 推断、本机/UTC 日期、人工维护或
猜测的节假日、operator assertion、静态时段 manifest、Sina OHLC endpoint/行、只观察 source 与
target 两天，以及跨越 gap 的长连接。不得修改 `natural_days == 1`、手写 checksummed transition、
删除 evidence 或用 FakeBroker/provider fallback 绕过。

未来只有真实目标柜台或交易所发布的不可变 official ledger 才可触发新一轮设计评审。交付物必须
包含 issuer/exchange、产品/session scope、有效区间、完整有序的 official trading-day chain、
版本/发布 identity、内容 digest 或签名、明确 finality/completeness 语义、真实目标机 fixture，
以及 request/generation（若经柜台查询）。任何覆盖边界不清、漏日、重复、乱序、未知版本、响应
不完整或身份不一致都继续保持 `HALTED`。在上述外部输入到位前，不创建空壳 ledger schema，
不运行非相邻 settlement/account-day roll-forward，也不宣称周末/节假日 continuity 已闭合。

## 12. 充值、出金或更换账户

运行期间禁止手工交易、其他策略、充值或出金。发生资金或账户生命周期变化后，先人工设置 kill switch 并进入 `HALTED`，清空 Broker 与本地持仓、处理全部活动委托、完成 fresh snapshot 和 reconcile，再执行：

```bash
export AFUTURE_STRESS90_REBASE_ACK=RESET_STRESS90_ACCOUNT_PATH
STRESS90_OPERATION_ID="$(openssl rand -hex 32)"

afuture stress90-account-rebase \
  --config /secure/path/afuture.directional-stress90.toml \
  --confirm-live \
  --confirm-rebase \
  --operation-id "$STRESS90_OPERATION_ID" \
  --operator-reason 'documented deposit/withdrawal/account replacement reason'

unset AFUTURE_STRESS90_REBASE_ACK STRESS90_OPERATION_ID
```

对隔离 Shadow 账户执行同一生命周期操作时，必须同时指定其 runtime 和账户能力，不能让命令退回 live 或临时空模拟账户：

```bash
afuture stress90-account-rebase \
  --config /secure/path/afuture.directional-stress90.toml \
  --runtime-dir runtime/shadow \
  --shadow-account \
  --confirm-live \
  --confirm-rebase \
  --operation-id "$(openssl rand -hex 32)" \
  --operator-reason 'documented Shadow account rebase reason'
```

同一账户 rebase 必须由已验证的非零充值/出金差额支持，并以资金流调整 hard daily/HWM 和 completed wealth 基线，不能用零资金流清除回撤或日损状态；更换账户才从新账户 verified settlement 建立新的 soft path。两类 rebase 都写 prepared/completed audit，但 candidate、HHI 和 seed 不重算。成功后仍保持 `HALTED` 和 kill switch，必须重新走 `status`、`doctor`、Shadow/测试柜台所需门。禁止运行中静默重置，也禁止把资金流算成策略收益。每个新操作都必须使用新的 `--operation-id`；只有原事务的精确重试可复用。

## 13. CTP order journal 容量 epoch 封存

order journal 的 20,000 个 order identity / 10,000 个 fill identity 是硬上限，不能删文件或放宽上限。容量接近上限时先保持 kill switch、进入 `HALTED`，确认 Broker/local 空仓、没有活动委托且 fresh reconcile 成功，再执行：

```bash
export AFUTURE_STRESS90_ORDER_EPOCH_ACK=I_CONFIRM_STRESS90_CTP_ORDER_JOURNAL_EPOCH_ROLLOVER
STRESS90_OPERATION_ID="$(openssl rand -hex 32)"

afuture stress90-order-journal-rollover \
  --config /secure/path/afuture.directional-stress90.toml \
  --confirm-live \
  --confirm-rollover \
  --operation-id "$STRESS90_OPERATION_ID" \
  --operator-reason 'capacity rollover after full cold-chain audit'

unset AFUTURE_STRESS90_ORDER_EPOCH_ACK STRESS90_OPERATION_ID
```

命令先全审计 current/archive chain，再把旧账户 epoch 封存为带 checksum、parent root、exact identity set 和 Bloom 摘要的不可变证据；任一复制、校验或清理中断都会留下显式 pending 状态并阻止 runtime，使用同一 operation id 精确重试。成功后 technical permit 失效，状态仍为 `HALTED`，必须重新运行 `status` 和 `doctor`。`status/doctor` 会全量检查所有 sealed cold epochs；不得手工删除 `.prev`、archive、epoch manifest 或 sealed directory。

## 14. 立即停止条件

出现以下任一情况，停止新增风险并按状态矩阵处理：

- current state、seed、policy、OI evidence 或 intent checksum/identity 不可信；
- CTP trading day 非法、倒退或与 account snapshot 不一致；
- target-day gap 或中间日数据不完整；
- OHLC cache、60m OI 或 activity 不覆盖 required completed day；
- expected contract coverage 不完整，尤其潜在更高 OI 合约未观察；
- Broker/local position drift、unknown order/trade 或活动委托未结；
- live metadata、quotes、margin 或 commission 不可信；
- deterministic fee + minimum reasonable slippage 显著超过单边 15bp；
- CTP/vendor flow 存在未解释差异；
- prior-day final funding/settlement witness 不可用或不完整；
- 外部 provider 失败且 verified cache 不足。

registry 初始化只用于没有任何 durable evidence 的空目标路径，并创建 schema 3 registry、lineage
marker 和认证 nonce ledger。已有 registry、lineage、nonce node 或 receipt 完整性检查失败时保持
`HALTED`；保全机器级 registry 目录、完整 nonce ledger、runtime 目录和外部备份，只能使用核验过的
备份与单独批准的恢复流程。禁止删除 marker、重建 registry、换路径或用新 nonce 绕过既有 lineage。

nonce receipt 永不删除或淘汰，
正常 membership 查询最多读取 256 层且不扫描目录。达到 800,000 条必须告警并安排磁盘扩容；达到
1,000,000 硬上限后所有新 registry mutation 失败关闭。任何已锚定 receipt 或 path node 缺失/损坏
都是 durable incident，禁止重建、删 marker 或用新 nonce 绕过。

`account-runtime-registry.json.lineage` 与
`stress90_lifecycle_transaction.json.lineage` 是首次落盘时以 `O_EXCL` 创建并完成文件、
父目录 `fsync` 的不可变 inception 证据。若 lineage marker 存在但 current 与 `.prev`
同时缺失，必须按 durable-state-loss 事故处理：保持 `HALTED`，保全 marker、lock、runtime
目录和外部备份，禁止重新执行 registry initialization、删除 marker、从 `.prev` 自动提升、
或创建另一笔 lifecycle transaction。只有核验过的外部备份和单独批准的恢复流程可以处理该
事故；普通生命周期命令会持续失败关闭。

完全 pristine、没有任何 durable evidence 的 registry 只取得稳定 kernel lock，不创建 visible
`.lock`；因此进程在只读检查中被终止不会制造 false lineage。首次成功 current initialization 才在
仍持有 kernel lock 时物化 visible lock。此后每次接受既有 lineage marker
都重新对 marker 文件和父目录执行 `fsync`；任一步失败，本次读取不得返回可用状态。

`stress90_oi_evidence.json` schema 3 以 `parent_checksum` 把 current 绑定到精确的 sequence N-1
`.prev`，并由稳定 kernel lock、持有至 CAS 结束的 visible `.lock` 和不可变 `.lineage` marker
共同保护。sequence 1 必须没有 parent；sequence N>1 缺少或不匹配 `.prev`、current/`.prev`
损坏、symlink、marker-only、lock-only 或 current 丢失都属于 OI durable-state incident，`.prev`
只能用于诊断，绝不提升为 current。

OI evidence 必须满足 schema 3、精确 predecessor checksum 和 lineage 约束。校验失败时保持
`HALTED`，保全 current、`.prev`、lock、runtime 目录和外部备份；在新的 pristine runtime 路径
重新 bootstrap/commission，并从柜台 raw evidence 观察一个完整、权威的 counter trading day。
在该证据完成前，Stress-90 activation 持续阻断。

`ctp_trading_day_evidence.json` schema 3 明确区分 `unbound` commissioning 与 `bound`
account lineage。unbound 不包含伪造 epoch、registry revision 或 receipt，只能在 policy
account identity/epoch 均未绑定且 machine registry 对该 account/runtime 没有 active binding 时
使用。bound 必须逐字段匹配 policy 的精确 `live_account_epoch` 和 registry 的稳定账户 receipt；
operation nonce 不是 account epoch。任何字段、identity、nonce、日期、policy checksum 或
predecessor evidence 不完整时都保持 `HALTED` 并保全 current/`.prev`；随后重新走完整的
commissioning/recovery。

lifecycle 的 broker-fenced durable 顺序固定为 registry CAS/acknowledgement、精确 trading-day
evidence CAS、generic/policy targets、coordinator committed。prepared command 重试先识别同一
transaction；registry 或 evidence 已完成时只接受同一 nonce、account、epoch、runtime 和 receipt
的 exact retry。任一字段不同均保持 `HALTED` 并失败关闭。

### 13.1 已授权崩溃成交的 HALTED checkpoint

通用 `recover-state` 故意拒绝 Stress-90 position recovery；不得用它绕过 lifecycle
crash-fill 门。只有已经确认是当前 CTP session、且由完整 order/trade query evidence 授权的
崩溃成交，才可在 `HALTED`、kill switch 开启、无 active/unknown order/trade 时运行独立命令：

```bash
export AFUTURE_STRESS90_CRASH_FILL_RECOVERY_ACK=I_CONFIRM_AUTHORIZED_STRESS90_CRASH_FILL_RECOVERY
afuture stress90-crash-fill-recover \
  --config config/afuture.directional-stress90-live.example.toml \
  --confirm-live --confirm-recovery \
  --operation-id <64-hex-unique-nonce> \
  --operator-reason "verified current-session authorized crash fills"
```

命令取得 canonical runtime 的 account-exclusive lease，验证精确 account identity、account
epoch、account-specific registry binding receipt、TradingDayEvidence schema 3 和 policy identity；
存在 prepared `stress90_lifecycle_transaction` 时直接拒绝，不提供 abort/amend。随后它重新查询
Broker，在 critical-ingress fence 内再次验证 query generation/evidence 仍为 current，并按
`registry recovery acknowledgement → TradingDayEvidence roll-forward → recovery prepared →
state.json CAS → recovery committed` 持久化。registry acknowledgement 的 request digest 绑定
pre-ack account receipt、完整 session 语义和 source/target state；checkpoint 则绑定确定性的
post-ack account receipt 与完整 TradingDayEvidence sequence/checksum。checkpoint 保存完整 session
order/trade rows、ownership digest、source/target state checksum 和 position digest。永久 nonce
成员资格由 machine registry schema-3 root 与 recovery checkpoint 自己的认证 compact root 固定，
不会把无界 history 数组重复写入 current。machine-wide registry sequence/checksum 只是审计快照，其他账户推进 registry
不得使本账户 receipt 失效。

同一 nonce 的精确重试只接受完全相同的操作原因、账户 receipt、session order/trade 语义、
fill IDs、持仓和 generic target；请求 id/generation 可以因重新查询而前进。任一语义或 state
变化、CAS 冲突、unknown/active order/trade 都保持 `HALTED` 并失败关闭。命令从不调用
`send_order` 或 `cancel_order`，也不自动清除 kill switch。`stress90_crash_fill_recovery.json`
的 current、`.prev`、`.lineage` 或 `.lock` 存在而 current 丢失/损坏时，按 durable incident
保全现场；`.prev` 只作证据，不能提升。generic state 已保存但 checkpoint 未 committed，或
checkpoint prepared 但 state 尚未保存，都必须用同一 operation id 精确重试收敛，禁止换 nonce。

首次生命周期命令只有在 `state.json` 的 recovery marker 与 committed checkpoint 的 transaction、
target sequence/checksum 完全一致后，才可把 marker 转换为绑定该 lifecycle operation nonce 的
consumed marker；generic CAS 后、lifecycle coordinator commit 前还必须向 recovery store 追加
consumption receipt。若在两者之间崩溃，同一 prepared lifecycle 只能精确补齐 receipt。receipt
完成后，后续合法 lifecycle 可继续推进 generic state，但必须保留并验证 consumed marker；proof
本身不会清除 `HALTED`/kill switch，也不会创建 permit 或订单权限。任何缺失、伪造、重放或
nonce 不一致仍由 `_require_no_unpersisted_lifecycle_crash_fill_adoption` 阻断。

首次 bootstrap 的 durable 顺序固定为 OHLC cache、activity、bootstrap seed、policy state，最后
才写 OI schema 3 sequence 1；OI 是 bootstrap commit point。整个 write bootstrap 从 source
复核、preflight snapshot、先决证据写入、OI save 到 rollback 都持有按 canonical runtime 路径
派生的稳定 OFD kernel lock；`..` 或安全 parent-symlink alias 不能拆分临界区，dry-run 不创建
visible lock 或 lineage。任何写入前必须分别通过 OHLC、
activity、seed、policy 和 OI 自己的 store/API 校验 current 及该 store 真正拥有的 sidecar；孤儿
policy `.prev`/`.lock`、symlink、corrupt current 或 OI sidecar-only 都是须原样保全的 incident。
四个 prerequisite 首写必须走 owner store 的 `O_EXCL` create-only API；各 owner 的普通后续
writer 也共享 canonical artifact-path OFD lock。owner 在构造时就冻结 real-parent canonical
path，policy `.prev`/`.lock` 只从该 canonical current 派生；运行中 retarget parent symlink 不能
造成“锁 target-1、写 target-2”。任何 `O_EXCL` 成功后的 partial write、file/parent `fsync`、
path/inode verify 或 descriptor close 失败，都必须用已捕获 dev/inode 只清理本次创建的精确
文件并 `fsync` parent；identity ambiguous 或 cleanup 失败则保全 incident，并同时报告 primary
和 cleanup diagnostics。

OI 开始前按 canonical path 字典序一次性取得全部 prerequisite OFD locks，并连续持有至 owner
unlocked decoder、exact payload/inode token 逐项复核和最终 OI CAS 完成；这是所有 aggregate
artifact lock 的唯一全局顺序，禁止在持锁区重新取得相同 OFD lock。任一 prerequisite 缺失、
替换或损坏都不得进入 OI phase。首次 OI save 必须显式 `expected_sequence=0`；preflight 后出现
的 ordinary OI seq1 只能导致 bootstrap fail-closed，绝不能被采用或推进为 seq2。rollback 只能
删除 token 仍精确匹配的本次 sequence-1 current，并对删除
执行 parent-directory `fsync`；不得按“preflight 时不存在”推断 ownership，不得推断或删除
store 未拥有的 `.prev`，也不得覆盖调用前或并发证据。单个 cleanup 失败或 token mismatch 须
保留原始 persistence 异常并继续尝试其余安全 cleanup。进入最终 OI save 前即跨过不可逆边界：
此后任何异常（包括 current replace 后或父目录
`fsync` 失败）都必须逐字节保全全部 prerequisites 和 OI current、`.prev`、`.lineage`、`.lock`。
若 current 尚未形成，该 marker/lock-only 状态按 durable incident 保持 `HALTED`；若 current
已替换但 durability ambiguous，同样不得由普通 bootstrap retry 清除或覆盖。

不要删除 kill switch，绝不自动采用 `.prev`，也不要跳过 target day、改用本机日期或在异常路径发送 opening。

## 14. 扩大风险

扩大风险必须在极小真钱跨多个新发生交易日后由人工批准，并复核 actual cost、tracking error、concentration、partial/reject、latency、turnover、margin 和 drawdown。`112.100053%` 不是未来收益承诺，96-template pool 存在已观察历史 selection bias；新发生数据才是真正 forward evidence。

即使以上代码和离线验证全部通过，只要目标机 CTP、Shadow、测试柜台和极小真钱证据未完成，结论仍是：**Stress-90 live wiring 已完成，但真实资金 activation gate 尚未完成。**

## Commissioning scale, overlay reactivation and capacity report

The repository live example now starts at `live_risk_scale=0.05` with one-lot caps and a materially tighter account envelope. This is only an execution-chain commissioning baseline. `live_risk_scale=1.0` retains current production sizing semantics; any smaller value multiplies survivor product weights before integer conversion, so a sufficiently small account/scale may legitimately produce an all-zero target.

After changing scale or any bound risk field, do not start live. Keep `HALTED`, kill switch on, Broker/local flat, no active orders, and run a fresh reconcile. Then use `stress90-activate` with the normal strong activation confirmation and a new 64-hex operation id. The same lifecycle coordinator records a risk-overlay-only reactivation and invalidates the prior technical permit. Current/future execution intent blocks the rebind rather than being reinterpreted.

Run the capacity diagnostic before Doctor permit issuance:

```bash
afuture stress90-capacity-report \
  --config /secure/path/afuture.directional-stress90.toml \
  --confirm-live \
  --output /secure/path/stress90-capacity.json
```

The command sends and cancels zero orders. Exit code `2` means hard safety evidence failed. Review raw/scaled weights and gross, raw/scaled/margin/freeze/final lots, real fee/margin/tick/spread cost, the 15bp gate, margin/cash ratios, clipped products, concentration and tracking error. Portfolio representation warnings are diagnostic only and never expand risk or product scope.

## 部署来源、可验证备份与恢复

生产部署不再把“某个 checkout + 某个 runtime 目录”当成隐含事实。固定历史 bootstrap 完成后，先从**从未绑定实盘账户**的干净 bootstrap runtime 生成确定性 bundle，并在目标机安装、封存部署身份：

```bash
afuture stress90-bundle-create \
  --config /secure/path/afuture.directional-stress90.toml \
  --runtime-dir /secure/path/clean-bootstrap-runtime \
  --output /secure/backup/stress90-bootstrap.tar

afuture stress90-bundle-verify \
  --bundle /secure/backup/stress90-bootstrap.tar

afuture stress90-bundle-install \
  --config /secure/path/afuture.directional-stress90.toml \
  --bundle /secure/backup/stress90-bootstrap.tar \
  --runtime-dir runtime
```

生产 bundle 只接受官方固定输入摘要、固定 candidate、精确历史 parity 和未绑定账户的 seed/policy/OI/OHLC/activity 证据。tar 成员必须是 allowlist 内普通文件；路径穿越、绝对路径、重复成员、软/硬链接、未知成员、超限尺寸、截断、附加垃圾或字节篡改全部失败关闭。测试 fixture 永远不能成为 production-ready bundle。安装目标必须为空，先在同一父目录 staging 并完成语义验证，再原子发布；失败不得留下可被误认为已安装 runtime 的半成品。

在最终目标机、最终 Python 虚拟环境且 `vnpy_ctp` 已安装后封存 deployment identity：

```bash
afuture deployment-seal \
  --config /secure/path/afuture.directional-stress90.toml \
  --bundle /secure/backup/stress90-bootstrap.tar \
  --runtime-dir runtime

afuture deployment-verify \
  --config /secure/path/afuture.directional-stress90.toml \
  --runtime-dir runtime
```

seal 绑定当前 Git HEAD、tracked production source tree digest、生产配置、core/live constraints、Python/OS/CPU/executable、canonical runtime、machine account registry、bundle/seed/policy/products/risk-overlay 以及目标机 `vnpy_ctp` 原生模块身份。生产配置下 `status` 会显示 deployment 诊断；`doctor`、`shadow`、`live` 在 deployment 不匹配时必须先失败，不允许通过已有 permit 绕过。重新 seal 会使既有 technical activation permit 失效，必须重新 Doctor。

需要备份时先主动停到 `HALTED` 且保持 `kill_switch=true`，确认 lifecycle transaction 不处于 `prepared`，然后只备份显式 allowlist 的账户/策略/执行/交易日/订单 journal/registry/nonce-ledger 权威证据：

```bash
afuture backup-runtime \
  --config /secure/path/afuture.directional-stress90.toml \
  --output /secure/backup/stress90-runtime.tar

afuture verify-backup \
  --backup /secure/backup/stress90-runtime.tar
```

备份不会递归打包 runtime，不包含日志、报告、告警、配置或凭证；检测到当前环境中的原始账户/认证秘密出现在待备份字节中也会拒绝。验证会重新 materialize 到临时目录并通过现有 State/Policy/OI/OHLC/Activity/ExecutionIntent/CTP journal/registry/TradingDayEvidence/DeploymentIdentity 等 loader 复核跨文件身份和链路，不能只靠 tar 能解压或单个 checksum 就判定可恢复。

恢复只允许写入**空 runtime** 和明确的 registry staging 路径，并且 source canonical runtime / registry identity 必须与备份一致；此命令不连接 Broker、不会报单或撤单：

```bash
afuture restore-runtime \
  --backup /secure/backup/stress90-runtime.tar \
  --runtime-dir runtime \
  --account-registry-path /var/lib/afuture/account-runtime-registry.json \
  --registry-staging-path /var/lib/afuture/account-runtime-registry.restore-stage
```

恢复成功后仍必须是 `HALTED`、`kill_switch=true`，原 technical activation permit 必须失效。随后严格按 `status` → `deployment-verify` → 无报单 `doctor` → fresh technical permit 的顺序继续；任何 source/config/constraints/native module/runtime/registry/risk overlay 漂移都先保持停机。`.prev` 仍只是诊断前驱证据，bundle 和 backup 也都不能成为自动解除停机或自动恢复交易权限的入口。

## 生产盘前与常驻监督

当前唯一生产操作链：

```bash
afuture deployment-verify --config <config>
afuture prepare-session --config <config> --confirm-live --output <session-preflight.json> [--refresh-ohlc]
# 仅按 preflight 的 allowed_next_actions 处理 operator continuity / account rebase
afuture doctor --config <config> --confirm-live
afuture stress90-capacity-report --config <config> --confirm-live
# 人工复核后按既有机制签发新的 activation permit
afuture live --config <config> --confirm-live
afuture watchdog --config <config> --once --max-age-seconds 15
```

`prepare-session` 一次运行后立即退出，不进入常驻循环；它不会签发 permit、恢复 RUNNING、清除 kill switch、修改 risk scale、自动 roll-forward/rebase，也不会从 live 订单路径同步调用外部 OHLC provider。安全阻断退出码为 2，配置/调用错误使用独立非零码。

systemd live 模板使用 `Restart=on-failure`，但异常/不确定重启围栏使用退出码 75 并列入 `RestartPreventExitStatus`。该围栏在任何 order-capable Broker 构造前运行；旧 permit 被失效，generic state 保持/转为 HALTED 且 kill switch=true。watchdog timer 只读本地证据并告警，不 kill/restart live 进程、不平仓、不解除 HALTED。

## 结算格式与无人值守验收边界

`ctp-settlement-capture` 是隔离测试柜台的私有证据采集入口，使用方法和权限边界见
[`live-trading.md`](live-trading.md#32-隔离测试柜台的结算格式采集)。它只证明指定查询的
已知分片和完成边界，不解析资金、费用和持仓，也不提升 `settlement_verified` 或签发许可。
必须先核验拟运行柜台的真实格式；当前零入出金、PreBalance 差额和结算确认成功都不能
代替上一交易日完整资金活动证据。生产持仓/账户查询现在只在 request-bound 完成后发布。

当前不能把 strict 命令入口、原人工 operator roll-forward 或 systemd 自动重启描述成
无人值守跨日已完成。阶段授权/技术就绪续接、正式日历、正常跨日资金证明和自动恢复
仍须形成同一生产闭环并通过验收后，才能替换日常人工门。没有获批目标机与测试账户时，
不得安装或连接未知柜台；真实一个自然月必须从现场经过时间与执行证据计算，不能用
本地测试、CI、加速回放或“服务进程存活”代替，更不能自动进入实盘。
