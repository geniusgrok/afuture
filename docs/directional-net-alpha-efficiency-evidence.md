# Directional Net-Alpha Efficiency Evidence

This document accumulates fixed evidence for the post-PR #14 net-alpha research phase. It is updated only with reproducible observations. Future-label diagnostics are never production inputs.

## Baseline and lineage

Remote `main` was confirmed at PR #14 merge `711633548af350155ede84e6bbd45e15447a5398`. The frozen Production L3 input remains artifact `9473260618`; PR #14 final output is workflow `32634296589`, artifact `9491959916`.

The archived Float Stress **58.1372%** is selection-biased evidence from an older float-notional lineage. PR #14's current execution-aligned weight lineage reports Float Stress **109.3145%**. They are not the same weight path, so a mathematically additive `58.1372% -> 28.9559%` component decomposition would be false precision. The final bridge therefore separates the archived headline gap from same-lineage production attribution/counterfactuals.

## Task 1 — Production PnL / turnover / capacity attribution

Behavior-neutral audit instrumentation was committed as `5020a7e0c2fa9a38bb65ce5b37c22508216da648`. Fixed principal-window L3 workflow `32642547947` then reproduced the PR #14 economics exactly while asserting event/PnL/cost reconciliation. Evidence artifact: `9494018121` (`sha256:1cf354bef01cd4e81d539404ec5d35876fd1e25896c4cdd825cbb7bb581627c7`).

| Metric | Base | Stress 15bp / 15% margin proxy |
|---|---:|---:|
| Annualized return | 109.0636% | 28.9559% |
| Total return | 312.2285% | 62.9735% |
| Max drawdown | 15.8529% | 28.1152% |
| Sharpe | 2.0976 | 0.9604 |
| Active days | 478 / 484 | 474 / 484 |
| Peak realized gross | 1.998253x | 1.668769x |
| Margin rejects | 0 | 0 |
| Permanent HALT | false | false |

Stress gross signal PnL is **700,245.00** on initial equity 500,000.00: long contribution **460,515.00**, short contribution **239,730.00**. Stress turnover is **256,918,290.00** and 15bp one-way transaction cost is **385,377.435**. Thus the audited cash reconciliation is `500,000 + 700,245 - 385,377.435 = 814,867.565`, equal to final Production Stress equity apart from display rounding.

### Stress turnover and cost attribution

| Action | Events | Affected days | Turnover | 15bp cost | Annualized drag proxy |
|---|---:|---:|---:|---:|---:|
| Entry | 415 | 238 | 97,057,695 | 145,586.5425 | 19.8080 pp |
| Exit | 411 | 227 | 92,862,545 | 139,293.8175 | 19.0010 pp |
| Resize | 264 | 206 | 51,722,155 | 77,583.2325 | 10.0825 pp |
| Reversal | 54 | 25 | 11,314,145 | 16,971.2175 | 2.0903 pp |
| Roll | 12 | 6 | 2,521,010 | 3,781.5150 | 0.4757 pp |
| Daily circuit | 2 | 2 | 1,440,740 | 2,161.1100 | 0.2707 pp |

The annualized-drag values are pathwise proxies, not additive counterfactual returns: changing one component would change future equity, integer lots and risk state.

### Stress capacity observations

- Average raw target gross: **1.928742x**; average completed-return-governor target gross: **1.715932x**; average realized gross: **1.175349x**.
- Governor affected **70** days and removed **103.0 gross-ratio-days** of target exposure.
- Integer-rounding tracking shortfall: **86,822,426.49 notional-days** across 483 days.
- Margin-fit tracking shortfall: **39,374,570.00 notional-days** across 264 days.
- Same-sign one-lot stabilization tracking shortfall: **1,751,885.00 notional-days** across 28 days.
- Max-35-lot clipping: **6,215,900.00 notional-days** across 7 days.
- Unavailable-contract tracking shortfall: **2,580,836.72 notional-days** across 11 days.

These quantities are exposure tracking losses accumulated by day; they are not dollars of PnL and must not be summed as if they were cash losses.

## Task 2 — Entry / exit quality diagnostics

`afuture.directional_entry_diagnostics` uses strict naming and causality boundaries: `feature_*` columns use only completed history before the event date; `label_*` columns may use future 1/3/5/10-session data and are research-only. Production modules do not import this analyzer.

On the fixed full-recent Stress event ledger there are 442 entry-role and 438 exit-role events after including reversal opening/closing legs. The strict Task 1 transaction buckets remain 415 entry and 411 exit events; the difference is only diagnostic role classification of reversal legs.

Future-only labels identify **40 false-breakout entries** (9,707,355 turnover) and **52 temporary-displacement exits** (16,755,545 turnover), but those labels cannot be used before the trade. Meta/top-k causation cannot be reliably reconstructed from the event artifact and is explicitly marked unavailable rather than guessed.

### Pre-trade entry cohorts — 5-session 15bp net return, turnover weighted

| Causal cohort | prior1 | prior2 | train | validation | OOS | Decision |
|---|---:|---:|---:|---:|---:|---|
| Rapid same-side re-entry | -0.3232% | +0.1535% | +0.1886% | +2.7871% | -0.1479% | unstable; do not suppress |
| Fresh entry | -1.8685% | -0.0620% | -0.3379% | -0.3732% | +0.6301% | OOS sign reversal; reject filter |
| 20-session trend-aligned | -2.1649% | +0.2656% | -0.5077% | +0.9282% | +0.0388% | unstable |
| 20-session countertrend | -1.0203% | -0.6254% | +0.2002% | -0.2271% | +1.1736% | unstable |

