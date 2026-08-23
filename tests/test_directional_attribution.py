import pandas as pd
import pytest

from afuture.directional_acceptance import ProductionMechanicsConfig
from afuture.directional_attribution import classify_rebalance_action
from afuture.directional_robustness import MarginAwareDirectionalProductionAcceptance


def test_rebalance_action_splits_entry_and_exit_without_losing_existing_classes():
    original = {
        "A2609": 5,
        "M2609": -4,
        "CU2609": 2,
        "AL2609": 3,
    }
    target = {
        "A2701": 5,
        "M2609": -3,
        "CU2609": -2,
        "RB2610": 1,
    }
    products = {
        "A2609": "A",
        "A2701": "A",
        "M2609": "M",
        "CU2609": "CU",
        "AL2609": "AL",
        "RB2610": "RB",
    }

    assert classify_rebalance_action(
        symbol="A2609",
        original_lots=original,
        target_lots=target,
        symbol_products=products,
    ) == "roll"
    assert classify_rebalance_action(
        symbol="A2701",
        original_lots=original,
        target_lots=target,
        symbol_products=products,
    ) == "roll"
    assert classify_rebalance_action(
        symbol="M2609",
        original_lots=original,
        target_lots=target,
        symbol_products=products,
    ) == "resize"
    assert classify_rebalance_action(
        symbol="CU2609",
        original_lots=original,
        target_lots=target,
        symbol_products=products,
    ) == "reversal"
    assert classify_rebalance_action(
        symbol="RB2610",
        original_lots=original,
        target_lots=target,
        symbol_products=products,
    ) == "entry"
    assert classify_rebalance_action(
        symbol="AL2609",
        original_lots=original,
        target_lots=target,
        symbol_products=products,
    ) == "exit"


def test_margin_aware_target_stages_expose_capacity_losses_without_changing_final_lots():
    sim = MarginAwareDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=100000.0,
            margin_rate_proxy=0.15,
            margin_estimate_buffer=1.25,
            max_contract_volume=200,
        )
    )
    kwargs = dict(
        equity=100000.0,
        product_weights={"A": 2.0},
        product_open_prices={"A": 100.0},
        selected_symbols={"A": "A2609"},
        current_lots={},
        completed_returns=(),
    )

    stages = sim.target_lot_stages(**kwargs)

    assert stages.raw_integer_lots == {"A2609": 200}
    assert stages.margin_fitted_lots == {"A2609": 160}
    assert stages.final_lots == {"A2609": 160}
    assert stages.desired_notional == pytest.approx(200000.0)
    assert stages.raw_integer_notional == pytest.approx(200000.0)
    assert stages.margin_fitted_notional == pytest.approx(160000.0)
    assert stages.final_notional == pytest.approx(160000.0)
    assert stages.integer_rounding_loss_notional == pytest.approx(0.0)
    assert stages.max_volume_clipping_notional == pytest.approx(0.0)
    assert stages.unavailable_contract_notional == pytest.approx(0.0)
    assert stages.soft_margin_share == pytest.approx(0.30)
    assert sim.target_lots(**kwargs) == stages.final_lots


def test_target_stages_split_rounding_volume_cap_and_unavailable_capacity():
    sim = MarginAwareDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=100000.0,
            margin_rate_proxy=0.01,
            margin_estimate_buffer=1.0,
            max_contract_volume=35,
            max_margin_ratio=0.9,
            min_available_ratio=0.0,
            max_daily_loss_ratio=0.05,
        )
    )
    stages = sim.target_lot_stages(
        equity=100000.0,
        product_weights={"A": 0.333, "M": 1.0, "RB": 0.25},
        product_open_prices={"A": 101.0, "M": 100.0},
        selected_symbols={"A": "A2609", "M": "M2609"},
        current_lots={},
        completed_returns=(),
    )

    # A: desired 33,300; 32 full lots at 1,010 -> 980 integer remainder.
    # M: desired 100,000; 100 full lots at 1,000, clipped to 35 -> 65,000 cap loss.
    # RB: no selected/priceable contract -> full 25,000 unavailable capacity.
    assert stages.integer_rounding_loss_notional == pytest.approx(980.0)
    assert stages.max_volume_clipping_notional == pytest.approx(65000.0)
    assert stages.unavailable_contract_notional == pytest.approx(25000.0)
    assert stages.raw_integer_lots == {"A2609": 32, "M2609": 35}


