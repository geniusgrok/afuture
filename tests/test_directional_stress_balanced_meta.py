import numpy as np
import pandas as pd


def _api():
    from afuture.directional_stress_balanced_meta import (
        META_COUNT,
        META_LOOKBACK,
        META_REBALANCE,
        StressBalancedMetaPolicy,
        combine_completed_base_stress_scores,
    )

    return (
        META_LOOKBACK,
        META_REBALANCE,
        META_COUNT,
        StressBalancedMetaPolicy,
        combine_completed_base_stress_scores,
    )


def test_stress_balanced_meta_keeps_frozen_meta_shape():
    lookback, rebalance, count, _, _ = _api()
    assert lookback == 11
    assert rebalance == 3
    assert count == 3


def test_score_is_exact_50_50_average_only_when_both_endpoints_survive():
    _, _, _, _, combine = _api()
    base = np.array([[2.0, np.nan, 4.0], [1.0, 3.0, np.nan]])
    stress = np.array([[4.0, 5.0, np.nan], [3.0, np.nan, 7.0]])

    result = combine(base, stress)

    assert result[0, 0] == 3.0
    assert result[1, 0] == 2.0
    assert np.isnan(result[0, 1])
    assert np.isnan(result[0, 2])
    assert np.isnan(result[1, 1])
    assert np.isnan(result[1, 2])


def test_policy_is_deterministic_and_never_exceeds_two_x_gross():
    _, _, _, Policy, _ = _api()
    index = pd.bdate_range("2025-01-01", periods=150)
    open_prices = pd.DataFrame(
        {
            "AG": 100.0 + np.linspace(0.0, 30.0, len(index)),
            "CU": 100.0 + np.sin(np.arange(len(index)) / 5.0) * 8.0,
        },
        index=index,
    )
    close = open_prices.copy()
    close["AG"] = open_prices["AG"] * (1.0 + 0.002 * np.sin(np.arange(len(index)) / 3.0))
    close["CU"] = open_prices["CU"] * (1.0 + 0.003 * np.cos(np.arange(len(index)) / 4.0))

    policy = Policy(products=("AG", "CU"))
    first = policy.weight_history(open_prices, close)
    second = policy.weight_history(open_prices, close)

    pd.testing.assert_frame_equal(first, second)
    assert float(first.abs().sum(axis=1).max()) <= 2.0 + 1e-10
    assert policy.score_source == "continuous_intraday_equal_base_stress"
