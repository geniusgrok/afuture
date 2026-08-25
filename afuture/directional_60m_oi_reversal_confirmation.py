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

import pandas as pd

from .directional_60m_oi_confirmation import apply_oi_confirmation_to_weights


def apply_oi_confirmation_to_direction_changes(
    *,
    raw_weights: pd.DataFrame,
    confirming_flow: pd.DataFrame,
    supported_products: Iterable[str],
) -> pd.DataFrame:
    """Require confirmation for entries/adds/reversals while preserving risk exits."""
    return apply_oi_confirmation_to_weights(
        raw_weights=raw_weights,
        confirming_flow=confirming_flow,
        supported_products=supported_products,
    )
