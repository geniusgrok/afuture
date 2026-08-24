"""Fixed Production L3 for cost-approved survivor risk-budget reallocation.

Composition is fixed and parameter-free beyond already frozen components:
9-product D->D+1 60m OI confirmation -> archived 20/3/15bp cost filter ->
proportional survivor reallocation back to the original OI gross -> freeze-new-risk
Production mechanics. The reallocation cannot add products, flip signs, exceed original
gross, exceed 2x, or alter the hard account gates.
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
import evaluate_directional_oi_freeze_cost_combo as suppress
from afuture.directional_cost_aware_no_trade import apply_cost_aware_no_trade
from afuture.directional_cost_survivor_reallocation import (
    renormalize_survivor_risk_budget,
)

CHAMPION_BASE = 1.21132378
CHAMPION_STRESS = 0.68059063
LINEAGE_TOLERANCE = 5e-5


def _turnover(weights: pd.DataFrame) -> float:
    values = weights.diff().abs().sum(axis=1)
    if len(values):
        values.iloc[0] = float(weights.iloc[0].abs().sum())
    return float(values.sum())


def build_reallocated_weights(
    *,
    base_weights: pd.DataFrame,
    bars_60m: pd.DataFrame,
    close_prices: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    oi_weights, _ = oi_gate.build_candidate_weights(base_weights, bars_60m)
    approved = apply_cost_aware_no_trade(
        weights=oi_weights,
        close_prices=close_prices,
    )
    reallocated = renormalize_survivor_risk_budget(
        original_weights=oi_weights,
        approved_weights=approved,
    )
    return oi_weights, approved, reallocated


def evaluate(
    *,
    specific_raw: pd.DataFrame,
    continuous_raw: pd.DataFrame,
    base_weights: pd.DataFrame,
    bars_60m: pd.DataFrame,
) -> dict:
    champion_report = champion.evaluate(
        specific_raw=specific_raw,
        base_weights=base_weights,
        bars_60m=bars_60m,
    )
    champion_base = float(
        champion_report["candidate"]["base"]["windows"]["full_recent"]["annualized_return"]
    )
    champion_stress = float(
        champion_report["candidate"]["stress"]["windows"]["full_recent"]["annualized_return"]
    )
    champion_lineage = {
        "base": abs(champion_base - CHAMPION_BASE) <= LINEAGE_TOLERANCE,
        "stress": abs(champion_stress - CHAMPION_STRESS) <= LINEAGE_TOLERANCE,
    }
    if not all(champion_lineage.values()):
        raise RuntimeError(
            f"merged Stress68 checkpoint reproduction failed: {champion_lineage}"
        )

    close = suppress.continuous_close_panel(
        continuous_raw, list(base_weights.columns)
    )
    oi_weights, approved_weights, candidate_weights = build_reallocated_weights(
        base_weights=base_weights,
        bars_60m=bars_60m,
        close_prices=close,
    )
    candidate, economics = suppress._simulate(
        specific_raw=specific_raw,
        weights=candidate_weights,
    )
    promotion = champion._promotion_gate(
        candidate["base"], candidate["stress"], economics["stress"]
    )
    stress_full = float(
        candidate["stress"]["windows"]["full_recent"]["annualized_return"]
    )

    return {
        "role": "fixed OI + cost-approved survivor reallocation + freeze Production L3",
        "parameter_search": False,
        "production_wiring": False,
        "order": [
            "9_product_60m_oi_confirmation",
            "20_3_15bp_cost_filter",
            "proportional_survivor_reallocation_to_original_oi_gross",
            "freeze_new_risk",
        ],
        "champion_lineage_reproduction": champion_lineage,
        "champion_reference": {
            "base_full_recent": champion_base,
            "stress_full_recent": champion_stress,
        },
        "candidate": candidate,
        "economics": economics,
        "weight_path": {
            "oi_turnover_full_path": _turnover(oi_weights),
            "approved_turnover_full_path": _turnover(approved_weights),
            "reallocated_turnover_full_path": _turnover(candidate_weights),
            "oi_average_gross_full_recent": float(
                oi_weights.loc["2024-08-21":"2026-08-20"].abs().sum(axis=1).mean()
            ),
            "approved_average_gross_full_recent": float(
                approved_weights.loc["2024-08-21":"2026-08-20"].abs().sum(axis=1).mean()
            ),
            "reallocated_average_gross_full_recent": float(
                candidate_weights.loc["2024-08-21":"2026-08-20"].abs().sum(axis=1).mean()
            ),
        },
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
        raise SystemExit(f"survivor-reallocation inputs missing: {missing}")

    report = evaluate(
        specific_raw=pd.read_csv(paths["specific"]),
        continuous_raw=pd.read_csv(paths["continuous"]),
        base_weights=oi_gate.load_frozen_weights(paths["weights"]),
        bars_60m=oi_gate.load_60m([paths["prior"], paths["recent"]]),
    )
    output = runtime / "stress80_oi_freeze_cost_reallocation_report.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
