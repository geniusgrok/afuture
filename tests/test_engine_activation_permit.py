from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from afuture.cli import _initialize_live_engine_after_snapshot
from afuture.engine import TradingEngine
from afuture.models import AccountSnapshot, RuntimeMode
from afuture.risk import RiskConfig, RiskManager
from afuture.state import RuntimeState, StateStore


class _Broker:
    def __init__(self) -> None:
        self.ready = False
        self.position_reads = 0

    def start(self) -> None:
        self.ready = True

    def stop(self) -> None:
        self.ready = False

    def is_ready(self) -> bool:
        return self.ready

    def subscribe(self, symbol, exchange) -> None:
        raise AssertionError("empty engine must not subscribe")

    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(500_000, 500_000, 500_000, 0, 0, 0, "20260825")

    def get_positions(self):
        self.position_reads += 1
        return []

    def get_active_orders(self):
        return []

    def poll_events(self):
        return []


class _Authority:
    requires_technical_activation_permit = True

    def __init__(self) -> None:
        self.calls = 0

    def activate(self, *, state_store, broker, lease):
        del broker, lease
        self.calls += 1
        current = state_store.load_required_record()
        return state_store.save(
            replace(
                current.state,
                kill_switch=False,
                kill_reason="",
                runtime_mode=RuntimeMode.RUNNING.value,
            ),
            expected_sequence=current.sequence,
            expected_checksum=current.checksum,
        )


def _engine(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    store.save(
        RuntimeState(
            kill_switch=True,
            kill_reason="commissioning",
            reconciled=True,
            runtime_mode=RuntimeMode.HALTED.value,
        )
    )
    broker = _Broker()
    authority = _Authority()
    engine = TradingEngine(
        broker,
        [],
        {},
        RiskManager(RiskConfig()),
        store,
        technical_activation_authority=authority,
    )
    return broker, authority, engine, store


def test_technical_activation_capability_defers_ready_startup_initialization(
    tmp_path: Path,
) -> None:
    broker, authority, engine, store = _engine(tmp_path)
    before = store.path.read_bytes()

    engine.start()

    assert broker.ready is True
    assert engine.halted is True
    assert engine._initialized is False
    assert authority.calls == 0
    assert store.path.read_bytes() == before


def test_running_stress90_restart_also_defers_ready_initialization(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.save(
        RuntimeState(
            kill_switch=False,
            reconciled=True,
            runtime_mode=RuntimeMode.RUNNING.value,
        )
    )
    broker = _Broker()
    authority = _Authority()
    engine = TradingEngine(
        broker,
        [],
        {},
        RiskManager(RiskConfig()),
        store,
        technical_activation_authority=authority,
    )

    engine.start()

    assert engine.halted is False
    assert engine._initialized is False


def test_generic_clear_cannot_bypass_technical_activation_capability(tmp_path: Path) -> None:
    broker, authority, engine, store = _engine(tmp_path)
    engine.start()

    assert engine.clear_kill_switch_after_reconcile() is False
    assert engine.halted is True
    assert store.load().runtime_mode == RuntimeMode.HALTED.value
    assert authority.calls == 0
    assert broker.position_reads == 0


def test_engine_activation_entrypoint_delegates_once_and_updates_memory(tmp_path: Path) -> None:
    _broker, authority, engine, store = _engine(tmp_path)
    engine.start()

    assert engine.activate_halted_runtime_from_permit(object()) is True

    assert authority.calls == 1
    assert engine.halted is False
    assert engine.state.runtime_mode == RuntimeMode.RUNNING.value
    assert engine.state.kill_switch is False
    assert store.load().runtime_mode == RuntimeMode.RUNNING.value


def test_live_startup_consumes_permit_before_runtime_initialization() -> None:
    calls: list[str] = []

    class _Engine:
        halted = True
        requires_technical_activation_permit = True
        state = SimpleNamespace(directional_daily_circuit_day="")

        def verify_stress90_startup_session(self, lease, *, timeout_seconds: float) -> bool:
            assert lease == "held-lease"
            assert timeout_seconds == 12.0
            calls.append("session")
            return True

        def activate_halted_runtime_from_permit(self, lease) -> bool:
            assert lease == "held-lease"
            calls.append("consume")
            self.halted = False
            return True

        def initialize_after_ready(self) -> None:
            calls.append("initialize")

    _initialize_live_engine_after_snapshot(
        _Engine(),
        "held-lease",
        session_timeout_seconds=12.0,
    )

    assert calls == ["session", "consume", "initialize"]


def test_live_startup_keeps_daily_circuit_recovery_separate_from_permit() -> None:
    calls: list[str] = []

    class _Engine:
        halted = True
        requires_technical_activation_permit = True
        state = SimpleNamespace(directional_daily_circuit_day="20260825")

        def verify_stress90_startup_session(self, lease, *, timeout_seconds: float) -> bool:
            assert lease == "held-lease"
            assert timeout_seconds == 12.0
            calls.append("session")
            return True

        def activate_halted_runtime_from_permit(self, lease) -> bool:
            raise AssertionError("daily circuit must not consume Doctor permit")

        def initialize_after_ready(self) -> None:
            calls.append("initialize")

    _initialize_live_engine_after_snapshot(
        _Engine(),
        "held-lease",
        session_timeout_seconds=12.0,
    )

    assert calls == ["session", "initialize"]


def test_running_stress90_restart_requires_fresh_session_before_initialization() -> None:
    calls: list[str] = []

    class _Engine:
        halted = False
        requires_technical_activation_permit = True
        state = SimpleNamespace(directional_daily_circuit_day="")

        def verify_stress90_startup_session(self, lease, *, timeout_seconds: float) -> bool:
            assert lease == "held-lease"
            assert timeout_seconds == 7.0
            calls.append("session")
            return True

        def activate_halted_runtime_from_permit(self, lease) -> bool:
            raise AssertionError("RUNNING restart must not consume a permit")

        def initialize_after_ready(self) -> None:
            calls.append("initialize")

    _initialize_live_engine_after_snapshot(
        _Engine(),
        "held-lease",
        session_timeout_seconds=7.0,
    )

    assert calls == ["session", "initialize"]
