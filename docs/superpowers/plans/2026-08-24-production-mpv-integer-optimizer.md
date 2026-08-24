# Production MPV and Integer Optimizer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and evaluate a research-only Production-aligned marginal-value framework and discrete lot optimizer without changing validated Production behavior unless the candidate passes all economic and risk gates.

**Architecture:** Keep decision-side MPV arithmetic, discrete optimization, and ex-post attribution in separate modules. The new optimizer consumes concrete lot intent and monetary MPV objective values but owns neither Alpha generation nor risk authority. A research acceptance adapter is added only after pure-unit behavior is proven, and live runtime remains untouched unless final promotion evidence passes.

**Tech Stack:** Python 3.10+, dataclasses, pandas, existing afuture directional acceptance/event ledger, pytest, GitHub Actions/L4 evidence tooling.

**Spec:** `docs/superpowers/specs/2026-08-24-production-mpv-integer-optimizer-design.md`

## Global Constraints

- Start from `d8a4158cadbea36ff7aa9e76dd7d562b218708c3` or newer `main`; never roll back newer main work.
- target gross <= 2x and realized gross <= 2x.
- hard margin <= 35%, available >= 25%, and the existing normal soft-margin envelope may not be expanded.
- daily loss = 5%, total drawdown = 30%, max absolute contract lots = 35.
- reduction-first and Broker / CTP truth semantics remain unchanged.
- RiskManager circuit / HALT authority remains unchanged.
- Do not tune PR #18 Candidate A/B parameters or the rejected tracking-error allocator.
- No future information may enter decision features; ex-post future outcomes are labels only.
- No large parameter search; preserve negative evidence when a candidate fails.
- Small changes use targeted tests; subsystem tests run at milestones; full CI/L4 runs once on the final candidate unless a material post-run change requires rerun.

---

### Task 1: Decision-side MPV domain model

**Files:**
- Create: `afuture/directional_mpv.py`
- Create: `tests/test_directional_mpv.py`

**Interfaces:**
- Produces: `MarginalLotAction`, `MarginalProductionValue`, `score_marginal_production_value(...)`.
- `MarginalLotAction` contains `decision_date`, `evidence_through`, `product`, `symbol`, `delta_lots`, `current_lots`, `requested_lots`, `lot_notional`, `per_lot_margin`, `equity`, `margin_utilization`, `available_ratio`, `governor_scale`, `roll_required`.
- `MarginalProductionValue` contains the eight monetary components plus `total_value`.

- [ ] **Step 1: Write failing arithmetic test**

```python
from datetime import date

from afuture.directional_mpv import (
    MarginalLotAction,
    score_marginal_production_value,
)


def test_mpv_total_is_expected_gross_alpha_less_all_production_drags():
    action = MarginalLotAction(
        decision_date=date(2026, 8, 20),
        evidence_through=date(2026, 8, 19),
        product="AG",
        symbol="AG2612",
        delta_lots=1,
        current_lots=2,
        requested_lots=4,
        lot_notional=120000.0,
        per_lot_margin=18000.0,
        equity=500000.0,
        margin_utilization=0.18,
        available_ratio=0.82,
        governor_scale=1.0,
        roll_required=False,
    )
    value = score_marginal_production_value(
        action,
        expected_incremental_gross_alpha=900.0,
        expected_incremental_transaction_cost=180.0,
        margin_opportunity_cost=40.0,
        turnover_penalty=30.0,
        downside_risk_penalty=100.0,
        correlation_penalty=50.0,
        concentration_penalty=25.0,
        roll_execution_penalty=0.0,
    )
    assert value.total_value == 475.0
```

- [ ] **Step 2: Run the single test and verify RED**

Run: `pytest tests/test_directional_mpv.py::test_mpv_total_is_expected_gross_alpha_less_all_production_drags -q`
Expected: FAIL because `afuture.directional_mpv` does not exist.

- [ ] **Step 3: Implement minimal dataclasses and arithmetic**

