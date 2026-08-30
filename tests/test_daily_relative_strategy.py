from datetime import date, datetime, timedelta, timezone
from math import log

from afuture.auto import AutoConfig, AutoPairManager, AutoPairSelector
from afuture.models import ContractInfo, PairConfig, SignalAction, Tick
from afuture.scanner import SpreadScanner
from afuture.strategy import CalendarSpreadStrategy


def tick(symbol: str, when: datetime, mid: float, trading_day: str = "20260821") -> Tick:
    return Tick(
        symbol=symbol,
        exchange="DCE",
        timestamp=when,
        bid_price=mid - 0.5,
        ask_price=mid + 0.5,
        last_price=mid,
        bid_volume=100,
        ask_volume=100,
        trading_day=trading_day,
        volume=20000,
        open_interest=80000,
    )


def book_tick(
    symbol: str,
    when: datetime,
    bid: float,
    ask: float,
    trading_day: str = "20260821",
) -> Tick:
    return Tick(
        symbol=symbol,
        exchange="DCE",
        timestamp=when,
        bid_price=bid,
        ask_price=ask,
        last_price=(bid + ask) / 2,
        bid_volume=100,
        ask_volume=100,
        trading_day=trading_day,
        volume=20000,
        open_interest=80000,
    )


def relative_pair(**overrides) -> PairConfig:
    values = dict(
        pair_id="m_daily",
        near_symbol="m2609",
        far_symbol="m2701",
        exchange="DCE",
        volume=5,
        lookback=4,
        entry_z=2.5,
        exit_z=0.75,
        stop_z=4.0,
        signal_transform="log_ratio",
        confirm_entry=True,
        confirmation_retrace_z=0.3,
        min_confirmed_entry_z=1.75,
        entry_trend_window=3,
        max_entry_z_slope=0.75,
        min_stationarity_score=0.0,
        max_half_life=999.0,
        daily_sample_window="14:55-15:00",
    )
    values.update(overrides)
    return PairConfig(**values)


def test_daily_sample_window_records_only_one_observation_per_trading_day():
    strategy = CalendarSpreadStrategy(relative_pair())

    before = datetime(2026, 8, 21, 6, 30, tzinfo=timezone.utc)  # 14:30 China
    strategy.on_quotes(tick("m2609", before, 3100), tick("m2701", before, 3000))
    assert strategy.snapshot_state()["history"] == []

    close = datetime(2026, 8, 21, 6, 56, tzinfo=timezone.utc)  # 14:56 China
    strategy.on_quotes(tick("m2609", close, 3100), tick("m2701", close, 3000))
    first = strategy.snapshot_state()
    assert len(first["history"]) == 1
    assert first["last_sample_trading_day"] == "20260821"

    later = datetime(2026, 8, 21, 6, 59, tzinfo=timezone.utc)
    strategy.on_quotes(tick("m2609", later, 3110), tick("m2701", later, 3000))
    assert strategy.snapshot_state()["history"] == first["history"]


def test_log_ratio_history_is_scale_invariant():
    left = CalendarSpreadStrategy(relative_pair(daily_sample_window=""))
    right = CalendarSpreadStrategy(relative_pair(daily_sample_window=""))
    base = datetime(2026, 8, 21, 1, 0, tzinfo=timezone.utc)

    for index, (near, far) in enumerate([(101, 100), (102, 100), (101, 100), (103, 100)]):
        when = base.replace(hour=1 + index)
        left.on_quotes(tick("m2609", when, near), tick("m2701", when, far))
        right.on_quotes(tick("m2609", when, near * 10), tick("m2701", when, far * 10))

    assert left.snapshot_state()["history"] == right.snapshot_state()["history"]
    assert left.spread_std != right.spread_std  # sizing remains in raw spread currency units


def test_confirmation_waits_for_retrace_before_opening():
    # Confirmation is a rolling-window behavior. Use a realistic 20-sample window so
    # the armed extreme does not dominate the very next reference distribution.
    strategy = CalendarSpreadStrategy(
        relative_pair(
            daily_sample_window="",
            lookback=20,
            entry_trend_window=6,
            max_entry_z_slope=999.0,
        )
    )
    base = datetime(2026, 8, 1, 1, 0, tzinfo=timezone.utc)

    for index, ratio in enumerate([1.000, 1.002, 0.998, 1.001] * 5):
        when = base + timedelta(hours=index)
        strategy.on_quotes(
            tick("m2609", when, 3000 * ratio),
            tick("m2701", when, 3000),
        )

    extreme_time = base + timedelta(hours=21)
    extreme = strategy.on_quotes(
        tick("m2609", extreme_time, 3060),
        tick("m2701", extreme_time, 3000),
    )
    assert extreme.action is SignalAction.HOLD

    confirm_time = base + timedelta(hours=22)
    confirmed = strategy.on_quotes(
        tick("m2609", confirm_time, 3054),
        tick("m2701", confirm_time, 3000),
    )
    assert confirmed.action is SignalAction.SHORT_SPREAD
    assert "confirmed" in confirmed.reason


def test_confirmation_seed_consumes_historical_confirmation():
    pair = relative_pair(
        daily_sample_window="",
        lookback=20,
        entry_trend_window=6,
        max_entry_z_slope=999.0,
    )
    scanner = SpreadScanner()
    base = datetime(2026, 8, 1, 1, 0, tzinfo=timezone.utc)
    synchronized: list[tuple[Tick, Tick]] = []
    for index, ratio in enumerate([1.000, 1.002, 0.998, 1.001] * 5):
        when = base + timedelta(hours=index)
        synchronized.append((tick("m2609", when, 3000 * ratio), tick("m2701", when, 3000)))
    synchronized.append(
        (
            tick("m2609", base + timedelta(hours=21), 3060),
            tick("m2701", base + timedelta(hours=21), 3000),
        )
    )
    synchronized.append(
        (
            tick("m2609", base + timedelta(hours=22), 3054),
            tick("m2701", base + timedelta(hours=22), 3000),
        )
    )

    _z_history, armed, extreme = scanner.confirmation_seed(pair, synchronized)
    assert armed == 0
    assert extreme == 0.0


