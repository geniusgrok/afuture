"""Constrained research allocation from signed forecasts, covariance and costs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from .directional_data_validation import validate_daily_index
from .mature_market_forecast import _day


@dataclass(frozen=True)
class ForecastPortfolio:
    ready: bool
    weights: dict[str, float]
    audit: dict


def _prox(values, threshold, *, cap=0.25, budget=2.0):
    magnitude = np.maximum(np.abs(values) - threshold, 0.0)
    clipped = np.minimum(cap, magnitude)
    if clipped.sum() > budget:
        low, high = 0.0, float(magnitude.max())
        for _ in range(55):
            middle = (low + high) / 2
            if np.minimum(cap, np.maximum(magnitude - middle, 0)).sum() > budget:
                low = middle
            else:
                high = middle
        clipped = np.minimum(cap, np.maximum(magnitude - high, 0))
    return np.sign(values) * clipped


def forecast_covariance_portfolio(
    forecasts: Mapping[str, float],
    completed_market_returns: pd.DataFrame,
    *,
    decision_day,
    risk_eligibility: Literal["all_assets", "per_asset"] = "all_assets",
) -> ForecastPortfolio:
    """Maximize a fixed five-session return/covariance/cost utility.

    Utility is mu'w - .003||w||1 - (1/.15)/2 * w'Cov5*w, with per-product
    absolute weight<=.25 and gross<=2. The risk preference uses a fixed reference
    Sharpe1 and annual risk .15, not an estimated achievable Sharpe. Covariance
    uses exactly63 completed rows, half sample and half diagonal. The optimum is
    subsequently only reduced to the .15 annual risk ceiling; this reduction is
    audited separately. Actual fills and fees remain the account's responsibility.

    ``all_assets`` retains the original all-or-nothing history gate. Research can
    explicitly select ``per_asset`` to close only products lacking all63 valid
    observations or nonzero risk. Both modes use the same trailing session rows:
    missing observations are never dropped to reach further back in history.
    """
    if risk_eligibility not in ("all_assets", "per_asset"):
        raise ValueError("unknown risk eligibility policy")
    day = _day(decision_day)
    products = sorted(forecasts)
    if any(not isinstance(p, str) or not p or p != p.strip().upper() for p in products):
        raise ValueError("forecasts need canonical product identities")
    mu = np.array([forecasts[p] for p in products], dtype=float)
    if not np.isfinite(mu).all() or any(
        isinstance(forecasts[p], (bool, np.bool_)) for p in products
    ):
        raise ValueError("forecasts must be finite numbers")
    frame = completed_market_returns.copy()
    validate_daily_index(pd.DataFrame({"row": 0}, index=frame.index), name="allocation returns")
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index))
    if frame.index.tz is not None or not frame.index.equals(frame.index.normalize()):
        raise ValueError("allocation returns require timezone-naive session dates")
    if frame.columns.has_duplicates:
        raise ValueError("allocation return products duplicated")
    sample = frame.loc[frame.index < day].iloc[-63:]
    audit = {
        "decision_day": day.date().isoformat(),
        "products": products,
        "forecast_products": products,
        "risk_eligibility": risk_eligibility,
        "observations": len(sample),
        "risk_history": {},
        "covariance_products": [],
        "included_products": [],
        "risk_window_first": sample.index[0].date().isoformat() if len(sample) else None,
        "risk_window_through": sample.index[-1].date().isoformat() if len(sample) else None,
    }

    def unavailable(reason):
        return ForecastPortfolio(False, dict.fromkeys(products, 0.0), {**audit, "status": reason})

    if not products:
        return ForecastPortfolio(True, {}, {**audit, "status": "no_forecasts"})
    eligible = []
    invalid_returns = False
    for product in products:
        if product not in sample.columns:
            count, reason = 0, "missing_risk_product"
        else:
            history = sample[product].to_numpy(dtype=float)
            count = int(np.isfinite(history).sum())
            if len(sample) != 63:
                reason = "insufficient_completed_risk_history"
            elif np.isinf(history).any() or (history <= -1).any():
                reason, invalid_returns = "invalid_market_risk_returns", True
            elif count == 0:
                # A column introduced by future rows is absent at this decision.
                reason = "missing_risk_product"
            elif not np.isfinite(history).all():
                reason = "missing_completed_risk_observation"
            elif np.var(history, ddof=1) <= 0:
                reason = "zero_observed_risk"
            else:
                reason = "eligible"
                eligible.append(product)
        audit["risk_history"][product] = {
            "observations": count,
            "eligible": reason == "eligible",
            "included": False,
            "reason": reason,
        }
    audit["eligible_products"] = eligible
    audit["excluded_products"] = [product for product in products if product not in eligible]
    if len(sample) != 63 or (
        risk_eligibility == "all_assets" and not set(products) <= set(sample.columns)
    ):
        return unavailable("insufficient_completed_risk_history")
    if invalid_returns:
        raise ValueError("invalid market risk returns")
    if risk_eligibility == "all_assets" and len(eligible) != len(products):
        reason = (
            "missing_completed_risk_observation"
            if any(
                item["reason"] in ("missing_risk_product", "missing_completed_risk_observation")
                for item in audit["risk_history"].values()
            )
            else "zero_observed_risk"
        )
        return unavailable(reason)
    if not eligible:
        return unavailable("no_eligible_risk_assets")
    audit["covariance_products"] = eligible
    audit["included_products"] = eligible
    for product in eligible:
        audit["risk_history"][product]["included"] = True
    values = sample[eligible].to_numpy(dtype=float)
    mu = np.array([forecasts[product] for product in eligible], dtype=float)
    covariance = np.atleast_2d(np.cov(values, rowvar=False, ddof=1))
    covariance = 0.5 * covariance + 0.5 * np.diag(np.diag(covariance))
    if (np.diag(covariance) <= 0).any():
        return unavailable("zero_observed_risk")
    quadratic = covariance * (5 / 0.15)
    step = 1.0 / float(np.linalg.eigvalsh(quadratic).max())
    weights = momentum = np.zeros(len(eligible))
    acceleration = 1.0
    converged = False
    residual = 0.0
    for _iteration in range(1, 5001):
        updated = _prox(momentum - step * (quadratic @ momentum - mu), step * 0.003)
        residual = float(
            np.max(
                np.abs(_prox(updated - step * (quadratic @ updated - mu), step * 0.003) - updated)
            )
        )
        if residual <= 1e-9:
            weights, converged = updated, True
            break
        next_acceleration = (1 + np.sqrt(1 + 4 * acceleration**2)) / 2
        momentum = updated + ((acceleration - 1) / next_acceleration) * (updated - weights)
        weights, acceleration = updated, next_acceleration
    if not converged:
        return unavailable("optimizer_not_converged")
    variance = float(weights @ covariance @ weights)
    annual_risk = float(np.sqrt(max(0.0, variance) * 252))
    scale = min(1.0, 0.15 / annual_risk) if annual_risk > 0 else 1.0
    utility = float(
        mu @ weights - 0.003 * np.abs(weights).sum() - 0.5 * weights @ quadratic @ weights
    )
    deployed = weights * scale
    if (np.abs(deployed) > 0.25 + 1e-12).any() or np.abs(deployed).sum() > 2 + 1e-12:
        raise ValueError("allocation constraints violated")
    return ForecastPortfolio(
        True,
        {**dict.fromkeys(products, 0.0), **dict(zip(eligible, deployed.tolist(), strict=True))},
        {
            **audit,
            "status": "allocated" if np.abs(deployed).sum() > 0 else "no_net_edge",
            "prior_through": sample.index[-1].date().isoformat(),
            "iterations": _iteration,
            "proximal_residual": residual,
            "pre_scale_utility": utility,
            "pre_scale_annual_risk": annual_risk,
            "risk_scale": scale,
            "post_scale_annual_risk": annual_risk * scale,
            "gross": float(np.abs(deployed).sum()),
        },
    )
