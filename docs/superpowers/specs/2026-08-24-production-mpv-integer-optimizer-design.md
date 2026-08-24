# Production MPV and Integer Optimizer Design

## Purpose

Build a research-only Production-aligned Marginal Production Value (MPV) framework and a discrete target-lot optimizer that can decide whether adding or removing one concrete-contract lot improves expected account-level net economics under the existing afuture Production risk envelope.

This design does **not** change the validated live Production path unless the candidate later passes causal prior/train/validation/OOS evidence, Production L3 Base + Stress, robustness, and final CI/L4. The existing validated target-lot construction remains the fallback and the default Production behavior.

## Frozen baseline and non-negotiable constraints

The branch starts from `main` at `d8a4158cadbea36ff7aa9e76dd7d562b218708c3`, the squash merge of PR #18.

Production full_recent remains the comparison baseline:

- Base annualized return: 109.0636%
- Base max drawdown: 15.8529%
- Base Sharpe: 2.0976
- Stress 15 bp one-way annualized return: 28.9559%
- Stress max drawdown: 28.1152%
- Stress Sharpe: 0.9604
- Stress gross signal PnL: 700,245
- Stress turnover notional: 256,918,290
- Stress transaction cost: 385,377.435
- Stress net alpha PnL: 314,867.565
- Stress Net Alpha / Turnover: 12.2556 bps

Hard risk authority and limits are immutable:

- target gross <= 2x
- realized gross <= 2x
- hard margin <= 35%
- available >= 25%
- normal soft-margin envelope is not expanded
- daily loss = 5%
- total drawdown = 30%
- max absolute contract lots = 35
- reduction-first semantics remain authoritative
- Broker / CTP remains the only account, order, fill, and position truth
- RiskManager circuit / HALT authority is unchanged
- no historical future information may enter a decision feature

PR #18 Candidate A/B tuning dimensions are out of scope. The earlier tracking-error integer allocator is also not a candidate to tune: it reduced Stress annualized return from 28.9559% to 17.9238% and was correctly rejected.

## Architectural decision

Use a three-boundary design:

1. **MPV domain model** converts already-available Production/PIT evidence into monetary value components for a single `+1` or `-1` lot action.
2. **Discrete optimizer** consumes only current state, raw requested directional intent, hard/soft capacity, and MPV actions. It never owns signal generation or RiskManager authority.
3. **Offline attribution/counterfactual tooling** may use later realized prices only as explicitly labeled evaluation targets. Those labels are physically separated from the decision-side MPV API and are not importable by live runtime code.

This avoids repeating the failed pattern of optimizing distance to float weights. The optimizer compares discrete Production economics directly.

## 1. MPV domain model

Create `afuture/directional_mpv.py` as a pure, dependency-light research module.

### Decision-side input

`MarginalLotAction` describes one concrete action at one decision time:

- product
- symbol
- delta_lots: exactly `+1` or `-1`
- current_lots
- requested_lots from the frozen raw strategy + existing governor path
- lot_notional
- per_lot_margin
- current account equity
- current margin utilization / available ratio when supplied by the caller
- governor state/scale when supplied by the caller
- roll-required flag
- evidence-through timestamp/date

It contains no future return or future fill field.

### Monetary components

`MarginalProductionValue` stores all components in account currency so the total is dimensionally auditable:

`total = expected_incremental_gross_alpha
         - expected_incremental_transaction_cost
         - margin_opportunity_cost
         - turnover_penalty
         - downside_risk_penalty
         - correlation_penalty
         - concentration_penalty
         - roll_execution_penalty`

No hidden factor weights are introduced inside this class. A caller must provide each monetary component explicitly. Zero is allowed only when the caller has a defensible reason/evidence for absence of that component.

Validation fails closed for non-finite values, negative cost/penalty values, non-positive notional/margin/equity, invalid delta size, requested sign violations, or evidence dated after the decision cutoff.

### Causality rule

Decision-day `D` MPV can consume evidence completed no later than the configured causal cutoff for `D`. Historical concrete-contract selection continues to use completed `D-1` activity for `D` execution. Any function that combines decision features with future outcome labels must live in the offline attribution module, not `directional_mpv.py`.

## 2. Net-alpha integer optimizer

Create `afuture/directional_integer_optimizer.py`.

The optimizer is deterministic and research-only. It receives:

- `requested_lots`: the frozen strategy/governor directional request expressed as concrete integer caps
- `current_lots`: Broker/simulator truth
- lot notionals
- per-lot margin estimates
- equity
- soft margin budget already derived from existing `adaptive_margin_sizing_share`
- max gross ratio <= 2.0
- max abs lots <= 35
- a caller-supplied objective callback that returns a fully decomposed Production value for a candidate target portfolio or one-lot transition

### Feasible domain

For each symbol/product:

- zero raw request means the optimizer cannot create exposure
- a raw long request permits only `[0, +requested]`
- a raw short request permits only `[requested, 0]`
- sign flips are permitted only when the raw request itself reverses sign
- current holdings do not authorize exposure beyond the raw requested magnitude
- hard gross, soft margin, max-lot and concrete-contract constraints are checked after every candidate move
- reduction-first order sequencing remains outside the optimizer in the existing rebalance planner
- hard RiskManager/Broker gates are still evaluated after target construction and remain authoritative

### Search method

Use bounded deterministic one-lot local improvement rather than parameter search or a large external solver dependency:

