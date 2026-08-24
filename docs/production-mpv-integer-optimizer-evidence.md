# Production MPV / Integer Optimizer Research Evidence

Date: 2026-08-24

## 1. Decision

**P0 v1 is rejected for Production.**

The research implementation successfully moved the optimization objective from float-weight tracking to explicit Production monetary marginal value and integer capacity allocation, but the first causal forecast candidate fails decisively once run through the fixed Production L3 mechanics.

No threshold, lookback, shrinkage constant, top fraction, core share, minimum observation count, factor weight, gross cap, margin cap, available-cash floor, or max-lot value was adjusted after observing the result. There is no v1 parameter rescue.

Validated Production therefore remains exactly the PR #18 baseline behavior and risk envelope:

- Base annualized return: **109.0636%**
- Base max drawdown: **15.8529%**
- Base Sharpe: **2.0976**
- Stress 15bp annualized return: **28.9559%**
- Stress max drawdown: **28.1152%**
- Stress Sharpe: **0.9604**
- Stress gross signal PnL: **700,245**
- Stress turnover: **256,918,290**
- Stress transaction cost: **385,377.435**
- Stress net alpha PnL: **314,867.565**
- Stress Net Alpha / Turnover: **12.2556 bps**
- gross <= 2x, hard margin <= 35%, available >= 25%, max 35 lots
- reduction-first and Broker / RiskManager authority unchanged

The retained value from P0 is research/audit infrastructure and a reproducible negative result, not a live allocation change.

## 2. Fixed lineage