```python
@dataclass(frozen=True)
class MarginalProductionValue:
    expected_incremental_gross_alpha: float
    expected_incremental_transaction_cost: float
    margin_opportunity_cost: float
    turnover_penalty: float
    downside_risk_penalty: float
    correlation_penalty: float
    concentration_penalty: float
    roll_execution_penalty: float

    @property
    def total_value(self) -> float:
        return (
            self.expected_incremental_gross_alpha
            - self.expected_incremental_transaction_cost
            - self.margin_opportunity_cost
            - self.turnover_penalty
            - self.downside_risk_penalty
            - self.correlation_penalty
            - self.concentration_penalty
            - self.roll_execution_penalty
        )
```

Implement `MarginalLotAction` and `score_marginal_production_value` only to the extent required by the test.

- [ ] **Step 4: Run the single test and verify GREEN**

Run: `pytest tests/test_directional_mpv.py::test_mpv_total_is_expected_gross_alpha_less_all_production_drags -q`
Expected: PASS.

- [ ] **Step 5: Add failing causality/sign/finite validation tests**

```python
import math
import pytest


def test_mpv_rejects_future_dated_evidence():
    with pytest.raises(ValueError, match="evidence_through"):
        MarginalLotAction(
            decision_date=date(2026, 8, 20),
            evidence_through=date(2026, 8, 20),
            product="AG", symbol="AG2612", delta_lots=1,
            current_lots=0, requested_lots=1,
            lot_notional=120000.0, per_lot_margin=18000.0,
            equity=500000.0, margin_utilization=0.0,
            available_ratio=1.0, governor_scale=1.0,
            roll_required=False,
        )


def test_mpv_rejects_action_that_exceeds_raw_requested_direction():
    with pytest.raises(ValueError, match="requested"):
        MarginalLotAction(
            decision_date=date(2026, 8, 20),
            evidence_through=date(2026, 8, 19),
            product="CU", symbol="CU2610", delta_lots=1,
            current_lots=0, requested_lots=-2,
            lot_notional=390000.0, per_lot_margin=60000.0,
            equity=500000.0, margin_utilization=0.0,
            available_ratio=1.0, governor_scale=1.0,
            roll_required=False,
        )


def test_mpv_rejects_non_finite_or_negative_drag():
    action = valid_action()
    with pytest.raises(ValueError):
        score_marginal_production_value(
            action,
            expected_incremental_gross_alpha=math.nan,
            expected_incremental_transaction_cost=0.0,
            margin_opportunity_cost=0.0,
            turnover_penalty=0.0,
            downside_risk_penalty=0.0,
            correlation_penalty=0.0,
            concentration_penalty=0.0,
            roll_execution_penalty=0.0,
        )
```

- [ ] **Step 6: Run only MPV tests and verify RED for validation cases**

Run: `pytest tests/test_directional_mpv.py -q`
Expected: validation tests FAIL for missing checks.

- [ ] **Step 7: Implement fail-closed validation**

Validate in `__post_init__` / scorer:

```python
if evidence_through >= decision_date:
    raise ValueError("evidence_through must precede decision_date")
if delta_lots not in (-1, 1):
    raise ValueError("delta_lots must be exactly -1 or +1")
if requested_lots > 0 and current_lots + delta_lots < 0:
    raise ValueError("action violates requested long direction")
if requested_lots < 0 and current_lots + delta_lots > 0:
    raise ValueError("action violates requested short direction")
if requested_lots == 0 and current_lots + delta_lots != 0:
    raise ValueError("action creates exposure absent from requested intent")
```

Also validate finite positive notionals/margins/equity, finite ratios, governor scale in `[0, 1]`, and nonnegative finite drag components.

- [ ] **Step 8: Run MPV tests and verify GREEN**

Run: `pytest tests/test_directional_mpv.py -q`
Expected: PASS.

- [ ] **Step 9: Commit and push milestone 1**

Commit message: `feat: add production marginal value domain model`

---

### Task 2: Deterministic net-alpha integer optimizer

**Files:**
- Create: `afuture/directional_integer_optimizer.py`
- Create: `tests/test_directional_integer_optimizer.py`

**Interfaces:**
- Consumes: monetary target objective callable and existing concrete integer request/current state.
- Produces: `IntegerOptimizationResult`, `optimize_integer_targets(...)`.
- Objective signature: `Callable[[Mapping[str, int]], float]`; the optimizer treats it as a pure Production-value function and never inspects future labels.

