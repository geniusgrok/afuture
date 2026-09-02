from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from multiprocessing import get_context
from pathlib import Path
from queue import Empty

import pytest

from afuture.broker.shadow import ShadowBroker
from afuture.broker.sim import SimBroker
from afuture.broker.sim_state import SimBrokerStateStore
from afuture.models import (
    BrokerEvent,
    ContractInfo,
    ContractPosition,
    ContractSpec,
    FeeSpec,
    Offset,
    OrderRequest,
    OrderSide,
    OrderStatus,
    Tick,
)


def _spec(
    *,
    multiplier: float = 10,
    open_fixed: float = 2.0,
    close_fixed: float = 3.0,
) -> ContractSpec:
    return ContractSpec(
        "A2612",
        "DCE",
        multiplier,
        1,
        0.1,
        0.1,
        FeeSpec(open_fixed=open_fixed, close_fixed=close_fixed),
    )


def _tick(
    *,
    minute: int = 0,
    bid: float = 99.0,
    ask: float = 101.0,
    last: float = 100.0,
    bid_volume: float = 10.0,
    ask_volume: float = 10.0,
    trading_day: str = "20260825",
) -> Tick:
    return Tick(
        symbol="A2612",
        exchange="DCE",
        timestamp=datetime(2026, 8, 25, 1, 0, tzinfo=timezone.utc) + timedelta(minutes=minute),
        bid_price=bid,
        ask_price=ask,
        last_price=last,
        bid_volume=bid_volume,
        ask_volume=ask_volume,
        trading_day=trading_day,
    )


class _LiveBroker:
    def __init__(
        self,
        identity: str = "live-account-a",
        trading_day: str = "20260825",
    ) -> None:
        self.identity = identity
        self.trading_day = trading_day
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def is_ready(self) -> bool:
        return self.started

    def get_trading_day(self) -> str:
        return self.trading_day

    def get_account_identity_digest(self) -> str:
        if not self.identity:
            return ""
        return sha256(self.identity.encode("utf-8")).hexdigest()


class _BatchLiveBroker(_LiveBroker):
    def __init__(
        self,
        *,
        events: list[BrokerEvent] | None = None,
        trading_day: str = "20260825",
    ) -> None:
        super().__init__(trading_day=trading_day)
        self.events = list(events or [])

    def poll_events(self) -> list[BrokerEvent]:
        events = list(self.events)
        self.events.clear()
        return events


class _FailingBatchLiveBroker(_BatchLiveBroker):
    def poll_events(self) -> list[BrokerEvent]:
        raise OSError("simulated live event batch failure")


class _CatalogLiveBroker(_LiveBroker):
    def __init__(self, catalog: list[ContractInfo]) -> None:
        super().__init__()
        self.catalog = list(catalog)

    def get_contract_catalog(self) -> list[ContractInfo]:
        return list(self.catalog)


def _resign(envelope: dict[str, object]) -> None:
    unsigned = {key: value for key, value in envelope.items() if key != "checksum"}
    encoded = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    envelope["checksum"] = sha256(encoded).hexdigest()


def _multiprocess_writer(
    state_path: str,
    ready,
    release,
    results,
) -> None:
    try:
        broker = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
        broker.start()
        ready.put("ready")
        release.wait()
        order_id = broker.send_order(
            OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 90.0)
        )
        results.put(("ok", order_id))
    except BaseException as exc:
        results.put(("error", str(exc)))


def _lock_tamper_holder(
    state_path: str,
    held,
    release,
    results,
) -> None:
    store = SimBrokerStateStore(state_path, {"identity": "lock-tamper"})
    record = store.load()
    assert record is not None
    real_replace = store._atomic_replace
    paused = False

    def pause_first_replace(
        directory_descriptor: int,
        target: Path,
        payload: bytes,
    ) -> None:
        nonlocal paused
        if not paused:
            paused = True
            held.put("held")
            release.wait()
        real_replace(directory_descriptor, target, payload)

    store._atomic_replace = pause_first_replace  # type: ignore[method-assign]
    try:
        store.begin(record.state, "holder")
        results.put(("holder", "ok"))
    except BaseException as exc:
        results.put(("holder", str(exc)))


def _lock_tamper_contender(
    state_path: str,
    ready,
    start,
    entered,
    results,
) -> None:
    store = SimBrokerStateStore(state_path, {"identity": "lock-tamper"})
    record = store.load()
    assert record is not None
    real_replace = store._atomic_replace

    def record_entry(
        directory_descriptor: int,
        target: Path,
        payload: bytes,
    ) -> None:
        entered.put("entered")
        real_replace(directory_descriptor, target, payload)

    store._atomic_replace = record_entry  # type: ignore[method-assign]
    ready.put("ready")
    start.wait()
    try:
        store.begin(record.state, "contender")
        results.put(("contender", "ok"))
    except BaseException as exc:
        results.put(("contender", str(exc)))


def _directory_rebind_holder(
    state_path: str,
    held,
    release,
    results,
) -> None:
    store = SimBrokerStateStore(state_path, {"identity": "directory-rebind"})
    record = store.load()
    assert record is not None
    real_replace = store._atomic_replace
    paused = False

    def pause_first_replace(
        directory_descriptor: int,
        target: Path,
        payload: bytes,
    ) -> None:
        nonlocal paused
        if not paused:
            paused = True
            held.put("held")
            release.wait()
        real_replace(directory_descriptor, target, payload)

    store._atomic_replace = pause_first_replace  # type: ignore[method-assign]
    try:
        store.begin({"value": "holder"}, "holder")
        results.put(("holder", "ok"))
    except BaseException as exc:
        results.put(("holder", str(exc)))


def _directory_rebind_contender(state_path: str, results) -> None:
    store = SimBrokerStateStore(state_path, {"identity": "directory-rebind"})
    record = store.load()
    assert record is not None
    try:
        store.begin({"value": "contender"}, "contender")
        store.complete({"value": "contender"}, "contender")
        results.put(("contender", "ok"))
    except BaseException as exc:
        results.put(("contender", str(exc)))


def test_shadow_open_position_and_wrapper_identity_survive_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "shadow-broker.json"
    live = _LiveBroker()
    broker = ShadowBroker(
        live,
        500_000,
        slippage_ticks=0,
        latency_ticks=0,
        market_impact_ticks=0,
        state_path=state_path,
    )
    broker.update_specs({"A2612": _spec()})
    broker.start()
    broker.publish_tick(_tick())
    order_id = broker.send_order(
        OrderRequest(
            "A2612",
            "DCE",
            OrderSide.BUY,
            Offset.OPEN,
            2,
            101.0,
            reference="stress90:A:open",
        )
    )
    original_identity = broker.get_account_identity_digest()
    broker.stop()

    restarted = ShadowBroker(
        _LiveBroker(),
        500_000,
        slippage_ticks=0,
        latency_ticks=0,
        market_impact_ticks=0,
        state_path=state_path,
    )
    restarted.update_specs({"A2612": _spec()})

    assert restarted.get_positions() == [
        ContractPosition(
            "A2612",
            "DCE",
            long_today=2,
            long_price=101.0,
        )
    ]
    assert restarted.owns_order(order_id)
    assert restarted.get_order(order_id).request.reference == "stress90:A:open"
    assert restarted.get_account().equity == pytest.approx(499_976.0)
    assert restarted.get_account_identity_digest() == original_identity


def test_sim_metadata_query_is_explicitly_nonblocking() -> None:
    broker = SimBroker(500_000, {"A2612": _spec()})

    assert broker.metadata_query_blocks is False


