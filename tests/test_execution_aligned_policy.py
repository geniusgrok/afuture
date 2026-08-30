import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from afuture.execution_aligned_policy import (
    META_ANNUALIZED_WEIGHT,
    META_SHARPE_WEIGHT,
    ExecutionAlignedAggressivePolicy,
    _clean_prices,
    _intraday_proxy_stream,
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


def _proxy_frames(*, open_a=100.0, close_a=110.0, weight_a=1.0):
    index = pd.DatetimeIndex(["2026-08-28"])
    open_prices = pd.DataFrame({"A": [open_a], "M": [100.0]}, index=index)
    close = pd.DataFrame({"A": [close_a], "M": [110.0]}, index=index)
    weights = pd.DataFrame({"A": [weight_a], "M": [1.0 - weight_a]}, index=index)
    return open_prices, close, weights


@pytest.mark.parametrize(
    ("kind", "open_a", "close_a"),
    [
        ("non_finite_open", np.nan, 110.0),
        ("non_finite_open", np.inf, 110.0),
        ("non_finite_open", -np.inf, 110.0),
        ("non_finite_close", 100.0, np.nan),
        ("non_finite_close", 100.0, np.inf),
        ("non_finite_close", 100.0, -np.inf),
        ("non_positive_open", 0.0, 110.0),
        ("non_positive_open", -1.0, 110.0),
        ("non_positive_close", 100.0, 0.0),
        ("non_positive_close", 100.0, -1.0),
        ("non_finite_return", 1e-320, 1e308),
        ("return_exceeds_limit", 100.0, 121.0),
        ("return_exceeds_limit", 100.0, 79.0),
    ],
)
def test_intraday_proxy_rejects_each_invalid_active_market_cell(kind, open_a, close_a):
    open_prices, close, weights = _proxy_frames(open_a=open_a, close_a=close_a)

    with pytest.raises(ValueError, match=kind) as error:
        _intraday_proxy_stream(open_prices, close, weights)

    message = str(error.value)
    assert "2026-08-28" in message
    assert "A" in message
    assert "weight=1.0" in message


@pytest.mark.parametrize(
    ("kind", "source", "axis"),
    [
        ("missing_open", "open_prices", "columns"),
        ("missing_open", "open_prices", "index"),
        ("missing_close", "close", "columns"),
        ("missing_close", "close", "index"),
    ],
)
def test_intraday_proxy_rejects_missing_active_market_coordinate(kind, source, axis):
    open_prices, close, weights = _proxy_frames()
    frame = open_prices if source == "open_prices" else close
    if axis == "columns":
        frame.drop(columns="A", inplace=True)
    else:
        frame.drop(index=frame.index[0], inplace=True)

    with pytest.raises(ValueError, match=kind) as error:
        _intraday_proxy_stream(open_prices, close, weights)

    message = str(error.value)
    assert "2026-08-28" in message
    assert "A" in message
    assert "weight=1.0" in message


@pytest.mark.parametrize(
    ("open_a", "close_a"),
    [
        (np.nan, 110.0),
        (np.inf, 110.0),
        (-np.inf, 110.0),
        (100.0, np.nan),
        (100.0, np.inf),
        (100.0, -np.inf),
        (0.0, 110.0),
        (-1.0, 110.0),
        (100.0, 0.0),
        (100.0, -1.0),
        (1e-320, 1e308),
        (100.0, 121.0),
        (100.0, 79.0),
    ],
)
def test_intraday_proxy_ignores_each_invalid_zero_exposure_market_cell(open_a, close_a):
    open_prices, close, weights = _proxy_frames(
        open_a=open_a,
        close_a=close_a,
        weight_a=0.0,
    )

    result = _intraday_proxy_stream(open_prices, close, weights)

    assert result.iloc[0] == pytest.approx(0.1 - 5.0 / 10000.0)


@pytest.mark.parametrize(
    ("source", "axis"),
    [
        ("open_prices", "columns"),
        ("open_prices", "index"),
        ("close", "columns"),
        ("close", "index"),
    ],
)
def test_intraday_proxy_ignores_missing_zero_exposure_market_coordinate(source, axis):
    open_prices, close, weights = _proxy_frames(weight_a=0.0)
    frame = open_prices if source == "open_prices" else close
    if axis == "columns":
        frame.drop(columns="A", inplace=True)
        expected = 0.1 - 5.0 / 10000.0
    else:
        weights.iloc[0] = 0.0
        frame.drop(index=frame.index[0], inplace=True)
        expected = 0.0

    result = _intraday_proxy_stream(open_prices, close, weights)

    assert result.iloc[0] == pytest.approx(expected)


@pytest.mark.parametrize("close_a", [80.0, 120.0])
def test_intraday_proxy_accepts_return_at_exact_absolute_limit(close_a):
    open_prices, close, weights = _proxy_frames(close_a=close_a)

    result = _intraday_proxy_stream(open_prices, close, weights)

    assert result.iloc[0] == pytest.approx(close_a / 100.0 - 1.0 - 5.0 / 10000.0)


def test_intraday_proxy_preserves_legal_pnl_turnover_and_cost():
    index = pd.DatetimeIndex(["2026-08-27", "2026-08-28"])
    open_prices = pd.DataFrame({"A": [100.0, 100.0], "M": [100.0, 100.0]}, index=index)
    close = pd.DataFrame({"A": [110.0, 120.0], "M": [90.0, 100.0]}, index=index)
    weights = pd.DataFrame({"A": [1.0, 0.5], "M": [0.0, 0.5]}, index=index)

    result = _intraday_proxy_stream(open_prices, close, weights)

    pd.testing.assert_series_equal(
        result,
        pd.Series([0.0995, 0.0995], index=index),
        check_names=False,
    )


def test_weight_history_does_not_fallback_when_an_active_proxy_cell_is_invalid(monkeypatch):
    import afuture.execution_aligned_policy as policy_module

    open_prices, close = _history()
    close.iloc[-2, close.columns.get_loc("A")] = np.nan

    def always_active_a(returns, _template):
        result = pd.DataFrame(0.0, index=returns.index, columns=returns.columns)
        result["A"] = 1.0
        return result

    monkeypatch.setattr(policy_module, "_template_weight_path", always_active_a)
    policy = ExecutionAlignedAggressivePolicy(products=tuple(close.columns))

    with pytest.raises(ValueError, match="non_finite_close"):
        policy.weight_history(open_prices, close)


def test_execution_aligned_policy_is_causal_and_capped_at_two_x():
    open_prices, close = _history()
    policy = ExecutionAlignedAggressivePolicy(products=tuple(close.columns))
    weights = policy.weight_history(open_prices, close)
    assert float(weights.abs().sum(axis=1).max()) <= 2.0 + 1e-12

    changed_open = open_prices.copy()
    changed_close = close.copy()
    changed_open.iloc[-1] *= [1.08, 0.93, 1.04, 0.97]
    changed_close.iloc[-1] *= [0.96, 1.07, 0.97, 1.03]
    changed_intraday = changed_close.iloc[-1] / changed_open.iloc[-1] - 1.0
    assert bool((changed_intraday.abs() <= 0.20).all())
    assert not changed_intraday.equals(close.iloc[-1] / open_prices.iloc[-1] - 1.0)
    changed = policy.weight_history(changed_open, changed_close)
    pd.testing.assert_series_equal(weights.iloc[-1], changed.iloc[-1])


def test_execution_aligned_policy_freezes_product_order():
    _, close = _history()
    ordered = _clean_prices(close, ("RB", "A", "M", "CU"))
    assert list(ordered.columns) == ["A", "CU", "M", "RB"]


def test_execution_aligned_policy_uses_frozen_meta_shape_without_turnover_pool_filter():
    policy = ExecutionAlignedAggressivePolicy(products=("A", "M"))
    assert policy.meta_lookback == 11
    assert policy.meta_rebalance == 3
    assert policy.meta_count == 3
    assert META_ANNUALIZED_WEIGHT == 0.25
    assert META_SHARPE_WEIGHT == 1.0
    assert len(policy.template_ids) == 96
    assert policy.meta_score_source == "continuous_intraday_base_rank_stress_survival"


def test_robust_meta_score_requires_stress_survival_but_preserves_base_ranking():
    from afuture.execution_aligned_policy import _robust_trailing_scores

    index = pd.date_range("2026-01-01", periods=12, freq="B")
    base = pd.DataFrame(
        {
            "strong_base": [0.004] * 12,
            "strong_stress": [0.003] * 12,
            "fails_stress": [0.002] * 12,
        },
        index=index,
    )
    stress = pd.DataFrame(
        {
            "strong_base": [0.0001] * 12,
            "strong_stress": [0.003] * 12,
            "fails_stress": [-0.001] * 12,
        },
        index=index,
    )

    scores = _robust_trailing_scores(base, stress, lookback=11)
    final = scores[-1]
    assert np.isfinite(final[0])
    assert np.isfinite(final[1])
    assert final[0] > final[1]
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
