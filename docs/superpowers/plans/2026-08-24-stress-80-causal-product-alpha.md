# Stress 80 Causal Product × Alpha Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and validate a causal Product × Alpha net-edge allocator whose final Production promotion gate is Stress 15bp annualized return >=80% without relaxing existing hard risk constraints.

**Architecture:** Add a research-only opportunity ledger that labels every observable product/family signal independently of whether Production traded it, estimate causal family/product edge from completed labels only, translate that edge into monetary integer-lot objectives, and evaluate the candidate through the existing Production mechanics. Live runtime wiring remains untouched unless the final Production gate passes.

**Tech Stack:** Python 3.10/3.13, pandas, numpy, pytest, GitHub Actions, existing Production acceptance classes and `optimize_integer_targets`.

**Spec:** `docs/superpowers/specs/2026-08-24-stress-80-causal-product-alpha-design.md`

## Global Constraints

- Stress one-way 15bp full_recent annualized return >=80% for Production promotion.
- Stress max DD <=30%; Base annualized >=80%.
- Gross <=2x; hard margin <=35%; available >=25%; max absolute contract lots <=35.
- Daily loss 5% and total DD 30% gates unchanged.
- Reduction-first, RiskManager authority and Broker/CTP truth unchanged.
- Fixed horizons are exactly 5, 10 and 20 sessions; no post-hoc horizon rescue.
- No future leakage, synthetic missing history, broad OHLC parameter search, or mixing rejected basis/curve signals to rescue the result.
- Progressive verification only: focused RED/GREEN, L3A, L3B only if L3A passes, then one final full CI/L4 gate.

---

### Task 1: Branch-only targeted TDD harness

**Files:** Create `.github/workflows/research-stress80-targeted.yml`.

**Produces:** Python 3.13 branch-only focused test loop. The workflow discovers only Stress-80 test files that currently exist, so a GREEN milestone is not polluted by later test modules that have not yet been created.

- [ ] Add workflow with these path filters: the four new research modules, four focused test modules, and `tools/evaluate_directional_stress80.py`.
- [ ] Install with `python -m pip install -e ".[dev]"`.
- [ ] Build an `existing=()` array from the four planned test paths, append only `-f` files, run `python -m pytest -q "${existing[@]}"`, then `python -m compileall -q afuture`.
- [ ] Commit harness only.

---

### Task 2: Exploration-independent opportunity ledger

**Files:** Create `tests/test_directional_opportunity_ledger.py`, then `afuture/directional_opportunity_ledger.py`.

**Produces:** `ALLOWED_HORIZONS=(5,10,20)`, `build_opportunity_ledger(...) -> pd.DataFrame`, `completed_opportunities(ledger, *, decision_date) -> pd.DataFrame`.

- [ ] RED test: an AG breakout signal receives a 5-session label even though no Production trade input exists; `label_available_date == decision_date` is hidden and becomes visible only for a later decision.
- [ ] RED tests: signal sign is applied to raw future return; zero/non-finite signals are omitted; missing labels are not filled; horizons outside 5/10/20 raise `ValueError`.
- [ ] Verify RED in targeted Actions: missing module/API is the expected failure.
- [ ] Implement minimal ledger. Normalize dates/products/families, emit rows only for valid nonzero observable signals with real labels, compute `signal_direction`, `signal_strength`, direction-conditioned gross return, and diagnostic 15bp round-trip net return `gross - 0.003`.
- [ ] Verify GREEN and commit.

---

### Task 3: Causal Product × Alpha estimator

**Files:** Create `tests/test_directional_causal_edge.py`, then `afuture/directional_causal_edge.py`.

**Produces:** `CausalEdgeEstimate`, `estimate_product_family_edge(...)`, `estimate_current_edges(...)`.

- [ ] RED test: adding an arbitrarily huge label whose `label_available_date >= decision_date` cannot change the current estimate.
- [ ] RED test: one sparse AG/breakout observation shrinks toward richer breakout/global completed evidence instead of hard-zeroing.
- [ ] RED tests: expose support and standard error; derive `prior_support` as median positive `(product,family,horizon)` support for the same horizon.
- [ ] Verify RED.
- [ ] Implement expanding completed-history estimator. For one horizon use PF mean/support, family mean/support, global mean/support; `prior_support=median positive PF support`; shrink family to global and PF to that family prior using `support/(support+prior_support)`. If no global evidence exists, mark insufficient rather than inventing zero alpha.
- [ ] Verify GREEN and commit.

---

### Task 4: Monetary net-edge integer allocator

