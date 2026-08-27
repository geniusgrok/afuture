# Codex continuation prompt — Stress-90 live productionization

Copy the text below into a new Codex Work session.

---

@Superpowers @GitHub

继续完成 GitHub 仓库 `ychenracing/afuture` 的 Stress-90 live productionization。

这不是重新开始。必须继承现有 Draft PR #29：
<https://github.com/ychenracing/afuture/pull/29>

## 一、权威现场

- 目标主分支：`main`
- 当前已验证 base SHA：`da8de59304963c7b1d6737a63e8dadd6eaecd860`
- 现有 feature branch：`codex/stress90-live-productionization`
- 权威远端 head：`8bb667dd2deb3f31da4b104eeb634dd57fddeb71`
- 权威远端 tree：`b7889126ea3e64182143bce21d745016064d23bc`
- Draft PR：#29
- 完整交接文档：`docs/stress90-live-handoff-20260826.md`

执行前重新读取最新 `origin/main`、PR #29 head、CI 和 review 状态。如果 `main`
前进，以新 `origin/main` 为准，但禁止回退、覆盖、force-push 或丢失现有工作。

注意：旧本地 worktree 可能停在本地 SHA
`8cdbbe7c316c2a9041f55cff271460095829da86`。它与远端 head 的 tree 完全相同，但
commit SHA 因 GitHub Git-data API 重建而不同。禁止 force-push。优先从远端 PR head
创建新的独立 continuation worktree，并把后续提交 fast-forward 推送到现有 PR branch。

必须完整阅读根目录 `AGENTS.md`、PR #29、以下文档及相关代码：

- `docs/stress90-live-handoff-20260826.md`
- `docs/stress90-live-productionization.md`
- `docs/stress90-live-runbook.md`
- `docs/stress90-final-evidence.md`
- `docs/stress90-live-continuation-prompt-20260826.md`

## 二、不可修改的经济行为

不得重新搜索、优化或“改进”候选：

- 基础 Alpha：固定 50 品种、96 模板、meta `11/3/3`、base cost 5bp、stress 15bp。
- OI 支持品种固定为 `A,C,EG,I,M,P,PP,TA,Y`。
- OI dominant tie-break：最后 OI、当日 volume、symbol 稳定排序。
- OI confirmation 的 entry/add/reversal、未确认 reversal 到 0、reduction/exit 无条件通过。
- 成本门固定 `completed lookback=20`、`benefit horizon=3`、单边 `15bp`，只阻止
  entry 和同方向 add。
- Survivor reallocation 不创造 support、不改方向，先最小化 candidate L1 tracking，
  再最小化相对上一 applied target 的 L1 turnover。
- HHI 在 survivor raw product weights 上计算，绝对权重归一化，与 strictly-prior
  expanding median 比较，先比较后追加；`current <= median` 触发，只冻结新风险。
- drawdown reserve 固定 `30% - 5% = 25%`，只使用已完成柜台交易日的复利财富/HWM，
  只冻结新风险。
- Stress-90 不得再套 0.25 target scaling；`execution_aligned` 必须维持原行为。
- 不得放宽 gross 2x、margin 35%、available 25%、daily loss 5%、total DD 30%、
  max lots 35、margin buffer 1.25。
- CTP/Broker 始终是订单、成交、账户和持仓唯一真相。

## 三、先修当前红色 CI，不要先加功能

GitHub Actions run 1749：
<https://github.com/ychenracing/afuture/actions/runs/32970605833>

当前证据：

- Python 3.10：`1310 passed, 24 failed, 1 skipped`。
- Python 3.13：因矩阵失败取消。
- Ruff lint/format：通过。
- MyPy：51 errors in 5 files。

先下载/读取精确 CI 日志并本地复现。按 TDD 修复，每个小批只运行直接相关测试。
禁止通过放宽 production gate、为 FakeBroker 增加 production fallback、跳过测试或全局
关闭 MyPy 来变绿。

24 个失败主要包括：

1. CTP live dependency isolation；
2. Directional daily recovery、freeze-new-risk、OHLC cache API regression；
3. activation/rebase/migration/bootstrap tests 的 FakeBroker 缺 durable journal、完整
   session evidence 或 lifecycle commit fence；
4. bootstrap rollover 缺 authoritative CTP transition；
5. README 未记录四个 CLI：`stress90-oi-collect`、`stress90-prepare-decision`、
   `stress90-registry-init`、`stress90-settlement-roll-forward`；
6. `docs/data-formats.md` 未记录 Tick 字段：`open_price`、`source_trading_day`、
   `source_action_day`、`source_trading_day_verified`；
7. 八个 lifecycle adversarial rebase fixtures 缺 critical-ingress fence。

MyPy 错误集中于：

- `afuture/broker/ctp_session_query.py`
- `afuture/directional_stress90_oi_runtime.py`
- `afuture/stress90_lifecycle_transaction.py`
- `afuture/directional_stress90_bootstrap.py`
- `afuture/cli.py`

## 四、仍需关闭的安全 blocker

按以下顺序处理：

### P0.1 registry/lifecycle

