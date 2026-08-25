from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

_CHINA = ZoneInfo("Asia/Shanghai")


def _write_seed_state(tmp_path: Path, *, last_target_day: str = "20260824"):
    from afuture.directional_stress90_policy import STRESS90_POLICY, Stress90CandidateState
    from afuture.directional_stress90_state import (
        Stress90BootstrapSeed,
        Stress90PolicyState,
        Stress90PolicyStateStore,
        Stress90SeedStore,
    )

    candidate = replace(
        Stress90CandidateState.initial(STRESS90_POLICY),
        last_completed_target_day=last_target_day,
        last_decision_digest="a" * 64,
    )
    seed = Stress90BootstrapSeed.from_candidate_state(
        candidate,
        bootstrap_source_manifest={"fixed": "1" * 64},
        bootstrap_through_day=last_target_day,
        last_completed_input_day="20260823",
    )
    seed_path = tmp_path / "stress90_bootstrap_seed.json"
    state_path = tmp_path / "stress90_policy_state.json"
    Stress90SeedStore(seed_path).save_new(seed)
    Stress90PolicyStateStore(state_path).save(Stress90PolicyState.from_seed(seed))
    return seed_path, state_path


def _write_completed_oi(tmp_path: Path):
    from afuture.directional_sessions import PRODUCT_SESSION_MANIFEST
    from afuture.directional_stress90_oi_runtime import (
        Stress90OiEvidenceAggregator,
        Stress90OiEvidenceStore,
    )
    from afuture.models import ContractInfo, Tick

    products = ("A", "C", "EG", "I", "M", "P", "PP", "TA", "Y")
    catalog = [
        ContractInfo(
            f"{product}2612",
            PRODUCT_SESSION_MANIFEST[product].exchange,
            product,
            "2026-12-15",
        )
        for product in products
    ]
    path = tmp_path / "stress90_oi_evidence.json"
    aggregator = Stress90OiEvidenceAggregator(store=Stress90OiEvidenceStore(path))
    aggregator.set_expected_contracts("20260824", catalog)
    for contract in catalog:
        for timestamp, price, volume, hold in (
            (datetime(2026, 8, 23, 21, 0, tzinfo=_CHINA), 100.0, 10.0, 100.0),
            (datetime(2026, 8, 24, 14, 59, tzinfo=_CHINA), 101.0, 20.0, 110.0),
        ):
            aggregator.observe_raw_tick(
                Tick(
                    contract.symbol,
                    contract.exchange,
                    timestamp,
                    price - 0.5,
                    price + 0.5,
                    price,
                    100.0,
                    100.0,
                    "20260824",
                    volume=volume,
                    open_interest=hold,
                ),
                contract,
            )
    aggregator.set_expected_contracts("20260825", catalog)
    aggregator.checkpoint()
    return path


def test_execution_intent_is_persisted_once_and_reused_after_broker_positions_change(
    tmp_path: Path,
):
    from afuture.directional_stress90_execution import (
        Stress90ExecutionIntentStore,
        prepare_stress90_execution_intent,
    )

    store = Stress90ExecutionIntentStore(tmp_path / "stress90_execution_intent.json")
    first = prepare_stress90_execution_intent(
        store,
        target_trading_day="20260825",
        daily_decision_digest="1" * 64,
        current_lots={"A2612": 2, "AG2612": 2},
        margin_fitted_lots={"A2612": -10, "AG2702": 3},
        symbol_products={"A2612": "A", "AG2612": "AG", "AG2702": "AG"},
    )
    restarted = prepare_stress90_execution_intent(
        Stress90ExecutionIntentStore(store.path),
        target_trading_day="20260825",
        daily_decision_digest="1" * 64,
        current_lots={},
        margin_fitted_lots={"A2612": -10, "AG2702": 3},
        symbol_products={"A2612": "A", "AG2702": "AG"},
    )

    assert first.authorized_transition_products == ("A", "AG")
    assert restarted == first
    assert store.load_required_record().sequence == 1


def test_execution_intent_rejects_identity_change_and_never_uses_corrupt_prev(tmp_path: Path):
    from afuture.directional_stress90_execution import (
        Stress90ExecutionIntentIntegrityError,
        Stress90ExecutionIntentStore,
        prepare_stress90_execution_intent,
    )

    path = tmp_path / "stress90_execution_intent.json"
    store = Stress90ExecutionIntentStore(path)
    prepare_stress90_execution_intent(
        store,
        target_trading_day="20260825",
        daily_decision_digest="1" * 64,
        current_lots={},
        margin_fitted_lots={},
        symbol_products={},
    )
    with pytest.raises(Stress90ExecutionIntentIntegrityError, match="decision identity"):
        prepare_stress90_execution_intent(
            store,
            target_trading_day="20260825",
            daily_decision_digest="2" * 64,
            current_lots={},
            margin_fitted_lots={},
            symbol_products={},
        )
    prepare_stress90_execution_intent(
        store,
        target_trading_day="20260826",
        daily_decision_digest="3" * 64,
        current_lots={},
        margin_fitted_lots={},
        symbol_products={},
    )
    assert store.previous_path.exists()
    path.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(Stress90ExecutionIntentIntegrityError, match="JSON"):
        Stress90ExecutionIntentStore(path).load_required_record()


