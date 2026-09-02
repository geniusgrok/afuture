import json
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


@pytest.mark.parametrize("mutation", ["old_schema", "missing_overlay"])
def test_permit_store_rejects_non_current_evidence_without_rewriting(
    tmp_path: Path,
    mutation: str,
) -> None:
    from afuture import stress90_activation_permit as permit_module
    from afuture.stress90_activation_permit import (
        Stress90ActivationPermitIntegrityError,
        Stress90ActivationPermitStore,
    )

    store = Stress90ActivationPermitStore(tmp_path / "stress90_activation_permit.json")
    store.issue(_evidence())
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    if mutation == "old_schema":
        raw["schema_version"] = 5
    else:
        evidence = raw["permit"]["evidence"]
        evidence.pop("risk_overlay_digest")
        raw["permit"]["evidence_digest"] = permit_module._digest(
            {**evidence, "risk_overlay_digest": ""}
        )
    unsigned = {key: value for key, value in raw.items() if key != "checksum"}
    raw["checksum"] = permit_module._digest(unsigned)
    store.path.write_text(json.dumps(raw), encoding="utf-8")
    original = store.path.read_bytes()

    with pytest.raises(Stress90ActivationPermitIntegrityError, match="schema|evidence fields"):
        store.load_required_record()

    assert store.path.read_bytes() == original


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_identity_digest", "0" * 64),
        ("account_snapshot_digest", "1" * 64),
        ("policy_account_epoch", "0" * 64),
        ("account_registry_sequence", 18),
        ("account_registry_checksum", "0" * 64),
        ("account_registry_runtime_identity_digest", "0" * 64),
        ("ctp_trading_day", "20260826"),
        ("policy_definition_digest", "0" * 64),
        ("policy_manifest_digest", "0" * 64),
        ("products_manifest_digest", "0" * 64),
        ("bootstrap_seed_digest", "0" * 64),
        ("generic_state_sequence", 8),
        ("generic_state_checksum", "0" * 64),
        ("policy_state_sequence", 12),
        ("policy_state_checksum", "0" * 64),
        ("policy_last_decision_digest", "0" * 64),
        ("policy_last_completed_target_day", "20260824"),
        ("ohlc_content_digest", "0" * 64),
        ("oi_state_sequence", 14),
        ("oi_state_checksum", "0" * 64),
        ("oi_latest_completed_digest", "0" * 64),
        ("activity_digest", "0" * 64),
        ("catalog_digest", "0" * 64),
        ("local_positions_digest", "0" * 64),
        ("broker_positions_digest", "0" * 64),
        ("reconciliation_digest", "0" * 64),
        ("session_trades_digest", "1" * 64),
        ("active_order_count", 1),
    ],
)
def test_permit_rejects_every_bound_evidence_change(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    from afuture.stress90_activation_permit import Stress90ActivationPermitStore

    store = Stress90ActivationPermitStore(tmp_path / "stress90_activation_permit.json")
    issued = store.issue(_evidence())

    with pytest.raises(RuntimeError, match="evidence mismatch"):
        store.consume(
            replace(_evidence(), **{field: value}),
            expected_sequence=issued.sequence,
        )

    assert store.load_required_record() == issued


def test_one_hundred_consume_attempts_have_exactly_one_success(tmp_path: Path) -> None:
    from afuture.stress90_activation_permit import Stress90ActivationPermitStore

    store = Stress90ActivationPermitStore(tmp_path / "stress90_activation_permit.json")
    store.issue(_evidence())
    successes = 0
    failures = 0
    for _ in range(100):
        record = store.load_required_record()
        try:
            store.consume(_evidence(), expected_sequence=record.sequence)
        except RuntimeError:
            failures += 1
        else:
            successes += 1

    assert (successes, failures) == (1, 99)
    assert store.load_required_record().sequence == 2
    assert store.load_required_record().permit.status == "consumed"


def test_consumed_permit_can_be_reissued_only_as_a_new_identity(tmp_path: Path) -> None:
    from afuture.stress90_activation_permit import Stress90ActivationPermitStore

    store = Stress90ActivationPermitStore(tmp_path / "stress90_activation_permit.json")
    first = store.issue(_evidence())
    consumed = store.consume(_evidence(), expected_sequence=first.sequence)
    second = store.issue(_evidence(), expected_sequence=consumed.sequence)

    assert second.sequence == 3
    assert second.permit.status == "issued"
    assert second.permit.permit_id != first.permit.permit_id


def test_active_issued_permit_cannot_be_silently_replaced(tmp_path: Path) -> None:
    from afuture.stress90_activation_permit import Stress90ActivationPermitStore

    store = Stress90ActivationPermitStore(tmp_path / "stress90_activation_permit.json")
    issued = store.issue(_evidence())

    with pytest.raises(RuntimeError, match="already issued"):
        store.issue(_evidence(), expected_sequence=issued.sequence)

    assert store.load_required_record() == issued


def test_collect_evidence_binds_every_authoritative_runtime_input(tmp_path: Path) -> None:
    from afuture.state import StateStore
    from afuture.stress90_activation_permit import collect_stress90_activation_evidence

    account, seed, generic, policy, ohlc, oi = _write_activation_evidence(tmp_path)
    catalog = [ContractInfo("M2612", "DCE", "M", "2026-12-15", "2026-01-01")]

    evidence = collect_stress90_activation_evidence(
        runtime_dir=tmp_path,
        state_store=StateStore(tmp_path / "state.json"),
        account_identity_digest=account,
        account_snapshot=_verified_account_snapshot(),
        ctp_trading_day="20260825",
        broker_positions=[],
        active_orders=[],
        **_empty_session_evidence(),
        catalog=catalog,
    )

    assert evidence.bootstrap_seed_digest == seed.seed_digest
    assert evidence.account_registry_sequence == 2
    assert len(evidence.account_registry_checksum) == 64
    assert len(evidence.account_registry_runtime_identity_digest) == 64
    assert (evidence.generic_state_sequence, evidence.generic_state_checksum) == (
        generic.sequence,
        generic.checksum,
    )
    assert (evidence.policy_state_sequence, evidence.policy_state_checksum) == (
        policy.sequence,
        policy.checksum,
    )
    assert evidence.policy_last_decision_digest == policy.state.last_decision_digest
    assert evidence.ohlc_content_digest == ohlc.content_digest
    assert (evidence.oi_state_sequence, evidence.oi_state_checksum) == (
        oi.sequence,
        oi.checksum,
    )
    assert evidence.oi_latest_completed_digest == oi.state.completed[-1].evidence_digest
    assert evidence.local_positions_digest == evidence.broker_positions_digest
    assert evidence.active_order_count == 0


def test_collect_evidence_uses_lifecycle_settlement_tolerance(tmp_path: Path) -> None:
    from afuture.state import StateStore
    from afuture.stress90_activation_permit import collect_stress90_activation_evidence

    account, *_ = _write_activation_evidence(tmp_path)
    state_store = StateStore(tmp_path / "state.json")
    record = state_store.load_required_record()
    state_store.save(
        replace(record.state, day_start_equity=500_000.0 + 5e-9),
        expected_sequence=record.sequence,
        expected_checksum=record.checksum,
    )

    evidence = collect_stress90_activation_evidence(
        runtime_dir=tmp_path,
        state_store=state_store,
        account_identity_digest=account,
        account_snapshot=_verified_account_snapshot(),
        ctp_trading_day="20260825",
        broker_positions=[],
        active_orders=[],
        **_empty_session_evidence(),
        catalog=[ContractInfo("M2612", "DCE", "M", "2026-12-15")],
    )

    assert evidence.account_snapshot_digest


def test_collect_evidence_rejects_target_behind_current_ctp_day(tmp_path: Path) -> None:
    from afuture.state import StateStore
    from afuture.stress90_activation_permit import collect_stress90_activation_evidence

    account, *_ = _write_activation_evidence(tmp_path)
    state_store = StateStore(tmp_path / "state.json")
    generic = state_store.load_required_record()
    state_store.save(
        replace(
            generic.state,
            trading_day="20260826",
            last_account_trading_day="20260826",
        ),
        expected_sequence=generic.sequence,
        expected_checksum=generic.checksum,
    )

    with pytest.raises(RuntimeError, match="exact current CTP trading day"):
        collect_stress90_activation_evidence(
            runtime_dir=tmp_path,
            state_store=state_store,
            account_identity_digest=account,
            account_snapshot=replace(
                _verified_account_snapshot(),
                trading_day="20260826",
            ),
            ctp_trading_day="20260826",
            broker_positions=[],
            active_orders=[],
            **_empty_session_evidence(),
            catalog=[ContractInfo("M2612", "DCE", "M", "2026-12-15")],
        )


def test_collect_evidence_binds_verified_account_snapshot(tmp_path: Path) -> None:
    from afuture.state import StateStore
    from afuture.stress90_activation_permit import collect_stress90_activation_evidence

    account_identity, *_ = _write_activation_evidence(tmp_path)
    common = dict(
        runtime_dir=tmp_path,
        state_store=StateStore(tmp_path / "state.json"),
        account_identity_digest=account_identity,
        ctp_trading_day="20260825",
        broker_positions=[],
        active_orders=[],
        **_empty_session_evidence(),
        catalog=[ContractInfo("M2612", "DCE", "M", "2026-12-15")],
    )

    original = collect_stress90_activation_evidence(
        account_snapshot=_verified_account_snapshot(),
        **common,
    )
    changed = collect_stress90_activation_evidence(
        account_snapshot=replace(_verified_account_snapshot(), available=499_999.0),
        **common,
    )

    assert original.account_snapshot_digest != changed.account_snapshot_digest


def test_collect_evidence_rejects_unknown_session_trade(tmp_path: Path) -> None:
    from afuture.models import Offset, OrderSide, Trade
    from afuture.state import StateStore
    from afuture.stress90_activation_permit import collect_stress90_activation_evidence

    account_identity, *_ = _write_activation_evidence(tmp_path)
    trade = Trade(
        "manual-fill",
        "manual-order",
        "M2612",
        "DCE",
        OrderSide.BUY,
        Offset.OPEN,
        1,
        100.0,
        datetime(2026, 8, 25, 1, tzinfo=timezone.utc),
    )

    with pytest.raises(RuntimeError, match="unknown session trade"):
        collect_stress90_activation_evidence(
            runtime_dir=tmp_path,
            state_store=StateStore(tmp_path / "state.json"),
            account_identity_digest=account_identity,
            account_snapshot=_verified_account_snapshot(),
            ctp_trading_day="20260825",
            broker_positions=[],
            active_orders=[],
            session_trades=[trade],
            owns_order=lambda _order_id: False,
            session_activity_proof=_session_activity_proof((trade,)),
            catalog=[ContractInfo("M2612", "DCE", "M", "2026-12-15")],
        )


def test_session_trade_identity_is_exchange_scoped_and_order_independent(
    tmp_path: Path,
) -> None:
    from afuture.models import Offset, OrderSide, Trade
    from afuture.state import StateStore
    from afuture.stress90_activation_permit import collect_stress90_activation_evidence

    account_identity, *_ = _write_activation_evidence(tmp_path)
    timestamp = datetime(2026, 8, 25, 1, tzinfo=timezone.utc)
    trades = [
        Trade(
            "shared-id",
            "order-dce",
            "M2612",
            "DCE",
            OrderSide.BUY,
            Offset.OPEN,
            1,
            100.0,
            timestamp,
        ),
        Trade(
            "shared-id",
            "order-czce",
            "TA701",
            "CZCE",
            OrderSide.SELL,
            Offset.CLOSE,
            1,
            5000.0,
            timestamp,
        ),
    ]
    common = dict(
        runtime_dir=tmp_path,
        state_store=StateStore(tmp_path / "state.json"),
        account_identity_digest=account_identity,
        account_snapshot=_verified_account_snapshot(),
        ctp_trading_day="20260825",
        broker_positions=[],
        active_orders=[],
        owns_order=lambda order_id: order_id in {"order-dce", "order-czce"},
        session_activity_proof=_session_activity_proof(trades),
        catalog=[ContractInfo("M2612", "DCE", "M", "2026-12-15")],
    )

    first = collect_stress90_activation_evidence(session_trades=trades, **common)
    reordered = collect_stress90_activation_evidence(
        session_trades=list(reversed(trades)),
        **common,
    )

    assert first.session_trades_digest == reordered.session_trades_digest


def test_collect_evidence_catalog_digest_is_order_independent_and_metadata_bound(
    tmp_path: Path,
) -> None:
    from afuture.state import StateStore
    from afuture.stress90_activation_permit import collect_stress90_activation_evidence

    account, *_ = _write_activation_evidence(tmp_path)
    catalog = [
        ContractInfo("M2612", "DCE", "M", "2026-12-15", "2026-01-01"),
        ContractInfo("A2612", "DCE", "A", "2026-12-15", "2026-01-01"),
    ]
    common = dict(
        runtime_dir=tmp_path,
        state_store=StateStore(tmp_path / "state.json"),
        account_identity_digest=account,
        account_snapshot=_verified_account_snapshot(),
        ctp_trading_day="20260825",
        broker_positions=[],
        active_orders=[],
        **_empty_session_evidence(),
    )

    first = collect_stress90_activation_evidence(catalog=catalog, **common)
    reordered = collect_stress90_activation_evidence(catalog=list(reversed(catalog)), **common)
    changed = collect_stress90_activation_evidence(
        catalog=[replace(catalog[0], expiry="2027-01-15"), catalog[1]],
        **common,
    )

    assert reordered.catalog_digest == first.catalog_digest
    assert changed.catalog_digest != first.catalog_digest


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


def test_consumption_precedes_single_authoritative_halted_to_running_save(
    tmp_path: Path,
) -> None:
    from afuture.state import StateStore
    from afuture.stress90_activation_permit import (
        Stress90ActivationPermitStore,
        activate_stress90_from_permit,
        collect_stress90_activation_evidence,
    )

    account, *_ = _write_activation_evidence(tmp_path)
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
        catalog=[ContractInfo("M2612", "DCE", "M", "2026-12-15")],
    )
    permit_store = Stress90ActivationPermitStore(tmp_path / "stress90_activation_permit.json")
    issued = permit_store.issue(evidence)

    running = activate_stress90_from_permit(
        state_store=state_store,
        permit_store=permit_store,
        evidence=evidence,
        expected_permit_sequence=issued.sequence,
    )

    assert running.sequence == evidence.generic_state_sequence + 1
    assert running.state.runtime_mode == RuntimeMode.RUNNING.value
    assert running.state.kill_switch is False
    assert running.state.metadata_verified is False
    assert permit_store.load_required_record().permit.status == "consumed"
    with pytest.raises(RuntimeError, match="not issued"):
        activate_stress90_from_permit(
            state_store=state_store,
            permit_store=permit_store,
            evidence=evidence,
            expected_permit_sequence=permit_store.load_required_record().sequence,
        )