- [ ] **Step 1: Write failing positive-one-lot improvement test**

```python
from afuture.directional_integer_optimizer import optimize_integer_targets


def test_optimizer_moves_one_lot_to_strictly_higher_production_value():
    result = optimize_integer_targets(
        reference_lots={"AG2612": 1, "CU2610": 1},
        requested_lots={"AG2612": 2, "CU2610": 2},
        current_lots={"AG2612": 1, "CU2610": 1},
        lot_notionals={"AG2612": 100000.0, "CU2610": 100000.0},
        per_lot_margin={"AG2612": 15000.0, "CU2610": 15000.0},
        equity=500000.0,
        soft_margin_budget=45000.0,
        max_gross_ratio=2.0,
        max_abs_lots=35,
        objective=lambda lots: lots.get("AG2612", 0) * 200.0 + lots.get("CU2610", 0) * 100.0,
    )
    assert result.target_lots == {"AG2612": 2, "CU2610": 1}
```

- [ ] **Step 2: Run single optimizer test and verify RED**

Run: `pytest tests/test_directional_integer_optimizer.py::test_optimizer_moves_one_lot_to_strictly_higher_production_value -q`
Expected: FAIL because optimizer module does not exist.

- [ ] **Step 3: Implement minimal one-lot local improvement loop**

Use stable symbol ordering, legal one-lot candidates, strict positive objective improvement, and no capacity swaps yet.

- [ ] **Step 4: Run test and verify GREEN**

Run the same test; expected PASS.

- [ ] **Step 5: Add failing invariant tests**

Cover:

```python
def test_optimizer_never_creates_sign_absent_from_raw_request(): ...
def test_optimizer_never_exceeds_requested_magnitude(): ...
def test_optimizer_never_exceeds_35_lots(): ...
def test_optimizer_never_exceeds_two_x_gross(): ...
def test_optimizer_never_exceeds_supplied_soft_margin_budget(): ...
def test_optimizer_is_deterministic_on_equal_value_moves(): ...
def test_invalid_objective_falls_back_exactly_to_reference_target(): ...
```

The fallback assertion must compare the entire target mapping byte-for-behavior, not only gross exposure.

- [ ] **Step 6: Run optimizer test file and verify RED**

Run: `pytest tests/test_directional_integer_optimizer.py -q`
Expected: the newly added invariant tests expose missing validation/fallback behavior.

- [ ] **Step 7: Implement feasibility and exact fallback**

Implement helpers:

```python
def _normalize_lots(...): ...
def _is_within_requested_intent(...): ...
def _gross_notional(...): ...
def _margin_notional(...): ...
def _is_feasible(...): ...
```

Requirements:

- validate positive equity/notional/margin
- clamp nothing silently; invalid reference/input returns explicit fallback or raises on malformed static evidence
- no requested sign creation
- max abs lots <= min(35, supplied cap)
- gross <= `equity * max_gross_ratio`
- margin <= `soft_margin_budget`
- deterministic tie key `(objective_delta desc, symbol asc, delta asc)`

- [ ] **Step 8: Add failing capacity-swap test**

```python
def test_optimizer_swaps_low_value_lot_for_higher_value_lot_when_capacity_is_full():
    # Reference consumes all margin with 2 CU lots. One AG lot is worth more than
    # one CU lot, but AG cannot be added until a CU lot is removed.
    result = optimize_integer_targets(
        reference_lots={"CU2610": 2},
        requested_lots={"AG2612": 2, "CU2610": 2},
        current_lots={"CU2610": 2},
        lot_notionals={"AG2612": 100000.0, "CU2610": 100000.0},
        per_lot_margin={"AG2612": 20000.0, "CU2610": 20000.0},
        equity=500000.0,
        soft_margin_budget=40000.0,
        max_gross_ratio=2.0,
        max_abs_lots=35,
        objective=lambda lots: lots.get("AG2612", 0) * 300.0 + lots.get("CU2610", 0) * 100.0,
    )
    assert result.target_lots == {"AG2612": 2}
```

- [ ] **Step 9: Verify swap test RED, then implement deterministic two-leg positive swaps**

