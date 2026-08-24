"""Causal, parameter-free holding-lifecycle evidence for Shadow MPV research.

A baseline signal run is a contiguous sequence of non-zero targets with the same sign;
weight resizing inside the sign does not end the run. A run becomes completed when a
later baseline target is zero or reverses. That completion is not allowed to influence
the same decision day that reveals the end; it enters lifecycle statistics only after
that day's horizon has been computed.

Expected total run duration is an expanding product+direction mean shrunk toward the
global completed-run mean. Prior strength is the median positive support count across
observed product+direction keys. No lookback, minimum-observation threshold or fitted
holding horizon is introduced.
"""
from __future__ import annotations

from math import isfinite

import numpy as np
import pandas as pd

_RUN_COLUMNS = (
    "product",
    "direction",
    "start_date",
    "end_date",
    "sessions",
    "label_available_date",
)


def _normalize_weights(weights: pd.DataFrame) -> pd.DataFrame:
    frame = weights.copy().astype(float)
    frame.index = pd.to_datetime(frame.index, errors="coerce").normalize()
    frame = frame.loc[~frame.index.isna()].sort_index().fillna(0.0)
    frame.columns = [str(column).upper() for column in frame.columns]
    return frame


def _direction(value: float) -> int:
    value = float(value)
    if not isfinite(value) or abs(value) <= 1e-15:
        return 0
    return 1 if value > 0.0 else -1


def build_completed_signal_runs(weights: pd.DataFrame) -> pd.DataFrame:
    """Return only fully observed baseline same-sign runs."""
    frame = _normalize_weights(weights)
    rows: list[dict] = []
    if frame.empty:
        return pd.DataFrame(columns=_RUN_COLUMNS)

    for product in frame.columns:
        active_direction = 0
        start_date: pd.Timestamp | None = None
        previous_date: pd.Timestamp | None = None
        sessions = 0
        for day, raw_value in frame[product].items():
            day = pd.Timestamp(day).normalize()
            current = _direction(raw_value)
            if active_direction != 0 and current != active_direction:
                rows.append(
                    {
                        "product": product,
                        "direction": active_direction,
                        "start_date": start_date,
                        "end_date": previous_date,
                        "sessions": sessions,
                        "label_available_date": day,
                    }
                )
            if current == 0:
                active_direction = 0
                start_date = None
                sessions = 0
            elif current == active_direction:
                sessions += 1
            else:
                active_direction = current
                start_date = day
                sessions = 1
            previous_date = day

    if not rows:
        return pd.DataFrame(columns=_RUN_COLUMNS)
    result = pd.DataFrame(rows, columns=_RUN_COLUMNS)
    return result.sort_values(
        ["label_available_date", "product", "direction"],
        kind="stable",
        ignore_index=True,
    )


def _estimate_total_duration(
    *,
    product: str,
    direction: int,
    duration_sum: dict[tuple[str, int], float],
    duration_count: dict[tuple[str, int], int],
    global_sum: float,
    global_count: int,
) -> float:
    if global_count <= 0:
        return 1.0
    global_mean = float(global_sum) / float(global_count)
    counts = [int(value) for value in duration_count.values() if int(value) > 0]
    prior_support = float(np.median(counts)) if counts else 0.0
    key = (str(product).upper(), int(direction))
    support = int(duration_count.get(key, 0))
    if support <= 0:
        estimate = global_mean
    else:
        local = float(duration_sum[key]) / float(support)
        weight = support / (support + prior_support) if prior_support > 0.0 else 1.0
        estimate = weight * local + (1.0 - weight) * global_mean
    if not isfinite(estimate) or estimate < 1.0:
        return 1.0
    return float(estimate)


def build_causal_remaining_horizon_panel(weights: pd.DataFrame) -> pd.DataFrame:
    """Return causal expected remaining same-sign sessions for every target day."""
    frame = _normalize_weights(weights)
    panel = pd.DataFrame(1.0, index=frame.index, columns=frame.columns, dtype=float)
    if frame.empty:
        return panel

    duration_sum: dict[tuple[str, int], float] = {}
    duration_count: dict[tuple[str, int], int] = {}
    global_sum = 0.0
    global_count = 0

    active_direction = {product: 0 for product in frame.columns}
    active_age = {product: 0 for product in frame.columns}

    for day, row in frame.iterrows():
        completions: list[tuple[str, int, int]] = []
        next_direction: dict[str, int] = {}
        next_age: dict[str, int] = {}

        # Compute today's expected remaining horizon before adding runs whose end is
        # revealed by today's baseline target. This enforces strict < decision-day use.
        for product, raw_value in row.items():
            current = _direction(raw_value)
            previous = int(active_direction[product])
            previous_age = int(active_age[product])

            if current == 0:
                age = 0
                panel.at[day, product] = 1.0
            else:
                age = previous_age + 1 if current == previous and previous != 0 else 1
                expected_total = _estimate_total_duration(
                    product=product,
                    direction=current,
                    duration_sum=duration_sum,
                    duration_count=duration_count,
                    global_sum=global_sum,
                    global_count=global_count,
                )
                panel.at[day, product] = max(1.0, expected_total - float(age) + 1.0)

            if previous != 0 and current != previous:
                completions.append((product, previous, previous_age))
            next_direction[product] = current
            next_age[product] = age

        for product, direction, sessions in completions:
            if sessions <= 0:
                continue
            key = (str(product).upper(), int(direction))
            duration_sum[key] = float(duration_sum.get(key, 0.0)) + float(sessions)
            duration_count[key] = int(duration_count.get(key, 0)) + 1
            global_sum += float(sessions)
            global_count += 1

        active_direction = next_direction
        active_age = next_age

    return panel
