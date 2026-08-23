# Directional Stress Robustness Design

## Goal

Improve the frozen execution-aligned directional portfolio's production Stress robustness without increasing leverage or relaxing any account hard gate. Historical acceptance targets are Base annualized return >=100% and Stress 15bp annualized return >=80% when honestly attainable; they are not future-return guarantees.

## Non-negotiable constraints

- target and realized gross leverage remain <=2.0x;
- max daily loss remains 5%;
- max total drawdown remains 30%;
- max margin ratio remains 35%;
- minimum available ratio remains 25%;
- directional max contract volume remains 35;
- no historical broker-margin truth is claimed where only a proxy exists;
- no large template/parameter sweep over the already-observed two-year window;
- causal signal, completed-return governor, reduction-first execution, Broker truth and fail-closed semantics remain intact.

## P0: margin-aware target sizing

The current signal may legitimately request up to 2.0x gross, but the final integer-lot target must also be feasible under the account's margin and cash-reserve limits. Add a deterministic shared primitive that receives requested signed lots, positive per-lot margin estimates and a margin budget. It may only reduce absolute lots, must preserve sign, and must keep the portfolio shape as closely as integer lots allow.

The production runtime derives per-lot margin from the live ContractSpec long/short margin rate, current quote, contract multiplier and the existing margin estimate buffer. The production-mechanics proxy derives the same quantity from its explicit margin-rate proxy. The feasible total-margin budget is equity multiplied by the stricter of max_margin_ratio and 1-min_available_ratio. Existing RiskManager opening checks and account hard gates remain unchanged as independent fail-closed guards.

This removes the structural Stress contradiction in which a 2.0x target at 15% proxy margin and 1.25 buffer implies 37.5% margin against a 35% hard cap.

## P1/P2: cost-aware robust meta allocation

Keep the frozen 96-template pool, 11-day lookback, 3-day meta rebalance and 3 active templates. Do not search new parameter grids.

For each template, calculate the same causal continuous open-to-close evidence at both 5bp and 15bp one-way cost. Compute the existing annualized-plus-Sharpe trailing score independently for each endpoint. A template is meta-eligible only when it has finite/positive evidence at both endpoints; its robust score is the equal-weight mean of the Base and Stress endpoint scores.

This has two effects without adding a separately tuned turnover parameter:

1. templates whose apparent edge is consumed by higher transaction cost lose rank or eligibility;
2. lower-turnover templates retain more of their score at 15bp and are favored when economic quality is otherwise comparable.

The same production policy implementation remains the sole source of weights for the L4 evaluator and production-mechanics acceptance.

## Validation

Use progressive validation:

1. L1 RED/GREEN tests for margin-budget fitting, acceptance sizing and robust meta scoring;
2. L2 directional subsystem tests and compileall;
3. L3 rerun the fixed production-mechanics dataset with Base 5bp/12% and Stress 15bp/15%, preserving all hard gates and publishing active days, HALT state, margin rejects, drawdown, realized gross and turnover evidence;
4. only after a stable final candidate, restore the normal repository CI, remove temporary targeted workflows, open a PR, run full Python 3.10/3.13 CI once, then squash-merge to main.

If the fixed historical Stress result remains below 80% after the bounded robust changes, retain the best causally justified version only if it improves robustness without breaking Base/hard gates, and document the miss instead of relaxing constraints or sweeping parameters until a target is manufactured.