def test_execution_intent_rejects_duplicate_json_keys(tmp_path: Path):
    from afuture.directional_stress90_execution import (
        Stress90ExecutionIntentIntegrityError,
        Stress90ExecutionIntentStore,
    )

    path = tmp_path / "intent.json"
    path.write_text('{"kind":"one","kind":"two"}', encoding="utf-8")

    with pytest.raises(Stress90ExecutionIntentIntegrityError, match="duplicate"):
        Stress90ExecutionIntentStore(path).load_required_record()


def test_candidate_target_sequence_uses_verified_sessions_not_business_day_guessing():
    from afuture.directional_stress90_runtime import stress90_target_transitions

    index = pd.DatetimeIndex(["2026-08-20", "2026-08-21", "2026-08-24"])
    assert stress90_target_transitions(
        last_completed_target_day="20260821",
        current_ctp_trading_day="20260825",
        completed_close_index=index,
    ) == (("20260821", "20260824"), ("20260824", "20260825"))

    with pytest.raises(RuntimeError, match="does not cover prior target"):
        stress90_target_transitions(
            last_completed_target_day="20260821",
            current_ctp_trading_day="20260825",
            completed_close_index=pd.DatetimeIndex(["2026-08-24"]),
        )
    with pytest.raises(RuntimeError, match="moved backward"):
        stress90_target_transitions(
            last_completed_target_day="20260825",
            current_ctp_trading_day="20260824",
            completed_close_index=index,
        )


def test_manager_validates_required_policy_state_before_market_subscriptions(tmp_path: Path):
    from afuture.directional import DirectionalConfig
    from afuture.directional_stress90_runtime import Stress90DirectionalPortfolioManager
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.risk import RiskConfig, RiskManager

    class Broker:
        def __init__(self):
            self.calls: list[str] = []

        def get_contract_catalog(self):
            self.calls.append("catalog")
            return []

        def get_trading_day(self):
            self.calls.append("trading_day")
            return "20260825"

        def subscribe(self, symbol, exchange):
            self.calls.append(f"subscribe:{symbol}:{exchange}")

    broker = Broker()
    manager = Stress90DirectionalPortfolioManager(
        DirectionalConfig(enabled=True, policy="stress90", products=FROZEN_PRODUCTS),
        broker,
        RiskManager(RiskConfig(margin_estimate_buffer=1.25)),
        policy_state_path=tmp_path / "stress90_policy_state.json",
        seed_path=tmp_path / "stress90_bootstrap_seed.json",
        oi_evidence_path=tmp_path / "stress90_oi_evidence.json",
    )

    with pytest.raises(RuntimeError, match="required Stress-90 policy state"):
        manager.bootstrap(datetime(2026, 8, 24, 20, 30, tzinfo=_CHINA))
    assert broker.calls == []


def test_manager_rejects_any_risk_envelope_looser_than_stress90(tmp_path: Path):
    from types import SimpleNamespace

    from afuture.directional import DirectionalConfig
    from afuture.directional_stress90_runtime import Stress90DirectionalPortfolioManager
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.risk import RiskConfig, RiskManager

    with pytest.raises(ValueError, match="hard risk envelope"):
        Stress90DirectionalPortfolioManager(
            DirectionalConfig(enabled=True, policy="stress90", products=FROZEN_PRODUCTS),
            SimpleNamespace(),
            RiskManager(
                RiskConfig(
                    max_margin_ratio=0.36,
                    margin_estimate_buffer=1.25,
                )
            ),
            policy_state_path=tmp_path / "state.json",
            seed_path=tmp_path / "seed.json",
            oi_evidence_path=tmp_path / "oi.json",
        )


