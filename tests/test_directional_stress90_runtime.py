from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Barrier, Thread
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

_CHINA = ZoneInfo("Asia/Shanghai")


_ACCOUNT_IDENTITY = "a" * 64


_ACCOUNT_EPOCH = "b" * 64


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


def _bind_seed_state_to_test_account(state_path: Path) -> None:
    from afuture.directional_stress90_state import (
        Stress90PolicyStateStore,
        bind_stress90_account_identity,
    )

    store = Stress90PolicyStateStore(state_path)
    record = store.load_required_record()
    store.save(
        bind_stress90_account_identity(
            record.state,
            _ACCOUNT_IDENTITY,
            account_epoch=_ACCOUNT_EPOCH,
        ),
        expected_sequence=record.sequence,
    )


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
    aggregator.note_raw_market_connection(connected=True, generation=1)
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
                    open_price=100.0,
                ),
                contract,
            )
    aggregator.set_expected_contracts("20260825", catalog)
    aggregator.observe_raw_tick(
        Tick(
            catalog[0].symbol,
            catalog[0].exchange,
            datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA),
            100.5,
            101.5,
            101.0,
            100.0,
            100.0,
            "20260825",
            volume=1.0,
            open_interest=110.0,
            open_price=101.0,
        ),
        catalog[0],
    )
    aggregator.checkpoint()
    return path


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
        account_identity_digest=_ACCOUNT_IDENTITY,
        account_epoch=_ACCOUNT_EPOCH,
        current_lots={},
        margin_fitted_lots={},
        symbol_products={},
    )
    with pytest.raises(Stress90ExecutionIntentIntegrityError, match="decision identity"):
        prepare_stress90_execution_intent(
            store,
            target_trading_day="20260825",
            daily_decision_digest="2" * 64,
            account_identity_digest=_ACCOUNT_IDENTITY,
            account_epoch=_ACCOUNT_EPOCH,
            current_lots={},
            margin_fitted_lots={},
            symbol_products={},
        )
    prepare_stress90_execution_intent(
        store,
        target_trading_day="20260826",
        daily_decision_digest="3" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        account_epoch=_ACCOUNT_EPOCH,
        current_lots={},
        margin_fitted_lots={},
        symbol_products={},
    )
    assert store.previous_path.exists()
    path.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(Stress90ExecutionIntentIntegrityError, match="JSON"):
        Stress90ExecutionIntentStore(path).load_required_record()


def test_same_day_execution_intent_rejects_account_lifecycle_epoch_change(
    tmp_path: Path,
):
    from afuture.directional_stress90_execution import (
        Stress90ExecutionIntentIntegrityError,
        Stress90ExecutionIntentStore,
        prepare_stress90_execution_intent,
    )

    store = Stress90ExecutionIntentStore(tmp_path / "stress90_execution_intent.json")
    prepare_stress90_execution_intent(
        store,
        target_trading_day="20260825",
        daily_decision_digest="1" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        account_epoch=_ACCOUNT_EPOCH,
        current_lots={},
        margin_fitted_lots={},
        symbol_products={},
    )

    with pytest.raises(
        Stress90ExecutionIntentIntegrityError,
        match="stale account lifecycle epoch",
    ):
        prepare_stress90_execution_intent(
            store,
            target_trading_day="20260825",
            daily_decision_digest="1" * 64,
            account_identity_digest=_ACCOUNT_IDENTITY,
            account_epoch="c" * 64,
            current_lots={},
            margin_fitted_lots={},
            symbol_products={},
        )


