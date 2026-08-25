# Stress-90 Live Productionization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将固定 Stress-90 候选接入 Shadow、测试柜台和 CTP runtime，并让离线与线上共享一个确定性核心，同时保持 `execution_aligned` 行为不变。

**Architecture:** 生产纯核心负责 Base→OI→cost→survivor→HHI 的 incremental transition；独立 checksum state 负责 exactly-once；CTP raw observer 在 Tick coalescing 前建立 60m/OI 证据；独立 Stress-90 manager 负责 Broker truth、整数 sizing、双重 freeze 和 reduction-first 执行。研究 evaluator 只作为共享核心的 batch wrapper，runtime 不导入研究入口。

**Tech Stack:** Python 3.10+、dataclasses、pandas/NumPy、原子 JSON envelope、VeighNa/CTP adapter、pytest、Ruff、MyPy、setuptools。

**Spec:** `docs/superpowers/specs/2026-08-25-stress90-live-productionization-design.md`

## Global Constraints

- Base policy 固定为 50 品种、96 模板、meta `11/3/3`、Base/Stress `5bp/15bp`、gross `<=2x`。
- OI 支持品种固定为 `A,C,EG,I,M,P,PP,TA,Y`；不能增删。
- 成本门固定为完成日 `20/3/15bp`，hurdle 必须严格大于。
- HHI 使用 raw survivor product weights；与 strictly-prior median 比较；先比较后追加。
- 软回撤预留固定为 `30%-5%=25%`，只读完成账户日。
- hard risk 不得放宽：gross `2x`、margin `35%`、available `25%`、daily loss `5%`、total drawdown `30%`、35 手、margin buffer `1.25`。
- Broker/CTP 是订单、成交、账户和持仓唯一真相；所有持仓变化只来自 trade callback。
- Stress-90 runtime 禁止导入 `tools/*`、`directional_acceptance` 或同步访问网络。
- `execution_aligned` 保持现有 0.25 target scaling 和既有经济行为。
- 当前文件损坏时不自动使用 `.prev`；missing OI 不得转换成合法 `0`。
- 每个里程碑先跑直接相关测试，再跑对应 L2；完整 L4 只在最终候选稳定后执行一次。

---

### Task 1: Shared Stress-90 Policy Core and Batch Parity

**Files:**
- Create: `afuture/directional_stress90_policy.py`
- Modify: `afuture/directional_60m_oi_confirmation.py`
- Modify: `afuture/directional_60m_oi_reversal_confirmation.py`
- Modify: `afuture/directional_cost_aware_no_trade.py`
- Modify: `afuture/directional_turnover_aware_survivor_reallocation.py`
- Modify: `afuture/directional_concentration_freeze.py`
- Modify: `afuture/directional_drawdown_reserve_freeze.py`
- Modify: `tools/evaluate_directional_stress80_final.py`
- Modify: `tools/evaluate_directional_stress90_final.py`
- Create: `tests/test_directional_stress90_policy.py`
- Create: `tests/test_directional_stress90_incremental_parity.py`
- Modify: existing mechanism tests named above

**Interfaces:**
- Produces: `Stress90PolicyDefinition`, `Stress90CandidateState`, `Stress90Decision`, `STRESS90_POLICY`, `step_stress90_candidate`, `build_stress90_candidate_path`, `candidate_weight_digest`.
- Consumes: `ExecutionAlignedAggressivePolicy`, frozen product/template constants, close/OI completed-day evidence.

- [ ] **Step 1: Write definition identity and immutability tests**

```python
def test_stress90_definition_has_distinct_definition_and_historical_digests():
    assert STRESS90_POLICY.policy_id == "directional.stress90"
    assert STRESS90_POLICY.oi_products == ("A", "C", "EG", "I", "M", "P", "PP", "TA", "Y")
    assert STRESS90_POLICY.historical_candidate_weight_sha256 == EXPECTED_CANDIDATE_SHA
    assert STRESS90_POLICY.policy_definition_digest != EXPECTED_CANDIDATE_SHA
    assert len(STRESS90_POLICY.products) == 50
    assert STRESS90_POLICY.max_gross_leverage == 2.0
```

- [ ] **Step 2: Run the definition test and verify RED**

Run: `.venv/bin/python -m pytest tests/test_directional_stress90_policy.py -q`

Expected: import failure for `directional_stress90_policy`.