def test_shadow_restart_exposes_durable_adjacent_settlement_before_first_new_day_tick(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "settlement.json"
    broker = ShadowBroker(
        _LiveBroker(trading_day="20260825"),
        500_000,
        slippage_ticks=0,
        latency_ticks=0,
        market_impact_ticks=0,
        state_path=state_path,
    )
    broker.update_specs({"A2612": _spec()})
    broker.start()
    broker.publish_tick(_tick())
    broker.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 101.0))
    completed = broker.get_account()
    assert completed.cash_flow_verified is True
    assert completed.settlement_verified is True
    assert completed.settlement_id == 0
    broker.stop()

    restarted = ShadowBroker(
        _LiveBroker(trading_day="20260826"),
        500_000,
        slippage_ticks=0,
        latency_ticks=0,
        market_impact_ticks=0,
        state_path=state_path,
    )
    restarted.update_specs({"A2612": _spec()})
    rolled = restarted.get_account()

    assert rolled.trading_day == "20260826"
    assert rolled.previous_settlement_equity == pytest.approx(completed.equity)
    assert rolled.settlement_verified is True
    assert rolled.settlement_id == 1
    assert rolled.cash_flow_verified is True
    assert rolled.deposit == 0.0
    assert rolled.withdrawal == 0.0
    assert restarted.get_positions()[0].long_yesterday == 1
    assert restarted.get_positions()[0].long_today == 0

    same_day = ShadowBroker(
        _LiveBroker(trading_day="20260826"),
        500_000,
        slippage_ticks=0,
        latency_ticks=0,
        market_impact_ticks=0,
        state_path=state_path,
    )
    same_day.update_specs({"A2612": _spec()})
    assert same_day.get_account().settlement_id == 1
    assert same_day.get_account().previous_settlement_equity == pytest.approx(completed.equity)


def test_shadow_batch_checkpoint_preserves_sigkill_equity_for_next_day_settlement(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "sigkill-market-checkpoint.json"
    live = _BatchLiveBroker()
    broker = ShadowBroker(
        live,
        500_000,
        slippage_ticks=0,
        latency_ticks=0,
        market_impact_ticks=0,
        state_path=state_path,
    )
    broker.update_specs({"A2612": _spec()})
    broker.start()
    broker.publish_tick(_tick())
    broker.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 101.0))
    live.events = [
        BrokerEvent(
            "tick",
            _tick(minute=1, bid=109.0, ask=111.0, last=110.0),
        )
    ]

    broker.poll_events()
    crash_equity = broker.get_account().equity
    assert crash_equity == pytest.approx(500_088.0)

    # Deliberately omit stop(): construction below models a new process after SIGKILL.
    restarted = ShadowBroker(
        _BatchLiveBroker(trading_day="20260826"),
        500_000,
        slippage_ticks=0,
        latency_ticks=0,
        market_impact_ticks=0,
        state_path=state_path,
    )
    restarted.update_specs({"A2612": _spec()})
    rolled = restarted.get_account()

    assert rolled.previous_settlement_equity == pytest.approx(crash_equity)
    assert rolled.previous_settlement_equity == pytest.approx(500_088.0)
    assert rolled.settlement_id == 1


def test_shadow_invokes_one_market_checkpoint_per_polled_event_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = _BatchLiveBroker(
        events=[
            BrokerEvent("tick", _tick(minute=minute, last=100.0 + minute)) for minute in range(3)
        ]
    )
    broker = ShadowBroker(
        live,
        500_000,
        state_path=tmp_path / "one-checkpoint.json",
    )
    broker.update_specs({"A2612": _spec()})
    begins = 0
    completes = 0
    real_begin = broker.sim.begin_market_batch
    real_complete = broker.sim.complete_market_batch

    def record_begin(*, trading_day: str | None = None) -> None:
        nonlocal begins
        begins += 1
        real_begin(trading_day=trading_day)

    def record_complete() -> None:
        nonlocal completes
        completes += 1
        real_complete()

    monkeypatch.setattr(
        broker.sim,
        "begin_market_batch",
        record_begin,
    )
    monkeypatch.setattr(broker.sim, "complete_market_batch", record_complete)

    broker.poll_events()

    assert begins == 1
    assert completes == 1


def test_shadow_empty_market_batch_performs_no_durable_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = ShadowBroker(
        _BatchLiveBroker(),
        500_000,
        state_path=tmp_path / "empty-batch.json",
    )
    broker.update_specs({"A2612": _spec()})
    broker.get_account()  # Establish the authoritative day before measuring the batch.
    replacements = 0
    fsyncs = 0
    real_replace = SimBrokerStateStore._replace_at
    real_fsync = os.fsync

    def record_replace(
        directory_descriptor: int,
        source_name: str,
        target: Path,
    ) -> None:
        nonlocal replacements
        replacements += 1
        real_replace(directory_descriptor, source_name, target)

    def record_fsync(fd: int) -> None:
        nonlocal fsyncs
        fsyncs += 1
        real_fsync(fd)

    monkeypatch.setattr(
        SimBrokerStateStore,
        "_replace_at",
        staticmethod(record_replace),
    )
    monkeypatch.setattr(os, "fsync", record_fsync)

    assert broker.poll_events() == []
    assert replacements == 0
    assert fsyncs == 0


def test_shadow_market_only_ticks_commit_once_after_batch_not_before(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = _BatchLiveBroker(
        events=[BrokerEvent("tick", _tick(minute=minute)) for minute in range(5)]
    )
    broker = ShadowBroker(live, 500_000, state_path=tmp_path / "market-batch.json")
    broker.update_specs({"A2612": _spec()})
    broker.get_account()
    market_path = broker.sim._market_state_store.path
    target_replacements: list[Path] = []
    operations_during_callbacks: list[str | None] = []
    real_replace = SimBrokerStateStore._replace_at

    class Observer:
        def observe_raw_tick(self, _tick: Tick, _contract: object) -> None:
            envelope = json.loads(market_path.read_text(encoding="utf-8"))
            operations_during_callbacks.append(envelope["operation"])

    broker.sim.set_raw_tick_observer(Observer())

    def record_replace(
        directory_descriptor: int,
        source_name: str,
        target: Path,
    ) -> None:
        target_replacements.append(Path(target))
        real_replace(directory_descriptor, source_name, target)

    monkeypatch.setattr(
        SimBrokerStateStore,
        "_replace_at",
        staticmethod(record_replace),
    )
    events = broker.poll_events()

    assert len(events) == 5
    assert operations_during_callbacks == [None] * 5
    assert target_replacements == [
        market_path.with_name(f"{market_path.name}.prev"),
        market_path,
        market_path.with_name(f"{market_path.name}.prev"),
        market_path,
    ]


def test_active_order_batch_is_pending_before_tick_and_clean_before_delivery(
    tmp_path: Path,
) -> None:
    live = _BatchLiveBroker(events=[BrokerEvent("tick", _tick(minute=1))])
    state_path = tmp_path / "active-batch.json"
    broker = ShadowBroker(
        live,
        500_000,
        latency_ticks=2,
        state_path=state_path,
    )
    broker.update_specs({"A2612": _spec()})
    broker.get_account()
    broker.start()
    broker.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 110.0))
    observed_operations: list[str | None] = []

    class Observer:
        def observe_raw_tick(self, _tick: Tick, _contract: object) -> None:
            envelope = json.loads(state_path.read_text(encoding="utf-8"))
            observed_operations.append(envelope["operation"])

    broker.sim.set_raw_tick_observer(Observer())
    events = broker.poll_events()

    assert observed_operations == ["market_event_batch"]
    assert json.loads(state_path.read_text(encoding="utf-8"))["operation"] is None
    assert events


def test_active_order_live_batch_failure_leaves_restart_visible_pending_truth(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "active-live-failure.json"
    broker = ShadowBroker(
        _FailingBatchLiveBroker(),
        500_000,
        latency_ticks=10,
        state_path=state_path,
    )
    broker.update_specs({"A2612": _spec()})
    broker.get_account()
    broker.start()
    broker.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 110.0))

    with pytest.raises(OSError, match="live event batch failure"):
        broker.poll_events()
    with pytest.raises(RuntimeError, match="persistence is faulted"):
        broker.poll_events()
    with pytest.raises(RuntimeError, match="incomplete broker operation"):
        ShadowBroker(
            _BatchLiveBroker(),
            500_000,
            latency_ticks=10,
            state_path=state_path,
        )


