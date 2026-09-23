from __future__ import annotations

import json
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from afuture.process_run import ProcessRunStore, apply_unclean_restart_fence
from afuture.runtime_heartbeat import (
    RuntimeHeartbeatObserver,
    install_engine_heartbeat_hooks,
    sigterm_as_keyboard_interrupt,
)
from afuture.runtime_lease import AccountExclusiveRuntimeLease
from afuture.state import RuntimeState, StateStore

IDENTITY = {
    "deployment_digest": "a" * 64,
    "runtime_identity_digest": "b" * 64,
    "account_identity_digest": "c" * 64,
    "start_state_checksum": "d" * 64,
}


def test_restart_fence_preserves_crashed_run_as_previous_receipt(tmp_path: Path) -> None:
    process_store = ProcessRunStore(tmp_path / "process_run.json")
    crashed = process_store.begin(
        **IDENTITY,
        process_uuid="11111111-1111-4111-8111-111111111111",
    )
    state_store = StateStore(tmp_path / "state.json")
    state_store.save(RuntimeState())

    with AccountExclusiveRuntimeLease(tmp_path, "c" * 64, role="live") as lease:
        result = apply_unclean_restart_fence(
            process_store=process_store,
            state_store=state_store,
            runtime_dir=tmp_path,
            deployment_digest="a" * 64,
            runtime_identity_digest="b" * 64,
            account_identity_digest="c" * 64,
            lease=lease,
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


def test_runtime_heartbeat_throttle_skips_state_reads_between_writes() -> None:
    class NotDueWriter:
        def is_due(self) -> bool:
            return False

        def write(self, _facts: object) -> bool:
            raise AssertionError("throttled heartbeat must not reach write")

    observer = object.__new__(RuntimeHeartbeatObserver)
    observer.writer = NotDueWriter()
    observer._last_successful_cycle_utc = ""
    reads: list[str] = []

    def facts() -> dict[str, object]:
        reads.append("state")
        return {}

    observer.facts = facts  # type: ignore[method-assign]
    observer.after_cycle()

    assert reads == []


def test_engine_heartbeat_hook_leaves_callbacks_untouched() -> None:
    from afuture.engine import TradingEngine

    callbacks = {
        "on_tick": TradingEngine.on_tick,
        "order": TradingEngine._handle_order_event,
        "trade": TradingEngine._handle_trade_event,
        "account": TradingEngine._handle_account_event,
    }
    install_engine_heartbeat_hooks()
    assert TradingEngine.on_tick is callbacks["on_tick"]
    assert TradingEngine._handle_order_event is callbacks["order"]
    assert TradingEngine._handle_trade_event is callbacks["trade"]
    assert TradingEngine._handle_account_event is callbacks["account"]


def test_directional_heartbeat_hook_observes_complete_directional_cycle() -> None:
    from afuture.directional_engine import DirectionalTradingEngine

    install_engine_heartbeat_hooks()
    assert (
        getattr(DirectionalTradingEngine.run_once, "_afuture_heartbeat_full_cycle", False) is True
    )


def test_sigterm_uses_existing_keyboard_interrupt_shutdown_path() -> None:
    original = signal.getsignal(signal.SIGTERM)
    with sigterm_as_keyboard_interrupt():
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGTERM, None)
    assert signal.getsignal(signal.SIGTERM) is original


def test_systemd_live_template_does_not_embed_confirmation_or_activation() -> None:
    text = (
        Path(__file__).resolve().parents[1] / "deploy" / "systemd" / "afuture-live.service"
    ).read_text(encoding="utf-8")
    assert "--confirm-live" not in text
    assert "LIVE_CONFIRM" not in text
    assert "ACTIVATION" not in text.upper()
    assert "stress90-operator-roll-forward" not in text
    assert "stress90-account-rebase" not in text
