import json
from pathlib import Path

import pytest

from afuture.state import RuntimeState, StateIntegrityError, StateStore


def write_envelope(
    path: Path,
    *,
    schema_version: object = 2,
    sequence: object = 1,
    state: object | None = None,
) -> None:
    state_payload = {} if state is None else state
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
        ({"schema_version": 2}, "state envelope missing fields"),
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
        (3, 1, "newer than this program"),
        (0, 1, "schema version must be a positive integer"),
        (2, 0, "sequence must be a positive integer"),
        (2, -1, "sequence must be a positive integer"),
        ("2", 1, "schema version must be a positive integer"),
        (2, "1", "sequence must be a positive integer"),
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
        [{"symbol": "cu2609"}],
        [{"symbol": "cu2609", "exchange": "SHFE", "long_today": -1}],
        [{"symbol": "cu2609", "exchange": "SHFE", "short_today": "1"}],
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


def test_save_migrates_valid_legacy_state(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"kill_switch": True, "positions": []}),
        encoding="utf-8",
    )
    store = StateStore(path)

    state = store.load()
    store.save(state)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 2
    assert raw["sequence"] == 1
    assert raw["state"]["kill_switch"] is True


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
