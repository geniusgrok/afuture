import numpy as np
import pandas as pd

from afuture.directional_efficiency import audit_policy_weight_history
from afuture.execution_aligned_policy import ExecutionAlignedAggressivePolicy


def test_same_direction_candidate_reduces_signal_turnover_vs_legacy_reference():
    periods = 80
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
    policy = ExecutionAlignedAggressivePolicy(products=tuple(close.columns))

    candidate = policy.weight_history(open_prices, close)
    legacy, _ = audit_policy_weight_history(policy, open_prices, close)

    candidate_turnover = float(candidate.diff().abs().sum(axis=1).sum())
    legacy_turnover = float(legacy.diff().abs().sum(axis=1).sum())
    assert candidate_turnover < legacy_turnover - 1e-12
