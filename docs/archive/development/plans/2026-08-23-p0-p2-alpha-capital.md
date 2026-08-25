# P0-P2 Alpha and Risk-Capital Efficiency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute all P0-P2 research/engineering directions while promoting only robust candidates that improve net Alpha per unit turnover/risk capital under unchanged hard gates.

**Architecture:** Separate deterministic production primitives from research-only evaluators. P0 modifies allocation only after TDD and L3 evidence; P1/P2 first build causal evidence/data and only integrate production behavior when independent gates pass.

**Tech Stack:** Python 3.10/3.13, pandas, numpy, pytest, akshare, GitHub Actions.

**Spec:** `docs/archive/development/specs/2026-08-23-p0-p2-alpha-capital-design.md`

## Global Constraints
- target/realized gross <= 2.0x; max margin 35%; min available 25%; daily loss 5%; total drawdown 30%; max 35 lots.
- Broker/CTP truth, reduction-first, daily circuit, hard/manual halt, fail-closed and governor authority unchanged.
- 15bp/15% Stress is primary economic gate; no cost/margin relabeling.
- No parameter grids on the repeatedly observed 2024-08-21..2026-08-20 window.
- Future labels may diagnose but never enter production decisions.
- Every behavior modification is RED -> GREEN -> affected L2; L3 only for plausible candidates; one final L4.

---

### Task 1 — P0-1 integer risk-capital projection
**Disposition:** REJECT — fixed Stress 17.9238%.
**Files:** create `afuture/directional_projection.py`; modify `afuture/directional.py`, `afuture/directional_robustness.py`; add `tests/test_directional_projection.py`.
**Interface:** `optimize_integer_projection(requested_lots, lot_notionals, per_lot_margin, margin_budget, current_lots=None, fallback_lots=None) -> dict[str,int]`.
- [ ] RED: prove same-sign/non-expansion, budget, improved squared-notional tracking versus proportional fitting, deterministic tie-break and turnover non-increase fallback.
- [ ] GREEN: greedy marginal tracking-error allocator with existing fitter fallback when turnover would increase.
- [ ] L2: directional projection/stress/runtime/acceptance tests + compile.
- [ ] Research screen and fixed L3 only if cheaper evidence improves tracking without hidden turnover.
- [ ] Commit/push milestone.

### Task 2 — P0-2 selected-template consensus
**Disposition:** REJECT — full_recent -4.3139pp proxy.
**Files:** modify `afuture/execution_aligned_policy.py`; create `afuture/directional_confidence.py`; modify projection/runtime/acceptance only if promoted; add policy/confidence tests.
**Interface:** policy audit returns frozen weights plus completed-history `consensus[date,product] in [0,1]`; projection accepts optional non-negative priority weights.
- [ ] RED: consensus uses only selected templates/current timestamp, is invariant to future-bar edits, and cannot create/sign-flip exposure.
- [ ] GREEN: expose consensus and priority-aware capacity allocation without changing unconstrained targets.
- [ ] Multi-window screen; L3 only if Stress materially improves and Base/hard gates survive.
- [ ] Commit/push accepted behavior or negative-evidence-only milestone.

### Task 3 — P1-1 regime-aware new-risk scalar
**Disposition:** REJECT — full_recent -8.6201pp proxy.
**Files:** create `afuture/directional_regime.py`, research evaluator, tests; integrate policy/runtime only if promoted.
**Interface:** `completed_regime_scale(close_history, weight_history, consensus_history) -> float` with result in `(0,1]` and no future data.
- [ ] RED causality/bounds/missing-data fail-closed tests.
- [ ] Implement predeclared breadth/dispersion/correlation/volatility formula.
- [ ] Walk-forward/prior/regime/cost screen; promote only with stable gain.

### Task 4 — P1-2 futures-specific Alpha from existing daily evidence
**Disposition:** REJECT — all predeclared daily families unstable/negative.
**Files:** create `afuture/futures_alpha_research.py`, `tools/evaluate_futures_alpha_families.py`, tests/evidence.
- [ ] Implement fixed price×OI/volume confirmation and predeclared curve/relative-value features with strict lagging using existing daily concrete-contract data.
- [ ] Evaluate independently across available prior/train/validation/OOS, leave-period/product and 15/20bp cost slices.
- [ ] If multiple families pass, combine only via capped equal-risk/equal-weight rules; otherwise no combination.
- [ ] L3 only for a final robust candidate.

### Task 5 — P2-1 bounded liquidity-governed universe expansion
**Disposition:** REJECT — no bounded root passes cross-window gate.
**Files:** create `afuture/directional_universe.py`, evaluator/tests; modify production universe only if promoted.
- [ ] Discover a bounded set of provider roots using daily completed OI/volume/history/metadata evidence; no multi-year minute warehouse.
- [ ] Freeze candidate list before final evaluation; require robustness and concentration controls on available evidence.
- [ ] Promote only if Base/Stress/common hard gates improve without selection concentration.

### Task 6 — P2-2 directional execution planner
**Disposition:** PROMOTE — depth-aware opening price only.
**Files:** create `afuture/directional_execution.py`; modify `afuture/directional_runtime.py`; add runtime/execution tests.
- [ ] RED tests for reduction-first, limit/quote bounds, deterministic slicing, no extra gross/order-rate risk and fail-closed stale/depth handling.
- [ ] Implement execution plan primitives and execution-quality attribution.
- [ ] Validate live-mechanics semantics; fixed Stress headline is unchanged unless execution timing Alpha is explicitly modeled.

### Task 7 — Final evidence and integration
**Files:** README, architecture/data/live/production/research evidence, new final report; remove temporary workflows/scripts not part of retained capability.
- [ ] Reconcile every accepted/rejected P0-P2 item and Before/After metrics.
- [ ] Restore standard `ci.yml`; remove source-export/temporary economic workflows.
- [ ] Run final fixed L3 for retained economic behavior, then one full L4 repository matrix.
- [ ] Review diff, update PR, mark ready, squash merge to `main`, verify main SHA.