- [ ] **Step 3: Implement canonical policy definition and digests**

```python
@dataclass(frozen=True)
class Stress90PolicyDefinition:
    policy_id: str
    definition_version: int
    products: tuple[str, ...]
    oi_products: tuple[str, ...]
    template_ids: tuple[str, ...]
    trend_lookback_sessions: int = 20
    benefit_horizon_sessions: int = 3
    cost_hurdle_bps: float = 15.0
    hard_drawdown_ratio: float = 0.30
    daily_loss_ratio: float = 0.05
    historical_candidate_weight_sha256: str = EXPECTED_CANDIDATE_SHA

    @property
    def drawdown_reserve_ratio(self) -> float:
        return self.hard_drawdown_ratio - self.daily_loss_ratio
```

Canonical payloads must sort keys/products and encode floats with `float.hex()` before SHA-256.

- [ ] **Step 4: Write pure transition behavior tests**

Cover flat→entry, same-sign add, reduction, exit, confirmed reversal, unconfirmed reversal→flat, unsupported product unchanged, missing vs zero OI, 20-session boundary, 3-session benefit, strict 15bp edge, survivor support/sign/gross/tie-break, HHI first/equal/below/above/append order, non-finite/gross/manifest failures.

```python
with pytest.raises(Stress90InputIncomplete, match="missing completed OI flow"):
    step_stress90_candidate(
        prior_state=state,
        target_trading_day="20260825",
        base_weights=weights,
        completed_close_history=close_history,
        completed_close_days=days,
        completed_oi_flow={"A": None},
        completed_oi_day="20260824",
    )
```

- [ ] **Step 5: Implement row-level OI, cost, survivor and HHI primitives**

```python
def apply_oi_confirmation_row(
    *,
    raw_weights: Mapping[str, float],
    prior_applied: Mapping[str, float],
    completed_flow: Mapping[str, int | None],
    supported_products: Collection[str],
) -> dict[str, float]:
    ...

def apply_cost_gate_row(
    *,
    oi_weights: Mapping[str, float],
    prior_approved: Mapping[str, float],
    completed_close_history: Mapping[str, Sequence[float]],
) -> dict[str, float]:
    ...

def reallocate_survivor_row(
    *,
    oi_weights: Mapping[str, float],
    approved_weights: Mapping[str, float],
    prior_survivor: Mapping[str, float],
) -> dict[str, float]:
    ...
```

DataFrame helpers become deterministic loops over these row primitives. Production paths never call `fillna(0)` for required OI inputs.

- [ ] **Step 6: Implement `step_stress90_candidate` and batch wrapper**

`Stress90Decision` must expose base/OI/cost/survivor maps, current HHI, prior median, freeze flag, input days/digests, pre/post-state and daily decision digest. Every map is normalized to the full 50-product order before digesting.

- [ ] **Step 7: Run pure and legacy mechanism tests**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_directional_stress90_policy.py \
  tests/test_directional_60m_oi_confirmation.py \
  tests/test_directional_60m_oi_reversal_confirmation.py \
  tests/test_directional_cost_aware_no_trade.py \
  tests/test_directional_turnover_aware_survivor_reallocation.py \
  tests/test_directional_concentration_freeze.py \
  tests/test_directional_drawdown_reserve_freeze.py
```

Expected: all pass.

- [ ] **Step 8: Add batch/incremental parity tests**

Synthetic fixture must compare every day/product at `abs(diff) <= 1e-14`, compare each intermediate layer, and verify changing a later row cannot alter an earlier decision digest. If the five fixed files exist, also assert `candidate_weight_digest == 8e38...9f28`.

- [ ] **Step 9: Make evaluators delegate shared core**

`tools/evaluate_directional_stress80_final.py::build_final_candidate_weights` calls `build_stress90_candidate_path(..., include_hhi=False)` for the Stress-80 candidate path; Stress-90 adds HHI account response without copying candidate formulas. Historical payload keeps `production_wiring=false`.

- [ ] **Step 10: Run Task 1 L2 and commit checkpoint**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_directional_stress90_policy.py tests/test_directional_stress90_incremental_parity.py tests/test_directional_stress90_final.py tests/test_directional_stress80_final_gate.py
.venv/bin/python -m ruff check afuture/directional_stress90_policy.py tests/test_directional_stress90_policy.py tests/test_directional_stress90_incremental_parity.py
```

