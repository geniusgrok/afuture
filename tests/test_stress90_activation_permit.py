from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from afuture.models import ContractInfo, ContractPosition, RuntimeMode


def _verified_account_snapshot():
    from afuture.models import AccountSnapshot

    return AccountSnapshot(
        500_000.0,
        500_000.0,
        500_000.0,
        0.0,
        0.0,
        0.0,
        "20260825",
        previous_settlement_equity=500_000.0,
        settlement_verified=True,
        settlement_id=42,
    )


def _session_activity_proof(local_trades=()):
    from afuture.broker.ctp_session_query import build_ctp_session_activity_evidence
    from afuture.stress90_session_authority import Stress90SessionOwnershipProof

    evidence = build_ctp_session_activity_evidence(
        account_identity_digest="1" * 64,
        trading_day="20260825",
        order_request_id=11,
        trade_request_id=12,
        orders=(),
        trades=(),
        critical_generation=0,
    )
    return Stress90SessionOwnershipProof(evidence, "0" * 64, tuple(local_trades))


def _empty_session_evidence():
    return {
        "session_trades": [],
        "owns_order": lambda _order_id: False,
        "session_activity_proof": _session_activity_proof(),
    }


def _evidence():
    from afuture.stress90_activation_permit import Stress90ActivationEvidence

    return Stress90ActivationEvidence(
        account_identity_digest="1" * 64,
        account_snapshot_digest="0" * 64,
        policy_account_epoch="f" * 64,
        account_registry_sequence=17,
        account_registry_checksum="1" * 64,
        account_registry_runtime_identity_digest="2" * 64,
        ctp_trading_day="20260825",
        policy_id="directional.stress90",
        policy_definition_digest="2" * 64,
        policy_manifest_digest="3" * 64,
        products_manifest_digest="4" * 64,
        bootstrap_seed_digest="5" * 64,
        generic_state_sequence=7,
        generic_state_checksum="6" * 64,
        policy_state_sequence=11,
        policy_state_checksum="7" * 64,
        policy_last_decision_digest="8" * 64,
        policy_last_completed_target_day="20260825",
        ohlc_content_digest="9" * 64,
        oi_state_sequence=13,
        oi_state_checksum="a" * 64,
        oi_latest_completed_digest="b" * 64,
        activity_digest="c" * 64,
        catalog_digest="d" * 64,
        local_positions_digest="e" * 64,
        broker_positions_digest="e" * 64,
        reconciliation_digest="f" * 64,
        session_trades_digest="0" * 64,
        session_activity_source_account_identity_digest="1" * 64,
        session_activity_orders_digest="1" * 64,
        session_activity_trades_digest="1" * 64,
        session_activity_ownership_digest="2" * 64,
        active_order_count=0,
        risk_overlay_digest="3" * 64,
    )


