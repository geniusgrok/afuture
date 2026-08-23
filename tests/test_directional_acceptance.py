from datetime import datetime, timezone

import pandas as pd

from afuture.directional_acceptance import (
    DirectionalProductionAcceptance,
    ProductionMechanicsConfig,
    PRODUCT_MULTIPLIERS,
)


UTC = timezone.utc


def _acceptance(**overrides):
    config = ProductionMechanicsConfig(**overrides)
    return DirectionalProductionAcceptance(config)


def test_integer_lot_floor_uses_frozen_multiplier_notional_and_contract_cap():
    sim = _acceptance(max_contract_volume=100)
    lots = sim.target_lots(
        equity=500000.0,
        product_weights={"A": 0.5, "NI": -0.8},
        product_open_prices={"A": 1000.0, "NI": 100000.0},
        selected_symbols={"A": "A2609", "NI": "NI2609"},
    )
    # A multiplier=10: floor(500000*0.5/(1000*10)) = 25.
    assert lots["A2609"] == 25
    # NI multiplier=1: floor(500000*0.8/100000) = 4, preserving sign.
    assert lots["NI2609"] == -4

    capped = sim.target_lots(
        equity=500000.0,
        product_weights={"A": 2.0},
        product_open_prices={"A": 100.0},
        selected_symbols={"A": "A2609"},
    )
    assert capped["A2609"] == 100
    assert PRODUCT_MULTIPLIERS["AU"] == 1000.0
    assert PRODUCT_MULTIPLIERS["JM"] == 60.0


def test_roll_is_two_phase_close_old_contract_then_open_new_contract():
    sim = _acceptance()
    first = sim.rebalance_plan(
        current_lots={"A2509": 3}, target_lots={"A2601": 2}
    )
    assert first.reductions == {"A2509": -3}
    assert first.openings == {}

    second = sim.rebalance_plan(current_lots={}, target_lots={"A2601": 2})
    assert second.reductions == {}
    assert second.openings == {"A2601": 2}


def test_margin_proxy_and_cash_reserve_can_reject_an_otherwise_valid_open():
    sim = _acceptance(
        margin_rate_proxy=0.12,
        margin_estimate_buffer=1.25,
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
    )
    allowed, reason, estimated_margin = sim.check_opening_batch(
        equity=100000.0,
        current_margin=0.0,
        current_lots={},
        openings={"AU2609": 1},
        open_prices={"AU2609": 1000.0},
    )
    assert allowed is False
    assert reason == "combined margin ratio would exceed limit"
    assert estimated_margin == 150000.0


def test_account_risk_matches_daily_loss_and_high_watermark_fail_closed_rules():
    sim = _acceptance(max_daily_loss_ratio=0.05, max_total_drawdown_ratio=0.30)
    assert sim.account_risk_reason(
        equity=94999.0, day_start_equity=100000.0, high_watermark=100000.0
    ) == "daily loss limit reached"
    assert sim.account_risk_reason(
        equity=69999.0, day_start_equity=70000.0, high_watermark=100000.0
    ) == "drawdown limit reached"
    assert sim.account_risk_reason(
        equity=95000.0, day_start_equity=100000.0, high_watermark=100000.0
    ) == "daily loss limit reached"


def test_contract_for_day_uses_previous_completed_activity_not_current_day_activity():
    sim = _acceptance(min_days_to_delivery=20)
    raw = pd.DataFrame(
        [
            {
                "date": "2026-08-20",
                "product": "A",
                "exchange": "DCE",
                "symbol": "A2609",
                "delivery": "2026-09-15",
                "open": 100.0,
                "close": 101.0,
                "volume": 2000.0,
                "hold": 30000.0,
            },
            {
                "date": "2026-08-20",
                "product": "A",
                "exchange": "DCE",
                "symbol": "A2701",
                "delivery": "2027-01-15",
                "open": 110.0,
                "close": 111.0,
                "volume": 1800.0,
                "hold": 20000.0,
            },
            # Current-day activity flips strongly to A2701. Production selection for the
            # 21st must remain frozen from the completed 20th snapshot.
            {
                "date": "2026-08-21",
                "product": "A",
                "exchange": "DCE",
                "symbol": "A2609",
                "delivery": "2026-09-15",
                "open": 102.0,
                "close": 103.0,
                "volume": 10.0,
                "hold": 1000.0,
            },
            {
                "date": "2026-08-21",
                "product": "A",
                "exchange": "DCE",
                "symbol": "A2701",
                "delivery": "2027-01-15",
                "open": 112.0,
                "close": 113.0,
                "volume": 9000.0,
                "hold": 90000.0,
            },
        ]
    )
    selected = sim.select_contracts_for_day(raw, pd.Timestamp("2026-08-21"))
    assert selected == {"A": "A2609"}


def test_simulation_normalizes_contract_table_only_once(monkeypatch):
    sim = _acceptance(
        max_contract_volume=100,
        max_daily_loss_ratio=0.50,
        max_total_drawdown_ratio=0.80,
        max_margin_ratio=0.90,
        min_available_ratio=0.0,
    )
    raw = pd.DataFrame(
        [
            {"date":"2026-08-20","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":100,"volume":5000,"hold":30000},
            {"date":"2026-08-21","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":101,"volume":5000,"hold":30000},
            {"date":"2026-08-24","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":101,"close":102,"volume":5000,"hold":30000},
        ]
    )
    weights = pd.DataFrame(
        {"A": [1.0, 1.0]},
        index=pd.to_datetime(["2026-08-21", "2026-08-24"]),
    )
    calls = 0
    original = sim._normalize_contracts

    def counted(frame):
        nonlocal calls
        calls += 1
        return original(frame)

    monkeypatch.setattr(sim, "_normalize_contracts", counted)
    sim.simulate(raw, weights, cost_bps=0)
    assert calls == 1


def test_prepared_contract_context_can_be_reused_without_renormalizing(monkeypatch):
    sim = _acceptance(
        max_contract_volume=100,
        max_daily_loss_ratio=0.50,
        max_total_drawdown_ratio=0.80,
        max_margin_ratio=0.90,
        min_available_ratio=0.0,
    )
    raw = pd.DataFrame(
        [
            {"date":"2026-08-20","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":100,"volume":5000,"hold":30000},
            {"date":"2026-08-21","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":101,"volume":5000,"hold":30000},
            {"date":"2026-08-24","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":101,"close":102,"volume":5000,"hold":30000},
        ]
    )
    weights = pd.DataFrame(
        {"A": [1.0, 1.0]},
        index=pd.to_datetime(["2026-08-21", "2026-08-24"]),
    )
    calls = 0
    original = sim._normalize_contracts

    def counted(frame):
        nonlocal calls
        calls += 1
        return original(frame)

    monkeypatch.setattr(sim, "_normalize_contracts", counted)
    prepared = sim.prepare_contracts(raw)
    first = sim.simulate(raw, weights, cost_bps=0, prepared=prepared)
    second = sim.simulate(raw, weights, cost_bps=0, prepared=prepared)
    assert calls == 1
    pd.testing.assert_frame_equal(first.daily, second.daily)
    assert first.final_equity == second.final_equity
