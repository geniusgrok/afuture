import pandas as pd


def _minimal_contracts():
    return pd.DataFrame(
        [
            {"date": "2026-01-05", "delivery": "2026-09-15", "product": "AG", "symbol": "AG2609", "open": 100.0, "close": 101.0, "volume": 10000.0, "hold": 20000.0},
            {"date": "2026-01-06", "delivery": "2026-09-15", "product": "AG", "symbol": "AG2609", "open": 101.0, "close": 102.0, "volume": 11000.0, "hold": 21000.0},
        ]
    )


def test_shadow_adapter_never_accumulates_candidate_owned_audit_rows():
    from afuture.directional_shadow_mpv_robustness import ShadowMPVDirectionalProductionAcceptance

    simulator = ShadowMPVDirectionalProductionAcceptance(
        shadow_outcomes=pd.DataFrame(
            [{"date": "2026-01-05", "product": "AG", "gross_return": 0.01, "baseline_abs_weight": 1.0}]
        )
    )

    assert not hasattr(simulator, "_mpv_observed_event_rows")
    assert not hasattr(simulator, "shadow_events")
    assert len(simulator.shadow_outcomes) == 1


def test_shadow_adapter_records_current_decision_day_via_behavior_neutral_hook():
    from afuture.directional_shadow_mpv_robustness import ShadowMPVDirectionalProductionAcceptance

    simulator = ShadowMPVDirectionalProductionAcceptance(shadow_outcomes=pd.DataFrame())
    assert simulator.active_decision_day is None
    simulator._on_simulation_day(pd.Timestamp("2026-01-06"))
    assert simulator.active_decision_day == pd.Timestamp("2026-01-06")


def test_shadow_allocator_objective_is_locked_to_15bp_in_every_evaluation_scenario():
    from afuture.directional_shadow_mpv_robustness import (
        SHADOW_OBJECTIVE_COST_BPS,
        ShadowMPVDirectionalProductionAcceptance,
    )

    assert SHADOW_OBJECTIVE_COST_BPS == 15.0
    simulator = ShadowMPVDirectionalProductionAcceptance(shadow_outcomes=pd.DataFrame())
    assert abs(simulator.shadow_objective_cost_rate - 0.0015) < 1e-12

    dates = pd.to_datetime(["2026-01-05", "2026-01-06"])
    zero_weights = pd.DataFrame({"AG": [0.0, 0.0]}, index=dates)
    simulator.simulate(_minimal_contracts(), zero_weights, cost_bps=5.0)
    assert abs(simulator.shadow_objective_cost_rate - 0.0015) < 1e-12
    simulator.simulate(_minimal_contracts(), zero_weights, cost_bps=15.0)
    assert abs(simulator.shadow_objective_cost_rate - 0.0015) < 1e-12


def test_target_stage_exactly_falls_back_before_decision_day_or_without_shadow_support():
    from afuture.directional_shadow_mpv_robustness import ShadowMPVDirectionalProductionAcceptance

    simulator = ShadowMPVDirectionalProductionAcceptance(shadow_outcomes=pd.DataFrame())
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
    assert simulator.last_shadow_optimization.optimization.fallback_reason == "insufficient shadow outcome evidence"


def test_adapter_passes_same_day_precomputed_causal_horizons_to_shadow_objective():
    from afuture.directional_shadow_mpv_robustness import ShadowMPVDirectionalProductionAcceptance

    outcomes = pd.DataFrame(
        [
            {"date": "2026-01-05", "product": "AG", "gross_return": 0.006, "baseline_abs_weight": 1.0},
            {"date": "2026-01-05", "product": "CU", "gross_return": 0.010, "baseline_abs_weight": 1.0},
        ]
    )
    horizons = pd.DataFrame(
        {"AG": [3.0], "CU": [1.0]},
        index=pd.to_datetime(["2026-01-06"]),
    )
    simulator = ShadowMPVDirectionalProductionAcceptance(
        shadow_outcomes=outcomes,
        remaining_horizon_panel=horizons,
    )
    simulator._on_simulation_day(pd.Timestamp("2026-01-06"))
    simulator.target_lot_stages(
        equity=100000.0,
        product_weights={"AG": 1.0, "CU": 1.0},
        product_open_prices={"AG": 100.0, "CU": 100.0},
        selected_symbols={"AG": "AG2606", "CU": "CU2606"},
        current_lots={},
        completed_returns=(),
    )

    assert simulator.last_shadow_optimization is not None
    assert simulator.last_shadow_optimization.product_expected_horizons == {
        "AG": 3.0,
        "CU": 1.0,
    }


def test_live_runtime_does_not_import_shadow_mpv_research_adapter():
    from pathlib import Path

    for path in (
        Path("afuture/runtime_factory.py"),
        Path("afuture/execution_aligned_runtime.py"),
        Path("afuture/directional_runtime.py"),
    ):
        if path.exists():
            text = path.read_text(encoding="utf-8")
            assert "directional_shadow_mpv_robustness" not in text
            assert "directional_shadow_outcomes" not in text
