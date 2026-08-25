# Directional Microstructure / Cost / Intraday Alpha Evidence

> **Historical record.** Preserved for research lineage and negative evidence. It does not describe the current Stress-90 checkpoint or live activation; see [`documentation-index.md`](documentation-index.md).

Date: 2026-08-24

## Scope and fixed baseline

This phase starts from `main@591bcc1f006b7588720b578b453ce58f0ba62690` (PR #16), tree `9490b33e42197f16e3809a33721a435a57d5e4bc`.

Frozen Production economics before this phase:

- Base annualized return: **109.0636%**; max drawdown **15.8529%**; Sharpe **2.0976**.
- 15bp one-way Stress annualized return: **28.9559%**; max drawdown **28.1152%**; Sharpe **0.9604**.
- Stress turnover notional: **256,918,290**.
- target / realized gross hard cap: **2.0x**; hard margin **35%**; available floor **25%**; max contract volume **35**; no permanent HALT.

Fixed data lineage is unchanged. Production L3 input artifact `9473260618` has SHA-256 `ab4321a9cf8b9a61a0f9d435a6fb6bba7dd63986043b429c12c0ff47fd15389c`; it contains the frozen 50-product continuous daily history and concrete-contract daily history. Baseline output artifact `9491959916` has SHA-256 `e531f2874cbc26c3a54ff561e074f59b86ac887a1f509e33effd6908eaa3144d`.

No candidate below was rescued with a threshold grid or a second parameter configuration.

## P0-1 — realistic microstructure execution Stress

`SimBroker` now has an opt-in realistic L1 stress mode while retaining its previous defaults. The retained simulator can model:

- bid/ask crossing and spread;
- queue-adjusted displayed depth via a fixed `depth_haircut`;
- shared L1 depth consumption across orders;
- partial fill and FAK/FOK unfilled cancellation;
- tick latency;
- fixed slippage / impact plus deterministic requested-size-to-displayed-depth impact;
- daily price-limit clipping;
- requested / filled / unfilled quantity, fill ratio, spread cost, slippage-impact cost, commission, turnover, and volume-weighted latency attribution.

The predeclared realistic configuration was depth haircut `0.75`, latency `1` tick, fixed extra slippage `0` ticks, and one extra impact tick per full displayed-depth multiple beyond the first.

Historical two-year L1 bid/ask/depth is not present in the frozen artifact. Therefore the simulator receives **no fabricated historical annualized-return credit**. Fixed 15bp remains the comparable two-year economic Stress; realistic L1 Stress is an executable replay model when point-in-time L1/tick history is supplied.

Decision: **retain simulator + audit infrastructure; no Production Alpha or sizing change**.

## P0-2 — minute-level daily signal timing overlay

A causal 5m/15m timing prototype was TDD-verified with opening gap, opening ranges, VWAP, relative volume, observed OI change, spread, depth, and L1 imbalance. It could only delay new exposure / same-sign increases; reductions, exits and reversals bypassed it; missing/stale minute evidence fell back to immediate execution; delay was bounded at 15 minutes.

The bounded data probe then failed the required evidence horizon. The four fixed liquid roots returned only 1,023 five-minute rows each:

| Symbol | First 5m row | Last observed row | OI field |
|---|---|---|---|
| RB0 | 2026-08-03 10:05 | 2026-08-21 23:00 | yes (`hold`) |
| M0 | 2026-08-03 10:05 | 2026-08-21 23:00 | yes |
| TA0 | 2026-08-03 10:05 | 2026-08-21 23:00 | yes |
| CU0 | 2026-08-07 09:05 | 2026-08-24 00:00 | yes |

This cannot support prior1 / prior2 / train / validation / OOS evidence from 2022-08-22 through 2026-08-20. Building that coverage would require stitching large expired-contract minute/OI history, which is explicitly outside this phase.

Decision: **reject Production timing overlay for insufficient multi-window minute/OI evidence**. The rejected prototype was removed from the final tree rather than retained as dead Production-adjacent code.

## P0-3 — transaction-cost-aware Meta

The requested rule is materially the same low-freedom candidate already evaluated during PR #14: frozen 96 templates; Meta 11-session lookback / 3-session rebalance / top-3; completed Base evidence; candidate product target turnover priced at 15bp; switch only when completed expected alpha improvement exceeds transition cost.

PR #14 already rejected this candidate after fixed Production L3; its final design explicitly records cost-aware Meta hysteresis as rejected. Re-running the same L3 merely because the request was restated would be duplicate expensive evidence, so no second grid or threshold was run.

Decision: **reject; Production Meta remains frozen and unchanged**.

The missing audit requirement was solved separately and behavior-neutrally. `directional_lineage.py` now reconstructs the exact selected-template -> template-product contribution -> aggregate product target path and fails if it diverges from `ExecutionAlignedAggressivePolicy.weight_history()`. It then joins product target to the realized trade ledger. Integer position, turnover and transaction cost remain exact product-level truth; they are intentionally **not** fictionally allocated back to individual templates when multiple templates jointly own one product target.

## P0-4 — cost-aware no-trade region

One predeclared research configuration was evaluated: completed 20-session product return, 3-session benefit horizon, 15bp one-way hurdle; only new positions and same-sign absolute increases could be suppressed; reductions, exits and reversals bypassed the filter.

The cheap screen used the same frozen current-weight lineage and the same roll-safe concrete-contract next-open path. It reduced full_recent signal-layer turnover from **608.4x** to **440.8444x** (-27.54%) and improved the 15bp endpoint, but materially deleted valid Base Alpha.

### Base 5bp specific-contract screen

| Window | Baseline annualized | Candidate annualized | Delta |
|---|---:|---:|---:|
| prior1 | -17.3796% | +11.5161% | +28.8957 pp |
| prior2 | -4.6130% | -6.3884% | -1.7755 pp |
| train | +23.3707% | +14.9678% | -8.4029 pp |
| validation | +1625.3367% | +1556.6247% | -68.7121 pp |
| OOS | +167.4162% | +145.1620% | **-22.2543 pp** |
| full_recent | +187.2603% | +168.5521% | **-18.7081 pp** |

full_recent Sharpe fell **2.0609 -> 1.9794**. Remove-best-20-session annualized fell **71.9966% -> 60.6961%**. Worst leave-one-product-out annualized fell **31.2670% -> 22.8939%**.

### Stress 15bp specific-contract screen

| Window | Baseline annualized | Candidate annualized | Delta |
|---|---:|---:|---:|
| prior1 | -42.2328% | -14.3574% | +27.8754 pp |
| prior2 | -30.3963% | -26.3727% | +4.0236 pp |
| train | -10.8548% | -8.6109% | +2.2439 pp |
| validation | +1142.5621% | +1180.6132% | +38.0511 pp |
| OOS | +100.3473% | +100.2655% | -0.0818 pp |
| full_recent | +109.3145% | +113.4880% | +4.1735 pp |

full_recent Stress Sharpe improved **1.5268 -> 1.5829**, max drawdown improved **35.3528% -> 29.4520%**, and Net Alpha / Turnover improved **28.8643 -> 40.3368 bps**. This positive high-cost result is not enough to promote a rule that materially damages 5bp Base and Base OOS.

Decision: **reject before Production L3**. No second lookback, horizon, or hurdle was attempted; research code was removed.

## P0 formal disposition

No P0 economic behavior passed the joint Base + Stress robustness gate. Consequently Production annualized return, turnover, DD, Sharpe, leverage and hard-risk limits remain unchanged. Retained P0 value is behavior-neutral realistic execution simulation and exact attribution.

## P1-1 / P1-2 — opening-range and Price x OI x Volume x Session

The required 5m/OI evidence does not cover prior/train/validation/OOS. Evaluating opening-range continuation, opening-gap exhaustion/reversal, or session-level Price/OI/Volume state on roughly two recent August weeks would violate the stated research discipline.

Decision: **reject for insufficient multi-window point-in-time minute/OI evidence; no short-window tuning**.

## P1-3 — same-product curve / calendar spread family

One fixed family was evaluated from the fixed concrete-contract daily artifact, using four ex-ante liquid roots: `RB`, `M`, `CU`, `TA`.

Predeclared rule:

- choose the two highest-OI/high-volume eligible same-root contracts from each completed day; minimum 20 days to delivery, volume >=1,000 and OI >=5,000;
- sort the chosen pair by maturity;
- annualize `log(far_close / near_close)` by maturity gap;
- use a completed 60-observation rolling z-score;
- `|z| >= 1`: mean-revert the curve with pair gross 0.5; execute only on the next root trading-day open -> close;
- contango/backwardation, far-contract OI share, five-day OI migration and roll pressure are recorded as point-in-time diagnostics; they are not used as fitted gates;
- no cross-product pair mining and no root ranking by outcome.

Point-in-time spot/basis history is absent from the fixed artifact. A synthetic basis was not invented.

### Calendar curve family results

| Cost | prior1 | prior2 | train | validation | OOS | full_recent | full DD | full Sharpe |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 5bp | +0.1084% | -4.7306% | -2.5896% | -2.4146% | -9.6196% | **-4.3842%** | 8.3410% | -2.1298 |
| 15bp | -7.7522% | -13.1817% | -9.3006% | -11.0077% | -16.4801% | **-11.5956%** | 21.0690% | -5.5001 |

15bp turnover weight is **150.5x**. Remove-best-20-session annualized remains **-11.7376%**. The worst leave-one-root-out result remains negative (**M excluded: -10.4503% annualized**). Daily 15bp correlation with the actual Production Stress sleeve over full_recent is **-0.0290**, so correlation is low, but low correlation cannot rescue negative standalone expectancy.

Because the family fails independent prior/train/validation/OOS and full_recent gates, combination marginal contribution is **not evaluated by blending it into Production**. Doing so would violate the explicit rule against mixing failed families to rescue a headline result.

Decision: **reject curve/calendar family**.

## P1 combination and risk scaling

No new Alpha family independently passes the gate. Therefore no failed family is combined with the directional sleeve.

The prerequisite for risk relaxation is also false: P0/P1 did not produce a robust Net Alpha / Sharpe improvement. Gross `2.25x`, margin `40%`, available `20%`, liquidity-aware higher lot caps, and `2.5x` are therefore **not tested or promoted**. This prevents leverage from being used to manufacture a higher return number.

## Final Production disposition before final acceptance gate

Economic behavior remains PR #16-equivalent:

- Base annualized **109.0636%**, DD **15.8529%**, Sharpe **2.0976**;
- 15bp Stress annualized **28.9559%**, DD **28.1152%**, Sharpe **0.9604**;
- Stress turnover notional **256,918,290**;
- gross <=2x; hard margin <=35%; available >=25%; max lots 35; no permanent HALT.

Retained changes are behavior-neutral infrastructure only:

1. realistic, opt-in L1 execution stress and auditable fill-friction summary in `SimBroker`, with legacy/default semantics unchanged;
2. exact template -> product -> target -> realized position -> product turnover/cost audit through `directional_lineage.py` and `tools/evaluate_directional_lineage.py`.

Targeted TDD on the retained/then-rejected implementation path reached **61 passed** plus compileall in draft-PR run `32655510078`. The final stable tree is accepted only by the standard Python 3.10/3.13 CI plus the fixed-input Production L4 reproduction; rejected research workflows and temporary evidence files are not part of the final tree.
