from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from afuture.auto import AutoConfig, AutoPairManager
from afuture.broker.sim import SimBroker
from afuture.engine import TradingEngine
from afuture.models import ContractInfo, ContractSpec, Tick
from afuture.risk import RiskConfig, RiskManager
from afuture.state import StateStore

_CHINA_TZ = ZoneInfo("Asia/Shanghai")


def _tick(symbol: str, when: datetime, bid: float, ask: float) -> Tick:
    return Tick(
        symbol=symbol,
        exchange="DCE",
        timestamp=when,
        bid_price=bid,
        ask_price=ask,
        last_price=(bid + ask) / 2,
        bid_volume=50,
        ask_volume=50,
        trading_day="20260821",
        volume=20000,
        open_interest=80000,
    )


def _auto_config() -> AutoConfig:
    return AutoConfig(
        enabled=True,
        products=("m",),
        exchanges=("DCE",),
        max_active_pairs=1,
        max_contracts_per_product=3,
        min_days_to_expiry=15,
        scan_interval_seconds=0.0,
        lookback=3,
        entry_z=0.8,
        exit_z=0.3,
        stop_z=4.0,
        max_pair_volume=2,
        sample_seconds=0,
        min_volume=1000,
        min_open_interest=1000,
        min_liquidity_score=0.2,
        min_stationarity_score=0.0,
        max_half_life=1000,
        min_net_edge=0.0,
        session_windows=("09:00-11:30",),
    )


class _Journal:
    def __init__(self) -> None:
        self.events = []

    def record(self, event_type, payload) -> None:
        self.events.append((event_type, payload))


def test_auto_open_then_reversion_exits_and_retires(tmp_path: Path):
    specs = {
        "m2609": ContractSpec("m2609", "DCE", 10, 1, 0.10, 0.10),
        "m2701": ContractSpec("m2701", "DCE", 10, 1, 0.10, 0.10),
    }
    catalog = [
        ContractInfo("m2609", "DCE", "m", "2026-09-15"),
        ContractInfo("m2701", "DCE", "m", "2027-01-15"),
    ]
    broker = SimBroker(500000, specs, contract_catalog=catalog)
    journal = _Journal()
    engine = TradingEngine(
        broker,
        [],
        {},
        RiskManager(RiskConfig()),
        StateStore(tmp_path / "state.json"),
        slippage_ticks=0,
        auto_manager=AutoPairManager(_auto_config()),
        historical_mode=True,
        journal=journal,
    )
    engine.start()
    base = datetime(2026, 8, 21, 9, 0, tzinfo=_CHINA_TZ)
    for i, spread in enumerate([10, 11, 10, 25, 10, 10]):
        broker.publish_tick(
            _tick(
                "m2609",
                base + timedelta(minutes=i),
                3000 + spread - 0.5,
                3000 + spread + 0.5,
            )
        )
        engine.run_once()
        broker.publish_tick(
            _tick(
                "m2701",
                base + timedelta(minutes=i, milliseconds=500),
                2999.5,
                3000.5,
            )
        )
        engine.run_once()
        engine.run_once()

    if broker.get_positions():
        print("positions", broker.get_positions())
        print(
            "orders",
            [
                (o.request.reference, o.request.offset.value, o.status.value, o.traded)
                for o in broker.get_orders()
            ],
        )
        print(
            "trades",
            [
                (t.symbol, t.offset.value, t.side.value, t.volume, t.price)
                for t in broker.get_trades()
            ],
        )
        print("pairs", sorted(engine.pairs))
        print("retiring", sorted(engine._retiring_auto_pairs))
        print("strategy", {k: v.snapshot_state() for k, v in engine.strategies.items()})
        print(
            "events",
            [
                (kind, getattr(payload, "action", payload))
                for kind, payload in journal.events
                if kind in {"signal", "risk_reject", "emergency_stop", "auto_scan_error"}
            ],
        )
    assert broker.get_positions() == []
