"""Fixed Production L3 for survivor reallocation under exact 35% hard-feasible sizing.

No margin-share search is performed. The only tested value is derived from the frozen
hard constraints: ``min(35% max margin, 1-25% min available) = 35%``. The candidate uses
9-product OI confirmation -> archived 20/3/15bp cost filter -> proportional survivor
reallocation -> freeze-new-risk, with the exact hard-feasible normal margin budget.
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
import evaluate_directional_oi_freeze_cost_reallocation as survivor
import evaluate_directional_production_mechanics as mechanics
from afuture.directional_acceptance import ProductionMechanicsConfig
from afuture.directional_hard_feasible_margin import (
    HardFeasibleMarginFreezeDirectionalProductionAcceptance,
)

CHECKPOINT_BASE = 1.21132378
CHECKPOINT_STRESS = 0.68059063
LINEAGE_TOLERANCE = 5e-5


def _simulate(
    *,
    specific_raw: pd.DataFrame,
    weights: pd.DataFrame,
) -> tuple[dict, dict]:
    base_sim = HardFeasibleMarginFreezeDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=mechanics.INITIAL_CAPITAL,
            margin_rate_proxy=mechanics.BASE_MARGIN_PROXY,
        )
    )
    stress_sim = HardFeasibleMarginFreezeDirectionalProductionAcceptance(
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
        "base": checkpoint._economics(
            base_daily, base_events, cost_bps=mechanics.BASE_COST_BPS
        ),
        "stress": checkpoint._economics(
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
    # Reproduce the merged checkpoint before interpreting the mechanical capacity change.
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
    _, _, candidate_weights = survivor.build_reallocated_weights(
        base_weights=base_weights,
        bars_60m=bars_60m,
        close_prices=close,
    )
    candidate, economics = _simulate(
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
        "role": "exact 35pct hard-feasible margin Production L3 on cost-approved survivors",
        "parameter_search": False,
        "production_wiring": False,
        "soft_margin_rule": {
            "formula": "min(max_margin_ratio, 1-min_available_ratio)",
            "value": 0.35,
            "searched_values": [],
        },
        "source_lineage_reproduction": lineage,
        "checkpoint_reference": {
            "base_full_recent": checkpoint_base,
            "stress_full_recent": checkpoint_stress,
        },
        "candidate": candidate,
        "economics": economics,
        "delta_vs_checkpoint_stress_annualized": stress_full - CHECKPOINT_STRESS,
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
        raise SystemExit(f"hard-feasible margin inputs missing: {missing}")

    report = evaluate(
        specific_raw=pd.read_csv(paths["specific"]),
        continuous_raw=pd.read_csv(paths["continuous"]),
        base_weights=oi_gate.load_frozen_weights(paths["weights"]),
        bars_60m=oi_gate.load_60m([paths["prior"], paths["recent"]]),
    )
    output = runtime / "stress80_hard_feasible_margin_report.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
