import json
from dataclasses import asdict
from pathlib import Path

import pytest

from afuture.state import RuntimeState, StateIntegrityError, StateStore


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
    ("schema_version", "sequence", "message"),
    [
        (4, 1, "schema is not current"),
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
