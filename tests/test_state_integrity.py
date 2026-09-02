import json
from dataclasses import asdict
from pathlib import Path

import pytest

from afuture.models import ContractPosition
from afuture.state import RuntimeState, StateIntegrityError, StateStore


def test_state_save_propagates_parent_directory_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A renamed lifecycle participant is not durable until its directory fsyncs."""
    import afuture.state as state_module

    calls = 0
    real_fsync = state_module.os.fsync

    def fail_parent_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected runtime-state parent fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(state_module.os, "fsync", fail_parent_fsync)

    with pytest.raises(OSError, match="parent fsync"):
        StateStore(tmp_path / "state.json").save(RuntimeState())

    assert calls == 2


def write_envelope(
    path: Path,
    *,
    schema_version: object = 3,
    sequence: object = 1,
    state: object | None = None,
) -> None:
    state_payload = asdict(RuntimeState())
    if state is not None:
        if not isinstance(state, dict):
            state_payload = state
        else:
            state_payload.update(state)
    raw = {
        "schema_version": schema_version,
        "sequence": sequence,
        "state": state_payload,
    }
    if isinstance(schema_version, int) and isinstance(sequence, int):
        raw["checksum"] = StateStore._checksum(
            schema_version,
            sequence,
            state_payload,
        )
    else:
        raw["checksum"] = "invalid"
    path.write_text(json.dumps(raw), encoding="utf-8")


def position_payload(**updates: object) -> dict[str, object]:
    payload = asdict(ContractPosition(symbol="cu2609", exchange="SHFE"))
    payload.update(updates)
    return payload


def test_save_refuses_to_replace_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        '{"schema_version": 2, "sequence": 7, "state": ',
        encoding="utf-8",
    )
    original = path.read_bytes()

    with pytest.raises(ValueError, match="invalid state JSON"):
        StateStore(path).save(RuntimeState())

    assert path.read_bytes() == original


def test_save_refuses_to_replace_checksum_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.save(RuntimeState(kill_switch=True))
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["state"]["kill_switch"] = False
    path.write_text(json.dumps(raw), encoding="utf-8")
    original = path.read_bytes()

    with pytest.raises(ValueError, match="state checksum mismatch"):
        store.save(RuntimeState())

    assert path.read_bytes() == original


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ([], "state root must be a JSON object"),
        ({"schema_version": 2}, "state envelope fields are not current"),
    ],
)
def test_load_rejects_malformed_envelope(
    tmp_path: Path,
    raw: object,
    message: str,
) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(StateIntegrityError, match=message):
        StateStore(path).load()


@pytest.mark.parametrize(
    ("schema_version", "sequence", "message"),
    [
        (4, 1, "schema is not current"),
        (0, 1, "schema is not current"),
        (2, 0, "schema is not current"),
        (2, -1, "schema is not current"),
        ("2", 1, "schema is not current"),
        (3, "1", "sequence must be a positive integer"),
    ],
)
def test_load_rejects_invalid_schema_or_sequence(
    tmp_path: Path,
    schema_version: object,
    sequence: object,
    message: str,
) -> None:
    path = tmp_path / "state.json"
    write_envelope(
        path,
        schema_version=schema_version,
        sequence=sequence,
    )

    with pytest.raises(StateIntegrityError, match=message):
        StateStore(path).load()


def test_load_rejects_non_object_state_payload(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    write_envelope(path, state=[])

    with pytest.raises(
        StateIntegrityError,
        match="state payload must be a JSON object",
    ):
        StateStore(path).load()


@pytest.mark.parametrize(
    "raw",
    [
        asdict(RuntimeState()),
        {
            "schema_version": 3,
            "sequence": 1,
            "state": asdict(RuntimeState()),
            "checksum": "x",
            "extra": True,
        },
    ],
)
def test_state_rejects_non_current_envelope_without_rewriting(
    tmp_path: Path,
    raw: object,
) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    original = path.read_bytes()

    with pytest.raises(StateIntegrityError, match="envelope|schema"):
        StateStore(path).save(RuntimeState())

    assert path.read_bytes() == original


@pytest.mark.parametrize("mutate", ["missing", "unknown"])
def test_state_requires_exact_current_payload_fields(
    tmp_path: Path,
    mutate: str,
) -> None:
    path = tmp_path / "state.json"
    payload = asdict(RuntimeState())
    if mutate == "missing":
        payload.pop("kill_switch")
    else:
        payload["retired_field"] = True
    raw = {
        "schema_version": 3,
        "sequence": 1,
        "state": payload,
    }
    raw["checksum"] = StateStore._checksum(3, 1, payload)
    path.write_text(json.dumps(raw), encoding="utf-8")
    original = path.read_bytes()

    with pytest.raises(StateIntegrityError, match="payload fields"):
        StateStore(path).load()

    assert path.read_bytes() == original


def test_state_rejects_duplicate_json_keys_without_rewriting(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    payload = asdict(RuntimeState())
    raw = {
        "schema_version": 3,
        "sequence": 1,
        "state": payload,
        "checksum": StateStore._checksum(3, 1, payload),
    }
    encoded = json.dumps(raw).replace(
        '"kill_switch": false',
        '"kill_switch": true, "kill_switch": false',
        1,
    )
    path.write_text(encoded, encoding="utf-8")
    original = path.read_bytes()

    with pytest.raises(StateIntegrityError, match="duplicate state JSON field"):
        StateStore(path).save(RuntimeState())

    assert path.read_bytes() == original


@pytest.mark.parametrize("mutate", ["missing", "unknown"])
def test_state_requires_exact_current_position_fields(tmp_path: Path, mutate: str) -> None:
    path = tmp_path / "state.json"
    position = asdict(ContractPosition(symbol="cu2609", exchange="SHFE"))
    if mutate == "missing":
        position.pop("long_today")
    else:
        position["retired_field"] = 0
    write_envelope(path, state={"positions": [position]})
    original = path.read_bytes()

    with pytest.raises(StateIntegrityError, match="position fields are not current"):
        StateStore(path).load()

    assert path.read_bytes() == original


@pytest.mark.parametrize(
    ("state", "message"),
    [
        ({"kill_switch": "false"}, "kill_switch must be bool"),
        ({"day_start_equity": "500000"}, "day_start_equity must be finite number"),
        ({"positions": {}}, "positions must be a list"),
        ({"strategy_states": []}, "strategy_states must be an object"),
        ({"runtime_mode": "UNKNOWN"}, "runtime_mode is unsupported"),
        ({"recent_daily_returns": [0.1, float("inf")]}, "recent_daily_returns"),
    ],
)
def test_load_rejects_invalid_runtime_state_field_types(
    tmp_path: Path,
    state: object,
    message: str,
) -> None:
    path = tmp_path / "state.json"
    write_envelope(path, state=state)

    with pytest.raises(StateIntegrityError, match=message):
        StateStore(path).load()


@pytest.mark.parametrize(
    "positions",
    [
        [position_payload(symbol="")],
        [position_payload(exchange="")],
        [position_payload(long_today=-1)],
        [position_payload(short_today="1")],
        [position_payload(long_today=1, long_price=0.0)],
        [position_payload(short_today=1, short_price=float("nan"))],
        [position_payload(long_price=-1.0)],
    ],
)
def test_load_rejects_invalid_position_payload(
    tmp_path: Path,
    positions: list[dict[str, object]],
) -> None:
    path = tmp_path / "state.json"
    write_envelope(path, state={"positions": positions})

    with pytest.raises(StateIntegrityError, match="invalid persisted position"):
        StateStore(path).load()


def test_load_rejects_duplicate_position_identities(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    position = position_payload(symbol="m2609", exchange="DCE", long_today=1, long_price=3000)
    write_envelope(path, state={"positions": [position, dict(position)]})

    with pytest.raises(StateIntegrityError, match="duplicate position identities"):
        StateStore(path).load()


def test_state_accepts_same_position_symbol_on_distinct_exchanges(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    positions = [
        position_payload(symbol="same", exchange="DCE", long_today=1, long_price=100.0),
        position_payload(symbol="same", exchange="SHFE", short_today=2, short_price=200.0),
    ]
    write_envelope(path, state={"positions": positions})

    restored = StateStore(path).load()

    assert [(item["symbol"], item["exchange"]) for item in restored.positions] == [
        ("same", "DCE"),
        ("same", "SHFE"),
    ]


@pytest.mark.parametrize(
    "recent_trade_ids",
    [[""], [1], ["20260825:T1", "20260825:T1"]],
)
def test_load_rejects_invalid_recent_trade_id_history(
    tmp_path: Path,
    recent_trade_ids: list[object],
) -> None:
    path = tmp_path / "state.json"
    write_envelope(path, state={"recent_trade_ids": recent_trade_ids})

    with pytest.raises(StateIntegrityError, match="recent_trade_ids"):
        StateStore(path).load()


@pytest.mark.parametrize("field", ["strategy_states", "auto_pairs"])
def test_load_rejects_non_object_nested_state(
    tmp_path: Path,
    field: str,
) -> None:
    path = tmp_path / "state.json"
    write_envelope(path, state={field: {"entry": []}})

    with pytest.raises(StateIntegrityError, match=f"{field} values must be objects"):
        StateStore(path).load()


def test_save_increments_only_verified_sequence(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.save(RuntimeState(trading_day="20260825"))
    store.save(RuntimeState(trading_day="20260826"))

    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert raw["sequence"] == 2
    assert store.load().trading_day == "20260826"


def test_save_retains_exact_previous_verified_state(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.save(RuntimeState(trading_day="20260825", last_order_id="order-1"))
    original = store.path.read_bytes()

    store.save(RuntimeState(trading_day="20260826", last_order_id="order-2"))

    assert store.previous_path.read_bytes() == original
    previous = store.load_previous()
    assert previous is not None
    assert previous.trading_day == "20260825"
    assert previous.last_order_id == "order-1"
    assert store.load().last_order_id == "order-2"


def test_load_never_falls_back_to_valid_previous_state(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.save(RuntimeState(trading_day="20260825"))
    store.save(RuntimeState(trading_day="20260826"))
    store.path.write_text("{broken", encoding="utf-8")

    with pytest.raises(StateIntegrityError, match="invalid state JSON"):
        store.load()

    previous = store.load_previous()
    assert previous is not None
    assert previous.trading_day == "20260825"


def test_save_verifies_the_exact_bytes_retained_as_previous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state.json")
    store.save(RuntimeState(trading_day="20260825"))
    original = store.path.read_bytes()
    real_read_bytes = Path.read_bytes

    def inject_unverified_bytes(path: Path) -> bytes:
        if path == store.path:
            return b"{broken"
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", inject_unverified_bytes)

    with pytest.raises(StateIntegrityError, match="invalid state JSON"):
        store.save(RuntimeState(trading_day="20260826"))

    monkeypatch.undo()
    assert store.path.read_bytes() == original
    assert not store.previous_path.exists()


def test_load_wraps_invalid_utf8_as_state_integrity_error(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b"\xff\xfe")

    with pytest.raises(StateIntegrityError, match="invalid state UTF-8"):
        StateStore(path).load()


def test_load_previous_returns_none_when_no_verified_backup_exists(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")

    assert store.load_previous() is None


def test_save_rejects_schema_less_state_without_migrating(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"kill_switch": True, "positions": []}),
        encoding="utf-8",
    )
    store = StateStore(path)

    original = path.read_bytes()
    with pytest.raises(StateIntegrityError, match="envelope"):
        store.save(RuntimeState())

    assert path.read_bytes() == original


def test_failed_atomic_replace_preserves_target_and_removes_temp_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state.json")
    store.save(RuntimeState(trading_day="20260825"))
    original = store.path.read_bytes()

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError(f"replace failed: {source.name} -> {target.name}")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        store.save(RuntimeState(trading_day="20260826"))

    assert store.path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [store.path]
