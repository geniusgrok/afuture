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
- Base 与 archived `execution_aligned_weights.csv` 逐日一致；
- batch/incremental 最大差在实现规定的机器精度范围内；
- `stress90_bootstrap_seed.json` 与 `stress90_policy_state.json` 成功创建。

若任一文件不存在、SHA 不匹配或 parity 失败，立即停止。禁止下载另一版本数据、修改摘要或生成伪 seed。seed 只继承候选 state 和完整 prior HHI，不继承历史回测账户收益。

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
account revision、last operation 和 receipt digest 固定的证据。registry schema 2 还为每次
bind/advance/switch/acknowledgement/transfer 持久化 operation kind 和精确参数 receipt；nonce
全 machine、全账户、全 operation kind 唯一，只有 kind 与全部参数都相同的重试可以复用。

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
- 开仓、平昨、平今、1 tick、spread/depth 的 15bp 兼容性；
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
  --current-trading-day YYYYMMDD

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

## 11. 充值、出金或更换账户

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

## 12. CTP order journal 容量 epoch 封存

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

## 13. 立即停止条件

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
- 外部 provider 失败且 verified cache 不足。

`account-runtime-registry.json.lineage` 与
`stress90_lifecycle_transaction.json.lineage` 是首次落盘时以 `O_EXCL` 创建并完成文件、
父目录 `fsync` 的不可变 inception 证据。若 lineage marker 存在但 current 与 `.prev`
同时缺失，必须按 durable-state-loss 事故处理：保持 `HALTED`，保全 marker、lock、runtime
目录和外部备份，禁止重新执行 registry initialization、删除 marker、从 `.prev` 自动提升、
或创建另一笔 lifecycle transaction。只有核验过的外部备份和单独批准的恢复流程可以处理该
事故；普通生命周期命令会持续失败关闭。

从 marker 引入前版本升级时，必须保留原 current、`.prev` 和 registry `.lock`。首次持锁
读取会先验证完整 envelope/checksum 及可用的 current/`.prev` 链，再一次性创建 marker；
该步骤不推断账户 authority，也不改变交易状态或经济参数。旧 current 缺失、链损坏，或只剩
旧 registry `.lock` 时绝不升级并持续失败关闭。marker 文件或父目录 `fsync` 报错的本次调用
一律失败；若 marker 已完整可读，重试仍会先重新验证 current，若 marker 为部分/异常内容则按
事故处理，禁止手工补写或删除。

完全 pristine、没有任何 durable evidence 的 registry 只取得稳定 kernel lock，不创建 visible
`.lock`；因此进程在只读检查中被终止不会制造 false lineage。首次成功 initialization 或已验证
legacy upgrade 才在仍持有 kernel lock 时物化 visible lock。此后每次接受既有 lineage marker
都重新对 marker 文件和父目录执行 `fsync`；任一步失败，本次读取不得返回可用状态。

`stress90_oi_evidence.json` schema 3 以 `parent_checksum` 把 current 绑定到精确的 sequence N-1
`.prev`，并由稳定 kernel lock、持有至 CAS 结束的 visible `.lock` 和不可变 `.lineage` marker
共同保护。sequence 1 必须没有 parent；sequence N>1 缺少或不匹配 `.prev`、current/`.prev`
损坏、symlink、marker-only、lock-only 或 current 丢失都属于 OI durable-state incident，`.prev`
只能用于诊断，绝不提升为 current。

schema 2 没有可验证的 predecessor checksum，不能原地升级、补写 parent、重置 sequence 或从
`.prev` 推断。部署发现 schema 2 时必须保持 `HALTED`，保全 current、`.prev`、lock、runtime
目录和外部备份；当前代码故意不提供自动恢复。只有单独审批的部署/恢复流程可以配置真正
pristine 的新 evidence 路径，并从柜台 raw evidence 重新观察一个完整、权威的 counter trading
day 后建立 schema 3 sequence 1。在该证据完成前，Stress-90 activation 持续阻断。

`ctp_trading_day_evidence.json` schema 3 明确区分 `unbound` commissioning 与 `bound`
account lineage。unbound 不包含伪造 epoch、registry revision 或 receipt，只能在 policy
account identity/epoch 均未绑定且 machine registry 对该 account/runtime 没有 active binding 时
使用。bound 必须逐字段匹配 policy 的精确 `live_account_epoch` 和 registry 的稳定账户 receipt；
operation nonce 不是 account epoch。schema 1/2 或任何不完整的中间 schema 均不得从 account
identity、nonce、日期、policy checksum 或 `.prev` 推断升级。部署发现旧 evidence 时必须保持
`HALTED` 并保全 current/`.prev`；只有现存的精确 lifecycle transaction 加上其精确 registry
binding 能确定性地产生 schema 3，否则这是部署阻断，必须重新走批准的 commissioning/recovery。

lifecycle 的 broker-fenced durable 顺序固定为 registry CAS/acknowledgement、精确 trading-day
evidence CAS、generic/policy targets、coordinator committed。prepared command 重试先识别同一
transaction；registry 或 evidence 已完成时只接受同一 nonce、account、epoch、runtime 和 receipt
的 exact retry。任一字段不同均保持 `HALTED` 并失败关闭。

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
