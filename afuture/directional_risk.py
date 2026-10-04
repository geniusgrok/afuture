"""Causal risk scaling for the execution-aligned directional portfolio."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from statistics import stdev
from typing import Protocol

import numpy as np
import pandas as pd


class DirectionalRiskResponseMode(str, Enum):
    """Declare whether a manager scales weights or freezes only new lot risk."""

    TARGET_SCALE = "target_scale"
    FREEZE_NEW_RISK = "freeze_new_risk"


class DirectionalRiskScale(Protocol):
    """Behavioral contract for causal target scaling policies."""

    def scale(self, completed_returns: Iterable[float]) -> float: ...


@dataclass(frozen=True)
class DirectionalRiskGovernor:
    """Scale future targets using completed account returns only.

    The governor never increases the frozen policy target. It preserves the normal
    target and moves to the defensive scale after either a completed daily loss or
    two-day sample-volatility trigger. Current-session PnL is deliberately excluded.
    """

    lookback_days: int = 2
    volatility_trigger: float = 0.03
    loss_trigger: float = 0.02
    defensive_scale: float = 0.25

    def __post_init__(self) -> None:
        if self.lookback_days < 2:
            raise ValueError("directional risk lookback_days must be >= 2")
        if self.volatility_trigger <= 0:
            raise ValueError("directional volatility_trigger must be positive")
        if self.loss_trigger <= 0:
            raise ValueError("directional loss_trigger must be positive")
        if not 0 < self.defensive_scale <= 1:
            raise ValueError("directional defensive_scale must be in (0, 1]")

    def scale(self, completed_returns: Iterable[float]) -> float:
        values = [float(value) for value in completed_returns if isfinite(float(value))]
        if values and values[-1] <= -self.loss_trigger:
            return self.defensive_scale
        sample = values[-self.lookback_days :]
        if len(sample) >= self.lookback_days and stdev(sample) >= self.volatility_trigger:
            return self.defensive_scale
        return 1.0


class DirectionalRiskScaledPolicy:
    """Decorate a frozen directional policy with the causal risk scale."""

    def __init__(
        self,
        policy,
        *,
        completed_returns_provider: Callable[[], Iterable[float]],
        governor: DirectionalRiskGovernor | None = None,
    ) -> None:
        self.policy = policy
        self.completed_returns_provider = completed_returns_provider
        self.governor = governor or DirectionalRiskGovernor()

    def target_weights(self, *args, **kwargs) -> dict[str, float]:
        weights = self.policy.target_weights(*args, **kwargs)
        scale = self.governor.scale(self.completed_returns_provider())
        return {str(product): float(weight) * scale for product, weight in weights.items()}

    def __getattr__(self, name):
        return getattr(self.policy, name)


@dataclass(frozen=True)
class CovarianceRiskBudgetDecision:
    """An ex-ante target scale, including explicit missing-evidence decisions."""

    scale: float
    forecast_annualized_volatility: float | None
    reason: str
    observation_count: int
    active_products: tuple[str, ...]


def covariance_risk_budget(
    product_weights: Mapping[str, float],
    completed_market_returns: pd.DataFrame,
    *,
    lookback_days: int = 63,
    target_annualized_volatility: float = 0.15,
    annualization: int = 252,
) -> CovarianceRiskBudgetDecision:
    """Shrink target risk using 50% sample covariance and 50% its diagonal.

    The caller owns the strictly-completed-session cut. Only the final contiguous
    ``lookback_days`` rows are used: missing observations are never dropped or filled.
    Ordinary negative returns and short weights are valid; returns at or below -100%
    and infinite values are invalid. Missing active observations prohibit all targets.
    """
    if isinstance(lookback_days, bool) or not isinstance(lookback_days, int) or lookback_days < 2:
        raise ValueError("covariance lookback_days must be an integer >= 2")
    target = float(target_annualized_volatility)
    if not isfinite(target) or target <= 0.0:
        raise ValueError("target annualized volatility must be finite and positive")
    if isinstance(annualization, bool) or not isinstance(annualization, int) or annualization <= 0:
        raise ValueError("annualization must be a positive integer")
    weights: dict[str, float] = {}
    for raw_product, raw_weight in product_weights.items():
        product = str(raw_product).upper()
        weight = float(raw_weight)
        if not product or product in weights or not isfinite(weight):
            raise ValueError("risk weights require unique products and finite values")
        weights[product] = weight
    active = tuple(sorted(product for product, weight in weights.items() if weight != 0.0))
    sample = completed_market_returns.iloc[-lookback_days:].copy()
    sample.columns = [str(product).upper() for product in sample.columns]
    if sample.columns.has_duplicates:
        raise ValueError("market returns contain duplicate products")
    count = len(sample)

    def decision(scale: float, forecast: float | None, reason: str):
        return CovarianceRiskBudgetDecision(scale, forecast, reason, count, active)

    if not active:
        return decision(0.0, 0.0, "no_active_targets")
    missing = sorted(set(active) - set(sample.columns))
    if missing:
        return decision(0.0, None, "missing_active_products:" + ",".join(missing))
    values = sample.loc[:, list(active)].to_numpy(dtype=float)
    if bool(np.isinf(values).any()) or bool((values <= -1.0).any()):
        raise ValueError("active market returns must be finite and greater than -100%")
    if count < lookback_days:
        return decision(0.0, None, "insufficient_completed_history")
    if bool(np.isnan(values).any()):
        return decision(0.0, None, "missing_active_observations")
    vector = np.asarray([weights[product] for product in active], dtype=float)
    covariance = np.atleast_2d(np.cov(values, rowvar=False, ddof=1))
    shrunk = 0.5 * covariance + 0.5 * np.diag(np.diag(covariance))
    variance = float(vector @ shrunk @ vector) * annualization
    if not isfinite(variance) or variance < 0.0:
        raise ValueError("forecast covariance risk must be finite and non-negative")
    forecast = float(np.sqrt(variance))
    scale = min(1.0, target / forecast) if forecast > 0.0 else 1.0
    return decision(scale, forecast, "risk_budget_scaled" if scale < 1.0 else "within_risk_budget")