Commit: `feat: unify Stress-90 candidate core`

---

### Task 2: Exactly-Once Policy State and Deterministic Bootstrap

**Files:**
- Create: `afuture/directional_stress90_state.py`
- Create: `afuture/directional_stress90_bootstrap.py`
- Modify: `afuture/cli.py`
- Modify: `afuture/directional_engine.py`
- Create: `tests/test_directional_stress90_state.py`
- Create: `tests/test_directional_stress90_bootstrap.py`
- Create: `tests/test_directional_stress90_account_path.py`

**Interfaces:**
- Consumes: Task 1 definitions and transition functions.
- Produces: `Stress90PolicyStateStore`, `Stress90SeedStore`, `prepare_stress90_decision`, `record_completed_account_day`, `rebase_stress90_account`, `bootstrap_stress90`.

- [ ] **Step 1: Write envelope, corruption and identity tests**

```python
store.save(initial_state)
first = store.load()
store.save(replace(first, last_completed_target_day="20260825"))
assert store.previous_path.exists()
store.path.write_bytes(b"corrupt")
with pytest.raises(Stress90StateIntegrityError):
    store.load()
```

Also test schema mismatch, sequence failure, checksum failure, policy digest mismatch, manifest mismatch, seed mismatch and no automatic `.prev` load.

- [ ] **Step 2: Implement strict state/seed schemas**

```python
@dataclass(frozen=True)
class Stress90PolicyState:
    policy_id: str
    policy_definition_digest: str
    products_manifest_digest: str
    bootstrap_source_manifest: dict[str, str]
    bootstrap_through_day: str
    last_completed_target_day: str
    last_completed_input_day: str
    last_decision_digest: str
    last_oi_confirmed_weights: dict[str, float]
    last_cost_approved_weights: dict[str, float]
    last_survivor_weights: dict[str, float]
    completed_concentrations: tuple[float, ...]
    prepared_decision: Stress90PreparedDecision | None
    completed_account_wealth: float
    completed_account_high_watermark: float
    last_completed_account_day: str
    recent_daily_returns_for_adaptive_margin: tuple[float, ...]
    live_inception_day: str
```

Serialization rejects unknown/duplicate keys, NaN/Inf, invalid days, invalid HHI range, extra products and state growth outside explicit limits.

- [ ] **Step 3: Write exactly-once/restart tests**

Call `prepare_stress90_decision` 100 times for one day; assert one sequence advance and one HHI append. Simulate save failure; assert no prepared decision. Reload after decision-before-order crash and assert identical digest/weights.

- [ ] **Step 4: Implement atomic prepared decision transaction**

```python
def prepare_stress90_decision(store, inputs, definition=STRESS90_POLICY):
    current = store.load_required(definition)
    if current.prepared_decision and current.prepared_decision.target_trading_day == inputs.target_day:
        return current.prepared_decision
    decision = step_stress90_candidate(...)
    prepared = Stress90PreparedDecision.from_decision(decision)
    store.save(decision.post_state.with_prepared(prepared))
    return prepared
```

Same-day input digest disagreement raises integrity error instead of recomputing.

- [ ] **Step 5: Write account sufficient-statistics parity tests**

For deterministic and random finite return paths, compare list compounding and `completed_wealth/completed_hwm` after every day. Test exact 25% boundary, current unfinished PnL exclusion, duplicate day, backward day and cash-flow rebase guards.

- [ ] **Step 6: Implement completed-account update and explicit rebase**

`DirectionalTradingEngine._advance_trading_day` calls a manager hook only when the old day is provably complete. Stress manager persists account sufficient state; execution-aligned receives a no-op hook and retains its two-return list behavior.

- [ ] **Step 7: Write bootstrap fixed-input validation tests**

Use synthetic files with injected expected SHA manifest to prove: all five files required, exact SHA required, Base weights rebuilt with production policy, daily Base parity, incremental/batch parity, immutable seed, no account history inheritance, and no overwrite.

- [ ] **Step 8: Implement `stress90-bootstrap`**

```bash
afuture stress90-bootstrap \
  --config config/afuture.directional-stress90-live.example.toml \
  --runtime-dir runtime \
  --through YYYYMMDD
```

Production CLI always uses the documented five SHA values and expected candidate SHA. It writes `stress90_seed.json`, `directional_stress90_state.json`, and bootstrap completed OI evidence only after all gates pass.

