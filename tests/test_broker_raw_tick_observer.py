from __future__ import annotations

from datetime import datetime
from threading import Event, Thread
from zoneinfo import ZoneInfo

from afuture.broker.shadow import ShadowBroker
from afuture.broker.sim import SimBroker
from afuture.models import ContractInfo, ContractSpec, Tick

_CHINA = ZoneInfo("Asia/Shanghai")


def _tick() -> Tick:
    return Tick(
        "A2612",
        "DCE",
        datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA),
        99.0,
        101.0,
        100.0,
        10.0,
        10.0,
        "20260825",
        volume=10.0,
        open_interest=100.0,
    )


def test_sim_broker_injects_raw_observer_before_normal_tick_event_path():
    contract = ContractInfo("A2612", "DCE", "A", "2026-12-15")
    broker = SimBroker(
        500_000,
        {"A2612": ContractSpec("A2612", "DCE", 10, 1, 0.1, 0.1)},
        contract_catalog=[contract],
    )
    calls = []

    class Observer:
        def observe_raw_tick(self, tick, metadata):
            assert broker._events == []
            calls.append((tick, metadata))

    broker.set_raw_tick_observer(Observer())
    broker.publish_tick(_tick())

    assert calls == [(_tick(), contract)]
    assert [event.event_type for event in broker.poll_events()] == ["tick"]


def test_sim_broker_exposes_injected_market_connection_generation():
    broker = SimBroker(
        500_000,
        {"A2612": ContractSpec("A2612", "DCE", 10, 1, 0.1, 0.1)},
    )
    states = []

    class Observer:
        def note_raw_market_connection(self, *, connected, generation):
            states.append((connected, generation))

        def observe_raw_tick(self, tick, metadata):
            del tick, metadata

    broker.set_raw_tick_observer(Observer())
    broker.start()
    broker.stop()

    assert states == [(True, 1), (False, 1)]


def test_sim_lifecycle_fence_blocks_market_account_mutation():
    broker = SimBroker(
        500_000,
        {"A2612": ContractSpec("A2612", "DCE", 10, 1, 0.1, 0.1)},
    )
    broker.start()
    entered = Event()
    completed = Event()

    def publish() -> None:
        entered.set()
        broker.publish_tick(_tick())
        completed.set()

    with broker.lifecycle_state_commit_fence():
        worker = Thread(target=publish)
        worker.start()
        assert entered.wait(1.0)
        assert completed.wait(0.05) is False
    assert completed.wait(1.0)
    worker.join(timeout=1.0)


def test_sim_broker_keeps_tick_flowing_when_observer_marks_market_evidence_incomplete():
    from afuture.broker.base import RawMarketEvidenceError

    broker = SimBroker(
        500_000,
        {"A2612": ContractSpec("A2612", "DCE", 10, 1, 0.1, 0.1)},
    )

    class IncompleteObserver:
        def observe_raw_tick(self, tick, metadata):
            del tick, metadata
            raise RawMarketEvidenceError("supported OI metadata is incomplete")

    broker.set_raw_tick_observer(IncompleteObserver())
    broker.publish_tick(_tick())

    assert [event.event_type for event in broker.poll_events()] == ["tick"]


def test_sim_broker_fails_closed_on_untyped_raw_observer_bug():
    broker = SimBroker(
        500_000,
        {"A2612": ContractSpec("A2612", "DCE", 10, 1, 0.1, 0.1)},
    )

    class BrokenObserver:
        def observe_raw_tick(self, tick, metadata):
            del tick, metadata
            raise AssertionError("observer bug")

    broker.set_raw_tick_observer(BrokenObserver())
    broker.publish_tick(_tick())

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert "raw Tick observer failed" in str(events[0].payload)


