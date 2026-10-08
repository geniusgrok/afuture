from datetime import datetime, timezone
from pathlib import Path

from afuture.broker.sim import SimBroker
from afuture.fees import calculate_commission
from afuture.models import (
    AccountSnapshot,
    ContractPosition,
    ContractSpec,
    FeeSpec,
    Offset,
    OrderRequest,
    OrderSide,
    OrderStatus,
    PairConfig,
    Tick,
    Trade,
)
from afuture.position import PositionBook
from afuture.reconcile import compare_positions
from afuture.risk import RiskConfig, RiskManager
from afuture.state import RuntimeState, StateStore
from afuture.strategy import CalendarSpreadStrategy


def trade(symbol, side, offset, volume, price):
    return Trade("t", "o", symbol, "SHFE", side, offset, volume, price, datetime.now(timezone.utc))


def test_fee_model_handles_open_close_and_close_today():
    spec = ContractSpec(
        "rb", "SHFE", 10, 1, 0.1, 0.1, FeeSpec(open_fixed=2, close_fixed=3, close_today_fixed=10)
    )
    assert calculate_commission(spec, Offset.OPEN, 3500, 2) == 4
    assert calculate_commission(spec, Offset.CLOSE, 3500, 2) == 6
    assert calculate_commission(spec, Offset.CLOSE_TODAY, 3500, 2) == 20


def test_position_book_splits_shfe_yesterday_today_and_realizes_pnl():
    book = PositionBook()
    book.apply_trade(trade("rb", "BUY" if False else OrderSide.BUY, Offset.OPEN, 3, 3500))
    book.roll_trading_day()
    book.apply_trade(trade("rb", OrderSide.BUY, Offset.OPEN, 2, 3510))
    plan = book.plan_close("rb", "SHFE", OrderSide.SELL, 4, close_today_first=False)
    assert [(p.offset, p.volume) for p in plan] == [
        (Offset.CLOSE_YESTERDAY, 3),
        (Offset.CLOSE_TODAY, 1),
    ]
    realized = book.apply_trade(trade("rb", OrderSide.SELL, Offset.CLOSE_YESTERDAY, 1, 3520))
    assert realized > 0


def test_non_shfe_close_uses_generic_close():
    book = PositionBook()
    book.apply_trade(
        Trade("t", "o", "m", "DCE", OrderSide.BUY, Offset.OPEN, 2, 3000, datetime.now(timezone.utc))
    )
    assert book.plan_close("m", "DCE", OrderSide.SELL, 2)[0].offset is Offset.CLOSE


def test_reconcile_rejects_exchange_mismatch_and_duplicate_contract_rows():
    expected = [ContractPosition("m2609", "DCE", long_today=1, long_price=3000)]

    exchange_mismatch = [ContractPosition("m2609", "CZCE", long_today=1, long_price=3000)]
    duplicates = [
        ContractPosition("m2609", "DCE", long_today=1, long_price=3000),
        ContractPosition("m2609", "DCE", long_today=1, long_price=3000),
    ]

    assert not compare_positions(expected, exchange_mismatch).matched
    result = compare_positions(expected, duplicates)
    assert not result.matched
    assert "duplicate" in result.details


def test_regular_sim_fills_market_limit_and_keeps_resting_order():
    spec = ContractSpec("m", "DCE", 10, 1, 0.1, 0.1, FeeSpec(open_fixed=2))
    broker = SimBroker(500000, {"m": spec})
    broker.start()
    now = datetime.now(timezone.utc)
    broker.publish_tick(Tick("m", "DCE", now, 2999, 3000, 2999.5, 10, 10, "20260821"))
    oid = broker.send_order(OrderRequest("m", "DCE", OrderSide.BUY, Offset.OPEN, 2, 3001))
    assert (
        broker.get_order(oid).status is OrderStatus.FILLED
        and broker.get_account().balance == 499996
    )
    resting = broker.send_order(OrderRequest("m", "DCE", OrderSide.BUY, Offset.OPEN, 1, 2990))
    assert broker.get_order(resting).status is OrderStatus.NOT_TRADED


def test_risk_account_limits_and_high_watermark_restore():
    manager = RiskManager(RiskConfig(max_daily_loss_ratio=0.01, max_total_drawdown_ratio=0.08))
    manager.restore_high_watermark(600000)
    manager.set_day_start_equity(560000, "20260821")
    account = AccountSnapshot(550000, 550000, 540000, 10000, 0, 0, "20260821")
    assert not manager.check_account(account).allowed
    assert manager.check_account(account).reason == "daily loss limit reached"
    manager.set_day_start_equity(550000, "20260821")
    assert manager.check_account(account).reason == "drawdown limit reached"

    fresh = RiskManager(RiskConfig())
    margin_breach = AccountSnapshot(100000, 100000, 64000, 36000, 0, 0, "20260825")
    assert not fresh.check_account(margin_breach).allowed
    assert fresh.check_account(margin_breach).reason == "margin ratio limit reached"
    cash_breach = AccountSnapshot(100000, 100000, 49000, 0, 0, 0, "20260825")
    assert not fresh.check_account(cash_breach).allowed
    assert fresh.check_account(cash_breach).reason == "available cash reserve too low"


def test_strategy_state_restores_history_and_position():
    pair = PairConfig("p", "m1", "m2", "DCE", 1, lookback=3, entry_z=1, exit_z=0.2)
    strategy = CalendarSpreadStrategy(pair)
    base = datetime(2026, 8, 21, 9, tzinfo=timezone.utc)
    for _i, s in enumerate([10, 11, 12]):
        strategy.on_quotes(
            Tick("m1", "DCE", base, 3000 + s - 0.5, 3000 + s + 0.5, 3000 + s, 10, 10, "20260821"),
            Tick("m2", "DCE", base, 2999.5, 3000.5, 3000, 10, 10, "20260821"),
        )
    restored = CalendarSpreadStrategy(pair)
    restored.restore_state(strategy.snapshot_state())
    assert len(restored.snapshot_state()["history"]) == 3 and restored.position == strategy.position


def test_state_store_kill_switch_requires_reconcile_and_metadata(tmp_path: Path):
    store = StateStore(tmp_path / "s.json")
    state = RuntimeState(kill_switch=True, reconciled=True, metadata_verified=False)
    store.save(state)
    assert store.load().kill_switch and not store.can_clear_kill_switch(state)
    state.metadata_verified = True
    assert store.can_clear_kill_switch(state)
