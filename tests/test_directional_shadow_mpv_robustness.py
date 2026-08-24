import pandas as pd


def test_shadow_adapter_never_accumulates_candidate_owned_audit_rows():
    from afuture.directional_shadow_mpv_robustness import ShadowMPVDirectionalProductionAcceptance

    simulator = ShadowMPVDirectionalProductionAcceptance(
        shadow_events=pd.DataFrame(
            [{"date": "2026-01-05", "kind": "pnl", "action": "intraday", "product": "AG", "symbol": "AG2606", "lots_before": 1, "gross_pnl": 100.0, "turnover_notional": 0.0, "transaction_cost": 0.0}]
        )
    )

    assert not hasattr(simulator, "_mpv_observed_event_rows")
    assert len(simulator.shadow_events) == 1


def test_shadow_adapter_records_current_decision_day_via_behavior_neutral_hook():
    from afuture.directional_shadow_mpv_robustness import ShadowMPVDirectionalProductionAcceptance

    simulator = ShadowMPVDirectionalProductionAcceptance(shadow_events=pd.DataFrame())
    assert simulator.active_decision_day is None
    simulator._on_simulation_day(pd.Timestamp("2026-01-06"))
    assert simulator.active_decision_day == pd.Timestamp("2026-01-06")


def test_shadow_allocator_objective_is_locked_to_15bp_in_every_evaluation_scenario():
    from afuture.directional_shadow_mpv_robustness import (
        SHADOW_OBJECTIVE_COST_BPS,
        ShadowMPVDirectionalProductionAcceptance,
    )

    assert SHADOW_OBJECTIVE_COST_BPS == 15.0
    simulator = ShadowMPVDirectionalProductionAcceptance(shadow_events=pd.DataFrame())
    assert abs(simulator.shadow_objective_cost_rate - 0.0015) < 1e-12

    # Evaluation cost changes account PnL only; it must not reveal Base/Stress scenario
    # to the allocator's decision hurdle.
    simulator.simulate(pd.DataFrame(), pd.DataFrame(), cost_bps=5.0)
    assert abs(simulator.shadow_objective_cost_rate - 0.0015) < 1e-12
    simulator.simulate(pd.DataFrame(), pd.DataFrame(), cost_bps=15.0)
    assert abs(simulator.shadow_objective_cost_rate - 0.0015) < 1e-12


def test_target_stage_exactly_falls_back_before_decision_day_or_without_shadow_support():
    from afuture.directional_shadow_mpv_robustness import ShadowMPVDirectionalProductionAcceptance

    simulator = ShadowMPVDirectionalProductionAcceptance(shadow_events=pd.DataFrame())
    kwargs = dict(
        equity=100000.0,
        product_weights={"AG": 1.0},
        product_open_prices={"AG": 100.0},
        selected_symbols={"AG": "AG2606"},
        current_lots={},
        completed_returns=(),
    )
    baseline = simulator.target_lot_stages(**kwargs)
    simulator._on_simulation_day(pd.Timestamp("2026-01-06"))
    with_day = simulator.target_lot_stages(**kwargs)

    assert with_day.final_lots == baseline.final_lots
    assert simulator.last_shadow_optimization is not None
    assert simulator.last_shadow_optimization.optimization.fallback_reason == "insufficient causal MPV evidence"


def test_live_runtime_does_not_import_shadow_mpv_research_adapter():
    from pathlib import Path

    for path in (
        Path("afuture/runtime_factory.py"),
        Path("afuture/execution_aligned_runtime.py"),
        Path("afuture/directional_runtime.py"),
    ):
        if path.exists():
            assert "directional_shadow_mpv_robustness" not in path.read_text(encoding="utf-8")
