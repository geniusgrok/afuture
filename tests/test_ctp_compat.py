from datetime import datetime, timezone
from threading import Event, Lock, Thread
from types import SimpleNamespace

import pytest

from afuture.broker.ctp import CtpBroker, CtpCredentials, build_ctp_setting
from afuture.models import (
    ContractInfo,
    ContractPosition,
    Offset,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
)


def credentials():
    return CtpCredentials("user", "secret", "9999", "tcp://td", "tcp://md", "app", "auth", "test")


def raw_tick(
    symbol: str = "cu2609",
    exchange: str = "SHFE",
    *,
    price: float = 70_000.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        exchange=SimpleNamespace(value=exchange),
        datetime=datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc),
        bid_price_1=price - 1,
        ask_price_1=price + 1,
        last_price=price,
        bid_volume_1=10,
        ask_volume_1=10,
        limit_up=80_000,
        limit_down=60_000,
        volume=100,
        open_interest=200,
    )


def test_ctp_setting_matches_gateway_contract():
    setting = build_ctp_setting(credentials())
    assert (
        setting["用户名"] == "user"
        and setting["经纪商代码"] == "9999"
        and setting["柜台环境"] == "测试"
    )


def test_ctp_maps_internal_order_with_fake_runtime():
    broker = CtpBroker(credentials())

    class EnumValue:
        def __init__(self, value):
            self.value = value

    class Exchange:
        DCE = EnumValue("DCE")

    class Direction:
        LONG = "LONG"
        SHORT = "SHORT"

    class VOffset:
        OPEN = "OPEN"
        CLOSE = "CLOSE"
        CLOSETODAY = "CLOSETODAY"
        CLOSEYESTERDAY = "CLOSEYESTERDAY"

    class VType:
        LIMIT = "LIMIT"
        FAK = "FAK"
        FOK = "FOK"

    class Req:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    broker._runtime = {
        "Exchange": Exchange,
        "Direction": Direction,
        "Offset": VOffset,
        "OrderType": VType,
        "OrderRequest": Req,
    }
    req = broker._to_vnpy_order(
        OrderRequest("m2609", "DCE", OrderSide.SELL, Offset.CLOSE, 2, 3000, OrderType.FAK, "pair")
    )
    assert (req.symbol, req.direction, req.offset, req.type) == ("m2609", "SHORT", "CLOSE", "FAK")


def test_ctp_account_margin_proxy_and_position_snapshot():
    broker = CtpBroker(credentials())
    broker._trading_day = "20260821"
    account = broker._convert_account(
        SimpleNamespace(balance=500000, available=350000, frozen=20000)
    )
    assert account.margin == 150000
    raw = [
        SimpleNamespace(
            symbol="m2609",
            exchange=SimpleNamespace(value="DCE"),
            direction=SimpleNamespace(name="LONG"),
            volume=3,
            yd_volume=1,
            price=3000.0,
        ),
        SimpleNamespace(
            symbol="m2609",
            exchange=SimpleNamespace(value="DCE"),
            direction=SimpleNamespace(name="SHORT"),
            volume=2,
            yd_volume=2,
            price=3010.0,
        ),
    ]
    broker._handle_position_snapshot(raw)
    p = broker.get_positions()[0]
    assert (p.long_today, p.long_yesterday, p.short_today, p.short_yesterday) == (2, 1, 0, 2)
    assert broker.poll_events()[0].event_type == "position_snapshot"


def test_ctp_critical_event_precedes_coalesced_tick_flood() -> None:
    broker = CtpBroker(credentials(), max_events_per_poll=2)
    broker._trading_day = "20260825"
    for index in range(500):
        broker._on_tick(SimpleNamespace(data=raw_tick(price=70_000 + index)))
    broker._on_account(SimpleNamespace(data=SimpleNamespace(balance=500_000, available=400_000)))

    events = broker.poll_events()

    assert [event.event_type for event in events] == ["account", "tick"]
    assert events[1].payload.last_price == 70_499


