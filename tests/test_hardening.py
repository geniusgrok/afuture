import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from afuture.broker.sim import SimBroker
from afuture.calibration import ParameterCalibrator
from afuture.economics import estimate_net_edge, executable_spreads
from afuture.health.monitor import HealthMonitor
from afuture.metadata import validate_contract_metadata
from afuture.models import (
    AccountSnapshot,
    ContractSpec,
    FeeSpec,
    Offset,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    PairConfig,
    SignalAction,
    Tick,
)
from afuture.portfolio_risk import PortfolioRiskAnalyzer
from afuture.research import (
    AcceptanceGate,
    FoldResult,
    ResearchConfig,
    ResearchResult,
    WalkForwardRunner,
)
from afuture.risk import RiskConfig, RiskManager
from afuture.scanner import SpreadScanner
from afuture.state import RuntimeState, StateIntegrityError, StateStore
from afuture.strategy import CalendarSpreadStrategy

_CHINA_TZ = timezone(timedelta(hours=8))


def tick(symbol, bid, ask, minute=0, *, day="20260821", depth=20, volume=10000, oi=50000):
    return Tick(
        symbol,
        "DCE",
        datetime(2026, 8, 21, 9, minute, tzinfo=_CHINA_TZ),
        bid,
        ask,
        (bid + ask) / 2,
        depth,
        depth,
        day,
        limit_up=3400,
        limit_down=2600,
        volume=volume,
        open_interest=oi,
    )


def specs(fee=False):
    fs = FeeSpec(open_fixed=2, close_fixed=2) if fee else FeeSpec()
    return {
        "m2609": ContractSpec("m2609", "DCE", 10, 1, 0.1, 0.1, fs),
        "m2701": ContractSpec("m2701", "DCE", 10, 1, 0.1, 0.1, fs),
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("bid_price", float("nan")),
        ("ask_price", float("inf")),
        ("last_price", 0.0),
        ("last_price", float("nan")),
        ("bid_volume", float("inf")),
        ("ask_volume", float("nan")),
        ("limit_up", float("inf")),
        ("limit_down", -1.0),
        ("volume", float("nan")),
        ("open_interest", float("inf")),
    ],
)
def test_tick_validation_rejects_non_finite_or_invalid_market_values(
    field: str,
    value: float,
) -> None:
    damaged = replace(tick("m2609", 3000, 3001), **{field: value})

    with pytest.raises(ValueError):
        damaged.validate()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("balance", float("nan")),
        ("equity", float("inf")),
        ("available", -1.0),
        ("margin", float("nan")),
        ("realized_pnl", float("inf")),
        ("unrealized_pnl", float("nan")),
    ],
)
def test_account_risk_rejects_invalid_economic_truth(field: str, value: float) -> None:
    account = AccountSnapshot(500000, 500000, 450000, 50000, 0, 0, "20260821")

    decision = RiskManager().check_account(replace(account, **{field: value}))

    assert not decision.allowed
    assert "invalid account snapshot" in decision.reason


@pytest.mark.parametrize(
    ("side", "field", "value"),
    [
        ("local", "multiplier", float("nan")),
        ("remote", "price_tick", float("inf")),
        ("local", "margin_rate_long", 0.0),
        ("remote", "margin_rate_short", float("nan")),
    ],
)
def test_metadata_gate_rejects_invalid_contract_economics(
    side: str,
    field: str,
    value: float,
) -> None:
    valid = ContractSpec("m2609", "DCE", 10, 1, 0.1, 0.1)
    local = valid
    remote = valid
    if side == "local":
        local = replace(valid, **{field: value})
    else:
        remote = replace(valid, **{field: value})

    decision = validate_contract_metadata({"m2609": local}, {"m2609": remote})

    assert not decision.allowed
    assert "invalid" in decision.reason


