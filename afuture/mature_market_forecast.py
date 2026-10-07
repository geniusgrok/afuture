"""Pure, position-independent forecasts from already matured market observations.

Callers must select each concrete contract and capture its features and complete
20-session volatility before ``entry_day``. ``gross_return`` is the observed return
from that contract's entry open to its open five trading sessions later; it is not
an account fill or profit. The caller owns that five-session calendar/provenance
check. This module accepts a label only after its entire maturity day is complete.
No account holdings, template selection or live execution enter the estimator.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import isfinite, sqrt, tanh

import numpy as np
import pandas as pd

FEATURES = ("x1", "x2", "x3")
TRAINING_ENTRY_DAYS = 252
MIN_ENTRY_DAYS = 126
MIN_PRODUCTS = 20
HORIZON_SESSIONS = 5


def completed_pressure_features(
    *,
    open_price: float,
    high: float,
    low: float,
    close: float,
    previous_close: float,
    open_interest: float,
    previous_open_interest: float,
    volume: float,
) -> dict[str, float]:
    """Build three bounded features from one completed concrete-contract bar.

    Both previous values must come from the immediately preceding complete session
    of the same contract. Missing evidence raises instead of becoming neutral data.
    A valid motionless price bar has zero price-pressure features.
    """
    values = (
        open_price,
        high,
        low,
        close,
        previous_close,
        open_interest,
        previous_open_interest,
        volume,
    )
    try:
        if any(isinstance(value, (bool, np.bool_)) for value in values) or not all(
            isfinite(float(value)) for value in values
        ):
            raise ValueError("completed bar requires finite numeric evidence")
        opening, highest, lowest, closing, prior_close, oi, prior_oi, traded = map(float, values)
    except (TypeError, OverflowError) as error:
        raise ValueError("completed bar requires finite numeric evidence") from error
    if min(opening, highest, lowest, closing, prior_close) <= 0:
        raise ValueError("completed bar prices must be positive")
    if not lowest <= min(opening, closing) <= max(opening, closing) <= highest:
        raise ValueError("completed bar has inconsistent OHLC")
    if min(oi, prior_oi) < 0 or traded <= 0:
        raise ValueError("completed bar requires nonnegative OI and positive volume")
    price_range = highest - lowest
    location = ((closing - lowest) - (highest - closing)) / price_range if price_range else 0.0
    intraday, gap = closing - opening, opening - prior_close
    movement = abs(intraday) + abs(gap)
    contrast = (intraday - gap) / movement if movement else 0.0
    participation = location * tanh((oi - prior_oi) / traded)
    if not all(isfinite(value) for value in (location, contrast, participation, movement)):
        raise ValueError("derived completed-bar features must be finite")
    return dict(zip(FEATURES, (location, contrast, participation), strict=True))


def _day(value) -> pd.Timestamp:
    if not isinstance(value, (str, date)):
        raise ValueError("session days must be calendar dates")
    try:
        # A string is a date, not a numeric epoch or an intraday timestamp.
        parsed = pd.Timestamp(date.fromisoformat(value) if isinstance(value, str) else value)
    except (ValueError, TypeError, OverflowError) as error:
        raise ValueError("invalid session day") from error
    if pd.isna(parsed) or parsed.tzinfo is not None or parsed != parsed.normalize():
        raise ValueError("session days must be finite timezone-naive calendar dates")
    return parsed


def _pool(frame: pd.DataFrame, *, exclude_ag: bool, training: bool) -> pd.DataFrame:
    required = {"entry_day", "product", "symbol", "volatility", *FEATURES}
    if training:
        required |= {"maturity_day", "gross_return"}
    if frame.columns.has_duplicates or not required.issubset(frame.columns):
        raise ValueError("market observation schema is incomplete or duplicated")
    result = frame.copy()
    for column in ("product", "symbol"):
        if (
            not result[column]
            .map(lambda value: isinstance(value, str) and bool(value.strip()))
            .all()
        ):
            raise ValueError("market observation identity must be a nonempty string")
        result[column] = result[column].str.strip().str.upper()
    if exclude_ag:
        result = result.loc[result["product"] != "AG"].copy()
    symbol_products = result["symbol"].str.extract(r"^([A-Z]+)[0-9]+$", expand=False)
    if symbol_products.isna().any() or symbol_products.ne(result["product"]).any():
        raise ValueError("concrete contract symbol does not match product identity")

    def session_dates(values, *, missing=False):
        # Repeated rolling fits receive already parsed dates. Validate these in a
        # batch instead of constructing a Timestamp for every row on every fit.
        if pd.api.types.is_datetime64_any_dtype(values.dtype):
            if (
                values.dt.tz is not None
                or not values.dropna().eq(values.dropna().dt.normalize()).all()
            ):
                raise ValueError("session days must be timezone-naive calendar dates")
            if not missing and values.isna().any():
                raise ValueError("session days must be finite calendar dates")
            return values.copy()
        return pd.to_datetime(
            values.map(lambda value: pd.NaT if missing and pd.isna(value) else _day(value))
        )

    result["entry_day"] = session_dates(result["entry_day"])
    if training:
        # A still-unobserved maturity is not permission to consume its label.
        result["maturity_day"] = session_dates(result["maturity_day"], missing=True)
    if result.duplicated(["entry_day", "product"]).any():
        raise ValueError("more than one selected contract per entry day and product")
    if training and (result["maturity_day"] <= result["entry_day"]).any():
        raise ValueError("maturity day must follow entry day")
    return result.sort_values(["entry_day", "product", "symbol"]).reset_index(drop=True)


def _numeric(frame: pd.DataFrame, *, training: bool) -> tuple[np.ndarray, np.ndarray]:
    columns = [*FEATURES, "volatility", *(["gross_return"] if training else [])]
    try:
        if any(isinstance(value, (bool, np.bool_)) for value in frame[columns].to_numpy().flat):
            raise ValueError("market values cannot be boolean")
        values = frame[columns].to_numpy(dtype=float)
    except (TypeError, OverflowError) as error:
        raise ValueError("market values must be numeric") from error
    if not np.isfinite(values).all():
        raise ValueError("required market evidence is missing or non-finite")
    if (np.abs(values[:, :3]) > 1.0 + 1e-12).any():
        raise ValueError("pressure features must be in [-1, 1]")
    if (values[:, 3] <= 0).any():
        raise ValueError("complete prior volatility must be positive")
    if training and (values[:, 4] <= -1).any():
        raise ValueError("positive-price gross return must exceed -100%")
    return values[:, :3], values[:, 3:]


@dataclass(frozen=True)
class MatureMarketForecast:
    ready: bool
    forecasts: pd.DataFrame
    audit: dict


def forecast_from_mature_market(
    observations: pd.DataFrame,
    targets: pd.DataFrame,
    *,
    decision_day: date | str,
    exclude_ag: bool = False,
) -> MatureMarketForecast:
    """Fit three shared ridge coefficients and return signed five-day returns.

    Training columns: entry_day, maturity_day, product, symbol, x1/x2/x3,
    volatility, gross_return. Targets omit maturity_day/gross_return and must
    have entry_day == decision_day. Volatility is the positive, complete,
    entry-time 20-session daily standard deviation supplied by the caller.

    AG is removed before sample selection, label normalization or fitting.
    Only matured labels from the latest 252 entry dates are read numerically;
    a NaT maturity denotes an as-yet unavailable label and is excluded.
    Each entry date carries total sample weight one. Ridge has no intercept and
    fixed penalty one; there is no parameter selection. Insufficient history
    returns ready=False and no forecasts, never fabricated zero observations.
    """
    if not isinstance(exclude_ag, bool):
        raise ValueError("exclude_ag must be boolean")
    day = _day(decision_day)
    sample = _pool(observations, exclude_ag=exclude_ag, training=True)
    target = _pool(targets, exclude_ag=exclude_ag, training=False)
    if not target["entry_day"].eq(day).all():
        raise ValueError("prediction entry day must equal decision day")
    sample = sample.loc[sample["maturity_day"] < day].copy()
    entry_days = sample["entry_day"].drop_duplicates().sort_values().iloc[-TRAINING_ENTRY_DAYS:]
    sample = sample.loc[sample["entry_day"].isin(entry_days)].copy()
    x, training_values = _numeric(sample, training=True)
    target_x, target_values = _numeric(target, training=False)
    counts = sample.groupby("entry_day", sort=True).size()
    weights = 1.0 / sample["entry_day"].map(counts).to_numpy(dtype=float)
    ready = len(counts) >= MIN_ENTRY_DAYS and sample["product"].nunique() >= MIN_PRODUCTS
    audit = {
        "decision_day": day.date().isoformat(),
        "pool": "exAG" if exclude_ag else "full",
        "status": "ready" if ready else "insufficient_mature_market_history",
        "training_records": len(sample),
        "training_entry_days": len(counts),
        "training_products": int(sample["product"].nunique()),
        "first_entry_day": entry_days.iloc[0].date().isoformat() if len(entry_days) else None,
        "last_entry_day": entry_days.iloc[-1].date().isoformat() if len(entry_days) else None,
        "last_maturity_day": sample["maturity_day"].max().date().isoformat()
        if len(sample)
        else None,
        "entry_day_sample_counts": {
            key.date().isoformat(): int(value) for key, value in counts.items()
        },
        "sample_weight_sum": float(weights.sum()),
        "horizon_sessions": HORIZON_SESSIONS,
        "ridge_penalty": 1.0,
        "coefficients": None,
    }
    forecast = target[["entry_day", "product", "symbol"]].copy()
    if not ready:
        forecast = forecast.iloc[:0]
        forecast["expected_return"] = pd.Series(dtype=float)
        return MatureMarketForecast(False, forecast, audit)
    normalized_return = training_values[:, 1] / (training_values[:, 0] * sqrt(HORIZON_SESSIONS))
    if not np.isfinite(normalized_return).all():
        raise ValueError("normalized matured return is non-finite")
    coefficients = np.linalg.solve(
        x.T @ (x * weights[:, None]) + np.eye(3), x.T @ (weights * normalized_return)
    )
    expected = (target_x @ coefficients) * target_values[:, 0] * sqrt(HORIZON_SESSIONS)
    if not np.isfinite(coefficients).all() or not np.isfinite(expected).all():
        raise ValueError("market forecast is non-finite")
    audit["coefficients"] = dict(zip(FEATURES, map(float, coefficients), strict=True))
    forecast["expected_return"] = expected
    return MatureMarketForecast(True, forecast.reset_index(drop=True), audit)