def test_log_ratio_position_waits_for_executable_liquidation_reversion():
    strategy = CalendarSpreadStrategy(
        relative_pair(
            daily_sample_window="",
            confirm_entry=False,
            stop_z=100.0,
        )
    )
    strategy.restore_state(
        {
            "history": [log(1.0), log(1.001), log(0.999), log(1.0)],
            "raw_history": [0.0, 3.0, -3.0, 0.0],
            "position": 1,
            "entry_mean": 0.0,
            "entry_std": 2.0,
        }
    )
    when = datetime(2026, 8, 21, 6, 56, tzinfo=timezone.utc)
    signal = strategy.on_quotes(
        book_tick("m2609", when, 2990, 3010),
        book_tick("m2701", when, 2990, 3010),
    )
    # Mid prices look fully reverted, but selling near/buying far still realizes a
    # strongly adverse relative value. A normal EXIT here would recreate the exact
    # bid/ask optimism removed from the legacy spread path.
    assert signal.action is SignalAction.HOLD


def test_daily_scanner_keeps_historical_samples_when_leg_ticks_are_seconds_apart():
    pair = relative_pair()
    scanner = SpreadScanner(max_sync_seconds=2.0)
    rows: list[Tick] = []
    base = datetime(2026, 8, 17, 6, 56, tzinfo=timezone.utc)
    for offset in range(5):
        day = (date(2026, 8, 17) + timedelta(days=offset)).strftime("%Y%m%d")
        when = base + timedelta(days=offset)
        rows.append(tick("m2609", when, 3100 + offset, day))
        rows.append(tick("m2701", when + timedelta(seconds=10), 3000, day))

    synchronized = scanner.synchronized_ticks(pair, rows)
    # The 2-second rule is an execution gate, not a reason to destroy a once-per-day
    # statistical observation. Engine/RiskManager still rejects stale current books.
    assert len(synchronized) == 5
    assert [near.trading_day for near, _ in synchronized] == [
        "20260817",
        "20260818",
        "20260819",
        "20260820",
        "20260821",
    ]


def test_auto_profile_copies_relative_daily_quality_parameters():
    config = AutoConfig(
        enabled=True,
        products=("m", "OI"),
        exchanges=("DCE", "CZCE"),
        lookback=25,
        entry_z=2.5,
        exit_z=0.75,
        stop_z=4.0,
        signal_transform="log_ratio",
        confirm_entry=True,
        confirmation_retrace_z=0.3,
        min_confirmed_entry_z=1.75,
        entry_trend_window=6,
        max_entry_z_slope=0.75,
        min_stationarity_score=0.01,
        max_half_life=60.0,
        daily_sample_window="22:55-23:00",
    )
    catalog = [
        ContractInfo("m2609", "DCE", "m", "2026-09-15"),
        ContractInfo("m2701", "DCE", "m", "2027-01-15"),
    ]
    pair = AutoPairSelector(config).build_pairs(catalog, date(2026, 8, 21))[0]
    assert pair.signal_transform == "log_ratio"
    assert pair.confirm_entry
    assert pair.confirmation_retrace_z == 0.3
    assert pair.min_confirmed_entry_z == 1.75
    assert pair.max_entry_z_slope == 0.75
    assert pair.min_stationarity_score == config.min_stationarity_score
    assert pair.daily_sample_window == "22:55-23:00"


def test_mean_reversion_heuristic_keeps_legacy_numeric_entry_gate():
    blocked = CalendarSpreadStrategy(
        relative_pair(daily_sample_window="", confirm_entry=False, min_stationarity_score=0.8)
    )
    blocked.restore_state({"history": [1.0, 1.4, 1.5, 1.55]})

    half_life, heuristic = blocked._mean_reversion_stats()

    assert abs(heuristic - 5.0 / 7.0) < 1e-12
    assert abs(half_life - 7.0 * log(2.0) / 5.0) < 1e-12
    assert blocked._entry_action(
        0.0,
        -3.0,
        3.0,
        slope=0.0,
        half_life=half_life,
        stationarity=heuristic,
    ) == (SignalAction.HOLD, "")

    allowed = CalendarSpreadStrategy(
        relative_pair(daily_sample_window="", confirm_entry=False, min_stationarity_score=0.7)
    )
    assert allowed._entry_action(
        0.0,
        -3.0,
        3.0,
        slope=0.0,
        half_life=half_life,
        stationarity=heuristic,
    ) == (SignalAction.SHORT_SPREAD, "")


def test_auto_history_round_trips_for_daily_restart_warmup():
    manager = AutoPairManager(
        AutoConfig(
            enabled=True,
            products=("m",),
            exchanges=("DCE",),
            lookback=4,
            daily_sample_window="14:55-15:00",
        )
    )
    catalog = [
        ContractInfo("m2609", "DCE", "m", "2026-09-15"),
        ContractInfo("m2701", "DCE", "m", "2027-01-15"),
    ]
    manager.prepare_catalog(catalog, date(2026, 8, 21))
    close = datetime(2026, 8, 21, 6, 56, tzinfo=timezone.utc)
    manager.observe(tick("m2609", close, 3100))
    snapshot = manager.snapshot_history()

    restored = AutoPairManager(manager.config)
    restored.prepare_catalog(catalog, date(2026, 8, 21))
    restored.restore_history(snapshot)
    assert restored.snapshot_history() == snapshot
