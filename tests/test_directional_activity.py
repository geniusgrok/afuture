from datetime import date, datetime, timezone

from afuture.directional import DirectionalConfig
from afuture.directional_activity import (
    ContractActivity,
    DirectionalActivitySnapshot,
    DirectionalActivityStore,
    DirectionalActivityTracker,
    select_contracts_from_activity,
)
from afuture.models import ContractInfo, Tick


def _tick(symbol: str, trading_day: str, *, volume: float, oi: float) -> Tick:
    return Tick(
        symbol=symbol,
        exchange="DCE",
        timestamp=datetime(2026, 8, 21, 7, 0, tzinfo=timezone.utc),
        bid_price=99.0,
        ask_price=101.0,
        last_price=100.0,
        bid_volume=1000,
        ask_volume=1000,
        volume=volume,
        open_interest=oi,
        trading_day=trading_day,
        limit_up=120.0,
        limit_down=80.0,
    )


def _catalog():
    return {
        "A2609": ContractInfo("A2609", "DCE", "A", "2026-09-15"),
        "A2611": ContractInfo("A2611", "DCE", "A", "2026-11-15"),
    }


def test_activity_tracker_freezes_previous_trading_day_and_reloads(tmp_path):
    store = DirectionalActivityStore(tmp_path / "directional_activity.json")
    tracker = DirectionalActivityTracker(store)
    catalog = _catalog()
    tracker.observe(_tick("A2609", "20260821", volume=8000, oi=30000), catalog["A2609"])
    tracker.observe(_tick("A2611", "20260821", volume=12000, oi=20000), catalog["A2611"])
    assert tracker.completed_snapshot is None
    tracker.observe(_tick("A2611", "20260825", volume=1, oi=99999), catalog["A2611"])
    snapshot = tracker.completed_snapshot
    assert snapshot is not None
    assert snapshot.trading_day == "20260821"
    assert snapshot.contracts["A2609"].open_interest == 30000
    assert snapshot.contracts["A2611"].volume == 12000
    restored = DirectionalActivityTracker(store)
    assert restored.completed_snapshot == snapshot


def test_previous_completed_activity_controls_contract_selection_not_current_ticks(tmp_path):
    store = DirectionalActivityStore(tmp_path / "directional_activity.json")
    tracker = DirectionalActivityTracker(store)
    catalog = _catalog()
    tracker.observe(_tick("A2609", "20260821", volume=20000, oi=50000), catalog["A2609"])
    tracker.observe(_tick("A2611", "20260821", volume=30000, oi=30000), catalog["A2611"])
    tracker.observe(_tick("A2611", "20260825", volume=500000, oi=999999), catalog["A2611"])
    config = DirectionalConfig(
        enabled=True,
        products=("A",),
        exchanges=("DCE",),
        min_days_to_expiry=20,
        min_volume=1000,
        min_open_interest=5000,
    )
    selected = select_contracts_from_activity(
        config, list(catalog.values()), tracker.completed_snapshot, date(2026, 8, 25)
    )
    assert selected["A"].symbol == "A2609"


def test_contract_selection_rejects_activity_identity_that_conflicts_with_catalog():
    snapshot = _snapshot(old_volume=20_000, old_oi=50_000, new_volume=0, new_oi=0)
    inconsistent = snapshot.contracts["A2609"]
    snapshot.contracts["A2609"] = ContractActivity(
        inconsistent.symbol,
        "SHFE",
        "RB",
        inconsistent.trading_day,
        inconsistent.volume,
        inconsistent.open_interest,
        inconsistent.timestamp,
    )

    selected = select_contracts_from_activity(
        _config(), list(_catalog().values()), snapshot, date(2026, 8, 25)
    )

    assert selected == {}


def _snapshot(*, old_volume: float, old_oi: float, new_volume: float, new_oi: float):
    timestamp = datetime(2026, 8, 21, 7, 0, tzinfo=timezone.utc)
    return DirectionalActivitySnapshot(
        "20260821",
        {
            "A2609": ContractActivity(
                "A2609", "DCE", "A", "20260821", old_volume, old_oi, timestamp
            ),
            "A2611": ContractActivity(
                "A2611", "DCE", "A", "20260821", new_volume, new_oi, timestamp
            ),
        },
    )


def _config():
    return DirectionalConfig(
        enabled=True,
        products=("A",),
        exchanges=("DCE",),
        min_days_to_expiry=20,
        min_volume=1000,
        min_open_interest=5000,
    )


def test_roll_hysteresis_keeps_eligible_incumbent_when_challenger_wins_only_one_liquidity_dimension():
    selected = select_contracts_from_activity(
        _config(),
        list(_catalog().values()),
        _snapshot(old_volume=40000, old_oi=50000, new_volume=30000, new_oi=60000),
        date(2026, 8, 25),
        preferred_symbols={"A": "A2609"},
    )
    assert selected["A"].symbol == "A2609"


def test_roll_hysteresis_rolls_when_challenger_dominates_both_oi_and_volume():
    selected = select_contracts_from_activity(
        _config(),
        list(_catalog().values()),
        _snapshot(old_volume=30000, old_oi=50000, new_volume=40000, new_oi=60000),
        date(2026, 8, 25),
        preferred_symbols={"A": "A2609"},
    )
    assert selected["A"].symbol == "A2611"


def test_roll_hysteresis_cannot_keep_incumbent_inside_delivery_blackout():
    selected = select_contracts_from_activity(
        _config(),
        list(_catalog().values()),
        _snapshot(old_volume=50000, old_oi=60000, new_volume=40000, new_oi=50000),
        date(2026, 9, 1),
        preferred_symbols={"A": "A2609"},
    )
    assert selected["A"].symbol == "A2611"
