import numpy as np
import pandas as pd

from afuture import execution_aligned_policy as policy_module
from afuture.execution_aligned_policy import ExecutionAlignedAggressivePolicy


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
    benefit = policy_module._net_switch_benefit(
        incumbent_expected_daily=0.001,
        candidate_expected_daily=0.002,
        transition_turnover=2.0,
        horizon_days=3,
        cost_bps=15.0,
    )
    assert abs(benefit) < 1e-12
    assert policy_module._net_switch_benefit(
        incumbent_expected_daily=0.001,
        candidate_expected_daily=0.003,
        transition_turnover=1.0,
        horizon_days=3,
        cost_bps=15.0,
    ) > 0


def test_zero_transition_cost_path_is_exactly_legacy_meta_behavior():
    open_prices, close = _history()
    policy = ExecutionAlignedAggressivePolicy(products=tuple(close.columns))
    legacy = policy.weight_history(open_prices, close)
    zero_cost, audit = policy.weight_history_with_audit(
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
    open_prices, close = _history()
    policy = ExecutionAlignedAggressivePolicy(products=tuple(close.columns))
    costed, audit = policy.weight_history_with_audit(
        open_prices,
        close,
        transition_cost_bps=15.0,
    )
    assert bool((audit["switched"] == False).any())  # noqa: E712
    rejected = audit[(audit["candidate_templates"] != audit["incumbent_templates"]) & (~audit["switched"])]
    assert not rejected.empty
    assert bool((rejected["net_switch_benefit"] <= 0.0 + 1e-15).all())

    changed_open = open_prices.copy()
    changed_close = close.copy()
    changed_open.iloc[-1] *= [2.0, 0.5, 1.8, 0.6]
    changed_close.iloc[-1] *= [3.0, 0.4, 2.5, 0.5]
    changed, _ = policy.weight_history_with_audit(
        changed_open,
        changed_close,
        transition_cost_bps=15.0,
    )
    pd.testing.assert_series_equal(costed.iloc[-1], changed.iloc[-1])