def test_ctp_poll_is_bounded_and_coalesces_latest_tick_per_contract() -> None:
    broker = CtpBroker(credentials(), max_events_per_poll=2)
    broker._trading_day = "20260825"
    broker._on_tick(SimpleNamespace(data=raw_tick("same", "DCE", price=100)))
    broker._on_tick(SimpleNamespace(data=raw_tick("same", "DCE", price=101)))
    broker._on_tick(SimpleNamespace(data=raw_tick("same", "SHFE", price=200)))
    broker._on_tick(SimpleNamespace(data=raw_tick("other", "DCE", price=300)))

    first = broker.poll_events()
    second = broker.poll_events()
    counters = broker.delivery_counters()

    assert len(first) == 2
    assert [
        (event.payload.symbol, event.payload.exchange, event.payload.last_price) for event in first
    ] == [
        ("same", "DCE", 101),
        ("same", "SHFE", 200),
    ]
    assert [(event.payload.symbol, event.payload.exchange) for event in second] == [
        ("other", "DCE")
    ]
    assert counters == {
        "critical_enqueued": 0,
        "ticks_received": 4,
        "ticks_coalesced": 1,
        "critical_delivered": 0,
        "ticks_delivered": 3,
        "critical_backlog": 0,
        "tick_backlog": 0,
    }


def test_ctp_raw_observer_sees_every_tick_before_manager_coalescing() -> None:
    broker = CtpBroker(credentials(), max_events_per_poll=2)
    broker._trading_day = "20260825"
    contract = ContractInfo("A2612", "DCE", "A", "2026-12-15")
    broker._contract_catalog[contract.symbol] = contract
    observed = []

    class Observer:
        def observe_raw_tick(self, tick, metadata):
            assert broker.delivery_counters()["ticks_received"] == len(observed)
            observed.append((tick.last_price, metadata))

    broker.set_raw_tick_observer(Observer())
    for index in range(1_000):
        broker._on_tick(SimpleNamespace(data=raw_tick("A2612", "DCE", price=100.0 + index)))
    broker._on_account(SimpleNamespace(data=SimpleNamespace(balance=500_000, available=400_000)))

    events = broker.poll_events()
    assert len(observed) == 1_000
    assert all(metadata == contract for _price, metadata in observed)
    assert [event.event_type for event in events] == ["account", "tick"]
    assert events[-1].payload.last_price == 1_099.0
    assert broker.delivery_counters()["ticks_coalesced"] == 999


def test_ctp_raw_observer_failure_is_critical_and_tick_is_not_delivered() -> None:
    broker = CtpBroker(credentials())
    broker._trading_day = "20260825"

    class Observer:
        def observe_raw_tick(self, tick, metadata):
            raise RuntimeError("injected evidence failure")

    broker.set_raw_tick_observer(Observer())
    broker._on_tick(SimpleNamespace(data=raw_tick("A2612", "DCE", price=100.0)))

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert "injected evidence failure" in str(events[0].payload)
    assert broker.delivery_counters()["ticks_received"] == 0


@pytest.mark.parametrize("trading_day", ["", "20260230", "2026-08-25"])
def test_ctp_rejects_missing_or_invalid_authoritative_trading_day(trading_day: str) -> None:
    broker = CtpBroker(credentials())
    broker._trading_day = trading_day

    with pytest.raises(RuntimeError, match="trading day"):
        broker.get_trading_day()


def test_ctp_callback_surfaces_missing_trading_day_without_raising() -> None:
    broker = CtpBroker(credentials())

    broker._on_tick(SimpleNamespace(data=raw_tick()))

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert "trading day" in str(events[0].payload)


@pytest.mark.parametrize(
    "gateway",
    [
        None,
        SimpleNamespace(td_api=None),
        SimpleNamespace(td_api=SimpleNamespace()),
        SimpleNamespace(td_api=SimpleNamespace(getTradingDay=lambda: "")),
    ],
    ids=["missing-gateway", "missing-td-api", "missing-getter", "empty-value"],
)
def test_started_ctp_rejects_missing_current_trading_day_source(gateway: object) -> None:
    broker = CtpBroker(credentials())
    broker._trading_day = "20260824"
    broker._main_engine = SimpleNamespace(get_gateway=lambda _gateway_name: gateway)

    with pytest.raises(RuntimeError, match="trading day"):
        broker.get_trading_day()


