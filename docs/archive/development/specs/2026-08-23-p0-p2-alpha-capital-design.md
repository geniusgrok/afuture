# P0-P2 Alpha and Risk-Capital Efficiency Design

> **归档说明：** 本文是已完成阶段的设计记录，不描述当前系统；当前事实见 [`documentation-index.md`](../../../documentation-index.md)。

## Goal
Raise execution-aligned directional Stress net return without relaxing the fixed hard-risk gates, increasing leverage above 2x, hiding transaction costs, or fitting the repeatedly observed 2024-08-21..2026-08-20 window.

## Fixed production invariants
- Target and realized gross <= 2.0x.
- Hard margin <= 35%; available >= 25%.
- Daily loss 5%; total drawdown 30%; max contract volume 35.
- Broker/CTP remains sole account/order/fill/position truth.
- Reduction-first, daily circuit, hard/manual halt and fail-closed behavior are unchanged.
- Completed-return governor can only reduce risk.
- Frozen 50-product/96-template policy remains the baseline; a candidate changes it only after independent evidence passes.
- 15bp one-way Stress and 15% margin proxy remain primary promotion evidence.

## Architecture
### P0-1 Risk-capital-aware integer projection
Replace only the margin-fit projection algorithm with a deterministic integer allocator that never increases any requested absolute lot, never changes sign, never exceeds the existing soft margin budget, and minimizes notional tracking error. Current positions are used only as a tie-break and as a hard non-increase turnover guard versus the existing proportional fitter. Risk actions remain outside this optimizer.

### P0-2 Template consensus capacity allocation
Expose per-product agreement from the already-selected META_COUNT=3 templates without using future returns. Consensus is the absolute signed-vote agreement among selected templates. It does not create new Alpha or suppress risk reductions; it is used only when capacity prevents full target realization, to prioritize limited integer/margin capacity toward higher-agreement existing signals. No threshold grid is allowed.

### P1-1 Regime-aware allocation
Build a completed-history market-quality scalar from low-degree-of-freedom observables: trend breadth, cross-sectional dispersion, average correlation, realized volatility and template consensus. The scalar may only reduce newly requested risk and must not affect reductions/reversals/hard-risk actions. The formula is predeclared before economic evaluation.

### P1-2 Futures-specific Alpha from existing daily evidence
Research only economically distinct families that can be built from the repository's already-available point-in-time daily concrete-contract evidence: price×OI/volume confirmation and predeclared curve/relative-value sleeves. Do not build a multi-year minute/session data warehouse in this phase. Each family is evaluated independently across existing prior/train/validation/OOS/cost slices before any production combination. No large parameter grid.

### P2-1 High-liquidity universe expansion
Discover a bounded set of additional exchange-listed roots using daily completed liquidity evidence only; do not build a long-history/minute warehouse. Candidates must meet history/OI/volume/contract metadata coverage and pass leave-one-product/period robustness on the available research window. Promotion cannot depend on a single new product.

### P2-2 Execution optimization
Improve the live directional execution planner with a deterministic opening-price primitive that can reduce expected slippage when displayed L1 depth covers the full order while preserving FAK/reduction-first/risk semantics. Fixed 15bp Stress return is not credited for lower live slippage unless timing changes gross Alpha; execution-quality evidence is reported separately.

## Promotion logic
Each milestone is independently accepted or rejected. P0/P1/P2 candidates enter fixed Production L3 only after targeted tests and cheaper multi-window evidence show a plausible positive effect. A rejected candidate is removed from production behavior and retained as negative evidence. Failed Alpha families are never combined merely to improve a headline number.

## Verification
L1 targeted tests/compile per change; L2 affected subsystem tests per milestone; L3 fixed production economics only for meaningful candidates; L4 full repository CI/acceptance/robustness once on the final stable candidate.