def test_base_weight_transition_uses_exact_authoritative_target_index(tmp_path: Path):
    from types import SimpleNamespace

    from afuture.directional import DirectionalConfig
    from afuture.directional_stress90_runtime import Stress90DirectionalPortfolioManager
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.risk import RiskConfig, RiskManager

    class SpyPolicy:
        def __init__(self):
            self.open_index = None
            self.close_index = None

        def target_weights(self, open_prices, close):
            self.open_index = open_prices.index.copy()
            self.close_index = close.index.copy()
            return {product: 0.0 for product in FROZEN_PRODUCTS}

    manager = Stress90DirectionalPortfolioManager(
        DirectionalConfig(enabled=True, policy="stress90", products=FROZEN_PRODUCTS),
        SimpleNamespace(),
        RiskManager(RiskConfig(margin_estimate_buffer=1.25)),
        policy_state_path=tmp_path / "state.json",
        seed_path=tmp_path / "seed.json",
        oi_evidence_path=tmp_path / "oi.json",
    )
    spy = SpyPolicy()
    manager.policy = spy
    index = pd.date_range("2026-01-01", periods=170, freq="D")
    close = pd.DataFrame(100.0, index=index, columns=FROZEN_PRODUCTS)
    entry = SimpleNamespace(open=close - 1.0, close=close)

    weights = manager._base_weights_for_transition(
        entry,
        completed_input_day=index[-2].strftime("%Y%m%d"),
        target_trading_day="20260930",
    )

    assert set(weights) == set(FROZEN_PRODUCTS)
    assert spy.open_index[-1] == pd.Timestamp("2026-09-30")
    assert spy.close_index[-1] == pd.Timestamp("2026-09-30")
    assert spy.close_index[-2] == index[-2]


def test_runtime_prepares_candidate_once_from_cache_and_completed_oi(tmp_path: Path):
    from types import SimpleNamespace

    from afuture.directional import DirectionalConfig
    from afuture.directional_ohlc_cache import DirectionalOHLCCacheStore
    from afuture.directional_stress90_runtime import Stress90DirectionalPortfolioManager
    from afuture.directional_stress90_state import Stress90PolicyStateStore
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.risk import RiskConfig, RiskManager

    seed_path, state_path = _write_seed_state(tmp_path)
    oi_path = _write_completed_oi(tmp_path)
    ohlc_path = tmp_path / "directional_ohlc_cache.json"
    index = pd.date_range(end="2026-08-24", periods=170, freq="D")
    values = {
        product: [100.0 * (1.001**row) for row in range(len(index))] for product in FROZEN_PRODUCTS
    }
    close = pd.DataFrame(values, index=index)
    DirectionalOHLCCacheStore(ohlc_path).save(FROZEN_PRODUCTS, close / 1.001, close)
    manager = Stress90DirectionalPortfolioManager(
        DirectionalConfig(enabled=True, policy="stress90", products=FROZEN_PRODUCTS),
        SimpleNamespace(),
        RiskManager(RiskConfig(margin_estimate_buffer=1.25)),
        policy_state_path=state_path,
        seed_path=seed_path,
        oi_evidence_path=oi_path,
        ohlc_cache_path=ohlc_path,
    )

    class Base:
        def target_weights(self, open_prices, close_prices):
            del open_prices, close_prices
            return {
                product: (1.0 if product in {"A", "AG"} else 0.0) for product in FROZEN_PRODUCTS
            }

    manager.policy = Base()
    first = manager._prepare_decision_for_current_day("20260825")
    second = manager._prepare_decision_for_current_day("20260825")
    record = Stress90PolicyStateStore(state_path).load_required_record()

    assert first == second
    assert first.target_trading_day == "20260825"
    assert first.input_days == {"completed_close": "20260824", "completed_oi": "20260824"}
    assert record.sequence == 2
    assert record.state.completed_concentrations == (0.5,)


