# Opportunity-Driven Directional V2 Design

> **归档说明：** 本文是已完成阶段的设计记录，不描述当前系统；当前事实见 [`documentation-index.md`](../../../documentation-index.md)。

Date: 2026-08-24
Baseline: `main@e89ff6c03b9909904ddcc958891cbedad0d30918`

## Goal

Stop treating 15bp Stress annualized return as the optimization target. Keep Stress as a robustness gate and improve the whole directional decision chain by adding an explicit, causal product-opportunity layer between the frozen directional core and portfolio execution.

The production objective is joint: Base return, Stress return, Sharpe, max drawdown, Net Alpha / Turnover, OOS/prior-window stability, product concentration, and unchanged hard-risk safety.

## Current structural gap

The frozen 96-template policy ranks products inside each template mainly by absolute signal strength, then the Meta layer ranks templates globally. This is not an independent product-selection model. The concrete-contract selector only chooses the tradable contract for a product already selected by Alpha; it is not Alpha selection.

## V2 architecture

```text
continuous completed daily evidence
        |
        v
frozen 96-template directional core
        |
        v
raw product target weights
        |
        v
Product Opportunity Selector  <-- new
        |
        v
conservative opportunity tilt <-- new portfolio construction
        |
        v
existing governor / integer lots / margin / risk / execution
```

The first V2 production candidate deliberately does not replace the 96-template core, global Meta, risk state machine, contract-selection semantics, or execution stack. It adds one low-degree-of-freedom product-selection overlay. More aggressive Product x Alpha allocation is research-only unless it independently clears the same gates; previously rejected selected-template consensus allocation is not resurrected.

## Opportunity evidence

All features are computed from completed daily data and shifted by one trading day before affecting target weights. No feature from trading day `t` may influence the weight traded at the open of `t`.

For each product, compute six fixed, directionless opportunity-quality components:

1. 20-session trend efficiency: absolute 20-session log return divided by 20-session sum of absolute daily returns.
2. 60-session trend efficiency: same definition over 60 sessions.
3. 20-session volatility-adjusted strength: absolute 20-session log return divided by 20-session realized volatility times `sqrt(20)`; clip to `[0, 5]`.
4. 20-session breakout-location magnitude: absolute normalized distance from the midpoint of the 20-session close range, scaled to `[0, 1]`.
5. Volume participation: completed volume divided by its 20-session median; clip to `[0, 5]`.
6. Open-interest participation: completed open interest divided by its 20-session median; clip to `[0, 5]`.

Each component is converted to a same-day cross-sectional percentile rank. The Opportunity Score is the equal-weight mean of the six ranks. No fitted factor weights or threshold grid are allowed.

## Selection and portfolio construction

Only products with a non-zero raw core target are eligible. Among eligible products with finite Opportunity Score, the top `ceil(N/2)` are the high-opportunity subset.

The final target is:

```text
final_weight = 0.75 * raw_core_weight + 0.25 * high_opportunity_raw_weight
```

where `high_opportunity_raw_weight` equals the raw core weight for selected products and zero otherwise.

Consequences are intentional:

- a high-opportunity exposure remains at 100% of the raw core weight;
- a lower-ranked exposure is de-emphasized to 75% rather than deleted;
- no new product or opposite-side position can be created;
- gross target can only stay the same or decrease;
- opportunity ranking never reallocates removed risk into another product;
- missing/warm-up evidence fails open to the frozen core only when no active product has usable opportunity evidence.

The 75/25 core-satellite split is frozen for this candidate. It is not a search parameter.

## Live data surface

`SinaContinuousOHLCProvider` becomes a completed-daily OHLCV/OI provider while remaining backward compatible with existing open/close consumers. The returned signal history carries:

- `open`
- `close`
- `volume`
- `open_interest` (mapped from Sina/AKShare `hold`)

The execution-aligned manager passes these panels to the policy. Historical evaluators use the same `broad_daily_universe.csv` fields, preserving backtest/live feature parity. If volume/OI evidence is unavailable, the opportunity overlay must fail open to the raw frozen core rather than invent or forward-fill participation evidence.

## Causality and safety invariants

The V2 overlay must satisfy all of the following mechanically:

- input row `t` cannot affect target row `t`;
- future-row perturbation cannot change earlier opportunity scores or weights;
- sign is preserved for every non-zero raw exposure;
- `abs(final_weight) <= abs(raw_weight)` product by product;
- final gross never exceeds raw gross or 2x;
- zero raw weight always remains zero;
- no change to margin 35%, available 25%, daily loss 5%, total drawdown 30%, max lots 35, reduction-first semantics, circuit/HALT authority, Broker/CTP truth, or depth-aware execution.

## Research evidence before implementation

A behavior-independent local screen on the fixed artifacts used by PR #17 applied this exact six-factor, top-half, 75/25 overlay to the frozen raw weight path. It is selection-biased research evidence, not production proof.

Float next-open proxy, 2024-08-21..2026-08-20:

| Metric | Frozen core | V2 screen |
|---|---:|---:|
| Base annualized | 187.2603% | ~190.2% |
| Stress 15bp annualized | 109.3145% | ~115.7% |
| Stress max DD | 35.35% | ~34.7% |
| Stress Sharpe | 1.5268 | ~1.60 |
| weight-turnover sum | 608.4x | ~570.7x |
| Stress OOS annualized | 100.35% | ~115.4% |

The same screen improved Stress prior2 and train, was roughly flat/slightly weaker on prior1, and slightly reduced the extraordinary validation-window return. These numbers only justify a fixed Production-mechanics L3 attempt; they do not justify promotion by themselves.

## Acceptance gates

The economic behavior is promoted only if one frozen Production-mechanics run shows a joint improvement rather than a headline-only Stress gain.

Required full-recent gates versus PR #17 baseline:

- Base annualized: no material degradation; target is `>= 109.0636%`.
- Stress annualized: `> 28.9559%`.
- Stress Sharpe: `>= 0.9604`.
- Stress max drawdown: no worse than `28.1152%` unless compensated by a clearly superior joint Base/Stress/OOS result and independently documented; default is no worsening.
- Stress turnover notional: preferably below `256,918,290`; Net Alpha / Turnover must improve if turnover rises.
- OOS Base and Stress: no material degradation.
- prior1/prior2/train/validation: no single-window catastrophic deterioration; no tuning after seeing a failed window.
- permanent HALT: false.
- margin rejects: 0.
- realized gross <= 2x; hard margin/available/max-lot gates unchanged.

If these gates fail, the overlay remains research/diagnostic only and Production stays PR #17 exact behavior.

## Files and boundaries

- New `afuture/directional_opportunity.py`: pure feature construction, causal ranking, conservative overlay, audit output.
- Modify `afuture/execution_aligned_policy.py`: apply the overlay after the frozen core aggregation; keep the 96 templates and Meta unchanged.
- Modify `afuture/execution_aligned_runtime.py`: carry completed volume/OI alongside open/close and pass them to the policy.
- Modify `tools/evaluate_execution_aligned_target.py` and exact-lineage/evaluation callers so historical feature inputs match live inputs.
- Add focused unit/causality tests; only after the candidate is stable run fixed L3 and final repository CI.
- Synchronize architecture, backtest, and final evidence documentation.

## Non-goals

- no leverage increase;
- no margin/risk-limit relaxation;
- no new parameter grid;
- no replacement of Broker/CTP account truth;
- no minute/L1 warehouse;
- no reintroduction of rejected cost-aware Meta, consensus allocation, or no-trade candidates;
- no claim that the already-observed 2024-2026 window is pristine OOS.