- [ ] **Step 9: Run Task 2 L2 and commit checkpoint**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_directional_stress90_state.py tests/test_directional_stress90_bootstrap.py tests/test_directional_stress90_account_path.py tests/test_directional_restart.py tests/test_state_integrity.py tests/test_cli_safety.py
.venv/bin/python -m ruff check afuture/directional_stress90_state.py afuture/directional_stress90_bootstrap.py
```

Commit: `feat: persist Stress-90 decisions exactly once`

---

### Task 3: Raw CTP 60m/OI Evidence and Session Manifest

**Files:**
- Create: `afuture/directional_sessions.py`
- Create: `afuture/directional_stress90_oi_runtime.py`
- Modify: `afuture/broker/base.py`
- Modify: `afuture/broker/ctp.py`
- Modify: `afuture/broker/shadow.py`
- Modify: `afuture/broker/sim.py`
- Modify: `afuture/directional_runtime.py`
- Modify: `afuture/directional_engine.py`
- Create: `tests/test_directional_sessions.py`
- Create: `tests/test_directional_stress90_oi_runtime.py`
- Modify: `tests/test_ctp_compat.py`

**Interfaces:**
- Consumes: Broker `Tick`, `ContractInfo`, authoritative CTP trading day.
- Produces: `PRODUCT_SESSION_MANIFEST`, `RawTickObserver`, `Stress90OiEvidenceAggregator`, `Stress90OiEvidenceStore`, `CompletedOiEvidence`, `opening_window_status`.

- [ ] **Step 1: Write fixed session and entry-deadline tests**

Test night product before/inside/after first night window, day-only product before/inside/after first day window, cross-midnight target-day ownership, weekend/holiday neutrality, and reductions/rolls bypassing entry deadline.

- [ ] **Step 2: Implement immutable session manifest**

```python
@dataclass(frozen=True)
class ProductSessionDefinition:
    product: str
    exchange: str
    sessions: tuple[str, ...]
    first_entry_window: str
    has_night_session: bool
```

Manifest must cover all 50 products exactly once and be included in policy definition digest. No live mutation or optimization API is exposed.

- [ ] **Step 3: Write raw aggregation RED tests**

Cover first/last tick, hourly boundaries, CTP cumulative-volume deltas, reset, duplicate, out-of-order, late tick, night cross-midnight, rollover, restart, no-trade session, missing contract, uncovered higher-OI contract, dominant tie-break and `-1/0/+1/missing` distinction.

- [ ] **Step 4: Implement in-memory raw observer aggregation**

```python
def observe_raw_tick(self, tick: Tick, contract: ContractInfo | None) -> None:
    # validate identity/day/session; update only bounded in-memory state
    ...
```

The callback acquires only the evidence lock, performs no file/network operation, and records a dirty flag. Contract bars preserve first open, last close, first/last hold and total non-negative volume delta.

- [ ] **Step 5: Implement checksummed evidence store and rollover**

Persist completed and in-progress sections with sequence/checksum/`.prev`; rollover atomically completes D and starts D+1. Keep enough completed days for bootstrap gap replay and reject corruption/backward reopen.

- [ ] **Step 6: Insert observer before CTP tick coalescing**

```python
tick.validate()
self._notify_raw_tick_observer(tick, self._contract_catalog.get(tick.symbol))
self._enqueue_tick(tick)
```

`ShadowBroker.set_raw_tick_observer` delegates to its market broker. `SimBroker.publish_tick` calls the observer before its normal event path.

- [ ] **Step 7: Checkpoint only after bounded broker event batch**

Extend the existing manager checkpoint hook to persist both activity and OI dirty state after `super().run_once()` drains one batch. Callback tests monkeypatch all store I/O to raise if called and must still process raw ticks.

- [ ] **Step 8: Add Tick flood and critical FIFO regressions**

Feed thousands of same-symbol ticks; assert manager-delivered ticks are coalesced, aggregator observed all raw ticks, critical event order is unchanged, and callback latency test contains no blocking call.

- [ ] **Step 9: Run Task 3 L2 and commit checkpoint**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_directional_sessions.py tests/test_directional_stress90_oi_runtime.py tests/test_ctp_compat.py tests/test_broker_delivery.py tests/test_directional_activity.py
.venv/bin/python -m ruff check afuture/directional_sessions.py afuture/directional_stress90_oi_runtime.py afuture/broker
```

