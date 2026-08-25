from datetime import datetime, timezone

import pytest

from afuture.models import ContractPosition, Offset, OrderSide, Trade
from afuture.position import PositionBook

_TRADE_TIME = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)


def make_trade(
    *,
    side: OrderSide,
    offset: Offset,
    volume: int = 1,
    price: float = 70_100.0,
) -> Trade:
    return Trade(
        "trade-1",
        "order-1",
        "cu2609",
        "SHFE",
        side,
        offset,
        volume,
        price,
        _TRADE_TIME,
    )


@pytest.mark.parametrize(
    ("side", "offset", "position", "message"),
    [
        (
            OrderSide.SELL,
            Offset.CLOSE_TODAY,
            ContractPosition("cu2609", "SHFE", long_yesterday=2, long_price=70_000),
            "today long",
        ),
        (
            OrderSide.SELL,
            Offset.CLOSE_YESTERDAY,
            ContractPosition("cu2609", "SHFE", long_today=2, long_price=70_000),
            "yesterday long",
        ),
        (
            OrderSide.BUY,
            Offset.CLOSE_TODAY,
            ContractPosition("cu2609", "SHFE", short_yesterday=2, short_price=70_200),
            "today short",
        ),
        (
            OrderSide.BUY,
            Offset.CLOSE_YESTERDAY,
            ContractPosition("cu2609", "SHFE", short_today=2, short_price=70_200),
            "yesterday short",
        ),
    ],
)
def test_bucket_specific_close_rejects_insufficient_bucket_without_mutation(
    side: OrderSide,
    offset: Offset,
    position: ContractPosition,
    message: str,
) -> None:
    book = PositionBook([position])
    before = book.all()

    with pytest.raises(ValueError, match=message):
        book.apply_trade(make_trade(side=side, offset=offset))

    assert book.all() == before


@pytest.mark.parametrize(
    ("side", "position", "expected_today", "expected_yesterday"),
    [
        (
            OrderSide.SELL,
            ContractPosition("cu2609", "SHFE", long_today=2, long_yesterday=1, long_price=70_000),
            1,
            0,
        ),
        (
            OrderSide.BUY,
            ContractPosition(
                "cu2609", "SHFE", short_today=2, short_yesterday=1, short_price=70_200
            ),
            1,
            0,
        ),
    ],
)
def test_generic_close_consumes_yesterday_before_today(
    side: OrderSide,
    position: ContractPosition,
    expected_today: int,
    expected_yesterday: int,
) -> None:
    book = PositionBook([position])

    book.apply_trade(make_trade(side=side, offset=Offset.CLOSE, volume=2))

    result = book.get("cu2609")
    if side is OrderSide.SELL:
        assert (result.long_today, result.long_yesterday) == (
            expected_today,
            expected_yesterday,
        )
    else:
        assert (result.short_today, result.short_yesterday) == (
            expected_today,
            expected_yesterday,
        )


@pytest.mark.parametrize(
    ("side", "offset", "position"),
    [
        (
            OrderSide.SELL,
            Offset.CLOSE_TODAY,
            ContractPosition("cu2609", "SHFE", long_today=1, long_price=70_000),
        ),
        (
            OrderSide.SELL,
            Offset.CLOSE_YESTERDAY,
            ContractPosition("cu2609", "SHFE", long_yesterday=1, long_price=70_000),
        ),
        (
            OrderSide.BUY,
            Offset.CLOSE_TODAY,
            ContractPosition("cu2609", "SHFE", short_today=1, short_price=70_200),
        ),
        (
            OrderSide.BUY,
            Offset.CLOSE_YESTERDAY,
            ContractPosition("cu2609", "SHFE", short_yesterday=1, short_price=70_200),
        ),
    ],
)
def test_bucket_specific_close_accepts_exact_available_volume(
    side: OrderSide,
    offset: Offset,
    position: ContractPosition,
) -> None:
    book = PositionBook([position])

    book.apply_trade(make_trade(side=side, offset=offset))

    assert book.all() == []


@pytest.mark.parametrize("price", [0.0, -1.0, float("nan"), float("inf")])
@pytest.mark.parametrize(
    ("side", "offset"),
    [
        (OrderSide.BUY, Offset.OPEN),
        (OrderSide.SELL, Offset.CLOSE_TODAY),
    ],
)
def test_trade_rejects_invalid_price_without_mutation(
    price: float,
    side: OrderSide,
    offset: Offset,
) -> None:
    book = PositionBook([ContractPosition("cu2609", "SHFE", long_today=1, long_price=70_000)])
    before = book.all()

    with pytest.raises(ValueError, match="price must be finite and positive"):
        book.apply_trade(make_trade(side=side, offset=offset, price=price))

    assert book.all() == before


@pytest.mark.parametrize(
    "position",
    [
        ContractPosition("cu2609", "SHFE", long_today=-1),
        ContractPosition("cu2609", "SHFE", long_yesterday=-1),
        ContractPosition("cu2609", "SHFE", short_today=-1),
        ContractPosition("cu2609", "SHFE", short_yesterday=-1),
    ],
)
def test_position_book_rejects_negative_initial_bucket(
    position: ContractPosition,
) -> None:
    with pytest.raises(ValueError, match="position buckets cannot be negative"):
        PositionBook([position])


@pytest.mark.parametrize("volume", [True, 1.5])
def test_trade_rejects_non_integer_volume_without_mutation(volume: object) -> None:
    book = PositionBook()
    trade = make_trade(side=OrderSide.BUY, offset=Offset.OPEN)
    object.__setattr__(trade, "volume", volume)

    with pytest.raises(ValueError, match="positive integer"):
        book.apply_trade(trade)

    assert book.all() == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("side", "BUY", "side"),
        ("offset", "OPEN", "offset"),
        ("symbol", "", "symbol"),
        ("exchange", "", "exchange"),
    ],
)
def test_trade_rejects_invalid_domain_identity_without_mutation(
    field: str,
    value: object,
    message: str,
) -> None:
    book = PositionBook()
    trade = make_trade(side=OrderSide.BUY, offset=Offset.OPEN)
    object.__setattr__(trade, field, value)

    with pytest.raises(ValueError, match=message):
        book.apply_trade(trade)

    assert book.all() == []


@pytest.mark.parametrize(
    "position",
    [
        ContractPosition("", "SHFE"),
        ContractPosition("cu2609", ""),
        ContractPosition("cu2609", "SHFE", long_today=1, long_price=0.0),
        ContractPosition("cu2609", "SHFE", short_today=1, short_price=float("nan")),
        ContractPosition("cu2609", "SHFE", long_price=-1.0),
    ],
)
def test_position_book_rejects_invalid_identity_or_valuation(
    position: ContractPosition,
) -> None:
    with pytest.raises(ValueError):
        PositionBook([position])