def test_shadow_market_checkpoint_failure_propagates_before_batch_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = _BatchLiveBroker(events=[BrokerEvent("tick", _tick())])
    state_path = tmp_path / "checkpoint-failure.json"
    broker = ShadowBroker(
        live,
        500_000,
        state_path=state_path,
    )
    broker.update_specs({"A2612": _spec()})
    broker.get_account()
    market_path = state_path.with_name(f"{state_path.name}.market")
    real_replace = SimBrokerStateStore._replace_at
    market_current_replaces = 0

    def fail_checkpoint_replace(
        directory_descriptor: int,
        source_name: str,
        target: Path,
    ) -> None:
        nonlocal market_current_replaces
        if Path(target) == market_path:
            market_current_replaces += 1
            if market_current_replaces == 2:
                raise OSError("simulated market checkpoint failure")
        real_replace(directory_descriptor, source_name, target)

    monkeypatch.setattr(
        SimBrokerStateStore,
        "_replace_at",
        staticmethod(fail_checkpoint_replace),
    )

    with pytest.raises(OSError, match="market checkpoint failure"):
        broker.poll_events()
    monkeypatch.setattr(
        SimBrokerStateStore,
        "_replace_at",
        staticmethod(real_replace),
    )

    with pytest.raises(RuntimeError, match="persistence is faulted"):
        broker.poll_events()
    with pytest.raises(RuntimeError, match="incomplete broker market operation"):
        ShadowBroker(_BatchLiveBroker(), 500_000, state_path=state_path)


def test_market_checkpoint_uses_bounded_sidecar_bound_to_main_truth(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "bounded-market.json"
    broker = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    broker.start()
    broker.publish_tick(_tick())
    broker.send_order(
        OrderRequest(
            "A2612",
            "DCE",
            OrderSide.BUY,
            Offset.OPEN,
            1,
            101.0,
            reference="history-" + "x" * 20_000,
        )
    )
    main_before = state_path.read_bytes()
    broker.publish_tick(_tick(minute=1, bid=109.0, ask=111.0, last=110.0))

    broker.checkpoint_market_state()

    assert state_path.read_bytes() == main_before
    market_path = state_path.with_name(f"{state_path.name}.market")
    market = json.loads(market_path.read_text(encoding="utf-8"))
    main = json.loads(main_before)
    assert set(market["state"]) == {
        "broker_sequence",
        "broker_checksum",
        "trading_day",
        "ticks",
        "tick_sequence",
        "depth",
        "market_digest",
    }
    assert market["state"]["broker_sequence"] == main["sequence"]
    assert market["state"]["broker_checksum"] == main["checksum"]
    assert "history-" not in market_path.read_text(encoding="utf-8")


def test_stale_market_sidecar_is_accepted_only_for_proven_absorbed_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    absorbed_path = tmp_path / "absorbed-market.json"
    absorbed = SimBroker(500_000, {"A2612": _spec()}, state_path=absorbed_path)
    absorbed.start()
    absorbed.publish_tick(_tick())
    absorbed.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 101.0))
    absorbed.publish_tick(_tick(minute=1, bid=109.0, ask=111.0, last=110.0))
    absorbed.checkpoint_market_state()
    monkeypatch.setattr(absorbed, "_rebind_market_store", lambda: None)
    absorbed.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 90.0))

    restarted = SimBroker(500_000, {"A2612": _spec()}, state_path=absorbed_path)
    assert restarted.get_account().equity == pytest.approx(500_088.0)

    unproven_path = tmp_path / "unproven-market.json"
    unproven = SimBroker(500_000, {"A2612": _spec()}, state_path=unproven_path)
    unproven.start()
    unproven.publish_tick(_tick())
    unproven.checkpoint_market_state()
    monkeypatch.setattr(unproven, "_rebind_market_store", lambda: None)
    order_id = unproven.send_order(
        OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 90.0)
    )
    unproven.cancel_order(order_id)

    with pytest.raises(RuntimeError, match="ancestry cannot be proven"):
        SimBroker(500_000, {"A2612": _spec()}, state_path=unproven_path)


def test_market_sidecar_rejects_same_sequence_with_different_canonical_content(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "market-content.json"
    broker = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    broker.publish_tick(_tick())
    broker.checkpoint_market_state()
    market_path = state_path.with_name(f"{state_path.name}.market")
    envelope = json.loads(market_path.read_text(encoding="utf-8"))
    envelope["state"]["ticks"][0]["last_price"] = 123.0
    _resign(envelope)
    market_path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(RuntimeError, match="market content digest mismatch"):
        SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)


def test_stale_market_ancestry_requires_absorbed_canonical_market_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alien_path = tmp_path / "alien.json"
    alien = SimBroker(500_000, {"A2612": _spec()}, state_path=alien_path)
    alien.publish_tick(_tick(last=123.0))
    alien.checkpoint_market_state()

    target_path = tmp_path / "target.json"
    target = SimBroker(500_000, {"A2612": _spec()}, state_path=target_path)
    target.start()
    target.publish_tick(_tick(last=100.0))
    target.checkpoint_market_state()
    monkeypatch.setattr(target, "_rebind_market_store", lambda: None)
    target.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 90.0))

    alien_market = alien_path.with_name(f"{alien_path.name}.market")
    target_market = target_path.with_name(f"{target_path.name}.market")
    target_market.write_bytes(alien_market.read_bytes())
    target_market.with_name(f"{target_market.name}.prev").write_bytes(
        alien_market.with_name(f"{alien_market.name}.prev").read_bytes()
    )

    with pytest.raises(RuntimeError, match="stale market content was not absorbed"):
        SimBroker(500_000, {"A2612": _spec()}, state_path=target_path)


def test_active_order_tick_requires_explicit_batch_and_publish_has_no_store_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "explicit-market-batch.json"
    broker = SimBroker(
        500_000,
        {"A2612": _spec()},
        conservative=True,
        latency_ticks=1,
        state_path=state_path,
    )
    broker.start()
    broker.publish_tick(_tick())
    broker.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 90.0))
    real_replace = SimBrokerStateStore._replace_at
    replacements = 0

    def count_replace(
        directory_descriptor: int,
        source_name: str,
        target: Path,
    ) -> None:
        nonlocal replacements
        replacements += 1
        real_replace(directory_descriptor, source_name, target)

    monkeypatch.setattr(
        SimBrokerStateStore,
        "_replace_at",
        staticmethod(count_replace),
    )
    with pytest.raises(RuntimeError, match="explicit market batch"):
        broker.publish_tick(_tick(minute=1))
    assert replacements == 0

    with broker.market_batch(trading_day="20260825"):
        before_publish = replacements
        broker.publish_tick(_tick(minute=1))
        assert replacements == before_publish
    assert replacements > before_publish


def test_shadow_d_plus_one_position_and_order_entry_sync_and_reject_stale_tick(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "d-plus-one-entry.json"
    first = ShadowBroker(
        _BatchLiveBroker(trading_day="20260825"),
        500_000,
        slippage_ticks=0,
        latency_ticks=0,
        market_impact_ticks=0,
        state_path=state_path,
    )
    first.update_specs({"A2612": _spec()})
    first.start()
    first.publish_tick(_tick())
    first.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 101.0))

    restarted = ShadowBroker(
        _BatchLiveBroker(trading_day="20260826"),
        500_000,
        slippage_ticks=0,
        latency_ticks=0,
        market_impact_ticks=0,
        state_path=state_path,
    )
    restarted.update_specs({"A2612": _spec()})
    restarted.start()

    position = restarted.get_positions()[0]
    assert position.long_today == 0
    assert position.long_yesterday == 1
    assert restarted.get_session_trades() == []

    order_id = restarted.send_order(
        OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 1_000.0)
    )
    assert restarted.get_order(order_id).status is OrderStatus.NOT_TRADED
    assert restarted.get_session_trades() == []


