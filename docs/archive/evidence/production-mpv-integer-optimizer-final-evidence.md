# Production MPV / Integer Optimizer / PIT Information Final Evidence

> **Historical record.** Preserved for research lineage and rejected-route evidence; see [`documentation-index.md`](../../documentation-index.md) for the current checkpoint.

Date: 2026-08-24

## 1. Final decision

No new economic behavior from this research phase is promoted to Production.

Validated Production remains the PR #18 baseline (`main@d8a4158cadbea36ff7aa9e76dd7d562b218708c3`):

- Base annualized return **109.0636%**, max drawdown **15.8529%**, Sharpe **2.0976**;
- Stress 15bp annualized return **28.9559%**, max drawdown **28.1152%**, Sharpe **0.9604**;
- Stress gross signal PnL **700,245**;
- Stress turnover **256,918,290**;
- Stress transaction cost **385,377.435**;
- Stress net alpha PnL **314,867.565**;
- Stress Net Alpha / Turnover **12.2556 bps**;
- target / realized gross <= **2x**;
- hard margin <= **35%**;
- available >= **25%**;
- max abs contract lots <= **35**;
- reduction-first, Broker/CTP truth, RiskManager, circuit and HALT authority unchanged.

The retained code is research/audit infrastructure only. `runtime_factory`, directional live runtime, RiskManager and Broker truth paths are not wired to MPV, the research integer optimizer, or basis Alpha.

## 2. Fixed evidence lineage

Fixed Production input:

- artifact `9473260618`;
- SHA-256 `ab4321a9cf8b9a61a0f9d435a6fb6bba7dd63986043b429c12c0ff47fd15389c`.

Validated baseline / frozen execution-aligned weights:

- artifact `9491959916`;
- SHA-256 `e531f2874cbc26c3a54ff561e074f59b86ac887a1f509e33effd6908eaa3144d`.

The research evaluators reuse these frozen inputs. They do not refit the 96-template Meta policy or change the Production hard-risk envelope.

## 3. P0 — Production-aligned MPV and integer allocation

### 3.1 What was implemented

The P0 research path separates:

1. `directional_mpv.py`: decision-side monetary marginal-value components and causal input validation;
2. `directional_integer_optimizer.py`: deterministic +/-1-lot local improvement and strictly positive two-leg capacity swaps under raw directional intent, the existing soft-margin envelope, gross <=2x and max 35 lots;
3. `directional_mpv_attribution.py`: completed Production attribution plus explicitly future-only counterfactual labels;
4. `directional_mpv_research.py`: a low-degree-of-freedom causal forecast bridge;
5. `directional_mpv_robustness.py`: research-only Production-mechanics adapter with exact fallback to the validated target construction;
6. `tools/evaluate_directional_mpv_production.py`: fixed-input Production L3 reproduction.

The first forecast candidate intentionally avoided hidden factor fitting:

- only already-completed Production `intraday` PnL is decision evidence;
- support is exact `sum(abs(lots_before))` lot-segment exposure;
- product gross Alpha per lot-segment is shrunk toward completed global evidence;
- shrinkage prior support is the cross-sectional median observed support, not a searched constant;
- current turnover cost is charged from the actual target-vs-current integer transition;
- unsupported correlation, concentration, downside and margin-opportunity penalty weights are not fabricated;
- future counterfactual labels never enter decision code.

### 3.2 Initial diagnostic L3

Run `32681002779`, artifact `9504222857`, digest `sha256:e748ff0a2d7c54256dd473f5302d91966d972c54d28450468f8de9858e8fe5e8` exposed a severe self-extinguishing path when each published account window also began with no estimator history:

- Base annualized **-14.2295%**, permanent HALT;
- Stress annualized **-12.8425%**, permanent HALT;
- Stress Net Alpha / Turnover **-15.3047 bps**.

This run is retained as a diagnostic, not the final correctness gate, because the research evaluator subsequently fixed the model-evidence reset semantics while preserving independent account resets.

### 3.3 Corrected causal-warmup L3 — final P0 result

The corrected evaluator keeps account capital, positions, governor and high-watermark independent per published window, but permits only candidate events with `event_date < first_decision_date` to seed that window's estimator.

Final run `32684064251`, artifact `9505190390`, digest `sha256:512ba1054930839202bda1cf807f35fabf75ae32e01850ac7de92a94f7c63abc` completed successfully.

The single chronological candidate training path generated only **13 Base events** and **13 Stress events**, then stopped generating useful exposure evidence. Neither training path halted; the failure is economic/informational, not a hard-risk implementation bug.

| Window | Base MPV annualized | Base active days | Stress MPV annualized | Stress active days |
|---|---:|---:|---:|---:|
| prior1 | **-5.1743%** | 5 | **-4.4865%** | 5 |
| prior2 | **0.0000%** | 0 | **0.0000%** | 0 |
| train | **0.0000%** | 0 | **0.0000%** | 0 |
| validation | **0.0000%** | 0 | **0.0000%** | 0 |
| OOS | **0.0000%** | 0 | **0.0000%** | 0 |
| full_recent | **0.0000%** | 0 | **0.0000%** | 0 |

Full-recent candidate gross PnL, turnover, transaction cost and net alpha are all zero under both Base and Stress because the estimator has already extinguished its own opportunity to learn.

### 3.4 Root cause: endogenous off-policy censoring

The P0 rejection is structural, not a parameter miss.

```text
early adverse candidate-owned product outcomes
-> lower estimated MPV
-> integer exposure removed
-> future product outcomes are no longer observed by the candidate
-> stale estimate persists
-> later profitable regimes cannot update the estimate
```

