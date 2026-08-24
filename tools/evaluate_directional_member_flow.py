"""Cheap roll-safe screen for the parameter-free Top20 member-flow alpha."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Mapping

import numpy as np
import pandas as pd

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from afuture.directional_member_flow import member_flow_weights
from evaluate_directional_stress80 import (
    BASE_COST_BPS,
    STRESS_COST_BPS,
    WINDOWS,
    _concentration,
    _window_economics,
    _window_metrics,
    _window_slice,
)

SOURCE_WINDOWS = ("prior1", "prior2", "train", "validation", "oos")
MIN_EXCHANGE_COVERAGE = 0.90


def member_flow_gate(report: Mapping) -> dict:
    stress = report["stress"]
    positive = {
        name: float(stress[name]["annualized_return"]) > 0.0
        for name in SOURCE_WINDOWS
    }
    coverage_ok = all(
        float(value) >= MIN_EXCHANGE_COVERAGE
        for audit in report["source_audits"].values()
        for value in audit["coverage"].values()
    )
    economics = report["economics"]["stress"]["full_recent"]
    checks = {
        "source_coverage_ge_90pct": coverage_ok,
        "train_positive": positive["train"],
        "validation_positive": positive["validation"],
        "oos_positive": positive["oos"],
        "at_least_4_of_5_source_windows_positive": sum(positive.values()) >= 4,
        "full_recent_positive": float(stress["full_recent"]["annualized_return"]) > 0.0,
        "full_recent_dd_within_30pct": abs(
            min(float(stress["full_recent"]["max_drawdown"]), 0.0)
        ) <= 0.30,
        "full_recent_net_alpha_per_turnover_positive": float(
            economics["net_alpha_per_turnover_bps"]
        ) > 0.0,
    }
    reasons = [name for name, passed in checks.items() if not passed]
    return {"passed": not reasons, "checks": checks, "reasons": reasons}


def _load_member_flow(directory: Path) -> tuple[pd.DataFrame, dict]:
    panels: list[pd.DataFrame] = []
    audits: dict[str, dict] = {}
    for window in SOURCE_WINDOWS:
        panel_path = directory / f"member_flow_{window}.csv"
        audit_path = directory / f"member_flow_{window}_audit.json"
        if not panel_path.exists() or not audit_path.exists():
            raise FileNotFoundError(f"member-flow cache incomplete for {window}")
        panel = pd.read_csv(panel_path)
        panel["date"] = pd.to_datetime(panel["date"], errors="coerce").dt.normalize()
        panel = panel.dropna(subset=["date"]).set_index("date").sort_index()
        panels.append(panel)
        audits[window] = json.loads(audit_path.read_text(encoding="utf-8"))
    frame = pd.concat(panels, axis=0).sort_index()
    frame = frame[~frame.index.duplicated(keep="last")]
    return frame.apply(pd.to_numeric, errors="coerce"), audits


def build_member_flow_weight_history(
    pressure: pd.DataFrame,
    *,
    execution_index: pd.DatetimeIndex,
    products: list[str],
) -> pd.DataFrame:
    """D completed member flow determines the D+1 target, with no forward fill."""
    target_from_signal = pd.DataFrame(0.0, index=pressure.index, columns=products)
    for date, row in pressure.reindex(columns=products).iterrows():
        values = {
            str(product): float(value)
            for product, value in row.items()
            if pd.notna(value) and np.isfinite(float(value))
        }
        for product, weight in member_flow_weights(values).items():
            target_from_signal.at[date, product] = float(weight)
    shifted = target_from_signal.shift(1)
    # Reindex after shifting on the frozen trading-day series. Missing source dates remain
    # flat instead of being filled across an unavailable publication day.
    return shifted.reindex(index=execution_index, columns=products).fillna(0.0)


def evaluate_member_flow(
    specific_raw: pd.DataFrame,
    pressure: pd.DataFrame,
    audits: Mapping[str, dict],
) -> dict:
    import evaluate_return_target_specific as specific

    _close_ret, gap_ret, intraday_ret, _selections, quality = (
        specific.build_roll_safe_execution_returns(specific_raw)
    )
    products = [str(item).upper() for item in gap_ret.columns]
    weights = build_member_flow_weight_history(
        pressure,
        execution_index=gap_ret.index,
        products=products,
    )
    old_weights = weights.shift(1).fillna(0.0)
    gaps = gap_ret.reindex_like(weights).fillna(0.0)
    intraday = intraday_ret.reindex_like(weights).fillna(0.0)
    gross_by_product = old_weights * gaps + weights * intraday
    gross = gross_by_product.sum(axis=1)
    turnover_by_product = weights.diff().abs()
    if len(turnover_by_product):
        turnover_by_product.iloc[0] = weights.iloc[0].abs()
    turnover = turnover_by_product.sum(axis=1)
    base = gross - turnover * BASE_COST_BPS / 10000.0
    stress = gross - turnover * STRESS_COST_BPS / 10000.0

    gross_path = weights.abs().sum(axis=1)
    risk: dict[str, dict] = {}
    for name in WINDOWS:
        slice_ = _window_slice(gross_path, name)
        maximum = float(slice_.max()) if len(slice_) else 0.0
        risk[name] = {
            "max_target_gross": maximum,
            "violations": ["gross cap"] if maximum > 2.0 + 1e-10 else [],
        }

    start, end = WINDOWS["full_recent"]
    product_net = (
        gross_by_product - turnover_by_product * STRESS_COST_BPS / 10000.0
    ).loc[pd.Timestamp(start) : pd.Timestamp(end)].sum(axis=0)
    report = {
        "role": "parameter-free Top20 member-flow independent alpha screen",
        "production_wiring": False,
        "signal": "(sum top20 long_oi_change - sum top20 short_oi_change) / sum top20 current long+short oi",
        "execution": "D completed rank publication -> D+1 target",
        "weighting": "signed pressure proportional, total gross 2x, no threshold/rank/lookback",
        "supported_exchanges": ["CZCE", "SHFE"],
        "unsupported_exchanges": ["DCE", "INE"],
        "base": _window_metrics(base),
        "stress": _window_metrics(stress),
        "economics": {
            "base": _window_economics(gross, turnover, cost_bps=BASE_COST_BPS),
            "stress": _window_economics(gross, turnover, cost_bps=STRESS_COST_BPS),
        },
        "risk": risk,
        "source_audits": dict(audits),
        "data_quality": quality,
        "concentration": {
            "stress_full_recent_product_net": _concentration(product_net)
        },
    }
    report["gate"] = member_flow_gate(report)
    return report


def main() -> None:
    runtime = Path("runtime")
    pressure, audits = _load_member_flow(runtime / "member_flow")
    report = evaluate_member_flow(
        pd.read_csv(runtime / "return_target_specific_contracts.csv"),
        pressure,
        audits,
    )
    output = runtime / "member_flow_screen.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "gate": report["gate"],
                "stress": {
                    name: report["stress"][name]
                    for name in ("prior1", "prior2", "train", "validation", "oos", "full_recent")
                },
                "economics_full_recent": report["economics"]["stress"]["full_recent"],
                "coverage": {
                    name: audit["coverage"]
                    for name, audit in report["source_audits"].items()
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