def test_shadow_rollover_terminal_event_retains_bounded_order_ownership_proof(
    tmp_path: Path,
) -> None:
    live = _BatchLiveBroker(trading_day="20260825")
    broker = ShadowBroker(
        live,
        500_000,
        latency_ticks=10,
        state_path=tmp_path / "rollover-owned.json",
    )
    broker.update_specs({"A2612": _spec()})
    broker.start()
    broker.publish_tick(_tick())
    order_id = broker.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 110.0))
    broker.poll_events()

    live.trading_day = "20260826"
    broker.get_positions()
    events = broker.poll_events()
    terminal_orders = [
        event.payload
        for event in events
        if event.event_type == "order" and event.payload.order_id == order_id
    ]

    assert len(terminal_orders) == 1
    assert terminal_orders[0].status is OrderStatus.CANCELLED
    assert broker.owns_order(order_id) is True
    assert broker.get_order(order_id) is None
    assert broker.owns_order("SIM-0") is False
    assert broker.owns_order("SIM-01") is False
    assert broker.owns_order("SIM-2") is False
    assert broker.owns_order("CTP.1") is False

    envelope = json.loads((tmp_path / "rollover-owned.json").read_text(encoding="utf-8"))
    baseline = envelope["state"]["accounting_baseline"]
    assert baseline["compacted_order_count"] == 1
    assert baseline["next_order_sequence"] == 2
    assert baseline["compacted_through_trading_day"] == "20260825"
    assert len(baseline["history_digest"]) == 64
    assert envelope["state"]["orders"] == []


def test_nonpersistent_sim_rollover_preserves_active_orders_and_history() -> None:
    broker = SimBroker(
        500_000,
        {"A2612": _spec()},
        conservative=True,
        latency_ticks=10,
    )
    broker.start()
    broker.publish_tick(_tick())
    active_id = broker.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 90.0))
    broker.publish_tick(_tick(minute=1, trading_day="20260826"))

    active = broker.get_order(active_id)
    assert active is not None
    assert active.status is OrderStatus.NOT_TRADED
    assert broker.get_active_orders() == [active]
    assert broker.owns_order(active_id) is True


def test_nonpersistent_sim_rollover_preserves_filled_history_and_summary() -> None:
    broker = SimBroker(500_000, {"A2612": _spec()})
    broker.start()
    broker.publish_tick(_tick())
    order_id = broker.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 101.0))
    before = broker.get_execution_stress_summary()
    broker.publish_tick(_tick(minute=1, last=110.0, trading_day="20260826"))

    assert [order.order_id for order in broker.get_orders()] == [order_id]
    assert [trade.trade_id for trade in broker.get_trades()] == ["SIM-T-1"]
    assert broker.get_execution_stress_summary() == before
    position = broker.get_positions()[0]
    assert position.long_today == 0
    assert position.long_yesterday == 1


def test_nonpersistent_rollover_can_fill_from_other_symbol_stale_quote() -> None:
    specs = {
        "A2612": _spec(),
        "M2701": ContractSpec("M2701", "DCE", 10, 1, 0.1, 0.1),
    }
    broker = SimBroker(500_000, specs)
    broker.start()
    broker.publish_tick(_tick())
    broker.publish_tick(
        Tick(
            symbol="M2701",
            exchange="DCE",
            timestamp=_tick().timestamp,
            bid_price=99.0,
            ask_price=100.0,
            last_price=99.5,
            bid_volume=10.0,
            ask_volume=10.0,
            trading_day="20260825",
        )
    )
    broker.publish_tick(_tick(minute=1, trading_day="20260826"))

    order_id = broker.send_order(OrderRequest("M2701", "DCE", OrderSide.BUY, Offset.OPEN, 1, 100.0))

    assert broker.get_order(order_id).status is OrderStatus.FILLED
    assert broker.get_positions()[0].symbol == "M2701"


def test_nonpersistent_sim_accepts_backward_tick_day() -> None:
    broker = SimBroker(500_000, {"A2612": _spec()})
    broker.publish_tick(_tick(trading_day="20260826"))

    broker.publish_tick(_tick(minute=1, trading_day="20260825"))

    assert broker.get_account().trading_day == "20260825"


def test_trading_day_roll_compacts_history_into_accounting_baseline(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "session-compaction.json"
    broker = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    broker.start()
    broker.publish_tick(_tick())
    broker.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 101.0))
    broker.send_order(OrderRequest("A2612", "DCE", OrderSide.SELL, Offset.OPEN, 1, 99.0))
    assert len(broker.get_orders()) == 2
    assert len(broker.get_session_trades()) == 2

    broker.synchronize_trading_day("20260826")

    assert broker.get_orders() == []
    assert broker.get_session_trades() == []
    order_id = broker.send_order(
        OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 1_000.0)
    )
    assert order_id == "SIM-3"
    envelope = json.loads(state_path.read_text(encoding="utf-8"))
    baseline = envelope["state"]["accounting_baseline"]
    assert baseline["next_order_sequence"] == 3
    assert baseline["next_trade_sequence"] == 3
    assert baseline["compacted_order_count"] == 2
    assert baseline["compacted_trade_count"] == 2
    assert baseline["compacted_through_trading_day"] == "20260825"
    assert len(baseline["history_digest"]) == 64
    assert len(envelope["state"]["orders"]) == 1
    assert envelope["state"]["trades"] == []

    restarted = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    assert restarted.get_order("SIM-1") is None
    assert restarted.get_order("SIM-2") is None
    assert restarted.get_order("SIM-3") is not None
    assert restarted.get_positions() == broker.get_positions()
    assert restarted.get_account().balance == pytest.approx(broker.get_account().balance)


def test_rechecksummed_compacted_history_digest_tampering_fails_closed(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "history-digest-tamper.json"
    broker = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    broker.start()
    broker.publish_tick(_tick())
    broker.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 101.0))
    broker.synchronize_trading_day("20260826")
    envelope = json.loads(state_path.read_text(encoding="utf-8"))
    envelope["state"]["accounting_baseline"]["history_digest"] = "a" * 64
    _resign(envelope)
    state_path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(RuntimeError, match="history compaction transition mismatch"):
        SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)


def test_sim_equity_commission_and_terminal_orders_survive_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "sim-broker.json"
    broker = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    broker.start()
    broker.publish_tick(_tick())
    entry_id = broker.send_order(
        OrderRequest(
            "A2612",
            "DCE",
            OrderSide.BUY,
            Offset.OPEN,
            1,
            101.0,
            reference="entry",
        )
    )
    broker.publish_tick(_tick(minute=1, bid=109.0, ask=111.0, last=110.0))
    exit_id = broker.send_order(
        OrderRequest(
            "A2612",
            "DCE",
            OrderSide.SELL,
            Offset.CLOSE,
            1,
            109.0,
            reference="exit",
        )
    )

    restarted = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    account = restarted.get_account()

    assert account.balance == pytest.approx(500_075.0)
    assert account.equity == pytest.approx(500_075.0)
    assert account.realized_pnl == pytest.approx(75.0)
    assert restarted.get_execution_stress_summary()["commission_cost"] == pytest.approx(5.0)
    assert [
        (order.order_id, order.status, order.request.reference) for order in restarted.get_orders()
    ] == [
        (entry_id, OrderStatus.FILLED, "entry"),
        (exit_id, OrderStatus.FILLED, "exit"),
    ]