Run only the swap test before implementation; expected FAIL. Add swap enumeration only after no feasible positive one-lot move remains. Require strict positive objective delta for the pair.

- [ ] **Step 10: Run optimizer unit tests and verify GREEN**

Run: `pytest tests/test_directional_integer_optimizer.py -q`
Expected: PASS.

- [ ] **Step 11: Commit and push milestone 2**

Commit message: `feat: add net-alpha integer target optimizer`

---

### Task 3: Offline Production MPV attribution and counterfactual labels

**Files:**
- Create: `afuture/directional_mpv_attribution.py`
- Create: `tests/test_directional_mpv_attribution.py`
- Modify only if required for exact audit data exposure: `afuture/directional_acceptance.py`

**Interfaces:**
- Consumes: existing `ProductionSimulationResult.daily` and `.events` plus explicit historical contract prices for label calculation.
- Produces: `completed_product_evidence(...)`, `build_one_lot_counterfactual_labels(...)`.
- The module name and API explicitly identify future outcome data as offline labels.

- [ ] **Step 1: Write failing exact completed-product aggregation test**

```python
def test_completed_product_evidence_uses_only_events_before_decision_day():
    evidence = completed_product_evidence(
        events=sample_events_with_ag_and_cu(),
        decision_date=pd.Timestamp("2026-08-20"),
    )
    assert evidence.loc["AG", "gross_pnl"] == 300.0
    assert evidence.loc["AG", "turnover_notional"] == 100000.0
    assert evidence.loc["AG", "transaction_cost"] == 150.0
    assert evidence.loc["AG", "net_alpha"] == 150.0
```

Include a deliberately large event dated on/after the decision day and assert it is excluded.

- [ ] **Step 2: Run aggregation test and verify RED**

Run: `pytest tests/test_directional_mpv_attribution.py::test_completed_product_evidence_uses_only_events_before_decision_day -q`
Expected: FAIL because module does not exist.

- [ ] **Step 3: Implement exact pre-decision event aggregation**

Use existing audit columns only; do not infer unavailable broker fields.

- [ ] **Step 4: Run aggregation test and verify GREEN**

Expected PASS.

- [ ] **Step 5: Write failing one-lot label test**

The fixture has known open/close prices and multiplier so `+1` lot gross PnL delta and incremental transaction cost can be calculated exactly. Assert label schema includes both `decision_date` and `label_end_date` and that the label is never returned from a decision-side module.

- [ ] **Step 6: Run label test and verify RED**

Expected FAIL due missing label function.

- [ ] **Step 7: Implement offline one-lot counterfactual label helper**

Explicitly document that future prices are evaluation labels. Do not add any import from this module to live runtime/policy modules.

- [ ] **Step 8: Add causality/import-boundary regression test**

```python
def test_live_directional_modules_do_not_import_mpv_counterfactual_labels():
    for path in LIVE_RUNTIME_PATHS:
        source = Path(path).read_text(encoding="utf-8")
        assert "directional_mpv_attribution" not in source
```

- [ ] **Step 9: Run attribution + existing attribution/causality tests**

Run:
`pytest tests/test_directional_mpv_attribution.py tests/test_directional_attribution.py tests/test_causality_closure.py -q`
Expected: PASS.

- [ ] **Step 10: Commit and push milestone 3**

Commit message: `feat: add offline production MPV attribution`

---

### Task 4: Low-degree-of-freedom causal product MPV forecast

**Files:**
- Modify: `afuture/directional_mpv.py`
- Modify: `tests/test_directional_mpv.py`

**Interfaces:**
- Consumes: completed product evidence from Task 3.
- Produces: `CausalProductValueEstimate` and `estimate_causal_product_value(...)`.

- [ ] **Step 1: Write failing no-future / global-fallback tests**

Test that the estimator:

- uses only rows whose evidence cutoff is before the decision date
- returns the portfolio/global completed rate when a product has zero completed exposure evidence
- moves monotonically from global toward product evidence as completed exposure evidence grows
- exposes product evidence weight and global fallback contribution
- contains no configurable lookback, threshold, core-share, top-half, minimum-observation, or factor-weight parameters

