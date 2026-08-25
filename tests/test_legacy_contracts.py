import json
from datetime import datetime, timezone
from pathlib import Path

from afuture.broker.sim import SimBroker
from afuture.fees import calculate_commission
from afuture.journal import AuditJournal
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
from afuture.report import calculate_performance, write_account_report
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


def test_reconcile_compares_position_quantities_not_prices():
    left = [ContractPosition("m", "DCE", long_today=1, long_price=3000)]
    right = [ContractPosition("m", "DCE", long_today=1, long_price=3100)]
    assert compare_positions(left, right).matched
    assert not compare_positions(left, [ContractPosition("m", "DCE", long_today=2)]).matched


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


def test_journal_and_report_are_json_serializable(tmp_path: Path):
    journal = AuditJournal(tmp_path / "audit.jsonl")
    journal.record("x", {"side": OrderSide.BUY})
    assert json.loads((tmp_path / "audit.jsonl").read_text())["payload"]["side"] == "BUY"
    metrics = calculate_performance(
        [("20260820", 500000), ("20260821", 505000)], initial_capital=500000, trade_count=2
    )
    assert metrics["total_return"] > 0 and metrics["trade_count"] == 2
    account = AccountSnapshot(505000, 505000, 505000, 0, 5000, 0, "20260821")
    write_account_report(tmp_path / "report.json", account, [], metrics)
    assert "performance" in json.loads((tmp_path / "report.json").read_text())
