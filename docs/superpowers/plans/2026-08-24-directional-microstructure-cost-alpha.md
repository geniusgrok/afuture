# Directional Microstructure and Cost Efficiency Implementation Plan

> Execute with Superpowers TDD and progressive verification. Base branch is `main`; feature branch is `codex/afuture-microstructure-cost-alpha-20260824`.

## Task 1 - Development harness and baseline lock
1. Keep normal repository CI skipped only for push events on the feature branch; PR/final tree restores the original CI verbatim.
2. Add a feature-branch-only targeted workflow that runs the affected tests and optional research entrypoint.
3. Verify the branch starts from `591bcc1f006b7588720b578b453ce58f0ba62690` and record tree `9490b33e42197f16e3809a33721a435a57d5e4bc`.

## Task 2 - P0-1 realistic L1 execution Stress
RED:
- add simulator tests proving 75% displayed-depth haircut can create partial FAK fills/unfilled quantity;
- add tests proving size/depth impact is deterministic, adverse, price-limit bounded and disabled in legacy mode;
- add report tests for requested/filled/unfilled/fill-ratio/spread/impact/slippage/latency.
GREEN:
- minimally extend `SimBroker` conservative options; preserve defaults byte-for-byte;
- add behavior-neutral execution summary primitive.
VERIFY: simulator/event/quality targeted suite, then affected broker/quality L2.

## Task 3 - P0-2 minute timing overlay primitive
RED:
- causal session-state tests for opening gap, 5m/15m range, VWAP, volume/OI deltas, spread/depth and imbalance;
- BUY/SELL opening permission tests;
- reduction/reversal bypass tests;
- missing/stale minute evidence falls back to immediate execution.
GREEN:
- add `afuture/directional_timing.py` with no account/risk ownership;
- integrate only into normal opening/increase submission path after reductions are absent.
VERIFY: timing + directional runtime/manager/risk/restart tests.

## Task 4 - P0-3 cost-aware Meta and exact lineage
RED:
- expose selected-template/product target audit path without changing frozen baseline output;
- test candidate switch rejected when completed expected score improvement does not exceed transition cost;
- test zero-cost endpoint reproduces old top-3 switching exactly;
- test no future row is read.
GREEN:
- refactor policy internals to return behavior-neutral Meta decision audit;
- add deterministic product-turnover cost estimator and `NetSwitchBenefit > 0` veto;
- preserve frozen template pool/lookback/rebalance/count and score formula.
VERIFY: execution-aligned policy + attribution + causality tests.

## Task 5 - P0-4 cost-aware no-trade region
RED:
- same-sign increase held when expected completed-history benefit <= transition cost;
- positive-net-benefit increase preserved;
- absolute reduction, exit, reversal and safety roll are never held;
- output gross never exceeds unfiltered requested target.
GREEN:
- generalize `directional_efficiency.py` with one deterministic net-benefit filter;
- integrate before normal rebalance plan only after target construction; do not touch hard-risk actions.
VERIFY: efficiency/manager/runtime/risk tests.

## Task 6 - P0 economic research
1. Add one bounded research fetch for 5-minute concrete-contract data over predeclared liquid roots/contracts only; do not persist a warehouse.
2. Evaluate baseline, timing overlay, cost-aware Meta, no-trade and combined P0 under prior/train/validation/OOS/full_recent.
3. Rebuild fixed 15bp Production L3 only for candidates whose cheap multi-window evidence is plausibly positive.
4. Run realistic L1 execution Stress with the single predeclared simulator configuration.
5. Record turnover, DD, Sharpe, Net Alpha/Turnover, concentration, remove-best-period, LOPO, causality and component attribution.
6. Reject/revert any economic behavior that fails robustness; retain behavior-neutral observability if useful.

## Task 7 - P1 fixed intraday families
RED/GREEN for pure research functions first.
- Opening-range continuation: 5m opening context, 15m range, causal next-bar execution.
- Opening-gap exhaustion/reversal: fixed gap/range/VWAP structure; no threshold grid.
- Price/OI/Volume/Session score: +1/-1 price direction crossed with +1/-1 observed OI direction; volume confirmation is current cumulative volume above completed same-clock median where available; night/day and VWAP sign are modifiers with fixed equal contributions. Missing OI => no P1 signal, never forward-filled from future timestamps.
Evaluate every family independently over all evidence windows and stress endpoints. Failed family stops there.

## Task 8 - P1 same-product curve family
1. Use a small ex-ante set of liquid existing roots and key nearby/deferred months.
2. Build point-in-time curve slope/calendar spread, contango/backwardation, OI migration and roll-pressure features.
3. One fixed z-normalized mean-reverting curve rule with completed 60-observation history; no pair/root outcome mining.
4. Evaluate standalone robustness, directional correlation and marginal combined contribution. Promote only if standalone gates pass.

## Task 9 - Combination and conditional risk study
1. Combine only P0/P1 components already independently accepted.
2. If and only if Net Alpha and Sharpe improve under both stresses, evaluate one dynamic 2.25x/40%/20% liquidity-aware candidate.
3. Reject if gains are leverage-only, concentration rises materially, or OOS/robustness deteriorates. Do not test 2.5x unless 2.25x evidence is exceptionally strong.

## Task 10 - Finalization
1. Sync architecture/live-trading/data-backtest/production-checklist and a final evidence report.
2. Delete temporary research/development workflows and runtime-only research code that has no production/evidence value; restore `.github/workflows/ci.yml` exactly except for intentional final changes.
3. Run one complete final L4/CI on Python 3.10 and 3.13 and any final economic acceptance workflow required by the promoted behavior.
4. Review changed files and PR threads; resolve every substantive issue.
5. Squash merge to `main` as requested and verify final `main` SHA/tree.
6. Final response contains only the five requested economic answers.