def test_partial_active_order_restores_timing_and_converges_without_duplicate_ids(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "partial-order.json"
    broker = SimBroker(
        500_000,
        {"A2612": _spec()},
        conservative=True,
        latency_ticks=1,
        state_path=state_path,
    )
    broker.start()
    broker.publish_tick(_tick(ask_volume=1.0))
    order_id = broker.send_order(
        OrderRequest(
            "A2612",
            "DCE",
            OrderSide.BUY,
            Offset.OPEN,
            3,
            110.0,
            reference="stress90:A:partial",
        )
    )
    with broker.market_batch(trading_day="20260825"):
        broker.publish_tick(_tick(minute=1, ask_volume=1.0))
    assert broker.get_order(order_id).status is OrderStatus.PART_TRADED

    restarted = SimBroker(
        500_000,
        {"A2612": _spec()},
        conservative=True,
        latency_ticks=1,
        state_path=state_path,
    )
    restarted.start()
    restored = restarted.get_order(order_id)
    assert restored is not None
    assert restored.traded == 1
    assert restored.average_price == pytest.approx(101.0)
    assert restored.request.reference == "stress90:A:partial"
    assert restarted.get_active_orders() == [restored]

    with restarted.market_batch(trading_day="20260825"):
        restarted.publish_tick(_tick(minute=2, bid=100.0, ask=102.0, last=101.0, ask_volume=2.0))

    assert restarted.get_order(order_id).status is OrderStatus.FILLED
    assert restarted.get_order(order_id).average_price == pytest.approx(305.0 / 3.0)
    assert [trade.trade_id for trade in restarted.get_trades()] == ["SIM-T-1", "SIM-T-2"]
    next_id = restarted.send_order(
        OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 90.0)
    )
    assert next_id == "SIM-2"


def test_all_position_buckets_and_average_prices_survive_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "position-buckets.json"
    broker = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    broker.start()
    broker.publish_tick(_tick())
    broker.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 2, 101.0))
    broker.send_order(OrderRequest("A2612", "DCE", OrderSide.SELL, Offset.OPEN, 3, 99.0))
    with broker.market_batch(trading_day="20260826"):
        broker.publish_tick(
            _tick(
                minute=1,
                bid=109.0,
                ask=111.0,
                last=110.0,
                trading_day="20260826",
            )
        )
    broker.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 111.0))
    broker.send_order(OrderRequest("A2612", "DCE", OrderSide.SELL, Offset.OPEN, 2, 109.0))

    restarted = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)

    assert restarted.get_positions() == [
        ContractPosition(
            "A2612",
            "DCE",
            long_today=1,
            long_yesterday=2,
            short_today=2,
            short_yesterday=3,
            long_price=pytest.approx(313.0 / 3.0),
            short_price=pytest.approx(103.0),
        )
    ]


def test_checksum_corruption_rejects_current_without_falling_back_to_previous(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "corrupt.json"
    broker = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    broker.start()
    broker.publish_tick(_tick())
    broker.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 90.0))

    current = json.loads(state_path.read_text(encoding="utf-8"))
    previous_path = state_path.with_name(f"{state_path.name}.prev")
    previous = json.loads(previous_path.read_text(encoding="utf-8"))
    assert current["kind"] == "afuture.broker.sim-state"
    assert current["schema_version"] == 1
    assert current["sequence"] == previous["sequence"] + 1
    assert len(current["checksum"]) == 64
    current["state"]["balance"] = 1.0
    state_path.write_text(json.dumps(current), encoding="utf-8")

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)


def test_boolean_schema_version_is_rejected_even_with_a_valid_checksum(tmp_path: Path) -> None:
    state_path = tmp_path / "boolean-schema.json"
    SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    envelope = json.loads(state_path.read_text(encoding="utf-8"))
    envelope["schema_version"] = True
    _resign(envelope)
    state_path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(RuntimeError, match="schema mismatch"):
        SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)


def test_persisted_configuration_and_shadow_account_mismatches_fail_closed(
    tmp_path: Path,
) -> None:
    sim_path = tmp_path / "configuration.json"
    SimBroker(
        500_000,
        {"A2612": _spec()},
        conservative=True,
        latency_ticks=1,
        state_path=sim_path,
    )
    with pytest.raises(RuntimeError, match="configuration mismatch"):
        SimBroker(
            500_000,
            {"A2612": _spec()},
            conservative=True,
            latency_ticks=2,
            state_path=sim_path,
        )

    shadow_path = tmp_path / "account.json"
    ShadowBroker(_LiveBroker("account-a"), 500_000, state_path=shadow_path)
    with pytest.raises(RuntimeError, match="configuration mismatch"):
        ShadowBroker(_LiveBroker("account-b"), 500_000, state_path=shadow_path)


def test_configuration_types_are_strict_and_numeric_specs_are_canonical(
    tmp_path: Path,
) -> None:
    numeric_path = tmp_path / "numeric-spec.json"
    SimBroker(
        500_000,
        {"A2612": _spec(multiplier=10)},
        state_path=numeric_path,
    )
    SimBroker(
        500_000,
        {"A2612": _spec(multiplier=10.0)},
        state_path=numeric_path,
    )

    typed_path = tmp_path / "typed-config.json"
    SimBroker(500_000, {"A2612": _spec()}, state_path=typed_path)
    envelope = json.loads(typed_path.read_text(encoding="utf-8"))
    envelope["configuration"]["conservative"] = 0
    _resign(envelope)
    typed_path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(RuntimeError, match="configuration mismatch"):
        SimBroker(500_000, {"A2612": _spec()}, state_path=typed_path)


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("symbol", "A2701"),
        ("exchange", "CZCE"),
        ("product", "M"),
        ("expiry", "2027-01-15"),
        ("listing", "2026-02-01"),
    ],
)
def test_persistent_static_catalog_manifest_is_order_independent_and_metadata_bound(
    tmp_path: Path,
    field: str,
    changed: str,
) -> None:
    state_path = tmp_path / f"catalog-{field}.json"
    first = ContractInfo("A2612", "DCE", "A", "2026-12-15", "2026-01-01")
    second = ContractInfo("M2701", "DCE", "M", "2027-01-15", "2026-03-01")
    SimBroker(
        500_000,
        {"A2612": _spec()},
        contract_catalog=[first, second],
        state_path=state_path,
    )
    SimBroker(
        500_000,
        {"A2612": _spec()},
        contract_catalog=[second, first],
        state_path=state_path,
    )
    changed_values = {
        "symbol": first.symbol,
        "exchange": first.exchange,
        "product": first.product,
        "expiry": first.expiry,
        "listing": first.listing,
    }
    changed_values[field] = changed
    changed_first = ContractInfo(**changed_values)

    with pytest.raises(RuntimeError, match="configuration mismatch"):
        SimBroker(
            500_000,
            {"A2612": _spec()},
            contract_catalog=[changed_first, second],
            state_path=state_path,
        )


@pytest.mark.parametrize(
    "catalog",
    [
        [
            ContractInfo("A2612", "DCE", "A", "2026-12-15"),
            ContractInfo("A2612", "DCE", "A", "2026-12-15"),
        ],
        [ContractInfo("", "DCE", "A", "2026-12-15")],
        [ContractInfo("A2612", "", "A", "2026-12-15")],
        [ContractInfo("A2612", "DCE", "", "2026-12-15")],
        [ContractInfo("A2612", "DCE", "A", "20261215")],
        [ContractInfo("A2612", "DCE", "A", "2026-12-15", "20260101")],
    ],
)
def test_static_catalog_duplicate_or_invalid_identity_fails_closed(
    tmp_path: Path,
    catalog: list[ContractInfo],
) -> None:
    with pytest.raises((RuntimeError, ValueError), match="contract catalog"):
        SimBroker(
            500_000,
            {"A2612": _spec()},
            contract_catalog=catalog,
            state_path=tmp_path / "invalid-catalog.json",
        )


