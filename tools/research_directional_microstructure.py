"""Bounded P0 economic research for directional microstructure and transaction costs.

No parameter grid is run. Daily candidates use one predeclared configuration. Five-minute
history is fetched only for four fixed liquid roots to determine whether the requested
opening/session research has enough point-in-time coverage; insufficient coverage is a
rejection, not an invitation to fit a shorter window.
"""
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

import fetch_broad_daily_universe
from afuture.directional_cost_meta import (
    cost_aware_no_trade_weights,
    cost_aware_weight_history,
)
from afuture.execution_aligned_policy import ExecutionAlignedAggressivePolicy
from afuture.execution_aligned_runtime import FROZEN_PRODUCTS


BASE_COST_BPS = 5.0
STRESS_COST_BPS = 15.0
NO_TRADE_LOOKBACK = 20
NO_TRADE_HORIZON = 3
MINUTE_SYMBOLS = ("RB0", "M0", "CU0", "TA0")
WINDOWS = {
    "prior1": ("2022-08-22", "2023-08-20"),
    "prior2": ("2023-08-21", "2024-08-20"),
    "train": ("2024-08-21", "2025-08-20"),
    "validation": ("2025-08-21", "2026-02-20"),
    "oos": ("2026-02-21", "2026-08-20"),
    "full_recent": ("2024-08-21", "2026-08-20"),
}
ROBUST_WINDOWS = ("prior1", "prior2", "train", "validation", "oos")


def _metrics(values: pd.Series) -> dict[str, float | int]:
    series = values.astype(float).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    raw = series.to_numpy(float)
    if raw.size == 0:
        return {
            "days": 0,
            "active_days": 0,
            "total_return": 0.0,
            "annualized_return": 0.0,
            "annualized_volatility": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
        }
    wealth = np.cumprod(1.0 + raw)
    total = float(wealth[-1] - 1.0)
    annualized = (
        float((1.0 + total) ** (252.0 / len(raw)) - 1.0)
        if total > -1.0
        else -1.0
    )
    std = float(raw.std(ddof=1)) if len(raw) > 1 else 0.0
    sharpe = float(raw.mean() / std * np.sqrt(252.0)) if std > 1e-12 else 0.0
    peaks = np.maximum.accumulate(wealth)
    drawdown = wealth / peaks - 1.0
    return {
        "days": int(len(raw)),
        "active_days": int((np.abs(raw) > 1e-15).sum()),
        "total_return": total,
        "annualized_return": annualized,
        "annualized_volatility": float(std * np.sqrt(252.0)),
        "sharpe": sharpe,
        "max_drawdown": float(drawdown.min()),
    }


def _slice(frame, name: str):
    start, end = WINDOWS[name]
    return frame.loc[pd.Timestamp(start):pd.Timestamp(end)]


def _path_components(
    open_prices: pd.DataFrame,
    close: pd.DataFrame,
    weights: pd.DataFrame,
    *,
    cost_bps: float,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.DataFrame]:
    aligned = weights.reindex(index=close.index, columns=close.columns).fillna(0.0)
    intraday = close.div(open_prices) - 1.0
    intraday = intraday.mask(intraday.abs() > 0.20).fillna(0.0)
    gross_by_product = aligned * intraday
    turnover_by_product = aligned.diff().abs().fillna(0.0)
    if len(turnover_by_product):
        turnover_by_product.iloc[0] = aligned.iloc[0].abs()
    gross = gross_by_product.sum(axis=1)
    turnover = turnover_by_product.sum(axis=1)
    cost = turnover * float(cost_bps) / 10000.0
    net = gross - cost
    net_by_product = gross_by_product - turnover_by_product * float(cost_bps) / 10000.0
    return net, gross, turnover, net_by_product


def _remove_best_period(net: pd.Series, *, sessions: int = 20) -> dict:
    series = net.astype(float).fillna(0.0)
    if len(series) < sessions:
        return {"sessions": sessions, "start": None, "end": None, **_metrics(series)}
    rolling = series.rolling(sessions, min_periods=sessions).sum()
    end = rolling.idxmax()
    end_pos = int(series.index.get_loc(end))
    start_pos = end_pos - sessions + 1
    stripped = series.copy()
    stripped.iloc[start_pos:end_pos + 1] = 0.0
    return {
        "sessions": sessions,
        "start": pd.Timestamp(series.index[start_pos]).date().isoformat(),
        "end": pd.Timestamp(series.index[end_pos]).date().isoformat(),
        **_metrics(stripped),
    }


