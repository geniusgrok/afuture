"""Causal research experts that correct, or replace, the grouped curve model.

Residuals use predictions actually issued before the label's entry, with the
parent's recorded training cutoff and exact contract identity. The caller must
bind these columns to its immutable forecast/audit archive; this module never
refits a parent on the residual training set or fills missing parent forecasts.
These routines generate forecasts only, without allocating or owning accounts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from math import sqrt

import numpy as np
import pandas as pd

from .curve_market_forecast import FEATURES, MatureCurveForecast
from .curve_rebuild import (
    ANNUAL_PERIOD_DAYS,
    GROUP_RIDGE,
    SEASONAL_FEATURES,
    SEASONAL_RIDGE,
    _completed_features,
    _seasonal_design,
)
from .mature_market_forecast import (
    HORIZON_SESSIONS,
    MIN_ENTRY_DAYS,
    MIN_PRODUCTS,
    TRAINING_ENTRY_DAYS,
    _day,
    _numeric,
    _pool,
)

MECHANISMS = (
    "seasonal_residual",
    "residual_only",
    "bounded_nonlinear",
    "execution_net_core",
)
NONLINEAR_RIDGE = 20.0
NORMALIZED_TARGET_LIMIT = 3.0
HINGE_LOCATION = 0.5
STRESS_ONE_WAY_FEE = 0.0015
ROUND_TRIP_SCORE_COST = 2 * STRESS_ONE_WAY_FEE
MIN_GROUP_ENTRY_DAYS = 60
NONLINEAR_FEATURES = (
    *(f"{name}_outer_hinge" for name in FEATURES),
    "curve_level_curve_change",
    "curve_level_pressure",
    "curve_change_pressure",
)
PARENT_COLUMNS = (
    "parent_expected_return",
    "parent_training_through",
    "parent_decision_day",
    "parent_product",
    "parent_symbol",
)


def _exclude(frame: pd.DataFrame, exclusions: tuple[str, ...]) -> pd.DataFrame:
    if frame.columns.has_duplicates or "product" not in frame:
        raise ValueError("curve feature schema is incomplete or duplicated")
    identities = frame["product"].map(
        lambda value: value.strip().upper() if isinstance(value, str) else value
    )
    return frame.loc[~identities.isin(exclusions)].copy()


def _prepare(frame: pd.DataFrame, *, training: bool) -> pd.DataFrame:
    if not set(FEATURES).issubset(frame) or {"x1", "x2", "x3"}.intersection(frame):
        raise ValueError("curve feature schema is incomplete or ambiguous")
    renamed = dict(zip(FEATURES, ("x1", "x2", "x3"), strict=True))
    return _pool(frame.rename(columns=renamed), exclude_ag=False, training=training)


def _parent_rows(frame: pd.DataFrame, *, required: bool) -> pd.DataFrame:
    if not set(PARENT_COLUMNS).issubset(frame):
        raise ValueError("archived out-of-fold parent provenance is incomplete")
    available = frame.parent_expected_return.notna()
    if required and not available.all():
        raise ValueError("target requires its actual archived parent forecast")
    result = frame.loc[available].copy()
    for identity in ("product", "symbol"):
        if not result[f"parent_{identity}"].eq(result[identity]).all():
            raise ValueError("archived parent forecast identity does not match observation")
    for field in ("parent_training_through", "parent_decision_day"):
        result[field] = pd.to_datetime(result[field].map(_day))
    if not result.parent_decision_day.eq(result.entry_day).all():
        raise ValueError("archived parent decision must equal observation entry day")
    if not result.parent_training_through.lt(result.entry_day).all():
        raise ValueError("parent training must strictly precede its forecast entry")
    if any(isinstance(value, (bool, np.bool_)) for value in result.parent_expected_return):
        raise ValueError("parent forecast must be finite numeric evidence")
    try:
        values = result.parent_expected_return.to_numpy(dtype=float)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("parent forecast must be finite numeric evidence") from error
    if not np.isfinite(values).all():
        raise ValueError("parent forecast must be finite numeric evidence")
    result["parent_expected_return"] = values
    return result


def _nonlinear_design(matrix: np.ndarray) -> np.ndarray:
    hinges = np.sign(matrix) * np.maximum(np.abs(matrix) - HINGE_LOCATION, 0.0) * 2
    interactions = np.column_stack(
        (matrix[:, 0] * matrix[:, 1], matrix[:, 0] * matrix[:, 2], matrix[:, 1] * matrix[:, 2])
    )
    return np.column_stack((matrix, hinges, interactions))


def forecast_curve_expert(
    observations: pd.DataFrame,
    targets: pd.DataFrame,
    *,
    decision_day,
    mechanism: str = "seasonal_residual",
    excluded_products: Iterable[str] = (),
    groups: Mapping[str, str] | None = None,
    grouped: bool = True,
) -> MatureCurveForecast:
    """Fit one fixed expert using only fully mature, previously available evidence.

    ``seasonal_residual`` adds four pooled annual curve interactions to the
    target's actual T2 prediction. ``residual_only`` emits just this correction.
    Both regress normalized actual returns minus genuine chronological parent
    forecasts, never in-sample errors. Parent provenance columns are mandatory
    on observations, and also on additive targets. Missing observation forecasts
    are omitted. A caller may explicitly retain the parent while this correction
    is unready, but must not count that fallback as a ready residual expert.

    ``bounded_nonlinear`` independently fits three base features, three fixed
    outer hinges and three pairwise interactions; normalized labels are clipped
    at +/-3. Its group deviations affect base slopes only. No adaptive threshold,
    selected winner, account profits or hyperparameter grid enter these models.

    ``execution_net_core`` adds an intercept to the same fixed basis and fits
    separate long/short five-session net market-return proxies, charging15bp at
    both actual label endpoints. These are hypothetical fixed-notional labels,
    not account fill returns. Its expected_return encodes the better positive
    net prediction plus the existing0.003 score hurdle, so a downstream score
    subtracting0.003 consumes the net edge once. Both net predictions are exposed.

    All routes require126 mature eligible entry dates and20 products, use the
    latest252 eligible dates, and give each date total weight one. Exclusions
    precede all numerical reads and training. The default pool restores SA/FG;
    exAG callers must pass excluded_products=("AG",) before every training stage.
    """
    if mechanism not in MECHANISMS:
        raise ValueError("unknown predeclared curve expert mechanism")
    if isinstance(excluded_products, str):
        raise ValueError("exclusions must be a collection of product identities")
    exclusions = tuple(excluded_products)
    if any(not isinstance(p, str) or not p or p != p.strip().upper() for p in exclusions):
        raise ValueError("exclusions must be canonical product identities")
    exclusions = tuple(sorted(set(exclusions)))
    if type(grouped) is not bool:
        raise ValueError("grouped must be boolean")
    day = _day(decision_day)
    sample = _prepare(_exclude(observations, exclusions), training=True)
    target = _prepare(_exclude(targets, exclusions), training=False)
    if not target.entry_day.eq(day).all():
        raise ValueError("prediction entry day must equal decision day")
    sample = sample.loc[sample.maturity_day < day].copy()
    residual = mechanism in ("seasonal_residual", "residual_only")
    execution_net = mechanism == "execution_net_core"
    omitted_parent_rows = 0
    if residual:
        original_rows = len(sample)
        sample = _parent_rows(sample, required=False)
        omitted_parent_rows = original_rows - len(sample)
        if mechanism == "seasonal_residual":
            target = _parent_rows(target, required=True)
    entry_days = sample.entry_day.drop_duplicates().sort_values().iloc[-TRAINING_ENTRY_DAYS:]
    sample = sample.loc[sample.entry_day.isin(entry_days)].copy()
    for frame in (sample, target):
        _completed_features(frame)
    x, values = _numeric(sample, training=True)
    target_x, target_values = _numeric(target, training=False)
    counts = sample.groupby("entry_day").size()
    weights = 1.0 / sample.entry_day.map(counts).to_numpy(dtype=float)
    ready = len(counts) >= MIN_ENTRY_DAYS and sample["product"].nunique() >= MIN_PRODUCTS
    names: list[str]
    group_names: list[str] = []
    if residual:
        names = list(SEASONAL_FEATURES)
        design_x = _seasonal_design(x, sample.entry_day)[:, 3:]
        design_target = _seasonal_design(target_x, target.entry_day)[:, 3:]
        penalty = np.full(len(names), SEASONAL_RIDGE)
    else:
        names = [*FEATURES, *NONLINEAR_FEATURES]
        design_x, design_target = _nonlinear_design(x), _nonlinear_design(target_x)
        penalty = np.r_[np.ones(3), np.full(6, NONLINEAR_RIDGE)]
        if execution_net:
            names = ["intercept", *names]
            design_x = np.column_stack((np.ones(len(x)), design_x))
            design_target = np.column_stack((np.ones(len(target_x)), design_target))
            penalty = np.r_[1.0, penalty]
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
    audit = {
        "decision_day": day.date().isoformat(),
        "mechanism": mechanism,
        "status": "ready" if ready else "insufficient_mature_expert_history",
        "excluded_products": list(exclusions),
        "grouped_core": bool(grouped and not residual),
        "training_records": len(sample),
        "training_entry_days": len(counts),
        "training_products": int(sample["product"].nunique()),
        "first_entry_day": entry_days.iloc[0].date().isoformat() if len(entry_days) else None,
        "last_entry_day": entry_days.iloc[-1].date().isoformat() if len(entry_days) else None,
        "last_maturity_day": sample.maturity_day.max().date().isoformat() if len(sample) else None,
        "residual_last_maturity_day": sample.maturity_day.max().date().isoformat()
        if residual and len(sample)
        else None,
        "target_parent_training_through": target.parent_training_through.max().date().isoformat()
        if mechanism == "seasonal_residual" and len(target)
        else None,
        "sample_weight_sum": float(weights.sum()),
        "parent_predictions": "archived_chronological_out_of_fold" if residual else None,
        "omitted_missing_parent_records": omitted_parent_rows,
        "annual_period_days": ANNUAL_PERIOD_DAYS if residual else None,
        "seasonal_ridge_penalty": SEASONAL_RIDGE if residual else None,
        "nonlinear_ridge_penalty": NONLINEAR_RIDGE if not residual else None,
        "normalized_target_limit": NORMALIZED_TARGET_LIMIT if not residual else None,
        "hinge_location": HINGE_LOCATION if not residual else None,
        "group_deviation_penalty": GROUP_RIDGE if group_names else None,
        "market_net_proxy_one_way_fee": STRESS_ONE_WAY_FEE if execution_net else None,
        "expected_return_semantics": "signed_net_edge_plus_score_hurdle"
        if execution_net
        else "five_session_gross_return",
        "coefficients": None,
    }
    forecast = target[["entry_day", "product", "symbol"]].copy()
    if not ready:
        forecast = forecast.iloc[:0]
        forecast["expected_return"] = pd.Series(dtype=float)
        return MatureCurveForecast(False, forecast, audit)
    if mechanism == "seasonal_residual" and len(target):
        audit["last_maturity_day"] = (
            max(sample.maturity_day.max(), target.parent_training_through.max()).date().isoformat()
        )
    gross = values[:, 1]
    if residual:
        gross = gross - sample.parent_expected_return.to_numpy(dtype=float)
    scale = values[:, 0] * sqrt(HORIZON_SESSIONS)
    if execution_net:
        cost = STRESS_ONE_WAY_FEE * (2 + gross)
        y = np.column_stack((gross - cost, -gross - cost)) / scale[:, None]
    else:
        y = gross / scale
    if not np.isfinite(y).all():
        raise ValueError("normalized expert label is non-finite")
    if not residual:
        y = np.clip(y, -NORMALIZED_TARGET_LIMIT, NORMALIZED_TARGET_LIMIT)
    weighted_y = weights[:, None] * y if execution_net else weights * y
    beta = np.linalg.solve(
        design_x.T @ (design_x * weights[:, None]) + np.diag(penalty),
        design_x.T @ weighted_y,
    )
    target_scale = target_values[:, 0] * sqrt(HORIZON_SESSIONS)
    if execution_net:
        net = design_target @ beta * target_scale[:, None]
        long_wins = net[:, 0] >= net[:, 1]
        edge = np.maximum(net[:, 0], net[:, 1])
        expected = np.where(
            edge > 0, np.where(long_wins, 1.0, -1.0) * (edge + ROUND_TRIP_SCORE_COST), 0.0
        )
        forecast["expected_net_long"] = net[:, 0]
        forecast["expected_net_short"] = net[:, 1]
    else:
        expected = design_target @ beta * target_scale
    if mechanism == "seasonal_residual":
        expected += target.parent_expected_return.to_numpy(dtype=float)
    if not np.isfinite(beta).all() or not np.isfinite(expected).all():
        raise ValueError("expert forecast is non-finite")

    def coefficients(vector):
        return {
            "shared": dict(zip(names, vector[: len(names)].tolist(), strict=True)),
            "group_deviations": {
                group: dict(
                    zip(
                        FEATURES,
                        vector[len(names) + 3 * i : len(names) + 3 * (i + 1)].tolist(),
                        strict=True,
                    )
                )
                for i, group in enumerate(group_names)
            },
        }

    audit["coefficients"] = (
        {"long": coefficients(beta[:, 0]), "short": coefficients(beta[:, 1])}
        if execution_net
        else coefficients(beta)
    )
    forecast["expected_return"] = expected
    return MatureCurveForecast(True, forecast.reset_index(drop=True), audit)


def _expert_archive(frame: pd.DataFrame, day, exclusions) -> pd.DataFrame:
    required = {
        "entry_day",
        "decision_day",
        "product",
        "symbol",
        "expected_return",
        "volatility",
        "training_through",
    }
    frame = _exclude(frame, exclusions)
    if not required.issubset(frame):
        raise ValueError("expert archive provenance is incomplete")
    for field in ("product", "symbol"):
        if (
            not frame[field]
            .map(
                lambda value: (
                    isinstance(value, str) and bool(value) and value == value.strip().upper()
                )
            )
            .all()
        ):
            raise ValueError("expert concrete contract identity is invalid")
    frame["entry_day"] = pd.to_datetime(frame.entry_day.map(_day))
    frame = frame.loc[frame.entry_day <= day].copy()
    if frame.duplicated(["entry_day", "product"]).any():
        raise ValueError("duplicate archived expert forecast")
    for field in ("decision_day", "training_through"):
        frame[field] = pd.to_datetime(frame[field].map(_day))
    if not frame.decision_day.eq(frame.entry_day).all():
        raise ValueError("expert decision must equal its forecast entry day")
    if not frame.training_through.lt(frame.entry_day).all():
        raise ValueError("expert training must strictly precede its forecast entry")
    symbol_products = frame.symbol.str.extract(r"^([A-Z]+)[0-9]+$", expand=False)
    if symbol_products.isna().any() or not symbol_products.eq(frame["product"]).all():
        raise ValueError("expert concrete contract identity is invalid")
    columns = ["expected_return", "volatility"]
    if any(isinstance(value, (bool, np.bool_)) for value in frame[columns].to_numpy().flat):
        raise ValueError("expert forecast values must be finite numeric evidence")
    try:
        values = frame[columns].to_numpy(dtype=float)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("expert forecast values must be finite numeric evidence") from error
    if not np.isfinite(values).all() or (values[:, 1] <= 0).any():
        raise ValueError("expert forecast values must be finite numeric evidence")
    frame[columns] = values
    return frame


def choose_mature_expert(
    experts: Mapping[str, pd.DataFrame],
    observations: pd.DataFrame,
    *,
    decision_day,
    groups: Mapping[str, str],
    excluded_products: Iterable[str] = (),
) -> MatureCurveForecast:
    """Choose group experts from common matured OOF net market-return proxies.

    A past forecast trades hypothetically only when abs(mu)>0.003. Its signed
    five-session actual open return is charged15bp at both label endpoints.
    Average products within date/group, then average dates equally. Only a
    strictly positive utility can replace C_T2, with lexical expert-name ties.
    This proxy omits integer sizing, funding, margin and interacting positions;
    the caller must evaluate the resulting forecasts in one actual account.

    Readiness requires126 distinct common mature entry dates and20 products,
    selected from the latest252 common dates. A group additionally needs60 such
    dates. Unready results are empty; the caller may retain the exact parent.
    Once globally ready, unqualified groups/current predictions retain C_T2 and
    are explicitly audited. All expert archives need actual training cutoffs.
    """
    if "C_T2" not in experts or len(experts) < 2:
        raise ValueError("selector needs C_T2 and at least one alternative")
    if any(not isinstance(name, str) or not name for name in experts):
        raise ValueError("expert names must be nonempty strings")
    if isinstance(excluded_products, str):
        raise ValueError("exclusions must be a collection of product identities")
    exclusions = tuple(excluded_products)
    if any(not isinstance(p, str) or not p or p != p.strip().upper() for p in exclusions):
        raise ValueError("exclusions must be canonical product identities")
    exclusions = tuple(sorted(set(exclusions)))
    day = _day(decision_day)
    sample = _prepare(_exclude(observations, exclusions), training=True)
    mapping = dict(groups)
    if any(not isinstance(g, str) or not g for g in mapping.values()):
        raise ValueError("group identities must be nonempty strings")
    sample = sample.loc[sample.maturity_day < day].copy()
    archives = {name: _expert_archive(frame, day, exclusions) for name, frame in experts.items()}
    keys = ["entry_day", "product"]
    common = sample.copy()
    for i, name in enumerate(sorted(archives)):
        archive = archives[name]
        prior = archive.loc[
            archive.entry_day < day, [*keys, "symbol", "volatility", "expected_return"]
        ]
        merged = common.merge(
            prior, on=keys, how="inner", suffixes=("", "_expert"), validate="one_to_one"
        )
        if not merged.symbol.eq(merged.symbol_expert).all():
            raise ValueError("expert archive contract does not match actual market label")
        if not np.array_equal(
            merged.volatility.to_numpy(dtype=float), merged.volatility_expert.to_numpy(dtype=float)
        ):
            raise ValueError("expert archive volatility does not match entry evidence")
        common = merged.drop(columns=["symbol_expert", "volatility_expert"]).rename(
            columns={"expected_return": f"mu_{i}"}
        )
    entry_days = common.entry_day.drop_duplicates().sort_values().iloc[-TRAINING_ENTRY_DAYS:]
    common = common.loc[common.entry_day.isin(entry_days)].copy()
    _completed_features(common)
    _, values = _numeric(common, training=True)
    common["group"] = common["product"].map(mapping)
    parent = archives["C_T2"].loc[archives["C_T2"].entry_day.eq(day)].copy()
    parent["group"] = parent["product"].map(mapping)
    if common.group.isna().any() or parent.group.isna().any():
        raise ValueError("missing predeclared economic group")
    ready = len(entry_days) >= MIN_ENTRY_DAYS and common["product"].nunique() >= MIN_PRODUCTS
    audit = {
        "decision_day": day.date().isoformat(),
        "mechanism": "causal_group_net_utility_selector",
        "status": "ready" if ready else "insufficient_common_mature_expert_history",
        "excluded_products": list(exclusions),
        "training_records": len(common),
        "training_entry_days": len(entry_days),
        "training_products": int(common["product"].nunique()),
        "last_maturity_day": common.maturity_day.max().date().isoformat() if len(common) else None,
        "selection_last_maturity_day": common.maturity_day.max().date().isoformat()
        if len(common)
        else None,
        "min_group_entry_days": MIN_GROUP_ENTRY_DAYS,
        "one_way_market_proxy_fee": STRESS_ONE_WAY_FEE,
        "utility_contract": "equal_date_group_signed_gross_minus_endpoint_fees_no_signal_zero",
        "parent_fallback": "C_T2",
        "groups": {},
        "missing_current_forecast_fallback_products": [],
    }
    forecast = parent[["entry_day", "product", "symbol", "expected_return"]].copy()
    if not ready:
        return MatureCurveForecast(False, forecast.iloc[:0], audit)
    used_cutoffs = [common.maturity_day.max()]
    if len(parent):
        used_cutoffs.append(parent.training_through.max())
    gross = values[:, 1]
    for i, _ in enumerate(sorted(archives)):
        mu = common[f"mu_{i}"].to_numpy(dtype=float)
        common[f"utility_{i}"] = np.where(
            np.abs(mu) > ROUND_TRIP_SCORE_COST,
            np.sign(mu) * gross - STRESS_ONE_WAY_FEE * (2 + gross),
            0.0,
        )
    for group in sorted(parent.group.unique()):
        rows = common.loc[common.group.eq(group)]
        dates = rows.entry_day.nunique()
        utilities = {
            name: float(rows.groupby("entry_day")[f"utility_{i}"].mean().mean()) if dates else None
            for i, name in enumerate(sorted(archives))
        }
        positive = [
            name
            for name, utility in utilities.items()
            if utility is not None and np.isfinite(utility) and utility > 0
        ]
        chosen = (
            min(positive, key=lambda name: (-(utilities[name] or 0.0), name))
            if positive and dates >= MIN_GROUP_ENTRY_DAYS
            else "C_T2"
        )
        audit["groups"][group] = {
            "training_entry_days": int(dates),
            "utilities": utilities,
            "chosen": chosen,
            "fallback": not (positive and dates >= MIN_GROUP_ENTRY_DAYS),
        }
        products = parent.loc[parent.group.eq(group), "product"]
        current = archives[chosen].loc[archives[chosen].entry_day.eq(day)].set_index("product")
        for product in products:
            index = forecast.index[forecast["product"].eq(product)][0]
            if product not in current.index:
                audit["missing_current_forecast_fallback_products"].append(product)
                continue
            row = current.loc[product]
            if row.symbol != forecast.at[index, "symbol"]:
                raise ValueError("selected current expert contract differs from parent")
            if row.volatility != parent.loc[parent["product"].eq(product), "volatility"].iloc[0]:
                raise ValueError("selected current expert volatility differs from parent")
            forecast.at[index, "expected_return"] = float(row.expected_return)
            used_cutoffs.append(row.training_through)
    audit["last_maturity_day"] = max(used_cutoffs).date().isoformat()
    return MatureCurveForecast(True, forecast.reset_index(drop=True), audit)