def test_shadow_declares_dynamic_catalog_capability_without_binding_startup_catalog(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "dynamic-catalog.json"
    first_catalog = [ContractInfo("A2612", "DCE", "A", "2026-12-15")]
    second_catalog = [ContractInfo("A2701", "DCE", "A", "2027-01-15")]
    first = ShadowBroker(_CatalogLiveBroker(first_catalog), 500_000, state_path=state_path)
    restarted = ShadowBroker(
        _CatalogLiveBroker(second_catalog),
        500_000,
        state_path=state_path,
    )
    envelope = json.loads(state_path.read_text(encoding="utf-8"))

    assert first.get_contract_catalog() == first_catalog
    assert restarted.get_contract_catalog() == second_catalog
    assert envelope["configuration"]["dynamic_catalog"] is True
    assert envelope["configuration"]["contract_catalog"] is None


def test_static_and_shadow_contract_spec_changes_fail_closed(tmp_path: Path) -> None:
    static_path = tmp_path / "static-specs.json"
    SimBroker(500_000, {"A2612": _spec()}, state_path=static_path)
    with pytest.raises(RuntimeError, match="(configuration|contract spec) mismatch"):
        SimBroker(
            500_000,
            {"A2612": _spec(multiplier=100)},
            state_path=static_path,
        )

    shadow_path = tmp_path / "shadow-specs.json"
    shadow = ShadowBroker(
        _LiveBroker(),
        500_000,
        slippage_ticks=0,
        latency_ticks=0,
        market_impact_ticks=0,
        state_path=shadow_path,
    )
    shadow.update_specs({"A2612": _spec()})
    shadow.start()
    shadow.publish_tick(_tick())
    shadow.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 101.0))

    restarted = ShadowBroker(
        _LiveBroker(),
        500_000,
        slippage_ticks=0,
        latency_ticks=0,
        market_impact_ticks=0,
        state_path=shadow_path,
    )
    with pytest.raises(RuntimeError, match="contract spec mismatch"):
        restarted.update_specs({"A2612": _spec(open_fixed=20.0)})


def test_persistent_fixed_specs_reject_addition_before_any_state_write(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "fixed-spec-addition.json"
    broker = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    before = {path.name: path.read_bytes() for path in sorted(tmp_path.iterdir()) if path.is_file()}
    added = ContractSpec("M2701", "DCE", 10, 1, 0.1, 0.1)

    with pytest.raises(RuntimeError, match="fixed persistent contract specs"):
        broker.update_specs({"M2701": added})

    after = {path.name: path.read_bytes() for path in sorted(tmp_path.iterdir()) if path.is_file()}
    assert after == before
    restarted = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    assert set(restarted.specs) == {"A2612"}


def test_persistent_shadow_requires_nonempty_authoritative_account_identity(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="non-empty account identity"):
        ShadowBroker(_LiveBroker(identity=""), 500_000, state_path=tmp_path / "shadow.json")


def test_rechecksummed_accounting_tampering_fails_closed(tmp_path: Path) -> None:
    state_path = tmp_path / "accounting-tamper.json"
    broker = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    broker.start()
    broker.publish_tick(_tick())
    broker.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 101.0))
    envelope = json.loads(state_path.read_text(encoding="utf-8"))
    envelope["state"]["balance"] += 1.0
    _resign(envelope)
    state_path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(RuntimeError, match="accounting mismatch"):
        SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)


def test_rechecksummed_trade_order_identity_tampering_fails_closed(tmp_path: Path) -> None:
    state_path = tmp_path / "trade-tamper.json"
    broker = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    broker.start()
    broker.publish_tick(_tick())
    broker.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 101.0))
    envelope = json.loads(state_path.read_text(encoding="utf-8"))
    envelope["state"]["trades"][0]["side"] = OrderSide.SELL.value
    _resign(envelope)
    state_path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(RuntimeError, match="trade/order identity mismatch"):
        SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)


def test_corrupt_previous_generation_fails_closed_instead_of_being_ignored(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "previous.json"
    broker = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    broker.start()
    broker.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 90.0))
    state_path.with_name(f"{state_path.name}.prev").write_text("{broken", encoding="utf-8")

    with pytest.raises(RuntimeError, match="previous.*JSON"):
        SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)


def test_previous_generation_must_be_present_and_cryptographically_linked(
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "missing-previous.json"
    missing = SimBroker(500_000, {"A2612": _spec()}, state_path=missing_path)
    missing.start()
    missing.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 90.0))
    missing_path.with_name(f"{missing_path.name}.prev").unlink()

    with pytest.raises(RuntimeError, match="previous.*missing"):
        SimBroker(500_000, {"A2612": _spec()}, state_path=missing_path)

    linked_path = tmp_path / "linked-previous.json"
    linked = SimBroker(500_000, {"A2612": _spec()}, state_path=linked_path)
    linked.start()
    linked.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 90.0))
    previous_path = linked_path.with_name(f"{linked_path.name}.prev")
    previous = json.loads(previous_path.read_text(encoding="utf-8"))
    previous["operation"] = "different-but-valid-predecessor"
    _resign(previous)
    previous_path.write_text(json.dumps(previous), encoding="utf-8")

    with pytest.raises(RuntimeError, match="predecessor checksum"):
        SimBroker(500_000, {"A2612": _spec()}, state_path=linked_path)


def test_rechecksummed_impossible_order_status_fails_closed(tmp_path: Path) -> None:
    state_path = tmp_path / "impossible-status.json"
    broker = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    broker.start()
    broker.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 90.0))
    envelope = json.loads(state_path.read_text(encoding="utf-8"))
    envelope["state"]["orders"][0]["status"] = OrderStatus.REJECTED.value
    _resign(envelope)
    state_path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(RuntimeError, match="order status"):
        SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)


def test_rechecksummed_nonadjacent_settlement_id_fails_closed(tmp_path: Path) -> None:
    state_path = tmp_path / "settlement-tamper.json"
    broker = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    broker.publish_tick(_tick())
    broker.synchronize_trading_day("20260826")
    envelope = json.loads(state_path.read_text(encoding="utf-8"))
    envelope["state"]["settlement_id"] = 9
    _resign(envelope)
    state_path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(RuntimeError, match="settlement.*adjacent"):
        SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)

    envelope["state"]["settlement_id"] = 1
    envelope["state"]["previous_settlement_equity"] += 1.0
    _resign(envelope)
    state_path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(RuntimeError, match="previous settlement equity mismatch"):
        SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)


def test_stale_competing_writer_cannot_replace_newer_broker_truth(tmp_path: Path) -> None:
    state_path = tmp_path / "competing.json"
    first = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    stale = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    first.start()
    first.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 90.0))
    stale.start()

    with pytest.raises(RuntimeError, match="competing simulated-broker writer"):
        stale.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 90.0))

    restarted = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    assert [order.order_id for order in restarted.get_orders()] == ["SIM-1"]


