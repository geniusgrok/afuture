import pandas as pd


def _raw_two_days():
    return pd.DataFrame(
        [
            {
                "date": "2026-01-05",
                "delivery": "2026-09-15",
                "product": "AG",
                "symbol": "AG2609",
                "open": 100.0,
                "close": 101.0,
                "volume": 10000.0,
                "hold": 20000.0,
            },
            {
                "date": "2026-01-06",
                "delivery": "2026-09-15",
                "product": "AG",
                "symbol": "AG2609",
                "open": 101.0,
                "close": 102.0,
                "volume": 11000.0,
                "hold": 21000.0,
            },
        ]
    )


def test_acceptance_exposes_behavior_neutral_simulation_day_hook():
    from afuture.directional_acceptance import (
        DirectionalProductionAcceptance,
        ProductionMechanicsConfig,
    )

    class Recorder(DirectionalProductionAcceptance):
        def __init__(self):
            super().__init__(ProductionMechanicsConfig())
            self.days = []

        def _on_simulation_day(self, day):
            self.days.append(pd.Timestamp(day).normalize())

    simulator = Recorder()
    index = pd.to_datetime(["2026-01-05", "2026-01-06"])
    weights = pd.DataFrame({"AG": [0.0, 0.0]}, index=index)

    simulator.simulate(_raw_two_days(), weights, cost_bps=15.0)

    assert simulator.days == list(index)


def test_historical_margin_uses_latest_strictly_previous_record_not_same_day():
    from afuture.directional_acceptance import ProductionMechanicsConfig
    from afuture.directional_historical_margin import (
        HistoricalMarginAwareDirectionalProductionAcceptance,
    )

    schedule = pd.DataFrame(
        [
            {"date": "2026-01-05", "symbol": "AG2609", "conservative_margin_ratio": 0.10},
            {"date": "2026-01-06", "symbol": "AG2609", "conservative_margin_ratio": 0.05},
        ]
    )
    simulator = HistoricalMarginAwareDirectionalProductionAcceptance(
        ProductionMechanicsConfig(margin_rate_proxy=0.15),
        margin_history=schedule,
    )
    simulator._on_simulation_day(pd.Timestamp("2026-01-06"))

    assert abs(simulator.margin_rate("AG2609") - 0.10) < 1e-12


def test_missing_previous_margin_falls_back_to_scenario_proxy():
    from afuture.directional_acceptance import ProductionMechanicsConfig
    from afuture.directional_historical_margin import (
        HistoricalMarginAwareDirectionalProductionAcceptance,
    )

    schedule = pd.DataFrame(
        [{"date": "2026-01-06", "symbol": "AG2609", "conservative_margin_ratio": 0.05}]
    )
    simulator = HistoricalMarginAwareDirectionalProductionAcceptance(
        ProductionMechanicsConfig(margin_rate_proxy=0.15),
        margin_history=schedule,
    )
    simulator._on_simulation_day(pd.Timestamp("2026-01-06"))

    assert abs(simulator.margin_rate("AG2609") - 0.15) < 1e-12
    assert abs(simulator.margin_rate("JM2609") - 0.15) < 1e-12


def test_per_lot_margin_keeps_existing_125_buffer_and_hard_config():
    from afuture.directional_acceptance import ProductionMechanicsConfig
    from afuture.directional_historical_margin import (
        HistoricalMarginAwareDirectionalProductionAcceptance,
    )

    schedule = pd.DataFrame(
        [{"date": "2026-01-05", "symbol": "AG2609", "conservative_margin_ratio": 0.10}]
    )
    config = ProductionMechanicsConfig(
        margin_rate_proxy=0.15,
        margin_estimate_buffer=1.25,
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
    )
    simulator = HistoricalMarginAwareDirectionalProductionAcceptance(
        config,
        margin_history=schedule,
    )
    simulator._on_simulation_day(pd.Timestamp("2026-01-06"))

    # AG multiplier is 15; 100 open * 15 notional * 10% exchange margin * 1.25 buffer.
    assert abs(simulator.per_lot_margin("AG2609", 100.0) - 187.5) < 1e-12
    assert simulator.config.max_margin_ratio == 0.35
    assert simulator.config.min_available_ratio == 0.25


def test_live_runtime_factory_does_not_import_historical_margin_research():
    from pathlib import Path

    for path in (
        Path("afuture/runtime_factory.py"),
        Path("afuture/execution_aligned_runtime.py"),
        Path("afuture/directional_runtime.py"),
    ):
        if path.exists():
            assert "directional_historical_margin" not in path.read_text(encoding="utf-8")