def test_crash_after_consume_leaves_halted_consumed_and_requires_new_doctor(
    tmp_path: Path,
) -> None:
    from afuture.state import StateStore
    from afuture.stress90_activation_permit import (
        Stress90ActivationPermitStore,
        activate_stress90_from_permit,
        collect_stress90_activation_evidence,
    )

    account, *_ = _write_activation_evidence(tmp_path)
    real_store = StateStore(tmp_path / "state.json")
    evidence = collect_stress90_activation_evidence(
        runtime_dir=tmp_path,
        state_store=real_store,
        account_identity_digest=account,
        account_snapshot=_verified_account_snapshot(),
        ctp_trading_day="20260825",
        broker_positions=[],
        active_orders=[],
        **_empty_session_evidence(),
        catalog=[ContractInfo("M2612", "DCE", "M", "2026-12-15")],
    )
    permit_store = Stress90ActivationPermitStore(tmp_path / "stress90_activation_permit.json")
    issued = permit_store.issue(evidence)

    class FailingStateStore(StateStore):
        def save(self, state, *, expected_sequence=None, expected_checksum=None):
            raise OSError("injected state save crash")

    with pytest.raises(OSError, match="state save crash"):
        activate_stress90_from_permit(
            state_store=FailingStateStore(real_store.path),
            permit_store=permit_store,
            evidence=evidence,
            expected_permit_sequence=issued.sequence,
        )

    assert permit_store.load_required_record().permit.status == "consumed"
    persisted = real_store.load_required_record()
    assert persisted.state.runtime_mode == RuntimeMode.HALTED.value
    assert persisted.state.kill_switch is True
    with pytest.raises(RuntimeError, match="not issued"):
        activate_stress90_from_permit(
            state_store=real_store,
            permit_store=permit_store,
            evidence=evidence,
            expected_permit_sequence=permit_store.load_required_record().sequence,
        )


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