def test_multiprocess_writers_use_locked_compare_and_swap(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "multiprocess-cas.json"
    SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    context = get_context("spawn")
    ready = context.Queue()
    release = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_multiprocess_writer,
            args=(str(state_path), ready, release, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    assert [ready.get(timeout=10) for _ in processes] == ["ready", "ready"]

    release.set()
    outcomes = [results.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert sorted(outcome[0] for outcome in outcomes) == ["error", "ok"]
    error = next(message for status, message in outcomes if status == "error")
    assert "competing simulated-broker writer" in error
    restarted = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    assert [order.order_id for order in restarted.get_orders()] == ["SIM-1"]


def test_lock_unlink_recreate_cannot_split_protocol_writers(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "lock-tamper.json"
    configuration = {"identity": "lock-tamper"}
    store = SimBrokerStateStore(state_path, configuration)
    store.initialize({"value": 0})
    context = get_context("spawn")
    held = context.Queue()
    release = context.Event()
    contender_ready = context.Queue()
    contender_start = context.Event()
    contender_entered = context.Queue()
    results = context.Queue()
    contender = context.Process(
        target=_lock_tamper_contender,
        args=(
            str(state_path),
            contender_ready,
            contender_start,
            contender_entered,
            results,
        ),
    )
    holder = context.Process(
        target=_lock_tamper_holder,
        args=(str(state_path), held, release, results),
    )
    contender.start()
    assert contender_ready.get(timeout=10) == "ready"
    holder.start()
    assert held.get(timeout=10) == "held"

    lock_path = state_path.with_name(f"{state_path.name}.lock")
    original_inode = lock_path.stat().st_ino
    lock_path.unlink()
    lock_path.touch(mode=0o600)
    assert lock_path.stat().st_ino != original_inode
    contender_start.set()
    contender_blocked = False
    try:
        contender_entered.get(timeout=0.25)
    except Empty:
        contender_blocked = True
    finally:
        release.set()
        for process in (holder, contender):
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert contender_blocked is True
    assert holder.exitcode == 0
    assert contender.exitcode == 0
    outcomes = [results.get(timeout=10) for _ in range(2)]

    holder_outcome = next(message for name, message in outcomes if name == "holder")
    contender_outcome = next(message for name, message in outcomes if name == "contender")
    assert "lock identity changed" in holder_outcome
    assert "competing simulated-broker writer" in contender_outcome
    with pytest.raises(Empty):
        contender_entered.get_nowait()


def test_parent_directory_rebind_cannot_overwrite_new_visible_truth(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    state_path = runtime_dir / "broker.json"
    store = SimBrokerStateStore(state_path, {"identity": "directory-rebind"})
    store.initialize({"value": "initial"})
    context = get_context("spawn")
    held = context.Queue()
    release = context.Event()
    results = context.Queue()
    holder = context.Process(
        target=_directory_rebind_holder,
        args=(str(state_path), held, release, results),
    )
    holder.start()
    assert held.get(timeout=10) == "held"

    rebound_dir = tmp_path / "runtime-old"
    runtime_dir.rename(rebound_dir)
    runtime_dir.mkdir()
    state_path.write_bytes((rebound_dir / "broker.json").read_bytes())
    contender = context.Process(
        target=_directory_rebind_contender,
        args=(str(state_path), results),
    )
    contender.start()
    contender_result = results.get(timeout=10)
    assert contender_result == ("contender", "ok")
    contender.join(timeout=10)
    assert contender.exitcode == 0
    visible_truth = state_path.read_bytes()

    release.set()
    holder_result = results.get(timeout=10)
    holder.join(timeout=10)
    assert holder.exitcode == 0
    assert holder_result[0] == "holder"
    assert "lock identity changed" in holder_result[1]
    assert state_path.read_bytes() == visible_truth
    visible = SimBrokerStateStore(state_path, {"identity": "directory-rebind"})
    record = visible.load()
    assert record is not None
    assert record.operation is None
    assert record.state == {"value": "contender"}


def test_market_only_ticks_do_not_rewrite_durable_broker_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "market-only.json"
    broker = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    real_replace = SimBrokerStateStore._replace_at
    replacements = 0

    def count_replacements(
        directory_descriptor: int,
        source_name: str,
        target: Path,
    ) -> None:
        nonlocal replacements
        replacements += 1
        real_replace(directory_descriptor, source_name, target)

    monkeypatch.setattr(
        SimBrokerStateStore,
        "_replace_at",
        staticmethod(count_replacements),
    )
    for minute in range(20):
        broker.publish_tick(_tick(minute=minute))
    assert replacements == 0

    broker.start()
    broker.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 90.0))
    assert replacements > 0


def test_raw_tick_observer_runs_before_any_durable_store_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "raw-observer.json"
    broker = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    broker.start()
    broker.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 90.0))
    events: list[str] = []

    class Observer:
        def observe_raw_tick(self, _tick: Tick, _contract: object) -> None:
            events.append("observer")

    broker.set_raw_tick_observer(Observer())
    real_replace = SimBrokerStateStore._replace_at

    def record_replace(
        directory_descriptor: int,
        source_name: str,
        target: Path,
    ) -> None:
        events.append("disk")
        real_replace(directory_descriptor, source_name, target)

    with broker.market_batch(trading_day="20260825"):
        monkeypatch.setattr(
            SimBrokerStateStore,
            "_replace_at",
            staticmethod(record_replace),
        )
        broker.publish_tick(_tick())
        assert events == ["observer"]
        monkeypatch.setattr(
            SimBrokerStateStore,
            "_replace_at",
            staticmethod(real_replace),
        )


def test_failed_begin_replace_preserves_last_complete_truth_for_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "failed-begin.json"
    broker = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    broker.start()
    broker.publish_tick(_tick())
    real_replace = SimBrokerStateStore._replace_at
    failed = False

    def fail_begin_current(
        directory_descriptor: int,
        source_name: str,
        target: Path,
    ) -> None:
        nonlocal failed
        if Path(target) == state_path and not failed:
            failed = True
            raise OSError("simulated crash while publishing pending marker")
        real_replace(directory_descriptor, source_name, target)

    monkeypatch.setattr(
        SimBrokerStateStore,
        "_replace_at",
        staticmethod(fail_begin_current),
    )
    with pytest.raises(OSError, match="pending marker"):
        broker.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 90.0))
    monkeypatch.setattr(
        SimBrokerStateStore,
        "_replace_at",
        staticmethod(real_replace),
    )

    restarted = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    assert restarted.get_orders() == []


def test_interrupted_mutation_leaves_pending_state_that_restart_rejects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "pending.json"
    broker = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    broker.start()
    broker.publish_tick(_tick())
    real_replace = SimBrokerStateStore._replace_at
    current_replaces = 0

    def fail_completion_replace(
        directory_descriptor: int,
        source_name: str,
        target: Path,
    ) -> None:
        nonlocal current_replaces
        if Path(target) == state_path:
            current_replaces += 1
            if current_replaces == 2:
                raise OSError("simulated crash before completion checkpoint")
        real_replace(directory_descriptor, source_name, target)

    monkeypatch.setattr(
        SimBrokerStateStore,
        "_replace_at",
        staticmethod(fail_completion_replace),
    )
    with pytest.raises(OSError, match="simulated crash"):
        broker.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 101.0))
    monkeypatch.setattr(
        SimBrokerStateStore,
        "_replace_at",
        staticmethod(real_replace),
    )

    with pytest.raises(RuntimeError, match="incomplete broker operation"):
        SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)


@pytest.mark.parametrize(
    "fault_stage",
    ["file_fsync", "current_replace", "directory_fsync"],
)
def test_initial_state_atomic_faults_recover_only_verified_clean_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_stage: str,
) -> None:
    state_path = tmp_path / f"initialize-{fault_stage}.json"
    real_fsync = os.fsync
    real_replace = SimBrokerStateStore._replace_at
    failed = False

    def fail_fsync(fd: int) -> None:
        nonlocal failed
        mode = os.fstat(fd).st_mode
        kind = "directory_fsync" if stat.S_ISDIR(mode) else "file_fsync"
        if kind == fault_stage and not failed:
            failed = True
            if kind == "directory_fsync":
                real_fsync(fd)
            raise OSError(f"fault at initialization {fault_stage}")
        real_fsync(fd)

    def fail_replace(
        directory_descriptor: int,
        source_name: str,
        target: Path,
    ) -> None:
        nonlocal failed
        if fault_stage == "current_replace" and Path(target) == state_path and not failed:
            failed = True
            raise OSError("fault at initialization current_replace")
        real_replace(directory_descriptor, source_name, target)

    monkeypatch.setattr(os, "fsync", fail_fsync)
    monkeypatch.setattr(
        SimBrokerStateStore,
        "_replace_at",
        staticmethod(fail_replace),
    )
    with pytest.raises(OSError, match="fault at initialization"):
        SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    assert failed is True

    monkeypatch.setattr(os, "fsync", real_fsync)
    monkeypatch.setattr(
        SimBrokerStateStore,
        "_replace_at",
        staticmethod(real_replace),
    )
    restarted = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    assert restarted.get_orders() == []
    assert json.loads(state_path.read_text(encoding="utf-8"))["operation"] is None