def test_sim_broker_turns_fatal_market_evidence_revision_into_broker_error():
    from afuture.broker.base import RawMarketEvidenceFatalError

    broker = SimBroker(
        500_000,
        {"A2612": ContractSpec("A2612", "DCE", 10, 1, 0.1, 0.1)},
    )

    class FatalObserver:
        def observe_raw_tick(self, tick, metadata):
            del tick, metadata
            raise RawMarketEvidenceFatalError("completed evidence would change")

    broker.set_raw_tick_observer(FatalObserver())
    broker.publish_tick(_tick())

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]


def test_shadow_delegates_raw_observer_to_live_market_source_only():
    class Live:
        def __init__(self):
            self.observer = None

        def set_raw_tick_observer(self, observer):
            self.observer = observer

    live = Live()
    shadow = ShadowBroker(live, 500_000)
    observer = object()

    shadow.set_raw_tick_observer(observer)

    assert live.observer is observer


def test_directional_manager_installs_expected_universe_before_subscribing_and_checkpoints():
    from afuture.directional import DirectionalConfig
    from afuture.directional_runtime import DirectionalPortfolioManager
    from afuture.risk import RiskConfig, RiskManager

    catalog = _catalog_for_supported_products()
    catalog[0] = ContractInfo(
        catalog[0].symbol,
        catalog[0].exchange,
        catalog[0].product,
        catalog[0].expiry,
        listing="2026-08-25",
    )
    catalog.append(ContractInfo("A2701", "DCE", "A", "2027-01-15", listing="2026-08-26"))

    class Broker:
        def __init__(self):
            self.log = []
            self.trading_day = "20260825"

        def get_contract_catalog(self):
            return catalog

        def refresh_contract_catalog(self, *, timeout_seconds):
            self.log.append(("refresh", self.trading_day, timeout_seconds))

        def get_trading_day(self):
            return self.trading_day

        def set_raw_tick_observer(self, observer):
            self.log.append(("observer", observer))

        def subscribe(self, symbol, exchange):
            self.log.append(("subscribe", symbol, exchange))

    class Observer:
        def __init__(self):
            self.expected = None
            self.refreshed = None
            self.checkpoints = 0

        def set_expected_contracts(self, day, contracts):
            self.expected = (day, tuple(contracts))

        def checkpoint(self):
            self.checkpoints += 1

        def refresh_contract_catalog(self, day, contracts):
            self.refreshed = (day, tuple(contracts))

    broker = Broker()
    observer = Observer()
    manager = DirectionalPortfolioManager(
        DirectionalConfig(enabled=True, products=tuple(item.product for item in catalog)),
        broker,
        RiskManager(RiskConfig()),
        raw_tick_observer=observer,
    )

    manager.bootstrap(datetime(2026, 8, 24, 20, 30, tzinfo=_CHINA))
    manager.checkpoint_oi_evidence()

    assert observer.expected == ("20260825", tuple(catalog))
    assert broker.log[0] == ("observer", observer)
    assert all(item[0] == "subscribe" for item in broker.log[1:])
    assert all(item[1] != "A2701" for item in broker.log[1:])
    assert observer.checkpoints == 1

    broker.log.clear()
    broker.trading_day = "20260826"
    manager.checkpoint_oi_evidence()

    # The order-capable run_once checkpoint is persistence-only: catalog refresh and
    # subscriptions belong to the dedicated non-order-capable maintenance worker.
    assert observer.refreshed is None
    assert broker.log == []
    manager._maintain_market_subscriptions_once()

    assert observer.refreshed == ("20260826", tuple(catalog))
    assert broker.log == [
        ("refresh", "20260826", manager.metadata_timeout_seconds),
        ("subscribe", "A2701", "DCE"),
    ]
    assert observer.checkpoints == 2


def _catalog_for_supported_products() -> list[ContractInfo]:
    from afuture.directional_sessions import PRODUCT_SESSION_MANIFEST

    products = ("A", "C", "EG", "I", "M", "P", "PP", "TA", "Y")
    return [
        ContractInfo(
            f"{product}2612",
            PRODUCT_SESSION_MANIFEST[product].exchange,
            product,
            "2026-12-15",
        )
        for product in products
    ]
