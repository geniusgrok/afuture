from datetime import date, datetime, timedelta, timezone

from afuture.auto import AutoConfig, AutoPairManager
from afuture.broker.sim import SimBroker
from afuture.execution import PairExecutor
from afuture.models import (
    ContractInfo,
    ContractSpec,
    PairConfig,
    SignalAction,
    SpreadSignal,
    Tick,
)
from afuture.risk import RiskConfig, RiskManager
from afuture.scanner import SpreadScanner
from afuture.strategy import CalendarSpreadStrategy


def specs() -> dict[str, ContractSpec]:
    return {
        "m2609": ContractSpec("m2609", "DCE", 10, 1, 0.10, 0.10),
        "m2701": ContractSpec("m2701", "DCE", 10, 1, 0.10, 0.10),
    }


def catalog() -> list[ContractInfo]:
    return [
        ContractInfo("m2609", "DCE", "m", "2026-09-15"),
        ContractInfo("m2701", "DCE", "m", "2027-01-15"),
    ]


def auto_config(**overrides) -> AutoConfig:
    values = dict(
        enabled=True,
        products=("m",),
        exchanges=("DCE",),
        max_active_pairs=1,
        max_contracts_per_product=2,
        min_days_to_expiry=15,
        scan_interval_seconds=0.0,
        lookback=3,
        entry_z=0.8,
        exit_z=0.2,
        stop_z=4.0,
        max_pair_volume=2,
        sample_seconds=0,
        min_volume=1000,
        min_open_interest=1000,
        min_liquidity_score=0.2,
        min_mean_reversion_score=0.0,
        max_half_life=1000,
        min_net_edge=0.0,
        slippage_ticks=0,
        session_windows=("09:00-11:30",),
    )
    values.update(overrides)
    return AutoConfig(**values)


def tick(
    symbol: str,
    when: datetime,
    bid: float,
    ask: float,
    *,
    bid_depth: float = 50,
    ask_depth: float = 50,
    volume: float = 20000,
    oi: float = 80000,
) -> Tick:
    return Tick(
        symbol=symbol,
        exchange="DCE",
        timestamp=when,
        bid_price=bid,
        ask_price=ask,
        last_price=(bid + ask) / 2,
        bid_volume=bid_depth,
        ask_volume=ask_depth,
        trading_day="20260821",
        volume=volume,
        open_interest=oi,
    )


def feed_spread(
    manager: AutoPairManager,
    when: datetime,
    spread: float,
    *,
    repeats: int = 1,
) -> None:
    for index in range(repeats):
        stamp = when + timedelta(seconds=index * 5)
        manager.observe(
            tick(
                "m2609",
                stamp,
                3000 + spread - 0.5,
                3000 + spread + 0.5,
            )
        )
        manager.observe(tick("m2701", stamp, 2999.5, 3000.5))


def boot_manager(config: AutoConfig) -> tuple[AutoPairManager, SimBroker]:
    broker = SimBroker(500000, specs(), contract_catalog=catalog())
    broker.start()
    manager = AutoPairManager(config)
    manager.bootstrap(broker, date(2026, 8, 21), {})
    return manager, broker


def test_dense_live_ticks_preserve_statistical_window():
    manager, broker = boot_manager(auto_config(sample_seconds=60, lookback=3))
    base = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)
    for minute, spread in enumerate([10, 11, 10, 25]):
        feed_spread(
            manager,
            base + timedelta(minutes=minute),
            spread,
            repeats=10,
        )

    selected = manager.select(
        broker,
        now=base + timedelta(minutes=4),
        protected_pair_ids=set(),
    )
    assert selected
    assert selected[0].pair_id == "auto_m_m2609_m2701"


def test_protected_pair_remains_eligible_when_hard_gates_pass():
    manager, broker = boot_manager(auto_config())
    pair = manager.candidate_pairs[0]
    base = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)
    for minute, spread in enumerate([10, 11, 10, 25]):
        feed_spread(manager, base + timedelta(minutes=minute), spread)

    selected = manager.select(
        broker,
        now=base + timedelta(minutes=4),
        protected_pair_ids={pair.pair_id},
    )
    assert selected and selected[0].pair_id == pair.pair_id
    assert pair.pair_id in manager.last_eligible_ids


