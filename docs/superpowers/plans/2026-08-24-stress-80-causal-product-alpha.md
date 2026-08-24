# Stress 80 Causal Product × Alpha Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and validate a causal Product × Alpha net-edge allocator whose final Production promotion gate is Stress 15bp annualized return >=80% without relaxing existing hard risk constraints.

**Architecture:** Add a research-only opportunity ledger that labels every observable product/family signal independently of whether Production traded it, estimate causal family/product edge from completed labels only, translate that edge into monetary integer-lot objectives, and evaluate the candidate through the existing Production mechanics. Live runtime wiring remains untouched unless the final Production gate passes.

**Tech Stack:** Python 3.10/3.13, pandas, numpy, pytest, GitHub Actions, existing `DirectionalProductionAcceptance` / `MarginAwareDirectionalProductionAcceptance`, existing `optimize_integer_targets`.

**Spec:** `docs/superpowers/specs/2026-08-24-stress-80-causal-product-alpha-design.md`

## Global Constraints

- Stress one-way 15bp full_recent annualized return >=80% for Production promotion.
- Stress max DD <=30%; Base annualized >=80%.
- Gross <=2x; hard margin <=35%; available >=25%; max absolute contract lots <=35.
- Daily loss 5% and total DD 30% gates unchanged.
- Reduction-first, RiskManager authority and Broker/CTP truth unchanged.
- Fixed horizons are exactly 5, 10 and 20 sessions; no post-hoc horizon rescue.
- No future leakage, synthetic missing history, broad OHLC parameter search, or mixing rejected basis/curve signals to rescue the result.
- Use progressive verification: focused RED/GREEN tests, then L3A, then L3B only if L3A passes, then one final full CI/L4 candidate gate.

---

### Task 1: Branch-only targeted TDD harness

**Files:**
- Create: `.github/workflows/research-stress80-targeted.yml`

**Interfaces:**
- Consumes: repository dev dependency set from `pyproject.toml`.
- Produces: branch-only push workflow that runs the four focused Stress-80 test modules and compileall on Python 3.13.

- [ ] **Step 1: Add the targeted workflow**

```yaml
name: research-stress80-targeted
on:
  push:
    branches: [research/stress-80-causal-product-alpha]
    paths:
      - "afuture/directional_opportunity_ledger.py"
      - "afuture/directional_causal_edge.py"
      - "afuture/directional_net_edge_allocator.py"
      - "afuture/directional_stress80_research.py"
      - "tests/test_directional_opportunity_ledger.py"
      - "tests/test_directional_causal_edge.py"
      - "tests/test_directional_net_edge_allocator.py"
      - "tests/test_directional_stress80_research.py"
      - "tools/evaluate_directional_stress80.py"
  workflow_dispatch:

jobs:
  focused:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - run: python -m pip install --upgrade pip
      - run: python -m pip install -e ".[dev]"
      - run: >-
          python -m pytest -q
          tests/test_directional_opportunity_ledger.py
          tests/test_directional_causal_edge.py
          tests/test_directional_net_edge_allocator.py
          tests/test_directional_stress80_research.py
      - run: python -m compileall -q afuture
```

- [ ] **Step 2: Commit only the harness**

Expected: no production behavior change and no expensive research run.

---

### Task 2: Exploration-independent opportunity ledger

**Files:**
- Create: `tests/test_directional_opportunity_ledger.py`
- Create: `afuture/directional_opportunity_ledger.py`

**Interfaces:**
- Consumes: `signals: Mapping[str, pd.DataFrame]`, `forward_returns: Mapping[int, pd.DataFrame]`, `label_available_dates: Mapping[int, pd.DataFrame]`.
- Produces: `ALLOWED_HORIZONS = (5, 10, 20)`; `build_opportunity_ledger(...) -> pd.DataFrame`; `completed_opportunities(ledger, *, decision_date) -> pd.DataFrame`.

- [ ] **Step 1: Write RED tests for causal availability and off-policy labels**

```python
def test_untraded_opportunity_is_labeled_and_future_label_is_hidden():
    signals = {"breakout": pd.DataFrame({"AG": [1.0]}, index=[pd.Timestamp("2026-01-02")])}
    forward = {5: pd.DataFrame({"AG": [0.04]}, index=signals["breakout"].index)}
    available = {5: pd.DataFrame({"AG": [pd.Timestamp("2026-01-09")]}, index=signals["breakout"].index)}
    ledger = build_opportunity_ledger(signals=signals, forward_returns=forward, label_available_dates=available, horizons=(5,))
    assert len(ledger) == 1
    assert completed_opportunities(ledger, decision_date=pd.Timestamp("2026-01-09")).empty
    visible = completed_opportunities(ledger, decision_date=pd.Timestamp("2026-01-12"))
    assert visible.iloc[0]["future_specific_contract_gross_return"] == 0.04
```