1. start from a feasible reference target supplied by the caller (normally the existing validated margin-fitted target)
2. enumerate legal `+1` / `-1` target changes in stable symbol order
3. compute objective delta for each feasible move
4. apply the strictly best positive improvement, tie-breaking deterministically by symbol/action
5. repeat until no positive one-lot improvement remains
6. optionally evaluate a one-for-one capacity swap when the best incoming lot is blocked only by gross/margin capacity; execute only when the two-leg net objective delta is strictly positive
7. cap iterations by the finite total feasible lot distance, not by a tunable search hyperparameter

If the objective is missing/non-finite, the reference target is infeasible, or no trustworthy MPV evidence exists, return the validated reference target with an explicit fallback reason.

## 3. Offline MPV attribution and counterfactual labels

Create `afuture/directional_mpv_attribution.py`.

The first responsibility is exact behavior-neutral aggregation from existing Production simulation `daily` + `events`:

- completed product gross PnL
- completed product turnover
- completed product transaction cost
- completed net alpha
- exposure-side and lots held from PnL audit rows
- product concentration / gross exposure where reconstructable from existing exact product-level events

The second responsibility is **offline-only** one-lot counterfactual labeling. A label row must make future use explicit in its name/schema, for example:

- decision date
- product/symbol
- action `+1` or `-1`
- realized next holding-period gross PnL delta
- realized incremental turnover/cost delta
- realized net counterfactual value
- label end date

These labels are evaluation targets only. They must not be imported by `directional_runtime.py`, `execution_aligned_runtime.py`, `runtime_factory.py`, or any live policy module.

The tooling must be able to answer examples such as `AG +1 lot`, `CU +1 lot`, and `RB -1 lot` for a historical decision date while preserving the distinction between PIT input evidence and ex-post label.

## 4. First causal forecast candidate

Do not invent a high-dimensional predictor in P0.

The initial candidate is deliberately low degrees of freedom and Production-native: derive product-level completed net-alpha-per-exposure evidence from prior completed Production audit events, then shrink it toward the portfolio-level completed rate when product evidence is sparse. The shrinkage rule must be deterministic, monotone in completed evidence amount, and contain no searched lookback/threshold/factor weights.

The forecast layer must expose the evidence count/weight and global fallback contribution so small-sample product estimates are auditable. It may use the frozen strategy direction/magnitude as context, but it must not modify the underlying signal family or Candidate A/B parameters.

If the existing event ledger cannot reconstruct a denominator with sufficient causal fidelity, stop promotion at infrastructure/negative evidence rather than substituting a fabricated exposure history.

## 5. Research acceptance adapter

If and only if unit evidence is sound, add a research-only subclass/adaptor of `MarginAwareDirectionalProductionAcceptance` that:

1. builds the unchanged raw integer request
2. computes the unchanged adaptive soft-margin envelope
3. constructs causal MPV evidence using completed information only
4. invokes the new integer optimizer inside the same envelope
5. emits target/audit diagnostics
6. falls back exactly to the existing validated final target when MPV evidence is unavailable or the optimizer cannot establish a valid candidate

Do not wire this adapter into live runtime or default CLI configuration during research.

## 6. Promotion gates

A candidate is promotable only after all of the following are evaluated jointly:

- Base annualized return
- Stress annualized return
- Base Sharpe
- Stress Sharpe
- Base max drawdown
- Stress max drawdown
- OOS Base
- OOS Stress
- prior1 / prior2 / train / validation
- Net Alpha / Turnover
- gross signal PnL
- turnover
- transaction cost
- product concentration
- leave-one-product-out
- remove-best-period
- active days
- realized gross
- margin rejects
- permanent HALT

A Production promotion requires improvement to realized Production net economics without weakening robustness or any hard risk gate. Float-only improvement is not evidence of promotion quality.

If P0 fails Production-aligned evidence, retain the framework and reliable negative evidence only where it has independent audit/research value; remove/disconnect rejected runtime candidate wiring and keep validated Production behavior unchanged. Do not tune Candidate A/B or perform large parameter searches to rescue P0.

## 7. Testing and verification strategy

TDD is mandatory for behavior changes.

Progressive verification:

- RED/GREEN: only new MPV/optimizer unit tests and directly affected tests
- milestone: directional attribution / acceptance / causality subsystem tests
- economic candidate: cheap causal windows before full Production L3
- final candidate only: one complete CI and L4/acceptance matrix
- rerun full validation only after a substantive post-full-run behavior change, shared infrastructure change, or evidence that the prior scope was insufficient

Required unit properties include:

- MPV component arithmetic and validation
- future-dated evidence rejection
- no sign creation/flip outside raw intent
- 35-lot cap
- gross <= 2x
- unchanged soft margin budget
- deterministic tie breaking
- capacity swap only on strictly positive net value
- fallback returns reference target exactly
- offline label module is not imported by live runtime
- reduction-first rebalance semantics remain unchanged

## 8. Scope boundary

This spec covers P0 only: Production-aligned MPV, discrete optimizer, attribution/counterfactual tooling, and evidence-driven decision whether it deserves Production promotion.

Product × Alpha Family Allocation, Selective Defensive Allocation, new term-structure/basis/inventory/member-positioning alpha, execution-aware contract selection, and real CTP execution optimization are separate follow-on specs. They are opened only after P0 evidence justifies the dependency or P0 is conclusively rejected and the next information-value route is selected.