@pytest.mark.parametrize("side", ["local", "remote"])
def test_metadata_gate_rejects_non_finite_or_negative_fees(side: str) -> None:
    valid = ContractSpec("m2609", "DCE", 10, 1, 0.1, 0.1)
    invalid = replace(valid, fee=FeeSpec(open_rate=float("nan")))
    local, remote = (invalid, valid) if side == "local" else (valid, invalid)

    decision = validate_contract_metadata({"m2609": local}, {"m2609": remote})

    assert not decision.allowed
    assert "invalid" in decision.reason


def test_executable_spreads_are_directional():
    assert executable_spreads(tick("m2609", 3010, 3011), tick("m2701", 3000, 3001)) == (11, 9)


def test_net_edge_deducts_round_trip_costs_and_stress_multiplier():
    near, far = tick("m2609", 3020, 3021), tick("m2701", 2999, 3000)
    base = estimate_net_edge(
        SignalAction.SHORT_SPREAD,
        reference_mean=10,
        near=near,
        far=far,
        specs=specs(True),
        volume=1,
        slippage_ticks=1,
        legging_buffer=10,
    )
    stressed = estimate_net_edge(
        SignalAction.SHORT_SPREAD,
        reference_mean=10,
        near=near,
        far=far,
        specs=specs(True),
        volume=1,
        slippage_ticks=1,
        legging_buffer=10,
        cost_multiplier=2,
    )
    assert base.gross_edge > 0 and base.net_edge < base.gross_edge
    assert stressed.transaction_cost == pytest.approx(base.transaction_cost * 2)
    assert stressed.legging_buffer == pytest.approx(base.legging_buffer * 2)
    assert stressed.net_edge < base.net_edge


def test_strategy_uses_executable_entry_and_max_holding_exit():
    pair = PairConfig(
        "p",
        "m2609",
        "m2701",
        "DCE",
        1,
        lookback=4,
        entry_z=1,
        exit_z=0.2,
        stop_z=1e12,
        max_holding_samples=2,
        structural_mean_shift_z=1e12,
        structural_vol_ratio=1e12,
    )
    strategy = CalendarSpreadStrategy(pair)
    for i in range(4):
        strategy.on_quotes(tick("m2609", 3009.5, 3010.5, i), tick("m2701", 2999.5, 3000.5, i))
    signal = strategy.on_quotes(tick("m2609", 3019, 3020, 5), tick("m2701", 2999, 3000, 5))
    assert signal.action is SignalAction.SHORT_SPREAD
    strategy.set_position(-1)
    strategy.on_quotes(tick("m2609", 3018, 3019, 6), tick("m2701", 2999, 3000, 6))
    signal = strategy.on_quotes(tick("m2609", 3018, 3019, 7), tick("m2701", 2999, 3000, 7))
    assert signal.action is SignalAction.EMERGENCY_EXIT
    assert "holding" in signal.reason


def test_strategy_structural_break_forces_exit():
    pair = PairConfig(
        "p",
        "m2609",
        "m2701",
        "DCE",
        1,
        lookback=5,
        entry_z=0.8,
        exit_z=0.1,
        stop_z=20,
        structural_mean_shift_z=1.2,
        structural_vol_ratio=10,
    )
    strategy = CalendarSpreadStrategy(pair)
    for i in range(5):
        strategy.on_quotes(tick("m2609", 3009.5, 3010.5, i), tick("m2701", 2999.5, 3000.5, i))
    strategy.set_position(1)
    signal = strategy.on_quotes(tick("m2609", 3029.5, 3030.5, 8), tick("m2701", 2999.5, 3000.5, 8))
    assert signal.action is SignalAction.EMERGENCY_EXIT
    assert "structural" in signal.reason


def test_market_entry_and_dynamic_sizing_are_enforced():
    manager = RiskManager(
        RiskConfig(min_depth_multiple=2, max_bid_ask_ticks=2, risk_budget_ratio=0.002)
    )
    pair = PairConfig("p", "m2609", "m2701", "DCE", 10, session_windows=("09:00-11:30",))
    account = AccountSnapshot(500000, 500000, 450000, 50000, 0, 0, "20260821")
    near, far = tick("m2609", 3000, 3001, depth=20), tick("m2701", 2990, 2991, depth=20)
    assert manager.check_market_entry(pair, near, far, SignalAction.LONG_SPREAD, 5, specs()).allowed
    size = manager.size_pair(account, pair, specs(), near, far, spread_std=10)
    assert 1 <= size <= pair.volume
    thin = tick("m2609", 3000, 3001, depth=2)
    assert not manager.check_market_entry(
        pair, thin, far, SignalAction.LONG_SPREAD, 5, specs()
    ).allowed