- migration nonce 必须进入 account-runtime registry operation history，防止重放；
- reactivation 必须同步清空 generic `recent_daily_returns`，不能只清 policy soft path；
- registry `current` 和 `.prev` 被删除但 lock 残留时必须失败关闭，禁止重建空 anchor；
- 完成 OI current/prev chain 和 interprocess CAS 审查；
- TradingDayEvidence 必须绑定 registry 的精确 account epoch。

### P0.2 durable crash-fill recovery

当前 lifecycle 会安全阻止未持久化的 authorized crash-fill adoption，但缺少独立、持久、
HALTED 的 recovery checkpoint/CLI。新增入口必须：

- HALTED + kill switch；
- Broker/local 重新对账；
- 绑定完整 session order/trade evidence、account identity/epoch、operation nonce；
- CAS 保存 generic state；
- checksum、sequence、atomic replace、parent fsync、`.prev` 仅证据；
- 不生成新订单；
- crash/retry exactly-once；
- 未知 order/trade 仍 HALT。

### P0.3 settlement/funding closure

生产 Stress-90 manager 已禁止自动跨日推进 account path。不要撤销这个 fail-closed gate。

必须先定义权威的 prior-day final funding/settlement witness。D+1 `PreBalance` 不能证明
D 日最后一次本地 snapshot 后没有充值/出金。证据必须绑定 account、completed day、
request/generation、settlement identity，并能证明完整资金流。如果真实 CTP API/目标 ABI
无法提供该证据：

- 保持 `stress90-settlement-roll-forward` fail-closed；
- 将其明确列为外部 activation blocker；
- 禁止把资金流当策略收益；
- 禁止用 operator 猜测或 D+1 当前日 Deposit/Withdraw=0 代替证据。

只有证据契约成立后，才完整接线 settlement lifecycle。它必须 zero-order、HALTED、
exactly-once、允许 Broker/local 已有持仓严格一致但不得有 active orders，使用
`settlement_snapshot` scope，并在 participant/coordinator/registry commit 全程持有 Broker
critical-ingress fence。

### P0.4 authoritative session continuity

当前非相邻自然日（周末/节假日）保持失败关闭。新增 official immutable CTP/session ledger，
逐日证明中间 target transitions；禁止 `BDay`、本机日期、猜测节假日或仅凭长连接。

## 五、历史 parity blocker

workspace 中缺少五份固定输入：

- `broad_daily_universe.csv`
- `return_target_specific_contracts.csv`
- `execution_aligned_weights.csv`
- `prior_two_year_broad_60m.csv`
- `two_year_broad_60m.csv`

不得伪造、替换或下载其他版本后声称精确复现。缺失时完成代码、合成测试和 CI，但必须将
以下内容标记为真实 blocker：

- candidate SHA
  `8e38dbf6441b561dd1728df08665b94b15cc3358823257505c2fcb9d63f09f28`；
- Stress-90 七窗口矩阵和 `112.100053%`；
- Stress-80 `80.067891%`。

如果固定输入后来可用，先校验 `docs/stress90-final-evidence.md` 中的 digest，再运行 batch
与 incremental 逐日逐产品 parity；不得只比较年化收益。

## 六、验证纪律

- L1：每个红测对应最小修复，只跑直接相关测试。
- L2：按 core/state/OI/runtime/CLI 子系统运行。
- L3：Directional、CTP compatibility、state integrity、CLI safety、restart、position
  invariants、production mechanics。
- L4：仅在最终候选稳定、Critical/Important 全关后运行一次：

```bash
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python -m mypy afuture
python -m compileall -q afuture
python -m pip check
```

同时运行配置校验、replay、shadow-safe smoke、CLI help、wheel build。若 L4 后只有文档、
注释、格式或元数据变化，不重复 L4。

每个有实际意义的 milestone 提交并 push 到现有 PR branch；不要制造大量微型 commits。
每 30–60 分钟向用户报告一次具体进度：commit、通过测试数或准确 blocker，不要长时间
无反馈。

## 七、完成和合并门

- 修复全部 CI 和 MyPy；
- 完成独立 adversarial review，Critical/Important 为零；
- full L4 和 required checks `quality`、`test (3.10)`、`test (3.13)` 通过；
- GitHub branch protection 配置为禁止 force push/删除 main、必须 PR 和 required checks；
- 固定输入存在时完成 Stress-90/Stress-80 matrix；不存在则不能宣布历史 parity完成；
- PR #29 转 ready 后等待正常 CI，不得直接绕过；
- 未取得 CTP ABI、Shadow、测试柜台、真实成本/保证金、FAK、重连和极小真钱证据前，
  不能声称真实资金可用。

最终报告必须给出：main SHA、PR、关键 commits、架构、candidate parity、完整命令/结果、
矩阵、外部现场门和目标机从 bootstrap 到 tiny-live 的命令顺序。

若所有代码完成但现场证据仍缺失，最终结论必须写：

**“Stress-90 live wiring 已完成，但真实资金 activation gate 尚未完成。”**

如果代码仍未完成，则必须明确写：

**“Stress-90 live wiring 尚未完成，真实资金 activation gate 尚未完成。”**

---
