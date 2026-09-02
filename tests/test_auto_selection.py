from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from afuture.auto import AutoConfig, AutoPairManager, AutoPairSelector
from afuture.broker.ctp import CtpBroker, CtpCredentials
from afuture.broker.sim import SimBroker
from afuture.engine import TradingEngine
from afuture.models import ContractInfo, ContractSpec, PairConfig, Tick
from afuture.risk import RiskConfig, RiskManager
from afuture.scanner import SpreadScanner
from afuture.state import StateStore

_CHINA_TZ = timezone(timedelta(hours=8))


def contract(
    symbol: str, expiry: str, *, product: str = "m", exchange: str = "DCE"
) -> ContractInfo:
    return ContractInfo(symbol=symbol, exchange=exchange, product=product, expiry=expiry)


def tick(
    symbol: str, when: datetime, bid: float, ask: float, *, volume: float = 20000, oi: float = 80000
) -> Tick:
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
        volume=volume,
        open_interest=oi,
    )


def specs() -> dict[str, ContractSpec]:
    return {
        "m2609": ContractSpec("m2609", "DCE", 10, 1, 0.10, 0.10),
        "m2701": ContractSpec("m2701", "DCE", 10, 1, 0.10, 0.10),
        "m2705": ContractSpec("m2705", "DCE", 10, 1, 0.10, 0.10),
    }


def auto_config(**overrides) -> AutoConfig:
    values = dict(
        enabled=True,
        products=("m",),
        exchanges=("DCE",),
        max_active_pairs=1,
        max_contracts_per_product=3,
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
        session_windows=("09:00-11:30",),
    )
    values.update(overrides)
    return AutoConfig(**values)


@pytest.mark.parametrize("reported_day", [None, "", "2026-08-25", "20260230"])
def test_engine_trading_date_rejects_missing_or_invalid_broker_day(
    tmp_path: Path,
    reported_day: object,
) -> None:
    broker = SimBroker(500000, specs())
    broker.get_trading_day = lambda: reported_day
    engine = TradingEngine(
        broker,
        [],
        specs(),
        RiskManager(RiskConfig()),
        StateStore(tmp_path / "state.json"),
    )

    with pytest.raises(ValueError, match="broker trading day"):
        engine._trading_date()


def test_selector_builds_only_adjacent_unexpired_same_product_pairs():
    selector = AutoPairSelector(auto_config(max_active_pairs=2))
    catalog = [
        contract("m2605", "2026-05-15"),
        contract("m2609", "2026-09-15"),
        contract("m2701", "2027-01-15"),
        contract("m2705", "2027-05-15"),
        contract("rb2610", "2026-10-15", product="rb", exchange="SHFE"),
    ]
    pairs = selector.build_pairs(catalog, date(2026, 8, 21))
    assert [(p.near_symbol, p.far_symbol) for p in pairs] == [
        ("m2609", "m2701"),
        ("m2701", "m2705"),
    ]
    assert all(p.pair_id.startswith("auto_m_") for p in pairs)


def test_scanner_pairs_asynchronous_ticks_with_small_time_skew():
    pair = PairConfig("p", "m2609", "m2701", "DCE", 1, lookback=3)
    base = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)
    rows: list[Tick] = []
    for i, spread in enumerate([10, 11, 10, 20]):
        rows.append(
            tick("m2609", base + timedelta(minutes=i), 3000 + spread - 0.5, 3000 + spread + 0.5)
        )
        rows.append(tick("m2701", base + timedelta(minutes=i, milliseconds=600), 2999.5, 3000.5))
    candidate = SpreadScanner(max_sync_seconds=2.0).scan_pair(pair, rows, specs())
    assert candidate is not None
    assert abs(candidate.zscore) > 0.5


def test_auto_manager_selects_best_pair_but_keeps_open_pair_protected():
    manager = AutoPairManager(auto_config(max_active_pairs=1))
    catalog = [
        contract("m2609", "2026-09-15"),
        contract("m2701", "2027-01-15"),
        contract("m2705", "2027-05-15"),
    ]
    manager.prepare_catalog(catalog, date(2026, 8, 21))
    first, second = manager.candidate_pairs
    selected = manager.rank_candidates(
        [(first, 10.0), (second, 100.0)],
        protected_pair_ids={first.pair_id},
    )
    assert [pair.pair_id for pair in selected] == [first.pair_id]