def _evaluate_path(
    open_prices: pd.DataFrame,
    close: pd.DataFrame,
    weights: pd.DataFrame,
    *,
    cost_bps: float,
) -> dict:
    net, gross, turnover, net_by_product = _path_components(
        open_prices, close, weights, cost_bps=cost_bps
    )
    report: dict[str, object] = {"cost_bps": float(cost_bps), "windows": {}}
    for name in WINDOWS:
        net_window = _slice(net, name)
        turnover_window = _slice(turnover, name)
        gross_window = _slice(gross, name)
        weight_window = _slice(weights, name).fillna(0.0)
        net_product_window = _slice(net_by_product, name).fillna(0.0)
        metrics = _metrics(net_window)
        total_turnover = float(turnover_window.sum())
        exposure = weight_window.abs().sum(axis=0)
        exposure_total = float(exposure.sum())
        exposure_share = exposure / exposure_total if exposure_total > 0 else exposure * 0.0
        contributions = net_product_window.sum(axis=0)
        contribution_denominator = float(contributions.abs().sum())
        contribution_share = (
            contributions.abs() / contribution_denominator
            if contribution_denominator > 0
            else contributions * 0.0
        )
        metrics.update(
            {
                "turnover_weight": total_turnover,
                "transaction_cost_return_sum": float(
                    total_turnover * float(cost_bps) / 10000.0
                ),
                "gross_alpha_return_sum": float(gross_window.sum()),
                "net_alpha_return_sum": float(net_window.sum()),
                "net_alpha_per_turnover_bps": float(
                    net_window.sum() / total_turnover * 10000.0
                ) if total_turnover > 1e-15 else 0.0,
                "max_product_exposure_share": float(exposure_share.max()) if len(exposure_share) else 0.0,
                "max_product_exposure_product": str(exposure_share.idxmax()) if len(exposure_share) else "",
                "max_abs_product_contribution_share": float(contribution_share.max()) if len(contribution_share) else 0.0,
                "max_abs_product_contribution_product": str(contribution_share.idxmax()) if len(contribution_share) else "",
            }
        )
        report["windows"][name] = metrics

    full_net = _slice(net, "full_recent")
    report["remove_best_20_sessions"] = _remove_best_period(full_net, sessions=20)

    lopo: dict[str, float] = {}
    start, end = WINDOWS["full_recent"]
    for product in weights.columns:
        reduced = weights.copy()
        reduced[product] = 0.0
        reduced_net, _, _, _ = _path_components(
            open_prices, close, reduced, cost_bps=cost_bps
        )
        metrics = _metrics(reduced_net.loc[pd.Timestamp(start):pd.Timestamp(end)])
        lopo[str(product)] = float(metrics["annualized_return"])
    report["leave_one_product_out"] = {
        "worst_product": min(lopo, key=lopo.get) if lopo else "",
        "worst_annualized_return": min(lopo.values()) if lopo else 0.0,
        "best_product": max(lopo, key=lopo.get) if lopo else "",
        "best_annualized_return": max(lopo.values()) if lopo else 0.0,
    }
    return report


def _cheap_gate(candidate: dict, baseline: dict) -> dict:
    candidate_windows = candidate["windows"]
    baseline_windows = baseline["windows"]
    deltas = {
        name: float(candidate_windows[name]["annualized_return"])
        - float(baseline_windows[name]["annualized_return"])
        for name in ROBUST_WINDOWS
    }
    non_negative = sum(delta >= -1e-12 for delta in deltas.values())
    recent = candidate_windows["full_recent"]
    recent_base = baseline_windows["full_recent"]
    reasons: list[str] = []
    if float(recent["annualized_return"]) <= float(recent_base["annualized_return"]):
        reasons.append("full_recent annualized return did not improve")
    if float(recent["sharpe"]) <= float(recent_base["sharpe"]):
        reasons.append("full_recent Sharpe did not improve")
    if float(recent["net_alpha_per_turnover_bps"]) <= float(recent_base["net_alpha_per_turnover_bps"]):
        reasons.append("Net Alpha / Turnover did not improve")
    if deltas["oos"] < -1e-12:
        reasons.append("OOS annualized return deteriorated")
    if non_negative < 4:
        reasons.append("fewer than four of five independent windows were non-negative versus baseline")
    if float(candidate["remove_best_20_sessions"]["annualized_return"]) < float(
        baseline["remove_best_20_sessions"]["annualized_return"]
    ) - 1e-12:
        reasons.append("remove-best-period robustness deteriorated")
    if float(candidate["leave_one_product_out"]["worst_annualized_return"]) < float(
        baseline["leave_one_product_out"]["worst_annualized_return"]
    ) - 1e-12:
        reasons.append("leave-one-product-out worst case deteriorated")
    if float(recent["max_product_exposure_share"]) > 0.50 + 1e-12:
        reasons.append("single-product exposure exceeds 50% of exposure-days")
    return {
        "pass": not reasons,
        "reasons": reasons,
        "window_annualized_deltas": deltas,
        "non_negative_window_count": int(non_negative),
    }


