# Directional Stress Robustness Implementation Plan

> **归档说明：** 本文是已完成阶段的实施记录，不描述当前系统；当前事实见 [`documentation-index.md`](../../../documentation-index.md)。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make directional production targets margin-feasible and make the frozen meta allocator explicitly robust to 5bp/15bp costs without relaxing any hard risk gate.

**Architecture:** Add one deterministic lot-budget primitive shared by live target construction and the historical production-mechanics proxy. Keep the 96-template signal surface fixed, but score every template on both Base and Stress cost evidence and require both endpoints to survive. Preserve RiskManager as the final fail-closed authority.

**Tech Stack:** Python 3.10/3.13, pandas, NumPy, pytest, GitHub Actions.

**Spec:** `docs/archive/development/specs/2026-08-23-directional-stress-robustness-design.md`

## Global Constraints

- Gross target and realized gross ceiling stay at 2.0x.
- `max_daily_loss_ratio=5%`, `max_total_drawdown_ratio=30%`, `max_margin_ratio=35%`, `min_available_ratio=25%` stay unchanged.
- Directional max contract volume stays 35.
- No large parameter/template search on the already-observed two-year history.
- Production and acceptance must use equivalent margin-aware sizing semantics.
- New behavior follows TDD; final full CI runs only after the candidate is stable.

---

### Task 1: Shared margin-budget sizing primitive

**Files:**
- Modify: `afuture/directional.py`
- Create: `tests/test_directional_stress_robustness.py`

**Interfaces:**
- Produces: `fit_target_lots_to_margin_budget(target_lots: Mapping[str, int], per_lot_margin: Mapping[str, float], margin_budget: float) -> dict[str, int]`.
- Guarantee: output sign is unchanged; `abs(output[s]) <= abs(input[s])`; estimated output margin never exceeds the non-negative budget; invalid positive target without a positive margin estimate fails closed with `ValueError`.

- [ ] **Step 1: Write RED tests** covering a 20-lot target whose 37,500 estimated margin exceeds a 35,000 budget, deterministic multi-symbol residual allocation, sign preservation and missing margin failure.
- [ ] **Step 2: Run only `tests/test_directional_stress_robustness.py` and verify the tests fail because the primitive does not exist.**
- [ ] **Step 3: Implement proportional integer scaling plus deterministic residual one-lot allocation in `afuture/directional.py`.**
- [ ] **Step 4: Run the new test file and `tests/test_directional_portfolio.py`; both must pass.**
- [ ] **Step 5: Commit the milestone.**

### Task 2: Production and acceptance margin-aware targets

**Files:**
- Modify: `afuture/directional_acceptance.py`
- Modify: `afuture/execution_aligned_runtime.py`
- Modify: `tests/test_directional_acceptance.py`
- Modify: `tests/test_execution_aligned_runtime.py`

**Interfaces:**
- Acceptance derives per-lot margin as `price * frozen_multiplier * margin_rate_proxy * margin_estimate_buffer` and budgets total target margin against `equity * min(max_margin_ratio, 1-min_available_ratio)`.
- Live runtime derives per-lot margin from the selected contract's current mid, multiplier and side-specific live `ContractSpec.margin_rate_long/short`, multiplied by the existing `RiskConfig.margin_estimate_buffer`.
- Existing `RiskManager.check_open_orders()` remains unchanged and is still authoritative.

- [ ] **Step 1: Add RED acceptance test** showing a 2.0x Stress-like target at 15% proxy/1.25 buffer is reduced from 20 lots to a feasible integer target instead of being generated above the 35% cap.
- [ ] **Step 2: Add RED runtime test** showing a 2.0x requested live target is pre-fitted to live margin capacity while never increasing requested lots.
- [ ] **Step 3: Run those exact tests and confirm expected failures.**
- [ ] **Step 4: Integrate the shared primitive into acceptance `target_lots()` and live `maybe_rebalance()`.**
- [ ] **Step 5: Run directional acceptance/runtime tests plus compileall; require green.**
- [ ] **Step 6: Commit the milestone.**

### Task 3: Cost-robust frozen meta score

**Files:**
- Modify: `afuture/execution_aligned_policy.py`
- Modify: `tests/test_execution_aligned_policy.py`
- Modify: `tools/evaluate_execution_aligned_target.py` only if report metadata/gates need synchronization.

**Interfaces:**
- Keep `META_LOOKBACK=11`, `META_REBALANCE=3`, `META_COUNT=3`, 96 template IDs and 2.0x cap.
- Add Stress endpoint cost constant 15bp.
- For each template compute Base and Stress causal intraday streams from the same weight path.
- Compute the existing trailing annualized/Sharpe score separately; robust score is the equal-weight mean where both endpoint scores are finite, otherwise NaN.
- Update `META_SCORE_SOURCE` to identify the dual-cost evidence.

- [ ] **Step 1: Add RED unit tests** proving a template whose high Base score collapses under Stress ranks below a template that survives both endpoints, and proving the frozen meta shape remains 11/3/3/96.
- [ ] **Step 2: Run only policy tests and verify RED.**
- [ ] **Step 3: Implement dual-endpoint streams and robust score without introducing tunable search parameters.**
- [ ] **Step 4: Run policy/L4 parity tests and compileall; require green.**
- [ ] **Step 5: Commit the milestone.**

### Task 4: Fixed-data L3 economic verification

**Files:**
- Modify: `tools/evaluate_directional_production_mechanics.py` for explicit margin-aware metadata if needed.
- Temporary: `.github/workflows/stress-robustness-targeted.yml` may gain a fixed-artifact economic job.

**Interfaces:**
- Inputs remain the fixed specific-contract and broad continuous daily artifacts used by prior L3.
- Base remains 5bp/12% proxy; Stress remains 15bp/15% proxy; buffer 1.25; no parameter search.

- [ ] **Step 1: Run L2 directional smoke before economic evaluation.**
- [ ] **Step 2: Download fixed input artifact in GitHub Actions and run `tools/evaluate_directional_production_mechanics.py`.**
- [ ] **Step 3: Record Base/Stress annualized return, total return, max DD, active days, margin rejects, HALT, realized gross and turnover.**
- [ ] **Step 4: Assert hard gates are unchanged and Base remains >=100% before accepting any candidate.**
- [ ] **Step 5: If Stress is <80%, inspect only attributable evidence (margin feasibility, turnover/cost sensitivity, robust selection) and allow at most bounded causal refinements; do not broaden into parameter sweeping.**
- [ ] **Step 6: Freeze the best causally justified candidate and commit.**

### Task 5: Documentation, repository gate and main merge

**Files:**
- Update: `README.md`
- Update: `docs/archive/evidence/return-target-100-evidence.md`
- Update: `docs/archive/evidence/directional-production-mechanics-evidence.md`
- Update other directional production docs only where semantics changed.
- Restore: `.github/workflows/ci.yml`
- Delete: `.github/workflows/stress-robustness-targeted.yml`

- [ ] **Step 1: Synchronize docs to the actual final metrics and explicitly state whether Stress >=80% was achieved.**
- [ ] **Step 2: Remove temporary branch-only workflow and restore normal full CI behavior.**
- [ ] **Step 3: Run one final directional L2 smoke if cleanup affected code/config; otherwise do not repeat economic L3.**
- [ ] **Step 4: Open PR to `main`; let normal Python 3.10/3.13 full CI run once.**
- [ ] **Step 5: Inspect CI evidence; fix only genuine failures and rerun affected scope as required.**
- [ ] **Step 6: Squash-merge after green CI and verify `main` points at the merge SHA.**
