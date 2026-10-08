"""Actual directional submission and SimBroker matching under injected wall clocks."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from afuture.broker.sim import SimBroker
from afuture.directional import DirectionalConfig
from afuture.directional_runtime import DirectionalPortfolioManager
from afuture.models import (
    ContractInfo,
    ContractSpec,
    Tick,
)
from afuture.risk import RiskConfig, RiskManager

NOW = datetime(2026, 8, 24, 13, 1, tzinfo=timezone.utc)


def setup_live(*, timestamp=NOW, max_orders=20):
    specs = {symbol: ContractSpec(symbol, "DCE", 10, 1, 0.1, 0.1) for symbol in ("A2611", "M2609")}
    catalog = [ContractInfo(s, "DCE", s[0], "2026-11-15") for s in specs]
    broker = SimBroker(100_000, specs, contract_catalog=catalog)
    broker.start()
    wall = [timestamp]
    manager = DirectionalPortfolioManager(
        DirectionalConfig(
            enabled=True,
            policy="execution_aligned",
            products=("A", "M"),
            exchanges=("DCE",),
            max_gross_leverage=2.0,
            max_contract_volume=5,
            rebalance_window="21:00-21:10",
        ),
        broker,
        RiskManager(
            RiskConfig(
                max_orders_per_minute=max_orders, open_cooldown_minutes=0, close_blackout_minutes=0
            )
        ),
        static_specs=specs,
        historical_mode=False,
        health_clock=lambda: wall[0],
        elapsed_clock=lambda: 100.0,
    )
    manager.bootstrap(timestamp)
    for symbol in specs:
        tick = Tick(
            symbol,
            "DCE",
            timestamp,
            99.0,
            101.0,
            100.0,
            bid_volume=1000,
            ask_volume=1000,
            volume=20_000,
            open_interest=20_000,
            trading_day="20260825",
            limit_up=120.0,
            limit_down=80.0,
        )
        broker.publish_tick(tick)
        manager.observe(tick)
    selected = {item.product: item for item in catalog}
    return manager, broker, wall, selected, specs


def submit(manager, broker, selected, specs, *, event_time=NOW):
    return manager._submit_openings(
        broker.get_positions(), {"A2611": 1, "M2609": 1}, selected, specs, event_time
    )


def test_first_fill_remains_real_when_later_child_quote_expires():
    manager, broker, wall, selected, specs = setup_live()
    native_send = broker.send_order

    def send(request):
        identity = native_send(request)
        wall[0] += timedelta(seconds=30)
        return identity

    broker.send_order = send
    result = submit(manager, broker, selected, specs)
    assert result.action == "reject" and "stale" in result.reason
    assert len(result.order_ids) == len(broker.get_orders()) == len(broker.get_trades()) == 1
    assert broker.get_positions()[0].long_total == 1


def test_future_signal_time_cannot_reset_live_monotonic_order_budget():
    manager, broker, _, selected, specs = setup_live(max_orders=1)
    result = submit(manager, broker, selected, specs)
    assert result.action == "reject" and "rate limit" in result.reason
    assert len(result.order_ids) == 1
    result2 = manager._submit_openings(
        broker.get_positions(), {"M2609": 1}, selected, specs, NOW + timedelta(days=7)
    )
    assert result2.action == "reject" and "rate limit" in result2.reason
    assert len(broker.get_orders()) == 1


def test_quote_account_day_mismatch_does_not_rebase_account_risk():
    manager, broker, _, selected, specs = setup_live()
    original = broker.get_account
    broker.get_account = lambda: replace(original(), trading_day="20260826")
    result = submit(manager, broker, selected, specs)
    assert result.action == "reject" and "trading day mismatch" in result.reason
    assert not broker.get_orders()
