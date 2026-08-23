import pandas as pd
from afuture.directional_acceptance import DirectionalProductionAcceptance, ProductionMechanicsConfig


def test_simulation_orders_gap_rebalance_intraday_and_cost():
    raw = pd.DataFrame([
        {"date":"2026-08-20","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":99,"close":99,"volume":5000,"hold":30000},
        {"date":"2026-08-21","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":110,"volume":5000,"hold":30000},
        {"date":"2026-08-24","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":111,"close":112,"volume":5000,"hold":30000},
    ])
    weights = pd.DataFrame({"A":[1.0,0.0]}, index=pd.to_datetime(["2026-08-21","2026-08-24"]))
    sim = DirectionalProductionAcceptance(ProductionMechanicsConfig(initial_capital=100000,max_contract_volume=100,max_daily_loss_ratio=.5,max_total_drawdown_ratio=.8,max_margin_ratio=.9,min_available_ratio=0))
    result = sim.simulate(raw, weights, cost_bps=5)
    assert abs(result.daily.loc[pd.Timestamp("2026-08-21"),"equity"] - 109950) < 1e-9
    assert abs(result.final_equity - 110894.5) < 1e-9


def test_simulation_uses_same_five_percent_production_gross_headroom_as_runtime():
    raw = pd.DataFrame([
        {"date":"2026-08-20","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":1000,"close":1000,"volume":5000,"hold":30000},
        {"date":"2026-08-21","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":1000,"close":1000,"volume":5000,"hold":30000},
    ])
    weights = pd.DataFrame({"A":[2.0]}, index=pd.to_datetime(["2026-08-21"]))
    sim = DirectionalProductionAcceptance(ProductionMechanicsConfig(initial_capital=100000,max_contract_volume=100,max_daily_loss_ratio=.5,max_total_drawdown_ratio=.8,max_margin_ratio=.9,min_available_ratio=0))
    day = sim.simulate(raw, weights, cost_bps=0).daily.loc[pd.Timestamp("2026-08-21")]
    assert day["risk_scale"] == 0.95
    assert day["gross_notional"] == 190000.0


def test_daily_loss_circuit_recovers_on_next_trading_day_with_defensive_scale():
    raw = pd.DataFrame([
        {"date":"2026-08-20","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":100,"volume":5000,"hold":30000},
        {"date":"2026-08-21","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":90,"volume":5000,"hold":30000},
        {"date":"2026-08-24","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":90,"close":120,"volume":5000,"hold":30000},
    ])
    weights = pd.DataFrame({"A":[1.0,1.0]}, index=pd.to_datetime(["2026-08-21","2026-08-24"]))
    sim = DirectionalProductionAcceptance(ProductionMechanicsConfig(initial_capital=100000,max_contract_volume=100,max_daily_loss_ratio=.05,max_total_drawdown_ratio=.3,max_margin_ratio=.9,min_available_ratio=0))
    result = sim.simulate(raw, weights, cost_bps=0)
    first = result.daily.loc[pd.Timestamp("2026-08-21")]
    recovered = result.daily.loc[pd.Timestamp("2026-08-24")]
    assert first["risk_reason"] == "daily loss limit reached"
    assert first["daily_circuit"]
    assert not first["halted"]
    assert recovered["gross_notional"] > 0
    assert recovered["risk_scale"] == 0.2375
    assert not recovered["halted"]
    assert result.first_divergence == "daily loss limit reached"


