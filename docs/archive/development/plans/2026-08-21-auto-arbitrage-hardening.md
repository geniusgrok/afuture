# Auto Arbitrage Hardening Implementation Plan

> **归档说明：** 本文是已完成阶段的实施记录，不描述当前系统；当前事实见 [`documentation-index.md`](../../../documentation-index.md)。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing personal CTP calendar-spread auto trader select and execute opportunities more consistently while reducing false spreads and legging tail risk.

**Architecture:** Keep the existing `AutoPairManager -> CalendarSpreadStrategy -> RiskManager -> PairExecutor -> Broker` chain. Fix correctness inside those units instead of adding new services or a second strategy stack.

**Tech Stack:** Python 3.10+, pytest, GitHub Actions, VeighNa/vnpy CTP adapter.

**Spec:** `docs/archive/development/specs/2026-08-21-auto-arbitrage-hardening-design.md`

## Global Constraints

- Keep one production strategy: same-product calendar-spread mean reversion.
- Keep one production risk/execution path.
- No ML selector, service platform, database, distributed scheduler or auto-expansion to all products.
- New behavior must fail closed when quote synchronization is uncertain.
- Existing replay, auto-replay and walk-forward commands must remain green.

---

### Task 1: Correct auto sampling and protected-pair eligibility

**Files:**
- Modify: `tests/test_auto_selection.py`
- Modify: `afuture/auto.py`

**Interfaces:**
- Consumes: `AutoPairManager.observe()`, `AutoPairManager.select()`.
- Produces: sampled per-symbol history and truthful `last_eligible_ids` for protected positions.

- [ ] **Step 1: Add regression tests before production changes**

Add tests equivalent to:

```python
def test_dense_live_ticks_preserve_statistical_window():
    manager = AutoPairManager(auto_config(sample_seconds=60, lookback=3))
    # Feed many raw updates in each of four one-minute buckets.
    # Selection must still have four usable samples and find the final dislocation.
    assert manager.select(...) != []


def test_protected_pair_remains_eligible_when_hard_gates_pass():
    selected = manager.select(..., protected_pair_ids={pair.pair_id})
    assert selected[0].pair_id == pair.pair_id
    assert pair.pair_id in manager.last_eligible_ids
```

- [ ] **Step 2: Run branch CI and confirm the new tests fail for the intended reasons**

Expected failures: dense raw ticks evict the intended minute window; protected pair is missing from `last_eligible_ids`.

- [ ] **Step 3: Implement bucketed observation retention and score protected pairs**

`observe()` must replace the last sample inside the same `sample_seconds` bucket and append only when the bucket changes. `select()` must evaluate protected pairs through the same hard gates while `rank_candidates()` still gives them first priority.

- [ ] **Step 4: Run targeted/full CI until green**

### Task 2: Align scanner and strategy with executable prices

**Files:**
- Modify: `tests/test_hardening.py`
- Modify: `afuture/scanner.py`
- Modify: `afuture/auto.py`
- Modify: `afuture/strategy.py`

**Interfaces:**
- `SpreadStatistics` adds `reference_std`.
- `SpreadScanner` uses direction-specific executable entry Z-scores.
- `CalendarSpreadStrategy` uses executable liquidation spread for normal exit and directional stop.

- [ ] **Step 1: Add failing tests**

```python
def test_scanner_rejects_mid_only_dislocation_that_is_not_executable():
    candidate = SpreadScanner().scan_pair(pair, ticks_with_wide_latest_book, specs())
    assert candidate is None


def test_long_spread_waits_until_liquidation_price_reverts():
    strategy.set_position(1)
    assert strategy.on_quotes(wide_near, wide_far).action is SignalAction.HOLD
    assert strategy.on_quotes(tight_near, tight_far).action is SignalAction.EXIT
```

Also add a stop regression where the executable liquidation spread breaches the anchored stop even though the mid spread has not.

- [ ] **Step 2: Run CI and verify RED**

- [ ] **Step 3: Implement executable entry/liquidation logic**

Use `executable_spreads()` for the latest direction. Scanner metadata queries remain behind the cheap statistical/executable threshold. Structural-break statistics remain mid-spread based.

- [ ] **Step 4: Run full CI**

### Task 3: Add cross-leg skew protection and liquidity-aware pair submission

**Files:**
- Modify: `tests/test_hardening.py`
- Modify: `afuture/risk.py`
- Modify: `afuture/execution.py`
- Modify: `config/afuture.live.example.toml`

**Interfaces:**
- `RiskConfig.max_leg_skew_seconds: float = 2.0`.
- `RiskManager.check_quotes()` rejects multi-leg quotes beyond that skew.
- `PairExecutor.execute_signal()` submits the thinner executable leg before the deeper hedge leg.

- [ ] **Step 1: Add failing risk and execution-order tests**

```python
def test_quote_gate_rejects_cross_leg_timestamp_skew():
    decision = RiskManager(RiskConfig(max_leg_skew_seconds=2)).check_quotes([near, far], now)
    assert not decision.allowed


def test_pair_executor_submits_thinner_leg_first():
    result = executor.execute_signal(...)
    assert result.accepted
    assert broker.sent_symbols[:2] == [thin_symbol, deep_symbol]
```

- [ ] **Step 2: Run CI and verify RED**

- [ ] **Step 3: Implement risk field, validation and request sorting**

Depth is side-specific: BUY uses `ask_volume`; SELL uses `bid_volume`. Sort ascending before sending. Do not alter reduce-only imbalance repair.

- [ ] **Step 4: Run full CI**

### Task 4: Documentation, final verification and publish

**Files:**
- Modify: `README.md`
- Modify: `config/afuture.live.example.toml`

- [ ] **Step 1: Document the corrected production semantics**

README must state sampled auto history, executable candidate/exit prices, cross-leg skew gate and thinner-leg-first execution. Do not claim guaranteed profit or drawdown.

- [ ] **Step 2: Run fresh GitHub Actions on Python 3.10 and 3.13**

Required jobs include `pytest`, `compileall`, static replay, auto replay, scanner and walk-forward acceptance command.

- [ ] **Step 3: Merge the verified branch into `main`**

Use the PR head SHA as the expected merge SHA. After merge, verify `main` points to the merge result and check its combined status/workflow evidence.
