# Directional Production Reality Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the gap between the frozen execution-aligned directional historical evidence and the production runtime without expanding Alpha search or leverage.

**Architecture:** Keep one production account/state owner (`TradingEngine` + Broker), add a small persisted previous-trading-day contract-activity evidence store, make the directional execution lifecycle reduction-first and signal-day-aware, extend the existing JSONL quality recorder for directional fills/cycles, and add a production-mechanics proxy acceptance that uses integer lots/multipliers/account gates. The final policy remains the existing 96-template `ExecutionAlignedAggressivePolicy` capped at 2.0x gross.

**Tech Stack:** Python 3.10/3.13, pandas/numpy, pytest, GitHub Actions, AKShare/Sina research data, existing afuture Broker/RiskManager/StateStore.

**Spec:** `docs/archive/development/specs/2026-08-22-directional-production-reality-design.md`

## Global Constraints

- Do not add Alpha families, templates, ML models, or leverage above 2.0x gross.
- Do not create a second account/order/fill/position state machine.
- Calendar Spread / Auto economic behavior must remain unchanged.
- Historical broker margin metadata is unavailable: production-mechanics reports must label margin as a proxy assumption.
- Follow `AGENTS.md`: L1/L2 during edits, L3 at milestones, one final expensive L4 only after behavior stabilizes.
- Every behavior change uses RED → GREEN TDD.

---

### Task 1: Previous-day activity evidence and trading-day-aware signal freshness

**Files:**
- Create: `afuture/directional_activity.py`
- Modify: `afuture/directional.py`
- Modify: `afuture/execution_aligned_runtime.py`
- Modify: `afuture/runtime_factory.py`
- Modify: `afuture/config.py`
- Modify: `config/afuture.directional-live.example.toml`
- Test: `tests/test_directional_activity.py`
- Test: `tests/test_execution_aligned_runtime.py`

**Interfaces:**
- Produce `ContractActivity(symbol, exchange, product, trading_day, volume, open_interest, timestamp)`.
- Produce `DirectionalActivitySnapshot(trading_day, contracts)` and `DirectionalActivityStore(path).load()/save()`.
- `DirectionalContractSelector.select_from_activity(catalog, snapshot, planned_date)` replaces live current-day activity for production opens.
- Execution-aligned signal validation receives `required_signal_day` from the completed activity snapshot.

- [ ] Write failing tests proving that current-day volume/OI cannot change the contract selected from the completed previous-day snapshot.
- [ ] Write failing tests proving activity freezes only when `Tick.trading_day` advances and survives store reload.
- [ ] Write failing tests proving a Monday/holiday gap is accepted when signal date equals the completed snapshot day, while a missing normal completed trading-day bar is rejected even if `signal_max_age_hours` has not expired.
- [ ] Run only `pytest -q tests/test_directional_activity.py tests/test_execution_aligned_runtime.py` and verify RED.
- [ ] Implement atomic activity JSON persistence and previous-day selector.
- [ ] Wire the store path through AppConfig/runtime factory without touching account state ownership.
- [ ] Re-run the two targeted files and verify GREEN.
- [ ] Commit `feat: align directional activity and signal trading days`.

### Task 2: Reduction-first rebalance and directional risk-off behavior

**Files:**
- Modify: `afuture/directional_runtime.py`
- Modify: `afuture/directional_engine.py`
- Modify: `afuture/engine.py` only through a narrow account-risk hook if required
- Test: `tests/test_directional_manager.py`
- Test: `tests/test_directional_engine.py`
- Test: `tests/test_directional_restart.py`

**Interfaces:**
- `DirectionalActionResult.action` additionally supports `risk_off` for required-signal failure with live risk.
- Add a helper that derives temporary target lots for unavailable nonzero products from their existing positions, so unavailable new risk is frozen rather than globally rejected.
- Existing target=0/flip/excess/roll reductions are always actionable before openings.

- [ ] Write a failing manager test: target product X is unavailable, but an unrelated existing product whose target is zero must still emit reduction FAK orders.
- [ ] Write a failing test: unavailable target X with an existing X position preserves current X lots while still reducing unrelated risk.
- [ ] Write a failing engine test: missing required signal with directional risk enters REDUCE_ONLY; the same failure while flat rejects new risk without creating unnecessary account state.
- [ ] Add restart test: a persisted directional position that exactly matches Broker positions reconciles successfully; mismatch remains fail-closed.
- [ ] Run only the three directional test files and verify RED.
- [ ] Refactor `maybe_rebalance()` to read positions before new-risk availability checks and execute deterministic reductions first.
- [ ] Add the minimal `risk_off` handoff to `DirectionalTradingEngine`; do not add a new risk state enum.
- [ ] If account-risk rejection currently jumps directly to HALTED while directional risk exists, add a protected hook in `TradingEngine` whose default remains `emergency_stop`, with directional override to `enter_reduce_only`; cover it in tests so Calendar Spread semantics stay unchanged.
- [ ] Re-run targeted directional tests and the existing engine risk tests; verify GREEN.
- [ ] Commit `fix: make directional risk reduction fail closed`.

### Task 3: Directional execution-quality lifecycle

**Files:**
- Modify: `afuture/quality.py`
- Modify: `afuture/directional_runtime.py`
- Modify: `afuture/directional_engine.py`
- Modify: `afuture/engine.py`
- Modify: `afuture/cli.py`
- Test: `tests/test_quality.py`
- Test: `tests/test_directional_quality.py`

**Interfaces:**
- `record_directional_rebalance(...)`
- `record_directional_fill(...)`
- `record_directional_cycle(...)`
- `ExecutionQualityRecorder.summary()` keeps every existing pair key and adds a nested `directional` summary.
- Directional order metadata is keyed by order id/cycle id but fills still come only from Broker trade callbacks.

