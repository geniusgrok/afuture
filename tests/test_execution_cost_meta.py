import numpy as np
import pandas as pd

from afuture.execution_aligned_policy import ExecutionAlignedAggressivePolicy


def _candidate_module():
    import importlib

    return importlib.import_module("afuture.directional_cost_meta")


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


def test_net_switch_benefit_uses_comparable_return_units_without_threshold_grid():
    candidate_module = _candidate_module()
    benefit = candidate_module.net_switch_benefit(
        incumbent_expected_daily=0.001,
        candidate_expected_daily=0.002,
        transition_turnover=2.0,
        horizon_days=3,
        cost_bps=15.0,
    )
    assert abs(benefit) < 1e-12
    assert candidate_module.net_switch_benefit(
        incumbent_expected_daily=0.001,
        candidate_expected_daily=0.003,
        transition_turnover=1.0,
        horizon_days=3,
        cost_bps=15.0,
    ) > 0


def test_zero_transition_cost_candidate_is_exactly_legacy_meta_behavior():
    candidate_module = _candidate_module()
    open_prices, close = _history()
    policy = ExecutionAlignedAggressivePolicy(products=tuple(close.columns))
    legacy = policy.weight_history(open_prices, close)
    zero_cost, audit = candidate_module.cost_aware_weight_history(
        policy,
        open_prices,
        close,
        transition_cost_bps=0.0,
    )
    pd.testing.assert_frame_equal(legacy, zero_cost)
    assert not audit.empty
    assert set(
        [
            "incumbent_templates",
            "candidate_templates",
            "selected_templates",
            "candidate_turnover",
            "expected_alpha_improvement",
            "expected_transition_cost",
            "net_switch_benefit",
            "switched",
            "candidate_product_targets",
            "selected_product_targets",
        ]
    ).issubset(audit.columns)


def test_cost_aware_meta_is_causal_and_can_reject_a_non_positive_net_switch():
    candidate_module = _candidate_module()
    open_prices, close = _history()
    policy = ExecutionAlignedAggressivePolicy(products=tuple(close.columns))
    costed, audit = candidate_module.cost_aware_weight_history(
        policy,
        open_prices,
        close,
        transition_cost_bps=15.0,
    )
    changed_candidates = audit[
        audit["candidate_templates"] != audit["incumbent_templates"]
    ]
    rejected = changed_candidates[~changed_candidates["switched"]]
    assert not rejected.empty
    assert bool((rejected["net_switch_benefit"] <= 0.0 + 1e-15).all())

    changed_open = open_prices.copy()
    changed_close = close.copy()
    changed_open.iloc[-1] *= [2.0, 0.5, 1.8, 0.6]
    changed_close.iloc[-1] *= [3.0, 0.4, 2.5, 0.5]
    changed, _ = candidate_module.cost_aware_weight_history(
        policy,
        changed_open,
        changed_close,
        transition_cost_bps=15.0,
    )
    pd.testing.assert_series_equal(costed.iloc[-1], changed.iloc[-1])


def test_weight_no_trade_proxy_suppresses_only_low_edge_openings_and_increases():
    candidate_module = _candidate_module()
    index = pd.date_range("2026-01-01", periods=25, freq="B")
    target = pd.DataFrame(0.0, index=index, columns=["A", "M"])
    target.loc[index[20]:, "A"] = 1.0
    target.loc[index[20]:, "M"] = -1.0
    returns = pd.DataFrame(
        {"A": [0.0001] * 25, "M": [-0.002] * 25},
        index=index,
    )
    filtered, audit = candidate_module.cost_aware_no_trade_weights(
        target,
        returns,
        lookback=20,
        horizon_days=3,
        cost_bps=15.0,
    )
    assert filtered.loc[index[20], "A"] == 0.0
    assert filtered.loc[index[20], "M"] == -1.0
    assert int((audit["suppressed"] == True).sum()) >= 1  # noqa: E712


def test_weight_no_trade_proxy_is_causal_and_never_blocks_reduction_or_reversal():
    candidate_module = _candidate_module()
    index = pd.date_range("2026-01-01", periods=25, freq="B")
    target = pd.DataFrame(0.0, index=index, columns=["A"])
    target.loc[index[5]:index[20], "A"] = 1.0
    target.loc[index[21], "A"] = 0.5
    target.loc[index[22]:, "A"] = -1.0
    returns = pd.DataFrame({"A": [0.002] * 25}, index=index)
    baseline, _ = candidate_module.cost_aware_no_trade_weights(
        target,
        returns,
        lookback=5,
        horizon_days=3,
        cost_bps=15.0,
    )
    assert baseline.loc[index[21], "A"] == 0.5
    assert baseline.loc[index[22], "A"] == -1.0

    changed_returns = returns.copy()
    changed_returns.loc[index[22]:, "A"] = -0.2
    changed, _ = candidate_module.cost_aware_no_trade_weights(
        target,
        changed_returns,
        lookback=5,
        horizon_days=3,
        cost_bps=15.0,
    )
    pd.testing.assert_series_equal(
        baseline.loc[:index[22], "A"],
        changed.loc[:index[22], "A"],
    )