def test_execution_intent_cross_instance_save_is_one_locked_cas(tmp_path: Path):
    from afuture.directional_stress90_execution import (
        Stress90ExecutionIntentIntegrityError,
        Stress90ExecutionIntentStore,
        prepare_stress90_execution_intent,
    )

    intent = prepare_stress90_execution_intent(
        Stress90ExecutionIntentStore(tmp_path / "template.json"),
        target_trading_day="20260825",
        daily_decision_digest="1" * 64,
        account_identity_digest=_ACCOUNT_IDENTITY,
        account_epoch=_ACCOUNT_EPOCH,
        current_lots={},
        margin_fitted_lots={},
        symbol_products={},
    )
    path = tmp_path / "shared.json"
    stores = (Stress90ExecutionIntentStore(path), Stress90ExecutionIntentStore(path))
    barrier = Barrier(2)
    for store in stores:
        original = store.load_record

        def synchronized_load(original=original):
            record = original()
            barrier.wait(timeout=2.0)
            return record

        store.load_record = synchronized_load

    outcomes: list[str] = []

    def save(store: Stress90ExecutionIntentStore) -> None:
        try:
            store.save(intent, expected_sequence=0)
        except Stress90ExecutionIntentIntegrityError:
            outcomes.append("rejected")
        else:
            outcomes.append("saved")

    workers = [Thread(target=save, args=(store,)) for store in stores]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(3.0)
        assert not worker.is_alive()

    assert sorted(outcomes) == ["rejected", "saved"]
    assert Stress90ExecutionIntentStore(path).load_required_record().sequence == 1


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
            historical_mode=True,
            policy_state_path=tmp_path / "state.json",
            seed_path=tmp_path / "seed.json",
            oi_evidence_path=tmp_path / "oi.json",
        )


def test_runtime_refuses_candidate_when_ctp_rollover_was_not_observed(tmp_path: Path):
    from types import SimpleNamespace

    from afuture.directional import DirectionalConfig
    from afuture.directional_ohlc_cache import DirectionalOHLCCacheStore
    from afuture.directional_stress90_oi_runtime import Stress90OiEvidenceStore
    from afuture.directional_stress90_runtime import (
        Stress90DataUnavailableError,
        Stress90DirectionalPortfolioManager,
    )
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.risk import RiskConfig, RiskManager

    seed_path, state_path = _write_seed_state(tmp_path)
    oi_path = _write_completed_oi(tmp_path)
    oi_store = Stress90OiEvidenceStore(oi_path)
    oi_record = oi_store.load_required_record()
    oi_store.save_state(
        replace(oi_record.state, observed_transitions=()),
        expected_sequence=oi_record.sequence,
    )
    ohlc_path = tmp_path / "directional_ohlc_cache.json"
    index = pd.date_range(end="2026-08-24", periods=170, freq="D")
    close = pd.DataFrame(100.0, index=index, columns=FROZEN_PRODUCTS)
    DirectionalOHLCCacheStore(ohlc_path).save(FROZEN_PRODUCTS, close, close)
    manager = Stress90DirectionalPortfolioManager(
        DirectionalConfig(enabled=True, policy="stress90", products=FROZEN_PRODUCTS),
        SimpleNamespace(),
        RiskManager(RiskConfig(margin_estimate_buffer=1.25)),
        historical_mode=True,
        policy_state_path=state_path,
        seed_path=seed_path,
        oi_evidence_path=oi_path,
        ohlc_cache_path=ohlc_path,
    )
    manager.policy = SimpleNamespace(
        target_weights=lambda _open, _close: dict.fromkeys(FROZEN_PRODUCTS, 0.0)
    )

    with pytest.raises(Stress90DataUnavailableError, match="rollover"):
        manager._prepare_decision_for_current_day("20260825")


