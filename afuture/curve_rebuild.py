"""Research-only independent curve refits and annual risk-premium interactions.

The supplied observations keep the original concrete-contract five-session label
and completed curve feature contract. Excluded products never enter training,
seasonal design or target prediction. The annual basis is known at entry time;
there is no event calendar inferred from future outcomes or parameter search.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from math import sqrt

import numpy as np
import pandas as pd

from .curve_market_forecast import FEATURES, MatureCurveForecast, forecast_from_mature_curve
from .mature_market_forecast import (
    HORIZON_SESSIONS,
    MIN_ENTRY_DAYS,
    MIN_PRODUCTS,
    TRAINING_ENTRY_DAYS,
    _day,
    _numeric,
    _pool,
)

SEASONAL_FEATURES = (
    "curve_level_annual_sin",
    "curve_change_annual_sin",
    "curve_level_annual_cos",
    "curve_change_annual_cos",
)
ANNUAL_PERIOD_DAYS = 365.25
SEASONAL_RIDGE = 20.0
GROUP_RIDGE = 20.0


def _seasonal_design(matrix: np.ndarray, days: pd.Series) -> np.ndarray:
    elapsed = (days - pd.Timestamp("2000-01-01")).dt.days.to_numpy(dtype=float)
    angle = 2 * np.pi * elapsed / ANNUAL_PERIOD_DAYS
    return np.column_stack(
        [matrix, matrix[:, :2] * np.sin(angle)[:, None], matrix[:, :2] * np.cos(angle)[:, None]]
    )


def _completed_features(frame: pd.DataFrame) -> None:
    if "feature_through" in frame:
        dates = frame.feature_through
        if pd.api.types.is_datetime64_any_dtype(dates.dtype):
            if (
                dates.dt.tz is not None
                or not dates.dropna().eq(dates.dropna().dt.normalize()).all()
            ):
                raise ValueError("session days must be timezone-naive calendar dates")
        else:
            dates = pd.to_datetime(dates.map(_day))
        if dates.isna().any() or not dates.lt(frame.entry_day).all():
            raise ValueError("curve features must precede their entry session")


def forecast_curve_rebuild(
    observations: pd.DataFrame,
    targets: pd.DataFrame,
    *,
    decision_day,
    excluded_products: Iterable[str] = (),
    groups: Mapping[str, str] | None = None,
    grouped: bool = False,
    seasonal: bool = False,
) -> MatureCurveForecast:
    """Refit an excluded pool, optionally learning annual curve-response changes.

    The default is the existing shared/grouped estimator, independently refitted.
    Seasonal mode adds four fixed annual sine/cosine interactions with curve level
    and change. Their ridge penalty20 is fixed before replay, like the existing
    industry-deviation penalty20. Each entry date has total training weight one;
    the original252-entry-date window,126-date/20-product gates and strict prior
    maturity rule remain in force. Group deviations affect the three base slopes;
    annual interactions are pooled. This routine does not allocate or trade.
    """
    if isinstance(excluded_products, str):
        raise ValueError("exclusions must be a collection of product identities")
    exclusions = tuple(excluded_products)
    if any(not isinstance(p, str) or not p or p != p.strip().upper() for p in exclusions):
        raise ValueError("exclusions must be canonical product identities")
    exclusions = tuple(sorted(set(exclusions)))
    if type(grouped) is not bool or type(seasonal) is not bool:
        raise ValueError("model modes must be boolean")
    day = _day(decision_day)

    def exclude(frame):
        if "product" not in frame or frame.columns.has_duplicates:
            raise ValueError("curve feature schema is incomplete or duplicated")
        identities = frame["product"].map(
            lambda value: value.strip().upper() if isinstance(value, str) else value
        )
        return frame.loc[~identities.isin(exclusions)].copy()

    observations, targets = exclude(observations), exclude(targets)
    if not seasonal:
        result = forecast_from_mature_curve(
            observations,
            targets,
            decision_day=day,
            excluded_products=exclusions,
            groups=groups,
            grouped=grouped,
        )
        result.audit["seasonal"] = False
        result.audit["mechanism"] = "independent_curve_refit"
        return result

    renamed = dict(zip(FEATURES, ("x1", "x2", "x3"), strict=True))

    def prepare(frame, training):
        if not set(FEATURES).issubset(frame.columns):
            raise ValueError("curve feature schema is incomplete")
        if {"x1", "x2", "x3"}.intersection(frame.columns):
            raise ValueError("ambiguous curve feature schema")
        return _pool(frame.rename(columns=renamed), exclude_ag=False, training=training)

    sample, target = prepare(observations, True), prepare(targets, False)
    if not target.entry_day.eq(day).all():
        raise ValueError("prediction entry day must equal decision day")
    sample = sample.loc[sample.maturity_day < day].copy()
    entry_days = sample.entry_day.drop_duplicates().sort_values().iloc[-TRAINING_ENTRY_DAYS:]
    sample = sample.loc[sample.entry_day.isin(entry_days)].copy()
    for frame in (sample, target):
        _completed_features(frame)
    x, values = _numeric(sample, training=True)
    target_x, target_values = _numeric(target, training=False)
    design_x, design_target = (
        _seasonal_design(x, sample.entry_day),
        _seasonal_design(target_x, target.entry_day),
    )
    names = [*FEATURES, *SEASONAL_FEATURES]
    penalty = np.r_[np.ones(3), np.full(4, SEASONAL_RIDGE)]
    group_names = []
    if grouped:
        mapping = dict(groups or {})
        if any(not isinstance(g, str) or not g for g in mapping.values()):
            raise ValueError("group identities must be nonempty strings")
        for frame in (sample, target):
            frame["group"] = frame["product"].map(mapping)
            if frame["group"].isna().any():
                raise ValueError("missing predeclared economic group")
        group_names = sorted(set(mapping.values()))
        design_x = np.column_stack(
            [design_x] + [x * sample.group.eq(g).to_numpy()[:, None] for g in group_names]
        )
        design_target = np.column_stack(
            [design_target]
            + [target_x * target.group.eq(g).to_numpy()[:, None] for g in group_names]
        )
        penalty = np.r_[penalty, np.full(3 * len(group_names), GROUP_RIDGE)]
    counts = sample.groupby("entry_day").size()
    weights = 1.0 / sample.entry_day.map(counts).to_numpy(dtype=float)
    ready = len(counts) >= MIN_ENTRY_DAYS and sample["product"].nunique() >= MIN_PRODUCTS
    audit = {
        "decision_day": day.date().isoformat(),
        "mechanism": "seasonal_curve_response",
        "status": "ready" if ready else "insufficient_mature_curve_history",
        "excluded_products": list(exclusions),
        "grouped": grouped,
        "seasonal": True,
        "training_records": len(sample),
        "training_entry_days": len(counts),
        "training_products": int(sample["product"].nunique()),
        "last_maturity_day": sample.maturity_day.max().date().isoformat() if len(sample) else None,
        "sample_weight_sum": float(weights.sum()),
        "shared_ridge_penalty": 1.0,
        "seasonal_ridge_penalty": SEASONAL_RIDGE,
        "group_deviation_penalty": GROUP_RIDGE if grouped else None,
        "annual_period_days": ANNUAL_PERIOD_DAYS,
        "coefficients": None,
    }
    forecast = target[["entry_day", "product", "symbol"]].copy()
    if not ready:
        forecast = forecast.iloc[:0]
        forecast["expected_return"] = pd.Series(dtype=float)
        return MatureCurveForecast(False, forecast, audit)
    y = values[:, 1] / (values[:, 0] * sqrt(HORIZON_SESSIONS))
    if not np.isfinite(y).all():
        raise ValueError("normalized mature curve label is non-finite")
    beta = np.linalg.solve(
        design_x.T @ (design_x * weights[:, None]) + np.diag(penalty),
        design_x.T @ (weights * y),
    )
    expected = design_target @ beta * target_values[:, 0] * sqrt(HORIZON_SESSIONS)
    if not np.isfinite(beta).all() or not np.isfinite(expected).all():
        raise ValueError("curve forecast is non-finite")
    audit["coefficients"] = {
        "shared": dict(zip(names, beta[:7].tolist(), strict=True)),
        "group_deviations": {
            group: dict(zip(FEATURES, beta[7 + 3 * i : 10 + 3 * i].tolist(), strict=True))
            for i, group in enumerate(group_names)
        },
    }
    forecast["expected_return"] = expected
    return MatureCurveForecast(True, forecast.reset_index(drop=True), audit)