Commit: `feat: capture authoritative Stress-90 OI evidence`

---

### Task 4: Stress-90 Runtime Wiring, Planner, Fail-Closed Behavior

**Files:**
- Create: `afuture/directional_stress90_runtime.py`
- Create: `afuture/directional_stress90_planner.py`
- Create: `afuture/directional_ohlc_refresh.py`
- Modify: `afuture/directional_risk.py`
- Modify: `afuture/directional.py`
- Modify: `afuture/config.py`
- Modify: `afuture/runtime_factory.py`
- Modify: `afuture/directional_engine.py`
- Modify: `afuture/execution_aligned_runtime.py` only through behavior-neutral hooks
- Modify: `afuture/directional_acceptance.py` to share lot-stage primitives
- Create: `config/afuture.directional-stress90-live.example.toml`
- Create: `tests/test_directional_stress90_planner.py`
- Create: `tests/test_directional_stress90_runtime.py`
- Create: `tests/test_directional_stress90_fail_closed.py`
- Modify: `tests/test_cli_runtime_factory.py`
- Modify: `tests/test_directional_risk_governor.py`

**Interfaces:**
- Consumes: Task 1 decision, Task 2 state, Task 3 OI evidence, verified OHLC cache, Broker truth.
- Produces: `DirectionalRiskResponseMode`, `Stress90DirectionalPortfolioManager`, `Stress90LotStages`, `build_stress90_rebalance_stages`, cache-only runtime provider and explicit OHLC refresh command.

- [ ] **Step 1: Write policy selection and double-scaling regressions**

```python
engine = build_runtime_engine(stress90_config, broker, store)
assert type(engine.directional_manager) is Stress90DirectionalPortfolioManager
assert engine.directional_manager.policy_risk_response_mode is DirectionalRiskResponseMode.FREEZE_NEW_RISK
assert not isinstance(engine.directional_manager.policy, DirectionalRiskScaledPolicy)
```

Also assert `execution_aligned` still receives the wrapper and produces the same 0.25 target under governor trigger.

- [ ] **Step 2: Implement explicit risk response capability and config policy**

```python
class DirectionalRiskResponseMode(str, Enum):
    TARGET_SCALE = "target_scale"
    FREEZE_NEW_RISK = "freeze_new_risk"
```

Live TOML with enabled directional and missing `policy` fails validation. Direct legacy/replay construction may normalize blank policy to `execution_aligned`. Stress-90 requires the frozen 50 products and account exclusivity.

- [ ] **Step 3: Write planner/acceptance parity tests**

For identical equity, completed returns/path, weights, symbols, prices, margin rates and current lots, compare raw integer lots, margin-fitted lots, drawdown-frozen lots, HHI-frozen lots, reductions, openings and action categories.

- [ ] **Step 4: Implement shared lot/freeze/rebalance stages**

```python
@dataclass(frozen=True)
class Stress90LotStages:
    raw_integer_lots: dict[str, int]
    margin_fitted_lots: dict[str, int]
    drawdown_frozen_lots: dict[str, int]
    hhi_frozen_lots: dict[str, int]
    reductions: dict[str, int]
    openings: dict[str, int]
```

Apply reserve freeze first and HHI freeze second. `freeze_new_risk_target` remains product-aware so reversals and same-product rolls pass.

- [ ] **Step 5: Write exactly-once runtime crash tests**

Cover decision save failure→0 orders, saved-before-first-order restart, first-order crash, partial fill restart, same day 100 calls, HHI single append, active-order wait, reductions before openings, fresh Broker reread and hard RiskManager authority.

- [ ] **Step 6: Implement Stress-90 manager**

The manager obtains `broker.get_trading_day()`, validates activity/OHLC/OI alignment, replays every missed completed day, prepares/persists the current decision, then reads Broker account/positions/specs. Reductions are submitted first; openings are recalculated in a later run from fresh Broker truth.

Products unavailable for a target keep incumbent lots and cannot add/roll. Missing data returns `reject` when flat and `risk_off` with risk. Invalid identity, backward day, position drift or unknown broker event remains HALT through the engine.

- [ ] **Step 7: Implement cache-only order path and refresh CLI**

`Stress90DirectionalPortfolioManager` only uses `DirectionalOHLCCacheStore.load`. Add:

```bash
afuture directional-ohlc-refresh --config <stress90.toml>
```

The command invokes the existing provider outside the engine, requires append-only overlap, then atomically replaces the verified cache. Network/provider code is absent from Stress-90 run/callback stack traces.

- [ ] **Step 8: Enforce product first-entry windows**

Prepared state advances even when the opening deadline is missed. The planner removes only entry/same-sign add deltas after the deadline; reductions, exits, reversal reductions and same-product rolls remain executable and audited.

- [ ] **Step 9: Run Task 4 L2 and commit checkpoint**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_directional_stress90_planner.py \
  tests/test_directional_stress90_runtime.py \
  tests/test_directional_stress90_fail_closed.py \
  tests/test_cli_runtime_factory.py \
  tests/test_directional_engine.py \
  tests/test_execution_aligned_runtime.py \
  tests/test_directional_risk_governor.py
.venv/bin/python -m ruff check afuture/directional_stress90_runtime.py afuture/directional_stress90_planner.py afuture/directional_ohlc_refresh.py
```

Commit: `feat: wire Stress-90 into directional runtime`

---

### Task 5: Operations, Doctor, Quality, Rebase and Cost Gate

**Files:**
- Modify: `afuture/operations.py`
- Modify: `afuture/quality.py`
- Modify: `afuture/cli.py`
- Create: `afuture/directional_stress90_costs.py`
- Create: `tests/test_directional_stress90_operations.py`
- Create: `tests/test_directional_stress90_quality.py`
- Create: `tests/test_directional_stress90_rebase.py`
- Modify: `tests/test_operations.py`
- Modify: `tests/test_cli_safety.py`

**Interfaces:**
- Consumes: all persisted state/evidence and read-only Broker snapshots/specs.
- Produces: expanded status/doctor payloads, `stress90_ready`, rebase CLI, raw comparator and quality fields.

- [ ] **Step 1: Write status/doctor RED tests**

Assert status exposes policy/version/digest, bootstrap, target/input days, OHLC/OI digests, layer digests, HHI/prior median/freeze, wealth/HWM/drawdown/reserve, pending decision, lots/gross/tracking error, gaps and blockers. Assert doctor always reports `orders_sent=0` and `stress90_ready=false` on any P0 failure.

- [ ] **Step 2: Implement read-only Stress-90 operational facts**

Status may inspect `.prev` only as warnings; readiness always derives from current files. Doctor validates 50-product OHLC, 9-product OI expected/observed/missing sets, state/seed identity, day continuity, activity/catalog, live specs and activation flat/reconcile gates.

- [ ] **Step 3: Write and implement live 15bp compatibility gate**

```python
def estimate_one_way_cost_bps(spec, tick, *, close_today: bool) -> Stress90CostEstimate:
    # commission + one tick + observed half/full spread, all in bps of contract notional
    ...
```

Doctor reports open, close-yesterday, close-today fee, one tick, bid/ask/depth and minimum reasonable one-way cost. A deterministic cost materially above 15bp blocks activation; it never changes the candidate hurdle.

- [ ] **Step 4: Expand decision/fill quality events**

Persist all Stress-90 intermediate targets, HHI, independent freeze flags, integer/final targets, reduction/opening plans and decision digest. Fill records include expected open, planned price, actual fill, slippage bps, commission, one-way realized cost, partial/reject, completion latency, actual/model turnover and tracking error.

- [ ] **Step 5: Add source comparator without trade capability**

Comparator accepts offline/vendor completed summaries and reports first open, last close, first/last hold, total volume, dominant symbol and final flow differences. It owns no Broker reference and cannot send orders.

- [ ] **Step 6: Write and implement explicit account rebase CLI**

```bash
afuture stress90-rebase \
  --config <stress90.toml> \
  --confirm-live \
  --confirm-rebase RESET_STRESS90_ACCOUNT_PATH \
  --reason "documented operator reason"
```

Require HALTED, Broker/local flat, no active orders, fresh account, reconcile, policy identity and non-empty reason. Write audit and reset only wealth/HWM/live inception; candidate/HHI state is unchanged.

- [ ] **Step 7: Run Task 5 L2 and commit checkpoint**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_directional_stress90_operations.py tests/test_directional_stress90_quality.py tests/test_directional_stress90_rebase.py tests/test_operations.py tests/test_cli_safety.py tests/test_quality_fee_estimate.py
.venv/bin/python -m ruff check afuture/operations.py afuture/quality.py afuture/directional_stress90_costs.py afuture/cli.py
```