A candidate that learns marginal value only from the exposures it chooses to carry changes its own future evidence distribution. Once it removes a product, there is no exploration-independent observation to tell it that the product's edge recovered.

This is exactly the wrong architecture for the stated objective of converting more theoretical Alpha into Production risk capital. Retuning shrinkage, minimum observations, thresholds or allocation fractions would only fit the same endogenous feedback path.

Therefore:

- no P0 parameter rescue is attempted;
- Product x Alpha allocation and selective defensive allocation are not promoted, because their prerequisite reliable MPV layer is absent;
- the pure integer optimizer remains a research primitive, but no current forecast has earned the right to control Production capital.

## 4. New PIT information probe

A bounded source-coverage probe used AKShare `1.18.84` on 15 deterministic dates: three dates in each of prior1, prior2, train, validation and OOS.

Run `32681972115`, artifact `9504424318`, digest `sha256:4118bad5d7b6a6b6692c3d7ea829ee4e73f87c0f9d3a660a3ebeb3135127d7a2`.

Results:

| Source | Successful / 15 | Non-empty / 15 | Conclusion |
|---|---:|---:|---|
| real spot / dominant futures basis | **15** | **15** | usable bounded PIT source |
| registered receipt / warehouse receipt API | **0** | **0** | current interface failed with `JSONDecodeError`; do not fabricate inventory history |
| member rank aggregate API | **0** | **0** | current interface failed with `BadZipFile`; do not fabricate member-position history |

The basis probe covered 42 of the frozen 50 products across the sampled windows. The receipt/member failures are data-interface failures in the bounded probe, not proof that exchange history does not exist. They are insufficient evidence for Alpha research in this phase.

## 5. Candidate C — corrected Dominant Basis Carry

### 5.1 Causal rule

Actual fetched data established the AKShare field semantics:

`dom_basis_rate = dominant_futures_price / spot_price - 1`.

The first basis implementation interpreted the sign incorrectly. Correcting that semantic bug is a data-definition fix, not post-result parameter tuning.

The final predeclared rule is deliberately parameter-free:

```text
D completed carry direction = -sign(dom_basis_rate)
-> D+1 direction
-> equal gross across products with valid immediately-prior-session basis
-> total gross <= 2x
```

There is no threshold, lookback, percentile, cross-sectional ranking or basis+trend combination. Missing or stale evidence produces zero exposure.

### 5.2 Corrected cheap specific-contract screen

Authoritative corrected run `32689234528`, artifact `9506710944`, digest `sha256:f55fad8077e88734224d776c0c69ffe9a384e2448a5c4d33c74a9996c0a5d14d`.

The reused historical basis panel has SHA-256 `35d14e934ad2058f2ad99c600e8c09d3fef1e3cdaff43e20f4b8093da886462c`.

| Window | Base 5bp annualized | Stress 15bp annualized |
|---|---:|---:|
| prior1 | **+13.2693%** | **+3.6509%** |
| prior2 | **+0.2526%** | **-10.1619%** |
| train | **-0.7931%** | **-10.4931%** |
| validation | **-11.6961%** | **-21.0548%** |
| OOS | **+25.1717%** | **+8.8753%** |
| full_recent | **+2.2748%** | **-8.7844%** |

Additional full_recent evidence:

- Base Sharpe **0.2497**, max DD **11.3004%**;
- Stress Sharpe **-0.7201**, max DD **24.4252%**;
- weight-turnover sum **410.7387x**;
- median active products **42**;
- independent gate = **false**.

The candidate fails train, validation and multiple Stress windows. It is rejected before any Production combination. There is no sign inversion, threshold search, percentile search or basis+trend rescue.

## 6. Full-curve exploratory negative evidence

The frozen concrete-contract history itself is rich enough for a broader curve representation: 41/50 products have at least three eligible maturities on more than 80% of full_recent sessions.

One low-freedom exploratory screen used the completed full-curve `log(price) ~ time-to-delivery` slope plus the front-contract residual to the fitted curve, with D information trading D+1 only. It also failed independent evidence: at 15bp full_recent was about **-14.62%**, train **-19.14%**, validation **-22.97%**, while OOS was only about **+4.95%**.

It was not combined with Production and no curve threshold/grid followed.

## 7. Final Production and research boundary

Production remains exactly PR #18 behavior:

```text
completed continuous OHLC
-> frozen 96-template execution-aligned policy
-> completed-return governor
-> completed-activity concrete-contract selection
-> existing integer / margin-aware target construction
-> reduction-first
-> RiskManager hard gates
-> Broker / CTP fills and positions truth
```

Research-only retained surfaces:

- monetary MPV data model and causal validation;
- deterministic integer optimizer;
- explicit completed-event attribution / future-label separation;
- research-only MPV Production evaluator and adapter;
- corrected causal basis weight builder and cheap evaluator;
- tests and this negative evidence.

None of these research modules is a second account, risk, order, fill or position state machine. None is wired by `runtime_factory`.

P1 Product x Alpha / selective defensive allocation is not promoted because the P0 marginal-value prerequisite failed. P2 execution remains the already-validated depth-aware / quality-ledger behavior; no historical L1 return credit is invented.

## 8. Verification

Engineering verification completed on the real repository:

- CI run `32684064106`: full Python 3.10 / 3.13 repository CI green after the causal-evidence correctness fix;
- corrected MPV L3 run `32684064251`: success;
- corrected basis unit tests: 3 passed in workflow;
- corrected basis screen run `32689234528`: success and independent gate false.

The final stable tree removes temporary research workflows/planning artifacts and is accepted only after its normal Python 3.10 / 3.13 CI is green.
