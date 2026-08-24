"""Re-evaluate the predeclared PR #17 cost-aware no-trade candidate under the Stress-80 gate.

The historical candidate was rejected before Production L3 solely because the old joint
promotion rule required preserving substantially more Base alpha. The current approved
research objective permits Base annualized >=80%, so this tool first proves exact cheap-
screen lineage against the archived PR #17 evidence and only then runs the unchanged
Production mechanics.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pandas as pd

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from afuture.directional_cost_aware_no_trade import apply_cost_aware_no_trade

import evaluate_aggressive_directional as aggressive
import evaluate_directional_production_mechanics as production
import evaluate_return_target_specific as specific

LEGACY_CANDIDATE_TURNOVER = 440.8444
LEGACY_BASE_FULL_RECENT = 1.685521
LEGACY_STRESS_FULL_RECENT = 1.134880
LINEAGE_TOLERANCE = 5e-5


def load_frozen_weights(path: Path) -> pd.DataFrame:
    rows = pd.read_csv(path)
    required = {"level_0", "level_1", "weight"}
    if not required.issubset(rows.columns):
        raise ValueError(
            f"frozen weight artifact missing columns: {sorted(required - set(rows.columns))}"
        )
    rows["level_0"] = pd.to_datetime(rows["level_0"], errors="coerce").dt.normalize()
    rows = rows[rows["level_0"].notna()].copy()
    rows["level_1"] = rows["level_1"].astype(str).str.upper()
    rows["weight"] = pd.to_numeric(rows["weight"], errors="coerce").fillna(0.0)
    if rows.duplicated(["level_0", "level_1"]).any():
        raise ValueError("frozen execution-aligned weights contain duplicates")
    frame = rows.pivot(index="level_0", columns="level_1", values="weight").fillna(0.0)
    frame.index.name = None
    frame.columns.name = None
    if bool((frame.abs().sum(axis=1) > 2.0 + 1e-10).any()):
        raise ValueError("frozen execution-aligned weights exceed 2x gross")
    return frame.sort_index()


def continuous_close_panel(raw: pd.DataFrame, products: list[str]) -> pd.DataFrame:
    frame = raw.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    frame["product"] = frame["product"].astype(str).str.upper()
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["date", "product", "close"])
    frame = frame[frame["close"] > 0.0]
    frame = frame.drop_duplicates(["date", "product"], keep="last")
    close = frame.pivot(index="date", columns="product", values="close").sort_index()
    missing = sorted(set(products) - set(close.columns))
    if missing:
        raise ValueError(f"continuous close history missing products: {missing}")
    return close.reindex(columns=products)


def _metrics(series: pd.Series) -> dict[str, dict]:
    return {
        name: aggressive._window_metrics(series, name)
        for name in aggressive.WINDOWS
    }


def _weight_turnover(weights: pd.DataFrame) -> float:
    turnover = weights.diff().abs().sum(axis=1)
    if len(turnover):
        turnover.iloc[0] = float(weights.iloc[0].abs().sum())
    return float(turnover.sum())


def _economics(attribution: dict) -> dict[str, float]:
    gross = float(attribution["alpha"]["gross_signal_pnl"])
    turnover = float(attribution["transaction_cost"]["turnover_notional"])
    cost = float(attribution["transaction_cost"]["total_cost"])
    net = gross - cost
    return {
        "gross_signal_pnl": gross,
        "turnover_notional": turnover,
        "transaction_cost": cost,
        "net_alpha_pnl": net,
        "net_alpha_per_turnover_bps": net / turnover * 10000.0 if turnover > 0.0 else 0.0,
    }


def _promotion_gate(report: dict) -> dict:
    base = report["base"]["windows"]
    stress = report["stress"]["windows"]
    reasons: list[str] = []
    if stress["full_recent"]["annualized_return"] < 0.80:
        reasons.append("stress_full_recent_below_80pct")
    if stress["full_recent"]["max_drawdown"] < -0.30:
        reasons.append("stress_full_recent_dd_exceeds_30pct")
    if base["full_recent"]["annualized_return"] < 0.80:
        reasons.append("base_full_recent_below_80pct")
    for window in ("validation", "oos"):
        if stress[window]["annualized_return"] <= 0.0:
            reasons.append(f"stress_{window}_not_positive")
        if stress[window]["max_drawdown"] < -0.30:
            reasons.append(f"stress_{window}_dd_exceeds_30pct")
        if stress[window]["halted"]:
            reasons.append(f"stress_{window}_halted")
    if report["stress"]["windows"]["full_recent"]["halted"]:
        reasons.append("stress_full_recent_halted")
    if report["base"]["windows"]["full_recent"]["halted"]:
        reasons.append("base_full_recent_halted")
    if report["stress"]["windows"]["full_recent"]["max_realized_gross_notional_ratio"] > 2.0 + 1e-10:
        reasons.append("stress_realized_gross_above_2x")
    return {"passed": not reasons, "reasons": reasons}


def evaluate(
    *,
    specific_raw: pd.DataFrame,
    continuous_raw: pd.DataFrame,
    frozen_weights: pd.DataFrame,
) -> dict:
    close = continuous_close_panel(continuous_raw, list(frozen_weights.columns))
    candidate = apply_cost_aware_no_trade(
        weights=frozen_weights,
        close_prices=close,
    )

    _close_ret, gap_ret, intraday_ret, _selections, quality = (
        specific.build_roll_safe_execution_returns(specific_raw)
    )
    baseline_aligned = frozen_weights.reindex(
        index=gap_ret.index, columns=gap_ret.columns, fill_value=0.0
    ).fillna(0.0)
    candidate_aligned = candidate.reindex(
        index=gap_ret.index, columns=gap_ret.columns, fill_value=0.0
    ).fillna(0.0)

    cheap = {}
    for label, cost in (("base", 5.0), ("stress", 15.0)):
        cheap[label] = _metrics(
            specific.apply_next_open_product_weights(
                gap_ret,
                intraday_ret,
                candidate_aligned,
                cost_bps=cost,
            )
        )
    baseline_turnover = _weight_turnover(baseline_aligned)
    candidate_turnover = _weight_turnover(candidate_aligned)
    cheap_base = float(cheap["base"]["full_recent"]["annualized_return"])
    cheap_stress = float(cheap["stress"]["full_recent"]["annualized_return"])
    lineage_checks = {
        "candidate_turnover": abs(candidate_turnover - LEGACY_CANDIDATE_TURNOVER) <= 5e-4,
        "base_full_recent": abs(cheap_base - LEGACY_BASE_FULL_RECENT) <= LINEAGE_TOLERANCE,
        "stress_full_recent": abs(cheap_stress - LEGACY_STRESS_FULL_RECENT) <= LINEAGE_TOLERANCE,
    }
    lineage_match = bool(all(lineage_checks.values()))

    report = {
        "role": "re-evaluation of predeclared PR17 cost-aware no-trade under Stress-80 gate",
        "parameter_search": False,
        "production_wiring": False,
        "rule": {
            "completed_product_return_sessions": 20,
            "benefit_horizon_sessions": 3,
            "one_way_hurdle_bps": 15.0,
            "suppressed_actions": ["entry", "same_sign_absolute_increase"],
            "bypass_actions": ["reduction", "exit", "reversal"],
        },
        "cheap_screen": cheap,
        "weight_turnover": {
            "baseline": baseline_turnover,
            "candidate": candidate_turnover,
        },
        "legacy_lineage": {
            "matched": lineage_match,
            "checks": lineage_checks,
            "expected": {
                "candidate_turnover": LEGACY_CANDIDATE_TURNOVER,
                "base_full_recent": LEGACY_BASE_FULL_RECENT,
                "stress_full_recent": LEGACY_STRESS_FULL_RECENT,
            },
        },
        "specific_quality": quality,
    }

    if not lineage_match:
        report["production_l3"] = None
        report["promotion_gate"] = {
            "passed": False,
            "reasons": ["legacy_cheap_screen_lineage_mismatch"],
        }
        return report

    production_report = production.evaluate_with_weights(specific_raw, candidate)
    report["production_l3"] = {
        key: value
        for key, value in production_report.items()
        if not key.startswith("_")
    }
    report["economics"] = {
        "base": _economics(production_report["production_attribution"]["base"]),
        "stress": _economics(production_report["production_attribution"]["stress"]),
    }
    report["promotion_gate"] = _promotion_gate(production_report)
    return report


def main() -> None:
    runtime = Path("runtime")
    paths = {
        "specific": runtime / "return_target_specific_contracts.csv",
        "continuous": runtime / "broad_daily_universe.csv",
        "weights": runtime / "execution_aligned_weights.csv",
    }
    missing = [str(path) for path in paths.values() if not path.exists()]
    if missing:
        raise SystemExit(f"cost-aware no-trade inputs missing: {missing}")
    report = evaluate(
        specific_raw=pd.read_csv(paths["specific"]),
        continuous_raw=pd.read_csv(paths["continuous"]),
        frozen_weights=load_frozen_weights(paths["weights"]),
    )
    output = runtime / "cost_aware_no_trade_production_report.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
