# Stress80 Final Production Evidence

## Status

This document records the final fixed-input Production evidence for the Stress-15bp research target. It is a Production-mechanics promotion record, not a claim that live runtime wiring has changed.

The final candidate passed the predeclared promotion gate on a fresh clean branch. Live `runtime_factory.py`, `execution_aligned_runtime.py`, `directional_runtime.py`, RiskManager ownership, Broker/CTP account truth and reduction-first execution remain unchanged.

## Fixed candidate

The final candidate has no fitted parameter in the promotion stage:

1. Frozen 9-product completed D -> D+1 60-minute Price x OI evidence.
2. For those supported products, entries, same-sign increases and reversals into a new side require OI confirmation; an unconfirmed reversal exits the incumbent side to flat rather than carrying stale risk.
3. Archived cost eligibility: arithmetic sum of 20 completed daily product returns, scaled to a fixed 3-session benefit horizon, compared with the fixed 15bp one-way hurdle.
4. Tracking-first / turnover-second reallocation among the cost-approved survivors, restoring only the already-confirmed OI gross. It cannot create new product support, change target sign or exceed 2x gross.
5. Strict freeze-new-risk soft defense only after completed account drawdown reaches the hard-limit-derived reserve boundary: 30% hard total drawdown - 5% daily-loss reserve = 25%. Reductions, exits, reversals and same-product rolls remain executable.

No threshold search, lookback search, product-PnL ranking, risk-limit relaxation, future leakage or fake data was used in this final promotion stage.

## Hard constraints

Unchanged throughout final evaluation:

- target and realized gross <= 2.0x
- hard margin ratio <= 35%
- minimum available ratio >= 25%
- maximum daily loss = 5%
- maximum total drawdown = 30%
- maximum contract volume = 35 lots
- reduction-first semantics unchanged
- RiskManager remains authoritative
- Broker/CTP state remains the account/order/position truth

## Frozen input lineage

- concrete-contract / broad-daily artifact `9473260618`
  - SHA256 `ab4321a9cf8b9a61a0f9d435a6fb6bba7dd63986043b429c12c0ff47fd15389c`
- frozen execution-aligned weights artifact `9491959916`
  - SHA256 `e531f2874cbc26c3a54ff561e074f59b86ac887a1f509e33effd6908eaa3144d`
- prior two-year 60m artifact `9516473115`
  - SHA256 `d0adcfb348c5ae89ddb488474c088d74a2402a2426c0e84fee7877dda5bd2a4e`
- recent two-year 60m artifact `9516472727`
  - SHA256 `68ff2200eed8326741ce76f1cff46379c4e1dbc263297f4602bd84422e95e421`

## Discovery checkpoint

Quick five-window Production gate run `32760575269` first crossed the target:

- Base full_recent annualized: **128.119261%**
- Stress full_recent annualized: **80.067891%**
- Stress full_recent max drawdown: **29.727688%**
- Stress train annualized: **10.177351%**
- Stress validation annualized: **511.519900%**
- Stress OOS annualized: **44.102450%**
- Stress gross peak: **1.683773x**
- Stress margin reject days: **0**
- Stress HALT: **false**
- Stress net alpha / turnover: **30.990722 bps**

The quick promotion gate passed. Research stopped after this candidate crossed the gate; no additional economic candidate was promoted afterward.

## Fresh clean-tree final matrix

Promotion branch: `promote/stress80-final`, forked exactly from main commit `78d96cabad17e7668a3077cfed843d4ea49a47cb`.

Fresh parallel final matrix workflow run: `32761623401`.

All seven independent account-window jobs rebuilt the exact same candidate weight path:

- candidate weight SHA256: `8e38dbf6441b561dd1728df08665b94b15cc3358823257505c2fcb9d63f09f28`

Final matrix results:

| Scenario | Window | Annualized | Max DD | Gross peak | HALT | Margin rejects | Net alpha / turnover |
| --- | --- | ---: | ---: | ---: | --- | ---: | ---: |
| Base | full_recent | 128.119261% | -28.518749% | 1.994925x | false | 0 | 42.095989 bps |
| Stress | prior1 | -32.117204% | -30.807368% | 1.646135x | true | 0 | -34.376805 bps |
| Stress | prior2 | -29.445651% | -30.144016% | 1.699139x | true | 0 | -40.351121 bps |
| Stress | train | 10.177351% | -24.231874% | 1.663371x | false | 0 | 4.120819 bps |
| Stress | validation | 511.519900% | -16.988818% | 1.619986x | false | 0 | 117.023217 bps |
| Stress | OOS | 44.102450% | -24.165871% | 1.675851x | false | 0 | 15.052732 bps |
| Stress | full_recent | **80.067891%** | **-29.727688%** | **1.683773x** | **false** | **0** | **30.990722 bps** |

Final promotion gate: **PASS**.

`prior1` and `prior2` are deliberately retained as negative historical evidence. They are not promotion windows in the predeclared Stress80 gate and were never retroactively removed or redefined. Their negative returns, drawdowns and HALT outcomes are therefore shown explicitly above.

Final summary artifact:

- artifact `9532997165`
- digest `sha256:7667ae671025bde9827436d29fe6e97e618650ca3e9526544420d67b39f43b1a`

## Repository verification at matrix head

Normal repository CI run `32761624469` completed successfully on Python 3.10 and 3.13 at the clean promotion head containing the winner, final evaluator/tests and temporary verification workflow. It included full repository tests, compileall, config validation, calendar/directional live examples, directional Production smoke, replay/accept flows, Auto Portfolio, execution-quality report, Production mechanics and CLI help.

The temporary final-matrix workflow is removed before merge. A fresh normal CI on that final permanent tree is required before the PR is merged to `main`.

## Promotion boundary

The promoted files preserve the validated research/Production evidence and its deterministic evaluator. They do **not** wire the candidate into the live directional runtime. Live economic activation requires its own explicit integration and runtime-data validation; this promotion does not silently change live trading behavior.
