from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from afuture.process_run import ProcessRunStore, apply_unclean_restart_fence
from afuture.runtime_heartbeat import RuntimeHeartbeatObserver, install_engine_heartbeat_hooks
from afuture.state import RuntimeState, StateStore


IDENTITY = {
    "deployment_digest": "a" * 64,
    "runtime_identity_digest": "b" * 64,
    "account_identity_digest": "c" * 64,
    "start_state_checksum": "d" * 64,
}


def test_restart_fence_preserves_crashed_run_as_previous_receipt(tmp_path: Path) -> None:
    process_store = ProcessRunStore(tmp_path / "process_run.json")
    crashed = process_store.begin(**IDENTITY, process_uuid="11111111-1111-4111-8111-111111111111")
    state_store = StateStore(tmp_path / "state.json")
    state_store.save(RuntimeState())

    result = apply_unclean_restart_fence(
        process_store=process_store,
        state_store=state_store,
        runtime_dir=tmp_path,
        deployment_digest="a" * 64,
        runtime_identity_digest="b" * 64,
        account_identity_digest="c" * 64,
    )

    assert result.blocked is True
    current = process_store.load_required()
    previous = json.loads(process_store.previous_path.read_text(encoding="utf-8"))
    assert current.phase == "restart_fenced"
    assert current.clean_shutdown is True
    assert current.parent_checksum == crashed.checksum
    assert previous["process_uuid"] == crashed.process_uuid
    assert previous["checksum"] == crashed.checksum
    assert previous["clean_shutdown"] is False


def test_missing_snapshot_clock_is_not_reported_as_fresh() -> None:
    source = SimpleNamespace()
    assert RuntimeHeartbeatObserver._age(source, "_last_account_monotonic") is None
    assert RuntimeHeartbeatObserver._age(source, "_last_position_snapshot_monotonic") is None


def test_engine_heartbeat_hook_leaves_callbacks_untouched() -> None:
    from afuture.engine import TradingEngine

    callbacks = {
        "on_tick": TradingEngine.on_tick,
        "on_order": TradingEngine.on_order,
        "on_trade": TradingEngine.on_trade,
    }
    install_engine_heartbeat_hooks()
    assert TradingEngine.on_tick is callbacks["on_tick"]
    assert TradingEngine.on_order is callbacks["on_order"]
    assert TradingEngine.on_trade is callbacks["on_trade"]


def test_systemd_live_template_does_not_embed_confirmation_or_activation() -> None:
    text = (
        Path(__file__).resolve().parents[1] / "deploy" / "systemd" / "afuture-live.service"
    ).read_text(encoding="utf-8")
    assert "--confirm-live" not in text
    assert "LIVE_CONFIRM" not in text
    assert "ACTIVATION" not in text.upper()
    assert "stress90-operator-roll-forward" not in text
    assert "stress90-account-rebase" not in text
