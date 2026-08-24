"""Research-only causal product weights from completed dominant-contract basis."""
from __future__ import annotations

from math import isfinite
from typing import Iterable

import numpy as np
import pandas as pd

HARD_MAX_GROSS_LEVERAGE = 2.0


def build_basis_carry_weights(
    basis_rows: pd.DataFrame,
    *,
    target_index: Iterable,
    products: Iterable[str],
    max_gross_leverage: float = HARD_MAX_GROSS_LEVERAGE,
) -> pd.DataFrame:
    """Use D completed dominant basis carry for D+1 equal-gross direction.

    AKShare 1.18.84 reports ``dom_basis_rate`` as
    ``dominant_futures_price / spot_price - 1``. Therefore a positive value is futures
    premium/contango and has negative convergence carry for a long future; carry direction
    is the *negative* sign of that field. The candidate has no fitted threshold, lookback
    or cross-sectional rank. Missing or zero basis evidence produces zero exposure. A
    target session only consumes basis from the immediately preceding target session, so
    stale evidence never carries forward.
    """
    gross_cap = float(max_gross_leverage)
    if not isfinite(gross_cap) or not 0.0 < gross_cap <= HARD_MAX_GROSS_LEVERAGE:
        raise ValueError("max_gross_leverage must be in (0, 2.0]")
    ordered_products = tuple(sorted({str(value).upper() for value in products}))
    index = pd.DatetimeIndex(pd.to_datetime(list(target_index), errors="coerce")).normalize()
    if index.hasnans:
        raise ValueError("target_index contains invalid dates")
    if not index.is_monotonic_increasing or index.has_duplicates:
        raise ValueError("target_index must be unique and increasing")
    if not ordered_products:
        return pd.DataFrame(index=index)

    required = {"date", "symbol", "dom_basis_rate"}
    if basis_rows is None or not required.issubset(set(getattr(basis_rows, "columns", ()))):
        return pd.DataFrame(0.0, index=index, columns=ordered_products)

    frame = basis_rows.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    frame["symbol"] = frame["symbol"].astype(str).str.upper()
    frame["dom_basis_rate"] = pd.to_numeric(frame["dom_basis_rate"], errors="coerce")
    frame = frame[
        frame["date"].notna()
        & frame["symbol"].isin(ordered_products)
        & np.isfinite(frame["dom_basis_rate"])
    ].copy()
    frame.drop_duplicates(["date", "symbol"], keep="last", inplace=True)

    if frame.empty:
        return pd.DataFrame(0.0, index=index, columns=ordered_products)
    rates = (
        frame.pivot(index="date", columns="symbol", values="dom_basis_rate")
        .reindex(index=index, columns=ordered_products)
        .astype(float)
    )
    completed_carry_sign = -np.sign(rates).fillna(0.0)
    signal = completed_carry_sign.shift(1).fillna(0.0)
    active_count = (signal.abs() > 0.0).sum(axis=1).astype(float)
    per_product = pd.Series(0.0, index=index, dtype=float)
    active = active_count > 0.0
    per_product.loc[active] = gross_cap / active_count.loc[active]
    weights = signal.mul(per_product, axis=0).astype(float)
    if bool((weights.abs().sum(axis=1) > gross_cap + 1e-10).any()):
        raise AssertionError("basis carry weights exceeded gross cap")
    return weights
