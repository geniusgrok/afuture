"""Fixed Production L3 for 9-product OI confirmation + fixed cost filter + freeze.

No parameter is fitted here. The candidate composes three already-declared mechanisms in
this exact order:

1. frozen 9-product D->D+1 60m Price x OI confirmation;
2. archived 20-session / 3-session / 15bp cost-aware no-trade filter;
3. freeze-new-risk Production mechanics using the unchanged governor trigger.

Before interpreting the combined candidate, the evaluator freshly reproduces the merged
68.059063% Stress checkpoint on the same frozen inputs.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pandas as pd

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import evaluate_directional_60m_oi_confirmation as oi_gate
import evaluate_directional_60m_oi_freeze as champion
import evaluate_directional_production_mechanics as mechanics
from afuture.directional_acceptance import ProductionMechanicsConfig
from afuture.directional_cost_aware_no_trade import apply_cost_aware_no_trade
from afuture.directional_freeze_new_risk import (
    FreezeNewRiskDirectionalProductionAcceptance,
)

CHAMPION_BASE = 1.21132378
CHAMPION_STRESS = 0.68059063
LINEAGE_TOLERANCE = 5e-5


def continuous_close_panel(raw: pd.DataFrame, products: list[str]) -> pd.DataFrame:
    """Build the same completed daily close panel used by the archived cost candidate."""
    frame = raw.copy()
    required = {"date", "product", "close"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"continuous close history missing columns: {sorted(missing)}")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    frame["product"] = frame["product"].astype(str).str.upper()
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["date", "product", "close"])
    frame = frame[frame["close"] > 0.0]
    frame = frame.drop_duplicates(["date", "product"], keep="last")
    close = frame.pivot(index="date", columns="product", values="close").sort_index()
    missing_products = sorted(set(products) - set(close.columns))
    if missing_products:
        raise ValueError(f"continuous close history missing products: {missing_products}")
    return close.reindex(columns=products).astype(float)


def build_combined_weights(
    *,
    base_weights: pd.DataFrame,
    bars_60m: pd.DataFrame,
    close_prices: pd.DataFrame,
) -> pd.DataFrame:
    """Apply OI confirmation first, then the frozen cost-aware suppress-only layer."""
    oi_weights, _ = oi_gate.build_candidate_weights(base_weights, bars_60m)
    combined = apply_cost_aware_no_trade(
        weights=oi_weights,
        close_prices=close_prices,
    )
    combined = combined.reindex(
        index=base_weights.index,
        columns=base_weights.columns,
        fill_value=0.0,
    ).fillna(0.0)
    if bool(
        (
            combined.abs().sum(axis=1)
            > base_weights.abs().sum(axis=1) + 1e-10
        ).any()
    ):
        raise AssertionError("OI + cost filter increased frozen baseline gross")
    return combined.astype(float)


def _simulate(
    *,
    specific_raw: pd.DataFrame,
    weights: pd.DataFrame,
) -> tuple[dict, dict]:
    base_sim = FreezeNewRiskDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=mechanics.INITIAL_CAPITAL,
            margin_rate_proxy=mechanics.BASE_MARGIN_PROXY,
        )
    )
    stress_sim = FreezeNewRiskDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=mechanics.INITIAL_CAPITAL,
            margin_rate_proxy=mechanics.STRESS_MARGIN_PROXY,
        )
    )
    prepared = base_sim.prepare_contracts(specific_raw)
    base, base_daily, base_events = mechanics._simulation_report(
        base_sim,
        specific_raw,
        weights,
        prepared=prepared,
        cost_bps=mechanics.BASE_COST_BPS,
        margin_rate_proxy=mechanics.BASE_MARGIN_PROXY,
    )
    stress, stress_daily, stress_events = mechanics._simulation_report(
        stress_sim,
        specific_raw,
        weights,
        prepared=prepared,
        cost_bps=mechanics.STRESS_COST_BPS,
        margin_rate_proxy=mechanics.STRESS_MARGIN_PROXY,
    )
    economics = {
        "base": champion._economics(
            base_daily, base_events, cost_bps=mechanics.BASE_COST_BPS
        ),
        "stress": champion._economics(
            stress_daily, stress_events, cost_bps=mechanics.STRESS_COST_BPS
        ),
    }
    return {"base": base, "stress": stress}, economics


def evaluate(
    *,
    specific_raw: pd.DataFrame,
    continuous_raw: pd.DataFrame,
    base_weights: pd.DataFrame,
    bars_60m: pd.DataFrame,
) -> dict:
    # Freshly reproduce the exact merged checkpoint before evaluating the extra filter.
    champion_report = champion.evaluate(
        specific_raw=specific_raw,
        base_weights=base_weights,
        bars_60m=bars_60m,
    )
    champion_base = float(
        champion_report["candidate"]["base"]["windows"]["full_recent"][
            "annualized_return"
        ]
    )
    champion_stress = float(
        champion_report["candidate"]["stress"]["windows"]["full_recent"][
            "annualized_return"
        ]
    )
    champion_lineage = {
        "base": abs(champion_base - CHAMPION_BASE) <= LINEAGE_TOLERANCE,
        "stress": abs(champion_stress - CHAMPION_STRESS) <= LINEAGE_TOLERANCE,
    }
    if not all(champion_lineage.values()):
        raise RuntimeError(
            f"merged Stress68 checkpoint reproduction failed: {champion_lineage}"
        )

    close = continuous_close_panel(continuous_raw, list(base_weights.columns))
    combined_weights = build_combined_weights(
        base_weights=base_weights,
        bars_60m=bars_60m,
        close_prices=close,
    )
    candidate, economics = _simulate(
        specific_raw=specific_raw,
        weights=combined_weights,
    )
    promotion = champion._promotion_gate(
        candidate["base"], candidate["stress"], economics["stress"]
    )
    stress_full = float(
        candidate["stress"]["windows"]["full_recent"]["annualized_return"]
    )

    weight_turnover = combined_weights.diff().abs().sum(axis=1)
    if len(weight_turnover):
        weight_turnover.iloc[0] = float(combined_weights.iloc[0].abs().sum())

    return {
        "role": "fixed 9-product OI + archived cost-aware no-trade + freeze Production L3",
        "parameter_search": False,
        "production_wiring": False,
        "order": ["9_product_60m_oi_confirmation", "20_3_15bp_cost_filter", "freeze_new_risk"],
        "cost_rule": {
            "completed_return_sessions": 20,
            "benefit_horizon_sessions": 3,
            "one_way_hurdle_bps": 15.0,
            "parameters_changed": False,
        },
        "champion_lineage_reproduction": champion_lineage,
        "champion_reference": {
            "base_full_recent": champion_base,
            "stress_full_recent": champion_stress,
        },
        "candidate": candidate,
        "economics": economics,
        "candidate_weight_turnover_full_path": float(weight_turnover.sum()),
        "delta_vs_champion_stress_annualized": stress_full - CHAMPION_STRESS,
        "promotion_gate": promotion,
        "constraints": champion_report["constraints"],
    }


def main() -> None:
    runtime = Path("runtime")
    paths = {
        "specific": runtime / "return_target_specific_contracts.csv",
        "continuous": runtime / "broad_daily_universe.csv",
        "weights": runtime / "execution_aligned_weights.csv",
        "prior": runtime / "prior_two_year_broad_60m.csv",
        "recent": runtime / "two_year_broad_60m.csv",
    }
    missing = [str(path) for path in paths.values() if not path.exists()]
    if missing:
        raise SystemExit(f"OI + cost + freeze inputs missing: {missing}")

    report = evaluate(
        specific_raw=pd.read_csv(paths["specific"]),
        continuous_raw=pd.read_csv(paths["continuous"]),
        base_weights=oi_gate.load_frozen_weights(paths["weights"]),
        bars_60m=oi_gate.load_60m([paths["prior"], paths["recent"]]),
    )
    output = runtime / "stress80_oi_freeze_cost_production_report.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
