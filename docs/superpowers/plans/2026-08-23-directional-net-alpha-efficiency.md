# Directional Net-Alpha Efficiency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Quantify and reduce the realizable Stress net-alpha gap with causal, risk-preserving changes and robust multi-period evidence.

**Architecture:** Add behavior-neutral production audit/event instrumentation first; use it for offline entry/exit diagnostics; only then add an entry-only completed-history net-edge gate if the diagnostics support one. Research distinct Alpha families separately and combine only independently robust families with low-degree-of-freedom weighting. Margin capacity is a final secondary lever.

**Tech Stack:** Python 3.10/3.13, pandas, numpy, pytest, deterministic fixed artifacts, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-23-directional-net-alpha-efficiency-design.md`

## Global Constraints

All hard risk values and production truth/causality rules from the spec are mandatory. The archived 58.1372% Float Stress and current post-PR#14 float path must not be conflated. The 2024-08-21..2026-08-20 window is not pristine OOS.

---

### Task 1: Production PnL / turnover / capacity attribution

**Files:**
- Modify: `afuture/directional_acceptance.py`
- Create: `afuture/directional_attribution.py`
- Modify: `tools/evaluate_directional_production_mechanics.py`
- Create: `tests/test_directional_attribution.py`
- Modify: `tests/test_directional_execution_efficiency.py`

**Interfaces:**
- `ProductionSimulationResult.events: pd.DataFrame` is audit-only.
- `summarize_production_attribution(result, cost_bps) -> dict` returns PnL, turnover, cost, holding-period and product/event summaries.
- `build_capacity_bridge(...) -> dict` reports labeled counterfactual/impact proxies with explicit non-additivity.

- [ ] RED: assert audit/event collection does not change final equity/daily returns and classifies entry/exit/resize/reversal/roll/risk events.
- [ ] RED: assert cost equals turnover notional × one-way cost and product totals reconcile to event totals.
- [ ] GREEN: add audit-only event rows and attribution helper.
- [ ] Run `pytest -q tests/test_directional_attribution.py tests/test_directional_execution_efficiency.py tests/test_directional_acceptance.py` and `compileall`.
- [ ] Run fixed local L3 once for the behavior-neutral milestone and verify baseline Base/Stress metrics are unchanged.
- [ ] Commit/push a meaningful attribution milestone.

### Task 2: Entry / exit quality diagnostics

**Files:**
- Create: `afuture/directional_entry_diagnostics.py`
- Create: `tools/analyze_directional_entry_quality.py`
- Create: `tests/test_directional_entry_diagnostics.py`

**Interfaces:**
- `label_entry_events(events, contract_data, horizons=(1,3,5,10)) -> pd.DataFrame` adds future labels only in the research tool.
- Causal feature columns are named `feature_*`; future labels are named `label_*` and are rejected by production modules.

- [ ] RED: verify forward labels use sessions strictly after the event and changing future rows cannot alter `feature_*` values.
- [ ] RED: verify rapid re-entry, genuine reversal and temporary displacement classifications on synthetic paths.
- [ ] GREEN: implement offline labels and slice summaries.
- [ ] Run only diagnostic tests and compileall.
- [ ] Run diagnostics on fixed artifacts and record whether a stable, pre-trade-identifiable low-quality entry class exists.
- [ ] Commit/push the diagnostic milestone.

### Task 3: Net-edge-aware entry qualification

**Files:**
- Modify: `afuture/directional_efficiency.py`
- Modify: `afuture/execution_aligned_policy.py` or the narrowest production entry-construction boundary supported by diagnostics.
- Modify: `afuture/directional_acceptance.py`
- Modify: `afuture/execution_aligned_runtime.py` only if live parity requires it.
- Create/modify: targeted tests.

**Interfaces:**
- `expected_net_edge(...)` consumes completed-history observations only.
- `qualify_new_entry(...)` can suppress only a new risk-increasing entry; reductions/reversals/risk actions always pass.
- Default hurdle is derived from round-trip Stress cost plus explicit capacity penalty; no history-fitted threshold grid.

- [ ] RED tests for causal history boundary, round-trip break-even hurdle, insufficient-history behavior and mandatory risk-action bypass.
- [ ] GREEN minimal implementation with live/acceptance parity.
- [ ] L1/L2 affected suites.
- [ ] Fixed L3 only if the candidate is economically meaningful; reject/revert if Base <100%, Stress DD >30%, HALT, leverage/risk changes or severe prior deterioration.
- [ ] Commit/push only promoted behavior; retain rejected result as negative evidence, not production code.

### Task 4: Distinct Alpha-family research

**Files:**
- Create: `afuture/directional_alpha_families.py`
- Create: `tools/evaluate_directional_alpha_families.py`
- Create: `tests/test_directional_alpha_families.py`

**Interfaces:**
- Deterministic slow trend, cross-sectional relative-strength and range-expansion confirmation weight paths using completed data.
- Carry constructor requires point-in-time term-structure columns; absent evidence returns an explicit unavailable result.

- [ ] RED causality, gross-cap, product-cap and deterministic tests.
- [ ] GREEN family implementations with structural horizons only.
- [ ] Evaluate prior1/prior2/train/validation/OOS/full_recent, rolling slices, leave-one-product, costs 5/15/30bp and structural-neighbor horizons.
- [ ] Reject families dominated by a single product/period or unstable to neighboring horizons.
- [ ] Commit/push research evidence milestone.

### Task 5: Multi-alpha combination and secondary margin review

**Files:**
- Modify/create the narrow research/production integration modules justified by Task 4 evidence.
- Modify: `afuture/directional.py` / robustness helpers only if the shock model supports a safer soft envelope.
- Add targeted tests.

- [ ] Combine only independent passing families using equal-risk/capped weighting; no combination grid search.
- [ ] RED/GREEN production integration tests, preserving <=2x gross and all hard gates.
- [ ] Evaluate marginal Stress return, DD, turnover, correlation and product concentration.
- [ ] Review margin capacity using completed adverse-move evidence; reject simple soft-share relaxation without shock-model support.
- [ ] Run fixed L3 for each materially distinct promoted candidate only.

### Task 6: Final evidence, cleanup, L4 and merge

**Files:**
- Update README and required docs from the user specification.
- Add: `docs/directional-net-alpha-efficiency-evidence.md`
- Remove temporary source/research workflows and restore standard `ci.yml`.

- [ ] Produce Before/After table and answer all 20 required evidence questions.
- [ ] Synchronize code/docstrings/comments/tests/docs to exact final semantics.
- [ ] Run affected L2 after final behavior change; then one complete L4 on the stable candidate.
- [ ] Review PR diff for scope/causality/risk/documentation consistency.
- [ ] Mark PR ready, require green CI, squash merge to `main`, and verify main SHA/tree.

## Final disposition

Tasks 1–6 were executed. Task 1 attribution was promoted as behavior-neutral instrumentation. Tasks 2–4 produced negative evidence; Task 5 was correctly skipped because no independent family passed; Task 6 margin candidate failed the fixed Stress screen. Final candidate changes no production Alpha/risk behavior. Detailed evidence: `docs/directional-net-alpha-efficiency-evidence.md`.