def test_simulation_audit_reconciles_gross_pnl_and_transaction_cost_to_equity():
    raw = pd.DataFrame([
        {"date":"2026-08-20","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":99,"close":99,"volume":5000,"hold":30000},
        {"date":"2026-08-21","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":110,"volume":5000,"hold":30000},
        {"date":"2026-08-24","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":111,"close":112,"volume":5000,"hold":30000},
    ])
    weights = pd.DataFrame(
        {"A":[1.0,0.0]},
        index=pd.to_datetime(["2026-08-21","2026-08-24"]),
    )
    sim = MarginAwareDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=100000,
            margin_rate_proxy=0.01,
            margin_estimate_buffer=1.0,
            max_contract_volume=100,
            max_daily_loss_ratio=.5,
            max_total_drawdown_ratio=.8,
            max_margin_ratio=.9,
            min_available_ratio=0,
        )
    )

    result = sim.simulate(raw, weights, cost_bps=5)
    events = result.events

    assert {"entry", "exit"} <= set(events.loc[events["kind"] == "trade", "action"])
    assert events.loc[events["kind"] == "pnl", "gross_pnl"].sum() == pytest.approx(11000.0)
    assert events.loc[events["kind"] == "trade", "transaction_cost"].sum() == pytest.approx(105.5)
    assert result.final_equity == pytest.approx(
        sim.config.initial_capital
        + events["gross_pnl"].sum()
        - events["transaction_cost"].sum()
    )
    assert result.final_equity == pytest.approx(110894.5)

    first = result.daily.iloc[0]
    assert first["raw_target_gross_ratio"] == pytest.approx(1.0)
    assert first["governor_target_gross_ratio"] == pytest.approx(1.0)
    assert first["raw_integer_target_gross_notional"] == pytest.approx(100000.0)
    assert first["margin_fitted_target_gross_notional"] == pytest.approx(100000.0)
    assert first["final_target_gross_notional"] == pytest.approx(100000.0)


def test_production_attribution_summary_reports_pnl_cost_capacity_and_holding_quality():
    from afuture.directional_attribution import summarize_production_attribution

    raw = pd.DataFrame([
        {"date":"2026-08-20","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":99,"close":99,"volume":5000,"hold":30000},
        {"date":"2026-08-21","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":110,"volume":5000,"hold":30000},
        {"date":"2026-08-24","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":111,"close":112,"volume":5000,"hold":30000},
    ])
    weights = pd.DataFrame(
        {"A":[1.0,0.0]},
        index=pd.to_datetime(["2026-08-21","2026-08-24"]),
    )
    sim = MarginAwareDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=100000,
            margin_rate_proxy=0.01,
            margin_estimate_buffer=1.0,
            max_contract_volume=100,
            max_daily_loss_ratio=.5,
            max_total_drawdown_ratio=.8,
            max_margin_ratio=.9,
            min_available_ratio=0,
        )
    )
    result = sim.simulate(raw, weights, cost_bps=5)

    summary = summarize_production_attribution(
        daily=result.daily,
        events=result.events,
        initial_capital=sim.config.initial_capital,
    )

    assert summary["alpha"]["gross_signal_pnl"] == pytest.approx(11000.0)
    assert summary["alpha"]["long_pnl"] == pytest.approx(11000.0)
    assert summary["alpha"]["short_pnl"] == pytest.approx(0.0)
    assert summary["alpha"]["product_pnl"] == {"A": pytest.approx(11000.0)}
    assert summary["transaction_cost"]["total_cost"] == pytest.approx(105.5)
    assert summary["transaction_cost"]["by_action"]["entry"]["turnover_notional"] == pytest.approx(100000.0)
    assert summary["transaction_cost"]["by_action"]["entry"]["cost"] == pytest.approx(50.0)
    assert summary["transaction_cost"]["by_action"]["entry"]["affected_days"] == 1
    assert summary["transaction_cost"]["by_action"]["exit"]["turnover_notional"] == pytest.approx(111000.0)
    assert summary["transaction_cost"]["by_action"]["exit"]["cost"] == pytest.approx(55.5)
    assert summary["transaction_cost"]["annualized_return_drag_proxy"] > 0.0
    assert summary["activity"]["execution_event_count"] == 2
    assert summary["activity"]["average_holding_sessions"] == pytest.approx(2.0)
    assert summary["capacity"]["peak_raw_target_gross_ratio"] == pytest.approx(1.0)
    assert summary["capacity"]["peak_governor_target_gross_ratio"] == pytest.approx(1.0)
    assert summary["capacity"]["integer_rounding_loss_notional"] == pytest.approx(0.0)
    assert summary["capacity"]["margin_capacity_loss_notional"] == pytest.approx(0.0)


def test_audit_records_end_of_day_daily_circuit_cost_exactly_once():
    raw = pd.DataFrame([
        {"date":"2026-08-20","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":100,"volume":5000,"hold":30000},
        {"date":"2026-08-21","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":70,"volume":5000,"hold":30000},
    ])
    weights = pd.DataFrame(
        {"A":[1.0]}, index=pd.to_datetime(["2026-08-21"])
    )
    sim = MarginAwareDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=100000,
            margin_rate_proxy=0.01,
            margin_estimate_buffer=1.0,
            max_contract_volume=100,
            max_daily_loss_ratio=.05,
            max_total_drawdown_ratio=.8,
            max_margin_ratio=.9,
            min_available_ratio=0,
        )
    )

    result = sim.simulate(raw, weights, cost_bps=5)
    trades = result.events[result.events["kind"] == "trade"]

    assert set(trades["action"]) == {"entry", "daily_circuit"}
    assert trades["turnover_notional"].sum() == pytest.approx(
        result.daily["turnover_notional"].sum()
    )
    assert trades["transaction_cost"].sum() == pytest.approx(
        result.daily["turnover_notional"].sum() * 5 / 10000.0
    )