@pytest.mark.parametrize("field", ["balance", "available"])
def test_ctp_account_callback_rejects_non_finite_truth_without_mutation(field: str) -> None:
    broker = CtpBroker(credentials())
    broker._trading_day = "20260821"
    broker._last_account = broker._convert_account(
        SimpleNamespace(balance=500000, available=350000)
    )
    before = broker._last_account
    raw = SimpleNamespace(balance=500000, available=350000)
    setattr(raw, field, float("nan"))

    broker._on_account(SimpleNamespace(data=raw))

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["account_error"]
    assert broker._last_account == before
    assert broker._account_event_generation == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("direction", SimpleNamespace(name="NOT_LONG")),
        ("volume", 1.5),
        ("volume", True),
        ("yd_volume", 1.5),
        ("price", float("nan")),
    ],
)
def test_ctp_position_snapshot_rejects_invalid_truth_without_mutation(
    field: str,
    value: object,
) -> None:
    broker = CtpBroker(credentials())
    broker._positions = {"m2609": ContractPosition("m2609", "DCE", long_today=1, long_price=3000.0)}
    before = broker.get_positions()
    raw = SimpleNamespace(
        symbol="m2609",
        exchange=SimpleNamespace(value="DCE"),
        direction=SimpleNamespace(name="LONG"),
        volume=2,
        yd_volume=1,
        price=3000.0,
    )
    setattr(raw, field, value)

    broker._handle_position_snapshot([raw])

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert broker.get_positions() == before
    assert broker._position_snapshot_generation == 0


def test_ctp_snapshot_generation_order_ownership_and_health():
    broker = CtpBroker(credentials(), snapshot_stale_seconds=20)
    broker._last_account = object()
    broker._account_event_generation = 4
    broker._position_snapshot_generation = 7
    marker = broker.snapshot_marker()
    assert not broker.snapshot_ready(marker)
    broker._account_event_generation += 1
    assert not broker.snapshot_ready(marker)
    broker._position_snapshot_generation += 1
    assert broker.snapshot_ready(marker)
    broker._order_references["CTP.1"] = "p"
    assert broker.owns_order("CTP.1") and not broker.owns_order("manual")
    broker._last_account_monotonic = 100
    broker._last_position_snapshot_monotonic = 100
    broker.is_ready = lambda: True
    assert broker.health_error(now_monotonic=110) is None
    assert "stale" in broker.health_error(now_monotonic=121)


def conversion_broker() -> tuple[CtpBroker, SimpleNamespace]:
    broker = CtpBroker(credentials())
    status = SimpleNamespace(
        SUBMITTING=object(),
        NOTTRADED=object(),
        PARTTRADED=object(),
        ALLTRADED=object(),
        CANCELLED=object(),
        REJECTED=object(),
    )
    broker._runtime = {"Status": status}
    return broker, status


def raw_order(status: object) -> SimpleNamespace:
    return SimpleNamespace(
        vt_orderid="CTP.1",
        symbol="cu2609",
        exchange=SimpleNamespace(value="SHFE"),
        direction=SimpleNamespace(name="LONG"),
        offset=SimpleNamespace(name="OPEN"),
        type=SimpleNamespace(name="LIMIT"),
        status=status,
        volume=2,
        traded=0,
        price=70_000.0,
        reference="pair",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("direction", SimpleNamespace(name="UNKNOWN")),
        ("offset", SimpleNamespace(name="FORCECLOSE")),
        ("type", SimpleNamespace(name="MARKET")),
        ("status", object()),
    ],
)
def test_ctp_order_conversion_rejects_unknown_protocol_value(
    field: str,
    value: object,
) -> None:
    broker, status = conversion_broker()
    raw = raw_order(status.NOTTRADED)
    setattr(raw, field, value)

    with pytest.raises(ValueError, match=field):
        broker._convert_order(raw)


