from datetime import datetime, timezone
from types import SimpleNamespace

from afuture.auto_acceptance import AutoPortfolioAcceptanceGate
from afuture.broker.sim import SimBroker
from afuture.models import ContractInfo, PairConfig, Tick
from afuture.strategy import CalendarSpreadStrategy


def _tick(symbol: str, trading_day: str) -> Tick:
    return Tick(
        symbol=symbol,
        exchange="DCE",
        timestamp=datetime.strptime(trading_day, "%Y%m%d").replace(hour=1, tzinfo=timezone.utc),
        bid_price=99.0,
        ask_price=101.0,
        last_price=100.0,
        bid_volume=10,
        ask_volume=10,
        trading_day=trading_day,
    )


def test_historical_sim_catalog_hides_contracts_before_listing_date():
    catalog = [
        ContractInfo("m2609", "DCE", "m", "2026-09-15", "2026-01-01"),
        ContractInfo("m2701", "DCE", "m", "2027-01-15", "2026-06-01"),
        ContractInfo("m2705", "DCE", "m", "2027-05-15"),
    ]
    broker = SimBroker(500000, {}, contract_catalog=catalog)

    broker.publish_tick(_tick("marker", "20260531"))
    assert [row.symbol for row in broker.get_contract_catalog()] == [
        "m2609",
        "m2705",
    ]

    broker.publish_tick(_tick("marker", "20260601"))
    assert [row.symbol for row in broker.get_contract_catalog()] == [
        "m2609",
        "m2701",
        "m2705",
    ]


def _acceptance_result(*, fold_positions: int = 0, stress_positions: int = 0):
    fold = SimpleNamespace(
        oos_metrics={
            "total_return": 0.02,
            "max_drawdown": -0.01,
            "trade_count": 4,
            "final_position_count": fold_positions,
            "halted": False,
        }
    )
    return SimpleNamespace(
        folds=[fold],
        stress_results={
            1.0: {
                "total_return": 0.01,
                "final_position_count": stress_positions,
                "halted": False,
            },
            2.0: {
                "total_return": 0.005,
                "final_position_count": 0,
                "halted": False,
            },
        },
        robustness={"leave_one_product_out": {}, "single_product": {}},
    )


def test_acceptance_rejects_oos_residual_positions():
    decision = AutoPortfolioAcceptanceGate().evaluate(_acceptance_result(fold_positions=1))
    assert not decision.accepted
    assert "OOS folds ended with residual positions" in decision.reasons
    assert decision.metrics["residual_oos_folds"] == 1


def test_acceptance_rejects_stress_residual_positions():
    decision = AutoPortfolioAcceptanceGate().evaluate(_acceptance_result(stress_positions=1))
    assert not decision.accepted
    assert "cost stress ended with residual positions" in decision.reasons
    assert decision.metrics["residual_cost_stress_cases"] == 1


def test_rejected_daily_signal_restores_position_but_keeps_consumed_sample():
    pair = PairConfig(
        "p",
        "N",
        "F",
        "DCE",
        1,
        lookback=3,
        entry_z=2.5,
        exit_z=0.75,
        stop_z=4.0,
        signal_transform="log_ratio",
        confirm_entry=True,
        confirmation_retrace_z=0.3,
        min_confirmed_entry_z=1.75,
        daily_sample_window="14:55-15:00",
    )
    strategy = CalendarSpreadStrategy(pair)
    previous = {
        "history": [1.0, 1.1, 1.2],
        "raw_history": [10.0, 11.0, 12.0],
        "z_history": [0.0, 1.0],
        "position": 0,
        "entry_mean": 0.0,
        "entry_std": 0.0,
        "holding_samples": 0,
        "last_sample_ts": "2026-08-20T06:59:00+00:00",
        "last_sample_trading_day": "20260820",
        "armed_direction": -1,
        "armed_extreme": 2.8,
    }
    strategy.restore_state(previous)
    strategy.restore_state(
        {
            **previous,
            "history": [1.1, 1.2, 1.25],
            "raw_history": [11.0, 12.0, 12.5],
            "z_history": [0.0, 1.0, 2.1],
            "position": -1,
            "entry_mean": 12.0,
            "entry_std": 0.5,
            "last_sample_ts": "2026-08-21T06:59:00+00:00",
            "last_sample_trading_day": "20260821",
            "armed_direction": 0,
            "armed_extreme": 0.0,
        }
    )

    strategy.restore_after_rejected_signal(previous)
    restored = strategy.snapshot_state()
    assert restored["position"] == 0
    assert restored["entry_mean"] == 0.0
    assert restored["history"] == [1.1, 1.2, 1.25]
    assert restored["raw_history"] == [11.0, 12.0, 12.5]
    assert restored["z_history"][-1] == 2.1
    assert restored["last_sample_trading_day"] == "20260821"
    assert restored["armed_direction"] == 0
