from __future__ import annotations

from datetime import datetime
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

    class Broker:
        def __init__(self):
            self.log = []

        def get_contract_catalog(self):
            return catalog

        def get_trading_day(self):
            return "20260825"

        def set_raw_tick_observer(self, observer):
            self.log.append(("observer", observer))

        def subscribe(self, symbol, exchange):
            self.log.append(("subscribe", symbol, exchange))

    class Observer:
        def __init__(self):
            self.expected = None
            self.checkpoints = 0

        def set_expected_contracts(self, day, contracts):
            self.expected = (day, tuple(contracts))

        def checkpoint(self):
            self.checkpoints += 1

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
    assert observer.checkpoints == 1


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
