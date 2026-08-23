# Product Alpha-Efficiency Selector — Candidate B

Date: 2026-08-24
Baseline: `main@e89ff6c03b9909904ddcc958891cbedad0d30918`
Parent experiment: Opportunity Candidate A, fixed L3 run `32666984798`

## Why Candidate A is rejected

Candidate A used generic directionless price/activity features (20/60 trend efficiency, normalized strength, breakout location, volume participation and open-interest participation) and a frozen top-half 75/25 overlay.

Its fixed Production-mechanics L3 is decisively negative:

- Base annualized: 109.0636% -> 86.5200%
- Stress annualized: 28.9559% -> 16.7075%
- Stress Sharpe: 0.9604 -> 0.6580
- Stress max drawdown: 28.1152% -> 24.7197%
- Stress turnover: 256,918,290 -> 227,088,785
- Stress transaction cost: 385,377.435 -> 340,633.1775
- Stress gross signal PnL: 700,245 -> 513,365
- permanent HALT: false -> false
- margin rejects: 0 -> 0

Thus Candidate A saves about 44.7k of cost but deletes about 186.9k of gross signal PnL. Product attribution shows the largest lost gross contribution is AG (about -136.4k versus the frozen baseline), followed by LU and AL. The failure is therefore an Alpha-selection failure, not an execution-cost failure.

No Candidate-A feature weight, threshold, window, top fraction or 75/25 share is tuned after this result. Candidate A is permanently rejected for Production.

## Candidate B hypothesis

The selector should rank products by **the strategy's own completed, product-level Alpha efficiency**, not by a generic market-shape proxy.

For each product and each completed day `t`, use the raw frozen-core target weight `w[t,p]` and the same continuous-contract open->close return stream already used by the frozen global Meta layer:

```text
intraday[t,p] = close[t,p] / open[t,p] - 1
product_gross_alpha[t,p] = w[t,p] * intraday[t,p]
product_turnover[t,p] = abs(w[t,p] - w[t-1,p])
```

The selection score used on trading day `t` is computed strictly from completed days `< t`:

```text
score[t,p] = cumulative_product_gross_alpha_through_(t-1)
             / cumulative_product_turnover_through_(t-1)
```

No rolling lookback is introduced. The score uses expanding completed history, so Candidate B adds no lookback-search degree of freedom.

For a flat one-way transaction-cost endpoint `c`, cumulative net Alpha per turnover is:

```text
(cumulative_gross_alpha - c * cumulative_turnover) / cumulative_turnover
= cumulative_gross_alpha / cumulative_turnover - c
```

Therefore product ranking is identical for Base and Stress when the cost is a common flat bps endpoint. Candidate B is cost-aware without optimizing the Stress headline itself.

## Frozen portfolio rule

The portfolio rule intentionally reuses Candidate A's already-predeclared overlay so the new experiment changes the selector information source, not the risk transformation:

- only products with a non-zero frozen-core target are eligible;
- active products with finite completed Alpha-efficiency scores are ranked cross-sectionally;
- top `ceil(N/2)` scored active products retain 100% of raw weight;
- lower-half scored active products are held at 75% of raw weight;
- active products without sufficient completed turnover evidence retain 100% of raw weight (fail open);
- zero raw target remains zero;
- sign never changes;
- absolute product weight never exceeds the frozen core;
- total gross can only stay equal or decrease;
- no removed risk is reallocated elsewhere.

The 75/25 share and top-half rule are not tuned using Candidate A's L3 result. They are reused unchanged to isolate whether **production-aligned product information** is materially better than generic price/activity ranking.

## Scope simplification

Candidate B does not require volume or open-interest history for the production decision. If it passes, Candidate A's unused OHLCV/OI production adapter is removed rather than retained as dead complexity. Volume/OI remain available for future research, not as a promoted selector merely because they were implemented.

The frozen 96 templates, global Meta selection, contract selector, governor, integer sizing, margin fit, hard risk gates and execution stack remain unchanged.

## Causality tests

Implementation must prove:

1. modifying open/close on the final row cannot alter that row's Alpha-efficiency score or adjusted target;
2. modifying any future row cannot alter earlier scores/weights;
3. products with zero cumulative prior turnover have no score and are not de-emphasized;
4. no adjusted product magnitude exceeds the raw core magnitude;
5. adjusted gross never exceeds raw gross or 2x;
6. sign and zero-exposure invariants are exact.

## Research and acceptance sequence

1. Targeted unit/causality tests.
2. Cheap fixed roll-safe float screen across prior1/prior2/train/validation/OOS/full_recent at both 5bp and 15bp.
3. Only if the cheap screen is jointly credible, run exactly one fixed Production-mechanics L3 using artifact `9473260618`.
4. No parameter change after the L3 result.

The Production acceptance gates remain the same as the parent design:

- Base annualized >= 109.0636%;
- Stress annualized > 28.9559%;
- Stress Sharpe >= 0.9604;
- default Stress max DD no worse than 28.1152%;
- OOS Base/Stress no material degradation;
- no catastrophic prior-window failure;
- no permanent HALT;
- zero margin rejects;
- realized gross <=2x;
- all existing hard risk limits unchanged.

If Candidate B fails, Production reverts exactly to PR #17. No Candidate C is created by changing Candidate B's share, fraction, minimum observations or history horizon.