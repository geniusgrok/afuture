from datetime import datetime, timezone

import pytest

from afuture.directional_execution import depth_aware_opening_price
from afuture.models import ContractSpec, OrderSide, Tick

NOW = datetime(2026, 8, 24, 13, 1, tzinfo=timezone.utc)


def _tick(*, bid_volume=10.0, ask_volume=10.0, limit_up=0.0, limit_down=0.0):
    return Tick(
        symbol="A2609",
        exchange="DCE",
        timestamp=NOW,
        bid_price=99.0,
        ask_price=101.0,
        last_price=100.0,
        bid_volume=bid_volume,
        ask_volume=ask_volume,
        trading_day="20260825",
        limit_up=limit_up,
        limit_down=limit_down,
        volume=1000.0,
        open_interest=10000.0,
    )


def _spec():
    return ContractSpec("A2609", "DCE", 10.0, 1.0, 0.1, 0.1)


def test_sufficient_opposite_depth_uses_best_quote_without_extra_aggression():
    assert (
        depth_aware_opening_price(
            _tick(ask_volume=8.0), _spec(), OrderSide.BUY, requested_volume=5, aggressive_ticks=1
        )
        == 101.0
    )
    assert (
        depth_aware_opening_price(
            _tick(bid_volume=8.0), _spec(), OrderSide.SELL, requested_volume=5, aggressive_ticks=1
        )
        == 99.0
    )


def test_insufficient_depth_falls_back_to_existing_aggressive_price_and_limits():
    assert (
        depth_aware_opening_price(
            _tick(ask_volume=4.0), _spec(), OrderSide.BUY, requested_volume=5, aggressive_ticks=1
        )
        == 102.0
    )
    assert (
        depth_aware_opening_price(
            _tick(bid_volume=4.0), _spec(), OrderSide.SELL, requested_volume=5, aggressive_ticks=1
        )
        == 98.0
    )
    assert (
        depth_aware_opening_price(
            _tick(ask_volume=4.0, limit_up=101.5),
            _spec(),
            OrderSide.BUY,
            requested_volume=5,
            aggressive_ticks=2,
        )
        == 101.5
    )
    assert (
        depth_aware_opening_price(
            _tick(bid_volume=4.0, limit_down=98.5),
            _spec(),
            OrderSide.SELL,
            requested_volume=5,
            aggressive_ticks=2,
        )
        == 98.5
    )


def test_invalid_execution_inputs_fail_closed():
    with pytest.raises(ValueError, match="requested_volume"):
        depth_aware_opening_price(
            _tick(), _spec(), OrderSide.BUY, requested_volume=0, aggressive_ticks=1
        )
    with pytest.raises(ValueError, match="price_tick"):
        depth_aware_opening_price(
            _tick(),
            ContractSpec("A2609", "DCE", 10.0, 0.0, 0.1, 0.1),
            OrderSide.BUY,
            requested_volume=1,
            aggressive_ticks=1,
        )