def _ready_doctor_report():
    from afuture.operations import OperationalReport
    from afuture.stress90_activation_permit import STRESS90_DOCTOR_INTERNAL_P0_CHECKS

    report = OperationalReport()
    for name in sorted(STRESS90_DOCTOR_INTERNAL_P0_CHECKS):
        report.add(name, True, "verified")
    report.add(
        "stress90_runtime_permission",
        False,
        "HALTED and awaiting a one-shot technical permit",
    )
    report.facts["broker"] = {"orders_sent": 0}
    report.facts["stress90"] = {
        "stress90_ready": True,
        "external_blocker_reasons": ["multi_day_shadow", "test_counter"],
        "capital_activation_eligible": False,
        "live_eligibility": False,
    }
    return report


def test_doctor_issues_technical_permit_while_runtime_is_halted_and_external_gates_remain(
    tmp_path: Path,
) -> None:
    from afuture.state import StateStore
    from afuture.stress90_activation_permit import (
        STRESS90_ACTIVATION_PERMIT_ACK,
        issue_stress90_doctor_permit,
    )

    account, *_ = _write_activation_evidence(tmp_path)
    report = _ready_doctor_report()
    issued = issue_stress90_doctor_permit(
        report=report,
        runtime_dir=tmp_path,
        state_store=StateStore(tmp_path / "state.json"),
        account_identity_digest=account,
        account_snapshot=_verified_account_snapshot(),
        ctp_trading_day="20260825",
        broker_positions=[],
        active_orders=[],
        **_empty_session_evidence(),
        catalog=[ContractInfo("M2612", "DCE", "M", "2026-12-15")],
        strong_confirmation=STRESS90_ACTIVATION_PERMIT_ACK,
    )

    assert issued.permit.status == "issued"
    assert issued.permit.external_activation_gates_completed is False
    assert report.facts["stress90"]["external_blocker_reasons"] == [
        "multi_day_shadow",
        "test_counter",
    ]
    assert report.facts["stress90"]["capital_activation_eligible"] is False
    assert report.facts["stress90"]["live_eligibility"] is False
    assert report.facts["broker"]["orders_sent"] == 0


