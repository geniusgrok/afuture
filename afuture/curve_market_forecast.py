"""Research forecasts from completed contract curves and matured market labels."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from math import sqrt

import numpy as np
import pandas as pd

from .mature_market_forecast import (
    HORIZON_SESSIONS,
    MIN_ENTRY_DAYS,
    MIN_PRODUCTS,
    TRAINING_ENTRY_DAYS,
    _day,
    _numeric,
    _pool,
)

FEATURES = ("curve_level", "curve_change", "pressure")


@dataclass(frozen=True)
class MatureCurveForecast:
    ready: bool
    forecasts: pd.DataFrame
    audit: dict


def forecast_from_mature_curve(
    observations: pd.DataFrame,
    targets: pd.DataFrame,
    *,
    decision_day,
    excluded_products: Iterable[str] = (),
    groups: Mapping[str, str] | None = None,
    grouped: bool = False,
    relative: bool = False,
    contract_role: bool = False,
) -> MatureCurveForecast:
    """Fit shared slopes, optionally with fixed, strongly shrunk group deviations.

    Curve features are bounded, completed evidence supplied by the caller. The
    five-session labels are the same selected contract's observed open returns.
    Relative mode instead predicts deviations from that entry date's group mean,
    and centers features within the same group; groups with one observation cannot
    provide relative information. Group assignments must be specified in advance.
    Exclusions precede numerical reads, centering, date selection and estimation.
    Only labels whose entire maturity session precedes the decision are consumed.

    Contract-role research adds two pooled interactions for whether the predicted
    contract is the near or far leg of its completed curve. It does not assume
    a convergence direction or change the label, allocation or account rules.
    """
    if isinstance(excluded_products, str):
        raise ValueError("exclusions must be a collection of product identities")
    exclusions = tuple(sorted(set(excluded_products)))
    if any(not isinstance(p, str) or not p or p != p.strip().upper() for p in exclusions):
        raise ValueError("exclusions must be canonical product identities")
    if any(type(flag) is not bool for flag in (grouped, relative, contract_role)):
        raise ValueError("model modes must be boolean")
    if relative and contract_role:
        raise ValueError("contract role requires original outright labels")
    day = _day(decision_day)
    renamed = dict(zip(FEATURES, ("x1", "x2", "x3"), strict=True))

    def prepare(frame, training):
        if not set(FEATURES).issubset(frame.columns):
            raise ValueError("curve feature schema is incomplete")
        if {"x1", "x2", "x3"}.intersection(frame.columns):
            raise ValueError("ambiguous curve feature schema")
        checked = _pool(frame.rename(columns=renamed), exclude_ag=False, training=training)
        return checked.loc[~checked["product"].isin(exclusions)].copy()

    sample, target = prepare(observations, True), prepare(targets, False)
    if not target.entry_day.eq(day).all():
        raise ValueError("prediction entry day must equal decision day")
    sample = sample.loc[sample.maturity_day < day].copy()
    group_names: list[str] = []
    if grouped or relative:
        mapping = dict(groups or {})
        if any(not isinstance(g, str) or not g for g in mapping.values()):
            raise ValueError("group identities must be nonempty strings")
        for frame in (sample, target):
            frame["group"] = frame["product"].map(mapping)
            if frame["group"].isna().any():
                raise ValueError("missing predeclared economic group")
        group_names = sorted(set(mapping.values()))
        if relative:
            sample = sample.loc[
                sample.groupby(["entry_day", "group"])["product"].transform("size") >= 2
            ].copy()
            target = target.loc[target.groupby("group")["product"].transform("size") >= 2].copy()
    entry_days = sample.entry_day.drop_duplicates().sort_values().iloc[-TRAINING_ENTRY_DAYS:]
    sample = sample.loc[sample.entry_day.isin(entry_days)].copy()
    x, values = _numeric(sample, training=True)
    target_x, target_values = _numeric(target, training=False)
    counts = sample.groupby("entry_day").size()
    weights = 1.0 / sample.entry_day.map(counts).to_numpy(dtype=float)
    ready = len(counts) >= MIN_ENTRY_DAYS and sample["product"].nunique() >= MIN_PRODUCTS
    audit = {
        "decision_day": day.date().isoformat(),
        "status": "ready" if ready else "insufficient_mature_curve_history",
        "excluded_products": list(exclusions),
        "grouped": grouped,
        "relative": relative,
        "contract_role": contract_role,
        "training_records": len(sample),
        "training_entry_days": len(counts),
        "training_products": int(sample["product"].nunique()),
        "last_maturity_day": sample.maturity_day.max().date().isoformat() if len(sample) else None,
        "sample_weight_sum": float(weights.sum()),
        "shared_ridge_penalty": 1.0,
        "group_deviation_penalty": 20.0 if grouped else None,
        "coefficients": None,
    }
    forecast = target[["entry_day", "product", "symbol"]].copy()
    if not ready:
        forecast = forecast.iloc[:0]
        forecast["expected_return"] = pd.Series(dtype=float)
        return MatureCurveForecast(False, forecast, audit)
    gross = pd.Series(values[:, 1], index=sample.index)
    if relative:
        gross = gross - gross.groupby([sample.entry_day, sample.group]).transform("mean")
        for frame, matrix in ((sample, x), (target, target_x)):
            centers = frame.groupby(["entry_day", "group"])[["x1", "x2", "x3"]].transform("mean")
            matrix -= centers.to_numpy(dtype=float)
    y = gross.to_numpy() / (values[:, 0] * sqrt(HORIZON_SESSIONS))
    if not np.isfinite(y).all():
        raise ValueError("normalized mature curve label is non-finite")

    def design(matrix, frame):
        parts = [matrix]
        if grouped:
            parts.extend(
                matrix * frame.group.eq(group).to_numpy()[:, None] for group in group_names
            )
        if contract_role:
            required = {"symbol_near", "symbol_far", "feature_through", "pair_source_day"}
            if not required.issubset(frame):
                raise ValueError("contract role requires completed exact pair identities")
            source = pd.to_datetime(frame.pair_source_day.map(_day))
            through = pd.to_datetime(frame.feature_through.map(_day))
            near, far = frame.symbol.eq(frame.symbol_near), frame.symbol.eq(frame.symbol_far)
            if (
                not (near ^ far).all()
                or not source.eq(through).all()
                or not through.lt(frame.entry_day).all()
            ):
                raise ValueError("contract role pair is ambiguous or not strictly prior")
            role = np.where(near, 1.0, -1.0)
            parts.append(matrix[:, :2] * role[:, None])
        return np.column_stack(parts) if len(parts) > 1 else matrix

    design_x, design_target = design(x, sample), design(target_x, target)
    penalty = np.r_[np.ones(3), np.full(3 * len(group_names), 20.0)] if grouped else np.ones(3)
    if contract_role:
        penalty = np.r_[penalty, np.full(2, 20.0)]
    beta = np.linalg.solve(
        design_x.T @ (design_x * weights[:, None]) + np.diag(penalty),
        design_x.T @ (weights * y),
    )
    predicted = design_target @ beta * target_values[:, 0] * sqrt(HORIZON_SESSIONS)
    if not np.isfinite(beta).all() or not np.isfinite(predicted).all():
        raise ValueError("curve forecast is non-finite")
    audit["coefficients"] = {
        "shared": dict(zip(FEATURES, beta[:3].tolist(), strict=True)),
        "group_deviations": {
            group: dict(zip(FEATURES, beta[3 + 3 * i : 6 + 3 * i].tolist(), strict=True))
            for i, group in enumerate(group_names)
        }
        if grouped
        else {},
    }
    if contract_role:
        audit["contract_role_ridge_penalty"] = 20.0
        audit["coefficients"]["contract_role_interactions"] = dict(
            zip(FEATURES[:2], beta[-2:].tolist(), strict=True)
        )
    forecast["expected_return"] = predicted
    return MatureCurveForecast(True, forecast.reset_index(drop=True), audit)
