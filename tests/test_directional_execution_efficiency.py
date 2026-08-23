import numpy as np
import pandas as pd

from afuture.directional_efficiency import attribute_rebalance_deltas
from afuture.execution_aligned_policy import ExecutionAlignedAggressivePolicy


def _history(periods: int = 80):
    dates = pd.date_range("2026-01-01", periods=periods, freq="B")
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


def test_rebalance_turnover_attribution_classifies_and_sums_executed_deltas():
    original = {"A2609": 5, "M2609": -4, "CU2609": 2}
    target = {"A2701": 5, "M2609": -3, "CU2609": -2, "RB2610": 1}
    executed = {
        "A2609": -5,
        "A2701": 5,
        "M2609": 1,
        "CU2609": -4,
        "RB2610": 1,
    }
    lot_notionals = {symbol: 100.0 for symbol in executed}
    products = {
        "A2609": "A",
        "A2701": "A",
        "M2609": "M",
        "CU2609": "CU",
        "RB2610": "RB",
    }

    buckets = attribute_rebalance_deltas(
        original_lots=original,
        target_lots=target,
        executed_deltas=executed,
        lot_notionals=lot_notionals,
        symbol_products=products,
    )

    assert buckets == {
        "roll": 1000.0,
        "resize": 100.0,
        "reversal": 400.0,
        "entry_exit": 100.0,
    }
    assert sum(buckets.values()) == 1600.0


def test_policy_weight_history_audit_is_behavior_neutral():
    open_prices, close = _history()
    policy = ExecutionAlignedAggressivePolicy(products=tuple(close.columns))

    baseline = policy.weight_history(open_prices, close)
    audited, audit = policy.weight_history_with_audit(open_prices, close)

    pd.testing.assert_frame_equal(audited, baseline)
    assert list(audit.index) == list(baseline.index)
    assert {"signal_turnover", "meta_switch", "selected_templates"} <= set(audit.columns)
    assert (audit["signal_turnover"] >= 0.0).all()
    assert audit["meta_switch"].isin([False, True]).all()