Full-recent rapid re-entry is especially important negative evidence against hysteresis: 82 events / 22,052,305 turnover have roughly **+1.1214%** 5-session turnover-weighted net return after the 30bp round-trip hurdle. Treating rapid re-entry as churn would delete real Alpha in this sample.

### Pre-exit completed holding age — 5-session continuation after exit

Positive values mean the exited same-side exposure would have earned positive 5-session net return if mechanically retained; this is an opportunity-cost diagnostic, not permission to block exits.

| Exit cohort | prior1 | prior2 | train | validation | OOS | Decision |
|---|---:|---:|---:|---:|---:|---|
| Holding age <=3 sessions | -0.8846% | -0.1504% | -0.3267% | +0.4756% | -0.1458% | mostly useful exits; not a persistence rule |
| Holding age >3 sessions | +0.4655% | **-0.7139%** | +0.3835% | +1.6613% | +0.4793% | prior2 fails; reject exit hysteresis |

### Task 2 conclusion

No low-degree-of-freedom, pre-trade-identifiable entry/exit cohort is consistently negative after 15bp across prior1, prior2, train, validation and OOS. Therefore Task 2 promotes **no** entry/exit hysteresis, persistence or minimum-hold production rule. This is negative evidence, not a failed task: it prevents reintroducing the same class of overfit turnover suppression that PR #14 already rejected.

Task 3 may still research a generic completed-history expected-net-edge estimator, but it must fail closed on insufficient evidence and must demonstrate independent prior/walk-forward value before any production promotion.

## Task 3 — Net-edge-aware entry qualification

The single pre-declared estimator was tested before any production integration:

- key: product + direction + whether completed 20-session directional trend exceeds the deterministic 30bp round-trip cost;
- outcome horizon: 5 sessions;
- only prior entries whose full 5-session outcome had completed before the current decision were eligible history;
- expected gross edge: empirical mean shrunk toward zero by one zero-valued prior observation;
- expected net edge: shrunk gross edge - 30bp round-trip cost;
- capacity penalty was set to zero for the first screen, giving the candidate its least restrictive test. A positive penalty cannot repair a sign reversal in realized separation.

| Window | Qualified turnover share | Qualified realized net h5 | Rejected realized net h5 | Separation |
|---|---:|---:|---:|---:|
| prior1 | 0.39% | +2.0120% | -1.5864% | +3.5984% |
| prior2 | 28.43% | **-1.2529%** | **+0.4600%** | **-1.7128%** |
| train | 32.01% | -0.0134% | -0.3600% | +0.3466% |
| validation | 35.48% | +2.1307% | -0.1307% | +2.2614% |
| OOS | 27.84% | +1.3962% | +0.1334% | +1.2628% |

The estimator fails the prior2 robustness requirement: the group it would admit is materially worse than the group it would reject. Broader side/bucket and bucket-only aggregation were also probed once as structural simplifications; they still fail in prior2 and/or validation. No threshold grid, minimum-count search or bucket search was performed.

**Decision:** rejected before production integration. No live/acceptance behavior changes and no fixed Production L3 run are justified for this candidate. This avoids spending L3 on a candidate already invalidated by cheaper independent evidence.

## Task 4 — Independent low-turnover Alpha-family research

The research execution harness was first validated against the frozen PR #14 execution-aligned weights: on the same concrete-contract, previous-completed-contract / next-open path it reproduces current-lineage Float Stress **109.3145% annualized**, so the following failures are not an execution-harness artifact.

No parameter grid was run. Each family used one pre-declared, low-degree-of-freedom specification: slow/confirmed trend used 20/60/120-session completed momentum and 5-session rebalance; cross-sectional momentum used 60-session ranks / top-bottom 20% / 5-session rebalance; carry used point-in-time nearest two eligible contracts / top-bottom 20% / 5-session rebalance; carry+trend added one 60-session completed trend confirmation; breakout confirmation used 60-session entry / 20-session exit / 20-session range confirmation. A final volatility-normalized trend-strength rank used the same 20/60/120 horizons, 5-session rebalance and top 20% absolute completed trend strength. All gross targets remained <=2x.

### 15bp roll-safe next-open annualized return

| Family | prior1 | prior2 | train | validation | OOS | full_recent | full_recent DD | Decision |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Slow multi-horizon trend | -6.19% | -14.58% | -24.54% | +6.05% | -6.57% | -13.38% | -28.71% | reject |
| All-horizon confirmed trend | -8.93% | -7.13% | -22.72% | +22.00% | -8.73% | -9.80% | -30.43% | reject |
| Cross-sectional momentum | -6.01% | -18.62% | -33.58% | +26.91% | -13.72% | -16.77% | -35.05% | reject |
| Breakout + range/trend confirmation | -15.61% | +2.23% | -37.02% | +26.01% | +4.34% | -15.09% | -42.22% | reject |
| Point-in-time carry | -4.29% | +2.17% | -6.82% | -11.06% | +16.26% | -2.55% | -23.75% | reject |
| Carry + trend confirmation | -10.41% | +1.06% | -17.96% | -13.75% | +9.81% | -10.55% | -25.98% | reject |
| Strength-ranked normalized trend | -0.45% | -20.66% | -32.48% | +45.56% | -43.20% | -21.95% | -48.22% | reject |

The family results are regime-fragile rather than merely cost-sensitive: several show strong validation-only performance while losing in prior windows and OOS, the exact pattern that would invite selection fitting if parameters were now tuned around the observed two-year window. Carry is the closest to a distinct economic source but is still negative in prior1/train/validation and negative full_recent after 15bp.

**Decision:** no new Alpha family is promoted. The stop rule is invoked here: further lookback/rebalance/fraction variants would add selection degrees of freedom without new economic information. Because no independent family passes, Task 5 does not blend rejected families into the production Alpha merely to improve the observed sample.