def _write_activation_evidence(runtime_dir: Path):
    from afuture.directional_activity import (
        ContractActivity,
        DirectionalActivitySnapshot,
        DirectionalActivityStore,
    )
    from afuture.directional_ohlc_cache import DirectionalOHLCCacheStore
    from afuture.directional_policy_activation import (
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
    )
    from afuture.directional_stress90_oi_runtime import (
        Stress90OiEvidenceState,
        Stress90OiEvidenceStore,
        build_fixed_historical_60m_evidence,
    )
    from afuture.directional_stress90_policy import STRESS90_POLICY, Stress90CandidateState
    from afuture.directional_stress90_state import (
        Stress90BootstrapSeed,
        Stress90DecisionInputs,
        Stress90PolicyState,
        Stress90PolicyStateStore,
        Stress90SeedStore,
        bind_stress90_account_identity,
        prepare_stress90_decision,
    )
    from afuture.state import RuntimeState, StateStore

    account_identity = "1" * 64
    candidate = replace(
        Stress90CandidateState.initial(STRESS90_POLICY),
        last_completed_target_day="20260824",
        last_decision_digest="a" * 64,
    )
    seed = Stress90BootstrapSeed.from_candidate_state(
        candidate,
        bootstrap_source_manifest={"fixed": "2" * 64},
        bootstrap_through_day="20260824",
        last_completed_input_day="20260821",
    )
    Stress90SeedStore(runtime_dir / "stress90_bootstrap_seed.json").save_new(seed)
    policy_state = bind_stress90_account_identity(
        Stress90PolicyState.from_seed(seed),
        account_identity,
        account_epoch="3" * 64,
    )
    policy_record = Stress90PolicyStateStore(runtime_dir / "stress90_policy_state.json").save(
        policy_state
    )
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,
        AccountRuntimeRegistry,
    )

    registry = AccountRuntimeRegistry(runtime_dir / ".account-runtime-registry.json")
    registry.initialize(strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION)
    registry.bind_new(
        account_identity,
        runtime_dir,
        "3" * 64,
        "4" * 64,
    )
    policy_store = Stress90PolicyStateStore(runtime_dir / "stress90_policy_state.json")
    prepare_stress90_decision(
        policy_store,
        Stress90DecisionInputs(
            previous_target_trading_day="20260824",
            target_trading_day="20260825",
            base_weights={product: 0.0 for product in STRESS90_POLICY.products},
            completed_close_history={
                product: (100.0,) * 21 for product in STRESS90_POLICY.products
            },
            completed_oi_flow={product: 0 for product in STRESS90_POLICY.oi_products},
            completed_close_day="20260824",
            completed_oi_day="20260824",
        ),
    )
    policy_record = policy_store.load_required_record()
    halted = activate_stress90_policy(
        RuntimeState(
            kill_switch=True,
            kill_reason="commissioning",
            reconciled=True,
            metadata_verified=False,
            runtime_mode=RuntimeMode.HALTED.value,
            trading_day="20260825",
            day_start_equity=500_000.0,
            equity_high_watermark=500_000.0,
            last_account_equity=500_000.0,
            last_account_trading_day="20260825",
            last_account_cash_flow_verified=True,
            last_account_settlement_id=42,
        ),
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        bootstrap_seed_digest=seed.seed_digest,
        account_identity_digest=account_identity,
        risk_overlay_digest="5" * 64,
        operator_reason="technical activation fixture",
        strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
    )
    generic_record = StateStore(runtime_dir / "state.json").save(halted)
    products = STRESS90_POLICY.products
    index = pd.DatetimeIndex(["2026-08-24"])
    values = pd.DataFrame([[100.0] * len(products)], index=index, columns=products)
    ohlc = DirectionalOHLCCacheStore(runtime_dir / "directional_ohlc_cache.json").save(
        products,
        values,
        values,
    )
    DirectionalActivityStore(runtime_dir / "directional_activity.json").save(
        DirectionalActivitySnapshot(
            "20260824",
            {
                "M2612": ContractActivity(
                    "M2612",
                    "DCE",
                    "M",
                    "20260824",
                    10_000.0,
                    20_000.0,
                    datetime(2026, 8, 24, 14, 59, tzinfo=timezone.utc),
                )
            },
        )
    )
    bars: list[dict[str, object]] = []
    for product in STRESS90_POLICY.oi_products:
        bars.extend(
            [
                {
                    "datetime": datetime(2026, 8, 24, 9, tzinfo=timezone.utc),
                    "product": product,
                    "symbol": f"{product}2612",
                    "open": 100.0,
                    "close": 100.5,
                    "volume": 10.0,
                    "hold": 100.0,
                },
                {
                    "datetime": datetime(2026, 8, 24, 14, tzinfo=timezone.utc),
                    "product": product,
                    "symbol": f"{product}2612",
                    "open": 100.5,
                    "close": 101.0,
                    "volume": 20.0,
                    "hold": 110.0,
                },
            ]
        )
    completed = build_fixed_historical_60m_evidence("20260824", bars)
    oi_record = Stress90OiEvidenceStore(runtime_dir / "stress90_oi_evidence.json").save_state(
        Stress90OiEvidenceState(completed=(completed,))
    )
    return account_identity, seed, generic_record, policy_record, ohlc, oi_record


