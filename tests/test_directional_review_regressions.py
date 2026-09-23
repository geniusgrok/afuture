from datetime import date, datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from afuture.broker.ctp import CtpBroker, CtpCredentials
from afuture.directional import DirectionalConfig
from afuture.directional_acceptance import (
    DirectionalProductionAcceptance,
    ProductionMechanicsConfig,
)
from afuture.directional_engine import DirectionalTradingEngine
from afuture.directional_runtime import DirectionalActionResult
from afuture.execution_aligned_runtime import (
    ExecutionAlignedDirectionalPortfolioManager,
    ExecutionAlignedSignalHistory,
)
from afuture.models import AccountSnapshot, RuntimeMode, Tick
from afuture.risk import RiskConfig, RiskManager
from afuture.state import StateStore

NOW = datetime(2026, 8, 24, 13, 1, tzinfo=timezone.utc)


class _SignalProvider:
    def __init__(self):
        dates = pd.date_range(end="2026-08-24", periods=180, freq="B")
        close = pd.DataFrame({"A": range(100, 280)}, index=dates, dtype=float)
        self.history = ExecutionAlignedSignalHistory(
            close.shift(1).fillna(close.iloc[0]),
            close,
        )

    def load(self, products):
        return self.history


class _Policy:
    def target_weights(self, open_prices, close):
        return {"A": 1.0}


class _SignalBroker:
    def is_ready(self):
        return True

    def get_account(self):
        return AccountSnapshot(100000, 100000, 100000, 0, 0, 0, "20260825")

    def get_positions(self):
        return []

    def get_active_orders(self):
        return []


def test_stale_completed_activity_cannot_hide_newer_completed_signal_day():
    tracker = SimpleNamespace(current_trading_day="20260825")
    manager = ExecutionAlignedDirectionalPortfolioManager(
        DirectionalConfig(
            enabled=True,
            policy="execution_aligned",
            products=("A",),
            exchanges=("DCE",),
            signal_max_age_hours=120.0,
        ),
        _SignalBroker(),
        RiskManager(RiskConfig()),
        historical_mode=True,
        signal_provider=_SignalProvider(),
        policy=_Policy(),
        activity_tracker=tracker,
    )
    with pytest.raises(RuntimeError, match="completed directional activity is stale"):
        manager._load_signal(NOW, required_signal_day=date(2026, 8, 21))


class _RiskManager:
    def __init__(self):
        self.risk = True
        self.flatten_calls = 0

    def bootstrap(self, now):
        pass

    def observe(self, tick):
        pass

    def maybe_rebalance(self, now):
        return DirectionalActionResult("hold")

    def flatten(self, now):
        self.flatten_calls += 1
        return DirectionalActionResult("reduce")

    def has_risk(self):
        return self.risk

    def required_symbols(self):
        return set()

    def close(self):
        pass


class _RiskBroker:
    def __init__(self):
        self.ready = False
        self.account = AccountSnapshot(100000, 100000, 100000, 0, 0, 0, "20260825")
        self.events = []

    def start(self):
        self.ready = True

    def stop(self):
        self.ready = False

    def is_ready(self):
        return self.ready

    def subscribe(self, symbol, exchange):
        pass

    def get_account(self):
        return self.account

    def get_positions(self):
        return []

    def get_active_orders(self):
        return []

    def poll_events(self):
        events = list(self.events)
        self.events.clear()
        return events

    def health_error(self):
        return None


def _tick():
    return Tick(
        symbol="A2609",
        exchange="DCE",
        timestamp=NOW,
        bid_price=99,
        ask_price=101,
        last_price=100,
        bid_volume=100,
        ask_volume=100,
        trading_day="20260825",
        volume=10000,
        open_interest=20000,
    )


def test_nonpositive_equity_with_directional_risk_reduces_before_halting(tmp_path):
    broker = _RiskBroker()
    manager = _RiskManager()
    engine = DirectionalTradingEngine(
        broker,
        [],
        {},
        RiskManager(RiskConfig()),
        StateStore(tmp_path / "state.json"),
        directional_manager=manager,
        health_clock=lambda: NOW,
    )
    engine.start()
    broker.account = AccountSnapshot(0, 0, 0, 0, -100000, 0, "20260825")
    engine.on_tick(_tick())
    assert engine.state.runtime_mode == RuntimeMode.REDUCE_ONLY.value
    assert engine.halted is False


