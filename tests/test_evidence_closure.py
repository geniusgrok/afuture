from __future__ import annotations

import importlib.util
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Event

import pytest

from afuture.auto import AutoConfig, AutoPairManager
from afuture.broker.sim import SimBroker
from afuture.cli import build_parser
from afuture.config import AppConfig
from afuture.engine import TradingEngine
from afuture.models import (
    ContractInfo,
    ContractSpec,
    FeeSpec,
    Offset,
    OrderRequest,
    OrderSide,
    Tick,
)
from afuture.risk import RiskConfig, RiskManager
from afuture.state import StateStore


def spec(symbol: str, exchange: str = "DCE") -> ContractSpec:
    return ContractSpec(
        symbol,
        exchange,
        10,
        1,
        0.10,
        0.10,
        FeeSpec(open_fixed=1.0, close_fixed=1.0),
    )


def tick(
    symbol: str,
    when: datetime,
    mid: float,
    *,
    spread: float = 1.0,
    volume: float = 20000,
    oi: float = 80000,
    trading_day: str = "20260821",
) -> Tick:
    return Tick(
        symbol=symbol,
        exchange="DCE",
        timestamp=when,
        bid_price=mid - spread / 2,
        ask_price=mid + spread / 2,
        last_price=mid,
        bid_volume=100,
        ask_volume=100,
        trading_day=trading_day,
        volume=volume,
        open_interest=oi,
    )


def auto_config(**overrides) -> AutoConfig:
    values = dict(
        enabled=True,
        products=("m",),
        exchanges=("DCE",),
        max_active_pairs=1,
        max_pairs_per_product=1,
        max_contracts_per_product=2,
        min_days_to_expiry=10,
        scan_interval_seconds=0.0,
        max_sync_seconds=2.0,
        lookback=3,
        entry_z=0.8,
        exit_z=0.2,
        stop_z=4.0,
        max_pair_volume=1,
        sample_seconds=0,
        min_volume=100,
        min_open_interest=100,
        min_liquidity_score=0.1,
        min_stationarity_score=0.0,
        max_half_life=1000,
        min_net_edge=0.0,
        slippage_ticks=0,
        session_windows=("09:00-15:00",),
    )
    values.update(overrides)
    return AutoConfig(**values)


def catalog() -> list[ContractInfo]:
    return [
        ContractInfo("m2609", "DCE", "m", "2026-09-15"),
        ContractInfo("m2701", "DCE", "m", "2027-01-15"),
    ]


def test_engine_keeps_retiring_pair_managed_but_blocks_new_open(tmp_path: Path):
    broker = SimBroker(
        500000,
        {"m2609": spec("m2609"), "m2701": spec("m2701")},
        contract_catalog=catalog(),
    )
    pair = AutoPairManager(auto_config()).selector.build_pairs(catalog(), date(2026, 8, 21))[0]
    engine = TradingEngine(
        broker,
        [],
        {},
        RiskManager(RiskConfig()),
        StateStore(tmp_path / "state.json"),
        auto_manager=AutoPairManager(auto_config()),
        historical_mode=True,
    )
    engine.pairs[pair.pair_id] = pair
    engine._auto_pair_ids.add(pair.pair_id)
    engine._retiring_auto_pairs.add(pair.pair_id)
    assert pair.pair_id in engine.pairs
    assert hasattr(engine, "_pair_open_eligible")
    assert engine._pair_open_eligible(pair.pair_id) is False


def test_metadata_prefetch_does_not_block_selector_thread():
    assert importlib.util.find_spec("afuture.auto_runtime") is not None
    from afuture.auto_runtime import MetadataPrefetcher

    started = Event()
    release = Event()

    class SlowBroker:
        def get_live_contract_specs(self, symbols, timeout_seconds=10.0):
            started.set()
            release.wait(2.0)
            return {symbol: spec(symbol) for symbol in symbols}

    worker = MetadataPrefetcher(timeout_seconds=1.0)
    try:
        assert worker.request(SlowBroker(), ("m2609", "m2701")) is False
        assert started.wait(0.5)
        assert worker.get(("m2609", "m2701")) is None
        release.set()
        result = worker.wait(("m2609", "m2701"), timeout_seconds=1.0)
        assert set(result or {}) == {"m2609", "m2701"}
    finally:
        release.set()
        worker.close()


def test_sample_store_is_bounded_and_restores_recent_history(tmp_path: Path):
    assert importlib.util.find_spec("afuture.sample_store") is not None
    from afuture.sample_store import MarketSampleStore

    store = MarketSampleStore(tmp_path / "samples", max_samples=3)
    base = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)
    rows = [tick("m2609", base + timedelta(minutes=i), 3000 + i) for i in range(5)]
    store.save("m2609", rows)
    restored = store.load("m2609")
    assert [row.last_price for row in restored] == [3002, 3003, 3004]