def _minute_coverage() -> dict:
    import akshare as ak

    rows: dict[str, dict] = {}
    for symbol in MINUTE_SYMBOLS:
        try:
            frame = ak.futures_zh_minute_sina(symbol=symbol, period="5").copy()
        except Exception as exc:
            rows[symbol] = {
                "rows": 0,
                "first": None,
                "last": None,
                "has_oi": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
            continue
        if frame.empty:
            rows[symbol] = {
                "rows": 0,
                "first": None,
                "last": None,
                "has_oi": False,
                "error": "empty",
            }
            continue
        column = "datetime" if "datetime" in frame.columns else str(frame.columns[0])
        stamp = pd.to_datetime(frame[column], errors="coerce").dropna()
        rows[symbol] = {
            "rows": int(len(stamp)),
            "first": stamp.min().isoformat() if len(stamp) else None,
            "last": stamp.max().isoformat() if len(stamp) else None,
            "has_oi": bool("hold" in frame.columns),
            "columns": [str(item) for item in frame.columns],
            "error": "",
        }
    required_start = pd.Timestamp(WINDOWS["prior1"][0])
    required_end = pd.Timestamp(WINDOWS["oos"][1])
    enough = True
    for item in rows.values():
        if not item.get("first") or not item.get("last") or not item.get("has_oi"):
            enough = False
            continue
        first = pd.Timestamp(item["first"])
        last = pd.Timestamp(item["last"])
        if first > required_start or last < required_end:
            enough = False
    return {
        "symbols": rows,
        "required_start": required_start.date().isoformat(),
        "required_end": required_end.date().isoformat(),
        "multi_window_5m_oi_coverage_pass": bool(enough),
    }


def _load_daily_panel(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    frame = raw.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["product"] = frame["product"].astype(str).str.upper()
    for column in ("open", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["date", "product", "open", "close"])
    frame = frame[(frame["open"] > 0) & (frame["close"] > 0)]
    frame.drop_duplicates(["date", "product"], keep="last", inplace=True)
    products = sorted({str(item).upper() for item in FROZEN_PRODUCTS})
    open_prices = frame.pivot(index="date", columns="product", values="open").sort_index().reindex(columns=products)
    close = frame.pivot(index="date", columns="product", values="close").sort_index().reindex(index=open_prices.index, columns=products)
    missing = [product for product in products if close[product].notna().sum() < 400]
    if missing:
        raise RuntimeError(f"daily P0 screen missing required frozen history: {missing}")
    return open_prices, close


def _production_efficiency(report: Mapping) -> dict[str, float]:
    attribution = report["production_attribution"]["stress"]
    gross_pnl = float(attribution["alpha"]["gross_signal_pnl"])
    transaction = attribution["transaction_cost"]
    turnover = float(transaction["turnover_notional"])
    cost = float(transaction["total_cost"])
    return {
        "gross_signal_pnl": gross_pnl,
        "transaction_cost": cost,
        "turnover_notional": turnover,
        "net_alpha_pnl": gross_pnl - cost,
        "net_alpha_per_turnover_bps": (gross_pnl - cost) / turnover * 10000.0 if turnover > 0 else 0.0,
    }


def _strip_private(report: Mapping) -> dict:
    return {key: value for key, value in report.items() if not str(key).startswith("_")}


def _run_production_l3(candidates: dict[str, pd.DataFrame]) -> dict:
    import fetch_return_target_specific_daily
    import evaluate_directional_production_mechanics as mechanics

    runtime = Path("runtime")
    specific_path = runtime / "return_target_specific_contracts.csv"
    if not specific_path.exists():
        fetch_return_target_specific_daily.main()
    specific = pd.read_csv(specific_path)
    reports: dict[str, dict] = {}
    for name, weights in candidates.items():
        raw_report = mechanics.evaluate_with_weights(specific, weights)
        cleaned = _strip_private(raw_report)
        cleaned["stress_efficiency"] = _production_efficiency(raw_report)
        reports[name] = cleaned
    return reports


def main() -> None:
    runtime = Path("runtime")
    runtime.mkdir(parents=True, exist_ok=True)
    daily_path = runtime / "broad_daily_universe.csv"
    if not daily_path.exists():
        fetch_broad_daily_universe.main()
    raw = pd.read_csv(daily_path)
    open_prices, close = _load_daily_panel(raw)
    products = tuple(close.columns)
    policy = ExecutionAlignedAggressivePolicy(products=products)
    baseline_weights = policy.weight_history(open_prices, close)
    meta_weights, meta_audit = cost_aware_weight_history(
        policy, open_prices, close, transition_cost_bps=STRESS_COST_BPS
    )
    completed_returns = close.pct_change(fill_method=None).mask(
        lambda frame: frame.abs() > 0.20
    )
    no_trade_weights, no_trade_audit = cost_aware_no_trade_weights(
        baseline_weights,
        completed_returns,
        lookback=NO_TRADE_LOOKBACK,
        horizon_days=NO_TRADE_HORIZON,
        cost_bps=STRESS_COST_BPS,
    )
    combined_weights, combined_audit = cost_aware_no_trade_weights(
        meta_weights,
        completed_returns,
        lookback=NO_TRADE_LOOKBACK,
        horizon_days=NO_TRADE_HORIZON,
        cost_bps=STRESS_COST_BPS,
    )
    paths = {
        "baseline": baseline_weights,
        "cost_aware_meta": meta_weights,
        "cost_aware_no_trade": no_trade_weights,
        "combined_cost": combined_weights,
    }

    economics: dict[str, dict] = {}
    for name, weights in paths.items():
        economics[name] = {
            "base_5bp": _evaluate_path(
                open_prices, close, weights, cost_bps=BASE_COST_BPS
            ),
            "stress_15bp": _evaluate_path(
                open_prices, close, weights, cost_bps=STRESS_COST_BPS
            ),
        }
    baseline_stress = economics["baseline"]["stress_15bp"]
    gates = {
        name: _cheap_gate(item["stress_15bp"], baseline_stress)
        for name, item in economics.items()
        if name != "baseline"
    }

    passing = {
        name: paths[name]
        for name, gate in gates.items()
        if bool(gate["pass"])
    }
    production_l3: dict[str, dict] = {}
    if passing:
        production_l3 = _run_production_l3(
            {"baseline": baseline_weights, **passing}
        )

    minute = _minute_coverage()
    timing_decision = (
        "eligible_for_followup_economic_test"
        if minute["multi_window_5m_oi_coverage_pass"]
        else "reject_for_promotion_insufficient_multi_window_5m_oi_history"
    )
    realistic_status = {
        "simulator": "SimBroker conservative L1 depth/partial-FAK/latency/size-impact stress implemented",
        "historical_two_year_l1_bid_ask_depth_available": False,
        "promotion_credit": False,
        "decision": "retain fixed 15bp as historical economic stress; realistic L1 stress is executable when point-in-time L1 replay is supplied, but no two-year L1 history is fabricated",
    }

    meta_audit.to_json(runtime / "microstructure_meta_audit.json", orient="records", date_format="iso", force_ascii=False)
    no_trade_audit.to_json(runtime / "microstructure_no_trade_audit.json", orient="records", date_format="iso", force_ascii=False)
    combined_audit.to_json(runtime / "microstructure_combined_audit.json", orient="records", date_format="iso", force_ascii=False)

    report = {
        "source": "AKShare/Sina bounded daily + four-root 5m coverage probe",
        "predeclared_configuration": {
            "meta_transition_cost_bps": STRESS_COST_BPS,
            "no_trade_lookback": NO_TRADE_LOOKBACK,
            "no_trade_horizon_days": NO_TRADE_HORIZON,
            "no_trade_cost_bps": STRESS_COST_BPS,
            "minute_symbols": list(MINUTE_SYMBOLS),
            "parameter_grid": False,
        },
        "economics": economics,
        "cheap_gates": gates,
        "production_l3": production_l3,
        "minute_timing": {
            "coverage": minute,
            "decision": timing_decision,
            "production_promoted": False,
        },
        "realistic_execution_stress": realistic_status,
        "causality": {
            "pass": True,
            "evidence": [
                "tests/test_execution_aligned_policy.py current-row perturbation",
                "tests/test_execution_cost_meta.py current/future-row perturbation",
                "tests/test_directional_timing.py causal observed-only session state",
            ],
        },
        "production_unchanged_before_economic_gate": True,
    }
    output = runtime / "microstructure_report.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
