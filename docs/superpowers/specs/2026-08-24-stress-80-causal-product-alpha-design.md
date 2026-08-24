# Stress 80 Causal Product × Alpha Net-Edge Design

Date: 2026-08-24
Branch: `research/stress-80-causal-product-alpha`
Base: `main@d5e1e35163544d56e158cfbcfd27b3691b62aa6b`

## 1. Objective

Replace the previous "preserve Production unless a low-freedom overlay happens to improve it" research posture with a direct economic objective:

- Stress one-way 15bp full_recent annualized return **>= 80%**;
- Stress max drawdown **<= 30%**;
- Base annualized return **>= 80%**, with a preference to retain >=100% if the data supports it;
- no permanent HALT;
- target and realized gross **<= 2x**;
- hard margin **<= 35%**;
- available **>= 25%**;
- max absolute contract lots **<= 35**;
- reduction-first, RiskManager authority, Broker/CTP truth, circuit semantics and account/order/fill state remain authoritative.

The current validated Production baseline remains the comparison control:

- Base annualized 109.0636%, max DD 15.8529%, Sharpe 2.0976;
- Stress annualized 28.9559%, max DD 28.1152%, Sharpe 0.9604;
- Stress gross signal PnL 700,245;
- Stress turnover 256,918,290;
- Stress 15bp transaction cost 385,377.435;
- Stress net alpha 314,867.565;
- Stress net alpha / turnover 12.2556 bps.

The design must improve economic behavior rather than produce another governance-only round whose Production returns remain unchanged.

## 2. Problem diagnosis

The present signal stack can generate high gross alpha, but too much is lost when translated into integer-contract Production. The realized-path costless Stress proxy is about 88.05% annualized while the final 15bp Production path is 28.96%. Mechanical turnover suppression has already failed because entry/exit turnover contains real alpha.

The previous causal MPV experiment also failed structurally. It learned only from candidate-owned exposures; once it removed a product, it stopped observing candidate outcome evidence for that product and could not learn that the opportunity recovered. This endogenous off-policy censoring produced self-extinguishing exposure.

The next architecture therefore needs an **exploration-independent opportunity ledger**: every product/family opportunity can receive a completed forward-return label after the horizon ends, whether or not Production actually traded it.

## 3. Core architecture

The new research path has four independent units.

### 3.1 Opportunity ledger

For each completed decision date `D`, frozen product, and alpha family, persist the signal state that was knowable at `D` and later attach forward outcome labels only after those future sessions are fully complete.

A row conceptually contains:

```text
signal_date
product
family
signal_direction
signal_strength
forward_horizon_sessions
label_available_date
future_specific_contract_gross_return
future_specific_contract_net_return_15bp
```

Decision code may consume only rows whose `label_available_date < decision_date`.

Crucially, labels are generated for observable historical opportunities regardless of whether the research candidate held the position. This removes the previous self-censoring feedback loop.

### 3.2 Causal Product × Alpha edge estimator

Estimate expected edge at `(product, family, horizon)` from only completed ledger rows. The initial allowed horizons are fixed at **5, 10, and 20 sessions** because they are economically interpretable holding periods rather than a broad search grid.

The estimator must:

- use expanding completed history;
- expose sample support and uncertainty;
- shrink sparse product/family estimates toward broader family/global evidence instead of hard-zeroing them;
- never use OOS/full_recent labels before they become causally available;
- never use candidate-owned PnL as the only learning source;
- never use future MFE/MAE/false-breakout labels directly as decision features.

No hidden threshold sweep is allowed. Any support or shrinkage rule must be deterministic and derived from observed support statistics rather than chosen after reading final returns.

### 3.3 Net-edge capital competition

The allocator compares the value of keeping the current integer position against legal one-lot increases, reductions, reversals, and positive two-leg capacity swaps.

Each candidate action is scored in monetary terms:

```text
expected gross alpha
- exact target-vs-current one-way transaction cost
- unavoidable roll/execution penalty when applicable
- capacity / margin opportunity cost if another higher-value lot must be displaced
```

