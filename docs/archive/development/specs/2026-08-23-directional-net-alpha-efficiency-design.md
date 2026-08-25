# Directional Net-Alpha Efficiency Design

> **归档说明：** 本文是已完成阶段的设计记录，不描述当前系统；当前事实见 [`documentation-index.md`](../../../documentation-index.md)。

## Goal

Improve realizable Stress net alpha for the frozen Execution-Aligned Directional Portfolio without relaxing any hard risk gate, increasing leverage above 2x, or fitting the repeatedly observed 2024-08-21..2026-08-20 window.

## Baseline and evidence status

Remote main is `711633548af350155ede84e6bbd45e15447a5398` (PR #14). Fixed Production L3 input artifact is `9473260618`; final baseline output artifact is `9491959916` from run `32634296589`.

The documented Float L4 Stress 58.1372% is archived research evidence from an earlier frozen float path. Re-running the current post-PR#14 policy through `evaluate_execution_aligned_target.py` is a different weight path; therefore Task 1 must explicitly separate archived-float, current-float, and production-mechanics semantics instead of treating 58.1372% as automatically lineage-identical to the current Production 28.9559% path.

## Hard constraints

- target gross <= 2.0x and realized gross <= 2.0x;
- max margin ratio 35%, min available ratio 25%;
- max daily loss 5%, max total drawdown 30%;
- max contract volume 35;
- Broker/CTP remains the only live account/order/fill/position truth;
- reduction-first, daily circuit, hard/manual halt and fail-closed semantics unchanged;
- completed-return governor can only reduce risk;
- no second account/risk state machine;
- no current/future outcome in production decisions;
- frozen 50-product/96-template/meta 11/3/3 configuration remains unchanged unless independent evidence justifies a separate future change.

## Architecture

### 1. Evidence-first production attribution

Extend `DirectionalProductionAcceptance` with audit-only daily/event records. Each rebalance day records raw product weights and gross, governor-scaled weights/gross, pre-margin integer target lots/gross, post-margin target lots/gross, unavailable-product retention, executed normal deltas, transaction costs, intraday PnL, circuit/gross-guard reductions and final realized gross/margin. Product-level event rows carry product, symbol, previous/target/executed lots, event type and turnover notional.

No audit field participates in target construction. Existing result/equity must remain byte-for-byte/economically equivalent within floating precision.

`tools/evaluate_directional_production_mechanics.py` converts the audit trail into a quantitative bridge and product/event attribution. Capacity effects are measured with one-at-a-time deterministic counterfactual simulators that change only the named mechanic while retaining the same frozen weight path. The report clearly labels non-additive annualized-return impact proxies rather than pretending compounded counterfactual effects are exactly additive.

### 2. Offline entry/exit diagnostics

A research-only analyzer reconstructs entry/exit/re-entry episodes from production event rows and concrete contract returns. Future 1/3/5/10-session returns, MFE/MAE and re-entry outcomes are labels only. Causal features are snapshotted at decision time from completed history (signal weight, prior weight, trailing product return/volatility, selected-template support where available, transaction-cost hurdle).

The analyzer groups low-quality candidates only when a pre-trade causal predicate exists and is stable across prior/train/validation/OOS slices. If no stable predicate exists, the result is explicit negative evidence.

### 3. Net-edge-aware entry qualification

Production gating is entry-only. Reductions, exits, reversals, circuits, gross guards and hard-risk actions always bypass it.

Expected edge must be derived only from completed observations. The first candidate is intentionally low-degree-of-freedom: a completed-history empirical edge estimate for the same product/direction signal-strength bucket with shrinkage toward zero, compared with a deterministic round-trip hurdle derived from `2 * stress_one_way_cost` plus an execution/capacity penalty. Insufficient history => no new risk (fail-closed for the optional new entry); existing risk reductions are never blocked.

Promotion requires material Stress improvement, Base >=100%, Stress DD <=30%, no permanent HALT, no leverage/risk-gate change and no severe prior/regime deterioration.

### 4. Distinct Alpha families

Research families are added in a separate module and evaluated independently before any production integration:

- slow/medium multi-horizon trend with volatility normalization;
- cross-sectional relative strength within the frozen universe;
- volatility/range-expansion confirmation for trend entries;
- carry only if point-in-time nearby/deferred contract data is demonstrably available. If not, record the data gap and do not synthesize it.

Parameters are structural calendar horizons and simple equal-risk/capped weighting, not a grid search. Families are evaluated over available 2022-2026 history with rolling windows, leave-one-period/product tests, regime slices, 5/15/30bp costs, margin stress and neighboring structural horizons.

### 5. Multi-alpha combination and capacity

Only independently passing families may be combined. Weighting uses equal-risk/capped equal weighting or completed-history contribution with explicit caps; no broad combination grid search. Margin capacity is reviewed last using completed adverse-move evidence; 35%/25%/5%/30% hard gates remain unchanged and any soft-envelope change may only be justified by a documented shock model.

## Verification

- L1: new/affected tests + compileall after small edits.
- L2: directional policy/runtime/activity/acceptance/risk/quality/restart suites at meaningful milestones.
- L3: fixed artifact `9473260618` only for economically meaningful candidates.
- L4: one complete repository/acceptance/robustness gate on the final stable candidate; repeat only after material behavior changes or substantive fixes.

## Stop rule

Stop when economically justified directions have been evaluated and further improvement would require extra parameter freedom or same-history selection fitting. Prefer a lower but stable Stress return over a fragile 80% print.

## Final disposition

Implementation/research completed with no promoted economic-behavior change. Behavior-neutral attribution and offline entry/exit diagnostics are retained. The completed-history net-edge candidate failed prior2; all pre-declared new Alpha families failed cross-window 15bp evidence; the shock-derived margin candidate produced 4.7970% Stress annualized and permanent HALT. The stop rule therefore preserves PR #14 production economics and forbids additional same-history threshold/search iterations.