@pytest.mark.parametrize(
    ("status_name", "expected"),
    [
        ("SUBMITTING", OrderStatus.SUBMITTING),
        ("NOTTRADED", OrderStatus.NOT_TRADED),
        ("PARTTRADED", OrderStatus.PART_TRADED),
        ("ALLTRADED", OrderStatus.FILLED),
        ("CANCELLED", OrderStatus.CANCELLED),
        ("REJECTED", OrderStatus.REJECTED),
    ],
)
def test_ctp_order_conversion_preserves_supported_status(
    status_name: str,
    expected: OrderStatus,
) -> None:
    broker, status = conversion_broker()

    order = broker._convert_order(raw_order(getattr(status, status_name)))

    assert order.status is expected
    assert order.request.side is OrderSide.BUY
    assert order.request.offset is Offset.OPEN
    assert order.request.order_type is OrderType.LIMIT


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("volume", 0),
        ("volume", 1.5),
        ("volume", True),
        ("traded", -1),
        ("traded", 3),
        ("price", float("nan")),
    ],
)
def test_ctp_order_conversion_rejects_invalid_economic_value(
    field: str,
    value: object,
) -> None:
    broker, status = conversion_broker()
    raw = raw_order(status.NOTTRADED)
    setattr(raw, field, value)

    with pytest.raises(ValueError, match=field):
        broker._convert_order(raw)


