from datetime import datetime, timezone

import pytest

from afuture.directional import (
    DirectionalConfig,
    DirectionalContractSelector,
    build_rebalance_plan,
    build_target_lots,
)
from afuture.models import (
    AccountSnapshot,
    ContractInfo,
    ContractPosition,
    ContractSpec,
    Tick,
)


def _tick(symbol: str, oi: float, *, price: float = 100.0, volume: float = 10000.0) -> Tick:
    return Tick(
        symbol=symbol,
        exchange="DCE",
        timestamp=datetime(2026, 8, 21, 13, 0, tzinfo=timezone.utc),
        bid_price=price - 1,
        ask_price=price + 1,
        last_price=price,
        bid_volume=1000,
        ask_volume=1000,
        volume=volume,
        open_interest=oi,
        trading_day="20260824",
        limit_up=price * 1.1,
        limit_down=price * 0.9,
    )


def test_directional_config_caps_gross_and_is_account_exclusive():
    config = DirectionalConfig(
        enabled=True,
        policy="execution_aligned",
        products=("A", "M"),
        max_gross_leverage=2.0,
    )
    config.validate()
    assert config.account_exclusive is True
    with pytest.raises(ValueError, match="gross leverage"):
        DirectionalConfig(
            enabled=True,
            policy="execution_aligned",
            products=("A",),
            max_gross_leverage=2.01,
        ).validate()


def test_contract_selector_uses_point_in_time_oi_and_delivery_blackout():
    today = datetime(2026, 8, 24).date()
    selector = DirectionalContractSelector(
        DirectionalConfig(
            enabled=True,
            policy="execution_aligned",
            products=("A",),
            min_days_to_expiry=20,
        )
    )
    catalog = [
        ContractInfo("A2609", "DCE", "A", "2026-09-15"),
        ContractInfo("A2611", "DCE", "A", "2026-11-15"),
        ContractInfo("A2701", "DCE", "A", "2027-01-15"),
    ]
    ticks = {
        "A2609": _tick("A2609", 999999),
        "A2611": _tick("A2611", 15000),
        "A2701": _tick("A2701", 12000),
    }
    selected = selector.select(catalog, ticks, today)
    assert selected["A"].symbol == "A2609"

    selector = DirectionalContractSelector(
        DirectionalConfig(
            enabled=True,
            policy="execution_aligned",
            products=("A",),
            min_days_to_expiry=25,
        )
    )
    selected = selector.select(catalog, ticks, today)
    assert selected["A"].symbol == "A2611"


def test_target_lots_respects_weight_notional_and_contract_cap():
    account = AccountSnapshot(
        balance=500000,
        equity=500000,
        available=400000,
        margin=0,
        realized_pnl=0,
        unrealized_pnl=0,
        trading_day="20260824",
    )
    specs = {
        "A2611": ContractSpec("A2611", "DCE", 10, 1, 0.1, 0.1),
        "M2609": ContractSpec("M2609", "DCE", 10, 1, 0.1, 0.1),
    }
    ticks = {
        "A": _tick("A2611", 20000, price=5000),
        "M": _tick("M2609", 30000, price=2500),
    }
    targets = build_target_lots(
        account,
        {"A": 1.0, "M": -1.0},
        ticks,
        specs,
        max_contract_volume=100,
    )
    assert targets == {"A2611": 10, "M2609": -20}
    capped = build_target_lots(
        account,
        {"A": 2.0},
        {"A": ticks["A"]},
        {"A2611": specs["A2611"]},
        max_contract_volume=7,
    )
    assert capped == {"A2611": 7}


def test_rebalance_plan_closes_old_or_excess_risk_before_opening():
    positions = [
        ContractPosition("A2609", "DCE", long_today=5),
        ContractPosition("M2609", "DCE", short_today=8),
    ]
    plan = build_rebalance_plan(
        positions,
        {"A2611": 6, "M2609": -3, "RB2610": 4},
    )
    assert plan.reductions == {
        "A2609": -5,
        "M2609": 5,
    }
    assert plan.openings == {}

    flat_plan = build_rebalance_plan([], {"A2611": 6, "M2609": -3})
    assert flat_plan.reductions == {}
    assert flat_plan.openings == {"A2611": 6, "M2609": -3}