def _mechanical_manager(
    tmp_path: Path,
    *,
    target_weight: float,
    positions=None,
    concentration_freeze: bool = False,
):
    from types import SimpleNamespace

    from afuture.directional import DirectionalConfig
    from afuture.directional_activity import ContractActivity, DirectionalActivitySnapshot
    from afuture.directional_stress90_policy import STRESS90_POLICY
    from afuture.directional_stress90_runtime import Stress90DirectionalPortfolioManager
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.models import AccountSnapshot, ContractInfo, ContractSpec, Tick
    from afuture.risk import RiskConfig, RiskManager

    seed_path, state_path = _write_seed_state(tmp_path)
    intent_path = tmp_path / "stress90_execution_intent.json"
    now = datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA)
    contract = ContractInfo("A2612", "DCE", "A", "2026-12-15")
    tick = Tick(
        "A2612",
        "DCE",
        now,
        999.0,
        1001.0,
        1000.0,
        1000.0,
        1000.0,
        "20260825",
        volume=20_000.0,
        open_interest=30_000.0,
    )
    spec = ContractSpec("A2612", "DCE", 10.0, 1.0, 0.10, 0.10)
    activity = DirectionalActivitySnapshot(
        "20260824",
        {
            "A2612": ContractActivity(
                "A2612",
                "DCE",
                "A",
                "20260824",
                20_000.0,
                30_000.0,
                now,
            )
        },
    )

    class Broker:
        def __init__(self):
            self.positions = list(positions or [])
            self.orders = []
            self.active_orders = []
            self.intent_path = intent_path

        def is_ready(self):
            return True

        def get_active_orders(self):
            return list(self.active_orders)

        def get_positions(self):
            return list(self.positions)

        def get_trading_day(self):
            return "20260825"

        def get_account(self):
            return AccountSnapshot(
                100_000.0,
                100_000.0,
                100_000.0,
                0.0,
                0.0,
                0.0,
                "20260825",
            )

        def send_order(self, request):
            assert self.intent_path.exists()
            self.orders.append(request)
            return f"order-{len(self.orders)}"

    broker = Broker()
    risk = RiskManager(
        RiskConfig(
            max_margin_ratio=0.35,
            min_available_ratio=0.25,
            max_daily_loss_ratio=0.05,
            max_total_drawdown_ratio=0.30,
            max_contract_volume=35,
            margin_estimate_buffer=1.25,
        )
    )
    manager = Stress90DirectionalPortfolioManager(
        DirectionalConfig(enabled=True, policy="stress90", products=FROZEN_PRODUCTS),
        broker,
        risk,
        policy_state_path=state_path,
        seed_path=seed_path,
        oi_evidence_path=tmp_path / "oi.json",
        execution_intent_path=intent_path,
        activity_tracker=SimpleNamespace(completed_snapshot=activity),
        static_specs={"A2612": spec},
    )
    weights = {product: 0.0 for product in STRESS90_POLICY.products}
    weights["A"] = target_weight
    prepared = SimpleNamespace(
        previous_target_trading_day="20260824",
        target_trading_day="20260825",
        daily_decision_digest="d" * 64,
        survivor_weights=weights,
        concentration_freeze=concentration_freeze,
        input_days={"completed_close": "20260824", "completed_oi": "20260824"},
    )
    manager._prepare_decision_for_current_day = lambda current, **kwargs: prepared
    manager._initialized = True
    manager._catalog = [contract]
    manager._catalog_by_symbol = {contract.symbol: contract}
    manager._ticks = {tick.symbol: tick}
    return manager, broker, intent_path, now


def test_runtime_persists_execution_intent_before_order_and_reuses_it_after_partial_fill(
    tmp_path: Path,
):
    from afuture.directional_stress90_execution import Stress90ExecutionIntentStore
    from afuture.models import ContractPosition

    manager, broker, intent_path, now = _mechanical_manager(tmp_path, target_weight=1.0)

    first = manager.maybe_rebalance(now)
    intent = Stress90ExecutionIntentStore(intent_path).load_required_record()
    assert first.action == "open"
    assert intent.sequence == 1
    assert intent.intent.initial_margin_fitted_lots == {"A2612": 10}

    broker.positions = [ContractPosition("A2612", "DCE", long_today=3, long_price=1000.0)]
    second = manager.maybe_rebalance(now)

    assert second.action == "open"
    assert broker.orders[-1].volume == 7
    assert Stress90ExecutionIntentStore(intent_path).load_required_record().sequence == 1


def test_runtime_intent_persistence_failure_sends_no_orders(tmp_path: Path):
    manager, broker, _intent_path, now = _mechanical_manager(tmp_path, target_weight=1.0)

    def fail_save(_intent):
        raise RuntimeError("intent fsync failed")

    manager.execution_intent_store.save = fail_save
    with pytest.raises(RuntimeError, match="intent fsync failed"):
        manager.maybe_rebalance(now)
    assert broker.orders == []


def test_runtime_reversal_is_reduction_first_then_opens_from_persisted_intent(
    tmp_path: Path,
):
    from afuture.models import ContractPosition, Offset, OrderSide

    manager, broker, _intent_path, now = _mechanical_manager(
        tmp_path,
        target_weight=-1.0,
        positions=[ContractPosition("A2612", "DCE", long_today=2, long_price=1000.0)],
        concentration_freeze=True,
    )

    first = manager.maybe_rebalance(now)
    assert first.action == "reduce"
    assert all(order.offset is not Offset.OPEN for order in broker.orders)

    broker.positions = []
    second = manager.maybe_rebalance(now)
    assert second.action == "open"
    assert broker.orders[-1].offset is Offset.OPEN
    assert broker.orders[-1].side is OrderSide.SELL


def test_runtime_missed_first_window_never_chases_new_entry(tmp_path: Path):
    manager, broker, intent_path, now = _mechanical_manager(tmp_path, target_weight=1.0)

    result = manager.maybe_rebalance(now.replace(hour=22))

    assert result.action == "hold"
    assert "entry window" in result.reason
    assert intent_path.exists()
    assert broker.orders == []
