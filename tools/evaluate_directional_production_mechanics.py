"""Production-mechanics proxy for the frozen execution-aligned directional policy.

The historical float-notional result is a signal/execution study. This evaluator keeps
its frozen weights but adds integer lots, contract multipliers, previous-completed-day
activity selection, contract roll semantics, account hard gates, the recoverable daily
loss circuit, causal defensive scaling, and an explicit margin proxy. It never searches
Alpha or parameters and does not claim historical broker margin schedules are known.

Each published reporting window is an independent account experiment: the frozen signal
path is unchanged, while account equity, positions, margin state and high-watermark reset
to the configured initial capital/flat state at that window's first day. Static contract
indexes are prepared once and safely reused across those independent account runs.
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

from afuture.directional_acceptance import (
    DirectionalProductionAcceptance,
    PreparedDirectionalContracts,
    ProductionMechanicsConfig,
    ProductionSimulationResult,
)

BASE_COST_BPS = 5.0
STRESS_COST_BPS = 15.0
BASE_MARGIN_PROXY = 0.12
STRESS_MARGIN_PROXY = 0.15
INITIAL_CAPITAL = 500000.0
WINDOWS = {
    "prior1": ("2022-08-22", "2023-08-20"),
    "prior2": ("2023-08-21", "2024-08-20"),
    "train": ("2024-08-21", "2025-08-20"),
    "validation": ("2025-08-21", "2026-02-20"),
    "selection_full": ("2024-08-21", "2026-02-20"),
    "oos": ("2026-02-21", "2026-08-20"),
    "full_recent": ("2024-08-21", "2026-08-20"),
}


def _metrics(values: pd.Series) -> dict:
    raw = np.nan_to_num(
        values.to_numpy(float), nan=0.0, posinf=0.0, neginf=0.0
    )
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
    equity = np.cumprod(1.0 + raw)
    total = float(equity[-1] - 1.0)
    annualized = (
        (1.0 + total) ** (252.0 / raw.size) - 1.0
        if total > -1.0
        else -1.0
    )
    std = float(raw.std(ddof=1)) if raw.size > 1 else 0.0
    sharpe = (
        float(raw.mean() / std * np.sqrt(252.0))
        if std > 1e-12
        else 0.0
    )
    peak = np.maximum.accumulate(equity)
    drawdown = equity / peak - 1.0
    return {
        "days": int(raw.size),
        "active_days": int((np.abs(raw) > 1e-15).sum()),
        "total_return": total,
        "annualized_return": float(annualized),
        "annualized_volatility": float(std * np.sqrt(252.0)),
        "sharpe": sharpe,
        "max_drawdown": float(drawdown.min()),
    }


def _result_stats(result: ProductionSimulationResult) -> dict:
    daily = result.daily
    returns = (
        daily["daily_return"].astype(float)
        if not daily.empty
        else pd.Series(dtype=float)
    )
    stats = _metrics(returns)
    margin_reject_days = (
        int((daily["margin_reject"].astype(str) != "").sum())
        if not daily.empty
        else 0
    )
    daily_circuit_days = (
        int(daily["daily_circuit"].astype(bool).sum())
        if not daily.empty and "daily_circuit" in daily
        else 0
    )
    defensive_days = (
        int((daily["risk_scale"].astype(float) < 1.0 - 1e-12).sum())
        if not daily.empty and "risk_scale" in daily
        else 0
    )
    halted = (
        bool(daily["halted"].astype(bool).any())
        if not daily.empty
        else False
    )
    max_gross_ratio = 0.0
    if not daily.empty:
        equity = daily["equity"].replace(0.0, pd.NA)
        ratio = daily["gross_notional"].div(equity)
        ratio = ratio.replace([float("inf"), -float("inf")], pd.NA).dropna()
        max_gross_ratio = float(ratio.max()) if not ratio.empty else 0.0
    return {
        **stats,
        "final_equity": float(result.final_equity),
        "first_divergence": str(result.first_divergence),
        "halted": halted,
        "daily_circuit_days": daily_circuit_days,
        "defensive_risk_days": defensive_days,
        "margin_reject_days": margin_reject_days,
        "max_realized_gross_notional_ratio": max_gross_ratio,
    }


def _simulation_report(
    simulator: DirectionalProductionAcceptance,
    specific_raw: pd.DataFrame,
    weights: pd.DataFrame,
    *,
    prepared: PreparedDirectionalContracts,
    cost_bps: float,
    margin_rate_proxy: float,
) -> tuple[dict, pd.DataFrame]:
    windows: dict[str, dict] = {}
    daily_by_window: dict[str, pd.DataFrame] = {}
    for name, (start, end) in WINDOWS.items():
        window_weights = weights.loc[
            pd.Timestamp(start) : pd.Timestamp(end)
        ].copy()
        result = simulator.simulate(
            specific_raw,
            window_weights,
            cost_bps=cost_bps,
            prepared=prepared,
        )
        windows[name] = _result_stats(result)
        daily_by_window[name] = result.daily.copy()

    if "full_recent" in windows:
        principal_name = "full_recent"
    elif windows:
        principal_name = next(reversed(windows))
    else:
        principal_name = ""

    principal = windows.get(
        principal_name,
        {
            "final_equity": INITIAL_CAPITAL,
            "first_divergence": "",
            "halted": False,
            "daily_circuit_days": 0,
            "defensive_risk_days": 0,
            "margin_reject_days": 0,
            "max_realized_gross_notional_ratio": 0.0,
        },
    )
    principal_daily = daily_by_window.get(principal_name, pd.DataFrame())
    config = simulator.config
    return (
        {
            "cost_bps": float(cost_bps),
            "margin_rate_proxy": float(margin_rate_proxy),
            "margin_estimate_buffer": float(config.margin_estimate_buffer),
            "initial_capital": INITIAL_CAPITAL,
            "max_contract_volume": int(config.max_contract_volume),
            "max_daily_loss_ratio": float(config.max_daily_loss_ratio),
            "max_total_drawdown_ratio": float(config.max_total_drawdown_ratio),
            "max_margin_ratio": float(config.max_margin_ratio),
            "min_available_ratio": float(config.min_available_ratio),
            "state_reset_per_window": True,
            "window_account_semantics": "independent_initial_capital_flat_start",
            "principal_window": principal_name,
            "final_equity": float(principal["final_equity"]),
            "first_divergence": str(principal["first_divergence"]),
            "halted": bool(principal["halted"]),
            "daily_circuit_days": int(principal["daily_circuit_days"]),
            "defensive_risk_days": int(principal["defensive_risk_days"]),
            "margin_reject_days": int(principal["margin_reject_days"]),
            "max_realized_gross_notional_ratio": float(
                principal["max_realized_gross_notional_ratio"]
            ),
            "windows": windows,
        },
        principal_daily,
    )


def evaluate_with_weights(
    specific_raw: pd.DataFrame,
    weights: pd.DataFrame,
    *,
    float_report: dict | None = None,
) -> dict:
    base_sim = DirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=INITIAL_CAPITAL,
            margin_rate_proxy=BASE_MARGIN_PROXY,
        )
    )
    stress_sim = DirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=INITIAL_CAPITAL,
            margin_rate_proxy=STRESS_MARGIN_PROXY,
        )
    )
    # Contract data preparation is invariant to account capital, margin proxy and
    # reporting window. Reuse it while every simulation still resets account state.
    prepared = base_sim.prepare_contracts(specific_raw)
    base, base_daily = _simulation_report(
        base_sim,
        specific_raw,
        weights,
        prepared=prepared,
        cost_bps=BASE_COST_BPS,
        margin_rate_proxy=BASE_MARGIN_PROXY,
    )
    stress, stress_daily = _simulation_report(
        stress_sim,
        specific_raw,
        weights,
        prepared=prepared,
        cost_bps=STRESS_COST_BPS,
        margin_rate_proxy=STRESS_MARGIN_PROXY,
    )

    production_gap: dict[str, dict] = {}
    if float_report is not None:
        for label, production in (("base", base), ("stress", stress)):
            historical = float_report["next_open_execution"][label]["full_recent"]
            proxy = production["windows"]["full_recent"]
            production_gap[label] = {
                "float_annualized_return": float(historical["annualized_return"]),
                "production_proxy_annualized_return": float(
                    proxy["annualized_return"]
                ),
                "annualized_return_delta": float(
                    proxy["annualized_return"]
                    - historical["annualized_return"]
                ),
                "float_total_return": float(historical["total_return"]),
                "production_proxy_total_return": float(proxy["total_return"]),
                "float_max_drawdown": float(historical["max_drawdown"]),
                "production_proxy_max_drawdown": float(proxy["max_drawdown"]),
                "max_drawdown_delta": float(
                    proxy["max_drawdown"] - historical["max_drawdown"]
                ),
            }

    return {
        "role": "production-mechanics proxy acceptance for frozen execution-aligned directional policy",
        "selection_frozen": True,
        "parameter_search": False,
        "margin_is_historical_truth": False,
        "state_reset_per_window": True,
        "mechanics": {
            "integer_lots": True,
            "frozen_contract_multipliers": True,
            "previous_completed_activity": True,
            "reduction_before_open": True,
            "target_gross_leverage_cap": 2.0,
            "max_contract_volume": int(base_sim.config.max_contract_volume),
            "daily_loss_same_day_circuit": True,
            "daily_loss_next_trading_day_safe_recovery": True,
            "hard_account_risk_permanent_halt": True,
            "hard_account_risk_precedes_daily_circuit": True,
            "high_watermark_drawdown_gate": True,
            "current_margin_gate": True,
            "available_cash_gate": True,
            "causal_completed_return_risk_governor": {
                "lookback_days": int(base_sim.risk_governor.lookback_days),
                "sample_volatility_trigger": float(
                    base_sim.risk_governor.volatility_trigger
                ),
                "completed_daily_loss_trigger": float(
                    base_sim.risk_governor.loss_trigger
                ),
                "defensive_scale": float(base_sim.risk_governor.defensive_scale),
            },
            "prepared_contract_indexes_reused_across_windows": True,
            "state_reset_per_window": True,
        },
        "base": base,
        "stress": stress,
        "production_gap": production_gap,
        "limitations": [
            "historical daily L1 bid/ask/depth and partial fills are unavailable",
            "margin uses an explicit proxy rather than historical broker schedules",
            "daily bars cannot identify the exact intraday instant at which an account risk gate would fire",
            "each published window resets account state to initial capital and flat positions; first-day prior holdings are not inherited",
            "production-mechanics historical return is not a forecast or guarantee of future live return",
        ],
        "_base_daily": base_daily,
        "_stress_daily": stress_daily,
    }


def evaluate(specific_raw: pd.DataFrame, continuous_raw: pd.DataFrame) -> dict:
    # Import the expensive research chain only when the full L4 evaluator actually runs.
    # Unit tests for mechanics therefore need only the package's normal dev dependencies.
    import evaluate_execution_aligned_target as float_l4

    # The policy weights are generated once from the full frozen history and are never
    # re-fit by reporting window. Only account state is reset per standalone experiment.
    weights = float_l4.generate_execution_signal_weights(continuous_raw)
    historical = float_l4.evaluate(specific_raw, continuous_raw)
    return evaluate_with_weights(specific_raw, weights, float_report=historical)


def _jsonable(report: dict) -> dict:
    return {
        key: value
        for key, value in report.items()
        if not key.startswith("_")
    }


def main() -> None:
    runtime = Path("runtime")
    specific_path = runtime / "return_target_specific_contracts.csv"
    continuous_path = runtime / "broad_daily_universe.csv"
    missing = [
        str(path)
        for path in (specific_path, continuous_path)
        if not path.exists()
    ]
    if missing:
        raise SystemExit(
            f"directional production mechanics inputs missing: {missing}"
        )
    report = evaluate(
        pd.read_csv(specific_path),
        pd.read_csv(continuous_path),
    )
    report["_base_daily"].to_csv(
        runtime / "directional_production_base_daily.csv"
    )
    report["_stress_daily"].to_csv(
        runtime / "directional_production_stress_daily.csv"
    )
    output = runtime / "directional_production_mechanics_report.json"
    output.write_text(
        json.dumps(
            _jsonable(report),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    print(json.dumps(_jsonable(report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