- [ ] **Step 2: Run only estimator tests and verify RED**

Run: `pytest tests/test_directional_mpv.py -q`
Expected: estimator tests FAIL because API does not yet exist.

- [ ] **Step 3: Implement deterministic hierarchical shrinkage without searched knobs**

Use cumulative completed evidence only. The evidence-strength calculation must be derived from observed exposure support, not a searched constant. If an exact exposure denominator is unavailable from current event data, return an explicit `insufficient_evidence` result and do not fabricate it.

- [ ] **Step 4: Run estimator tests and verify GREEN**

Expected PASS.

- [ ] **Step 5: Commit and push milestone 4**

Commit message: `feat: add causal production value estimator`

---

### Task 5: Research-only acceptance adapter and fallback parity

**Files:**
- Create: `afuture/directional_mpv_robustness.py`
- Create: `tests/test_directional_mpv_robustness.py`
- Do not modify live default wiring.

**Interfaces:**
- Subclass or wrap `MarginAwareDirectionalProductionAcceptance`.
- Produces a research simulator whose target construction can invoke MPV optimization but falls back exactly to `super().target_lot_stages(...)` when evidence is unavailable/invalid.

- [ ] **Step 1: Write failing exact fallback parity test**

```python
def test_mpv_acceptance_without_causal_evidence_matches_validated_margin_aware_targets_exactly():
    baseline = MarginAwareDirectionalProductionAcceptance(config)
    candidate = MPVDirectionalProductionAcceptance(config)
    kwargs = representative_target_inputs()
    assert candidate.target_lot_stages(**kwargs).final_lots == baseline.target_lot_stages(**kwargs).final_lots
```

- [ ] **Step 2: Run fallback test and verify RED**

Expected: FAIL because research adapter does not exist.

- [ ] **Step 3: Implement minimal research adapter with fallback only**

No optimizer invocation until evidence is explicitly supplied.

- [ ] **Step 4: Verify fallback GREEN**

Run only the new test; expected PASS.

- [ ] **Step 5: Write failing causal optimizer integration test**

Fixture supplies completed pre-decision product evidence where AG has higher expected Production net value than CU under a binding soft-margin budget. Assert candidate reallocates integer capacity toward AG while satisfying gross/margin/max-lot bounds.

- [ ] **Step 6: Verify integration RED, implement optimizer bridge, verify GREEN**

Pass only pre-decision evidence into `estimate_causal_product_value` and convert its monetary forecast into candidate portfolio objective components. Transaction cost must be computed from `abs(target-current) * lot_notional * cost_rate` in the acceptance research path; do not give turnover free credit.

- [ ] **Step 7: Run focused directional subsystem tests**

Run:
`pytest tests/test_directional_mpv*.py tests/test_directional_stress_robustness.py tests/test_directional_acceptance.py tests/test_directional_acceptance_simulation.py tests/test_directional_attribution.py tests/test_directional_portfolio.py tests/test_causality_closure.py -q`
Expected: PASS.

- [ ] **Step 8: Commit and push milestone 5**

Commit message: `feat: integrate research MPV production simulator`

---

### Task 6: Cheap causal evidence and go/no-go gate

**Files:**
- Create: `scripts/research_directional_mpv.py`
- Create: `tests/test_directional_mpv_research.py`
- Create or update after evidence: `docs/production-mpv-integer-optimizer-evidence.md`

**Interfaces:**
- Consumes the same frozen directional weights/contracts used by existing L3 acceptance tooling.
- Produces deterministic JSON/Markdown metrics for baseline vs MPV candidate by required causal windows.

- [ ] **Step 1: Write failing research metric test**

Test pure summary functions on a tiny deterministic fixture for annualized return, Sharpe, DD, turnover, transaction cost, net alpha/turnover, concentration, active days, gross, margin rejects, and HALT.

- [ ] **Step 2: Run research metric test and verify RED**

Expected: FAIL because research script/helper does not exist.

- [ ] **Step 3: Implement metric/evidence adapter without running expensive L4**

Reuse existing acceptance metrics where possible. Do not duplicate formulas with divergent semantics.

- [ ] **Step 4: Run research unit tests and verify GREEN**

Expected PASS.

