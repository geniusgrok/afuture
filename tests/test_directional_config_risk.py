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
policy = "execution_aligned"
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
policy = "execution_aligned"
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


def test_directional_policy_must_be_explicit_in_every_enabled_mode(tmp_path: Path):
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
    with pytest.raises(ValueError, match="directional.policy must be explicit"):
        load_config(replay)

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


def test_stress90_rejects_explicit_legacy_rebalance_window(tmp_path: Path) -> None:
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS

    config = _write(
        tmp_path,
        """
[system]
mode = "replay"
initial_capital = 500000

[directional]
enabled = true
policy = "stress90"
products = [{products}]
rebalance_window = "20:55-09:10"
""".format(products=", ".join(f'"{product}"' for product in FROZEN_PRODUCTS)),
    )

    with pytest.raises(ValueError, match="rebalance_window.*Stress-90"):
        load_config(config)


def test_stress90_order_capable_config_requires_expected_ctp_account_identity(
    tmp_path: Path,
    monkeypatch,
):
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS

    live = _write(
        tmp_path,
        """
[system]
mode = "live"
initial_capital = 500000

[ctp]
environment = "production"
td_address = "tcp://trade"
md_address = "tcp://market"

[directional]
enabled = true
policy = "stress90"
products = [{products}]
""".format(products=", ".join(f'"{product}"' for product in FROZEN_PRODUCTS)),
    )
    for key, value in {
        "AFUTURE_CTP_USER": "user",
        "AFUTURE_CTP_PASSWORD": "secret",
        "AFUTURE_CTP_BROKER": "9999",
    }.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(
        ValueError,
        match="AFUTURE_CTP_ACCOUNT_ID.*AFUTURE_CTP_CURRENCY_ID",
    ):
        load_config(live)

    monkeypatch.setenv("AFUTURE_CTP_ACCOUNT_ID", "acct-01")
    monkeypatch.setenv("AFUTURE_CTP_CURRENCY_ID", "CNY")
    monkeypatch.setenv("AFUTURE_CTP_INVESTOR_ID", "investor-01")
    monkeypatch.setenv("AFUTURE_CTP_INVEST_UNIT_ID", "unit-01")
    config = load_config(live)
    assert config.ctp is not None
    assert config.ctp.account_id == "acct-01"
    assert config.ctp.currency_id == "CNY"
    assert config.ctp.investor_id == "investor-01"
    assert config.ctp.invest_unit_id == "unit-01"
    assert config.account_registry_path == "/var/lib/afuture/account-runtime-registry.json"
    base_config_text = live.read_text(encoding="utf-8")

    relative_registry = _write(
        tmp_path,
        base_config_text
        + """

[paths]
account_registry = "runtime/account-runtime-registry.json"
""",
    )
    with pytest.raises(ValueError, match="account_registry.*absolute"):
        load_config(relative_registry)

    split_registry = _write(
        tmp_path,
        base_config_text
        + """

[paths]
account_registry = "/var/lib/afuture/account-runtime-registry-copy.json"
""",
    )
    with pytest.raises(ValueError, match="account_registry.*fixed machine-level"):
        load_config(split_registry)


def test_execution_aligned_order_capable_config_remains_legacy_compatible(
    tmp_path: Path,
    monkeypatch,
):
    live = _write(
        tmp_path,
        """
[system]
mode = "live"
initial_capital = 500000

[ctp]
environment = "production"
td_address = "tcp://trade"
md_address = "tcp://market"

[directional]
enabled = true
policy = "execution_aligned"
products = ["A"]
""",
    )
    for key, value in {
        "AFUTURE_CTP_USER": "user",
        "AFUTURE_CTP_PASSWORD": "secret",
        "AFUTURE_CTP_BROKER": "9999",
    }.items():
        monkeypatch.setenv(key, value)

    config = load_config(live)

    assert config.ctp is not None
    assert config.ctp.account_id == ""
    assert config.ctp.currency_id == ""


def test_directional_policy_rejects_unknown_identity():
    from afuture.directional import DirectionalConfig

    with pytest.raises(ValueError, match="directional.policy"):
        DirectionalConfig(enabled=True, products=("A",), policy="optimized90").validate()


def test_enabled_directional_config_requires_current_policy_identity():
    from afuture.directional import DirectionalConfig

    with pytest.raises(ValueError, match="directional.policy must be explicit"):
        DirectionalConfig(enabled=True, products=("A",), policy="").validate()


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