- [ ] Write failing recorder tests for directional rebalance/fill/cycle JSONL and summary fields: cycles, fill_count, turnover_notional, commission_total, median/p95_slippage_bps, median_tracking_error, partial/rejected counts.
- [ ] Write failing runtime/engine test showing a directional FAK order registers expected price/notional and a Broker `Trade` records realized fill/slippage without mutating position truth outside `_apply_expected_trade()`.
- [ ] Run targeted quality tests and verify RED.
- [ ] Add directional JSONL methods and compact aggregation to the existing recorder.
- [ ] Wire cycle metadata from manager order submission through engine order/trade callbacks; use `directional:` references only, leaving pair quality unchanged.
- [ ] Update `quality-report` to serialize the nested directional summary while retaining old top-level compatibility fields.
- [ ] Re-run targeted quality tests and verify GREEN.
- [ ] Commit `feat: close directional execution quality evidence`.

### Task 4: Production-mechanics proxy acceptance

**Files:**
- Create: `afuture/directional_acceptance.py`
- Create: `tools/evaluate_directional_production_mechanics.py`
- Create: `tools/test_directional_production_mechanics.py`
- Modify: `.github/workflows/research-return-target-specific.yml`
- Test: `tests/test_directional_acceptance.py`

**Interfaces:**
- Frozen product multipliers:
  `A/B/C/CS/M/P/Y/OI/RM/SR/TA/MA=10`, `AG=15`, `AL/CU/PB/ZN=5`, `AU=1000`, `AP=10`, `BC=5`, `BU/FU/HC/RB/RU/SP=10`, `CF=5`, `CJ=5`, `EB=5`, `EG=10`, `FG=20`, `I/J=100`, `JM=60`, `L/PP/V/PF/PK/SF/SM/SS=5`, `LH=16`, `LU/NR=10`, `NI/SN=1`, `PG=20`, `SA/UR=20`.
- Explicit base margin-rate proxy: 0.12 before existing `margin_estimate_buffer`; stress proxy: 0.15.
- Initial capital follows the directional example: 500000.
- Use the exact frozen execution-aligned product weights; do not search parameters.

- [ ] Write RED unit tests for integer lot floor, multiplier notional, roll old-contract close/new-contract open, margin rejection, current `max_contract_volume`, daily/high-watermark risk behavior, and previous-day contract selection.
- [ ] Implement `DirectionalProductionAcceptance` as a pure deterministic simulator over concrete daily OHLC + completed activity selection.
- [ ] Produce base 5bp / stress 15bp results plus a `production_gap` section comparing float-notional L4 vs integer/account proxy metrics.
- [ ] Run local tool tests only.
- [ ] Extend the existing expensive specific-contract workflow so the same fetched 50-product/3000-contract dataset runs this proxy after the established L4; no duplicate network fetch.
- [ ] Run one final L3/L4 milestone workflow only after Tasks 1–4 behavior is stable.
- [ ] Do not change production risk thresholds merely because the proxy misses 100%; if a gate is inconsistent, report exact first divergence and only change policy with explicit safety/economic rationale.
- [ ] Commit `feat: add directional production mechanics acceptance`.

### Task 5: Remove obsolete intermediate directional code, synchronize docs, and final CI

**Files:**
- Modify: `afuture/directional.py`
- Modify: `afuture/directional_runtime.py`
- Modify: `afuture/execution_aligned_policy.py`
- Delete research tools that are superseded and have no references after dependency scan
- Modify: `.github/workflows/ci.yml`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/data-and-backtest.md`
- Modify: `docs/live-trading.md`
- Modify: `docs/production-checklist.md`
- Modify: `docs/archive/evidence/return-target-100-evidence.md`

**Interfaces:**
- `ExecutionAlignedAggressivePolicy` is the only production directional policy.
- `directional.py` contains only configuration/selection/lot/rebalance primitives.
- `directional_runtime.py` contains only generic execution lifecycle; no close-only production provider/default legacy policy.

- [ ] Dependency-scan old `FrozenAggressivePolicy`, `_FROZEN_TEMPLATE_IDS`, `SinaContinuousSignalProvider`, superseded return-target research scripts and remove only symbols/files with no required final-evidence callers.
- [ ] Move any final policy primitives required by `execution_aligned_policy.py` into that module so it is self-contained.
- [ ] Add a fast CI directional smoke: validate directional live example with dummy CTP env, run deterministic OHLC/activity → reduction → opening → trade → restart reconcile → risk_off/REDUCE_ONLY test entrypoint, and assert `quality-report` contains directional summary.
- [ ] Correct README/evidence metrics to the official artifact values: base annualized 107.4623%, total 306.1855%, max DD 27.4097%, Sharpe 1.6874; stress annualized 58.1372%, total 141.1415%, max DD 32.9554%, Sharpe 1.1525; extreme annualized 5.09% and max DD 43.51% from the final artifact.
- [ ] Document the production-mechanics proxy result separately from the historical float-notional 107.46% result.
- [ ] Run affected L2 directional + quality + engine suites.
- [ ] Run the final complete repository CI once on the stable candidate: Python 3.10/3.13 `pytest`, compileall, config validations, replay/Auto gates, directional smoke and quality report.
- [ ] Request/perform two review passes over causality, risk permissions, order/fill truth, restart, research-vs-production equivalence, and docs. Fix Critical/Important findings and re-run only affected validation unless behavior changes require the full gate.
- [ ] Squash merge to `main`, then verify merged tree equals the validated PR head tree.