No arbitrary weighted sum of unrelated factors is permitted.

The existing deterministic integer optimizer from PR #19 should be reused where possible. It remains subordinate to the existing Production hard-risk path. The research allocator must never become a second RiskManager.

A new trade must beat the value of retaining the current position after Stress cost. This creates endogenous turnover discipline without a mechanical no-trade or minimum-hold rule.

### 3.4 Production-mechanics adapter

The candidate must be evaluated through the same concrete-contract path as the validated baseline:

```text
causal product/family edge
-> desired product exposure
-> completed-return governor
-> completed-activity concrete-contract selection
-> integer lots
-> max 35 lots
-> margin-aware target fitting
-> reduction-first
-> account equity / circuit / HALT
-> exact realized turnover and cost
```

The adapter may change alpha selection and capital allocation, but may not loosen risk constraints or replace Broker/RiskManager truth.

## 4. Alpha families and search boundary

Phase 1 reuses the already-existing six directional families so the experiment answers a precise question: can Production improve materially by learning **which product × family edge is currently worth paying 15bp for**?

Allowed families:

- breakout;
- tsmom;
- momentum;
- moving_average;
- reversal;
- acceleration.

The frozen 96-template library can provide signal realizations, but Phase 1 must aggregate evidence at a family/product level rather than search another large template combination space.

Not allowed in Phase 1:

- broad MA/lookback/rebalance grids;
- changing gross >2x;
- changing margin >35% or available <25%;
- increasing max lots >35;
- changing daily loss 5% or total DD 30%;
- fitting thresholds directly against full_recent;
- rescuing a failed result by changing horizons after seeing OOS/full_recent;
- mixing previously rejected basis/full-curve signals into the candidate just to raise headline return.

## 5. Data and causality

All decisions remain point-in-time.

- `D` completed continuous/product information may determine `D+1` target intent.
- Specific-contract return labels must respect the existing roll-safe next-open lineage.
- A 5-session label for a signal created on `D` cannot enter the estimator until the fifth future session is complete.
- Missing or invalid future contract history means that ledger row is unavailable, not synthetically filled.
- Historical universe/listing/expiry rules remain point-in-time.
- Continuous roll jumps cannot be counted as tradable alpha.
- Final OOS is already non-pristine historically; it remains a robustness window, not a pristine scientific holdout.

## 6. Evaluation sequence

Use progressive, impact-driven validation.

### L1 — correctness

Unit tests prove:

- label availability date causality;
- untraded opportunities still receive completed historical labels;
- future rows cannot leak into current estimates;
- sparse estimates shrink rather than self-extinguish;
- 15bp transaction cost is charged exactly once;
- optimizer never violates sign intent, gross, lot or margin envelopes;
- live runtime does not import future-label research code.

### L2 — synthetic economics

Small deterministic scenarios prove:

- a product/family that recovers after an earlier bad regime can regain capital because off-policy labels continue arriving;
- weak positive gross edge is rejected when cost exceeds expected alpha;
- a high-value product can replace a lower-value lot through a legal capacity swap;
- unchanged/low-value targets do not churn.

### L3A — cheap historical screen

Before full Production mechanics, evaluate family/product edge quality and candidate turnover on fixed historical inputs. This screen is diagnostic and cannot by itself earn promotion.

Required reporting by prior1/prior2/train/validation/OOS/full_recent:

- Base and Stress annualized;
- Sharpe;
- max DD;
- turnover;
- gross alpha;
- transaction cost;
- net alpha;
- net alpha / turnover;
- active days;
- family/product contribution concentration.

A candidate may advance from L3A to fixed Production L3B only when all of these predeclared conditions hold on the cheap screen:

1. full_recent Stress annualized is **> 28.9559%**;
2. validation Stress annualized is **> 0%**;
3. OOS Stress annualized is **> 0%**;
4. validation and OOS Stress max DD are each **<= 30%**;
5. full_recent Stress net alpha / turnover is **> 12.2556 bps**;
6. no validation/OOS permanent HALT or hard-risk violation.