def test_permit_store_is_checksummed_sequenced_and_one_shot(tmp_path: Path) -> None:
    from afuture.stress90_activation_permit import Stress90ActivationPermitStore

    store = Stress90ActivationPermitStore(tmp_path / "stress90_activation_permit.json")
    issued = store.issue(_evidence())

    assert issued.sequence == 1
    assert issued.permit.status == "issued"
    assert len(issued.checksum) == 64
    assert store.load_required_record() == issued

    consumed = store.consume(_evidence(), expected_sequence=issued.sequence)

    assert consumed.sequence == 2
    assert consumed.permit.status == "consumed"
    assert consumed.permit.permit_id == issued.permit.permit_id
    assert store.load_previous_record() == issued
    with pytest.raises(RuntimeError, match="not issued"):
        Stress90ActivationPermitStore(store.path).consume(
            _evidence(),
            expected_sequence=consumed.sequence,
        )


def test_collect_evidence_rejects_orders_position_drift_and_daily_circuit(
    tmp_path: Path,
) -> None:
    from afuture.state import StateStore
    from afuture.stress90_activation_permit import collect_stress90_activation_evidence

    account, *_ = _write_activation_evidence(tmp_path)
    store = StateStore(tmp_path / "state.json")
    common = dict(
        runtime_dir=tmp_path,
        state_store=store,
        account_identity_digest=account,
        account_snapshot=_verified_account_snapshot(),
        ctp_trading_day="20260825",
        broker_positions=[],
        active_orders=[],
        **_empty_session_evidence(),
        catalog=[ContractInfo("M2612", "DCE", "M", "2026-12-15")],
    )

    with pytest.raises(RuntimeError, match="active orders"):
        collect_stress90_activation_evidence(**{**common, "active_orders": [object()]})
    with pytest.raises(RuntimeError, match="reconciliation"):
        collect_stress90_activation_evidence(
            **{
                **common,
                "broker_positions": [
                    ContractPosition("M2612", "DCE", long_today=1, long_price=100.0)
                ],
            }
        )

    record = store.load_required_record()
    store.save(
        replace(record.state, directional_daily_circuit_day="20260825"),
        expected_sequence=record.sequence,
        expected_checksum=record.checksum,
    )
    with pytest.raises(RuntimeError, match="daily circuit"):
        collect_stress90_activation_evidence(**common)


def test_runtime_authority_requires_exact_held_account_lease(tmp_path: Path) -> None:
    from afuture.runtime_lease import AccountExclusiveRuntimeLease
    from afuture.state import StateStore
    from afuture.stress90_activation_permit import (
        Stress90ActivationPermitStore,
        Stress90TechnicalActivationAuthority,
        collect_stress90_activation_evidence,
    )

    account, *_ = _write_activation_evidence(tmp_path)
    catalog = [ContractInfo("M2612", "DCE", "M", "2026-12-15")]
    state_store = StateStore(tmp_path / "state.json")
    evidence = collect_stress90_activation_evidence(
        runtime_dir=tmp_path,
        state_store=state_store,
        account_identity_digest=account,
        account_snapshot=_verified_account_snapshot(),
        ctp_trading_day="20260825",
        broker_positions=[],
        active_orders=[],
        **_empty_session_evidence(),
        catalog=catalog,
    )
    permit_store = Stress90ActivationPermitStore(tmp_path / "stress90_activation_permit.json")
    permit_store.issue(evidence)

    class Broker:
        def get_account_identity_digest(self):
            return account

        def get_account(self):
            return _verified_account_snapshot()

        def get_trading_day(self):
            return "20260825"

        def get_positions(self):
            return []

        def get_active_orders(self):
            return []

        def get_session_trades(self):
            return []

        def owns_order(self, _order_id):
            return False

        def get_contract_catalog(self):
            return catalog

        def require_session_activity_evidence_current(self, evidence):
            assert evidence == _session_activity_proof().evidence

    authority = Stress90TechnicalActivationAuthority(tmp_path)
    lease = AccountExclusiveRuntimeLease(tmp_path, account, role="live")
    with pytest.raises(RuntimeError, match="account lease"):
        authority.activate(state_store=state_store, broker=Broker(), lease=lease)
    assert permit_store.load_required_record().permit.status == "issued"

    lease.acquire()
    try:
        authority.startup_session_authority.last_proof = _session_activity_proof()
        running = authority.activate(state_store=state_store, broker=Broker(), lease=lease)
    finally:
        lease.release()

    assert running.state.runtime_mode == RuntimeMode.RUNNING.value
    assert permit_store.load_required_record().permit.status == "consumed"


