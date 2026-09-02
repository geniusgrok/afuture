# Opportunity-Driven Directional V2 Implementation Plan

> **归档说明：** 本文是已完成阶段的实施记录，不描述当前系统；当前事实见 [`documentation-index.md`](../../../documentation-index.md)。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a causal product-opportunity selection and conservative portfolio overlay to the frozen execution-aligned directional core, promoting it only if fixed Production mechanics improves the joint Base/Stress/OOS/risk objective.

**Architecture:** Keep the 96-template core and global Meta as the raw Alpha engine. Build six completed-day product-opportunity features, rank only active raw targets, and apply a frozen 75/25 core-satellite de-emphasis that can never create, flip, or enlarge product risk. Extend the daily signal history with volume/open-interest so live and historical feature inputs are identical.

**Tech Stack:** Python 3.10+, pandas, numpy, pytest, GitHub Actions.

**Spec:** `docs/archive/development/specs/2026-08-24-opportunity-driven-directional-v2-design.md`

## Global Constraints

- Frozen baseline is `main@e89ff6c03b9909904ddcc958891cbedad0d30918`.
- Keep all 96 frozen directional templates and existing global Meta logic unchanged.
- Hard target/realized gross remains <=2x.
- Hard margin remains <=35%; available remains >=25%; max contract volume remains 35.
- Daily loss 5%, total drawdown 30%, reduction-first, circuit/HALT, Broker/CTP truth and fail-closed risk semantics remain unchanged.
- Opportunity evidence is completed-day only and shifted one trading day before use.
- No parameter grid or post-failure threshold search.
- Use progressive verification: targeted tests during edits, affected directional suite at milestones, one full final CI/L4 after the final candidate is stable.

---

### Task 1: Pure product-opportunity engine

**Files:**
- Create: `afuture/directional_opportunity.py`
- Create: `tests/test_directional_opportunity.py`

**Interfaces:**
- Consumes: daily `close`, optional `volume`, optional `open_interest`, and raw product-weight history as aligned pandas DataFrames.
- Produces:
  - `build_opportunity_score(close, volume, open_interest) -> pd.DataFrame`
  - `apply_opportunity_overlay(raw_weights, score) -> pd.DataFrame`
  - frozen constants `OPPORTUNITY_CORE_SHARE = 0.75`, `SHORT_WINDOW = 20`, `LONG_WINDOW = 60`.

- [ ] **Step 1: Write RED tests for invariants**

Tests must prove:

```python
assert (overlay.abs() <= raw.abs() + 1e-12).all().all()
assert ((overlay * raw) >= -1e-12).all().all()
assert (overlay.where(raw == 0.0, 0.0) == 0.0).all().all()
assert overlay.abs().sum(axis=1).le(raw.abs().sum(axis=1) + 1e-12).all()
```

Also assert top `ceil(N/2)` active products retain 100% raw weight, lower-ranked active products become 75% raw weight, and no-evidence warm-up rows equal the raw core.

- [ ] **Step 2: Run the smallest RED scope**

Run `pytest -q tests/test_directional_opportunity.py`; expected failure is missing module/functions.

- [ ] **Step 3: Implement causal score construction**

Implement exactly the six components from the spec. Cross-sectional percentile ranks are equal-weighted. Shift the combined score by one row before returning it. Do not forward-fill missing volume/OI.

- [ ] **Step 4: Implement conservative overlay**

For each row, restrict ranking to non-zero raw targets. If no active target has finite score, return raw weights unchanged. Otherwise select top `ceil(valid_active/2)`, keep selected raw weights unchanged, and multiply every other active raw target by `0.75`.

- [ ] **Step 5: GREEN targeted tests**

Run `pytest -q tests/test_directional_opportunity.py` and `python -m compileall -q afuture/directional_opportunity.py`.

- [ ] **Step 6: Commit checkpoint**

Commit `feat: add causal directional opportunity selector`.

---

### Task 2: Live OHLCV/OI signal parity

**Files:**
- Modify: `afuture/execution_aligned_runtime.py`
- Modify/add focused tests in the existing execution-aligned runtime test module(s).

**Interfaces:**
- Extend `ExecutionAlignedSignalHistory` to carry `volume: pd.DataFrame | None` and `open_interest: pd.DataFrame | None` without breaking callers that construct it with open/close only.
- `SinaContinuousOHLCProvider._load_one()` must parse `volume` and `hold` when present.
- `_normalize_history()` must align all available panels to the same completed-date/product axes.

- [ ] **Step 1: Write RED provider/history tests**

Test that a Sina-like frame with `date/open/close/volume/hold` returns aligned volume/OI panels, and that open/close-only history still normalizes safely.

- [ ] **Step 2: Run only affected runtime tests**

Expected RED from missing new fields/normalization behavior.

- [ ] **Step 3: Implement the minimal history extension**

Require open/close as before. Treat absent volume/hold as unavailable opportunity evidence, not zeros or synthetic data.

- [ ] **Step 4: GREEN affected tests and compile**

Run only the execution-aligned runtime/provider tests plus `compileall` for modified modules.

- [ ] **Step 5: Commit checkpoint**

Commit `feat: carry completed activity into directional signals`.

---

### Task 3: Integrate selector after frozen Alpha core

**Files:**
- Modify: `afuture/execution_aligned_policy.py`
- Modify: `afuture/execution_aligned_runtime.py`
- Modify: `tests/test_execution_aligned_policy.py` or the existing policy test file.

