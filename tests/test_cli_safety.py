from pathlib import Path

import pytest

from afuture.cli import (
    adopt_recovery_state,
    drain_after_halt,
    validate_recovery_positions,
    wait_for_fresh_snapshot,
)
from afuture.models import AccountSnapshot, ContractPosition, PairConfig
from afuture.state import RuntimeState, StateStore


def test_wait_for_fresh_snapshot_requires_both_generations_to_advance():
    class FakeBroker:
        def __init__(self):
            self.calls = 0

        def snapshot_marker(self):
            return (2, 3)

        def snapshot_ready(self, marker):
            assert marker == (2, 3)
            self.calls += 1
            return self.calls >= 2

    broker = FakeBroker()
    wait_for_fresh_snapshot(broker, timeout_seconds=0.2, poll_interval=0.001)
    assert broker.calls >= 2


def test_wait_for_fresh_snapshot_times_out_without_complete_snapshot():
    class FakeBroker:
        def snapshot_marker(self):
            return (0, 0)

        def snapshot_ready(self, marker):
            return False

    with pytest.raises(RuntimeError, match="fresh CTP account/position snapshot"):
        wait_for_fresh_snapshot(FakeBroker(), timeout_seconds=0.001, poll_interval=0.0001)


def test_recovery_accepts_balanced_dynamic_volume_not_only_pair_cap():
    pair = PairConfig("m_pair", "m2609", "m2701", "DCE", 5)
    positions = [
        ContractPosition("m2609", "DCE", long_yesterday=2),
        ContractPosition("m2701", "DCE", short_yesterday=2),
    ]

    validate_recovery_positions([pair], positions)


def test_recovery_rejects_volume_above_pair_cap_and_unbalanced_positions():
    pair = PairConfig("m_pair", "m2609", "m2701", "DCE", 3)
    with pytest.raises(RuntimeError, match="configured risk cap"):
        validate_recovery_positions(
            [pair],
            [
                ContractPosition("m2609", "DCE", long_today=4),
                ContractPosition("m2701", "DCE", short_today=4),
            ],
        )
    with pytest.raises(RuntimeError, match="balanced spread"):
        validate_recovery_positions([pair], [ContractPosition("m2609", "DCE", long_today=1)])


def test_recovery_rejects_unknown_contract():
    pair = PairConfig("m_pair", "m2609", "m2701", "DCE", 3)
    with pytest.raises(RuntimeError, match="not configured"):
        validate_recovery_positions([pair], [ContractPosition("rb2610", "SHFE", long_today=1)])


def test_adopt_recovery_state_keeps_kill_switch_and_requires_fresh_metadata(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    state = RuntimeState(
        kill_switch=True,
        kill_reason="position reconciliation failed",
        trading_day="20260819",
        day_start_equity=500000,
        equity_high_watermark=520000,
        metadata_verified=True,
    )
    account = AccountSnapshot(500000, 500000, 400000, 100000, 0, 0, "20260820")
    positions = [ContractPosition("m2609", "DCE", long_yesterday=1, long_price=3000.0)]

    adopt_recovery_state(store, state, account, positions)

    saved = store.load()
    assert saved.kill_switch
    assert not saved.reconciled
    assert not saved.metadata_verified
    assert saved.trading_day == "20260820"
    assert saved.day_start_equity == 500000
    assert saved.equity_high_watermark == 520000
    assert store.positions_from_state(saved)[0].long_yesterday == 1


def test_drain_after_halt_waits_until_active_orders_are_gone():
    class FakeBroker:
        def __init__(self):
            self.active = [type("Order", (), {"order_id": "o1"})()]
            self.cancelled = []

        def get_active_orders(self):
            return list(self.active)

        def cancel_order(self, order_id):
            self.cancelled.append(order_id)

    class FakeEngine:
        def __init__(self, broker):
            self.broker = broker
            self.calls = 0

        def run_once(self):
            self.calls += 1
            self.broker.active.clear()

    broker = FakeBroker()
    engine = FakeEngine(broker)

    assert drain_after_halt(engine, broker, timeout_seconds=0.1, poll_interval=0.001)
    assert broker.cancelled == ["o1"]
    assert engine.calls >= 1
