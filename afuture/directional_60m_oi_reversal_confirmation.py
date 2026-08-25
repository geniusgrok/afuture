"""Research-only OI confirmation for entries, increases and direction changes.

This is a narrow extension of the validated 9-product D->D+1 60-minute Price x OI
confirmation rule. For supported products, a target that introduces a new directional
side (flat -> position or long <-> short) must agree with the completed-session OI flow.
An unconfirmed reversal exits the incumbent side to zero rather than preserving stale
risk. Same-sign increases also require confirmation. Reductions and exits always pass.
Unsupported products remain exactly unchanged.

The helper never increases absolute product exposure relative to the raw target and has
no fitted threshold, lookback, product ranking or return estimate.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd

_EPS = 1e-15


def apply_oi_confirmation_to_direction_changes(
    *,
    raw_weights: pd.DataFrame,
    confirming_flow: pd.DataFrame,
    supported_products: Iterable[str],
) -> pd.DataFrame:
    """Require confirmation for entries/adds/reversals while preserving risk exits."""
    raw = raw_weights.copy().astype(float)
    raw.index = pd.DatetimeIndex(pd.to_datetime(raw.index, errors="coerce")).normalize()
    raw = raw[~raw.index.isna()]
    raw.columns = [str(column).upper() for column in raw.columns]
    if not np.isfinite(raw.to_numpy()).all():
        raise ValueError("raw OI direction-change weights must be finite")

    flow = confirming_flow.copy().astype(float)
    flow.index = pd.DatetimeIndex(pd.to_datetime(flow.index, errors="coerce")).normalize()
    flow = flow[~flow.index.isna()]
    flow.columns = [str(column).upper() for column in flow.columns]
    flow = flow.reindex(index=raw.index, columns=raw.columns).fillna(0.0)
    if not np.isfinite(flow.to_numpy()).all():
        raise ValueError("confirming OI flow must be finite")

    supported = {str(product).upper() for product in supported_products}
    result = pd.DataFrame(0.0, index=raw.index, columns=raw.columns)
    previous = {str(product): 0.0 for product in raw.columns}

    for day in raw.index:
        for product in raw.columns:
            target = float(raw.at[day, product])
            current = float(previous[product])

            if product not in supported:
                final = target
            elif abs(target) <= _EPS:
                # Exit is always risk reducing and must never require confirmation.
                final = 0.0
            elif abs(current) <= _EPS:
                # Flat -> new directional risk.
                evidence = float(flow.at[day, product])
                final = target if evidence == float(np.sign(target)) else 0.0
            elif np.sign(target) != np.sign(current):
                # A reversal is exit-old + enter-new. The exit half always passes; if the
                # new side is not confirmed, stop at flat rather than preserving old risk.
                evidence = float(flow.at[day, product])
                final = target if evidence == float(np.sign(target)) else 0.0
            elif abs(target) > abs(current) + _EPS:
                # Same-direction add requires confirmation; otherwise keep the incumbent.
                evidence = float(flow.at[day, product])
                final = target if evidence == float(np.sign(target)) else current
            else:
                # Same-direction reduction passes exactly.
                final = target

            if abs(final) > abs(target) + _EPS:
                # The only case where a prior position is retained is a blocked add, and
                # by definition its magnitude is below the requested target. Fail closed
                # if any future refactor violates that invariant.
                raise AssertionError("OI direction confirmation increased product exposure")
            result.at[day, product] = final
            previous[product] = final

    if bool((result.abs().sum(axis=1) > raw.abs().sum(axis=1) + 1e-10).any()):
        raise AssertionError("OI direction confirmation increased raw gross exposure")
    return result.astype(float)
