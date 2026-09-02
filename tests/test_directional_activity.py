import json
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256

import pytest

from afuture.directional import DirectionalConfig
from afuture.directional_activity import (
    ContractActivity,
    DirectionalActivitySnapshot,
    DirectionalActivityStore,
    DirectionalActivityTracker,
    select_contracts_from_activity,
)
from afuture.models import ContractInfo, Tick


def _tick(
    symbol: str,
    trading_day: str,
    *,
    volume: float,
    oi: float,
    timestamp: datetime | None = None,
) -> Tick:
    return Tick(
        symbol=symbol,
        exchange="DCE",
        timestamp=timestamp or datetime(2026, 8, 21, 7, 0, tzinfo=timezone.utc),
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
    tracker.observe(
        _tick(
            "A2611",
            "20260825",
            volume=1,
            oi=99999,
            timestamp=datetime(2026, 8, 25, 7, 0, tzinfo=timezone.utc),
        ),
        catalog["A2611"],
    )
    snapshot = tracker.completed_snapshot
    assert snapshot is not None
    assert snapshot.trading_day == "20260821"
    assert snapshot.contracts["A2609"].open_interest == 30000
    assert snapshot.contracts["A2611"].volume == 12000
    restored = DirectionalActivityTracker(store)
    assert restored.completed_snapshot == snapshot


def test_activity_tracker_restores_in_progress_observations_after_midday_restart(tmp_path):
    store = DirectionalActivityStore(tmp_path / "directional_activity.json")
    catalog = _catalog()
    tracker = DirectionalActivityTracker(store)
    tracker.observe(_tick("A2609", "20260821", volume=8000, oi=30000), catalog["A2609"])
    tracker.observe(_tick("A2611", "20260821", volume=12000, oi=20000), catalog["A2611"])
    tracker.checkpoint()

    restored = DirectionalActivityTracker(store)

    assert restored.current_trading_day == "20260821"
    restored.observe(
        _tick(
            "A2611",
            "20260825",
            volume=1,
            oi=99999,
            timestamp=datetime(2026, 8, 25, 7, 0, tzinfo=timezone.utc),
        ),
        catalog["A2611"],
    )
    snapshot = restored.completed_snapshot
    assert snapshot is not None
    assert set(snapshot.contracts) == {"A2609", "A2611"}
    assert snapshot.contracts["A2609"].open_interest == 30000


def test_activity_tracker_restart_sees_only_explicitly_checkpointed_observations(tmp_path):
    store = DirectionalActivityStore(tmp_path / "directional_activity.json")
    contract = _catalog()["A2609"]
    tracker = DirectionalActivityTracker(store)

    tracker.observe(_tick("A2609", "20260821", volume=8000, oi=30000), contract)

    assert tracker.current_trading_day == "20260821"
    assert not store.path.exists()
    assert DirectionalActivityTracker(store).current_trading_day == ""

    tracker.checkpoint()

    committed = store.load_state()
    assert committed.in_progress is not None
    assert committed.in_progress.contracts["A2609"].volume == 8000
    assert DirectionalActivityTracker(store).current_trading_day == "20260821"


def _completed_payload(envelope: dict) -> dict:
    completed = envelope.get("completed")
    return completed if isinstance(completed, dict) else envelope


def _resign(envelope: dict) -> None:
    unsigned = {key: value for key, value in envelope.items() if key != "checksum"}
    encoded = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    envelope["checksum"] = sha256(encoded).hexdigest()


def _saved_completed_envelope(tmp_path):
    path = tmp_path / "directional_activity.json"
    store = DirectionalActivityStore(path)
    store.save(_snapshot(old_volume=20_000, old_oi=50_000, new_volume=30_000, new_oi=60_000))
    return store, path, json.loads(path.read_text(encoding="utf-8"))


def test_activity_store_writes_versioned_envelope(tmp_path):
    _, _, envelope = _saved_completed_envelope(tmp_path)

    assert envelope.get("schema_version") == 1
    assert "checksum" in envelope
    assert "completed" in envelope
    assert "in_progress" in envelope


def test_activity_store_rejects_corrupt_json(tmp_path):
    path = tmp_path / "directional_activity.json"
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid directional activity JSON"):
        DirectionalActivityStore(path).load()


def test_activity_store_rejects_duplicate_json_keys(tmp_path):
    store, path, _ = _saved_completed_envelope(tmp_path)
    encoded = path.read_text(encoding="utf-8").replace(
        '"schema_version": 1',
        '"schema_version": 1,\n  "schema_version": 1',
        1,
    )
    path.write_text(encoded, encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate directional activity JSON key"):
        store.load()


def test_activity_store_rejects_tampered_payload(tmp_path):
    store, path, envelope = _saved_completed_envelope(tmp_path)
    _completed_payload(envelope)["contracts"]["A2609"]["volume"] = 999_999
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(ValueError, match="directional activity checksum mismatch"):
        store.load()


def test_activity_store_rejects_checksummed_non_finite_activity(tmp_path):
    store, path, envelope = _saved_completed_envelope(tmp_path)
    _completed_payload(envelope)["contracts"]["A2609"]["open_interest"] = float("nan")
    _resign(envelope)
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(ValueError, match="finite and non-negative"):
        store.load()


def test_activity_store_and_selection_reject_internal_symbol_identity_mismatch(tmp_path):
    store, path, envelope = _saved_completed_envelope(tmp_path)
    _completed_payload(envelope)["contracts"]["A2609"]["symbol"] = "A2611"
    _resign(envelope)
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(ValueError, match="symbol identity"):
        store.load()

    snapshot = _snapshot(old_volume=20_000, old_oi=50_000, new_volume=0, new_oi=0)
    activity = snapshot.contracts["A2609"]
    snapshot.contracts["A2609"] = ContractActivity(
        "A2611",
        activity.exchange,
        activity.product,
        activity.trading_day,
        activity.volume,
        activity.open_interest,
        activity.timestamp,
    )
    with pytest.raises(ValueError, match="symbol identity"):
        select_contracts_from_activity(
            _config(), list(_catalog().values()), snapshot, date(2026, 8, 25)
        )


def test_activity_store_rejects_checksummed_non_string_contract_identity(tmp_path):
    store, path, envelope = _saved_completed_envelope(tmp_path)
    _completed_payload(envelope)["contracts"]["A2609"]["exchange"] = 7
    _resign(envelope)
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(ValueError, match="identity fields must be non-empty strings"):
        store.load()


def test_activity_tracker_rejects_older_contract_observation_without_mutation(tmp_path):
    store = DirectionalActivityStore(tmp_path / "directional_activity.json")
    tracker = DirectionalActivityTracker(store)
    contract = _catalog()["A2609"]
    latest = datetime(2026, 8, 21, 7, 0, tzinfo=timezone.utc)
    tracker.observe(
        _tick("A2609", "20260821", volume=8000, oi=30000, timestamp=latest),
        contract,
    )
    tracker.checkpoint()

    with pytest.raises(ValueError, match="older than the latest activity"):
        tracker.observe(
            _tick(
                "A2609",
                "20260821",
                volume=9000,
                oi=40000,
                timestamp=latest - timedelta(seconds=1),
            ),
            contract,
        )

    state = store.load_state()
    assert state.in_progress is not None
    assert state.in_progress.contracts["A2609"].volume == 8000
    assert tracker.current_trading_day == "20260821"


def test_activity_tracker_rejects_older_timestamp_on_forward_trading_day(tmp_path):
    store = DirectionalActivityStore(tmp_path / "directional_activity.json")
    tracker = DirectionalActivityTracker(store)
    contract = _catalog()["A2609"]
    latest = datetime(2026, 8, 24, 7, 0, tzinfo=timezone.utc)
    tracker.observe(
        _tick("A2609", "20260824", volume=8000, oi=30000, timestamp=latest),
        contract,
    )
    tracker.checkpoint()

    with pytest.raises(ValueError, match="older than the latest activity"):
        tracker.observe(
            _tick(
                "A2609",
                "20260825",
                volume=9000,
                oi=40000,
                timestamp=latest - timedelta(seconds=1),
            ),
            contract,
        )

    state = store.load_state()
    assert state.completed is None
    assert state.in_progress is not None
    assert state.in_progress.trading_day == "20260824"
    assert tracker.current_trading_day == "20260824"


def test_activity_tracker_rejects_conflicting_equal_timestamp_observation(tmp_path):
    store = DirectionalActivityStore(tmp_path / "directional_activity.json")
    tracker = DirectionalActivityTracker(store)
    contract = _catalog()["A2609"]
    timestamp = datetime(2026, 8, 21, 7, 0, tzinfo=timezone.utc)
    tracker.observe(
        _tick("A2609", "20260821", volume=8000, oi=30000, timestamp=timestamp),
        contract,
    )
    tracker.checkpoint()

    with pytest.raises(ValueError, match="conflicts at the latest activity timestamp"):
        tracker.observe(
            _tick("A2609", "20260821", volume=9000, oi=30000, timestamp=timestamp),
            contract,
        )

    state = store.load_state()
    assert state.in_progress is not None
    assert state.in_progress.contracts["A2609"].volume == 8000


def test_activity_tracker_rejects_backward_trading_day_before_mutation(tmp_path):
    store = DirectionalActivityStore(tmp_path / "directional_activity.json")
    tracker = DirectionalActivityTracker(store)
    contract = _catalog()["A2609"]
    tracker.observe(_tick("A2609", "20260825", volume=8000, oi=30000), contract)
    tracker.checkpoint()

    with pytest.raises(ValueError, match="cannot move backward"):
        tracker.observe(_tick("A2609", "20260824", volume=9000, oi=40000), contract)

    state = store.load_state()
    assert state.completed is None
    assert state.in_progress is not None
    assert state.in_progress.trading_day == "20260825"
    assert tracker.current_trading_day == "20260825"


def test_activity_tracker_rejects_day_older_than_completed_only_state(tmp_path):
    store = DirectionalActivityStore(tmp_path / "directional_activity.json")
    completed = DirectionalActivitySnapshot(
        "20260825",
        {
            "A2609": ContractActivity(
                "A2609",
                "DCE",
                "A",
                "20260825",
                8000,
                30000,
                datetime(2026, 8, 25, 7, 0, tzinfo=timezone.utc),
            )
        },
    )
    store.save(completed)
    tracker = DirectionalActivityTracker(store)

    with pytest.raises(ValueError, match="cannot move backward"):
        tracker.observe(
            _tick("A2609", "20260824", volume=9000, oi=40000),
            _catalog()["A2609"],
        )

    assert tracker.current_trading_day == ""
    assert tracker.completed_snapshot == completed


def test_activity_tracker_rejects_non_string_raw_trading_day(tmp_path):
    store = DirectionalActivityStore(tmp_path / "directional_activity.json")
    tracker = DirectionalActivityTracker(store)

    with pytest.raises(ValueError, match="trading day must be string"):
        tracker.observe(
            _tick("A2609", 20260821, volume=8000, oi=30000),  # type: ignore[arg-type]
            _catalog()["A2609"],
        )

    assert not store.path.exists()


def test_activity_tracker_rejects_boolean_raw_activity_before_float_coercion(tmp_path):
    store = DirectionalActivityStore(tmp_path / "directional_activity.json")
    tracker = DirectionalActivityTracker(store)

    with pytest.raises(ValueError, match="raw activity must contain finite numbers"):
        tracker.observe(
            _tick("A2609", "20260821", volume=True, oi=30000),
            _catalog()["A2609"],
        )

    assert not store.path.exists()


def test_previous_completed_activity_controls_contract_selection_not_current_ticks(tmp_path):
    store = DirectionalActivityStore(tmp_path / "directional_activity.json")
    tracker = DirectionalActivityTracker(store)
    catalog = _catalog()
    tracker.observe(_tick("A2609", "20260821", volume=20000, oi=50000), catalog["A2609"])
    tracker.observe(_tick("A2611", "20260821", volume=30000, oi=30000), catalog["A2611"])
    tracker.observe(
        _tick(
            "A2611",
            "20260825",
            volume=500000,
            oi=999999,
            timestamp=datetime(2026, 8, 25, 7, 0, tzinfo=timezone.utc),
        ),
        catalog["A2611"],
    )
    config = DirectionalConfig(
        enabled=True,
        policy="execution_aligned",
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
        policy="execution_aligned",
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