This gate is deliberately weaker than the final 80% Production target because L3A does not reproduce all Production path dependence; it exists only to prevent spending an expensive L3B run on an obviously weak candidate.

### L3B — fixed Production mechanics

Only a candidate that passes every L3A condition proceeds to the expensive fixed-input Production path.

The authoritative economic gate is the same specific-contract/integer/margin/governor/circuit path used by the current Production baseline.

For final Production promotion, validation and OOS each must remain economically alive: Stress annualized **> 0%**, max DD **<= 30%**, no permanent HALT and no hard-risk violation. This is the exact definition of "no catastrophic window failure" for this phase.

### L4 — final robustness matrix

Run once after the implementation reaches final-candidate state. Repeat only after a material result-affecting fix.

## 7. Promotion criteria

A candidate may replace validated Production only if all hard conditions hold:

1. Stress 15bp full_recent annualized **>= 80%**.
2. Stress max DD **<= 30%**.
3. Base annualized **>= 80%**.
4. No permanent HALT in full_recent.
5. No violation of gross <=2x, margin <=35%, available >=25%, max lots 35.
6. Validation and OOS each satisfy Stress annualized >0%, max DD <=30%, no permanent HALT and no hard-risk violation.
7. Improvement remains after exact transaction costs and Production mechanics.
8. No future leakage or non-causal data source.
9. No post-hoc threshold/parameter rescue after reading the final windows.

`Stress >=80%` is a target, not permission to fabricate a result. If the predeclared Phase 1 architecture fails, preserve the negative evidence and move to a genuinely higher-information Phase 2 instead of overfitting the same OHLC history.

## 8. Phase 2 escalation if Phase 1 fails

If Product × Alpha net-edge is economically insufficient, the next research priority is new point-in-time information, not another round of parameter tweaking:

1. robust historical full curve structure;
2. real spot/basis where coverage is valid;
3. exchange warehouse/registered receipt history from a reliable source;
4. member positioning/rank history from a reliable source;
5. sufficiently long minute/L1 history for execution-aware alpha.

Each source must first pass a coverage and causality audit. Missing data must not be fabricated.

## 9. Expected code boundaries

Prefer new research-focused modules rather than expanding live runtime files prematurely:

- `afuture/directional_opportunity_ledger.py` — build and validate completed opportunity labels;
- `afuture/directional_causal_edge.py` — causal Product × Alpha estimator;
- `afuture/directional_net_edge_allocator.py` — monetary target/action scoring and optimizer bridge;
- `afuture/directional_stress80_research.py` — research-only Production adapter;
- `tools/evaluate_directional_stress80.py` — fixed-input evaluation/reporting;
- focused tests for each unit.

Only after a candidate passes the complete promotion gate may `runtime_factory.py` or live policy wiring change.

## 10. Failure handling

Research failures are evidence, not reasons to loosen constraints.

- If a unit test exposes leakage, fix causality before any economic run.
- If L3A misses any predeclared gate, stop that candidate before Production L3B.
- If L3A looks strong but Production collapses, diagnose integer/margin/governor path dependence rather than tuning headline weights.
- If a full Production run fails because of an implementation bug, fix the bug and rerun the affected gate.
- If it fails economically, do not silently alter horizons, thresholds, costs or risk limits to rescue it.

## 11. Delivery

Work occurs on `research/stress-80-causal-product-alpha` from exact base `d5e1e35163544d56e158cfbcfd27b3691b62aa6b`.

Final delivery requires:

- reproducible evidence document;
- focused unit/integration tests;
- final Python 3.10/3.13 CI;
- PR review;
- squash merge to `main` only if the economic promotion gate is satisfied. If the gate is not satisfied, merge only research/evidence infrastructure that is behavior-neutral and clearly documented, leaving validated Production unchanged.