**Files:** Create `tests/test_directional_net_edge_allocator.py`, then `afuture/directional_net_edge_allocator.py`.

**Produces:** `NetEdgeAllocationResult`, `optimize_net_edge_targets(...)`, reusing existing `optimize_integer_targets`.

- [ ] RED test: flat -> one AG lot with positive gross alpha smaller than exact 15bp entry cost remains flat.
- [ ] RED test: at full capacity, higher-value AG can legally replace lower-value CU via two-leg swap.
- [ ] RED test: a recovered product can regain capital using off-policy edge evidence even when it has never existed in `current_lots`.
- [ ] RED tests: gross<=2x, caller soft margin<=35%, max lots<=35, sign intent preserved, unchanged targets cost zero.
- [ ] Verify RED.
- [ ] Implement objective: `sum(abs(target_lots)*lot_notional*expected_return) - sum(abs(target-current)*lot_notional*cost_rate)`. Do not add arbitrary risk-factor weights.
- [ ] Verify GREEN and commit.

---

### Task 5: Research Product × Alpha signal and Production adapter

**Files:** Create `tests/test_directional_stress80_research.py`, then `afuture/directional_stress80_research.py`.

**Produces:** family-level history for exactly breakout/tsmom/momentum/moving_average/reversal/acceleration, deterministic current product edge, and research-only `Stress80DirectionalProductionAcceptance`.

- [ ] RED tests: only six allowed families; decision D consumes only labels available strictly before D; fixed 5/10/20 composite has no configurable horizon weights; negative edge can suppress but never invert a family direction.
- [ ] RED tests: `directional_runtime.py`, `execution_aligned_runtime.py`, `runtime_factory.py` do not import research/future-label modules; invalid evidence and roll mismatch fall back exactly to baseline stages.
- [ ] Verify RED.
- [ ] Derive family paths by grouping the frozen 96 template paths by existing template `family`; do not add templates/lookbacks. Convert available expected gross returns to per-session values (`expected/horizon`) and equal-average 5/10/20. Normalize desired product gross <=2x, then use Task 4 inside the existing margin-aware target envelope.
- [ ] Verify GREEN and commit.

---

### Task 6: Fixed-input L3A cheap screen

**Files:** Create `tests/test_directional_stress80_evaluator.py`, `tools/evaluate_directional_stress80.py`; temporary `.github/workflows/research-stress80-l3a.yml` if needed.

**Produces:** prior1/prior2/train/validation/OOS/full_recent Base+Stress annualized, Sharpe, DD, turnover, gross alpha, exact cost, net alpha, net-alpha/turnover, active days and contribution concentration.

- [ ] RED test the gate before evaluator code: require full_recent Stress >28.9559%, validation/OOS Stress >0%, validation/OOS DD<=30%, full_recent Stress net-alpha/turnover >12.2556bps, no HALT/hard-risk violation.
- [ ] Verify RED; implement evaluator/gate; verify GREEN.
- [ ] Run one fixed-input L3A using the frozen historical lineage.
- [ ] If any gate fails, stop Phase-1 economic promotion, preserve negative evidence, and move to genuinely new PIT data research. Do not tune horizons/thresholds.
- [ ] If every gate passes, continue to Task 7.

---

### Task 7: Fixed Production L3B, evidence, final decision

**Files:** Extend `tools/evaluate_directional_stress80.py`; temporary `.github/workflows/research-stress80-l3b.yml`; create `docs/stress80-causal-product-alpha-final-evidence.md`; modify live wiring only after all final gates pass.

- [ ] Run current concrete-contract/integer/margin/governor/circuit Production mechanics at Base 5bp and Stress 15bp.
- [ ] Require Stress full_recent annualized>=80%, DD<=30%, no permanent HALT; Base>=80%; validation/OOS Stress>0%, DD<=30%, no HALT/hard-risk violation; gross<=2x, margin<=35%, available>=25%, max lots<=35.
- [ ] Report gross PnL, turnover, exact cost, net alpha, net-alpha/turnover, family/product concentration and baseline divergence.
- [ ] If every gate passes, wire the candidate through the existing live factory with focused live-surface regression tests. If any gate fails economically, leave validated Production unchanged.
- [ ] Remove temporary research workflows after evidence capture.
- [ ] Run final full Python 3.10/3.13 CI once after final candidate tree stabilizes.
- [ ] Review PR and squash merge. Behavior-changing code merges only if the Production promotion gate passes; otherwise only behavior-neutral research/evidence may merge.