Add separate assertions that signal direction is applied to the raw future return, zero/non-finite signals are omitted, missing labels are not filled, and any horizon outside `(5, 10, 20)` raises `ValueError`.

- [ ] **Step 2: Run the targeted workflow and verify RED**

Expected: import failure for `afuture.directional_opportunity_ledger`.

- [ ] **Step 3: Implement the minimal ledger**

The implementation normalizes dates/products/families, emits one row per nonzero finite signal and available label, computes `signal_direction`, `signal_strength`, direction-conditioned gross return, and a diagnostic 15bp round-trip net return as `gross - 0.003`. It must not accept a Production-trade ledger as an input.

- [ ] **Step 4: Run targeted workflow and verify GREEN**

Expected: opportunity-ledger tests pass; unrelated new test modules may still be absent only until their RED commits are added one task at a time.

- [ ] **Step 5: Commit**

---

### Task 3: Causal Product × Alpha edge estimator

**Files:**
- Create: `tests/test_directional_causal_edge.py`
- Create: `afuture/directional_causal_edge.py`

**Interfaces:**
- Consumes: completed opportunity ledger from Task 2.
- Produces: `CausalEdgeEstimate` dataclass; `estimate_product_family_edge(ledger, *, decision_date, product, family, horizon) -> CausalEdgeEstimate`; `estimate_current_edges(ledger, *, decision_date) -> pd.DataFrame`.

- [ ] **Step 1: Write RED tests**

Tests must prove:

```python
def test_future_labels_do_not_change_current_estimate():
    # Add one huge label whose label_available_date equals/comes after decision_date.
    # Current estimate must be byte-for-byte numerically identical to the estimate without it.
```

```python
def test_sparse_product_family_shrinks_to_family_then_global_instead_of_zero():
    # One AG/breakout observation plus richer breakout/global evidence.
    # Expected edge is finite, nonzero and between the sparse cell mean and its prior.
```

Also prove support count and standard error are exposed, and prior support is the median positive `(product,family,horizon)` support for the same horizon rather than a tuned constant.

- [ ] **Step 2: Verify RED**

Expected: missing estimator module/API.

- [ ] **Step 3: Implement deterministic empirical shrinkage**

For one horizon:

```text
completed rows = label_available_date < decision_date
PF mean/support
family mean/support
global mean/support
prior_support = median positive PF support
family_weight = family_support / (family_support + prior_support)
family_prior = family_weight * family_mean + (1-family_weight) * global_mean
pf_weight = pf_support / (pf_support + prior_support)
expected = pf_weight * pf_mean + (1-pf_weight) * family_prior
```

No searched threshold or minimum-observation cutoff. If no global evidence exists, mark the estimate insufficient rather than inventing zero alpha.

- [ ] **Step 4: Verify GREEN and refactor**

- [ ] **Step 5: Commit**

---

### Task 4: Monetary net-edge allocator and integer optimizer bridge

**Files:**
- Create: `tests/test_directional_net_edge_allocator.py`
- Create: `afuture/directional_net_edge_allocator.py`

**Interfaces:**
- Consumes: current lots, requested directional lots, symbol→product mapping, lot notionals/margins, causal expected return per product, Stress cost rate.
- Produces: `NetEdgeAllocationResult`; `optimize_net_edge_targets(...) -> NetEdgeAllocationResult` using existing `optimize_integer_targets`.

- [ ] **Step 1: Write RED tests**

Required scenarios:

```python
def test_weak_positive_gross_edge_is_rejected_when_entry_cost_is_larger():
    # Flat -> one AG lot; expected monetary alpha < 15bp one-way cost.
    # Target remains flat.
```

```python
def test_higher_value_product_can_replace_lower_value_lot_at_capacity():
    # Capacity full with CU; AG has higher completed causal edge.
    # Optimizer performs legal CU -1 / AG +1 swap.
```

```python
def test_recovered_product_can_regain_capital_without_candidate_owned_history():
    # Edge input comes from Task-2/3 off-policy ledger and is positive after recovery.
    # Allocator adds the product even though current_lots has never contained it.
```

Also prove gross <=2x, soft margin <=caller envelope<=35%, max lots<=35, sign intent unchanged, and unchanged targets incur zero turnover cost.

- [ ] **Step 2: Verify RED**

- [ ] **Step 3: Implement objective**

For a candidate integer target:

```text
expected gross monetary alpha
= sum(abs(lots) * lot_notional * expected_horizon_return)

exact transition cost
= sum(abs(target_lots-current_lots) * lot_notional * cost_rate)

objective = expected gross monetary alpha - exact transition cost
```

No arbitrary risk-factor weights. Hard constraints remain delegated to existing optimizer plus downstream Production mechanics.

