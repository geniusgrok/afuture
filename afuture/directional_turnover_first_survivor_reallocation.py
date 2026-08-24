"""Parameter-free turnover-first allocation across cost-approved survivors.

The cost-aware layer supplies only the eligible survivor set. Each target day keeps the
original OI target sign and aggregate gross, then solves a deterministic lexicographic
allocation:

1. minimize L1 target turnover versus the previous applied target;
2. among turnover-optimal allocations, minimize L1 tracking error to the original OI
   target; and
3. use proportional allocation only to resolve remaining exact ties.

No return forecast, threshold, fitted coefficient or product ranking is introduced. New
support and sign changes outside the original OI target are rejected, and gross cannot
exceed either the original target or 2x.
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

    original_support = original.abs() > _EPS
    approved_support = approved.abs() > _EPS
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


def _proportional_add(
    values: np.ndarray,
    capacity: np.ndarray,
    amount: float,
) -> float:
    total = float(capacity.sum())
    if amount <= _EPS or total <= _EPS:
        return float(amount)
    used = min(float(amount), total)
    values += capacity * (used / total)
    return float(amount - used)


def reallocate_survivors_turnover_first(
    *,
    original_weights: pd.DataFrame,
    approved_weights: pd.DataFrame,
) -> pd.DataFrame:
    """Restore original gross on survivors using turnover-first lexicographic allocation."""
    original, approved = _validated_frames(original_weights, approved_weights)
    result = pd.DataFrame(0.0, index=original.index, columns=original.columns)
    previous = np.zeros(len(original.columns), dtype=float)

    for timestamp in original.index:
        original_row = original.loc[timestamp].to_numpy(float)
        approved_row = approved.loc[timestamp].to_numpy(float)
        support = np.abs(approved_row) > _EPS
        gross = float(np.abs(original_row).sum())

        if gross <= _EPS or not bool(support.any()):
            previous = np.zeros_like(previous)
            continue

        signs = np.sign(original_row)
        original_magnitude = np.where(support, np.abs(original_row), 0.0)
        same_sign_previous = support & (np.sign(previous) == signs)
        previous_magnitude = np.where(
            same_sign_previous,
            np.abs(previous),
            0.0,
        )
        previous_survivor_gross = float(previous_magnitude.sum())

        if previous_survivor_gross >= gross - _EPS:
            # Primary minimum turnover: remain entirely inside the previous same-sign
            # survivor inventory and only reduce it to the new gross budget.
            magnitude = np.minimum(original_magnitude, previous_magnitude)
            residual = max(gross - float(magnitude.sum()), 0.0)
            capacity = np.maximum(previous_magnitude - magnitude, 0.0)
            residual = _proportional_add(magnitude, capacity, residual)
            if residual > 1e-10:
                raise AssertionError("turnover-first reduction could not fill gross")
        else:
            # Primary minimum turnover: keep every feasible previous same-sign survivor
            # lot, then add exactly the missing gross. Secondary tracking first fills
            # original-target deficits; any tracking-indifferent excess is allocated in
            # original survivor proportions.
            magnitude = previous_magnitude.copy()
            residual = max(gross - previous_survivor_gross, 0.0)
            deficits = np.where(
                support,
                np.maximum(original_magnitude - magnitude, 0.0),
                0.0,
            )
            residual = _proportional_add(magnitude, deficits, residual)
            if residual > _EPS:
                tertiary = np.where(support, original_magnitude, 0.0)
                total = float(tertiary.sum())
                if total <= _EPS:
                    raise AssertionError("survivor support has no original magnitude")
                magnitude += tertiary * (residual / total)
                residual = 0.0

        applied = np.where(support, signs * magnitude, 0.0)
        result.loc[timestamp] = applied
        previous = applied

    if not np.isfinite(result.to_numpy()).all():
        raise AssertionError("turnover-first survivor reallocation produced non-finite weights")

    original_gross = original.abs().sum(axis=1)
    result_gross = result.abs().sum(axis=1)
    survivor_days = approved.abs().sum(axis=1) > _EPS
    if bool(
        (
            (result_gross[survivor_days] - original_gross[survivor_days]).abs()
            > 1e-10
        ).any()
    ):
        raise AssertionError("turnover-first survivor allocation failed to restore gross")
    if bool((result_gross > original_gross + 1e-10).any()):
        raise AssertionError("turnover-first survivor allocation exceeded original gross")
    if bool((result_gross > MAX_GROSS_LEVERAGE + 1e-10).any()):
        raise AssertionError("turnover-first survivor allocation exceeded 2x gross")

    approved_support = approved.abs() > _EPS
    result_support = result.abs() > _EPS
    if bool((result_support & ~approved_support).any().any()):
        raise AssertionError("turnover-first survivor allocation created new support")
    sign_flip = result_support & (
        np.sign(result.to_numpy()) != np.sign(original.to_numpy())
    )
    if bool(np.asarray(sign_flip).any()):
        raise AssertionError("turnover-first survivor allocation changed target sign")
    return result.astype(float)
