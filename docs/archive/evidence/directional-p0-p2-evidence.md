# Directional P0-P2 Alpha / Risk-Capital Evidence

> **Historical record.** Preserved for research lineage and rejected-route evidence; see [`documentation-index.md`](../../documentation-index.md) for current authority.

Date: 2026-08-23

## Scope

This phase starts from `main@d1ba277004904f6d0d2d2a1b302d50ca9127c4a6` (PR #15). All hard production gates remain unchanged: target/realized gross <=2x, margin <=35%, available >=25%, daily loss 5%, total drawdown 30%, max 35 lots, Broker/CTP account truth, reduction-first, circuit/halt semantics and fail-closed behavior.

The originally proposed 5-10 year minute/OI/curve warehouse was explicitly removed from scope because its resource/time cost was not justified. Futures-specific research below therefore uses only the already-available daily evidence, plus a bounded eight-root daily universe screen.

## P0-1 — integer risk-capital projection

Candidate: replace proportional margin fitting with a deterministic same-sign integer allocator that greedily maximizes reduction in squared target-notional tracking error per unit margin. It cannot exceed requested lots, cannot flip sign, uses the unchanged soft-margin budget, and falls back to the proportional projection whenever current-position turnover would increase.

Targeted live/acceptance parity and risk tests passed, but fixed Stress Production rejected the candidate. Workflow `32650103516`, artifact `9495961918`, SHA-256 `395116fcd0b8b03fb6a89e82eb45fc31ff3fe83f820ca2a2958c6b261312d61e`:

| Metric | Baseline | Candidate |
|---|---:|---:|
| Stress annualized | 28.9559% | **17.9238%** |
| Total return | 62.9735% | **37.2524%** |
| Max DD | 28.1152% | **28.0417%** |
| Sharpe | 0.9604 | **0.6767** |
| Active days | 474/484 | **472/484** |
| Max gross | 1.668769x | **1.664496x** |
| Margin rejects | 0 | **0** |
| Permanent HALT | false | **false** |
| Turnover notional | 256,918,290 | **240,725,360** |

Turnover fell about 6.3%, but the allocator removed economically valuable exposures and materially reduced Stress return. The production patch was therefore rejected; no alternative tracking objective or allocator parameter search followed.

## P0-2 — selected-template consensus capacity allocation — REJECT

Candidate used only the already-selected META_COUNT=3 templates. Per-product consensus was the absolute signed-vote mean; it used no future return and only changed allocation order when capacity was binding.

15bp proxy annualized delta versus proportional capacity allocation:

| Window | Delta |
|---|---:|
| prior1 | +0.7699 pp |
| prior2 | -4.7973 pp |
| train | -1.3875 pp |
| validation | -25.1636 pp |
| OOS | -3.6559 pp |
| full_recent | -4.3139 pp |

Turnover also increased in most relevant windows. The candidate was reverted before production integration; no consensus threshold/grid was attempted.

## P1-1 — regime-aware new-risk scalar — REJECT

Predeclared completed-history scalar used 20/60 trend agreement breadth, cross-sectional 20-day momentum dispersion, average 20-day correlation, and 20-day realized volatility. Missing history reduced rather than expanded risk. It never used future regime labels.

15bp annualized delta versus frozen weights:

| Window | Delta |
|---|---:|
| prior1 | +19.5074 pp |
| prior2 | +6.1156 pp |
| train | +0.8866 pp |
| validation | -206.8941 pp |
| OOS | +6.0209 pp |
| full_recent | -8.6201 pp |

The large validation opportunity-cost failure means the rule is not robust. No threshold or component-weight tuning followed.

## P1-2 — futures-specific daily Alpha — REJECT

No minute/session warehouse was built. Two distinct daily families were predeclared and evaluated at 15/20bp.

### Price × OI × volume confirmation

20-day price momentum required positive completed OI growth and completed short/long volume expansion, selecting a fixed top-five equal-gross sleeve every five sessions. At 15bp it was negative in every major window: prior1 -13.93%, prior2 -13.50%, train -28.66%, validation -17.24%, OOS -62.29%, full_recent -37.08%. Rejected.

### Predeclared relative-value spreads

Fixed pairs: RB/HC, M/RM, Y/P, J/JM, PP/L. A 60-session completed log-ratio mean-reversion signal was used without pair search.

15bp full_recent annualized results:

| Pair | Annualized | Decision |
|---|---:|---|
| RB/HC | -2.14% | reject |
| M/RM | +0.22% | reject: prior1/prior2/OOS unstable |
| Y/P | -10.20% | reject |
| J/JM | -12.29% | reject |
| PP/L | -9.63% | reject |

20bp evidence was weaker. Failed families were not combined.

## P2-1 — bounded high-liquidity universe expansion — REJECT

Only eight predeclared roots were fetched with daily continuous evidence: SC, AO, BR, EC, SI, LC, PS and LG. All eight passed the bounded coverage/liquidity screen; no large history warehouse was created.

At 15bp, full_recent annualized delta versus frozen-50 continuous next-open proxy:

| Root | Delta | Robustness conclusion |
|---|---:|---|
| AO | -11.26 pp | reject |
| BR | -12.19 pp | reject |
| EC | -61.26 pp | reject |
| LC | -60.40 pp | reject |
| LG | -4.18 pp | reject |
| PS | -39.32 pp | reject |
| SC | -7.03 pp | reject |
| SI | +5.94 pp | reject: prior2 -2.26 pp, OOS -24.01 pp, proxy DD >50% |

The same conclusion holds at 20bp (SI full_recent +4.50 pp but prior2/OOS worsen; all other roots reduce full_recent return). Because no root passes the cheap independent gate, no new concrete-contract download or production-universe change is justified.

P2 universe workflow `32650103485`, artifact `9495950216`, SHA-256 `1893a504ad7f2ac92a357610388ec3ad8826056b579e7c8a91b42a45b5eca34f`.

## P2-2 — depth-aware opening execution

Production execution optimization is intentionally narrow. Reduction orders keep the configured aggressive FAK price. Opening FAK orders use the best opposite quote only when displayed opposite L1 depth covers the full requested volume; otherwise they use the configured aggressive price. Daily price limits are still enforced. The change does not alter volume, order count, order type, gross, margin sizing, risk authority or reduction-first ordering.

Fixed 15bp historical Stress receives no artificial return credit for this live execution improvement. Its value is expected live slippage reduction; partial/rejected/tracking quality remains observable through the existing quality ledger.

Targeted runtime/quality/restart/surface verification: 27 tests pass. Broader affected P0/P2 L2 verification: 40 tests pass plus compileall.
