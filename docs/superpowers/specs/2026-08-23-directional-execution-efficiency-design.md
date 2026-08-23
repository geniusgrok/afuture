# Directional Execution Efficiency Design

Date: 2026-08-23

## Objective

Raise the production-mechanics Stress result materially toward the 80% annualized research target by removing economically low-value turnover and recovering safe margin capacity, without relaxing hard risk gates, increasing leverage, expanding the frozen template pool, or hiding regime/generalization failures.

The target is directional: 80% is a research objective, not permission to fit the repeatedly observed two-year window until it prints 80%.

## Non-negotiable constraints

- Frozen universe: 50 mature Chinese commodity futures products.
- Frozen template pool: 96 templates.
- Meta shape remains lookback 11 / rebalance 3 / active templates 3.
- Signal and meta remain causal and use completed history only.
- Target and realized gross hard ceiling remain 2.0x.
- Max contract volume remains 35.
- Max margin remains 35%; minimum available remains 25%.
- Daily loss remains 5%; total drawdown remains 30%.
- Hard risk reductions, reversals, daily circuit, gross guard, fail-closed paths and restart reconciliation must never be suppressed by turnover controls.
- Base full_recent annualized return must remain >=100% for a promotion candidate.
- Stress full_recent must remain no-HALT with max drawdown <=30%.
- No high-dimensional parameter search on the already-observed two-year history.

## Architecture

Add a small `directional_efficiency` module containing causal, deterministic execution-efficiency primitives. The production policy remains the only Alpha policy; efficiency code may retain an incumbent decision when the expected completed-history advantage of changing is smaller than the modeled Stress transaction cost, but it may never invent a new signal or increase risk beyond the frozen policy.

Production runtime and production-mechanics acceptance must share the same semantics for lot stabilization, contract-roll hysteresis and adaptive margin sizing. The acceptance path additionally records turnover attribution so each improvement can be tied to a concrete economic loss mechanism.

## 1. Turnover attribution

Extend production-mechanics daily evidence with mutually auditable turnover buckets. At minimum report:

- normal rebalance reductions;
- normal rebalance openings;
- contract-roll turnover;
- same-contract resize turnover;
- signal reversal turnover;
- daily-circuit flatten turnover;
- gross-guard turnover.

Also report signal-layer weight turnover and meta-switch count from the production policy. Every execution bucket must sum back to total `turnover_notional` within floating tolerance. This instrumentation is behavior-neutral and lands first.

## 2. Cost-aware meta hysteresis

At a normal meta rebalance date:

1. Rank all Stress-surviving templates by the existing Base score.
2. Build the normal top-3 candidate set.
3. If there is no incumbent set, select the candidate set.
4. Any incumbent that no longer survives Stress evidence may be replaced immediately.
5. Otherwise compare incumbent and candidate aggregate target weights at the current timestamp.
6. Compute one-way switch cost from L1 weight turnover using `STRESS_COST_BPS`.
7. Compute the candidate expected edge improvement from completed Base template returns only: difference in trailing mean daily return between candidate and incumbent sets, multiplied by `META_REBALANCE` sessions.
8. Switch only when expected edge improvement is strictly greater than modeled switch cost. Otherwise retain the incumbent set.

This preserves Base score ordering, adds no tunable score threshold, and prices switching in the same return units as expected edge.

## 3. Product-level hysteresis and no-trade bands

After template aggregation, stabilize only same-direction magnitude changes. For each product whose previous and candidate weights have the same non-zero sign:

- expected benefit of the incremental weight over the next `META_REBALANCE` sessions is derived from the trailing completed product intraday mean;
- modeled round-trip cost uses `2 * STRESS_COST_BPS` on the incremental absolute weight;
- change magnitude only when expected benefit is strictly greater than modeled round-trip cost;
- otherwise keep the previous product weight.

Sign reversals, exits to zero and new positions are not blocked by this product gate.

At integer-lot execution, suppress a one-lot same-sign resize only when retaining the current lot count still satisfies the soft margin envelope and 2.0x gross limit. Never suppress a risk-driven reduction, a reversal, a roll, a daily circuit action or gross-guard reduction.

## 4. Contract-roll hysteresis

The previous completed trading-day activity snapshot remains the only activity evidence. If the currently held contract for a product is still fully eligible under listing, expiry, volume and open-interest filters, keep it unless the normal leader has both strictly higher open interest and strictly higher volume. If the current contract becomes ineligible, roll immediately according to the existing deterministic ranking.

This adds no fitted ratio threshold and prevents leader flip-flops caused by one liquidity dimension narrowly crossing another.

## 5. Adaptive margin headroom

The 35% margin hard gate and 25% available hard gate remain authoritative. Normal target sizing uses a causal soft envelope derived from completed account-return risk evidence.

Let `hard_share = min(max_margin_ratio, 1-min_available_ratio)`. Let `shock` be the completed-history adverse-move proxy bounded to `[volatility_trigger, max_daily_loss_ratio]`, using the latest absolute completed return and two-day sample volatility. The safe target share is:

`hard_share * (1-max_daily_loss_ratio) / (1+shock)`

and is never allowed below the existing 30% conservative floor or above the hard share. Defensive governor scaling remains authoritative and naturally reduces exposure further.

This formula reserves capacity for a full 5% equity loss plus a completed-evidence mark expansion; it does not modify the hard gate.

## 6. Production parity

- Live runtime uses side-specific `ContractSpec` margin rates and current quotes.
- Historical production-mechanics acceptance keeps explicit 12% Base / 15% Stress margin proxies and the existing 1.25 estimate buffer.
- Policy, live runtime and acceptance must use the same cost-hysteresis and roll semantics where equivalent evidence exists.
- Missing evidence fails closed or falls back to the current conservative behavior; it must never create extra risk.

## 7. Validation and promotion

Use progressive validation.

- L1: new primitive/policy/selector/lot tests, including explicit RED→GREEN evidence.
- L2: affected directional runtime, activity, acceptance, restart and review-regression tests plus compileall.
- L3 after each meaningful economic milestone: fixed artifact `9473260618`; Base 5bp/12% margin and Stress 15bp/15% margin; record turnover attribution and compare to current main.
- Full CI only for the final candidate.

Promotion candidate gates:

- Base annualized >=100%.
- Base max drawdown <=30% and no permanent HALT.
- Stress max drawdown <=30% and no permanent HALT.
- Stress margin rejects remain zero or are strictly explainable without gate relaxation.
- Actual gross <=2.0x.
- All 35%/25%/5%/30%/35-lot hard limits unchanged.
- Turnover reduction must be explained by attribution, not by deleting risk actions.
- Prior/independent windows are reviewed for material negative regression.

If a change raises full_recent Stress but damages Base below 100%, causes hard-gate failures, or materially worsens prior/regime evidence, reject it. If all economically grounded changes are exhausted before 80%, keep the best robust candidate rather than overfit the same history.

## 8. Documentation consistency

Update README, architecture, data/backtest, live-trading, production checklist, production-mechanics evidence and research evidence to match the final code and measured results. Remove obsolete statements that describe the previous 20.4057% candidate as current once a new candidate is promoted. Rejected experiments remain documented as rejected evidence, not production behavior.