def raw_trade(
    *,
    direction: str = "LONG",
    offset: str = "OPEN",
    trade_id: str = "CTP.T1",
    symbol: str = "cu2609",
    exchange: str = "SHFE",
) -> SimpleNamespace:
    return SimpleNamespace(
        vt_tradeid=trade_id,
        vt_orderid="CTP.1",
        symbol=symbol,
        exchange=SimpleNamespace(value=exchange),
        direction=SimpleNamespace(name=direction),
        offset=SimpleNamespace(name=offset),
        volume=1,
        price=70_000.0,
        datetime=datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize(
    ("direction", "offset", "field"),
    [
        ("UNKNOWN", "OPEN", "direction"),
        ("LONG", "FORCECLOSE", "offset"),
    ],
)
def test_ctp_trade_handler_rejects_unknown_protocol_value_without_mutation(
    direction: str,
    offset: str,
    field: str,
) -> None:
    broker = CtpBroker(credentials())
    broker._positions = {
        "cu2609": ContractPosition(
            "cu2609",
            "SHFE",
            long_today=1,
            long_price=70_000,
        )
    }
    before = broker.get_positions()

    broker._on_trade(SimpleNamespace(data=raw_trade(direction=direction, offset=offset)))

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert field in str(events[0].payload)
    assert broker.get_positions() == before


def test_ctp_order_handler_rejects_unknown_value_as_broker_error() -> None:
    broker, status = conversion_broker()
    raw = raw_order(status.NOTTRADED)
    raw.direction = SimpleNamespace(name="UNKNOWN")

    broker._on_order(SimpleNamespace(data=raw))

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert "direction" in str(events[0].payload)


def test_ctp_order_callback_surfaces_unexpected_conversion_failure() -> None:
    broker = CtpBroker(credentials())

    def fail_conversion(_raw: object) -> None:
        raise RuntimeError("unexpected adapter failure")

    broker._convert_order = fail_conversion

    broker._on_order(SimpleNamespace(data=object()))

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert "unexpected adapter failure" in str(events[0].payload)


def test_ctp_trade_callback_surfaces_unexpected_event_extraction_failure() -> None:
    broker = CtpBroker(credentials())

    class BrokenEvent:
        @property
        def data(self) -> object:
            raise RuntimeError("unexpected trade event failure")

    broker._on_trade(BrokenEvent())

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert "unexpected trade event failure" in str(events[0].payload)


def test_ctp_trade_handler_preserves_supported_trade_and_position_mirror() -> None:
    broker = CtpBroker(credentials())
    broker._trading_day = "20260825"

    broker._on_trade(SimpleNamespace(data=raw_trade()))

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["trade"]
    trade = events[0].payload
    assert trade.side is OrderSide.BUY
    assert trade.offset is Offset.OPEN
    assert broker.get_positions()[0].long_today == 1


def test_ctp_trade_callback_is_idempotent_within_session() -> None:
    broker = CtpBroker(credentials())
    broker._trading_day = "20260825"
    event = SimpleNamespace(data=raw_trade())

    broker._on_trade(event)
    broker._on_trade(event)

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["trade"]
    assert broker.get_positions()[0].long_today == 1


def test_ctp_trade_idempotency_includes_exchange() -> None:
    broker = CtpBroker(credentials())
    broker._trading_day = "20260825"

    broker._on_trade(
        SimpleNamespace(data=raw_trade(trade_id="SHARED-T1", symbol="same", exchange="DCE"))
    )
    broker._on_trade(
        SimpleNamespace(data=raw_trade(trade_id="SHARED-T1", symbol="same", exchange="SHFE"))
    )

    events = broker.poll_events()
    positions = sorted(broker.get_positions(), key=lambda position: position.exchange)
    assert [event.event_type for event in events] == ["trade", "trade"]
    assert [
        (position.symbol, position.exchange, position.long_today) for position in positions
    ] == [
        ("same", "DCE", 1),
        ("same", "SHFE", 1),
    ]


def test_ctp_position_snapshot_keeps_same_symbol_on_distinct_exchanges() -> None:
    broker = CtpBroker(credentials())
    raw_positions = [
        SimpleNamespace(
            symbol="same",
            exchange=SimpleNamespace(value=exchange),
            direction=SimpleNamespace(name="LONG"),
            volume=1,
            yd_volume=0,
            price=price,
        )
        for exchange, price in (("DCE", 100.0), ("SHFE", 200.0))
    ]

    broker._handle_position_snapshot(raw_positions)

    positions = sorted(broker.get_positions(), key=lambda position: position.exchange)
    assert [
        (position.symbol, position.exchange, position.long_price) for position in positions
    ] == [
        ("same", "DCE", 100.0),
        ("same", "SHFE", 200.0),
    ]


def test_ctp_serializes_snapshot_and_trade_position_truth_and_events() -> None:
    broker = CtpBroker(credentials())
    broker._trading_day = "20260825"
    snapshot_conversion_started = Event()
    release_snapshot = Event()
    trade_prelock_reached = Event()
    trade_waiting_on_position = Event()
    trade_finished = Event()

    class ObservablePositionLock:
        """Expose real lock contention without adding a production test hook."""

        def __init__(self) -> None:
            self._lock = Lock()

        def __enter__(self) -> "ObservablePositionLock":
            if not self._lock.acquire(blocking=False):
                trade_waiting_on_position.set()
                self._lock.acquire()
            return self

        def __exit__(self, *_args: object) -> None:
            self._lock.release()

    broker._position_lock = ObservablePositionLock()

    class BlockingDirection:
        @property
        def name(self) -> str:
            snapshot_conversion_started.set()
            release_snapshot.wait()
            return "LONG"

    class CheckpointTradeEvent:
        @property
        def data(self) -> SimpleNamespace:
            trade_prelock_reached.set()
            return raw_trade(
                trade_id="INTERLEAVED-T1",
                symbol="same",
                exchange="DCE",
            )

    snapshot = [
        SimpleNamespace(
            symbol="same",
            exchange=SimpleNamespace(value="DCE"),
            direction=BlockingDirection(),
            volume=1,
            yd_volume=0,
            price=100.0,
        )
    ]

    def process_trade() -> None:
        broker._on_trade(CheckpointTradeEvent())
        trade_finished.set()

    snapshot_thread = Thread(target=broker._handle_position_snapshot, args=(snapshot,))
    trade_thread = Thread(target=process_trade)
    snapshot_thread.start()
    try:
        assert snapshot_conversion_started.wait(1)
        trade_thread.start()
        assert trade_prelock_reached.wait(1)
        assert trade_waiting_on_position.wait(1)
        assert not trade_finished.is_set()
    finally:
        release_snapshot.set()
        snapshot_thread.join(2)
        if trade_thread.ident is not None:
            trade_thread.join(2)

    assert not snapshot_thread.is_alive()
    assert not trade_thread.is_alive()
    assert trade_finished.is_set()
    events = broker.poll_events()
    assert [event.event_type for event in events] == ["position_snapshot", "trade"]
    assert events[0].payload[0].long_today == 1
    position = broker.get_positions()[0]
    assert (position.long_today, position.long_price) == (2, 35_050.0)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("volume", 0),
        ("volume", -1),
        ("price", 0.0),
        ("price", -1.0),
        ("price", float("nan")),
        ("price", float("inf")),
    ],
)
def test_ctp_trade_handler_rejects_invalid_economic_value(
    field: str,
    value: object,
) -> None:
    broker = CtpBroker(credentials())
    raw = raw_trade()
    setattr(raw, field, value)

    broker._on_trade(SimpleNamespace(data=raw))

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["broker_error"]
    assert field in str(events[0].payload)
    assert broker.get_positions() == []