- [ ] **Step 5: Run cheap causal windows first**

Evaluate prior1, prior2, train, validation, OOS Base and OOS Stress before full_recent. Record exact run commands, data lineage, and candidate metrics.

Go/no-go rule: if the candidate shows broad degradation, future leakage, unstable product concentration, or Net Alpha / Turnover deterioration with no compensating robust net-alpha gain, mark it rejected and do not tune hyperparameters to rescue it.

- [ ] **Step 6: If cheap evidence passes, run Production L3 Base + Stress**

Compare against the frozen baseline. Record gross signal PnL, turnover, transaction cost, net alpha PnL, Net Alpha / Turnover, active days, realized gross, margin rejects, HALT, and capacity diagnostics.

- [ ] **Step 7: If L3 passes, run robustness**

Run leave-one-product-out and remove-best-period checks plus concentration diagnostics. Do not promote on full_recent alone.

- [ ] **Step 8: Write evidence document with explicit status**

The document must say one of:

- `PROMOTABLE CANDIDATE — pending final CI/L4`, or
- `REJECTED — validated Production remains unchanged`.

Include negative evidence rather than deleting it.

- [ ] **Step 9: Commit and push milestone 6**

Commit message: `research: evaluate production MPV integer optimizer`

---

### Task 7: Production promotion only if all gates pass

**Files:**
- Conditional modifications only if Task 6 status is promotable: the narrow target-construction wiring in `afuture/directional.py`, `afuture/directional_runtime.py`, and/or `afuture/execution_aligned_runtime.py` as required by the existing architecture.
- Tests: directly affected runtime/manager tests plus new exact fallback tests.

**Interfaces:**
- Production must retain RiskManager/Broker authority and exact reduction-first sequencing.

- [ ] **Step 1: If candidate is rejected, skip Production wiring entirely**

Delete/disconnect any experimental default/runtime path. Research modules may remain only if they have independent audit/research value and tests.

- [ ] **Step 2: If candidate is promotable, write failing runtime wiring test first**

Assert that the promoted runtime obtains target lots through the proven MPV path while an invalid/missing-evidence case produces the exact legacy validated target.

- [ ] **Step 3: Verify runtime test RED**

Run only the directly affected runtime test.

- [ ] **Step 4: Implement the smallest Production wiring**

Do not modify risk thresholds, governor authority, execution policy, contract-selection semantics, or Broker truth.

- [ ] **Step 5: Verify runtime tests GREEN and run directional subsystem milestone suite**

Use targeted runtime/manager/risk tests first, then the directional subsystem suite once.

- [ ] **Step 6: Commit and push conditional promotion**

Commit message if applicable: `feat: promote validated production MPV allocation`

---

### Task 8: Final verification, diff review, PR, and squash merge

**Files:**
- Update final evidence/docs only if verification changes evidence status.

- [ ] **Step 1: Read `superpowers:verification-before-completion` and review final branch diff**

Check for temporary workflow files, dead candidate wiring, accidental risk-limit edits, Candidate A/B parameter changes, future-label imports, and documentation contradictions.

- [ ] **Step 2: Run final complete CI once**

Use the repository's existing required Python 3.10 and 3.13 CI jobs. If a specific job fails, inspect its logs and run/re-run only the failed/affected job after the fix unless the fix changes shared behavior enough to require the full matrix again.

- [ ] **Step 3: Run final L4/acceptance matrix once if the candidate is being promoted**

If the candidate was rejected and Production remained exactly unchanged, do not wastefully rerun a full economic L4 solely to rediscover the frozen baseline; run the repository-required evidence/closure checks that prove runtime parity and document why Production L4 is inherited.

- [ ] **Step 4: Create PR and review changed files/diff**

PR body must list:

- baseline SHA
- each milestone commit
- whether Production changed
- exact economic evidence status
- hard-risk parity
- tests/CI run
- retained negative evidence

- [ ] **Step 5: Resolve material review findings with TDD and affected verification**

No cosmetic churn after final full validation unless necessary.

- [ ] **Step 6: Squash merge only if gates are satisfied**

Use expected head SHA protection. After merge, fetch `main`, confirm the merge SHA and required CI status, and report the final Production/research status.
