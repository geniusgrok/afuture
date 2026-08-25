# Stress 68 OI + Freeze Checkpoint Evidence

> **Superseded checkpoint.** Preserved for lineage; later Stress-80 and Stress-90 evidence controls current research status. See [`documentation-index.md`](documentation-index.md).

## Status

This document preserves the strongest validated Production-mechanics checkpoint found during the Stress-80 research on 2026-08-24. It is intentionally a **checkpoint**, not the final Stress>=80% promotion and not live-runtime wiring.

The checkpoint combines:

1. a frozen 9-product 60-minute Price x OI confirmation overlay for `A, C, EG, I, M, P, PP, TA, Y`; and
2. a freeze-new-risk response to the existing directional governor trigger.

The 60m overlay has no fitted threshold or lookback. For completed day D, the dominant contract is selected by final open interest, then total volume, then symbol. Direction is `sign(first_open -> last_close)` only when final OI is greater than first OI. Evidence from D is available to the next frozen target session only. The overlay may suppress new risk or same-sign increases; it never creates exposure and never blocks reductions, exits or reversals.

The freeze response keeps the existing governor trigger semantics (completed loss >=2% or two-day sample volatility >=3%) but replaces global 0.25 target scaling with a freeze on new entries and same-sign increases. Reductions, exits, reversals and same-product rolls bypass the freeze. Existing hard gates remain authoritative.

## Frozen constraints

- target/realized gross <= 2.0x
- margin <= 35%
- available >= 25%
- daily loss limit = 5%
- total drawdown limit = 30%
- max contract lots = 35
- reduction-first semantics unchanged
- RiskManager/Broker/CTP authority unchanged

## Frozen data lineage

- concrete-contract Production input artifact: `9473260618`
  - ZIP SHA256: `ab4321a9cf8b9a61a0f9d435a6fb6bba7dd63986043b429c12c0ff47fd15389c`
- validated execution-aligned weights artifact: `9491959916`
  - ZIP SHA256: `e531f2874cbc26c3a54ff561e074f59b86ac887a1f509e33effd6908eaa3144d`
- prior 60m artifact: `9516473115`
  - ZIP SHA256: `d0adcfb348c5ae89ddb488474c088d74a2402a2426c0e84fee7877dda5bd2a4e`
- recent 60m artifact: `9516472727`
  - ZIP SHA256: `68ff2200eed8326741ce76f1cff46379c4e1dbc263297f4602bd84422e95e421`

## Fresh clean-tree verification

Promotion PR #22 was created from `main` commit `d5e1e35163544d56e158cfbcfd27b3691b62aa6b` with only the checkpoint implementation/tests/evaluators plus temporary verification workflow.

Fresh verification workflow run `32745738882` completed successfully. Artifact `9527268153` (`stress68-checkpoint-evidence`) has digest:

`sha256:fd19cc1c1736ee4f191e42380cfb4de7c7c6c025159b03eff5878972a212647a`

The run passed:

- focused OI-confirmation tests;
- focused freeze-new-risk tests;
- affected Production mechanics smoke;
- all four frozen artifact hash checks;
- fixed Production L3 reproduction;
- explicit checkpoint metric and hard-gate assertions.

Fresh fixed Production results:

| Metric | Base | Stress 15bp |
| --- | ---: | ---: |
| full_recent annualized | 121.132378% | 68.059063% |
| full_recent total return | 359.146537% | 171.037930% |
| full_recent max drawdown | -25.086693% | -27.748669% |
| full_recent Sharpe | 2.044592 | 1.443260 |
| max realized gross | 1.998743x | 1.674749x |
| margin reject days | 0 | 0 |
| halted | false | false |

Stress validation annualized: **397.123558%**, DD **-16.736520%**.

Stress OOS annualized: **35.960462%**, DD **-16.262395%**.

Stress economics:

- gross signal PnL: `1,304,825.00`
- turnover: `299,756,900.00`
- transaction cost: `449,635.35`
- net alpha: `855,189.65`
- net alpha / turnover: **28.529440 bps**

The OI-only lineage was reproduced exactly within the fixed evaluator tolerance: Base `104.613625%`, Stress `39.609479%`.

## Important limitations

This checkpoint does **not** satisfy the final Stress>=80% goal. Its evaluator correctly reports the failed gate `stress_full_recent_below_80pct`.

Earlier prior windows are also weak and must not be hidden:

- Stress prior1 annualized: `-31.779330%`, max drawdown `-30.475076%`, halted=true.
- Stress prior2 annualized: `-29.795015%`, max drawdown `-30.477598%`, halted=true.
- Base prior1/prior2 are also negative and halted.

Therefore this checkpoint is preserved on `main` as reproducible research/Production-mechanics evidence only. Live runtime wiring remains unchanged. Research toward Stress>=80%, stronger cross-regime robustness and eventual live promotion continues from this checkpoint.