@pytest.mark.parametrize(
    ("failed_fsync", "restart_outcome"),
    [
        (1, "clean_before"),
        (2, "clean_before"),
        (3, "clean_before"),
        (4, "pending"),
        (5, "pending"),
        (6, "pending"),
        (7, "pending"),
        (8, "clean_after"),
    ],
)
def test_main_operation_file_and_directory_fsync_fault_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_fsync: int,
    restart_outcome: str,
) -> None:
    state_path = tmp_path / f"main-fsync-{failed_fsync}.json"
    broker = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    broker.start()
    real_fsync = os.fsync
    calls: list[str] = []

    def fail_selected_fsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        calls.append("directory" if stat.S_ISDIR(mode) else "file")
        if len(calls) == failed_fsync:
            if restart_outcome == "clean_after":
                real_fsync(fd)
            raise OSError(f"fault at fsync {failed_fsync}")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_selected_fsync)
    with pytest.raises(OSError, match=f"fsync {failed_fsync}"):
        broker.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 90.0))
    assert (
        calls[: min(failed_fsync, 8)]
        == [
            "file",
            "directory",
            "file",
            "directory",
            "file",
            "directory",
            "file",
            "directory",
        ][:failed_fsync]
    )
    with pytest.raises(RuntimeError, match="persistence is faulted"):
        broker.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 90.0))

    monkeypatch.setattr(os, "fsync", real_fsync)
    if restart_outcome == "pending":
        with pytest.raises(RuntimeError, match="incomplete broker operation"):
            SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    else:
        restarted = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
        expected_ids = ["SIM-1"] if restart_outcome == "clean_after" else []
        assert [order.order_id for order in restarted.get_orders()] == expected_ids


@pytest.mark.parametrize(
    ("target_kind", "occurrence", "restart_outcome"),
    [
        ("previous", 1, "clean_before"),
        ("current", 1, "clean_before"),
        ("previous", 2, "pending"),
        ("current", 2, "pending"),
    ],
)
def test_main_operation_previous_and_current_replace_fault_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_kind: str,
    occurrence: int,
    restart_outcome: str,
) -> None:
    state_path = tmp_path / f"main-replace-{target_kind}-{occurrence}.json"
    previous_path = state_path.with_name(f"{state_path.name}.prev")
    broker = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    broker.start()
    target_path = previous_path if target_kind == "previous" else state_path
    real_replace = SimBrokerStateStore._replace_at
    matching_replaces = 0

    def fail_selected_replace(
        directory_descriptor: int,
        source_name: str,
        target: Path,
    ) -> None:
        nonlocal matching_replaces
        if Path(target) == target_path:
            matching_replaces += 1
            if matching_replaces == occurrence:
                raise OSError(f"fault at {target_kind} replace {occurrence}")
        real_replace(directory_descriptor, source_name, target)

    monkeypatch.setattr(
        SimBrokerStateStore,
        "_replace_at",
        staticmethod(fail_selected_replace),
    )
    with pytest.raises(OSError, match=f"{target_kind} replace {occurrence}"):
        broker.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 90.0))
    monkeypatch.setattr(
        SimBrokerStateStore,
        "_replace_at",
        staticmethod(real_replace),
    )

    if restart_outcome == "pending":
        with pytest.raises(RuntimeError, match="incomplete broker operation"):
            SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
    else:
        restarted = SimBroker(500_000, {"A2612": _spec()}, state_path=state_path)
        assert restarted.get_orders() == []


@pytest.mark.parametrize(
    ("failed_fsync", "restart_outcome"),
    [
        (1, "clean_before"),
        (3, "clean_before"),
        (4, "pending_main"),
        (7, "pending_main"),
        (8, "clean_after"),
        (9, "clean_after"),
        (11, "clean_after"),
        (12, "pending_market"),
        (15, "pending_market"),
        (16, "clean_after"),
    ],
)
def test_active_market_batch_begin_complete_and_rebind_fsync_fault_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_fsync: int,
    restart_outcome: str,
) -> None:
    state_path = tmp_path / f"active-market-fsync-{failed_fsync}.json"
    broker = SimBroker(
        500_000,
        {"A2612": _spec()},
        conservative=True,
        latency_ticks=10,
        state_path=state_path,
    )
    broker.start()
    broker.publish_tick(_tick())
    broker.send_order(OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 110.0))
    real_fsync = os.fsync
    fsync_count = 0

    def fail_selected_fsync(fd: int) -> None:
        nonlocal fsync_count
        os.fstat(fd)
        fsync_count += 1
        if fsync_count == failed_fsync:
            if restart_outcome == "clean_after":
                real_fsync(fd)
            raise OSError(f"fault at active market fsync {failed_fsync}")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_selected_fsync)
    with pytest.raises(OSError, match=f"market fsync {failed_fsync}"):
        with broker.market_batch(trading_day="20260825"):
            broker.publish_tick(_tick(minute=1, last=110.0))
    assert fsync_count == failed_fsync
    monkeypatch.setattr(os, "fsync", real_fsync)

    if restart_outcome == "pending_main":
        with pytest.raises(RuntimeError, match="incomplete broker operation"):
            SimBroker(
                500_000,
                {"A2612": _spec()},
                conservative=True,
                latency_ticks=10,
                state_path=state_path,
            )
    elif restart_outcome == "pending_market":
        with pytest.raises(RuntimeError, match="incomplete broker market operation"):
            SimBroker(
                500_000,
                {"A2612": _spec()},
                conservative=True,
                latency_ticks=10,
                state_path=state_path,
            )
    else:
        restarted = SimBroker(
            500_000,
            {"A2612": _spec()},
            conservative=True,
            latency_ticks=10,
            state_path=state_path,
        )
        expected_tick_sequence = 2 if restart_outcome == "clean_after" else 1
        assert restarted._tick_seq == expected_tick_sequence


@pytest.mark.parametrize(
    ("failed_fsync", "restart_outcome"),
    [
        (1, "clean_before"),
        (2, "clean_before"),
        (3, "clean_before"),
        (4, "pending_market"),
        (5, "pending_market"),
        (6, "pending_market"),
        (7, "pending_market"),
        (8, "clean_after"),
    ],
)
def test_market_only_batch_begin_and_complete_fsync_fault_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_fsync: int,
    restart_outcome: str,
) -> None:
    state_path = tmp_path / f"market-only-fsync-{failed_fsync}.json"
    live = _BatchLiveBroker(events=[BrokerEvent("tick", _tick())])
    broker = ShadowBroker(live, 500_000, state_path=state_path)
    broker.update_specs({"A2612": _spec()})
    broker.get_account()
    real_fsync = os.fsync
    fsync_count = 0

    def fail_selected_fsync(fd: int) -> None:
        nonlocal fsync_count
        os.fstat(fd)
        fsync_count += 1
        if fsync_count == failed_fsync:
            if restart_outcome == "clean_after":
                real_fsync(fd)
            raise OSError(f"fault at market-only fsync {failed_fsync}")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_selected_fsync)
    with pytest.raises(OSError, match=f"market-only fsync {failed_fsync}"):
        broker.poll_events()
    assert fsync_count == failed_fsync
    with pytest.raises(RuntimeError, match="persistence is faulted"):
        broker.poll_events()
    monkeypatch.setattr(os, "fsync", real_fsync)

    if restart_outcome == "pending_market":
        with pytest.raises(RuntimeError, match="incomplete broker market operation"):
            ShadowBroker(_BatchLiveBroker(), 500_000, state_path=state_path)
    else:
        restarted = ShadowBroker(
            _BatchLiveBroker(),
            500_000,
            state_path=state_path,
        )
        expected_tick_sequence = 1 if restart_outcome == "clean_after" else 0
        assert restarted.sim._tick_seq == expected_tick_sequence
