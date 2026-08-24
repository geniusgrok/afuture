"""Exploration-independent baseline signal outcomes for Shadow MPV allocation.

The ledger is independent of candidate positions. A row is the fixed baseline target
sign multiplied by the roll-safe concrete-contract intraday return for that same trading
day. The outcome is observable only after that trading day completes, so a decision on D
may use rows strictly before D.

Product expected returns use an expanding, parameter-free empirical shrinkage: each
product mean is shrunk toward the global mean with prior strength equal to the median
positive product observation count. Monetary allocation evaluates current one-lot
notional times expected return times the caller-supplied causal remaining lifecycle,
minus exact transition cost. Omitting lifecycle evidence is exactly the original one-day
objective.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Mapping

import numpy as np
import pandas as pd

from .directional_integer_optimizer import IntegerOptimizationResult, optimize_integer_targets

_EPS = 1e-12
_OUTCOME_COLUMNS = ("date", "product", "gross_return", "baseline_abs_weight")


@dataclass(frozen=True)
class ShadowOutcomeOptimization:
    optimization: IntegerOptimizationResult
    completed_outcome_count: int
    product_expected_returns: dict[str, float]
    product_expected_horizons: dict[str, float]
    product_support: dict[str, int]
    prior_support: float
    global_expected_return: float
    evidence_through: pd.Timestamp | None


def _normalize_panel(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy().astype(float)
    result.index = pd.to_datetime(result.index, errors="coerce").normalize()
    result = result.loc[~result.index.isna()].sort_index()
    result.columns = [str(column).upper() for column in result.columns]
    return result


def build_shadow_signal_outcomes(
    baseline_weights: pd.DataFrame,
    intraday_returns: pd.DataFrame,
) -> pd.DataFrame:
    """Pair fixed baseline direction with same-day roll-safe intraday outcomes."""
    weights = _normalize_panel(baseline_weights)
    returns = _normalize_panel(intraday_returns)
    index = weights.index.intersection(returns.index)
    columns = sorted(set(weights.columns) & set(returns.columns))
    if not len(index) or not columns:
        return pd.DataFrame(columns=_OUTCOME_COLUMNS)

    rows: list[dict] = []
    for day in index:
        for product in columns:
            weight = float(weights.at[day, product]) if product in weights.columns else 0.0
            if not isfinite(weight) or abs(weight) <= _EPS:
                continue
            value = returns.at[day, product]
            try:
                raw_return = float(value)
            except (TypeError, ValueError):
                continue
            if not isfinite(raw_return):
                continue
            rows.append(
                {
                    "date": pd.Timestamp(day).normalize(),
                    "product": str(product).upper(),
                    "gross_return": float(np.sign(weight) * raw_return),
                    "baseline_abs_weight": abs(weight),
                }
            )
    if not rows:
        return pd.DataFrame(columns=_OUTCOME_COLUMNS)
    return pd.DataFrame(rows, columns=_OUTCOME_COLUMNS).sort_values(
        ["date", "product"], kind="stable", ignore_index=True
    )


def _completed_outcomes(shadow_outcomes: pd.DataFrame, decision_date) -> pd.DataFrame:
    if shadow_outcomes.empty:
        return pd.DataFrame(columns=_OUTCOME_COLUMNS)
    missing = set(_OUTCOME_COLUMNS) - set(shadow_outcomes.columns)
    if missing:
        raise ValueError(f"shadow outcome ledger missing columns: {sorted(missing)}")
    cutoff = pd.to_datetime(decision_date, errors="coerce")
    if pd.isna(cutoff):
        raise ValueError("decision_date must be valid")
    frame = shadow_outcomes.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    frame["product"] = frame["product"].astype(str).str.upper()
    frame["gross_return"] = pd.to_numeric(frame["gross_return"], errors="coerce")
    frame["baseline_abs_weight"] = pd.to_numeric(
        frame["baseline_abs_weight"], errors="coerce"
    )
    frame = frame[
        frame["date"].notna()
        & (frame["date"] < pd.Timestamp(cutoff).normalize())
        & frame["gross_return"].notna()
        & np.isfinite(frame["gross_return"].to_numpy(float))
    ].copy()
    return frame.sort_values(["date", "product"], kind="stable")


def _shrunk_product_returns(
    completed: pd.DataFrame,
    products: list[str],
) -> tuple[dict[str, float], dict[str, int], float, float]:
    if completed.empty:
        return ({product: 0.0 for product in products}, {product: 0 for product in products}, 0.0, 0.0)
    grouped = completed.groupby("product", sort=True)["gross_return"]
    counts_series = grouped.count().astype(int)
    means_series = grouped.mean().astype(float)
    positive_counts = counts_series[counts_series > 0]
    if positive_counts.empty:
        return ({product: 0.0 for product in products}, {product: 0 for product in products}, 0.0, 0.0)
    prior_support = float(positive_counts.median())
    global_mean = float(completed["gross_return"].astype(float).mean())
    if not isfinite(global_mean):
        global_mean = 0.0

    estimates: dict[str, float] = {}
    supports: dict[str, int] = {}
    for product in products:
        support = int(counts_series.get(product, 0))
        supports[product] = support
        if support > 0:
            local = float(means_series.loc[product])
            weight = support / (support + prior_support) if prior_support > 0.0 else 1.0
            estimate = weight * local + (1.0 - weight) * global_mean
        else:
            estimate = global_mean
        estimates[product] = float(estimate if isfinite(estimate) else global_mean)
    return estimates, supports, prior_support, global_mean


def _normalize_expected_horizons(
    products: list[str],
    expected_horizons: Mapping[str, float] | None,
) -> dict[str, float]:
    source = {
        str(product).upper(): float(value)
        for product, value in (expected_horizons or {}).items()
    }
    result: dict[str, float] = {}
    for product in products:
        value = float(source.get(product, 1.0))
        if not isfinite(value) or value < 1.0:
            raise ValueError("expected lifecycle horizon must be finite and >= 1")
        result[product] = value
    return result


def optimize_with_shadow_outcomes(
    *,
    shadow_outcomes: pd.DataFrame,
    decision_date,
    reference_lots: Mapping[str, int],
    requested_lots: Mapping[str, int],
    current_lots: Mapping[str, int],
    symbol_products: Mapping[str, str],
    lot_notionals: Mapping[str, float],
    per_lot_margin: Mapping[str, float],
    equity: float,
    soft_margin_budget: float,
    max_gross_ratio: float,
    max_abs_lots: int,
    cost_rate: float,
    expected_horizons: Mapping[str, float] | None = None,
) -> ShadowOutcomeOptimization:
    """Optimize monetary lifecycle net edge from completed exogenous outcomes."""
    if not isfinite(float(cost_rate)) or float(cost_rate) < 0.0:
        raise ValueError("cost_rate must be finite and nonnegative")
    completed = _completed_outcomes(shadow_outcomes, decision_date)
    products = sorted({str(value).upper() for value in symbol_products.values()})
    estimates, supports, prior_support, global_mean = _shrunk_product_returns(
        completed, products
    )
    horizons = _normalize_expected_horizons(products, expected_horizons)
    reference = {
        str(symbol): int(volume)
        for symbol, volume in reference_lots.items()
        if int(volume)
    }
    evidence_through = (
        pd.Timestamp(completed["date"].max()).normalize() if not completed.empty else None
    )
    if completed.empty:
        fallback = IntegerOptimizationResult(
            target_lots=dict(reference),
            reference_objective=0.0,
            objective_value=0.0,
            fallback_reason="insufficient shadow outcome evidence",
        )
        return ShadowOutcomeOptimization(
            optimization=fallback,
            completed_outcome_count=0,
            product_expected_returns=estimates,
            product_expected_horizons=horizons,
            product_support=supports,
            prior_support=prior_support,
            global_expected_return=global_mean,
            evidence_through=evidence_through,
        )

    current = {
        str(symbol): int(volume)
        for symbol, volume in current_lots.items()
        if int(volume)
    }
    product_by_symbol = {
        str(symbol): str(product).upper()
        for symbol, product in symbol_products.items()
    }

    def objective(target: Mapping[str, int]) -> float:
        expected_gross_alpha = 0.0
        for symbol, volume in target.items():
            product = product_by_symbol[str(symbol)]
            expected_return = float(estimates[product])
            expected_horizon = float(horizons[product])
            expected_gross_alpha += (
                abs(int(volume))
                * float(lot_notionals[str(symbol)])
                * expected_return
                * expected_horizon
            )
        transition_cost = 0.0
        for symbol in set(current) | set(target):
            delta = int(target.get(symbol, 0)) - int(current.get(symbol, 0))
            if delta:
                transition_cost += (
                    abs(delta)
                    * float(lot_notionals[str(symbol)])
                    * float(cost_rate)
                )
        return float(expected_gross_alpha - transition_cost)

    optimization = optimize_integer_targets(
        reference_lots=reference,
        requested_lots=requested_lots,
        current_lots=current,
        lot_notionals=lot_notionals,
        per_lot_margin=per_lot_margin,
        equity=float(equity),
        soft_margin_budget=float(soft_margin_budget),
        max_gross_ratio=float(max_gross_ratio),
        max_abs_lots=int(max_abs_lots),
        objective=objective,
    )
    return ShadowOutcomeOptimization(
        optimization=optimization,
        completed_outcome_count=int(len(completed)),
        product_expected_returns=estimates,
        product_expected_horizons=horizons,
        product_support=supports,
        prior_support=prior_support,
        global_expected_return=global_mean,
        evidence_through=evidence_through,
    )
