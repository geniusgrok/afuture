import sys
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from afuture.directional import DirectionalConfig
from afuture.execution_aligned_runtime import (
    FROZEN_PRODUCTS,
    ExecutionAlignedDirectionalPortfolioManager,
    ExecutionAlignedSignalHistory,
    SinaContinuousOHLCProvider,
)
from afuture.models import (
    AccountSnapshot,
    ContractInfo,
    ContractPosition,
    ContractSpec,
    Offset,
    OrderType,
    Tick,
)
from afuture.risk import RiskConfig, RiskManager

NOW = datetime(2026, 8, 24, 13, 1, tzinfo=timezone.utc)


class _Provider:
    def __init__(self):
        dates = pd.date_range(end="2026-08-21", periods=180, freq="B")
        close = pd.DataFrame({"A": range(100, 280)}, index=dates, dtype=float)
        open_prices = close.shift(1).fillna(close.iloc[0])
        self.history = ExecutionAlignedSignalHistory(open_prices, close)
        self.fail = False

    def load(self, products):
        if self.fail:
            raise RuntimeError("provider unavailable")
        return self.history


class _Policy:
    def __init__(self):
        self.calls = 0

    def target_weights(self, open_prices, close):
        self.calls += 1
        assert open_prices.index.equals(close.index)
        assert open_prices.index[-1] > self._last_observed(close)
        return {"A": 1.0}

    @staticmethod
    def _last_observed(close):
        return close.index[-2]


class _Broker:
    def is_ready(self):
        return True

    def get_account(self):
        return AccountSnapshot(
            balance=100000,
            equity=100000,
            available=100000,
            margin=0,
            realized_pnl=0,
            unrealized_pnl=0,
            trading_day="20260825",
        )

    def get_positions(self):
        return []

    def get_active_orders(self):
        return []


class _FlattenBroker(_Broker):
    def __init__(self):
        self.positions = [
            ContractPosition(
                "A2609",
                "DCE",
                long_today=3,
                short_today=3,
                long_price=100.0,
                short_price=100.0,
            )
        ]
        self.orders = []

    def get_positions(self):
        return self.positions

    def get_contract_catalog(self):
        return [
            ContractInfo(
                symbol="A2609",
                exchange="DCE",
                product="A",
                expiry="2026-12-15",
            )
        ]

    def subscribe(self, symbol, exchange):
        return None

    def get_live_contract_specs(self, symbols, timeout_seconds=10.0):
        return {
            symbol: ContractSpec(
                symbol=symbol,
                exchange="DCE",
                multiplier=10.0,
                price_tick=1.0,
                margin_rate_long=0.1,
                margin_rate_short=0.1,
            )
            for symbol in symbols
        }

    def send_order(self, request):
        self.orders.append(request)
        return f"order-{len(self.orders)}"


def _manager(provider=None, policy=None):
    return ExecutionAlignedDirectionalPortfolioManager(
        DirectionalConfig(
            enabled=True,
            products=("A",),
            exchanges=("DCE",),
            signal_max_age_hours=120.0,
        ),
        _Broker(),
        RiskManager(RiskConfig()),
        signal_provider=provider or _Provider(),
        policy=policy or _Policy(),
    )


def test_execution_aligned_runtime_passes_open_and_close_history_to_policy():
    policy = _Policy()
    manager = _manager(policy=policy)
    history = manager._load_signal(NOW)
    weights = manager._next_target_weights(history)
    assert weights == {"A": 1.0}
    assert policy.calls == 1


def test_signal_freshness_uses_completed_trading_day_not_only_hour_age():
    manager = _manager()
    # Friday's completed bar is valid for the first post-weekend trading session.
    history = manager._load_signal(NOW, required_signal_day=date(2026, 8, 21))
    assert history.close.index[-1].date() == date(2026, 8, 21)

    # Missing a required normal completed trading day must fail even though 120h has not expired.
    with pytest.raises(RuntimeError, match="required signal trading day"):
        manager._load_signal(NOW, required_signal_day=date(2026, 8, 24))


def test_cached_signal_can_cover_transient_provider_failure_when_required_day_is_present():
    provider = _Provider()
    manager = _manager(provider=provider)
    manager._load_signal(NOW, required_signal_day=date(2026, 8, 21))
    provider.fail = True
    later = datetime(2026, 8, 25, 13, 1, tzinfo=timezone.utc)
    cached = manager._load_signal(later, required_signal_day=date(2026, 8, 21))
    assert cached.close.index[-1].date() == date(2026, 8, 21)


def test_default_execution_aligned_runtime_requires_the_frozen_50_product_universe():
    assert len(FROZEN_PRODUCTS) == 50
    with pytest.raises(ValueError, match="frozen 50-product universe"):
        ExecutionAlignedDirectionalPortfolioManager(
            DirectionalConfig(
                enabled=True,
                products=("A", "M"),
                exchanges=("DCE",),
            ),
            _Broker(),
            RiskManager(RiskConfig()),
            signal_provider=_Provider(),
        )


def test_execution_aligned_flatten_closes_both_sides_when_same_contract_is_hedged():
    broker = _FlattenBroker()
    manager = ExecutionAlignedDirectionalPortfolioManager(
        DirectionalConfig(
            enabled=True,
            products=("A",),
            exchanges=("DCE",),
            signal_max_age_hours=120.0,
        ),
        broker,
        RiskManager(RiskConfig(max_contract_volume=10)),
        signal_provider=_Provider(),
        policy=_Policy(),
    )
    manager.bootstrap(NOW)
    manager.observe(
        Tick(
            symbol="A2609",
            exchange="DCE",
            timestamp=NOW,
            bid_price=99.0,
            ask_price=101.0,
            last_price=100.0,
            bid_volume=100.0,
            ask_volume=100.0,
            trading_day="20260825",
            volume=5000.0,
            open_interest=30000.0,
        )
    )

    result = manager.flatten(NOW)

    assert result.action == "reduce"
    assert len(broker.orders) == 2
    assert {order.side.value for order in broker.orders} == {"BUY", "SELL"}
    assert {order.volume for order in broker.orders} == {3}
    assert all(order.offset is not Offset.OPEN for order in broker.orders)
    assert all(order.order_type is OrderType.FAK for order in broker.orders)
    assert all(order.reference == "directional:flatten" for order in broker.orders)


def test_sina_provider_rejects_duplicate_daily_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = pd.DataFrame(
        {
            "date": ["2026-08-21", "2026-08-21"],
            "open": [100.0, 200.0],
            "close": [101.0, 201.0],
        }
    )
    fake_akshare = SimpleNamespace(futures_zh_daily_sina=lambda symbol: source)
    monkeypatch.setitem(sys.modules, "akshare", fake_akshare)

    with pytest.raises(ValueError, match="duplicate daily date"):
        SinaContinuousOHLCProvider._load_one("A")


def test_execution_history_rejects_duplicate_daily_index() -> None:
    manager = _manager()
    dates = list(pd.date_range("2026-01-01", periods=140, freq="B"))
    dates.append(dates[-1])
    frame = pd.DataFrame({"A": range(len(dates))}, index=dates, dtype=float)
    history = ExecutionAlignedSignalHistory(frame, frame)

    with pytest.raises(ValueError, match="duplicate daily date"):
        manager._normalize_history(history, max_date=date(2026, 8, 21))