def test_portfolio_risk_computes_correlation_and_group_limit():
    analyzer = PortfolioRiskAnalyzer(
        window=6, min_samples=4, max_correlation=0.8, max_group_open_pairs=1
    )
    for a, b in zip([1, 2, 4, 3, 6, 5], [2, 4, 8, 6, 12, 10], strict=True):
        analyzer.update("p1", a)
        analyzer.update("p2", b)
    assert analyzer.correlation("p1", "p2") > 0.99
    assert not analyzer.allow_open("p2", risk_group="oilseed", open_pairs={"p1": "oilseed"}).allowed


def test_health_monitor_is_fail_closed():
    monitor = HealthMonitor(5)
    assert "connection" in monitor.evaluate(
        connected=False, account_ready=True, position_ready=True, quotes_ready=True, max_quote_age=0
    )
    assert "stale" in monitor.evaluate(
        connected=True, account_ready=True, position_ready=True, quotes_ready=True, max_quote_age=6
    )
    assert (
        monitor.evaluate(
            connected=True,
            account_ready=True,
            position_ready=True,
            quotes_ready=True,
            max_quote_age=1,
        )
        == ""
    )


def test_state_has_checksum_sequence_and_rejects_schema_less_state(tmp_path: Path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.save(RuntimeState(kill_switch=True, last_order_id="o1", last_trade_id="t1"))
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] >= 2 and raw["sequence"] == 1 and raw["checksum"]
    store.save(store.load())
    assert json.loads(path.read_text())["sequence"] == 2
    raw = json.loads(path.read_text())
    raw["state"]["kill_switch"] = False
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        store.load()
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"kill_switch": True, "positions": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="envelope"):
        StateStore(legacy).load()


@pytest.mark.parametrize("evidence_suffix", [".prev", ".lock"])
def test_state_store_missing_current_with_evidence_fails_load_and_save(
    tmp_path: Path,
    evidence_suffix: str,
):
    path = tmp_path / "state.json"
    evidence_path = path.with_name(f"{path.name}{evidence_suffix}")
    evidence_path.write_bytes(b"incident evidence")
    store = StateStore(path)

    with pytest.raises(StateIntegrityError, match="current.*missing"):
        store.load()
    with pytest.raises(StateIntegrityError, match="current.*missing"):
        store.save(RuntimeState())

    assert not path.exists()
    assert evidence_path.read_bytes() == b"incident evidence"