- [ ] **Step 4: Verify GREEN**

- [ ] **Step 5: Commit**

---

### Task 5: Research-only Product × Alpha signal/Production adapter

**Files:**
- Create: `tests/test_directional_stress80_research.py`
- Create: `afuture/directional_stress80_research.py`

**Interfaces:**
- Consumes: frozen 96-template signal machinery, completed opportunity ledger, current decision date, existing margin-aware Production acceptance path.
- Produces: family-level signal history, deterministic current expected product edge, and `Stress80DirectionalProductionAcceptance` subclass that changes only research target allocation.

- [ ] **Step 1: Write RED tests for family aggregation and live isolation**

Tests prove:
- only the six allowed families are emitted;
- decision `D` only consumes labels available strictly before `D`;
- equal per-session composite across fixed 5/10/20 horizons is deterministic and contains no configurable horizon weights;
- negative/insufficient family edge cannot invert the raw family direction;
- `directional_runtime.py`, `execution_aligned_runtime.py`, and `runtime_factory.py` do not import the research module or opportunity-label module;
- adapter reuses baseline `TargetLotStages`/margin envelope and falls back exactly on invalid evidence or roll-evidence mismatch.

- [ ] **Step 2: Verify RED**

- [ ] **Step 3: Implement research adapter**

Family signal history is derived from the already-frozen 96 templates by grouping template paths by `family`; no new template IDs or lookbacks are searched. Current product edge is the equal average of available `expected_gross_return / horizon` across 5/10/20 for families whose current signal is nonzero; negative expected edge suppresses that family contribution but never reverses its sign. Desired product direction/gross is normalized to <=2x before integer allocation.

- [ ] **Step 4: Verify GREEN**

- [ ] **Step 5: Commit**

---

### Task 6: Fixed-input L3A evaluator and predeclared gate

**Files:**
- Create: `tools/evaluate_directional_stress80.py`
- Create or temporary: `.github/workflows/research-stress80-l3a.yml`
- Modify tests only if the evaluator exposes a correctness bug.

**Interfaces:**
- Consumes: frozen fixed historical artifacts and Task 2-5 research modules.
- Produces: `runtime/stress80_l3a_report.json` with prior1/prior2/train/validation/OOS/full_recent Base+Stress annualized, Sharpe, DD, turnover, gross alpha, cost, net alpha, net-alpha/turnover, active days and concentration.

- [ ] **Step 1: Add evaluator-level tests for the gate function before implementation**

```python
def test_l3a_gate_requires_all_predeclared_conditions():
    # exactly enforce >28.9559 full_recent Stress, >0 validation/OOS,
    # <=30% validation/OOS DD, >12.2556 bps net-alpha/turnover, no HALT/risk violation.
```

- [ ] **Step 2: Verify RED, implement evaluator/gate, verify GREEN**

- [ ] **Step 3: Run one fixed-input L3A**

If any gate fails, stop Phase 1 economic promotion, preserve the negative evidence, and proceed only to Phase 2 data-source research; do not tune horizons/thresholds.

- [ ] **Step 4: If and only if L3A passes, continue to Task 7**

---

### Task 7: Fixed Production L3B and final promotion decision

**Files:**
- Modify: `tools/evaluate_directional_stress80.py`
- Create or temporary: `.github/workflows/research-stress80-l3b.yml`
- Create: `docs/stress80-causal-product-alpha-final-evidence.md`
- Modify live wiring only if every final promotion gate passes.

**Interfaces:**
- Consumes: same concrete-contract Production mechanics and baseline artifacts used by current validated Production.
- Produces: auditable Base/Stress Production report and final promote/reject decision.

- [ ] **Step 1: Run fixed Production mechanics with Base 5bp and Stress 15bp**

Required hard checks:
- Stress full_recent annualized >=80%; DD<=30%; no permanent HALT.
- Base annualized >=80%.
- Validation/OOS Stress annualized >0%, DD<=30%, no HALT/hard-risk violation.
- gross<=2x, margin<=35%, available>=25%, max lots<=35.

- [ ] **Step 2: Preserve economic attribution**

Report gross PnL, turnover, exact transaction cost, net alpha, net-alpha/turnover, product/family concentration and divergence from baseline.

- [ ] **Step 3: Promotion behavior**

If every gate passes, wire the validated candidate through the existing live factory without creating a second risk/account state machine and add focused live-surface regression tests. If any gate fails economically, leave live Production exactly unchanged and retain research/evidence only.

- [ ] **Step 4: Remove temporary research workflows after evidence is captured**

- [ ] **Step 5: Run final full Python 3.10/3.13 CI once**

- [ ] **Step 6: Review PR and squash merge**

Merge behavior-changing code only if the final Production promotion gate passes; otherwise merge only behavior-neutral research/evidence infrastructure if it is clean and useful.
