# Directional Execution Efficiency Implementation Plan

> **归档说明：** 本文是已完成阶段的实施记录，不描述当前系统；当前事实见 [`documentation-index.md`](../../../documentation-index.md)。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce economically low-value directional turnover and safely recover margin capacity so Stress annualized return moves materially toward 80% while Base remains >=100% and all production hard gates remain unchanged.

**Architecture:** Keep `ExecutionAlignedAggressivePolicy` as the only Alpha policy. Add deterministic execution-efficiency primitives for cost-aware meta/product hysteresis, lot stabilization and adaptive margin sizing; extend completed-activity selection with incumbent-contract hysteresis; keep live runtime and deterministic production-mechanics acceptance semantically aligned.

**Tech Stack:** Python 3.10/3.13, pandas, numpy, pytest, GitHub Actions.

**Spec:** `docs/archive/development/specs/2026-08-23-directional-execution-efficiency-design.md`

**Final disposition (2026-08-23):** execution completed. Promoted: attribution, roll hysteresis, one-lot increase no-trade and adaptive margin contraction. Rejected/reverted after fixed L3: meta hysteresis, product replacement persistence and same-direction weight hysteresis. Final promoted L3: Base 109.0636%, Stress 28.9559%; 80% not achieved.


## Global Constraints

- Frozen 50-product universe and 96-template pool.
- Meta lookback 11 / rebalance 3 / count 3.
- Target and realized gross <=2.0x.
- Max contract volume 35.
- Max margin 35%; min available 25%.
- Daily loss 5%; total drawdown 30%.
- No risk reduction, reversal, circuit, gross guard or fail-closed action may be suppressed.
- Base full_recent annualized >=100% for promotion.
- Stress max drawdown <=30% and no permanent HALT.
- No high-dimensional parameter search on the repeatedly observed two-year window.
- Final code, comments, README and Markdown evidence must describe the same semantics and numbers.

---

### Task 1: Behavior-neutral turnover attribution

**Files:**
- Create: `afuture/directional_efficiency.py`
- Modify: `afuture/directional_acceptance.py`
- Modify: `afuture/execution_aligned_policy.py`
- Modify: `tools/evaluate_directional_production_mechanics.py`
- Create: `tests/test_directional_execution_efficiency.py`
- Modify: `tests/test_execution_aligned_policy.py`

**Interfaces:**
- Produces `TurnoverAttribution` helpers that classify contract deltas by roll / resize / reversal and aggregate daily execution buckets.
- Produces policy audit columns for signal weight turnover and meta switch count without changing returned weights.

- [ ] Write failing tests proving attribution buckets sum to total turnover and policy audit leaves `weight_history()` byte-for-byte equivalent on the same inputs.
- [ ] Run only the new attribution and policy tests; verify RED.
- [ ] Implement the minimal attribution/audit primitives without changing economic behavior.
- [ ] Run the affected tests plus `compileall`; verify GREEN.
- [ ] Run fixed L3 once and confirm Base/Stress economic metrics are unchanged while attribution appears in the report.
- [ ] Commit and push the milestone.

### Task 2: Cost-aware meta hysteresis

**Files:**
- Modify: `afuture/directional_efficiency.py`
- Modify: `afuture/execution_aligned_policy.py`
- Modify: `tests/test_directional_execution_efficiency.py`
- Modify: `tests/test_execution_aligned_policy.py`

**Interfaces:**
- Add `select_cost_aware_templates(...) -> list[int]` using incumbent set, candidate top-3, completed Base template returns, Stress survival, current template weight paths, `STRESS_COST_BPS`, and `META_REBALANCE`.
- The candidate is accepted only when completed-history expected edge gain over the holding horizon exceeds modeled one-way switch cost; non-surviving incumbents are replaced immediately.

- [ ] Add tests: initial selection matches current top-3; small score improvement with high turnover retains incumbents; large causal edge gain switches; Stress-failed incumbent is evicted; modifying the current/future bar cannot affect the current decision.
- [ ] Run policy tests and verify RED.
- [ ] Implement selection helper and integrate it only at scheduled meta rebalance points.
- [ ] Run affected tests and compileall; verify GREEN.
- [ ] Run fixed L3 and record Base, Stress, DD, turnover, meta switches and prior windows. Reject the milestone if Base <100% or Stress hard gates regress.
- [ ] Commit and push if promoted.

### Task 3: Product hysteresis and integer no-trade band

**Files:**
- Modify: `afuture/directional_efficiency.py`
- Modify: `afuture/execution_aligned_policy.py`
- Modify: `afuture/directional.py`
- Modify: `afuture/execution_aligned_runtime.py`
- Modify: `afuture/directional_robustness.py`
- Modify: `afuture/directional_acceptance.py`
- Modify: `tests/test_directional_execution_efficiency.py`
- Modify: `tests/test_execution_aligned_runtime.py`
- Modify: `tests/test_directional_stress_robustness.py`

