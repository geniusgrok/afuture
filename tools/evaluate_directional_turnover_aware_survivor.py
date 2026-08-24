"""Fixed Production L3 for lexicographic turnover-aware survivor reallocation.

Composition is fixed and parameter-free:
9-product D->D+1 60m OI confirmation -> archived 20/3/15bp cost eligibility ->
tracking-first / turnover-second survivor reallocation back to the original OI gross ->
freeze-new-risk Production mechanics.

The survivor set and target signs are identical to the already-tested proportional
survivor candidate. This candidate changes only how rejected gross is distributed among
those survivors; it performs no parameter search and cannot exceed the original OI gross.
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
import evaluate_directional_60m_oi_freeze as checkpoint
import evaluate_directional_oi_freeze_cost_combo as suppress
from afuture.directional_cost_aware_no_trade import apply_cost_aware_no_trade
from afuture.directional_turnover_aware_survivor_reallocation import (
    reallocate_survivors_lexicographically,
)

CHECKPOINT_BASE = 1.21132378
CHECKPOINT_STRESS = 0.68059063
SURVIVOR_STRESS_REFERENCE = 0.71703271
LINEAGE_TOLERANCE = 5e-5


def _turnover(weights: pd.DataFrame) -> float:
    values = weights.diff().abs().sum(axis=1)
    if len(values):
        values.iloc[0] = float(weights.iloc[0].abs().sum())
    return float(values.sum())


def build_turnover_aware_weights(
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
    candidate = reallocate_survivors_lexicographically(
        original_weights=oi_weights,
        approved_weights=approved,
    )
    return oi_weights, approved, candidate


def evaluate(
    *,
    specific_raw: pd.DataFrame,
    continuous_raw: pd.DataFrame,
    base_weights: pd.DataFrame,
    bars_60m: pd.DataFrame,
) -> dict:
    checkpoint_report = checkpoint.evaluate(
        specific_raw=specific_raw,
        base_weights=base_weights,
        bars_60m=bars_60m,
    )
    checkpoint_base = float(
        checkpoint_report["candidate"]["base"]["windows"]["full_recent"][
            "annualized_return"
        ]
    )
    checkpoint_stress = float(
        checkpoint_report["candidate"]["stress"]["windows"]["full_recent"][
            "annualized_return"
        ]
    )
    lineage = {
        "base": abs(checkpoint_base - CHECKPOINT_BASE) <= LINEAGE_TOLERANCE,
        "stress": abs(checkpoint_stress - CHECKPOINT_STRESS) <= LINEAGE_TOLERANCE,
    }
    if not all(lineage.values()):
        raise RuntimeError(f"Stress68 checkpoint reproduction failed: {lineage}")

    close = suppress.continuous_close_panel(
        continuous_raw, list(base_weights.columns)
    )
    oi_weights, approved_weights, candidate_weights = build_turnover_aware_weights(
        base_weights=base_weights,
        bars_60m=bars_60m,
        close_prices=close,
    )

    # A cheap structural gate is diagnostic only; no threshold is selected from it.
    cheap = oi_gate._cheap_report(specific_raw, candidate_weights)
    candidate, economics = suppress._simulate(
        specific_raw=specific_raw,
        weights=candidate_weights,
    )
    promotion = checkpoint._promotion_gate(
        candidate["base"], candidate["stress"], economics["stress"]
    )
    stress_full = float(
        candidate["stress"]["windows"]["full_recent"]["annualized_return"]
    )

    return {
        "role": "fixed turnover-aware cost-approved survivor Production L3",
        "parameter_search": False,
        "production_wiring": False,
        "order": [
            "9_product_60m_oi_confirmation",
            "20_3_15bp_cost_eligibility",
            "original_oi_tracking_first",
            "previous_target_turnover_second",
            "freeze_new_risk",
        ],
        "checkpoint_lineage_reproduction": lineage,
        "checkpoint_reference": {
            "base_full_recent": checkpoint_base,
            "stress_full_recent": checkpoint_stress,
        },
        "survivor_reference_stress_full_recent": SURVIVOR_STRESS_REFERENCE,
        "cheap": cheap,
        "candidate": candidate,
        "economics": economics,
        "weight_path": {
            "oi_turnover_full_path": _turnover(oi_weights),
            "approved_turnover_full_path": _turnover(approved_weights),
            "turnover_aware_turnover_full_path": _turnover(candidate_weights),
            "oi_average_gross_full_recent": float(
                oi_weights.loc["2024-08-21":"2026-08-20"]
                .abs()
                .sum(axis=1)
                .mean()
            ),
            "approved_average_gross_full_recent": float(
                approved_weights.loc["2024-08-21":"2026-08-20"]
                .abs()
                .sum(axis=1)
                .mean()
            ),
            "turnover_aware_average_gross_full_recent": float(
                candidate_weights.loc["2024-08-21":"2026-08-20"]
                .abs()
                .sum(axis=1)
                .mean()
            ),
        },
        "delta_vs_survivor_stress_annualized": (
            stress_full - SURVIVOR_STRESS_REFERENCE
        ),
        "promotion_gate": promotion,
        "constraints": checkpoint_report["constraints"],
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
        raise SystemExit(f"turnover-aware survivor inputs missing: {missing}")

    report = evaluate(
        specific_raw=pd.read_csv(paths["specific"]),
        continuous_raw=pd.read_csv(paths["continuous"]),
        base_weights=oi_gate.load_frozen_weights(paths["weights"]),
        bars_60m=oi_gate.load_60m([paths["prior"], paths["recent"]]),
    )
    output = runtime / "stress80_turnover_aware_survivor_report.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
