"""Cheap causal specific-contract screen for the predeclared Dominant Basis Carry family."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pandas as pd

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from afuture.directional_basis_alpha import build_basis_carry_weights

import evaluate_aggressive_directional as aggressive
import evaluate_return_target_specific as specific

BASE_COST_BPS = 5.0
STRESS_COST_BPS = 15.0


def _window_metrics(series: pd.Series) -> dict[str, dict]:
    return {
        name: aggressive._window_metrics(series, name)
        for name in aggressive.WINDOWS
    }


def evaluate(specific_raw: pd.DataFrame, basis_raw: pd.DataFrame) -> dict:
    _close, gap, intraday, _selections, quality = specific.build_roll_safe_execution_returns(
        specific_raw
    )
    weights = build_basis_carry_weights(
        basis_raw,
        target_index=gap.index,
        products=tuple(gap.columns),
    )
    base = specific.apply_next_open_product_weights(
        gap, intraday, weights, cost_bps=BASE_COST_BPS
    )
    stress = specific.apply_next_open_product_weights(
        gap, intraday, weights, cost_bps=STRESS_COST_BPS
    )
    turnover = weights.diff().abs().sum(axis=1)
    if len(weights):
        turnover.iloc[0] = float(weights.iloc[0].abs().sum())
    active_products = (weights.abs() > 1e-15).sum(axis=1)

    basis = basis_raw.copy()
    basis["date"] = pd.to_datetime(basis["date"], errors="coerce").dt.normalize()
    basis["symbol"] = basis["symbol"].astype(str).str.upper()
    valid_basis = basis[basis["date"].isin(gap.index) & basis["symbol"].isin(gap.columns)]
    coverage = {
        product: float(
            valid_basis.loc[valid_basis["symbol"] == product, "date"].nunique()
            / max(len(gap.index), 1)
        )
        for product in gap.columns
    }

    base_windows = _window_metrics(base)
    stress_windows = _window_metrics(stress)
    required = ("prior1", "prior2", "train", "validation", "oos", "full_recent")
    independent_pass = all(
        base_windows[name]["annualized_return"] > 0.0
        and stress_windows[name]["annualized_return"] > 0.0
        for name in required
    )
    return {
        "role": "research-only standalone Dominant Basis Carry cheap screen",
        "candidate": "dominant_basis_carry_v1",
        "selection_frozen": True,
        "parameter_search": False,
        "production_wiring": False,
        "signal_definition": "D completed dom_basis_rate sign -> D+1 equal-gross direction",
        "threshold": None,
        "lookback": None,
        "ranking": None,
        "max_gross_leverage": 2.0,
        "base_cost_bps": BASE_COST_BPS,
        "stress_cost_bps": STRESS_COST_BPS,
        "base": base_windows,
        "stress": stress_windows,
        "weight_turnover_sum": float(turnover.sum()),
        "median_active_products": float(active_products.median()) if len(active_products) else 0.0,
        "basis_coverage_ratio": coverage,
        "specific_contract_quality": quality,
        "gate": {
            "independent_pass": bool(independent_pass),
            "rule": "Base and Stress annualized return must both be positive in prior1/prior2/train/validation/OOS/full_recent before any Production combination",
        },
    }


def main() -> None:
    runtime = Path("runtime")
    specific_path = runtime / "return_target_specific_contracts.csv"
    basis_path = runtime / "directional_basis_history.csv"
    missing = [str(path) for path in (specific_path, basis_path) if not path.exists()]
    if missing:
        raise SystemExit(f"basis alpha inputs missing: {missing}")
    report = evaluate(pd.read_csv(specific_path), pd.read_csv(basis_path))
    output = runtime / "directional_basis_alpha_report.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
