"""Research-only causal margin-capacity models for the directional portfolio.

Nothing in this module is imported by live or production acceptance code. Candidates
must pass fixed Production L3 before any separate production implementation is allowed.
"""
from __future__ import annotations

from statistics import stdev
from typing import Iterable


def shock_derived_margin_sizing_share(
    *,
    max_margin_ratio: float,
    min_available_ratio: float,
    max_daily_loss_ratio: float,
    max_gross_ratio: float,
    stress_cost_bps: float,
    completed_returns: Iterable[float] = (),
) -> float:
    """Derive a soft share from hard gates, completed shocks and full-reversal cost.

    The adverse-move reserve is the larger of the configured one-day loss gate, the
    latest two completed absolute returns, and their two-point sample volatility. A
    full +gross to -gross rotation reserves ``2 * gross`` one-way turnover at the fixed
    Stress cost endpoint. No future/current-session return is an input.
    """
    margin = float(max_margin_ratio)
    available = float(min_available_ratio)
    daily_loss = float(max_daily_loss_ratio)
    gross = float(max_gross_ratio)
    cost_bps = float(stress_cost_bps)
    if not 0.0 < margin < 1.0:
        raise ValueError("max_margin_ratio must be in (0, 1)")
    if not 0.0 <= available < 1.0:
        raise ValueError("min_available_ratio must be in [0, 1)")
    if not 0.0 < daily_loss < 1.0:
        raise ValueError("max_daily_loss_ratio must be in (0, 1)")
    if gross <= 0.0 or cost_bps < 0.0:
        raise ValueError("gross must be positive and stress cost non-negative")

    hard_share = min(margin, 1.0 - available)
    sample = [float(value) for value in completed_returns][-2:]
    sample_volatility = stdev(sample) if len(sample) >= 2 else 0.0
    observed_shock = max([0.0, *(abs(value) for value in sample)])
    adverse_move = max(daily_loss, observed_shock, sample_volatility)
    rotation_cost = 2.0 * gross * cost_bps / 10000.0
    remaining_equity = max(0.0, 1.0 - adverse_move - rotation_cost)
    return max(0.0, min(hard_share, hard_share * remaining_equity))