def test_consumed_activation_crash_reenters_real_startup_without_faking_clean_shutdown(
    tmp_path: Path,
) -> None:
    from afuture.process_run import ProcessRunStore, apply_unclean_restart_fence
    from afuture.runtime_lease import AccountExclusiveRuntimeLease
    from afuture.state import StateStore
    from afuture.stress90_activation_permit import (
        Stress90ActivationPermitStore,
        Stress90TechnicalActivationAuthority,
        collect_stress90_activation_evidence,
    )

    account, *_ = _write_activation_evidence(tmp_path)
    state_store = StateStore(tmp_path / "state.json")
    original = state_store.load_required_record()
    catalog = [ContractInfo("M2612", "DCE", "M", "2026-12-15")]
    evidence = collect_stress90_activation_evidence(
        runtime_dir=tmp_path,
        state_store=state_store,
        account_identity_digest=account,
        account_snapshot=_verified_account_snapshot(),
        ctp_trading_day="20260825",
        broker_positions=[],
        active_orders=[],
        catalog=catalog,
        **_empty_session_evidence(),
    )
    permits = Stress90ActivationPermitStore(tmp_path / "stress90_activation_permit.json")
    issued = permits.issue(evidence)
    processes = ProcessRunStore(tmp_path / "process_run.json")
    identities = dict(
        deployment_digest="a" * 64,
        runtime_identity_digest="b" * 64,
        account_identity_digest=account,
    )
    old = processes.begin(**identities, start_state_checksum=original.checksum)
    processes.mark_phase(old.process_uuid, "broker_constructed")
    consumed = permits.consume(evidence, expected_sequence=issued.sequence)

    class Broker:
        snapshot = _verified_account_snapshot()

        def get_account_identity_digest(self):
            return account

        def get_account(self):
            return self.snapshot

        def get_trading_day(self):
            return "20260825"

        def get_positions(self):
            return []

        def get_active_orders(self):
            return []

        def get_session_trades(self):
            return []

        def owns_order(self, _order_id):
            return False

        def get_contract_catalog(self):
            return catalog

        def require_session_activity_evidence_current(self, query):
            assert query == _session_activity_proof().evidence

    broker = Broker()
    with AccountExclusiveRuntimeLease(tmp_path, account, role="live") as lease:
        recovery = apply_unclean_restart_fence(
            process_store=processes,
            state_store=state_store,
            runtime_dir=tmp_path,
            lease=lease,
            **identities,
        )
        assert not recovery.blocked and recovery.resume_activation
        assert not processes.load_required().clean_shutdown
        assert state_store.load_required_record() == original
        assert permits.load_required_record() == consumed
        new = processes.begin(
            **identities,
            start_state_checksum=original.checksum,
            _allow_unclean_parent=recovery.resume_activation,
        )
        assert new.process_uuid != old.process_uuid
        assert not new.clean_shutdown
        authority = Stress90TechnicalActivationAuthority(tmp_path)
        authority.startup_session_authority.last_proof = _session_activity_proof()
        # The local proof admits startup only. Fresh changed Broker facts still
        # prevent activation; restoring the query facts completes the one CAS.
        broker.snapshot = replace(broker.snapshot, equity=broker.snapshot.equity - 1)
        with pytest.raises(RuntimeError, match="evidence mismatch"):
            authority.activate(state_store=state_store, broker=broker, lease=lease)
        assert state_store.load_required_record() == original
        broker.snapshot = _verified_account_snapshot()
        running = authority.activate(state_store=state_store, broker=broker, lease=lease)
        assert running.sequence == original.sequence + 1
        assert running.state.runtime_mode == "RUNNING"
        assert permits.load_required_record() == consumed
