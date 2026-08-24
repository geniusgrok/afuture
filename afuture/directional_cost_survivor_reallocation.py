"""Parameter-free reallocation of risk budget across cost-approved survivors.

The helper never creates a product, changes a sign, or raises portfolio gross above the
pre-filter target. It only rescales already-approved non-zero weights proportionally so
their aggregate gross reuses the original target budget when at least one survivor exists.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MAX_GROSS_LEVERAGE = 2.0
_EPS = 1e-12


def renormalize_survivor_risk_budget(
    *,
    original_weights: pd.DataFrame,
    approved_weights: pd.DataFrame,
) -> pd.DataFrame:
    """Restore original gross across approved survivors without adding support."""
    original = original_weights.copy().astype(float).fillna(0.0)
    if not original.index.equals(approved_weights.index):
        raise ValueError("approved weights index must match original weights")

    extra_columns = [column for column in approved_weights.columns if column not in original.columns]
    if extra_columns and bool(
        approved_weights[extra_columns].fillna(0.0).abs().gt(_EPS).any().any()
    ):
        raise ValueError("approved weights contain new product support")
    approved = approved_weights.reindex(columns=original.columns, fill_value=0.0).astype(float).fillna(0.0)

    if not np.isfinite(original.to_numpy()).all() or not np.isfinite(approved.to_numpy()).all():
        raise ValueError("weights must be finite")

    original_gross = original.abs().sum(axis=1)
    approved_gross = approved.abs().sum(axis=1)
    if bool((original_gross > MAX_GROSS_LEVERAGE + _EPS).any()):
        raise ValueError("original weights exceed 2x gross")

    new_support = (approved.abs() > _EPS) & (original.abs() <= _EPS)
    if bool(new_support.any().any()):
        raise ValueError("approved weights contain new product support")
    if bool((approved_gross > original_gross + _EPS).any()):
        raise ValueError("approved gross exceeds original gross")

    scale = pd.Series(0.0, index=original.index, dtype=float)
    active = approved_gross > _EPS
    scale.loc[active] = original_gross.loc[active] / approved_gross.loc[active]
    result = approved.mul(scale, axis=0)

    result_gross = result.abs().sum(axis=1)
    if bool((result_gross > original_gross + 1e-10).any()):
        raise AssertionError("survivor reallocation exceeded original gross")
    if bool((result_gross > MAX_GROSS_LEVERAGE + 1e-10).any()):
        raise AssertionError("survivor reallocation exceeded 2x gross")
    result_new_support = (result.abs() > _EPS) & (approved.abs() <= _EPS)
    if bool(result_new_support.any().any()):
        raise AssertionError("survivor reallocation created new support")
    sign_flip = (result.abs() > _EPS) & (approved.abs() > _EPS) & (np.sign(result) != np.sign(approved))
    if bool(sign_flip.any().any()):
        raise AssertionError("survivor reallocation changed sign")
    return result.astype(float)
