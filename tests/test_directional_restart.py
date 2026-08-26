from dataclasses import asdict
from pathlib import Path

from afuture.directional_engine import DirectionalTradingEngine
from afuture.directional_runtime import DirectionalActionResult
from afuture.models import AccountSnapshot, ContractPosition
from afuture.risk import RiskConfig, RiskManager
from afuture.state import RuntimeState, StateStore


class _Manager:
    def bootstrap(self, now):
        pass

    def observe(self, tick):
        pass

    def maybe_rebalance(self, now):
        return DirectionalActionResult("hold")

    def flatten(self, now):
        return DirectionalActionResult("hold")

    def has_risk(self):
        return False

    def required_symbols(self):
        return set()

    def close(self):
        pass


class _Broker:
    def __init__(self, positions):
        self._positions = list(positions)
        self._ready = False
        self.account = AccountSnapshot(
            balance=100000,
            equity=100000,
            available=100000,
            margin=0,
            realized_pnl=0,
            unrealized_pnl=0,
            trading_day="20260825",
        )

    def start(self):
        self._ready = True

    def stop(self):
        self._ready = False

    def is_ready(self):
        return self._ready

    def subscribe(self, symbol, exchange):
        pass

    def get_account(self):
        return self.account

    def get_positions(self):
        return list(self._positions)

    def get_active_orders(self):
        return []

    def poll_events(self):
        return []

    def health_error(self):
        return None


def _engine(tmp_path: Path, stored, remote):
    store = StateStore(tmp_path / "state.json")
    state = RuntimeState(
        positions=[asdict(item) for item in stored],
        trading_day="20260825",
        day_start_equity=100000,
        equity_high_watermark=100000,
    )
    store.save(state)
    broker = _Broker(remote)
    engine = DirectionalTradingEngine(
        broker,
        [],
        {},
        RiskManager(RiskConfig()),
        store,
        directional_manager=_Manager(),
    )
    engine.start()
    return broker, engine


def test_directional_restart_reconciles_matching_broker_position(tmp_path):
    position = ContractPosition("A2609", "DCE", long_yesterday=2, long_price=100.0)
    _, engine = _engine(tmp_path, [position], [position])
    assert engine.reconcile_startup() is True
    assert engine.state.reconciled is True
    assert engine.halted is False


def test_stress90_restart_adopts_query_recovered_fills_even_when_net_position_matches(
    tmp_path,
):
    class CrashRecoveryManager(_Manager):
        def __init__(self):
            self.calls = 0

        def reconcile_authorized_crash_fills(self, local, remote, recent):
            self.calls += 1
            assert local == remote == []
            assert recent == ()
            return True, ("20260825:DCE:CTP.T-open", "20260825:DCE:CTP.T-close"), "ok"

    store = StateStore(tmp_path / "state.json")
    store.save(
        RuntimeState(
            trading_day="20260825",
            day_start_equity=100000,
            equity_high_watermark=100000,
        )
    )
    broker = _Broker([])
    manager = CrashRecoveryManager()
    engine = DirectionalTradingEngine(
        broker,
        [],
        {},
        RiskManager(RiskConfig()),
        store,
        directional_manager=manager,
    )
    engine.start()

    assert engine.reconcile_startup() is True
    assert manager.calls == 1
    assert engine.state.recent_trade_ids == [
        "20260825:DCE:CTP.T-open",
        "20260825:DCE:CTP.T-close",
    ]


def test_directional_restart_mismatch_fails_closed(tmp_path):
    stored = ContractPosition("A2609", "DCE", long_yesterday=2, long_price=100.0)
    remote = ContractPosition("A2609", "DCE", long_yesterday=1, long_price=100.0)
    _, engine = _engine(tmp_path, [stored], [remote])
    assert engine.reconcile_startup() is False
    assert engine.halted is True
    assert "position reconciliation failed" in engine.state.kill_reason
