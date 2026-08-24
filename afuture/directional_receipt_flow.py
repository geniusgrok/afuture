"""Point-in-time CZCE registered-receipt flow for Stress-80 research."""
from __future__ import annotations

from math import isfinite
from typing import Mapping, Sequence

import pandas as pd

MAX_GROSS_LEVERAGE = 2.0
_EPS = 1e-15


def aggregate_czce_receipt_flow(
    payload: Mapping[str, pd.DataFrame],
    *,
    products: Sequence[str],
) -> dict[str, float]:
    """Extract signed inventory pressure from exchange-published product total rows.

    Rising registered receipts are treated as negative pressure and falling receipts as
    positive pressure.  Only an explicit ``总计`` row is accepted, preventing warehouse,
    grade, and subtotal rows from being counted more than once.
    """
    normalized = {str(key).upper(): value for key, value in payload.items()}
    result: dict[str, float] = {}
    for product in sorted({str(item).upper() for item in products}):
        frame = normalized.get(product)
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            continue
        required = {"仓库编号", "仓单数量", "当日增减"}
        if not required.issubset(frame.columns):
            continue
        total = frame[frame["仓库编号"].astype(str).str.strip() == "总计"]
        if len(total) != 1:
            continue
        today = pd.to_numeric(total.iloc[0]["仓单数量"], errors="coerce")
        change = pd.to_numeric(total.iloc[0]["当日增减"], errors="coerce")
        if pd.isna(today) or pd.isna(change):
            continue
        today = float(today)
        change = float(change)
        yesterday = today - change
        denominator = max(abs(today), abs(yesterday), 1.0)
        value = -change / denominator
        if isfinite(value):
            result[product] = float(value)
    return result


def receipt_flow_weights(
    pressure: Mapping[str, float],
    *,
    gross_leverage: float = MAX_GROSS_LEVERAGE,
) -> dict[str, float]:
    gross_leverage = float(gross_leverage)
    if not isfinite(gross_leverage) or gross_leverage < 0.0:
        raise ValueError("gross_leverage must be finite and nonnegative")
    if gross_leverage > MAX_GROSS_LEVERAGE + 1e-12:
        raise ValueError("receipt-flow gross leverage cannot exceed 2x")
    clean = {
        str(product).upper(): float(value)
        for product, value in pressure.items()
        if isfinite(float(value)) and abs(float(value)) > _EPS
    }
    denominator = float(sum(abs(value) for value in clean.values()))
    if denominator <= _EPS or gross_leverage <= _EPS:
        return {}
    result = {
        product: value * gross_leverage / denominator
        for product, value in sorted(clean.items())
    }
    if sum(abs(value) for value in result.values()) > MAX_GROSS_LEVERAGE + 1e-10:
        raise AssertionError("receipt-flow weights exceeded 2x gross")
    return result
