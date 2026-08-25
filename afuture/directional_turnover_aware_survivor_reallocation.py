"""Parameter-free lexicographic reallocation across cost-approved survivors.

The cost-aware layer is used only as an eligibility mask. On each target day this helper:

1. keeps only products approved by that layer and preserves the original OI target sign;
2. restores the original OI aggregate gross when at least one survivor exists;
3. minimizes L1 tracking error to the original OI target; and
4. among the resulting tracking-equivalent allocations, minimizes L1 target turnover
   versus the previous applied allocation, with deterministic proportional tie-breaking.

No threshold, fitted coefficient, product ranking or return estimate is introduced here.
The helper cannot create support, change a sign, or exceed the original/2x gross budget.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MAX_GROSS_LEVERAGE = 2.0
_EPS = 1e-12


def _validated_frames(
    original_weights: pd.DataFrame,
    approved_weights: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    original = original_weights.copy().astype(float).fillna(0.0)
    if not original.index.equals(approved_weights.index):
        raise ValueError("approved weights index must match original weights")

    extra_columns = [
        column for column in approved_weights.columns if column not in original.columns
    ]
    if extra_columns and bool(
        approved_weights[extra_columns].fillna(0.0).abs().gt(_EPS).any().any()
    ):
        raise ValueError("approved weights contain new product support")

    approved = (
        approved_weights.reindex(columns=original.columns, fill_value=0.0)
        .astype(float)
        .fillna(0.0)
    )
    if not np.isfinite(original.to_numpy()).all() or not np.isfinite(
        approved.to_numpy()
    ).all():
        raise ValueError("weights must be finite")

    approved_support = approved.abs() > _EPS
    original_support = original.abs() > _EPS
    if bool((approved_support & ~original_support).any().any()):
        raise ValueError("approved weights contain new product support")

    sign_flip = approved_support & (
        np.sign(approved.to_numpy()) != np.sign(original.to_numpy())
    )
    if bool(np.asarray(sign_flip).any()):
        raise ValueError("approved weights changed original target sign")

    original_gross = original.abs().sum(axis=1)
    approved_gross = approved.abs().sum(axis=1)
    if bool((original_gross > MAX_GROSS_LEVERAGE + _EPS).any()):
        raise ValueError("original weights exceed 2x gross")
    if bool((approved_gross > original_gross + _EPS).any()):
        raise ValueError("approved gross exceeds original gross")
    return original, approved


def reallocate_survivors_lexicographically(
    *,
    original_weights: pd.DataFrame,
    approved_weights: pd.DataFrame,
) -> pd.DataFrame:
    """Restore gross on survivors with tracking-first, turnover-second allocation."""
    original, approved = _validated_frames(original_weights, approved_weights)
    result = pd.DataFrame(0.0, index=original.index, columns=original.columns)
    previous: np.ndarray = np.zeros(len(original.columns), dtype=float)

    for timestamp in original.index:
        original_row = original.loc[timestamp].to_numpy(float)
        approved_row = approved.loc[timestamp].to_numpy(float)
        support = np.abs(approved_row) > _EPS
        original_gross = float(np.abs(original_row).sum())

        if original_gross <= _EPS or not bool(support.any()):
            previous = np.zeros_like(previous)
            continue

        signs = np.sign(original_row)
        base = np.where(support, np.abs(original_row), 0.0)
        base_gross = float(base.sum())
        residual = max(original_gross - base_gross, 0.0)
        magnitudes = base.copy()

        # Every unit of residual necessarily increases L1 distance from the original
        # target by one unit. Within that tracking-equivalent set, a unit assigned toward
        # yesterday's same-sign excess reduces target turnover one-for-one. Consume that
        # capacity first. Proportional allocation makes simultaneous ties deterministic.
        same_sign_previous = support & (np.sign(previous) == signs)
        turnover_capacity = np.where(
            same_sign_previous,
            np.maximum(np.abs(previous) - base, 0.0),
            0.0,
        )
        capacity_total = float(turnover_capacity.sum())
        if residual > _EPS and capacity_total > _EPS:
            used = min(residual, capacity_total)
            magnitudes += turnover_capacity * (used / capacity_total)
            residual -= used

        # Any residual beyond the previous-target capacity is turnover-indifferent.
        # Preserve the original survivor composition proportionally as a deterministic
        # tertiary tie-break rather than inventing a product preference.
        if residual > _EPS:
            denominator = float(base.sum())
            if denominator <= _EPS:
                raise AssertionError("survivor support has no original magnitude")
            magnitudes += base * (residual / denominator)

        applied = np.where(support, signs * magnitudes, 0.0)
        result.loc[timestamp] = applied
        previous = applied

    values = result.to_numpy(float)
    if not np.isfinite(values).all():
        raise AssertionError("turnover-aware survivor reallocation produced non-finite weights")

    original_gross = original.abs().sum(axis=1)
    result_gross = result.abs().sum(axis=1)
    survivor_days = approved.abs().sum(axis=1) > _EPS
    if bool(
        (
            (result_gross[survivor_days] - original_gross[survivor_days]).abs()
            > 1e-10
        ).any()
    ):
        raise AssertionError("survivor reallocation failed to restore original gross")
    if bool((result_gross > original_gross + 1e-10).any()):
        raise AssertionError("survivor reallocation exceeded original gross")
    if bool((result_gross > MAX_GROSS_LEVERAGE + 1e-10).any()):
        raise AssertionError("survivor reallocation exceeded 2x gross")

    approved_support = approved.abs() > _EPS
    result_support = result.abs() > _EPS
    if bool((result_support & ~approved_support).any().any()):
        raise AssertionError("survivor reallocation created new support")
    sign_flip = result_support & (
        np.sign(result.to_numpy()) != np.sign(original.to_numpy())
    )
    if bool(np.asarray(sign_flip).any()):
        raise AssertionError("survivor reallocation changed target sign")
    return result.astype(float)