def test_engine_auto_discovers_pair_and_persists_it(tmp_path: Path):
    catalog = [
        contract("m2609", "2026-09-15"),
        contract("m2701", "2027-01-15"),
    ]
    broker = SimBroker(500000, specs(), contract_catalog=catalog)
    store = StateStore(tmp_path / "state.json")
    engine = TradingEngine(
        broker,
        [],
        {},
        RiskManager(RiskConfig(max_quote_age_seconds=5)),
        store,
        slippage_ticks=0,
        auto_manager=AutoPairManager(auto_config()),
        historical_mode=True,
    )
    engine.start()
    base = datetime(2026, 8, 21, 9, 0, tzinfo=_CHINA_TZ)
    for i, spread in enumerate([10, 11, 10, 25]):
        broker.publish_tick(
            tick("m2609", base + timedelta(minutes=i), 3000 + spread - 0.5, 3000 + spread + 0.5)
        )
        engine.run_once()
        broker.publish_tick(
            tick("m2701", base + timedelta(minutes=i, milliseconds=500), 2999.5, 3000.5)
        )
        engine.run_once()
    assert engine.pairs
    # 被自动选中的候选必须进入原有策略/风控/执行链，而不是只停留在候选列表。
    assert broker.get_positions()
    saved = store.load()
    assert saved.auto_pairs
    assert set(saved.auto_pairs) == set(engine.pairs)


def test_engine_restores_persisted_auto_pair_before_reconciliation(tmp_path: Path):
    catalog = [contract("m2609", "2026-09-15"), contract("m2701", "2027-01-15")]
    pair = AutoPairSelector(auto_config()).build_pairs(catalog, date(2026, 8, 21))[0]
    store = StateStore(tmp_path / "state.json")
    state = store.load()
    state.auto_pairs[pair.pair_id] = asdict(pair)
    store.save(state)

    broker = SimBroker(500000, specs(), contract_catalog=catalog)
    engine = TradingEngine(
        broker,
        [],
        {},
        RiskManager(RiskConfig()),
        store,
        auto_manager=AutoPairManager(auto_config()),
        historical_mode=True,
    )
    engine.start()
    assert pair.pair_id in engine.pairs
    assert pair.near_symbol in engine.specs and pair.far_symbol in engine.specs


def test_ctp_contract_catalog_captures_raw_instrument_metadata():
    broker = CtpBroker(CtpCredentials("u", "p", "b", "td", "md", "", ""))
    broker._trading_day = "20260825"
    broker._handle_contract_metadata(
        {
            "InstrumentID": "m2609",
            "ExchangeID": "DCE",
            "ProductID": "m",
            "ExpireDate": "20260915",
            "ProductClass": "1",
        }
    )
    row = broker.get_contract_catalog()[0]
    assert row.symbol == "m2609"
    assert row.product == "M"
    assert row.expiry == "2026-09-15"


def test_config_loads_auto_mode_without_static_pairs(tmp_path: Path):
    from afuture.config import load_config

    path = tmp_path / "auto.toml"
    path.write_text(
        """
[system]
mode = "replay"
initial_capital = 500000

[[contracts]]
symbol = "m2609"
exchange = "DCE"
product = "m"
expiry = "2026-09-15"
multiplier = 10
price_tick = 1
margin_rate_long = 0.1
margin_rate_short = 0.1

[[contracts]]
symbol = "m2701"
exchange = "DCE"
product = "m"
expiry = "2027-01-15"
multiplier = 10
price_tick = 1
margin_rate_long = 0.1
margin_rate_short = 0.1

[auto]
enabled = true
products = ["m"]
exchanges = ["DCE"]
max_active_pairs = 2
max_contracts_per_product = 3
lookback = 20
entry_z = 2.0
exit_z = 0.5
stop_z = 4.0
session_windows = ["09:00-11:30"]
""",
        encoding="utf-8",
    )
    loaded = load_config(path)
    assert loaded.auto.enabled
    assert loaded.auto.products == ("m",)
    assert loaded.pairs == []


