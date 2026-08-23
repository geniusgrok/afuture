from pathlib import Path
import sys
import types

import numpy as np
import pandas as pd

from afuture.execution_aligned_policy import (
    ExecutionAlignedAggressivePolicy,
    META_ANNUALIZED_WEIGHT,
    META_MIN_TEMPLATE_REBALANCE,
    META_SHARPE_WEIGHT,
    _clean_prices,
)


def _history(periods: int = 220):
    dates = pd.date_range("2025-01-01", periods=periods, freq="B")
    close = pd.DataFrame(
        {
            "A": 100 * np.cumprod([1.006 if i % 7 else 0.98 for i in range(periods)]),
            "M": 100 * np.cumprod([0.996 if i % 5 else 1.018 for i in range(periods)]),
            "RB": 100 * np.cumprod([1.004 if i % 4 else 0.985 for i in range(periods)]),
            "CU": 100 * np.cumprod([0.997 if i % 6 else 1.016 for i in range(periods)]),
        },
        index=dates,
    )
    overnight = np.where(np.arange(periods) % 3 == 0, 1.004, 0.998)
    open_prices = close.shift(1).mul(overnight, axis=0)
    open_prices.iloc[0] = close.iloc[0]
    return open_prices, close


def test_execution_aligned_policy_is_causal_and_capped_at_two_x():
    open_prices, close = _history()
    policy = ExecutionAlignedAggressivePolicy(products=tuple(close.columns))
    weights = policy.weight_history(open_prices, close)
    assert float(weights.abs().sum(axis=1).max()) <= 2.0 + 1e-12

    changed_open = open_prices.copy()
    changed_close = close.copy()
    changed_open.iloc[-1] *= [2.0, 0.5, 1.8, 0.6]
    changed_close.iloc[-1] *= [3.0, 0.4, 2.5, 0.5]
    changed = policy.weight_history(changed_open, changed_close)
    pd.testing.assert_series_equal(weights.iloc[-1], changed.iloc[-1])


def test_execution_aligned_policy_freezes_product_order():
    _, close = _history()
    ordered = _clean_prices(close, ("RB", "A", "M", "CU"))
    assert list(ordered.columns) == ["A", "CU", "M", "RB"]


def test_execution_aligned_policy_uses_frozen_meta_shape():
    policy = ExecutionAlignedAggressivePolicy(products=("A", "M"))
    assert policy.meta_lookback == 11
    assert policy.meta_rebalance == 3
    assert policy.meta_count == 3
    assert META_ANNUALIZED_WEIGHT == 0.25
    assert META_SHARPE_WEIGHT == 1.0
    assert META_MIN_TEMPLATE_REBALANCE == 5
    assert len(policy.template_ids) == 96
    assert len(policy.meta_eligible_template_ids) == 57
    assert all(
        int(item.split("_r", 1)[1].split("_g", 1)[0]) >= 5
        for item in policy.meta_eligible_template_ids
    )
    assert policy.meta_score_source == "continuous_intraday_base_stress_low_turnover"


def test_robust_meta_score_requires_both_cost_endpoints_and_favors_survival():
    from afuture.execution_aligned_policy import _robust_trailing_scores

    index = pd.date_range("2026-01-01", periods=12, freq="B")
    base = pd.DataFrame(
        {
            "fragile": [0.004] * 12,
            "robust": [0.003] * 12,
            "fails_stress": [0.002] * 12,
        },
        index=index,
    )
    stress = pd.DataFrame(
        {
            "fragile": [0.0001] * 12,
            "robust": [0.003] * 12,
            "fails_stress": [-0.001] * 12,
        },
        index=index,
    )

    scores = _robust_trailing_scores(base, stress, lookback=11)
    final = scores[-1]
    assert np.isfinite(final[0])
    assert np.isfinite(final[1])
    assert final[1] > final[0]
    assert np.isnan(final[2])


def test_execution_proxy_changes_meta_evidence_without_future_leakage():
    open_prices, close = _history()
    policy = ExecutionAlignedAggressivePolicy(products=tuple(close.columns))
    baseline = policy.weight_history(open_prices, close)

    altered_open = open_prices.copy()
    altered_open.iloc[-30:-1] = altered_open.iloc[-30:-1] * 1.03
    altered = policy.weight_history(altered_open, close)
    assert not baseline.iloc[-1].equals(altered.iloc[-1])


def test_l4_weight_generator_matches_production_policy(monkeypatch):
    tools_dir = Path(__file__).resolve().parents[1] / "tools"
    monkeypatch.syspath_prepend(str(tools_dir))
    monkeypatch.setitem(sys.modules, "akshare", types.ModuleType("akshare"))
    import evaluate_execution_aligned_target as l4

    open_prices, close = _history()
    rows = []
    for timestamp in close.index:
        for product in close.columns:
            rows.append(
                {
                    "date": timestamp,
                    "product": product,
                    "open": float(open_prices.loc[timestamp, product]),
                    "close": float(close.loc[timestamp, product]),
                }
            )
    continuous = pd.DataFrame(rows)
    products = tuple(sorted(close.columns))
    monkeypatch.setattr(l4, "REQUIRED_PRODUCTS", products)

    generated = l4.generate_execution_signal_weights(continuous)
    expected = ExecutionAlignedAggressivePolicy(products=products).weight_history(
        open_prices.reindex(columns=products),
        close.reindex(columns=products),
    )
    pd.testing.assert_frame_equal(
        generated,
        expected,
        check_names=False,
        check_freq=False,
    )
