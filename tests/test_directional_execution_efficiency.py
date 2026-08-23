import pandas as pd
import pytest

from afuture.directional import adaptive_margin_sizing_share
from afuture.directional_acceptance import (
    DirectionalProductionAcceptance,
    ProductionMechanicsConfig,
)
from afuture.directional_efficiency import (
    attribute_rebalance_deltas,
    stabilize_one_lot_increases,
)


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


def test_production_daily_turnover_buckets_sum_to_total_without_changing_equity():
    raw = pd.DataFrame([
        {"date":"2026-08-20","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":99,"close":99,"volume":5000,"hold":30000},
        {"date":"2026-08-21","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":110,"volume":5000,"hold":30000},
        {"date":"2026-08-24","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":111,"close":112,"volume":5000,"hold":30000},
    ])
    weights = pd.DataFrame(
        {"A":[1.0,0.0]},
        index=pd.to_datetime(["2026-08-21","2026-08-24"]),
    )
    sim = DirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=100000,
            max_contract_volume=100,
            max_daily_loss_ratio=.5,
            max_total_drawdown_ratio=.8,
            max_margin_ratio=.9,
            min_available_ratio=0,
        )
    )
    result = sim.simulate(raw, weights, cost_bps=5)
    expected_columns = {
        "turnover_roll",
        "turnover_resize",
        "turnover_reversal",
        "turnover_entry_exit",
        "turnover_daily_circuit",
        "turnover_hard_halt",
        "turnover_gross_guard",
    }
    assert expected_columns <= set(result.daily.columns)
    for _, row in result.daily.iterrows():
        attributed = sum(float(row[column]) for column in expected_columns)
        assert attributed == pytest.approx(float(row["turnover_notional"]))
    assert abs(result.final_equity - 110894.5) < 1e-9


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
    common = dict(
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
        max_daily_loss_ratio=0.05,
    )
    assert adaptive_margin_sizing_share(completed_returns=(), **common) == pytest.approx(0.30)
    calm = adaptive_margin_sizing_share(completed_returns=(0.002, 0.003), **common)
    stressed = adaptive_margin_sizing_share(completed_returns=(-0.04, 0.01), **common)
    assert calm == pytest.approx(0.30)
    assert 0.0 < stressed < calm
    assert calm <= 0.35
    assert stressed <= 0.35