Repository baseline: `main@d8a4158cadbea36ff7aa9e76dd7d562b218708c3` (PR #18).

Research branch: `research/production-mpv-integer-optimizer`.

Fixed Production input artifact:

- artifact `9473260618`
- SHA-256 `ab4321a9cf8b9a61a0f9d435a6fb6bba7dd63986043b429c12c0ff47fd15389c`

Frozen validated execution-aligned weights / baseline artifact:

- artifact `9491959916`
- SHA-256 `e531f2874cbc26c3a54ff561e074f59b86ac887a1f509e33effd6908eaa3144d`

MPV Production L3:

- workflow run `32681002779`
- evidence artifact `9504222857`
- artifact digest `sha256:e748ff0a2d7c54256dd473f5302d91966d972c54d28450468f8de9858e8fe5e8`

The workflow verifies both frozen-input SHA-256 values before simulation. It does not fetch a new market-data history and does not regenerate or refit the 96-template Meta weights.

## 3. P0 v1 design tested

The implementation separates three concerns:

1. `directional_mpv.py`: decision-side monetary MPV components and causal validation;
2. `directional_integer_optimizer.py`: deterministic +/-1-lot local improvement plus strictly positive two-leg capacity swaps under the existing raw-intent, soft-margin, 2x gross and 35-lot envelope;
3. `directional_mpv_attribution.py`: offline completed Production attribution and explicitly future-only counterfactual labels.

The first forecast candidate deliberately has low degrees of freedom:

- use already-observed completed Production `intraday` PnL only;
- denominator is exact `sum(abs(lots_before))` lot-segment exposure from the Production audit ledger;
- estimate product gross Alpha per lot-segment;
- shrink the product rate toward the global completed rate;
- prior support is the cross-sectional median observed lot-segment support, derived from the evidence snapshot rather than a searched constant;
- current target turnover cost is charged exactly as `abs(target - current) * lot_notional * current one-way cost rate`;
- unsupported correlation/concentration/downside/margin-opportunity penalty weights are **not fabricated**;
- no future outcome label is imported into runtime/research decision code.

The research acceptance adapter subclasses the validated margin-aware Production simulator. It first builds the unchanged baseline raw/margin-fitted target, then permits the optimizer to reallocate only inside that feasible envelope. Missing causal evidence, incomplete roll evidence, invalid objective values, or infeasible inputs fall back exactly to the validated baseline target.

No live runtime module imports the research adapter or offline label module.

## 4. Fixed Production L3 result

### 4.1 Full recent

| Metric | Validated baseline | MPV v1 | Decision |
|---|---:|---:|---|
| Base annualized | 109.0636% | **-14.2295%** | fail |
| Base Sharpe | 2.0976 | **-0.8693** | fail |
| Base max DD | 15.8529% | **30.8887%** | hard-path fail / HALT |
| Base active days | 478 / 484 | **296 / 484** | severe activity collapse |
| Base max realized gross | 1.998253x | **1.999792x** | within cap |
| Base permanent HALT | false | **true** | fail |
| Stress annualized | 28.9559% | **-12.8425%** | fail |
| Stress Sharpe | 0.9604 | **-0.9354** | fail |
| Stress max DD | 28.1152% | **30.4193%** | hard-path fail / HALT |
| Stress active days | 474 / 484 | **174 / 484** | severe activity collapse |
| Stress max realized gross | 1.668769x | **1.678598x** | within cap |
| Stress margin rejects | 0 | **0** | unchanged |
| Stress permanent HALT | false | **true** | fail |

Base reaches the permanent drawdown HALT on **2026-03-25**. Stress reaches it on **2025-06-24**.

### 4.2 Production economics

| Full recent | Baseline Base | MPV Base | Baseline Stress | MPV Stress |
|---|---:|---:|---:|---:|
| gross signal PnL | 1,786,425 | **-53,090** | 700,245 | **-2,310** |
| turnover | ~450.6m | **149,145,650** | 256,918,290 | **75,801,730** |
| transaction cost | 225,282.575 | **74,572.825** | 385,377.435 | **113,702.595** |
| net alpha PnL | 1,561,142.425 | **-127,662.825** | 314,867.565 | **-116,012.595** |
| Net Alpha / Turnover | 34.6485 bps | **-8.5596 bps** | 12.2556 bps | **-15.3047 bps** |

The candidate succeeds at reducing turnover and cost but destroys much more gross Alpha than it saves. This is not an execution-cost win hidden by headline return; the realized Production net economics themselves become negative.

### 4.3 Causal windows

Annualized returns:

| Window | Base baseline | Base MPV | Stress baseline | Stress MPV |
|---|---:|---:|---:|---:|
| prior1 | -28.9394% | **-5.1743%** | -29.7096% | **-4.4865%** |
| prior2 | -14.4991% | **+12.7962%** | -29.7319% | **-13.9086%** |
| train | +24.0523% | **-11.3011%** | -8.5133% | **-24.0358%** |
| validation | +229.6523% | **-2.5110%** | +88.4423% | **+84.6286%** |
| OOS | +69.2576% | **-6.2575%** | +74.5376% | **-3.9030%** |
| full_recent | +109.0636% | **-14.2295%** | +28.9559% | **-12.8425%** |

The apparently better prior1/prior2 headline is not evidence of robust selection: those paths trade very little. For example, prior1 has only **5 active days** in both Base and Stress; Base validation has only **2 active days**. OOS is negative under both cost regimes.

## 5. Failure mechanism: self-extinguishing Production feedback

The key negative result is structural.

v1 learns only from the candidate's own completed Production exposure. When early realized evidence for a product becomes weak or negative, the optimizer can reduce that product to zero. Once exposure is removed, the candidate stops generating new product-level Production PnL evidence. The estimate therefore cannot observe a later regime recovery unless some independent mechanism reintroduces enough exposure.

This creates a self-reinforcing loop:

```text
early adverse realized product outcome
-> lower estimated MPV
-> integer exposure removed
-> no new realized product outcome evidence
-> stale low MPV persists
-> future high-Alpha regime is missed
```

The full_recent capacity path exposes this directly:

- Base: **209 / 484** days have zero final target gross; average realized gross falls to about **0.618x**.
- Stress: **315 / 484** days have zero final target gross and **317 / 484** days have zero realized gross; average realized gross falls to about **0.358x** versus the validated ~**1.175x** baseline.
- Candidate losses also push the existing completed-return governor defensive more often, further reducing risk capital. This is valid existing risk behavior, not a governor bug.

The candidate therefore does not solve the original problem of converting theoretical Alpha into Production risk capital; it makes the conversion failure much worse.

## 6. Destruction of known high-value Production exposures

The failure is not explained by a few low-quality products.

Stress gross PnL by major baseline contributor:

| Product | Baseline gross PnL | MPV v1 gross PnL | Delta |
|---|---:|---:|---:|
| AG | +354,525 | **-4,545** | **-359,070** |
| LU | +123,150 | **+4,020** | **-119,130** |
| JM | +95,220 | **-18,630** | **-113,850** |
| EB | +36,505 | **-1,415** | **-37,920** |
| AL | +32,800 | **+525** | **-32,275** |

The candidate does remove some historical losers, for example it reduces the damage from `L`, `AP` and `HC`. That benefit is overwhelmed by starving the products that later produce most of the validated Production Alpha.

For AG specifically, validated Stress records **242** PnL lot-segments and +354,525 gross PnL. MPV v1 leaves only **6** AG lot-segments and -4,545 gross PnL. The candidate therefore has no basis for claiming that recent realized product competence is a stable marginal-value forecast.

## 7. Interpretation

The pure optimizer itself is not disproven by this run. What fails is the v1 forecast used to rank its discrete moves.

The negative evidence establishes several constraints for any future MPV attempt:

1. **Do not learn solely from the candidate's own selected Production outcomes without an exploration-independent information source.** Selection changes the future evidence distribution.
2. Product-only expanding realized PnL is too coarse. Direction / Alpha-family / state information may matter, but adding them without independent evidence would worsen sparsity and must not become a large product x family parameter table.
3. A Production objective cannot manufacture marginal value for correlation, concentration, liquidity or margin opportunity cost by assigning arbitrary historical-fit weights. Those components remain explicit but unpriced until defensible PIT evidence exists.
4. The next research route should add an information set that continues to update even when the strategy is flat, rather than tuning the failed feedback estimator.

This points toward exogenous point-in-time futures information such as the **complete same-product term structure / OI migration** available in the frozen concrete-contract daily history, not another threshold rescue of MPV v1.

## 8. Engineering verification

Before fixed L3:

- real repository CI run `32680699700`: Python 3.10 / 3.13 full CI green;
- latest pre-L3 branch CI run `32681002767`: Python 3.10 / 3.13 full CI green;
- fixed L3 run `32681002779`: targeted MPV tests, exact artifact SHA checks, candidate simulation and evidence upload all completed successfully.

The failed result is therefore an economic rejection, not an unverified implementation or broken risk-gate result.

## 9. Production disposition

**Rejected — validated Production remains unchanged.**

No `directional_runtime.py`, `execution_aligned_runtime.py`, `runtime_factory.py`, RiskManager, broker truth path, governor threshold, leverage cap, margin cap, available floor or max-lot cap is changed by this candidate.

Research MPV primitives, deterministic integer optimization, explicit attribution/counterfactual boundaries, and this negative evidence may be retained as research infrastructure. The temporary fixed-L3 workflow must be removed before final merge.