def test_data_quality_detects_gap_and_reports_contract_coverage(tmp_path: Path):
    assert importlib.util.find_spec("afuture.data_quality") is not None
    from afuture.data_quality import DataQualityAnalyzer

    base = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)
    rows = [
        tick("m2609", base, 3010),
        tick("m2701", base, 3000),
        tick("m2609", base + timedelta(minutes=20), 3011),
        tick("m2701", base + timedelta(minutes=20), 3001),
    ]
    result = DataQualityAnalyzer(max_gap_seconds=300).analyze(rows, catalog())
    assert result.contract_count == 2
    assert result.trading_days == 1
    assert result.gap_count >= 2
    assert result.coverage_by_product["m"]["contracts"] == 2


def test_auto_portfolio_runner_uses_global_parameter_grid_and_reports_robustness(tmp_path: Path):
    assert importlib.util.find_spec("afuture.auto_research") is not None
    from afuture.auto_research import AutoPortfolioResearchConfig, AutoPortfolioRunner

    specs = {"m2609": spec("m2609"), "m2701": spec("m2701")}
    cfg = AppConfig(
        mode="replay",
        initial_capital=500000,
        contracts=specs,
        pairs=[],
        risk=RiskConfig(),
        ctp=None,
        slippage_ticks=0,
        conservative_simulation=True,
        state_path=str(tmp_path / "state.json"),
        report_path=str(tmp_path / "report.json"),
        journal_path=str(tmp_path / "audit.jsonl"),
        auto=auto_config(),
        contract_catalog=catalog(),
    )
    rows: list[Tick] = []
    day0 = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)
    for day in range(12):
        trading_day = f"202608{day + 1:02d}"
        for minute, spread_value in enumerate([10, 11, 10, 25, 10, 10]):
            when = day0 + timedelta(days=day, minutes=minute)
            rows.extend(
                [
                    tick("m2609", when, 3000 + spread_value, trading_day=trading_day),
                    tick("m2701", when, 3000, trading_day=trading_day),
                ]
            )
    research = AutoPortfolioResearchConfig(
        train_days=4,
        validation_days=2,
        oos_days=2,
        step_days=2,
        parameter_grid=(
            {"entry_z": 0.8, "exit_z": 0.2, "lookback": 3},
            {"entry_z": 1.0, "exit_z": 0.2, "lookback": 3},
        ),
        cost_stress_multipliers=(1.0, 2.0),
    )
    result = AutoPortfolioRunner(cfg).run(rows, research)
    assert result.folds
    assert set(result.stress_results) == {1.0, 2.0}
    assert "leave_one_product_out" in result.robustness
    assert "single_product" in result.robustness
    assert "remove_best_period" in result.robustness


def test_shadow_broker_never_sends_order_to_live_broker():
    assert importlib.util.find_spec("afuture.broker.shadow") is not None
    from afuture.broker.shadow import ShadowBroker

    class LiveBroker:
        def __init__(self):
            self.sent = 0

        def start(self):
            pass

        def stop(self):
            pass

        def is_ready(self):
            return True

        def subscribe(self, symbol, exchange):
            pass

        def send_order(self, request):
            self.sent += 1
            raise AssertionError("shadow must never delegate send_order")

        def get_contract_catalog(self):
            return catalog()

        def get_live_contract_specs(self, symbols, timeout_seconds=10.0):
            return {symbol: spec(symbol) for symbol in symbols}

        def poll_events(self):
            return []

        def get_account(self):
            from afuture.models import AccountSnapshot

            return AccountSnapshot(500000, 500000, 500000, 0, 0, 0, "20260821")

        def get_positions(self):
            return []

        def get_active_orders(self):
            return []

        def get_order(self, order_id):
            return None

        def cancel_order(self, order_id):
            pass

        def get_trading_day(self):
            return "20260821"

        def health_error(self):
            return None

    live = LiveBroker()
    shadow = ShadowBroker(live, 500000)
    shadow.start()
    try:
        shadow.update_specs({"m2609": spec("m2609")})
        now = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)
        shadow.publish_tick(tick("m2609", now, 3000))
        order_id = shadow.send_order(
            OrderRequest("m2609", "DCE", OrderSide.BUY, Offset.OPEN, 1, 3001)
        )
        assert order_id.startswith("SIM-")
        assert live.sent == 0
    finally:
        shadow.stop()


def test_execution_quality_round_trip_report(tmp_path: Path):
    assert importlib.util.find_spec("afuture.quality") is not None
    from afuture.quality import ExecutionQualityRecorder

    path = tmp_path / "quality.jsonl"
    recorder = ExecutionQualityRecorder(path)
    recorder.record_round_trip(
        pair_id="auto_m_m2609_m2701",
        expected_net_edge=120.0,
        realized_net_edge=90.0,
        expected_spread=10.0,
        entry_spread=10.5,
        exit_spread=9.0,
        commission=4.0,
        leg_latency_ms=180.0,
        partial_fill=False,
        rollback=False,
        reduce_only=False,
    )
    report = recorder.summary()
    assert report["round_trips"] == 1
    assert report["median_slippage"] == pytest.approx(0.5)
    assert report["p95_slippage"] == pytest.approx(0.5)
    assert report["realized_edge_total"] == pytest.approx(90.0)


def test_cli_exposes_evidence_closure_commands():
    parser = build_parser()
    help_text = parser.format_help()
    for command in ("accept-auto", "data-check", "shadow", "quality-report", "doctor"):
        assert command in help_text
