from datetime import datetime, timezone
from pathlib import Path

import pytest

from afuture.config import load_config
from afuture.models import (
    AccountSnapshot,
    ContractSpec,
    Offset,
    OrderRequest,
    OrderSide,
    OrderType,
    Tick,
)
from afuture.risk import RiskConfig, RiskManager


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "directional.toml"
    path.write_text(body.strip(), encoding="utf-8")
    return path


def test_directional_config_can_be_the_only_strategy_and_is_account_exclusive(tmp_path: Path):
    directional_only = _write(
        tmp_path,
        """
[system]
mode = "replay"
initial_capital = 500000

[directional]
enabled = true
products = ["A", "M"]
exchanges = ["DCE"]
max_gross_leverage = 2.0
min_days_to_expiry = 20
rebalance_window = "21:00-21:10"
""",
    )
    config = load_config(directional_only)
    assert config.directional.enabled is True
    assert config.directional.products == ("A", "M")
    assert not config.pairs
    assert not config.auto.enabled

    mixed = _write(
        tmp_path,
        """
[system]
mode = "replay"
initial_capital = 500000

[directional]
enabled = true
products = ["A"]
exchanges = ["DCE"]

[[contracts]]
symbol = "A2609"
exchange = "DCE"
product = "A"
expiry = "2026-09-15"
multiplier = 10
price_tick = 1
margin_rate_long = 0.1
margin_rate_short = 0.1

[[contracts]]
symbol = "A2701"
exchange = "DCE"
product = "A"
expiry = "2027-01-15"
multiplier = 10
price_tick = 1
margin_rate_long = 0.1
margin_rate_short = 0.1

[[pairs]]
pair_id = "a_calendar"
near_symbol = "A2609"
far_symbol = "A2701"
exchange = "DCE"
volume = 1
lookback = 20
entry_z = 2.0
exit_z = 0.5
stop_z = 4.0
""",
    )
    with pytest.raises(ValueError, match="account-exclusive"):
        load_config(mixed)


def test_live_directional_policy_must_be_explicit_but_replay_blank_is_legacy_compatible(
    tmp_path: Path,
):
    replay = _write(
        tmp_path,
        """
[system]
mode = "replay"
initial_capital = 500000

[directional]
enabled = true
products = ["A"]
exchanges = ["DCE"]
""",
    )
    assert load_config(replay).directional.policy == ""

    live = _write(
        tmp_path,
        """
[system]
mode = "live"
initial_capital = 500000

[ctp]
environment = "test"
td_address = "tcp://trade"
md_address = "tcp://market"

[directional]
enabled = true
products = ["A"]
exchanges = ["DCE"]
""",
    )
    with pytest.raises(ValueError, match="directional.policy must be explicit"):
        load_config(live, require_ctp_credentials=False)


def test_directional_policy_rejects_unknown_identity():
    from afuture.directional import DirectionalConfig

    with pytest.raises(ValueError, match="directional.policy"):
        DirectionalConfig(enabled=True, products=("A",), policy="optimized90").validate()


def _tick(*, bid=99.0, ask=101.0, bid_volume=100, ask_volume=100) -> Tick:
    return Tick(
        symbol="A2609",
        exchange="DCE",
        timestamp=datetime(2026, 8, 24, 13, 1, tzinfo=timezone.utc),
        bid_price=bid,
        ask_price=ask,
        last_price=100.0,
        bid_volume=bid_volume,
        ask_volume=ask_volume,
        volume=10000,
        open_interest=20000,
        trading_day="20260825",
        limit_up=120.0,
        limit_down=80.0,
    )


def test_directional_open_reuses_single_contract_microstructure_and_account_gates():
    risk = RiskManager(
        RiskConfig(
            max_margin_ratio=0.50,
            min_available_ratio=0.20,
            max_contract_volume=50,
            min_depth_multiple=2.0,
            max_bid_ask_ticks=4.0,
            limit_distance_ticks=3.0,
        )
    )
    spec = ContractSpec("A2609", "DCE", 10, 1, 0.1, 0.1)
    good = _tick()
    decision = risk.check_contract_entry(
        good,
        OrderSide.BUY,
        requested_volume=10,
        spec=spec,
        session_windows=("21:00-23:00",),
    )
    assert decision.allowed

    shallow = _tick(ask_volume=10)
    decision = risk.check_contract_entry(
        shallow,
        OrderSide.BUY,
        requested_volume=10,
        spec=spec,
        session_windows=("21:00-23:00",),
    )
    assert not decision.allowed and "depth" in decision.reason

    account = AccountSnapshot(
        balance=500000,
        equity=500000,
        available=500000,
        margin=0,
        realized_pnl=0,
        unrealized_pnl=0,
        trading_day="20260825",
    )
    order = OrderRequest(
        symbol="A2609",
        exchange="DCE",
        side=OrderSide.BUY,
        offset=Offset.OPEN,
        volume=10,
        price=101.0,
        order_type=OrderType.FAK,
        reference="directional:A",
    )
    decision = risk.check_open_orders(
        account,
        [order],
        {"A2609": spec},
        current_contract_volumes={},
    )
    assert decision.allowed

    oversized = OrderRequest(
        symbol="A2609",
        exchange="DCE",
        side=OrderSide.BUY,
        offset=Offset.OPEN,
        volume=51,
        price=101.0,
        order_type=OrderType.FAK,
        reference="directional:A",
    )
    decision = risk.check_open_orders(
        account,
        [oversized],
        {"A2609": spec},
        current_contract_volumes={},
    )
    assert not decision.allowed and "contract volume" in decision.reason