def test_auto_pair_retires_after_position_is_flat_and_signal_edge_disappears(tmp_path: Path):
    catalog = [contract("m2609", "2026-09-15"), contract("m2701", "2027-01-15")]
    broker = SimBroker(500000, specs(), contract_catalog=catalog)
    engine = TradingEngine(
        broker,
        [],
        {},
        RiskManager(RiskConfig()),
        StateStore(tmp_path / "state.json"),
        slippage_ticks=0,
        auto_manager=AutoPairManager(auto_config(exit_z=0.3)),
        historical_mode=True,
    )
    engine.start()
    base = datetime(2026, 8, 21, 9, 0, tzinfo=_CHINA_TZ)
    for i, spread in enumerate([10, 11, 10, 25, 10, 10]):
        broker.publish_tick(
            tick("m2609", base + timedelta(minutes=i), 3000 + spread - 0.5, 3000 + spread + 0.5)
        )
        engine.run_once()
        broker.publish_tick(
            tick("m2701", base + timedelta(minutes=i, milliseconds=500), 2999.5, 3000.5)
        )
        engine.run_once()
        engine.run_once()
    assert broker.get_positions() == []
    assert engine.pairs == {}
    assert StateStore(tmp_path / "state.json").load().auto_pairs == {}


def test_auto_manager_refreshes_expiry_filter_on_new_trading_day():
    catalog = [
        contract("m2609", "2026-09-15"),
        contract("m2701", "2027-01-15"),
        contract("m2705", "2027-05-15"),
    ]
    broker = SimBroker(500000, specs(), contract_catalog=catalog)
    broker.start()
    manager = AutoPairManager(auto_config(min_days_to_expiry=20, max_contracts_per_product=3))
    manager.bootstrap(broker, date(2026, 8, 21), {})
    assert manager.candidate_pairs[0].near_symbol == "m2609"
    changed = manager.refresh_if_needed(broker, date(2026, 9, 1))
    assert changed
    assert all(pair.near_symbol != "m2609" for pair in manager.candidate_pairs)


