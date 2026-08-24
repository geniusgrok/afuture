# Stress90 Final Production Evidence

## Decision

Promote the causal expanding-median leadership freeze as the new Production research checkpoint. It materially exceeds the inherited Stress80 checkpoint, clears the ideal `>=90%` Stress objective, makes both prior windows positive, and leaves live runtime wiring unchanged.

Authoritative parent: `b4207abb50aca1e39d5ebba3affc04765857251a` (PR #24). The fresh matrix was executed from clean promotion commit `01aebdbbce9ded98a48108c7193f028218faef91`; the only subsequent pre-CI changes are evidence documentation and fail-closed matrix-payload validation, not simulation behavior.

## Frozen candidate

The Stress80 target construction is unchanged:

1. 9-product 60m Price × OI confirmation;
2. D -> D+1 causal alignment;
3. entry / same-side increase / reversal confirmation;
4. fixed 20-completed-session cost eligibility;
5. fixed 3-session benefit horizon;
6. fixed 15bp one-way hurdle;
7. tracking-first / turnover-second survivor reallocation.

The promoted response adds one zero-fit causal state:

- Compute standard HHI from absolute current target weights.
- Compare it with the median of all strictly earlier finite target HHI values.
- `current HHI <= prior expanding median` freezes only new product entries and same-sign increases.
- Reductions, exits, reversals and same-product rolls remain executable.
- Inactive initial history passes unchanged.
- The state advances from every causal target day, not candidate holdings or PnL.

The fixed drawdown reserve remains `25% = 30% total hard DD - 5% daily-loss reserve`, now using the full completed causal account-return path. No winning window reaches the 25% soft boundary, so this correctness repair is behavior-neutral for the final matrix while making the documented definition truthful.

Candidate weight SHA256: `8e38dbf6441b561dd1728df08665b94b15cc3358823257505c2fcb9d63f09f28`.

## Immutable inputs

| File | SHA256 |
| --- | --- |
| `broad_daily_universe.csv` | `c1d46bc113a79bd2e000bf5d66ee53c59337eda750b2d3b1409e40f3f4833d0f` |
| `return_target_specific_contracts.csv` | `f8b3f4232cb1bc9eaed65a4401dff6bed121faee064252727202874ad7d53c64` |
| `execution_aligned_weights.csv` | `250a55c1c18ecd3754f489136515275eec3a48d505fd41026d68ab43924e08c1` |
| `prior_two_year_broad_60m.csv` | `3351aa3ae8dec0cf0e9b9a64026181ac8ff2cc22858c442821fe3c94767036b1` |
| `two_year_broad_60m.csv` | `5faf112bb69dd5ddf48ed34419e2046b1c6bdd651b595ca46a46804d8317a27b` |

No spot, inventory, margin, position or transaction history was fabricated.

## Fresh Production matrix

All seven rows are independent account simulations. Base uses 5bp and the frozen Base margin proxy; Stress uses 15bp and the frozen Stress margin proxy.

| Window | Annualized | Max DD | Gross PnL | Cost | Net alpha | Turnover | Net/turn | Gross peak | HALT | Rejects |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| Stress prior1 | 12.141524% | -23.228982% | 122,085.00 | 66,958.75 | 55,126.25 | 44,639,165 | 12.349302 bps | 1.677099x | false | 0 |
| Stress prior2 | 8.578529% | -18.354608% | 111,235.00 | 69,935.80 | 41,299.20 | 46,623,865 | 8.857953 bps | 1.648349x | false | 0 |
| Base full_recent | 156.881655% | -15.708467% | 2,693,765.00 | 132,381.86 | 2,561,383.14 | 264,763,725 | 96.742223 bps | 1.983123x | false | 0 |
| Stress train | 28.891985% | -13.657897% | 229,025.00 | 91,023.25 | 138,001.75 | 60,682,165 | 22.741732 bps | 1.648285x | false | 0 |
| Stress validation | 512.267292% | -11.783634% | 726,940.00 | 50,469.45 | 676,470.55 | 33,646,300 | 201.053474 bps | 1.626864x | false | 0 |
| Stress OOS | 102.808956% | -17.632605% | 268,380.00 | 62,293.73 | 206,086.28 | 41,529,150 | 49.624487 bps | 1.649642x | false | 0 |
| Stress full_recent | 112.100053% | -14.567214% | 1,929,280.00 | 310,257.23 | 1,619,022.78 | 206,838,150 | 78.274862 bps | 1.670510x | false | 0 |

The assembled promotion gate returns `passed=true` with an empty reason list.

The inherited Stress80 evaluator was rerun on the same clean tree and still returns candidate digest `8e38dbf...d63f09f28`, Stress annualized `80.067891%`, DD `-29.727688%`, turnover `337,934,465`, efficiency `30.990722 bps`, no HALT and zero margin rejects. The new behavior is therefore additive and the PR #24 checkpoint remains exactly reproducible.

## Improvement over Stress80

| Metric | Stress80 | Stress90 | Change |
| --- | ---: | ---: | ---: |
| Stress annualized | 80.067891% | 112.100053% | +32.032162 pp |
| Stress max DD | -29.727688% | -14.567214% | +15.160474 pp |
| Stress net alpha | 1,047,283.30 | 1,619,022.78 | +571,739.47 |
| Stress turnover | 337,934,465 | 206,838,150 | -131,096,315 |
| Stress net/turn | 30.990722 bps | 78.274862 bps | +47.284141 bps |
| prior1 annualized | -32.117204% / HALT | 12.141524% | positive / no HALT |
| prior2 annualized | -29.445651% / HALT | 8.578529% | positive / no HALT |

The gain is not mechanical minimum-turnover optimization: concentrated recent entry/increase remains the alpha engine. The response suppresses only diffuse-leadership additions whose net contribution is negative in prior1, prior2 and full_recent.

## Causality and overfit controls

- Current target weights are already available at the decision; strictly prior expanding HHI contains no future market data.
- The comparison happens before the current HHI is appended.
- Independent validation/OOS simulations receive only target-state history strictly before their start.
- Appended future data cannot change prior target-state labels.
- No candidate holdings, realized PnL or outcome label feeds the state, avoiding the failed candidate-owned feedback loop.
- No threshold, percentile, lookback, product, leverage, margin, OI setting, cost hurdle, horizon or reserve parameter was searched.
- Two of three allowed families and two of six quick candidates were used; the third family was stopped after the ideal target passed.
- Full-path reserve correctness was tested once and rejected as standalone negative evidence; it was not tuned.

## Risk and runtime boundary

Unchanged hard authorities:

- target and realized gross `<=2x`;
- margin `<=35%` and available `>=25%`;
- daily-loss hard gate `5%`;
- total-drawdown hard gate `30%`;
- maximum contract lots `35`;
- reduction-first execution;
- Broker/CTP remains the only truth for positions, capital and fills;
- RiskManager/circuit/HALT authority is unchanged.

This promotion contains offline validated Production evaluator components only. It does not wire the strategy into live runtime and does not use research CSV artifacts as live data sources. Any later live promotion still requires restart recovery, missing/stale 60m fail-closed behavior, session/calendar and roll correctness, startup warmup, and live/offline equivalence tests.

## Negative evidence

`docs/stress90-bounded-research-evidence.md` preserves the complete bounded counters, P0 attribution, causal regime decomposition, the standalone full-path reserve collapse to `0.829912%`, and the inherited locked failures. No failed route was rescued with parameter changes.

## Verification disposition

- Fresh matrix: passed (`1 / 2` used).
- Focused mechanics/tests before matrix: passed.
- Code review: one evidence-assembly fail-closed issue found and fixed; no open behavioral finding.
- Permanent-tree Python 3.10 / Python 3.13 full CI and merge-tree identity: pending final execution.
