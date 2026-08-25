from datetime import datetime, timezone
from pathlib import Path

from afuture.broker.sim import SimBroker
from afuture.engine import TradingEngine
from afuture.models import ContractSpec, FeeSpec, Offset, OrderSide, Trade
from afuture.risk import RiskManager
from afuture.state import StateStore


def test_quality_uses_verified_fee_schedule_when_trade_has_no_commission(tmp_path: Path):
    spec = ContractSpec(
        "m2609",
        "DCE",
        10,
        1,
        0.1,
        0.1,
        FeeSpec(open_fixed=2.0, close_fixed=3.0),
    )
    broker = SimBroker(500000, {"m2609": spec})
    engine = TradingEngine(
        broker,
        [],
        {"m2609": spec},
        RiskManager(),
        StateStore(tmp_path / "state.json"),
    )
    trade = Trade(
        "T1",
        "O1",
        "m2609",
        "DCE",
        OrderSide.BUY,
        Offset.OPEN,
        2,
        3000,
        datetime(2026, 8, 21, 9, tzinfo=timezone.utc),
        commission=0.0,
    )
    commission, source = engine._quality_commission(trade)
    assert commission == 4.0
    assert source == "verified_fee_schedule"