**Interfaces:**
- Add optional keyword-only arguments to policy history/target methods:

```python
weight_history(open_prices, close, *, volume=None, open_interest=None)
target_weights(open_prices, close, *, volume=None, open_interest=None)
```

- Raw 96-template/Meta aggregation remains byte-for-byte behaviorally equivalent until the final opportunity overlay call.

- [ ] **Step 1: Write RED integration tests**

Assert missing volume/OI yields the exact core raw weights; complete activity data yields the selector overlay; future-row changes cannot alter earlier weights; gross remains <=2x.

- [ ] **Step 2: Run targeted policy tests and confirm RED**

- [ ] **Step 3: Apply the opportunity overlay only after raw Meta aggregation**

Do not alter `_signal_scores`, template IDs, template selection, Meta scoring, Base/Stress survival logic, or existing raw aggregation.

- [ ] **Step 4: Pass live volume/OI from the execution-aligned manager**

When appending the synthetic next session, append the latest completed volume/OI row only so the selector's one-row shift makes the synthetic target consume the actual latest completed evidence.

- [ ] **Step 5: GREEN targeted policy/runtime tests**

Run the smallest policy + runtime set and compile modified modules.

- [ ] **Step 6: Commit checkpoint**

Commit `feat: apply product opportunity overlay to directional core`.

---

### Task 4: Historical evaluator and exact-lineage parity

**Files:**
- Modify: `tools/evaluate_execution_aligned_target.py`
- Modify: `afuture/directional_lineage.py`
- Modify: `tools/evaluate_directional_lineage.py` if required by its current call surface.
- Modify/add focused lineage/evaluator tests.

**Interfaces:**
- Historical `broad_daily_universe.csv` must be pivoted into the same open/close/volume/hold panels used live.
- Exact-lineage replay must call the production policy with the same opportunity inputs, then assert exact target-weight closure as it does today.

- [ ] **Step 1: Write RED parity tests**

Use a tiny deterministic panel to prove evaluator and direct policy calls produce identical opportunity-adjusted weights.

- [ ] **Step 2: Implement shared historical panel construction**

Map `hold` -> `open_interest`; do not change specific-contract roll/execution semantics.

- [ ] **Step 3: Update lineage replay and closure**

No fictional template-level turnover/cost allocation is introduced.

- [ ] **Step 4: Run affected directional evaluator/lineage tests**

- [ ] **Step 5: Commit checkpoint**

Commit `test: close opportunity selector backtest lineage`.

---

### Task 5: Fixed L3 candidate evidence

**Files:**
- Create temporary branch-only workflow if needed for fixed-artifact L3; delete it before final merge.
- Create: `docs/directional-opportunity-v2-evidence.md` after results are known.

**Interfaces:**
- Fixed input artifact remains the PR #17 lineage input.
- Production mechanics remain `DirectionalProductionAcceptance` / `MarginAwareDirectionalProductionAcceptance` with unchanged risk limits.

- [ ] **Step 1: Run the cheap float screen once**

Record Base/Stress full_recent, prior1, prior2, train, validation, OOS, Sharpe, DD and weight turnover. The implementation must match the pre-implementation screen within numeric tolerance.

- [ ] **Step 2: Run one fixed Production-mechanics L3**

Record Base/Stress annualized, total return, DD, Sharpe, active days, realized gross, margin rejects, HALT, turnover notional, gross PnL, transaction cost and Net Alpha / Turnover.

- [ ] **Step 3: Apply the acceptance gate without retuning**

If the candidate fails the spec gates, revert its production activation while retaining only useful behavior-neutral diagnostics/tests. Do not change 75/25, lookbacks, factor weights, top-half rule, or thresholds after seeing L3.

- [ ] **Step 4: Robustness diagnostics for a passing candidate**

Run leave-one-product-out / top-contributor concentration diagnostics and a small lookback perturbation check (e.g. 15/45 and 30/90) strictly as sensitivity evidence; do not choose parameters from those variants.

- [ ] **Step 5: Commit evidence checkpoint**

Commit `docs: record directional opportunity v2 evidence`.

---

### Task 6: Documentation, final verification, PR and merge

**Files:**
- Modify: `docs/architecture.md`
- Modify: `docs/data-and-backtest.md`
- Modify: `docs/production-checklist.md` only if production behavior is promoted.
- Keep standard `.github/workflows/ci.yml` unchanged in the final tree unless a genuine permanent CI requirement exists.

- [ ] **Step 1: Synchronize architecture and economic evidence**

State exactly whether V2 was promoted or rejected. Do not report float results as Production results.

- [ ] **Step 2: Run affected directional subsystem suite**

Run the policy/runtime/opportunity/lineage/acceptance test group and compileall.

- [ ] **Step 3: Run one final full CI/L4 on the stable final tree**

Python 3.10 and 3.13 must both pass. If full verification fails and behavior code changes, rerun only failed/affected checks first, then one new final full run.

- [ ] **Step 4: Review final diff**

Verify no temporary workflow, no rejected research code, no risk-limit relaxation, no stale docs, and no unintended template/Meta changes.

- [ ] **Step 5: Create/update PR, make ready, squash merge to main**

Merge only after all final gates pass and remote `main` has not advanced unexpectedly.

- [ ] **Step 6: Verify merged main**

Confirm merge SHA, tree parity with the final tested feature head, and final CI status.