def test_state_store_both_current_and_evidence_absent_is_fresh(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")

    assert store.load() == RuntimeState()
    store.save(RuntimeState(kill_switch=True))

    assert store.load().kill_switch is True


def test_state_store_exposes_verified_record_and_rejects_stale_cas(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    first = store.save(RuntimeState(kill_switch=True))

    assert first.sequence == 1
    assert len(first.checksum) == 64
    assert store.load_required_record() == first
    before = store.path.read_bytes()

    with pytest.raises(StateIntegrityError, match="changed concurrently"):
        store.save(
            RuntimeState(kill_switch=False),
            expected_sequence=first.sequence,
            expected_checksum="0" * 64,
        )

    assert store.path.read_bytes() == before


def test_conservative_sim_consumes_depth_and_cancels_fak_after_latency():
    broker = SimBroker(
        100000,
        {"m2609": specs()["m2609"]},
        conservative=True,
        latency_ticks=1,
        market_impact_ticks=1,
    )
    broker.start()
    broker.publish_tick(tick("m2609", 3000, 3001, depth=1))
    oid = broker.send_order(
        OrderRequest("m2609", "DCE", OrderSide.BUY, Offset.OPEN, 2, 3005, OrderType.FAK)
    )
    assert broker.get_order(oid).active
    broker.publish_tick(tick("m2609", 3000, 3001, 1, depth=1))
    order = broker.get_order(oid)
    assert order.traded == 1 and order.status is OrderStatus.CANCELLED
    assert broker.get_trades()[0].price > 3001


def test_scanner_uses_volume_oi_stationarity_and_edge():
    pair = PairConfig("p", "m2609", "m2701", "DCE", 1, lookback=5)
    ticks = []
    for i, spread in enumerate([10, 11, 10, 9, 10, 15]):
        ticks += [
            tick("m2609", 3000 + spread - 0.5, 3000 + spread + 0.5, i, volume=10000, oi=50000),
            tick("m2701", 2999.5, 3000.5, i, volume=12000, oi=60000),
        ]
    candidate = SpreadScanner().scan_pair(pair, ticks, specs())
    assert candidate and candidate.liquidity_score > 0 and candidate.volume_score > 0
    assert candidate.open_interest_score > 0 and candidate.half_life > 0
    assert isinstance(candidate.net_edge, float) and candidate.net_edge >= 0


def test_calibrator_prefers_stable_region_and_rejects_isolated_peaks():
    rows = [
        {"lookback": 20, "entry_z": 1.8, "score": 1.0, "max_drawdown": 0.05},
        {"lookback": 20, "entry_z": 2.0, "score": 1.05, "max_drawdown": 0.05},
        {"lookback": 20, "entry_z": 2.2, "score": 1.02, "max_drawdown": 0.05},
        {"lookback": 60, "entry_z": 3.5, "score": 2.0, "max_drawdown": 0.02},
    ]
    assert (
        ParameterCalibrator(neighbor_radius=0.25, min_neighbors=2).select_best(rows)["lookback"]
        == 20
    )
    isolated = [
        {"lookback": 10, "entry_z": 1, "score": 2},
        {"lookback": 80, "entry_z": 4, "score": 3},
    ]
    assert ParameterCalibrator(neighbor_radius=0.1, min_neighbors=2).select_best(isolated) is None


def test_walk_forward_and_acceptance_gate_cover_oos_and_cost_stress():
    pair = PairConfig("p", "m2609", "m2701", "DCE", 1, lookback=3, entry_z=0.8, exit_z=0.2)
    ticks = []
    for day in range(1, 9):
        base = datetime(2026, 8, day, 9, tzinfo=timezone.utc)
        for i, spread in enumerate([10, 10, 10, 20, 10]):
            near = Tick(
                "m2609",
                "DCE",
                base + timedelta(minutes=i),
                3000 + spread - 0.5,
                3000 + spread + 0.5,
                3000 + spread,
                20,
                20,
                f"202608{day:02d}",
                volume=10000,
                open_interest=50000,
            )
            far = Tick(
                "m2701",
                "DCE",
                base + timedelta(minutes=i),
                2999.5,
                3000.5,
                3000,
                20,
                20,
                f"202608{day:02d}",
                volume=12000,
                open_interest=60000,
            )
            ticks += [near, far]
    result = WalkForwardRunner(specs(), 500000).run(
        pair,
        ticks,
        ResearchConfig(
            train_days=2,
            validation_days=2,
            oos_days=2,
            step_days=2,
            cost_stress_multipliers=(1.0, 2.0),
        ),
    )
    assert result.folds and set(result.stress_results) == {1.0, 2.0}
    assert result.stress_results[2.0]["total_return"] <= result.stress_results[1.0]["total_return"]
    synthetic = ResearchResult(
        [FoldResult({}, {}, {}, {"total_return": 0.03, "max_drawdown": -0.05})] * 2,
        {1.0: {"total_return": 0.02}, 2.0: {"total_return": 0.01}},
        {},
    )
    assert AcceptanceGate(min_positive_oos_ratio=0.5).evaluate(synthetic).accepted
