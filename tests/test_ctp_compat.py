from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from afuture.broker.ctp import CtpBroker, CtpCredentials, build_ctp_setting
from afuture.models import (
    ContractPosition,
    Offset,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
)


def credentials():
    return CtpCredentials("user", "secret", "9999", "tcp://td", "tcp://md", "app", "auth", "test")


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


def raw_trade(*, direction: str = "LONG", offset: str = "OPEN") -> SimpleNamespace:
    return SimpleNamespace(
        vt_tradeid="CTP.T1",
        vt_orderid="CTP.1",
        symbol="cu2609",
        exchange=SimpleNamespace(value="SHFE"),
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


def test_ctp_trade_handler_preserves_supported_trade_and_position_mirror() -> None:
    broker = CtpBroker(credentials())

    broker._on_trade(SimpleNamespace(data=raw_trade()))

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["trade"]
    trade = events[0].payload
    assert trade.side is OrderSide.BUY
    assert trade.offset is Offset.OPEN
    assert broker.get_positions()[0].long_today == 1


def test_ctp_trade_callback_is_idempotent_within_session() -> None:
    broker = CtpBroker(credentials())
    event = SimpleNamespace(data=raw_trade())

    broker._on_trade(event)
    broker._on_trade(event)

    events = broker.poll_events()
    assert [event.event_type for event in events] == ["trade"]
    assert broker.get_positions()[0].long_today == 1


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