def _mechanical_manager(
    tmp_path: Path,
    *,
    target_weight: float,
    positions=None,
    concentration_freeze: bool = False,
    quality_recorder=None,
    max_gross_leverage: float = 2.0,
    real_inputs: bool = False,
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
    _bind_seed_state_to_test_account(state_path)
    oi_path = tmp_path / "oi.json"
    ohlc_path = None
    if real_inputs:
        import numpy as np

        from afuture.directional_ohlc_cache import DirectionalOHLCCacheStore

        oi_path = _write_completed_oi(tmp_path)
        ohlc_path = tmp_path / "directional_ohlc_cache.json"
        index = pd.bdate_range(end="2026-08-24", periods=400)
        step = np.arange(len(index))
        prices = pd.DataFrame(
            {
                product: 100 * np.exp(np.cumsum(0.0015 + 0.002 * np.sin(step * 0.17 + i * 0.6)))
                for i, product in enumerate(FROZEN_PRODUCTS)
            },
            index=index,
        )
        DirectionalOHLCCacheStore(ohlc_path).save(FROZEN_PRODUCTS, prices * 0.999, prices)
    intent_path = tmp_path / "stress90_execution_intent.json"
    now = datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA)
    from afuture.directional_sessions import PRODUCT_SESSION_MANIFEST

    products = ("AU", "B", "BC") if real_inputs else ("A",)
    catalog = [
        ContractInfo(
            f"{product}2612", PRODUCT_SESSION_MANIFEST[product].exchange, product, "2026-12-15"
        )
        for product in products
    ]
    ticks = {
        contract.symbol: Tick(
            contract.symbol,
            contract.exchange,
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
        for contract in catalog
    }
    specs = {
        contract.symbol: ContractSpec(contract.symbol, contract.exchange, 10.0, 1.0, 0.10, 0.10)
        for contract in catalog
    }
    activity = DirectionalActivitySnapshot(
        "20260824",
        {
            contract.symbol: ContractActivity(
                contract.symbol,
                contract.exchange,
                contract.product,
                "20260824",
                20_000.0,
                30_000.0,
                now,
            )
            for contract in catalog
        },
    )

    class Broker:
        metadata_query_blocks = False

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

        def get_account_identity_digest(self):
            return _ACCOUNT_IDENTITY

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
        DirectionalConfig(
            enabled=True,
            policy="stress90",
            products=FROZEN_PRODUCTS,
            max_gross_leverage=max_gross_leverage,
        ),
        broker,
        risk,
        historical_mode=True,
        policy_state_path=state_path,
        seed_path=seed_path,
        oi_evidence_path=oi_path,
        ohlc_cache_path=ohlc_path,
        execution_intent_path=intent_path,
        activity_tracker=SimpleNamespace(completed_snapshot=activity),
        static_specs=specs,
        quality_recorder=quality_recorder,
    )
    weights = {product: 0.0 for product in STRESS90_POLICY.products}
    weights[products[0]] = target_weight
    prepared = SimpleNamespace(
        previous_target_trading_day="20260824",
        target_trading_day="20260825",
        daily_decision_digest="d" * 64,
        base_weights=weights,
        oi_confirmed_weights=weights,
        cost_approved_weights=weights,
        survivor_weights=weights,
        current_hhi=1.0,
        prior_hhi_median=0.5,
        concentration_freeze=concentration_freeze,
        input_days={"completed_close": "20260824", "completed_oi": "20260824"},
    )
    if not real_inputs:
        manager._prepare_decision_for_current_day = lambda current, **kwargs: prepared
    manager._initialized = True
    manager._catalog = catalog
    manager._catalog_by_symbol = {contract.symbol: contract for contract in catalog}
    manager._ticks = ticks
    return manager, broker, intent_path, now


def test_final_opening_batch_rechecks_strict_gross_with_fresh_broker_truth(tmp_path: Path):
    from afuture.models import Offset, OrderRequest, OrderSide, OrderType

    manager, broker, _intent_path, now = _mechanical_manager(
        tmp_path,
        target_weight=1.0,
        max_gross_leverage=1.0,
    )
    request = OrderRequest(
        symbol="A2612",
        exchange="DCE",
        side=OrderSide.BUY,
        offset=Offset.OPEN,
        volume=11,
        price=1000.0,
        order_type=OrderType.FAK,
        reference="directional:A",
    )

    reason = manager._opening_policy_rejection(
        broker.get_account(),
        [],
        [request],
        manager._specs,
        now,
    )

    assert reason == "Stress-90 opening batch would exceed configured gross limit"


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
    assert broker.orders[-1].volume == 2

    broker.positions = [ContractPosition("A2612", "DCE", short_today=1, short_price=1000.0)]
    third = manager.maybe_rebalance(now)
    assert third.action == "open"
    assert broker.orders[-1].volume == 1


def test_runtime_missed_first_window_never_chases_new_entry(tmp_path: Path):
    manager, broker, intent_path, now = _mechanical_manager(tmp_path, target_weight=1.0)

    result = manager.maybe_rebalance(now.replace(hour=22))

    assert result.action == "hold"
    assert "entry window" in result.reason
    assert intent_path.exists()
    assert broker.orders == []
