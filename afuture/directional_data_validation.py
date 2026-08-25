"""Fail-closed validation for causal daily directional inputs."""

from __future__ import annotations

from collections.abc import Collection

import numpy as np
import pandas as pd


def validate_daily_index(frame: pd.DataFrame, *, name: str) -> None:
    """Require one chronologically ordered observation per calendar date.

    Sorting or deduplicating here would make provider row order choose the historical
    truth. Callers may normalize a validated index, but conflicting daily rows must be
    fixed at the data source.
    """
    if frame.empty:
        raise ValueError(f"{name} cannot be empty")
    parsed = pd.to_datetime(frame.index, errors="coerce")
    invalid = pd.isna(parsed)
    if bool(invalid.any()):
        position = int(np.flatnonzero(np.asarray(invalid))[0])
        raise ValueError(
            f"{name} has invalid date at index position {position}: "
            f"{frame.index[position]!r}"
        )
    index = pd.DatetimeIndex(parsed)
    daily = index.normalize()
    duplicate = daily.duplicated(keep=False)
    if bool(duplicate.any()):
        day = daily[duplicate][0]
        raise ValueError(
            f"{name} has duplicate daily date: {day.date().isoformat()}"
        )
    if not daily.is_monotonic_increasing:
        raise ValueError(f"{name} dates must be monotonic increasing")


def validate_finite_columns(
    frame: pd.DataFrame,
    columns: Collection[str],
    *,
    name: str,
    positive: Collection[str] = (),
) -> None:
    """Require numeric finite values, with strict positivity for economic prices."""
    required = tuple(dict.fromkeys([*columns, *positive]))
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(f"{name} missing columns: {', '.join(missing)}")
    positive_columns = set(positive)
    for column in required:
        values = pd.to_numeric(frame[column], errors="coerce")
        numeric = values.to_numpy(dtype=float)
        finite = np.isfinite(numeric)
        if not bool(finite.all()):
            position = int(np.flatnonzero(~finite)[0])
            raise ValueError(
                f"{name} column {column} must be finite; "
                f"invalid row={frame.index[position]!r}"
            )
        if column in positive_columns:
            invalid = numeric <= 0
            if bool(invalid.any()):
                position = int(np.flatnonzero(invalid)[0])
                raise ValueError(
                    f"{name} column {column} must be positive; "
                    f"invalid row={frame.index[position]!r}, "
                    f"value={numeric[position]!r}"
                )


def validate_unique_keys(
    frame: pd.DataFrame,
    columns: Collection[str],
    *,
    name: str,
) -> None:
    """Reject ambiguous observations for a declared compound business key."""
    keys = tuple(columns)
    missing = [column for column in keys if column not in frame.columns]
    if missing:
        raise ValueError(f"{name} missing columns: {', '.join(missing)}")
    duplicate = frame.duplicated(list(keys), keep=False)
    if bool(duplicate.any()):
        row = frame.loc[duplicate, list(keys)].iloc[0]
        rendered = ", ".join(
            f"{column}={row[column]!r}" for column in keys
        )
        raise ValueError(
            f"{name} has duplicate {'/'.join(keys)} observation: {rendered}"
        )
