from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from afuture.directional import DirectionalConfig
from afuture.models import ContractSpec, FeeSpec, Tick
from afuture.operations import estimate_stress90_contract_cost
from afuture.risk import RiskConfig
from afuture.stress90_risk_overlay import stress90_risk_overlay_digest

_CHINA = ZoneInfo("Asia/Shanghai")


def test_capacity_cost_surface_exposes_cash_fees_and_spread_bps() -> None:
    tick = Tick(
        symbol="A2612",
        exchange="DCE",
        timestamp=datetime(2026, 8, 27, 21, 1, tzinfo=_CHINA),
        bid_price=99.0,
        ask_price=101.0,
        last_price=100.0,
        bid_volume=1000.0,
        ask_volume=1000.0,
        trading_day="20260827",
    )
    spec = ContractSpec(
        symbol="A2612",
        exchange="DCE",
        multiplier=10.0,
        price_tick=1.0,
        margin_rate_long=0.10,
        margin_rate_short=0.12,
        fee=FeeSpec(open_fixed=2.0, close_fixed=3.0, close_today_fixed=4.0),
    )

    result = estimate_stress90_contract_cost(
        tick,
        spec,
        require_commission_evidence=True,
    )

    assert result["open_fee_per_lot"] == pytest.approx(2.0)
    assert result["close_yesterday_fee_per_lot"] == pytest.approx(3.0)
    assert result["close_today_fee_per_lot"] == pytest.approx(4.0)
    assert result["spread_bps"] == pytest.approx(result["bid_ask_bps"])
    assert result["one_tick_bps"] > 0.0
    assert result["minimum_reasonable_one_way_bps"] > 0.0


def test_risk_overlay_digest_binds_scale_and_order_rate_limit() -> None:
    directional = DirectionalConfig(
        enabled=True,
        policy="stress90",
        products=("A",),
        exchanges=("DCE",),
        live_risk_scale=0.5,
    )
    risk = RiskConfig(margin_estimate_buffer=1.25)
    baseline = stress90_risk_overlay_digest(directional, risk)

    assert stress90_risk_overlay_digest(directional, risk) == baseline
    assert (
        stress90_risk_overlay_digest(replace(directional, live_risk_scale=0.25), risk)
        != baseline
    )
    assert (
        stress90_risk_overlay_digest(
            directional,
            replace(risk, max_orders_per_minute=risk.max_orders_per_minute + 1),
        )
        != baseline
    )