def test_ctp_invalid_account_event_reduces_directional_risk_before_halt(tmp_path):
    ctp = CtpBroker(
        CtpCredentials("user", "secret", "9999", "tcp://td", "tcp://md", "app", "auth", "test")
    )
    ctp._trading_day = "20260825"
    ctp._on_account(SimpleNamespace(data=SimpleNamespace(balance=float("nan"), available=100000.0)))

    broker = _RiskBroker()
    broker.events = ctp.poll_events()
    manager = _RiskManager()
    engine = DirectionalTradingEngine(
        broker,
        [],
        {},
        RiskManager(RiskConfig()),
        StateStore(tmp_path / "state.json"),
        directional_manager=manager,
        health_clock=lambda: NOW,
    )
    engine.start()
    engine.run_once()

    assert engine.state.runtime_mode == RuntimeMode.REDUCE_ONLY.value
    assert engine.halted is False
    assert manager.flatten_calls == 1


def test_proxy_open_equity_updates_high_watermark_before_intraday_drawdown():
    raw = pd.DataFrame(
        [
            {
                "date": "2026-08-20",
                "product": "A",
                "exchange": "DCE",
                "symbol": "A2609",
                "delivery": "2026-12-15",
                "open": 100,
                "close": 100,
                "volume": 5000,
                "hold": 30000,
            },
            {
                "date": "2026-08-21",
                "product": "A",
                "exchange": "DCE",
                "symbol": "A2609",
                "delivery": "2026-12-15",
                "open": 100,
                "close": 100,
                "volume": 5000,
                "hold": 30000,
            },
            {
                "date": "2026-08-24",
                "product": "A",
                "exchange": "DCE",
                "symbol": "A2609",
                "delivery": "2026-12-15",
                "open": 150,
                "close": 100,
                "volume": 5000,
                "hold": 30000,
            },
        ]
    )
    weights = pd.DataFrame(
        {"A": [1.0, 1.0]},
        index=pd.to_datetime(["2026-08-21", "2026-08-24"]),
    )
    sim = DirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=100000,
            max_contract_volume=100,
            max_daily_loss_ratio=0.90,
            max_total_drawdown_ratio=0.30,
            max_margin_ratio=0.90,
            min_available_ratio=0,
        )
    )
    result = sim.simulate(raw, weights, cost_bps=0)
    day = result.daily.loc[pd.Timestamp("2026-08-24")]
    assert day["risk_reason"] == "drawdown limit reached"
    assert day["halted"]
    assert day["gross_notional"] == 0


def test_proxy_existing_margin_breach_reduces_before_normal_rebalance():
    raw = pd.DataFrame(
        [
            {
                "date": "2026-08-20",
                "product": "A",
                "exchange": "DCE",
                "symbol": "A2609",
                "delivery": "2026-12-15",
                "open": 100,
                "close": 100,
                "volume": 5000,
                "hold": 30000,
            },
            {
                "date": "2026-08-21",
                "product": "A",
                "exchange": "DCE",
                "symbol": "A2609",
                "delivery": "2026-12-15",
                "open": 100,
                "close": 100,
                "volume": 5000,
                "hold": 30000,
            },
            {
                "date": "2026-08-24",
                "product": "A",
                "exchange": "DCE",
                "symbol": "A2609",
                "delivery": "2026-12-15",
                "open": 150,
                "close": 150,
                "volume": 5000,
                "hold": 30000,
            },
        ]
    )
    weights = pd.DataFrame(
        {"A": [-1.0, -1.0]},
        index=pd.to_datetime(["2026-08-21", "2026-08-24"]),
    )
    sim = DirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=100000,
            max_contract_volume=100,
            max_daily_loss_ratio=0.90,
            max_total_drawdown_ratio=0.90,
            max_margin_ratio=0.35,
            min_available_ratio=0,
        )
    )
    result = sim.simulate(raw, weights, cost_bps=0)
    day = result.daily.loc[pd.Timestamp("2026-08-24")]
    assert day["risk_reason"] == "margin ratio limit reached"
    assert day["halted"]
    assert day["gross_notional"] == 0
