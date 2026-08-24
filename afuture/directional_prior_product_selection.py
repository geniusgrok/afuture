"""Research-only prior-window product selection for Stress-80 investigation.

The product set is selected strictly from the two pre-Production prior windows. A product
must have positive Stress net alpha per turnover in both prior1 and prior2. After the set
is frozen, the daily raw directional gross is redistributed only across surviving active
products in proportion to their original absolute weights, preserving signs and never
increasing the raw day's gross or the 2x hard ceiling.
"""
from __future__ import annotations

from math import isfinite
from typing import Mapping

import pandas as pd

MAX_GROSS_LEVERAGE = 2.0
_EPS = 1e-12


def freeze_positive_prior_products(
    *,
    prior1: Mapping[str, Mapping[str, float]],
    prior2: Mapping[str, Mapping[str, float]],
) -> tuple[str, ...]:
    """Freeze products with strictly positive net-alpha/turnover in both prior windows."""
    result: list[str] = []
    common = sorted({str(item).upper() for item in prior1} & {str(item).upper() for item in prior2})
    normalized1 = {str(key).upper(): value for key, value in prior1.items()}
    normalized2 = {str(key).upper(): value for key, value in prior2.items()}
    for product in common:
        try:
            first = float(normalized1[product]["net_alpha_per_turnover_bps"])
            second = float(normalized2[product]["net_alpha_per_turnover_bps"])
        except (KeyError, TypeError, ValueError):
            continue
        if isfinite(first) and isfinite(second) and first > 0.0 and second > 0.0:
            result.append(product)
    return tuple(result)


def redistribute_to_frozen_products(
    raw_weights: pd.DataFrame,
    *,
    frozen_products: tuple[str, ...],
) -> pd.DataFrame:
    """Preserve daily raw gross while removing products not in the frozen prior set."""
    raw = raw_weights.copy().astype(float)
    raw.index = pd.to_datetime(raw.index, errors="coerce")
    raw = raw[~raw.index.isna()].sort_index()
    raw.columns = [str(column).upper() for column in raw.columns]
    raw = raw.fillna(0.0)

    original_gross = raw.abs().sum(axis=1)
    if bool((original_gross > MAX_GROSS_LEVERAGE + _EPS).any()):
        raise ValueError("raw directional target exceeds 2x gross")

    frozen = {str(product).upper() for product in frozen_products}
    result = pd.DataFrame(0.0, index=raw.index, columns=raw.columns)
    eligible_columns = [column for column in raw.columns if column in frozen]
    if not eligible_columns:
        return result

    for timestamp in raw.index:
        target_gross = min(float(original_gross.loc[timestamp]), MAX_GROSS_LEVERAGE)
        surviving = raw.loc[timestamp, eligible_columns]
        surviving_gross = float(surviving.abs().sum())
        if target_gross <= _EPS or surviving_gross <= _EPS:
            continue
        scale = target_gross / surviving_gross
        result.loc[timestamp, eligible_columns] = surviving * scale

    if bool((result.abs().sum(axis=1) > original_gross + _EPS).any()):
        raise AssertionError("prior-product redistribution increased raw daily gross")
    if bool((result.abs().sum(axis=1) > MAX_GROSS_LEVERAGE + _EPS).any()):
        raise AssertionError("prior-product redistribution exceeded 2x gross")
    return result
