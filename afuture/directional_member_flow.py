"""Point-in-time exchange member-position flow for Stress-80 research.

The input is an exchange-published end-of-day member ranking payload.  The output is a
product-level normalized net position *change* pressure, intended for D-complete -> D+1
research only.  No thresholds, rankings, or lookbacks are encoded here.
"""
from __future__ import annotations

from math import isfinite
from typing import Mapping, Sequence

import pandas as pd

MAX_GROSS_LEVERAGE = 2.0
_EPS = 1e-15
_REQUIRED_COLUMNS = (
    "rank",
    "variety",
    "long_open_interest",
    "long_open_interest_chg",
    "short_open_interest",
    "short_open_interest_chg",
)


def _clean_rank_frame(frame: pd.DataFrame) -> pd.DataFrame:
    missing = set(_REQUIRED_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f"member rank frame missing columns: {sorted(missing)}")
    result = frame.copy()
    result["variety"] = result["variety"].astype(str).str.upper()
    for column in (
        "rank",
        "long_open_interest",
        "long_open_interest_chg",
        "short_open_interest",
        "short_open_interest_chg",
    ):
        result[column] = pd.to_numeric(
            result[column].astype(str).str.replace(",", "", regex=False),
            errors="coerce",
        )
    result = result[result["rank"].between(1, 20, inclusive="both")]
    return result


def _pressure(frame: pd.DataFrame, *, product: str) -> float | None:
    clean = _clean_rank_frame(frame)
    clean = clean[clean["variety"] == str(product).upper()]
    if clean.empty:
        return None
    columns = (
        "long_open_interest",
        "long_open_interest_chg",
        "short_open_interest",
        "short_open_interest_chg",
    )
    clean = clean.dropna(subset=list(columns))
    if clean.empty:
        return None
    long_oi = float(clean["long_open_interest"].sum())
    short_oi = float(clean["short_open_interest"].sum())
    denominator = long_oi + short_oi
    if not isfinite(denominator) or denominator <= 0.0:
        return None
    numerator = float(clean["long_open_interest_chg"].sum()) - float(
        clean["short_open_interest_chg"].sum()
    )
    value = numerator / denominator
    return float(value) if isfinite(value) else None


def aggregate_member_flow(
    payload: Mapping[str, pd.DataFrame],
    *,
    exchange: str,
    products: Sequence[str],
) -> dict[str, float]:
    """Normalize exchange-specific Top20 payloads to product member-flow pressure."""
    exchange = str(exchange).upper()
    if exchange not in {"CZCE", "SHFE"}:
        raise ValueError("member-flow research currently supports CZCE and SHFE only")
    requested = tuple(sorted({str(item).upper() for item in products}))
    normalized_payload = {str(key).upper(): value for key, value in payload.items()}
    result: dict[str, float] = {}

    for product in requested:
        if exchange == "CZCE" and product in normalized_payload:
            # CZCE publishes a product aggregate table as well as contract tables.
            # Prefer the aggregate to avoid double counting the same ranked interest.
            value = _pressure(normalized_payload[product], product=product)
        else:
            frames: list[pd.DataFrame] = []
            for frame in normalized_payload.values():
                if not isinstance(frame, pd.DataFrame) or frame.empty:
                    continue
                try:
                    clean = _clean_rank_frame(frame)
                except ValueError:
                    continue
                subset = clean[clean["variety"] == product]
                if not subset.empty:
                    frames.append(subset)
            value = (
                _pressure(pd.concat(frames, ignore_index=True), product=product)
                if frames
                else None
            )
        if value is not None:
            result[product] = value
    return result


def member_flow_weights(
    pressure: Mapping[str, float],
    *,
    gross_leverage: float = MAX_GROSS_LEVERAGE,
) -> dict[str, float]:
    """Allocate gross proportionally to signed member-flow pressure without tuning."""
    gross_leverage = float(gross_leverage)
    if not isfinite(gross_leverage) or gross_leverage < 0.0:
        raise ValueError("gross_leverage must be finite and nonnegative")
    if gross_leverage > MAX_GROSS_LEVERAGE + 1e-12:
        raise ValueError("member-flow gross leverage cannot exceed 2x")
    clean: dict[str, float] = {}
    for raw_product, raw_value in pressure.items():
        value = float(raw_value)
        if isfinite(value) and abs(value) > _EPS:
            clean[str(raw_product).upper()] = value
    denominator = float(sum(abs(value) for value in clean.values()))
    if denominator <= _EPS or gross_leverage <= _EPS:
        return {}
    result = {
        product: value * gross_leverage / denominator
        for product, value in sorted(clean.items())
    }
    if sum(abs(value) for value in result.values()) > MAX_GROSS_LEVERAGE + 1e-10:
        raise AssertionError("member-flow weights exceeded 2x gross")
    return result