**Interfaces:**
- Add `stabilize_same_direction_weights(previous, candidate, trailing_product_returns, horizon, stress_cost_bps)`; only same-sign magnitude changes are suppressible.
- Add `stabilize_one_lot_resize(current_lots, target_lots, lot_notionals, equity, soft_margin_budget, per_lot_margin)`; only a +/-1 same-sign resize may be held and only if current lots remain within soft margin and gross limits.

- [ ] Write RED tests for same-direction cost gate, forced reversal/exit/new position, one-lot churn suppression and hard-risk exceptions.
- [ ] Implement weight stabilization after template aggregation and before final gross assertion.
- [ ] Implement one-lot lot stabilization after margin-aware target sizing; do not apply it to roll/reversal/risk-reduction paths.
- [ ] Run affected policy/runtime/acceptance tests; verify GREEN.
- [ ] Run fixed L3; promote only if Base >=100%, Stress no-HALT/DD<=30%, turnover falls for identifiable resize buckets, and prior windows do not materially deteriorate.
- [ ] Commit and push if promoted.

### Task 4: Completed-activity contract-roll hysteresis

**Files:**
- Modify: `afuture/directional_activity.py`
- Modify: `afuture/execution_aligned_runtime.py`
- Modify: `afuture/directional_acceptance.py`
- Modify: `tests/test_directional_activity.py`
- Modify: `tests/test_directional_trading_day_selection.py`
- Modify: `tests/test_directional_acceptance.py`

**Interfaces:**
- Extend activity selection with optional `preferred_symbols: Mapping[str, str]`.
- If the preferred current contract remains eligible, retain it unless another eligible contract has both strictly higher completed-day open interest and strictly higher completed-day volume; ineligible preferred contracts roll immediately via the existing deterministic ranking.

- [ ] Write RED tests for leader flip-flop retention, dual-dimension dominance roll, expiry/ineligibility forced roll, causal completed-day behavior and deterministic tie ordering.
- [ ] Integrate live preferred symbols from Broker positions and acceptance preferred symbols from current lots.
- [ ] Run activity/runtime/acceptance tests; verify GREEN.
- [ ] Run fixed L3 and attribute the change specifically to roll turnover. Reject if it worsens Base/Stress promotion gates.
- [ ] Commit and push if promoted.

### Task 5: Adaptive causal margin headroom

**Files:**
- Modify: `afuture/directional.py`
- Modify: `afuture/directional_robustness.py`
- Modify: `afuture/execution_aligned_runtime.py`
- Modify: `afuture/directional_acceptance.py`
- Modify: `tests/test_directional_stress_robustness.py`
- Modify: `tests/test_execution_aligned_runtime.py`

**Interfaces:**
- Replace the fixed soft-share helper with `adaptive_margin_sizing_share(...)` using hard margin/cash gates, max daily loss, completed latest return and two-day sample volatility.
- `shock = clamp(max(volatility_trigger, abs(latest_completed_return), sample_volatility), volatility_trigger, max_daily_loss_ratio)`.
- `share = hard_share * (1-max_daily_loss_ratio) / (1+shock)`, clamped to `[0.30, hard_share]`.
- Live uses persisted completed account returns; acceptance uses its `completed_returns` list. Missing evidence falls back to the current conservative 30% share.

- [ ] Write RED tests for no-history 30%, calm-history safe expansion, stressed-history contraction, never-above-hard-share, and parity between live/acceptance calculations.
- [ ] Implement helper and thread completed-return evidence through live and acceptance margin-aware sizing.
- [ ] Run affected tests and compileall; verify GREEN.
- [ ] Run fixed L3. Reject if Stress halts, margin rejects reappear, DD >30% or Base <100%.
- [ ] Commit and push if promoted.

### Task 6: Final economic gate, documentation and repository verification

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/data-and-backtest.md`
- Modify: `docs/live-trading.md`
- Modify: `docs/production-checklist.md`
- Modify: `docs/archive/evidence/directional-production-mechanics-evidence.md`
- Modify: `docs/archive/evidence/research-final-evidence.md`
- Modify: `docs/archive/evidence/return-target-100-evidence.md`
- Modify: tests/docs only as required for consistency.

**Interfaces:**
- Final evidence must include turnover attribution, current Base/Stress metrics, active days, DD, margin rejects, gross peak, HALT state, and explicit comparison with main commit `b6b2cdca0f04193c10e14f8b3ad61902d6e36769`.

- [ ] Run one final fixed L3 on frozen artifact `9473260618`; record all windows and attribution.
- [ ] Review each accepted milestone against Base>=100%, Stress DD<=30%, no-HALT and unchanged hard gates. Revert any unpromoted experiment rather than documenting it as production.
- [ ] Update all code comments and Markdown to the exact final semantics and measured values; search for obsolete `20.4057%`, old margin-share wording and rejected-policy descriptions.
- [ ] Run final Python 3.10/3.13 full repository CI once, including directional production smoke, replay, acceptance, quality and production-mechanics tool tests.
- [ ] Review PR diff for code/comment/doc consistency and accidental scope expansion.
- [ ] Squash merge the green PR into `main` and verify `main` points to the merge SHA.
