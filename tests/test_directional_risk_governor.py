import afuture.execution_aligned_policy as execution_policy
from afuture.directional_risk import (
    DirectionalRiskGovernor,
    DirectionalRiskScaledPolicy,
)
from afuture.state import RuntimeState, StateStore


def test_directional_risk_governor_uses_only_completed_returns_and_scales_defensively():
    governor = DirectionalRiskGovernor()

    assert governor.scale([]) == 0.95
    assert governor.scale([-0.0299]) == 0.95
    assert governor.scale([-0.03]) == 0.2375
    assert governor.scale([-0.04, 0.01]) == 0.2375


def test_directional_risk_governor_keeps_scale_inside_headroom_and_never_increases_gross():
    governor = DirectionalRiskGovernor()
    for returns in ([], [0.10], [0.10, -0.10], [-0.50, 0.50]):
        scale = governor.scale(returns)
        assert 0.0 < scale <= 0.95


def test_scaled_policy_reserves_five_percent_gross_headroom_and_only_reduces_targets():
    class _Policy:
        def target_weights(self, *args, **kwargs):
            return {"A": 1.2, "CU": -0.8}

    completed = [-0.04, 0.01]
    policy = DirectionalRiskScaledPolicy(
        _Policy(),
        completed_returns_provider=lambda: tuple(completed),
    )
    assert policy.target_weights(object(), object()) == {
        "A": 0.285,
        "CU": -0.19,
    }

    completed[:] = [0.01, 0.01]
    assert policy.target_weights(object(), object()) == {
        "A": 1.14,
        "CU": -0.76,
    }


def test_runtime_state_persists_completed_directional_return_history_and_daily_circuit(tmp_path):
    store = StateStore(tmp_path / "state.json")
    state = RuntimeState(
        recent_daily_returns=[-0.031, 0.012],
        directional_daily_circuit_day="20260825",
        last_account_equity=96900.0,
        last_account_trading_day="20260825",
    )
    store.save(state)

    restored = store.load()
    assert restored.recent_daily_returns == [-0.031, 0.012]
    assert restored.directional_daily_circuit_day == "20260825"
    assert restored.last_account_equity == 96900.0
    assert restored.last_account_trading_day == "20260825"


def test_production_meta_allocator_uses_selected_high_return_shape():
    assert execution_policy.META_LOOKBACK == 11
    assert execution_policy.META_REBALANCE == 3
    assert execution_policy.META_COUNT == 3
    assert execution_policy.META_ANNUALIZED_WEIGHT == 0.25
    assert execution_policy.META_SHARPE_WEIGHT == 1.0
