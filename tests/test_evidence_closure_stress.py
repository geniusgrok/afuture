from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from afuture.auto import AutoConfig, AutoPairManager
from afuture.auto_acceptance import AutoPortfolioAcceptanceGate
from afuture.auto_research import AutoPortfolioResearchConfig, AutoPortfolioRunner
from afuture.broker.shadow import ShadowBroker
from afuture.config import AppConfig
from afuture.data import read_ticks
from afuture.data_quality import DataQualityAnalyzer
from afuture.models import ContractInfo, ContractSpec, Tick
from afuture.risk import RiskConfig
from afuture.sample_store import MarketSampleStore


def spec(symbol: str) -> ContractSpec:
    return ContractSpec(symbol, "DCE", 10, 1, 0.10, 0.10)


def tick(
    symbol: str, when: datetime, mid: float, *, day: str = "20260821", volume=20000, oi=80000
) -> Tick:
    return Tick(
        symbol=symbol,
        exchange="DCE",
        timestamp=when,
        bid_price=mid - 0.5,
        ask_price=mid + 0.5,
        last_price=mid,
        bid_volume=100,
        ask_volume=100,
        trading_day=day,
        volume=volume,
        open_interest=oi,
    )


def catalog() -> list[ContractInfo]:
    return [
        ContractInfo("m2609", "DCE", "m", "2026-09-15"),
        ContractInfo("m2701", "DCE", "m", "2027-01-15"),
    ]


def auto_config() -> AutoConfig:
    return AutoConfig(
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
        slippage_ticks=0,
        session_windows=("09:00-15:00",),
    )


def test_data_check_can_preserve_original_csv_order(tmp_path: Path):
    path = tmp_path / "ticks.csv"
    later = datetime(2026, 8, 21, 9, 1, tzinfo=timezone.utc)
    earlier = later - timedelta(minutes=1)
    path.write_text(
        "timestamp,symbol,exchange,bid_price,ask_price,last_price,bid_volume,ask_volume,trading_day,volume,open_interest\n"
        f"{later.isoformat()},m2609,DCE,3000,3001,3000.5,10,10,20260821,1000,1000\n"
        f"{earlier.isoformat()},m2609,DCE,2999,3000,2999.5,10,10,20260821,1000,1000\n",
        encoding="utf-8",
    )
    raw = read_ticks(path, sort_rows=False)
    result = DataQualityAnalyzer().analyze(raw, catalog(), auto_config())
    assert result.out_of_order_count == 1


def test_daily_auto_candidate_count_requires_quotes_for_both_legs():
    base = datetime(2026, 8, 21, 9, tzinfo=timezone.utc)
    rows = [tick("m2609", base, 3010)]
    result = DataQualityAnalyzer().analyze(rows, catalog(), auto_config())
    assert result.daily_auto_candidates["20260821"] == 0
    assert not result.passed


def test_auto_research_includes_data_gap_skew_and_activity_stress(tmp_path: Path):
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
    start = datetime(2026, 8, 1, 9, tzinfo=timezone.utc)
    for day in range(10):
        trading_day = f"202608{day + 1:02d}"
        for minute, spread in enumerate([10, 11, 10, 25, 10, 10]):
            when = start + timedelta(days=day, minutes=minute)
            rows.extend(
                [
                    tick("m2609", when, 3000 + spread, day=trading_day),
                    tick("m2701", when, 3000, day=trading_day),
                ]
            )
    result = AutoPortfolioRunner(cfg).run(
        rows,
        AutoPortfolioResearchConfig(
            train_days=4,
            validation_days=2,
            oos_days=2,
            step_days=2,
            cost_stress_multipliers=(1.0, 2.0),
        ),
    )
    assert "data_gap" in result.robustness
    assert "quote_skew" in result.robustness
    assert "activity_missing" in result.robustness
    assert {"0.5", "1.0", "2.0"}.issubset(result.robustness["quote_skew"])


def test_shadow_account_uses_live_trading_day():
    class Live:
        def start(self):
            pass

        def stop(self):
            pass

        def is_ready(self):
            return True

        def subscribe(self, symbol, exchange):
            pass

        def get_trading_day(self):
            return "20260822"

        def get_contract_catalog(self):
            return catalog()

        def get_live_contract_specs(self, symbols, timeout_seconds=10):
            return {symbol: spec(symbol) for symbol in symbols}

        def poll_events(self):
            return []

        def health_error(self):
            return None

        def snapshot_marker(self):
            return (0, 0)

        def snapshot_ready(self, marker):
            return True

    shadow = ShadowBroker(Live(), 500000)
    shadow.start()
    try:
        account = shadow.get_account()
        assert account.trading_day == "20260822"
    finally:
        shadow.stop()


def test_warm_samples_are_loaded_into_auto_manager(tmp_path: Path):
    store = MarketSampleStore(tmp_path / "samples", max_samples=8)
    base = datetime(2026, 8, 21, 9, tzinfo=timezone.utc)
    store.save("m2609", [tick("m2609", base + timedelta(minutes=i), 3010 + i) for i in range(3)])
    store.save("m2701", [tick("m2701", base + timedelta(minutes=i), 3000) for i in range(3)])
    manager = AutoPairManager(auto_config(), sample_store=store)
    manager.prepare_catalog(catalog(), base.date())
    pair = manager.candidate_pairs[0]
    seed = manager.strategy_seed(pair)
    assert len(seed["history"]) >= 2
    manager.close()


def test_preregistered_gate_rejects_no_oos_evidence():
    class EmptyResult:
        folds = []
        stress_results = {1.0: {"total_return": 0.0}, 2.0: {"total_return": 0.0}}
        robustness = {"leave_one_product_out": {}, "single_product": {}}

    decision = AutoPortfolioAcceptanceGate().evaluate(EmptyResult())
    assert not decision.accepted
    assert "no auto portfolio walk-forward folds" in decision.reasons