def test_total_drawdown_breach_remains_permanent_halt():
    raw = pd.DataFrame([
        {"date":"2026-08-20","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":100,"volume":5000,"hold":30000},
        {"date":"2026-08-21","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":90,"volume":5000,"hold":30000},
        {"date":"2026-08-24","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":90,"close":120,"volume":5000,"hold":30000},
    ])
    weights = pd.DataFrame({"A":[1.0,1.0]}, index=pd.to_datetime(["2026-08-21","2026-08-24"]))
    sim = DirectionalProductionAcceptance(ProductionMechanicsConfig(initial_capital=100000,max_contract_volume=100,max_daily_loss_ratio=.5,max_total_drawdown_ratio=.05,max_margin_ratio=.9,min_available_ratio=0))
    result = sim.simulate(raw, weights, cost_bps=0)
    assert result.daily.loc[pd.Timestamp("2026-08-21"),"risk_reason"] == "drawdown limit reached"
    assert result.daily.loc[pd.Timestamp("2026-08-21"),"halted"]
    assert result.daily.loc[pd.Timestamp("2026-08-24"),"gross_notional"] == 0
    assert result.daily.loc[pd.Timestamp("2026-08-24"),"halted"]


def test_total_drawdown_outranks_daily_loss_when_both_are_breached():
    raw = pd.DataFrame([
        {"date":"2026-08-20","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":100,"volume":5000,"hold":30000},
        {"date":"2026-08-21","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":60,"volume":5000,"hold":30000},
        {"date":"2026-08-24","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":60,"close":120,"volume":5000,"hold":30000},
    ])
    weights = pd.DataFrame({"A":[1.0,1.0]}, index=pd.to_datetime(["2026-08-21","2026-08-24"]))
    sim = DirectionalProductionAcceptance(ProductionMechanicsConfig(initial_capital=100000,max_contract_volume=100,max_daily_loss_ratio=.05,max_total_drawdown_ratio=.30,max_margin_ratio=.9,min_available_ratio=0))
    result = sim.simulate(raw, weights, cost_bps=0)
    first = result.daily.loc[pd.Timestamp("2026-08-21")]
    assert first["risk_reason"] == "drawdown limit reached"
    assert first["halted"]
    assert not first["daily_circuit"]
    assert result.daily.loc[pd.Timestamp("2026-08-24"),"gross_notional"] == 0


def test_overnight_gap_loss_counts_against_day_start_loss_gate():
    raw = pd.DataFrame([
        {"date":"2026-08-20","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":100,"volume":5000,"hold":30000},
        {"date":"2026-08-21","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":100,"volume":5000,"hold":30000},
        {"date":"2026-08-24","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":94,"close":94,"volume":5000,"hold":30000},
    ])
    weights = pd.DataFrame({"A":[1.0,1.0]}, index=pd.to_datetime(["2026-08-21","2026-08-24"]))
    sim = DirectionalProductionAcceptance(ProductionMechanicsConfig(initial_capital=100000,max_contract_volume=100,max_daily_loss_ratio=.05,max_total_drawdown_ratio=.30,max_margin_ratio=.90,min_available_ratio=0))
    result = sim.simulate(raw, weights, cost_bps=0)
    day = result.daily.loc[pd.Timestamp("2026-08-24")]
    assert day["risk_reason"] == "daily loss limit reached"
    assert day["daily_circuit"]
    assert not day["halted"]
    assert day["gross_notional"] == 0


def test_unavailable_nonzero_target_freezes_existing_product_lots():
    raw = pd.DataFrame([
        {"date":"2026-08-20","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":100,"volume":5000,"hold":30000},
        # D activity drops below eligibility, but the existing D+1 contract still has prices.
        {"date":"2026-08-21","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":100,"volume":10,"hold":1000},
        {"date":"2026-08-24","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":101,"volume":5000,"hold":30000},
    ])
    weights = pd.DataFrame({"A":[1.0,1.0]}, index=pd.to_datetime(["2026-08-21","2026-08-24"]))
    sim = DirectionalProductionAcceptance(ProductionMechanicsConfig(initial_capital=100000,max_daily_loss_ratio=.50,max_total_drawdown_ratio=.80,max_margin_ratio=.90,min_available_ratio=0))
    result = sim.simulate(raw, weights, cost_bps=0)
    day = result.daily.loc[pd.Timestamp("2026-08-24")]
    assert day["turnover_notional"] == 0
    assert day["gross_notional"] > 0
    assert not day["halted"]
