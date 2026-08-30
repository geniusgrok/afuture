from __future__ import annotations

import numpy as np
import pandas as pd

from afuture.alpha_efficiency_policy import AlphaEfficiencyDirectionalPolicy
from afuture.directional_alpha_efficiency import (
    build_product_alpha_efficiency_score,
)
from afuture.execution_aligned_policy import ExecutionAlignedAggressivePolicy


def _panel(periods: int = 180):
    index = pd.bdate_range("2025-01-01", periods=periods)
    close = pd.DataFrame(
        {
            "A": 100.0 * np.cumprod([1.01 if i % 3 else 0.995 for i in range(periods)]),
            "B": 100.0 * np.cumprod([0.995 if i % 3 else 1.002 for i in range(periods)]),
            "C": 100.0 * np.cumprod([1.006 if i % 5 else 0.99 for i in range(periods)]),
            "D": 100.0 * np.cumprod([1.001 if i % 2 else 0.999 for i in range(periods)]),
        },
        index=index,
    )
    open_prices = close.shift(1)
    open_prices.iloc[0] = close.iloc[0]
    return open_prices, close


def test_alpha_efficiency_score_uses_only_completed_prior_alpha_and_turnover():
    index = pd.bdate_range("2026-01-01", periods=6)
    open_prices = pd.DataFrame(
        [
            [100, 100, 100],
            [100, 100, 100],
            [100, 100, 100],
            [100, 100, 100],
            [100, 100, 100],
            [100, 100, 100],
        ],
        index=index,
        columns=["A", "B", "C"],
        dtype=float,
    )
    close = pd.DataFrame(
        [
            [101, 99, 100],
            [102, 99, 100],
            [103, 99, 100],
            [104, 99, 100],
            [105, 99, 100],
            [106, 99, 100],
        ],
        index=index,
        columns=open_prices.columns,
        dtype=float,
    )
    raw = pd.DataFrame(
        [[1, 1, 0], [1, 1, 0], [1, 1, 0], [1, 1, 0], [1, 1, 0], [1, 1, 0]],
        index=index,
        columns=open_prices.columns,
        dtype=float,
    )

    score = build_product_alpha_efficiency_score(open_prices, close, raw)

    assert score.iloc[-1]["A"] > score.iloc[-1]["B"]
    assert np.isnan(score.iloc[-1]["C"])

    changed_open = open_prices.copy()
    changed_close = close.copy()
    changed_open.iloc[-1] = [50, 200, 100]
    changed_close.iloc[-1] = [200, 50, 100]
    changed = build_product_alpha_efficiency_score(changed_open, changed_close, raw)
    pd.testing.assert_series_equal(score.iloc[-1], changed.iloc[-1])


def test_alpha_efficiency_policy_changes_selection_without_creating_or_flipping_risk():
    open_prices, close = _panel()
    products = tuple(close.columns)
    core = ExecutionAlignedAggressivePolicy(products=products)
    policy = AlphaEfficiencyDirectionalPolicy(products=products)

    raw = core.weight_history(open_prices, close)
    adjusted = policy.weight_history(open_prices, close)

    assert not adjusted.equals(raw)
    assert bool((adjusted.abs() <= raw.abs() + 1e-12).all().all())
    assert bool(((adjusted * raw) >= -1e-12).all().all())
    assert float(adjusted.abs().sum(axis=1).max()) <= 2.0 + 1e-12


def test_alpha_efficiency_policy_target_is_causal_to_final_day_outcome():
    open_prices, close = _panel()
    policy = AlphaEfficiencyDirectionalPolicy(products=tuple(close.columns))
    baseline = policy.weight_history(open_prices, close)

    changed_open = open_prices.copy()
    changed_close = close.copy()
    changed_open.iloc[-1] *= [1.08, 0.93, 1.04, 0.97]
    changed_close.iloc[-1] *= [0.96, 1.07, 0.97, 1.03]
    changed_intraday = changed_close.iloc[-1] / changed_open.iloc[-1] - 1.0
    assert bool((changed_intraday.abs() <= 0.20).all())
    assert not changed_intraday.equals(close.iloc[-1] / open_prices.iloc[-1] - 1.0)
    changed = policy.weight_history(changed_open, changed_close)

    pd.testing.assert_series_equal(baseline.iloc[-1], changed.iloc[-1])
