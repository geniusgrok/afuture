# Directional Microstructure, Cost, and Intraday Alpha Design

Date: 2026-08-24
Base: `main@591bcc1f006b7588720b578b453ce58f0ba62690` (PR #16)

## Goal
Improve execution-aligned directional Stress net return, Sharpe and Net Alpha / Turnover without fitting the repeatedly observed 2024-08-21..2026-08-20 window. Fixed 15bp one-way Stress remains a promotion gate; a separate realistic L1 execution Stress is added rather than replacing it.

## Frozen invariants
- Daily Alpha remains the sole owner of directional sign and requested target exposure.
- Risk reductions, reversals, daily circuit, gross guard, hard/manual halt and fail-closed paths can never be delayed or suppressed by timing/cost logic.
- Broker/CTP remains sole account/order/fill/position truth.
- Initial hard envelope remains gross <=2.0x, margin <=35%, available >=25%, max contract volume 35.
- No ordinary MA/momentum/breakout/META parameter search and no threshold grid.
- Each candidate gets one predeclared configuration before economic evidence is inspected.
- No 5-10 year minute/OI warehouse. Intraday research uses a bounded liquid-product/concrete-contract sample only.

## P0-1 Realistic microstructure Stress
Reuse `SimBroker`; do not create a second matching engine. Existing mechanics already model bid/ask crossing, shared displayed L1 depth, partial fills, FAK/FOK cancellation, latency, fixed slippage and market impact. Extend conservative matching with an optional deterministic displayed-depth haircut and size/depth impact schedule. The first realistic Stress configuration is fixed before evaluation:
- L1 displayed-depth haircut: 0.75;
- latency: one incoming tick/bar;
- fixed adverse slippage: 0 ticks beyond crossing;
- additional impact: one price tick for each full displayed-depth multiple requested beyond the first, capped only by daily price limits;
- FAK remainder stays unfilled/cancelled.
The report must expose requested, filled and unfilled quantity, fill ratio, spread paid, impact/slippage, latency and turnover/cost. Fixed 15bp Stress is retained independently.

## P0-2 Minute timing overlay
Add an opening/increase-only timing primitive. It receives an already-approved daily target delta and causal minute/L1 state; it cannot change target sign or size. Reductions and reversals bypass it.

Predeclared execution rule (single configuration):
- first 5 minutes establish opening gap/context; first 15 minutes establish opening range;
- calculate causal session VWAP from cumulative-volume deltas, relative volume versus completed same-clock observations when available, OI change from observed session open, spread in ticks, L1 imbalance `(bid_volume-ask_volume)/(bid_volume+ask_volume)`;
- for a BUY increase, execute when price is not extended above VWAP/opening range while spread/depth are acceptable, or after the 15-minute decision boundary; SELL is symmetric;
- if minute evidence is missing/stale, fall back to the existing immediate opening path rather than inventing an Alpha change;
- risk reduction/circuit/halt always executes immediately.
The overlay is promoted to production only if a bounded minute-data evaluation improves net economics across prior/train/validation/OOS and both cost stresses.

## P0-3 Transaction-cost-aware Meta
Keep the frozen 96-template pool, score formula, 11-session lookback, 3-session rebalance and top-3 count. Add exact audit lineage `template -> product -> requested target -> integer target -> realized position -> turnover -> cost`.

At a scheduled Meta decision, compare incumbent selected templates with the frozen-score candidate. Define:
`NetSwitchBenefit = expected_alpha_improvement - expected_transition_cost`.
Expected alpha improvement uses only completed trailing template return evidence already available to Meta. Expected transition cost uses the product target change implied by incumbent versus candidate and the same declared one-way cost endpoint. Candidate replaces incumbent only when NetSwitchBenefit > 0. There is no tunable threshold and no grid.

## P0-4 Cost-aware no-trade region
Generalize the existing same-direction +1-lot stabilizer to an opening/increase-only net-benefit veto. For a same-sign target increase or small resize, estimate completed-history expected benefit over the policy's next rebalance horizon and compare with deterministic one-way transition cost for the requested delta. If benefit does not cover cost, keep incumbent lots.

Non-negotiable bypasses: any absolute exposure reduction, full exit, reversal, roll required for contract safety, circuit/gross guard/hard halt. The filter may never increase gross relative to the unfiltered requested target.

## P1 independent Alpha families
All are research-only until independent gates pass.

### Opening-range intraday Alpha
Exactly two families, fixed 5m/15m structure: opening-range continuation and opening-gap exhaustion/reversal. No window sweep. Signal timestamps use only completed bars. Evaluate standalone before combination.

### Price x OI x Volume x Session
Classify completed observations into Price up/down x OI up/down and condition on night/day/opening context, relative volume and VWAP location. OI is consumed only from the timestamp at which it is observed; no end-of-day OI may leak backward. One fixed scoring rule is declared in the implementation plan.

### Same-product curve / calendar Alpha
Use only same-root nearby/deferred contracts. Fixed liquid roots are chosen ex ante from existing production products, not from outcome ranking. Features: normalized calendar spread/curve slope, contango/backwardation, completed OI migration and roll pressure. No cross-product pair mining. Report standalone return/Sharpe/DD/turnover, correlation with the directional sleeve, and marginal portfolio contribution.

## Risk scaling
Do not change the 2x/35%/25%/35-lot production envelope unless at least one P0/P1 economic behavior passes both fixed 15bp and realistic Stress with improved Net Alpha / Sharpe. Only then evaluate one predeclared dynamic candidate: gross cap 2.25x, margin cap 40%, available floor 20%, and liquidity-aware lot cap. Extra risk is allowed only under high completed Alpha quality, high displayed/observed liquidity and low sleeve correlation; bad liquidity or shock state reverts to the original envelope. 2.5x is out of scope unless evidence at 2.25x is exceptionally strong.

## Evidence gates
For every economic candidate report prior/train/validation/OOS/full_recent, fixed 15bp Stress, realistic Stress, max DD, Sharpe, turnover, Net Alpha/Turnover, concentration, remove-best-period, leave-one-product-out and causality. A full_recent-only win, weak OOS, one-product dependency or leverage-only gain is rejected. Failed families are never blended to rescue the headline result.

## Verification
L1 targeted RED/GREEN tests; L2 affected directional/simulator/policy suites at meaningful milestones; L3 fixed Production economics only for plausible candidates; one final L4/CI on the final stable tree. Temporary development/research workflows are removed before the PR.