def test_scanner_rejects_mid_only_dislocation_that_is_not_executable():
    pair = PairConfig(
        "p",
        "m2609",
        "m2701",
        "DCE",
        1,
        lookback=3,
        entry_z=1.0,
        exit_z=0.2,
        stop_z=4.0,
    )
    base = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)
    rows: list[Tick] = []
    for minute, spread in enumerate([9, 10, 11]):
        when = base + timedelta(minutes=minute)
        rows.extend(
            [
                tick(
                    "m2609",
                    when,
                    3000 + spread - 0.5,
                    3000 + spread + 0.5,
                ),
                tick("m2701", when, 2999.5, 3000.5),
            ]
        )

    latest = base + timedelta(minutes=3)
    rows.extend(
        [
            # mid spread is 13, but the executable short spread is only 9.
            tick("m2609", latest, 3010, 3016),
            tick("m2701", latest, 2999, 3001),
        ]
    )

    assert SpreadScanner(slippage_ticks=0).scan_pair(pair, rows, specs()) is None


def seeded_strategy(*, stop_z: float = 10.0) -> CalendarSpreadStrategy:
    pair = PairConfig(
        "p",
        "m2609",
        "m2701",
        "DCE",
        1,
        lookback=4,
        entry_z=1.0,
        exit_z=0.5,
        stop_z=stop_z,
        structural_mean_shift_z=1e12,
        structural_vol_ratio=1e12,
    )
    strategy = CalendarSpreadStrategy(pair)
    base = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)
    for minute, spread in enumerate([9, 10, 11, 10]):
        strategy.on_quotes(
            tick(
                "m2609",
                base + timedelta(minutes=minute),
                3000 + spread - 0.5,
                3000 + spread + 0.5,
            ),
            tick(
                "m2701",
                base + timedelta(minutes=minute),
                2999.5,
                3000.5,
            ),
        )
    strategy.set_position(1)
    return strategy


def test_long_spread_waits_until_liquidation_price_reverts():
    strategy = seeded_strategy()
    wide_time = datetime(2026, 8, 21, 9, 5, tzinfo=timezone.utc)
    wide = strategy.on_quotes(
        tick("m2609", wide_time, 3008, 3012),
        tick("m2701", wide_time, 2998, 3002),
    )
    assert wide.action is SignalAction.HOLD

    tight_time = wide_time + timedelta(minutes=1)
    tight = strategy.on_quotes(
        tick("m2609", tight_time, 3009.95, 3010.05),
        tick("m2701", tight_time, 2999.95, 3000.05),
    )
    assert tight.action is SignalAction.EXIT


def test_long_spread_stop_uses_adverse_liquidation_price():
    strategy = seeded_strategy(stop_z=4.0)
    when = datetime(2026, 8, 21, 9, 5, tzinfo=timezone.utc)
    signal = strategy.on_quotes(
        # Mid spread is only 8.5, while executable liquidation spread is 6.
        tick("m2609", when, 3007, 3010),
        tick("m2701", when, 2999, 3001),
    )
    assert signal.action is SignalAction.EMERGENCY_EXIT
    assert "stop" in signal.reason


def test_quote_gate_rejects_cross_leg_timestamp_skew():
    manager = RiskManager(RiskConfig())
    base = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)
    far = tick("m2701", base, 2999.5, 3000.5)
    near = tick(
        "m2609",
        base + timedelta(seconds=3),
        3009.5,
        3010.5,
    )
    decision = manager.check_quotes([near, far], near.timestamp)
    assert not decision.allowed
    assert "skew" in decision.reason


class RecordingSimBroker(SimBroker):
    def __init__(self) -> None:
        super().__init__(500000, specs())
        self.sent_symbols: list[str] = []

    def send_order(self, request):
        self.sent_symbols.append(request.symbol)
        return super().send_order(request)


def test_pair_executor_submits_thinner_leg_first():
    broker = RecordingSimBroker()
    broker.start()
    when = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)
    near = tick(
        "m2609",
        when,
        3010,
        3011,
        bid_depth=20,
        ask_depth=20,
    )
    far = tick(
        "m2701",
        when,
        3000,
        3001,
        bid_depth=2,
        ask_depth=20,
    )
    broker.publish_tick(near)
    broker.publish_tick(far)

    risk = RiskManager(RiskConfig(min_depth_multiple=2.0))
    executor = PairExecutor(broker, risk, specs(), slippage_ticks=0)
    pair = PairConfig(
        "p",
        "m2609",
        "m2701",
        "DCE",
        1,
        min_net_edge=0,
    )
    signal = SpreadSignal(
        "p",
        SignalAction.LONG_SPREAD,
        -2.0,
        when,
        11.0,
        20.0,
        1.0,
    )

    result = executor.execute_signal(
        pair,
        signal,
        near,
        far,
        open_pair_count=0,
        spread_std=1.0,
    )
    assert result.accepted
    assert broker.sent_symbols[:2] == ["m2701", "m2609"]
