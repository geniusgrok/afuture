"""Causal broad-universe screen for economically related futures pairs.

This L3 screen adapts the robust parts of rolling cointegration / volatility-regime
pairs trading without importing heavy modelling dependencies. It intentionally limits
pairs to economically related contracts on the same exchange so any survivor can later
reuse afuture's existing two-leg execution and exchange semantics.

Continuous daily contracts are used only for family screening. Production promotion
still requires specific-contract and CTP Shadow evidence.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from math import log
from pathlib import Path

import numpy as np
import pandas as pd

WINDOWS = {
    "prior1": ("2022-08-22", "2023-08-20"),
    "prior2": ("2023-08-21", "2024-08-20"),
    "train": ("2024-08-21", "2025-08-20"),
    "validation": ("2025-08-21", "2026-02-20"),
    "selection_full": ("2024-08-21", "2026-02-20"),
    "oos": ("2026-02-21", "2026-08-20"),
    "full_recent": ("2024-08-21", "2026-08-20"),
}

BASE_COST_BPS = 15.0
STRESS_COST_BPS = 30.0
MAX_GROSS_LEVERAGE = 2.0
LEVERAGE_GRID = (0.5, 1.0, 1.5, 2.0)
MAX_CALIBRATION_DRAWDOWN = -0.15
MAX_TARGET_DRAWDOWN = -0.20


@dataclass(frozen=True)
class EconomicPair:
    left: str
    right: str
    exchange: str
    family: str


PAIRS = (
    # DCE: oilseed/grain and polymers
    EconomicPair("A", "M", "DCE", "soy"),
    EconomicPair("A", "Y", "DCE", "soy"),
    EconomicPair("M", "Y", "DCE", "soy"),
    EconomicPair("P", "Y", "DCE", "edible_oil"),
    EconomicPair("C", "CS", "DCE", "corn"),
    EconomicPair("L", "PP", "DCE", "polymer"),
    EconomicPair("L", "V", "DCE", "polymer"),
    EconomicPair("PP", "V", "DCE", "polymer"),
    EconomicPair("EB", "L", "DCE", "petrochemical"),
    EconomicPair("J", "JM", "DCE", "coal"),
    # CZCE
    EconomicPair("FG", "SA", "CZCE", "glass_soda"),
    EconomicPair("SF", "SM", "CZCE", "ferroalloy"),
    # SHFE
    EconomicPair("RB", "HC", "SHFE", "steel"),
    EconomicPair("CU", "ZN", "SHFE", "base_metal"),
    EconomicPair("CU", "AL", "SHFE", "base_metal"),
    EconomicPair("CU", "PB", "SHFE", "base_metal"),
    EconomicPair("AL", "ZN", "SHFE", "base_metal"),
    EconomicPair("PB", "ZN", "SHFE", "base_metal"),
    EconomicPair("NI", "SS", "SHFE", "stainless"),
    EconomicPair("AU", "AG", "SHFE", "precious"),
    EconomicPair("BU", "FU", "SHFE", "fuel"),
)


@dataclass(frozen=True)
class PairProfile:
    formation: int
    entry_z: float
    min_correlation: float
    min_volatility_ratio: float
    exit_z: float = 0.5
    stop_z: float = 4.0
    max_holding_days: int = 60


PROFILES = tuple(
    PairProfile(formation, entry_z, min_correlation, min_volatility_ratio)
    for formation in (60, 120)
    for entry_z in (1.5, 2.0, 2.5)
    for min_correlation in (0.4, 0.6)
    for min_volatility_ratio in (0.7, 1.0)
)


def _metrics(series: pd.Series) -> dict:
    values = series.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    if values.empty:
        return {
            "days": 0,
            "active_days": 0,
            "total_return": 0.0,
            "annualized_return": 0.0,
            "annualized_volatility": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
        }
    equity = (1.0 + values).cumprod()
    total = float(equity.iloc[-1] - 1.0)
    annualized = (1.0 + total) ** (252.0 / len(values)) - 1.0 if total > -1.0 else -1.0
    standard_deviation = float(values.std(ddof=1))
    volatility = standard_deviation * np.sqrt(252.0)
    sharpe = (
        float(values.mean() / standard_deviation * np.sqrt(252.0))
        if standard_deviation > 0
        else 0.0
    )
    drawdown = equity / equity.cummax() - 1.0
    return {
        "days": int(len(values)),
        "active_days": int((values != 0.0).sum()),
        "total_return": total,
        "annualized_return": float(annualized),
        "annualized_volatility": float(volatility),
        "sharpe": sharpe,
        "max_drawdown": float(drawdown.min()),
    }


def _window_metrics(series: pd.Series, window: str) -> dict:
    start, end = WINDOWS[window]
    return _metrics(series.loc[pd.Timestamp(start) : pd.Timestamp(end)])


def _load_panel(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    frame = raw.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame[frame["close"] > 0].dropna(subset=["date", "product", "close"])
    frame = frame.drop_duplicates(["date", "product"], keep="last")
    close = frame.pivot(index="date", columns="product", values="close").sort_index().astype(float)
    returns = close.pct_change(fill_method=None).mask(lambda values: values.abs() > 0.20)
    return close, returns


def _pair_statistics(
    close: pd.DataFrame,
    returns: pd.DataFrame,
    pair: EconomicPair,
    formation: int,
) -> dict[str, pd.Series]:
    left = np.log(close[pair.left])
    right = np.log(close[pair.right])

    right_mean = right.rolling(formation, min_periods=formation).mean().shift(1)
    left_mean = left.rolling(formation, min_periods=formation).mean().shift(1)
    covariance = right.rolling(formation, min_periods=formation).cov(left).shift(1)
    variance = right.rolling(formation, min_periods=formation).var().shift(1)
    beta = covariance / variance
    alpha = left_mean - beta * right_mean

    residual = left - (alpha + beta * right)
    residual_std = residual.rolling(formation, min_periods=max(20, formation // 2)).std().shift(1)
    zscore = residual / residual_std
    correlation = right.rolling(formation, min_periods=formation).corr(left).shift(1)

    # Lag-one residual persistence is a lightweight causal OU proxy. A finite
    # positive half-life is required; non-reverting or explosive residuals fail closed.
    phi = (
        residual.rolling(formation, min_periods=max(20, formation // 2))
        .corr(residual.shift(1))
        .shift(1)
    )
    half_life = pd.Series(999.0, index=close.index)
    valid_phi = (phi > 0.0) & (phi < 0.9999)
    half_life.loc[valid_phi] = -log(2.0) / np.log(phi.loc[valid_phi])

    normalized_return = (returns[pair.left] - beta * returns[pair.right]) / (1.0 + beta.abs())
    fast_volatility = normalized_return.rolling(20, min_periods=20).std().shift(1)
    formation_volatility = (
        normalized_return.rolling(formation, min_periods=max(20, formation // 2)).std().shift(1)
    )
    volatility_ratio = fast_volatility / formation_volatility

    return {
        "beta": beta,
        "zscore": zscore,
        "correlation": correlation,
        "half_life": half_life,
        "volatility_ratio": volatility_ratio,
    }


def _simulate_pair(
    close: pd.DataFrame,
    returns: pd.DataFrame,
    pair: EconomicPair,
    profile: PairProfile,
    statistics: dict[str, pd.Series],
    *,
    cost_bps: float,
) -> tuple[pd.Series, list[pd.Timestamp]]:
    index = close.index
    left_returns = returns[pair.left].reindex(index).to_numpy(float)
    right_returns = returns[pair.right].reindex(index).to_numpy(float)
    beta = statistics["beta"].reindex(index).to_numpy(float)
    zscore = statistics["zscore"].reindex(index).to_numpy(float)
    correlation = statistics["correlation"].reindex(index).to_numpy(float)
    half_life = statistics["half_life"].reindex(index).to_numpy(float)
    volatility_ratio = statistics["volatility_ratio"].reindex(index).to_numpy(float)

    pnl = np.zeros(len(index), dtype=float)
    position = 0
    left_weight = 0.0
    right_weight = 0.0
    entry_index = -1
    entries: list[pd.Timestamp] = []

    for position_index in range(len(index) - 1):
        current_z = zscore[position_index]
        if position == 0:
            eligible = (
                np.isfinite(current_z)
                and np.isfinite(beta[position_index])
                and beta[position_index] > 0.0
                and np.isfinite(correlation[position_index])
                and correlation[position_index] >= profile.min_correlation
                and np.isfinite(half_life[position_index])
                and 2.0 <= half_life[position_index] <= 60.0
                and np.isfinite(volatility_ratio[position_index])
                and volatility_ratio[position_index] >= profile.min_volatility_ratio
            )
            direction = 0
            if eligible and current_z >= profile.entry_z:
                direction = -1
            elif eligible and current_z <= -profile.entry_z:
                direction = 1
            if direction:
                position = direction
                entry_index = position_index
                entries.append(pd.Timestamp(index[position_index]))
                current_beta = float(beta[position_index])
                normalization = 1.0 + abs(current_beta)
                left_weight = direction / normalization
                right_weight = direction * (-current_beta / normalization)
                pnl[position_index] -= cost_bps / 10000.0
        else:
            holding_days = position_index - entry_index
            exit_now = (
                not np.isfinite(current_z)
                or abs(current_z) <= profile.exit_z
                or abs(current_z) >= profile.stop_z
                or holding_days >= profile.max_holding_days
            )
            if exit_now:
                pnl[position_index] -= cost_bps / 10000.0
                position = 0
                entry_index = -1
                left_weight = 0.0
                right_weight = 0.0

        # Decision at close t earns only t->t+1 return; this prevents same-bar look-ahead.
        if position != 0:
            next_index = position_index + 1
            left_value = left_returns[next_index]
            right_value = right_returns[next_index]
            if np.isfinite(left_value) and np.isfinite(right_value):
                pnl[next_index] += left_weight * left_value + right_weight * right_value

    if position != 0:
        pnl[-1] -= cost_bps / 10000.0
    return pd.Series(pnl, index=index), entries


def _qualifies(metrics: dict, minimum_active_days: int = 3) -> bool:
    return (
        metrics["active_days"] >= minimum_active_days
        and metrics["annualized_return"] > 0.0
        and metrics["sharpe"] > 0.0
        and metrics["max_drawdown"] > -0.15
    )


def _portfolio(series: dict[str, pd.Series], pair_ids: list[str], index) -> pd.Series:
    if not pair_ids:
        return pd.Series(0.0, index=index)
    return pd.concat([series[pair_id] for pair_id in pair_ids], axis=1).mean(axis=1)


def _profile_id(profile: PairProfile) -> str:
    return (
        f"f{profile.formation}_e{profile.entry_z:g}_c{profile.min_correlation:g}"
        f"_v{profile.min_volatility_ratio:g}"
    )


def _choose_leverage(calibration: pd.Series) -> float:
    selected = 0.0
    for leverage in LEVERAGE_GRID:
        scaled = calibration * leverage
        item = _metrics(scaled)
        if (
            item["annualized_return"] > 0.0
            and item["max_drawdown"] > MAX_CALIBRATION_DRAWDOWN
            and bool((scaled > -1.0).all())
        ):
            selected = leverage
    return selected


def evaluate(raw: pd.DataFrame) -> dict:
    close, returns = _load_panel(raw)
    required = {item for pair in PAIRS for item in (pair.left, pair.right)}
    missing = sorted(required - set(close.columns))
    if missing:
        raise ValueError(f"broad pair screen missing products: {missing}")

    statistics_cache: dict[tuple[str, int], dict[str, pd.Series]] = {}
    results: list[dict] = []
    pair_lookup = {f"{pair.left}/{pair.right}": pair for pair in PAIRS}

    for profile in PROFILES:
        stressed_series: dict[str, pd.Series] = {}
        base_series: dict[str, pd.Series] = {}
        pair_metrics: dict[str, dict] = {}

        for pair_id, pair in pair_lookup.items():
            cache_key = (pair_id, profile.formation)
            statistics = statistics_cache.get(cache_key)
            if statistics is None:
                statistics = _pair_statistics(close, returns, pair, profile.formation)
                statistics_cache[cache_key] = statistics
            stressed, entries = _simulate_pair(
                close,
                returns,
                pair,
                profile,
                statistics,
                cost_bps=STRESS_COST_BPS,
            )
            base, _ = _simulate_pair(
                close,
                returns,
                pair,
                profile,
                statistics,
                cost_bps=BASE_COST_BPS,
            )
            stressed_series[pair_id] = stressed
            base_series[pair_id] = base
            pair_metrics[pair_id] = {
                "pair": asdict(pair),
                "entries": int(len(entries)),
                **{
                    window: _window_metrics(stressed, window)
                    for window in ("prior1", "prior2", "train", "validation", "oos", "full_recent")
                },
            }

        prior_pairs = sorted(
            pair_id
            for pair_id, item in pair_metrics.items()
            if _qualifies(item["prior1"]) and _qualifies(item["prior2"])
        )
        current_pairs = sorted(
            pair_id
            for pair_id, item in pair_metrics.items()
            if _qualifies(item["train"]) and _qualifies(item["validation"])
        )
        prior_portfolio = _portfolio(stressed_series, prior_pairs, close.index)
        current_stress = _portfolio(stressed_series, current_pairs, close.index)
        current_base = _portfolio(base_series, current_pairs, close.index)

        prior1 = _window_metrics(prior_portfolio, "prior1")
        prior2 = _window_metrics(prior_portfolio, "prior2")
        prior_forward = _window_metrics(prior_portfolio, "train")
        train = _window_metrics(current_stress, "train")
        validation = _window_metrics(current_stress, "validation")
        row = {
            "profile_id": _profile_id(profile),
            "profile": asdict(profile),
            "prior_pairs": prior_pairs,
            "current_pairs": current_pairs,
            "prior1": prior1,
            "prior2": prior2,
            "prior_forward_train": prior_forward,
            "train": train,
            "validation": validation,
            "oos": _window_metrics(current_stress, "oos"),
            "full_recent_stress": _window_metrics(current_stress, "full_recent"),
            "full_recent_base": _window_metrics(current_base, "full_recent"),
            "pair_metrics": pair_metrics,
        }
        row["pre_oos_pass"] = bool(
            prior_pairs
            and current_pairs
            and _qualifies(prior1)
            and _qualifies(prior2)
            and _qualifies(prior_forward)
            and _qualifies(train)
            and _qualifies(validation)
        )
        row["pre_oos_score"] = (
            min(
                prior1["sharpe"],
                prior2["sharpe"],
                prior_forward["sharpe"],
                train["sharpe"],
                validation["sharpe"],
            )
            if row["pre_oos_pass"]
            else -999.0
        )
        results.append(row)

    eligible_profiles = [row for row in results if row["pre_oos_pass"]]
    selected = (
        max(eligible_profiles, key=lambda row: row["pre_oos_score"]) if eligible_profiles else None
    )
    support = {
        "eligible_profiles": int(len(eligible_profiles)),
        "total_profiles": int(len(results)),
        "formation_60": sum(
            row["pre_oos_pass"] and row["profile"]["formation"] == 60 for row in results
        ),
        "formation_120": sum(
            row["pre_oos_pass"] and row["profile"]["formation"] == 120 for row in results
        ),
    }

    report = {
        "source": "AKShare/Sina continuous daily futures bars",
        "role": "L3 economic-pair / volatility-regime screen only",
        "historical_l1_available": False,
        "continuous_roll_series": True,
        "pristine_final_oos": False,
        "pair_count": len(PAIRS),
        "profile_count": len(PROFILES),
        "base_cost_bps_one_way": BASE_COST_BPS,
        "stress_cost_bps_one_way": STRESS_COST_BPS,
        "max_gross_leverage": MAX_GROSS_LEVERAGE,
        "support": support,
        "profiles": results,
        "selected_profile": selected["profile"] if selected else None,
        "selected_profile_id": selected["profile_id"] if selected else None,
        "selected_prior_pairs": selected["prior_pairs"] if selected else [],
        "selected_current_pairs": selected["current_pairs"] if selected else [],
        "selected_prior_forward_train": (selected["prior_forward_train"] if selected else None),
        "selected_oos_unlevered": selected["oos"] if selected else None,
        "selected_full_recent_unlevered": (selected["full_recent_stress"] if selected else None),
    }

    reasons: list[str] = []
    leverage = 0.0
    if selected is None:
        reasons.append("no economic-pair profile survives all pre-OOS gates")
        report["selected_leverage"] = leverage
        report["selected_oos"] = None
        report["selected_full_recent"] = None
    else:
        # Rebuild only the selected current portfolio for leverage calibration.
        profile = PairProfile(**selected["profile"])
        selected_series: dict[str, pd.Series] = {}
        for pair_id in selected["current_pairs"]:
            pair = pair_lookup[pair_id]
            statistics = statistics_cache[(pair_id, profile.formation)]
            series, _ = _simulate_pair(
                close,
                returns,
                pair,
                profile,
                statistics,
                cost_bps=STRESS_COST_BPS,
            )
            selected_series[pair_id] = series
        selected_portfolio = _portfolio(selected_series, selected["current_pairs"], close.index)
        selection_start, selection_end = WINDOWS["selection_full"]
        calibration = selected_portfolio.loc[
            pd.Timestamp(selection_start) : pd.Timestamp(selection_end)
        ]
        leverage = _choose_leverage(calibration)
        report["selected_leverage"] = leverage
        if leverage <= 0.0:
            reasons.append("no leverage level satisfies calibration drawdown gate")
            scaled = selected_portfolio * 0.0
        else:
            scaled = selected_portfolio * leverage
        report["selected_oos"] = _window_metrics(scaled, "oos")
        report["selected_full_recent"] = _window_metrics(scaled, "full_recent")
        if support["eligible_profiles"] < 2:
            reasons.append("profile neighborhood support is below two")
        if report["selected_oos"]["annualized_return"] <= 0.0:
            reasons.append("selected profile final OOS return is not positive")
        if report["selected_full_recent"]["annualized_return"] < 1.0:
            reasons.append("stressed two-year annualized return is below 100%")
        if report["selected_full_recent"]["max_drawdown"] <= MAX_TARGET_DRAWDOWN:
            reasons.append("stressed two-year drawdown exceeds 20%")

    report["target"] = {
        "annualized_return": 1.0,
        "max_drawdown": MAX_TARGET_DRAWDOWN,
        "target_met": not reasons,
        "reasons": reasons,
    }
    return report


def main() -> None:
    path = Path("runtime/broad_daily_universe.csv")
    if not path.exists():
        raise SystemExit("broad daily universe missing; run fetch_broad_daily_universe.py")
    report = evaluate(pd.read_csv(path))
    output = Path("runtime/broad_pair_regime_report.json")
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "support": report["support"],
                "selected_profile": report["selected_profile"],
                "selected_prior_pairs": report["selected_prior_pairs"],
                "selected_current_pairs": report["selected_current_pairs"],
                "selected_prior_forward_train": report["selected_prior_forward_train"],
                "selected_leverage": report["selected_leverage"],
                "selected_oos": report["selected_oos"],
                "selected_full_recent": report["selected_full_recent"],
                "target": report["target"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