Commit: `feat: expose Stress-90 activation evidence`

---

### Task 6: Documentation, L3/L4, Adversarial Review, PR and Merge

**Files:**
- Create: `docs/stress90-live-productionization.md`
- Create: `docs/stress90-live-runbook.md`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/configuration.md`
- Modify: `docs/live-trading.md`
- Modify: `docs/production-checklist.md`
- Modify: `docs/glossary.md`
- Modify: `docs/data-and-backtest.md`
- Modify: `docs/documentation-index.md`
- Modify: `tests/test_documentation_consistency.py`
- Modify: `.github/workflows/ci.yml` only if new deterministic bootstrap/config smoke belongs in normal CI

**Interfaces:**
- Consumes: completed code and verification evidence.
- Produces: operator lifecycle, final validation report, reviewed PR and merged main.

- [ ] **Step 1: Write documentation consistency tests first**

Require the two new documents and enforce that README says Stress-90 is wired but not activated, historical evidence still says `production_wiring=false`, the exact warning “112.100053% 不是未来收益承诺”, selection-bias warning, CTP source-difference warning and six activation stages.

- [ ] **Step 2: Write productionization and runbook documents**

Document exact bootstrap→OHLC refresh→status→doctor→Shadow→comparator→test CTP→tiny-live command sequence, failure matrix, account exclusivity, rebase, state inspection, no `.prev` auto recovery and rollback/incident procedure.

- [ ] **Step 3: Update project documentation consistently**

README and architecture describe the shared core and runtime selection. Configuration documents every new field/path. Live/checklist documents external gates without checking them. Data/backtest distinguishes historical candidate digest from daily decision digest and fixed input limitations.

- [ ] **Step 4: Run L3 subsystem validation**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_directional_*.py \
  tests/test_execution_aligned_*.py \
  tests/test_ctp_compat.py \
  tests/test_state_integrity.py \
  tests/test_cli_safety.py \
  tests/test_operations.py \
  tests/test_position_invariants.py \
  tests/test_documentation_consistency.py
.venv/bin/python tools/test_directional_production_mechanics.py
```

- [ ] **Step 5: Run one final L4 candidate validation**

Run exactly once after the candidate stops changing:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy afuture
.venv/bin/python -m compileall -q afuture
.venv/bin/python -m pip check
.venv/bin/python -m build
.venv/bin/afuture --help
.venv/bin/afuture stress90-bootstrap --help
.venv/bin/afuture stress90-rebase --help
.venv/bin/afuture directional-ohlc-refresh --help
```

Validate all example configs and run replay/shadow-safe smoke with zero external orders.

- [ ] **Step 6: Conditionally run fixed historical matrices**

First verify all five input SHA values. If and only if they match, run seven Stress-90 windows and the Stress-80 regression. Assert candidate SHA and all fixed metrics from the spec. If files are absent/mismatched, record a genuine blocker and do not claim historical reproduction.

- [ ] **Step 7: Perform independent adversarial code review**

Review final diff against the 20-section user spec. Classify findings Critical/Important/Minor. Fix all Critical/Important with targeted regressions, then rerun only affected checks. Re-run full L4 only if a behavior-affecting or shared-infrastructure fix invalidates it.

- [ ] **Step 8: Commit and push final checkpoint**

Commit: `docs: complete Stress-90 productionization evidence`

- [ ] **Step 9: Create PR and wait for CI**

Open a PR to `main`, include architecture, checkpoint commits, exact commands/results, matrix status and external blockers. Required checks are `quality`, `test (3.10)`, `test (3.13)`. Resolve every review thread and rerun only failed jobs when the cause is transient.

- [ ] **Step 10: Configure or report branch protection**

If the available GitHub API supports repository rules, require PRs, disable force push/deletion and require the three checks. Otherwise provide exact manual steps in the final report.

- [ ] **Step 11: Merge and verify main**

Merge only after green CI and no Critical/Important finding. Fetch final `origin/main`, verify clean tree and report final main SHA, PR, commits, tests, matrices, external gates and operator commands. End with:

> Stress-90 live wiring 已完成，但真实资金 activation gate 尚未完成。