@pytest.mark.parametrize("failure", ["strong_ack", "internal_p0", "orders_sent"])
def test_doctor_permit_fails_closed_without_each_technical_authority(
    tmp_path: Path,
    failure: str,
) -> None:
    from afuture.state import StateStore
    from afuture.stress90_activation_permit import (
        STRESS90_ACTIVATION_PERMIT_ACK,
        STRESS90_DOCTOR_INTERNAL_P0_CHECKS,
        issue_stress90_doctor_permit,
    )

    account, *_ = _write_activation_evidence(tmp_path)
    report = _ready_doctor_report()
    if failure == "internal_p0":
        failed_name = sorted(STRESS90_DOCTOR_INTERNAL_P0_CHECKS)[0]
        report.checks = [
            replace(check, passed=False) if check.name == failed_name else check
            for check in report.checks
        ]
    if failure == "orders_sent":
        report.facts["broker"]["orders_sent"] = 1

    with pytest.raises(RuntimeError):
        issue_stress90_doctor_permit(
            report=report,
            runtime_dir=tmp_path,
            state_store=StateStore(tmp_path / "state.json"),
            account_identity_digest=account,
            account_snapshot=_verified_account_snapshot(),
            ctp_trading_day="20260825",
            broker_positions=[],
            active_orders=[],
            **_empty_session_evidence(),
            catalog=[ContractInfo("M2612", "DCE", "M", "2026-12-15")],
            strong_confirmation=(
                "wrong" if failure == "strong_ack" else STRESS90_ACTIVATION_PERMIT_ACK
            ),
        )

    assert not (tmp_path / "stress90_activation_permit.json").exists()
