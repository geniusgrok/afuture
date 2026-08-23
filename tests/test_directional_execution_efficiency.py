import numpy as np
import pandas as pd
import pytest

from afuture.directional import adaptive_margin_sizing_share
from afuture.directional_acceptance import (
    DirectionalProductionAcceptance,
    ProductionMechanicsConfig,
)
from afuture.directional_efficiency import (
    attribute_rebalance_deltas,
    audit_policy_weight_history,
    should_switch_meta,
    stabilize_one_lot_increases,
    stabilize_same_direction_weights,
)
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


def test_policy_weight_history_audit_preserves_legacy_reference_for_efficiency_comparison():
    open_prices, close = _history()
    policy = ExecutionAlignedAggressivePolicy(products=tuple(close.columns))
    candidate = policy.weight_history(open_prices, close)
    legacy, audit = audit_policy_weight_history(policy, open_prices, close)

    assert float(candidate.abs().sum(axis=1).max()) <= 2.0 + 1e-12
    assert float(legacy.abs().sum(axis=1).max()) <= 2.0 + 1e-12
    assert list(audit.index) == list(legacy.index)
    assert {"signal_turnover", "meta_switch", "selected_templates"} <= set(audit.columns)
    assert (audit["signal_turnover"] >= 0.0).all()
    assert audit["meta_switch"].isin([False, True]).all()
    assert audit["selected_templates"].map(lambda value: isinstance(value, tuple)).all()
    candidate_turnover = float(candidate.diff().abs().sum(axis=1).sum())
    legacy_turnover = float(legacy.diff().abs().sum(axis=1).sum())
    assert candidate_turnover <= legacy_turnover + 1e-12


def test_production_daily_turnover_buckets_sum_to_total_without_changing_equity():
    raw = pd.DataFrame([
        {"date":"2026-08-20","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":99,"close":99,"volume":5000,"hold":30000},
        {"date":"2026-08-21","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":110,"volume":5000,"hold":30000},
        {"date":"2026-08-24","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":111,"close":112,"volume":5000,"hold":30000},
    ])
    weights = pd.DataFrame({"A":[1.0,0.0]}, index=pd.to_datetime(["2026-08-21","2026-08-24"]))
    sim = DirectionalProductionAcceptance(ProductionMechanicsConfig(initial_capital=100000,max_contract_volume=100,max_daily_loss_ratio=.5,max_total_drawdown_ratio=.8,max_margin_ratio=.9,min_available_ratio=0))
    result = sim.simulate(raw, weights, cost_bps=5)
    expected_columns = {
        "turnover_roll", "turnover_resize", "turnover_reversal",
        "turnover_entry_exit", "turnover_daily_circuit", "turnover_hard_halt",
        "turnover_gross_guard",
    }
    assert expected_columns <= set(result.daily.columns)
    for _, row in result.daily.iterrows():
        attributed = sum(float(row[column]) for column in expected_columns)
        assert attributed == pytest.approx(float(row["turnover_notional"]))
    assert abs(result.final_equity - 110894.5) < 1e-9


def test_meta_switch_requires_completed_edge_to_pay_modeled_switch_cost():
    incumbent = {"A": 1.0, "M": -1.0}
    candidate = {"A": -1.0, "M": 1.0}
    assert not should_switch_meta(
        incumbent_mean_return=0.0010,
        candidate_mean_return=0.0015,
        incumbent_weights=incumbent,
        candidate_weights=candidate,
        horizon=3,
        cost_bps=15.0,
        incumbent_survives=True,
    )
    assert should_switch_meta(
        incumbent_mean_return=0.0010,
        candidate_mean_return=0.0040,
        incumbent_weights=incumbent,
        candidate_weights=candidate,
        horizon=3,
        cost_bps=15.0,
        incumbent_survives=True,
    )
    assert should_switch_meta(
        incumbent_mean_return=0.0010,
        candidate_mean_return=0.0010,
        incumbent_weights=incumbent,
        candidate_weights=candidate,
        horizon=3,
        cost_bps=15.0,
        incumbent_survives=False,
    )


def test_product_hysteresis_only_suppresses_same_direction_low_value_resize():
    previous = {"A": 0.60, "M": -0.60, "RB": 0.40, "CU": 0.0}
    candidate = {"A": 0.70, "M": -0.50, "RB": -0.40, "CU": 0.30}
    stabilized = stabilize_same_direction_weights(
        previous,
        candidate,
        trailing_mean_returns={"A": 0.0001, "M": 0.0001, "RB": 0.0001, "CU": 0.0001},
        horizon=3,
        cost_bps=15.0,
    )
    assert stabilized["A"] == 0.60
    assert stabilized["M"] == -0.50
    assert stabilized["RB"] == -0.40
    assert stabilized["CU"] == 0.30


def test_product_hysteresis_allows_same_direction_resize_when_edge_pays_cost():
    stabilized = stabilize_same_direction_weights(
        {"A": 0.60},
        {"A": 0.80},
        trailing_mean_returns={"A": 0.01},
        horizon=3,
        cost_bps=15.0,
    )
    assert stabilized == {"A": 0.80}


def test_one_lot_increase_is_held_only_when_existing_risk_stays_inside_soft_limits():
    assert stabilize_one_lot_increases(
        current_lots={"A2609": 10},
        target_lots={"A2609": 11},
        lot_notionals={"A2609": 1000.0},
        per_lot_margin={"A2609": 150.0},
        equity=10000.0,
        soft_margin_share=0.30,
        max_gross_ratio=2.0,
    ) == {"A2609": 10}
    assert stabilize_one_lot_increases(
        current_lots={"A2609": 10},
        target_lots={"A2609": 9},
        lot_notionals={"A2609": 1000.0},
        per_lot_margin={"A2609": 150.0},
        equity=10000.0,
        soft_margin_share=0.30,
        max_gross_ratio=2.0,
    ) == {"A2609": 9}


def test_adaptive_margin_share_uses_completed_risk_and_never_relaxes_hard_gate():
    common = dict(max_margin_ratio=0.35, min_available_ratio=0.25, max_daily_loss_ratio=0.05)
    assert adaptive_margin_sizing_share(completed_returns=(), **common) == pytest.approx(0.30)
    calm = adaptive_margin_sizing_share(completed_returns=(0.002, 0.003), **common)
    stressed = adaptive_margin_sizing_share(completed_returns=(-0.04, 0.01), **common)
    assert 0.30 < calm < 0.35
    assert 0.30 <= stressed < calm
    assert calm <= 0.35
    assert stressed <= 0.35