def test_live_auto_config_does_not_require_static_contracts_or_pairs(tmp_path: Path, monkeypatch):
    from afuture.config import load_config

    for key, value in {
        "AFUTURE_CTP_USER": "u",
        "AFUTURE_CTP_PASSWORD": "p",
        "AFUTURE_CTP_BROKER": "b",
    }.items():
        monkeypatch.setenv(key, value)
    path = tmp_path / "live-auto.toml"
    path.write_text(
        """
[system]
mode = "live"
initial_capital = 500000

[ctp]
environment = "test"
td_address = "tcp://td"
md_address = "tcp://md"

[auto]
enabled = true
products = ["m", "rb"]
exchanges = ["DCE", "SHFE"]
max_active_pairs = 2
session_windows = ["09:00-10:15", "10:30-11:30", "13:30-15:00"]
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.mode == "live"
    assert config.auto.enabled
    assert config.contracts == {}
    assert config.pairs == []


def test_replay_uses_same_auto_selection_lifecycle(tmp_path: Path):
    from afuture.config import AppConfig
    from afuture.replay import run_replay

    catalog = [contract("m2609", "2026-09-15"), contract("m2701", "2027-01-15")]
    config = AppConfig(
        mode="replay",
        initial_capital=500000,
        contracts={k: v for k, v in specs().items() if k in {"m2609", "m2701"}},
        pairs=[],
        risk=RiskConfig(),
        ctp=None,
        slippage_ticks=0,
        state_path=str(tmp_path / "state.json"),
        report_path=str(tmp_path / "report.json"),
        journal_path=str(tmp_path / "audit.jsonl"),
        auto=auto_config(exit_z=0.3),
        contract_catalog=catalog,
    )
    data = tmp_path / "ticks.csv"
    base = datetime(2026, 8, 21, 9, 0, tzinfo=_CHINA_TZ)
    rows = [
        "timestamp,symbol,exchange,bid_price,ask_price,last_price,bid_volume,ask_volume,trading_day,volume,open_interest"
    ]
    for i, spread in enumerate([10, 11, 10, 25, 10, 10]):
        stamp = (base + timedelta(minutes=i)).isoformat()
        rows.append(
            f"{stamp},m2609,DCE,{3000 + spread - 0.5},{3000 + spread + 0.5},{3000 + spread},50,50,20260821,20000,80000"
        )
        rows.append(f"{stamp},m2701,DCE,2999.5,3000.5,3000,50,50,20260821,22000,90000")
    data.write_text("\n".join(rows) + "\n", encoding="utf-8")
    account = run_replay(config, data)
    assert account.margin == 0
    report = (tmp_path / "report.json").read_text(encoding="utf-8")
    assert '"trade_count": 4' in report


def test_research_pair_generation_matches_live_auto_selector(tmp_path: Path):
    from afuture.cli import _research_pairs
    from afuture.config import AppConfig

    catalog = [contract("m2609", "2026-09-15"), contract("m2701", "2027-01-15")]
    config = AppConfig(
        "replay",
        500000,
        {k: v for k, v in specs().items() if k in {"m2609", "m2701"}},
        [],
        RiskConfig(),
        None,
        auto=auto_config(),
        contract_catalog=catalog,
    )
    rows = [tick("m2609", datetime(2026, 8, 21, 9, tzinfo=timezone.utc), 3010, 3011)]
    generated = _research_pairs(config, rows)
    assert len(generated) == 1
    assert generated[0].pair_id == "auto_m_m2609_m2701"


def test_live_metadata_is_rechecked_when_trading_day_changes(tmp_path: Path):
    from afuture.models import AccountSnapshot, BrokerEvent

    class CountingBroker(SimBroker):
        def __init__(self):
            super().__init__(500000, {"m2609": specs()["m2609"]})
            self.metadata_calls = 0
            self._trading_day = "20260821"

        def get_live_contract_specs(self, symbols, timeout_seconds=10.0):
            self.metadata_calls += 1
            return super().get_live_contract_specs(symbols, timeout_seconds)

    broker = CountingBroker()
    engine = TradingEngine(
        broker,
        [],
        {"m2609": specs()["m2609"]},
        RiskManager(RiskConfig()),
        StateStore(tmp_path / "state.json"),
        require_live_metadata=True,
    )
    engine.start()
    assert broker.metadata_calls == 1
    broker._events.append(
        BrokerEvent(
            "account",
            AccountSnapshot(500000, 500000, 500000, 0, 0, 0, "20260822"),
        )
    )
    engine.run_once()
    assert broker.metadata_calls == 2


def test_auto_manager_queries_ctp_rates_only_after_statistical_prefilter():
    catalog = [
        contract("m2609", "2026-09-15", product="m"),
        contract("m2701", "2027-01-15", product="m"),
        contract("y2609", "2026-09-15", product="y"),
        contract("y2701", "2027-01-15", product="y"),
    ]
    all_specs = {
        **{k: v for k, v in specs().items() if k in {"m2609", "m2701"}},
        "y2609": ContractSpec("y2609", "DCE", 10, 1, 0.1, 0.1),
        "y2701": ContractSpec("y2701", "DCE", 10, 1, 0.1, 0.1),
    }

    class CountingBroker(SimBroker):
        def __init__(self):
            super().__init__(500000, all_specs, contract_catalog=catalog)
            self.queried: list[str] = []

        def get_live_contract_specs(self, symbols, timeout_seconds=10.0):
            self.queried.extend(symbols)
            return super().get_live_contract_specs(symbols, timeout_seconds)

    manager = AutoPairManager(
        auto_config(products=("m", "y"), max_active_pairs=1, max_contracts_per_product=2)
    )
    broker = CountingBroker()
    broker.start()
    manager.bootstrap(broker, date(2026, 8, 21), {})
    base = datetime(2026, 8, 21, 9, tzinfo=timezone.utc)
    for i, (m_spread, y_spread) in enumerate(zip([10, 11, 10, 10], [10, 11, 10, 25], strict=True)):
        for symbol, spread in (("m2609", m_spread), ("y2609", y_spread)):
            manager.observe(
                tick(symbol, base + timedelta(minutes=i), 3000 + spread - 0.5, 3000 + spread + 0.5)
            )
        manager.observe(
            tick("m2701", base + timedelta(minutes=i, milliseconds=500), 2999.5, 3000.5)
        )
        manager.observe(
            tick("y2701", base + timedelta(minutes=i, milliseconds=500), 2999.5, 3000.5)
        )
    manager.select(broker, now=base + timedelta(minutes=4), protected_pair_ids=set())
    assert set(broker.queried) == {"y2609", "y2701"}
