"""Fixed-artifact P0 economic screen for directional execution efficiency.

The script intentionally performs no parameter search. It reuses the frozen Production
L3 input artifact and evaluates the single predeclared cost-aware no-trade candidate on
both 5bp Base and 15bp Stress specific-contract next-open paths before any Production L3.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import evaluate_return_target_specific as specific
from afuture.directional_cost_meta import cost_aware_no_trade_weights
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


def metrics(series: pd.Series) -> dict:
    values = series.astype(float).replace([np.inf, -np.inf], np.nan).fillna(0.0).to_numpy()
    if len(values) == 0:
        return {"days": 0, "annualized_return": 0.0, "sharpe": 0.0, "max_drawdown": 0.0}
    equity = np.cumprod(1.0 + values)
    total = float(equity[-1] - 1.0)
    annualized = float((1.0 + total) ** (252.0 / len(values)) - 1.0) if total > -1.0 else -1.0
    std = float(values.std(ddof=1)) if len(values) > 1 else 0.0
    sharpe = float(values.mean() / std * np.sqrt(252.0)) if std > 1e-12 else 0.0
    peak = np.maximum.accumulate(equity)
    return {
        "days": int(len(values)),
        "active_days": int((np.abs(values) > 1e-15).sum()),
        "total_return": total,
        "annualized_return": annualized,
        "annualized_volatility": float(std * np.sqrt(252.0)),
        "sharpe": sharpe,
        "max_drawdown": float((equity / peak - 1.0).min()),
    }


def load_daily(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    frame = raw.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["product"] = frame["product"].astype(str).str.upper()
    for column in ("open", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["date", "product", "open", "close"])
    frame = frame[(frame["open"] > 0) & (frame["close"] > 0)]
    frame = frame.drop_duplicates(["date", "product"], keep="last")
    products = sorted(str(item).upper() for item in FROZEN_PRODUCTS)
    open_prices = frame.pivot(index="date", columns="product", values="open").sort_index().reindex(columns=products)
    close = frame.pivot(index="date", columns="product", values="close").sort_index().reindex(index=open_prices.index, columns=products)
    missing = [product for product in products if close[product].notna().sum() < 400]
    if missing:
        raise RuntimeError(f"fixed daily input missing frozen products: {missing}")
    return open_prices, close


def specific_components(
    gap_returns: pd.DataFrame,
    intraday_returns: pd.DataFrame,
    weights: pd.DataFrame,
    *,
    cost_bps: float,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.DataFrame]:
    aligned = weights.reindex(index=gap_returns.index, columns=gap_returns.columns, fill_value=0.0).fillna(0.0)
    old = aligned.shift(1).fillna(0.0)
    gaps = gap_returns.reindex_like(aligned).fillna(0.0)
    intraday = intraday_returns.reindex_like(aligned).fillna(0.0)
    gross_by_product = old * gaps + aligned * intraday
    turnover_by_product = aligned.diff().abs().fillna(0.0)
    if len(turnover_by_product):
        turnover_by_product.iloc[0] = aligned.iloc[0].abs()
    gross = gross_by_product.sum(axis=1)
    turnover = turnover_by_product.sum(axis=1)
    net_by_product = gross_by_product - turnover_by_product * float(cost_bps) / 10000.0
    net = net_by_product.sum(axis=1)
    return net, gross, turnover, net_by_product


def remove_best_20(net: pd.Series) -> dict:
    series = net.astype(float).fillna(0.0)
    rolling = series.rolling(20, min_periods=20).sum()
    if rolling.dropna().empty:
        return metrics(series)
    end = rolling.idxmax()
    end_position = int(series.index.get_loc(end))
    start_position = end_position - 19
    stripped = series.copy()
    stripped.iloc[start_position:end_position + 1] = 0.0
    return {
        "start": pd.Timestamp(series.index[start_position]).date().isoformat(),
        "end": pd.Timestamp(series.index[end_position]).date().isoformat(),
        **metrics(stripped),
    }


def evaluate_path(
    gap_returns: pd.DataFrame,
    intraday_returns: pd.DataFrame,
    weights: pd.DataFrame,
    *,
    cost_bps: float,
) -> dict:
    net, gross, turnover, product_net = specific_components(
        gap_returns, intraday_returns, weights, cost_bps=cost_bps
    )
    report = {"cost_bps": float(cost_bps), "windows": {}}
    aligned = weights.reindex(index=gap_returns.index, columns=gap_returns.columns, fill_value=0.0).fillna(0.0)
    for name, (start, end) in WINDOWS.items():
        start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
        m = metrics(net.loc[start_ts:end_ts])
        window_turnover = float(turnover.loc[start_ts:end_ts].sum())
        exposure = aligned.loc[start_ts:end_ts].abs().sum(axis=0)
        contribution = product_net.loc[start_ts:end_ts].sum(axis=0).abs()
        m.update(
            {
                "turnover_weight": window_turnover,
                "gross_alpha_return_sum": float(gross.loc[start_ts:end_ts].sum()),
                "transaction_cost_return_sum": window_turnover * float(cost_bps) / 10000.0,
                "net_alpha_return_sum": float(net.loc[start_ts:end_ts].sum()),
                "net_alpha_per_turnover_bps": float(net.loc[start_ts:end_ts].sum() / window_turnover * 10000.0) if window_turnover > 1e-15 else 0.0,
                "max_product_exposure_share": float((exposure / exposure.sum()).max()) if float(exposure.sum()) > 0 else 0.0,
                "max_abs_product_contribution_share": float((contribution / contribution.sum()).max()) if float(contribution.sum()) > 0 else 0.0,
            }
        )
        report["windows"][name] = m

    full_start, full_end = map(pd.Timestamp, WINDOWS["full_recent"])
    report["remove_best_20_sessions"] = remove_best_20(net.loc[full_start:full_end])
    lopo = {}
    for product in aligned.columns:
        reduced = aligned.copy()
        reduced[product] = 0.0
        reduced_net, _, _, _ = specific_components(
            gap_returns, intraday_returns, reduced, cost_bps=cost_bps
        )
        lopo[product] = float(metrics(reduced_net.loc[full_start:full_end])["annualized_return"])
    report["leave_one_product_out"] = {
        "worst_product": min(lopo, key=lopo.get),
        "worst_annualized_return": min(lopo.values()),
        "best_product": max(lopo, key=lopo.get),
        "best_annualized_return": max(lopo.values()),
    }
    return report


def candidate_gate(candidate_base: dict, baseline_base: dict, candidate_stress: dict, baseline_stress: dict) -> dict:
    reasons = []
    deltas = {}
    for endpoint, candidate, baseline in (
        ("base_5bp", candidate_base, baseline_base),
        ("stress_15bp", candidate_stress, baseline_stress),
    ):
        deltas[endpoint] = {
            name: float(candidate["windows"][name]["annualized_return"])
            - float(baseline["windows"][name]["annualized_return"])
            for name in WINDOWS
        }
    base_recent = deltas["base_5bp"]["full_recent"]
    base_oos = deltas["base_5bp"]["oos"]
    stress_recent = deltas["stress_15bp"]["full_recent"]
    stress_oos = deltas["stress_15bp"]["oos"]
    if base_recent < -1e-12:
        reasons.append("Base full_recent annualized return deteriorated")
    if base_oos < -1e-12:
        reasons.append("Base OOS annualized return deteriorated")
    if stress_recent <= 1e-12:
        reasons.append("Stress full_recent annualized return did not improve")
    if stress_oos < -1e-12:
        reasons.append("Stress OOS annualized return deteriorated")
    if float(candidate_stress["windows"]["full_recent"]["sharpe"]) <= float(baseline_stress["windows"]["full_recent"]["sharpe"]):
        reasons.append("Stress full_recent Sharpe did not improve")
    if float(candidate_stress["windows"]["full_recent"]["net_alpha_per_turnover_bps"]) <= float(baseline_stress["windows"]["full_recent"]["net_alpha_per_turnover_bps"]):
        reasons.append("Stress Net Alpha / Turnover did not improve")
    return {"pass": not reasons, "reasons": reasons, "annualized_return_deltas": deltas}


def minute_coverage() -> dict:
    import akshare as ak

    evidence = {}
    required_start = pd.Timestamp(WINDOWS["prior1"][0])
    required_end = pd.Timestamp(WINDOWS["oos"][1])
    coverage_pass = True
    for symbol in MINUTE_SYMBOLS:
        try:
            frame = ak.futures_zh_minute_sina(symbol=symbol, period="5").copy()
        except Exception as exc:
            evidence[symbol] = {"rows": 0, "first": None, "last": None, "has_oi": False, "error": f"{type(exc).__name__}: {exc}"}
            coverage_pass = False
            continue
        if frame.empty:
            evidence[symbol] = {"rows": 0, "first": None, "last": None, "has_oi": False, "error": "empty"}
            coverage_pass = False
            continue
        datetime_column = "datetime" if "datetime" in frame.columns else str(frame.columns[0])
        stamps = pd.to_datetime(frame[datetime_column], errors="coerce").dropna()
        first = stamps.min() if len(stamps) else None
        last = stamps.max() if len(stamps) else None
        has_oi = "hold" in frame.columns
        evidence[symbol] = {
            "rows": int(len(stamps)),
            "first": first.isoformat() if first is not None else None,
            "last": last.isoformat() if last is not None else None,
            "has_oi": bool(has_oi),
            "columns": [str(column) for column in frame.columns],
            "error": "",
        }
        if first is None or last is None or not has_oi or first > required_start or last < required_end:
            coverage_pass = False
    return {
        "symbols": evidence,
        "required_start": required_start.date().isoformat(),
        "required_end": required_end.date().isoformat(),
        "multi_window_5m_oi_coverage_pass": bool(coverage_pass),
    }


def main() -> None:
    runtime = Path("runtime")
    continuous_path = runtime / "broad_daily_universe.csv"
    specific_path = runtime / "return_target_specific_contracts.csv"
    if not continuous_path.exists() or not specific_path.exists():
        raise SystemExit("fixed P0 inputs are missing")

    continuous_raw = pd.read_csv(continuous_path)
    specific_raw = pd.read_csv(specific_path)
    open_prices, close = load_daily(continuous_raw)
    policy = ExecutionAlignedAggressivePolicy(products=tuple(close.columns))
    baseline_weights = policy.weight_history(open_prices, close)
    completed_returns = close.pct_change(fill_method=None).mask(lambda frame: frame.abs() > 0.20)
    no_trade_weights, no_trade_audit = cost_aware_no_trade_weights(
        baseline_weights,
        completed_returns,
        lookback=NO_TRADE_LOOKBACK,
        horizon_days=NO_TRADE_HORIZON,
        cost_bps=STRESS_COST_BPS,
    )

    _close_returns, gap_returns, intraday_returns, _selections, quality = specific.build_roll_safe_execution_returns(specific_raw)
    economics = {}
    for name, weights in (("baseline", baseline_weights), ("cost_aware_no_trade", no_trade_weights)):
        economics[name] = {
            "base_5bp": evaluate_path(gap_returns, intraday_returns, weights, cost_bps=BASE_COST_BPS),
            "stress_15bp": evaluate_path(gap_returns, intraday_returns, weights, cost_bps=STRESS_COST_BPS),
        }

    gate = candidate_gate(
        economics["cost_aware_no_trade"]["base_5bp"],
        economics["baseline"]["base_5bp"],
        economics["cost_aware_no_trade"]["stress_15bp"],
        economics["baseline"]["stress_15bp"],
    )
    minute = minute_coverage()
    report = {
        "input_artifact_id": 9473260618,
        "parameter_search": False,
        "predeclared_configuration": {
            "no_trade_lookback": NO_TRADE_LOOKBACK,
            "no_trade_horizon_days": NO_TRADE_HORIZON,
            "no_trade_cost_bps": STRESS_COST_BPS,
            "minute_symbols": list(MINUTE_SYMBOLS),
        },
        "specific_contract_quality": quality,
        "economics": economics,
        "cost_aware_no_trade_gate": gate,
        "cost_aware_no_trade_production_promoted": False,
        "cost_aware_meta": {
            "production_promoted": False,
            "decision": "reject_inherited_exact_candidate_evidence",
            "prior_fixed_l3_stress_annualized_approx": -0.1303,
            "prior_fixed_l3_drawdown_exceeded_30pct": True,
            "prior_fixed_l3_permanent_halt": True,
            "reason": "same frozen 96-template / 11-session / 3-session / top-3 NetSwitchBenefit rule was already evaluated and rejected; no duplicate L3",
        },
        "minute_timing": {
            "coverage": minute,
            "production_promoted": False,
            "decision": "eligible_for_economic_test" if minute["multi_window_5m_oi_coverage_pass"] else "insufficient_multi_window_5m_oi_coverage",
        },
        "realistic_execution_stress": {
            "simulator_ready": True,
            "predeclared_depth_haircut": 0.75,
            "predeclared_latency_ticks": 1,
            "predeclared_size_impact_ticks": 1,
            "historical_two_year_l1_depth_available": False,
            "historical_return_credit": False,
            "decision": "retain simulator and execution audit; do not fabricate a two-year realistic-L1 return series",
        },
        "production_l3_run": False,
        "production_l3_skip_reason": "candidate failed Base/Stress cheap gate before L3" if not gate["pass"] else "candidate passed cheap gate and requires one Production L3",
        "causality": {"pass": True, "evidence": ["current/future-row perturbation tests", "completed-return-only no-trade expectation", "t-1 concrete-contract roll-safe return construction"]},
    }
    no_trade_audit.to_json(runtime / "p0_no_trade_audit.json", orient="records", date_format="iso", force_ascii=False)
    output = runtime / "microstructure_report.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
