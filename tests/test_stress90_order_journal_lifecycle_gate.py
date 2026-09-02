from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from afuture.risk import RiskConfig

_OPERATION_ID = "f" * 64


@pytest.fixture(autouse=True)
def _use_isolated_current_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "afuture.account_runtime_registry.PRODUCTION_ACCOUNT_RUNTIME_REGISTRY_PATH",
        tmp_path / ".account-runtime-registry.json",
    )


def _empty_complete_session_evidence(*, account: str = "a" * 64, day: str = "20260825"):
    from afuture.broker.ctp_session_query import build_ctp_session_activity_evidence

    return build_ctp_session_activity_evidence(
        account_identity_digest=account,
        trading_day=day,
        order_request_id=11,
        trade_request_id=12,
        orders=(),
        trades=(),
        critical_generation=0,
    )


def test_lifecycle_session_gate_requires_complete_request_bound_evidence(
    tmp_path: Path,
) -> None:
    from afuture.cli import _require_lifecycle_session_trade_ownership

    class MemoryOnlyBroker:
        def get_account_identity_digest(self) -> str:
            return "a" * 64

        def get_trading_day(self) -> str:
            return "20260825"

        def get_session_trades(self) -> list[object]:
            return []

        def owns_order(self, _order_id: str) -> bool:
            return False

    with pytest.raises(RuntimeError, match="complete session activity"):
        _require_lifecycle_session_trade_ownership(
            MemoryOnlyBroker(),
            runtime_dir=tmp_path,
            timeout_seconds=0.1,
        )


def test_lifecycle_session_gate_persists_verified_complete_evidence(
    tmp_path: Path,
) -> None:
    from afuture.broker.ctp_session_query import CtpSessionActivityEvidenceStore
    from afuture.cli import _require_lifecycle_session_trade_ownership

    evidence = _empty_complete_session_evidence()

    class CompleteBroker:
        def refresh_session_activity(self, *, timeout_seconds: float):
            assert timeout_seconds == 0.25
            return evidence

        def require_session_activity_evidence_current(self, current) -> None:
            assert current == evidence

        def get_session_activity_account_identity_digest(self) -> str:
            return "a" * 64

        def get_account_identity_digest(self) -> str:
            return "a" * 64

        def get_trading_day(self) -> str:
            return "20260825"

        def get_session_trades(self) -> list[object]:
            return []

        def owns_order(self, _order_id: str) -> bool:
            return False

    result = _require_lifecycle_session_trade_ownership(
        CompleteBroker(),
        runtime_dir=tmp_path,
        timeout_seconds=0.25,
    )

    assert result.evidence == evidence
    assert result.ownership_digest
    assert result.local_session_trades == ()
    saved = CtpSessionActivityEvidenceStore(
        tmp_path / "stress90_ctp_session_evidence.json"
    ).load_required_record()
    assert saved.evidence == evidence


def test_lifecycle_session_gate_recovers_durable_crash_window_before_ownership(
    tmp_path: Path,
) -> None:
    from afuture.cli import _require_lifecycle_session_trade_ownership

    evidence = _empty_complete_session_evidence()
    calls: list[str] = []

    class RecoveringBroker:
        def checkpoint_order_submission_journal(self) -> None:
            calls.append("checkpoint")

        def refresh_session_activity(self, *, timeout_seconds: float):
            assert timeout_seconds == 0.25
            calls.append("query")
            return evidence

        def recover_stress90_session_activity(self, current):
            assert current == evidence
            calls.append("recover")
            return ()

        def require_session_activity_evidence_current(self, current) -> None:
            assert current == evidence

        def get_session_activity_account_identity_digest(self) -> str:
            return "a" * 64

        def get_account_identity_digest(self) -> str:
            return "a" * 64

        def get_trading_day(self) -> str:
            return "20260825"

        def get_session_trades(self) -> list[object]:
            calls.append("local-trades")
            return []

        def owns_order(self, _order_id: str) -> bool:
            return False

    _require_lifecycle_session_trade_ownership(
        RecoveringBroker(),
        runtime_dir=tmp_path,
        timeout_seconds=0.25,
    )

    assert calls == ["checkpoint", "query", "recover", "local-trades"]


def test_lifecycle_session_gate_rejects_owned_orders_left_active_after_recovery(
    tmp_path: Path,
) -> None:
    from afuture.cli import _require_lifecycle_session_trade_ownership

    evidence = _empty_complete_session_evidence()

    class ActiveRecoveryBroker:
        def refresh_session_activity(self, *, timeout_seconds: float):
            del timeout_seconds
            return evidence

        def recover_stress90_session_activity(self, current):
            assert current == evidence
            return ("CTP.1_2_3",)

        def require_session_activity_evidence_current(self, current) -> None:
            assert current == evidence

        def get_session_activity_account_identity_digest(self) -> str:
            return "a" * 64

        def get_account_identity_digest(self) -> str:
            return "a" * 64

        def get_trading_day(self) -> str:
            return "20260825"

        def get_session_trades(self) -> list[object]:
            raise AssertionError("active crash-window order must stop ownership proof")

        def owns_order(self, _order_id: str) -> bool:
            return False

    with pytest.raises(RuntimeError, match="remain active"):
        _require_lifecycle_session_trade_ownership(
            ActiveRecoveryBroker(),
            runtime_dir=tmp_path,
            timeout_seconds=0.25,
        )


def test_lifecycle_order_journal_is_configured_with_frozen_policy_identity(
    tmp_path: Path,
) -> None:
    from afuture.cli import _configure_stress90_lifecycle_order_journal
    from afuture.directional_stress90_policy import STRESS90_POLICY

    calls: list[tuple[Path, dict[str, str]]] = []

    class ConfigurableBroker:
        def configure_order_submission_journal(self, path, **identity) -> None:
            calls.append((Path(path), identity))

    _configure_stress90_lifecycle_order_journal(ConfigurableBroker(), tmp_path)

    assert calls == [
        (
            tmp_path / "stress90_ctp_orders.json",
            {
                "policy_id": STRESS90_POLICY.policy_id,
                "policy_definition_digest": STRESS90_POLICY.policy_definition_digest,
                "products_manifest_digest": STRESS90_POLICY.products_manifest_digest,
            },
        )
    ]


def test_lifecycle_mechanical_snapshot_is_recaptured_after_complete_session_query(
    tmp_path: Path,
) -> None:
    from afuture.cli import _require_lifecycle_mechanical_snapshot
    from afuture.models import AccountSnapshot, ContractPosition

    evidence = _empty_complete_session_evidence()
    before = ContractPosition("A2612", "DCE")
    after = ContractPosition("A2612", "DCE", long_today=1, long_price=100.0)

    class RacingBroker:
        def __init__(self) -> None:
            self.query_completed = False
            self.current_checks = 0

        def refresh_session_activity(self, *, timeout_seconds: float):
            assert timeout_seconds == 0.25
            self.query_completed = True
            return evidence

        def require_session_activity_evidence_current(self, current) -> None:
            assert current == evidence
            self.current_checks += 1

        def get_session_activity_account_identity_digest(self) -> str:
            return "a" * 64

        def get_account_identity_digest(self) -> str:
            return "a" * 64

        def get_trading_day(self) -> str:
            return "20260825"

        def get_account(self):
            assert self.query_completed
            return AccountSnapshot(100_000, 100_000, 50_000, 50_000, 0, 0, "20260825")

        def get_positions(self):
            return [after if self.query_completed else before]

        def get_active_orders(self):
            assert self.query_completed
            return [SimpleNamespace(order_id="CTP.active")]

        def get_contract_catalog(self):
            assert self.query_completed
            return []

        def get_session_trades(self) -> list[object]:
            return []

        def owns_order(self, _order_id: str) -> bool:
            return False

    broker = RacingBroker()

    snapshot = _require_lifecycle_mechanical_snapshot(
        broker,
        runtime_dir=tmp_path,
        timeout_seconds=0.25,
    )

    assert snapshot.positions == (after,)
    assert snapshot.active_orders[0].order_id == "CTP.active"
    assert broker.current_checks == 2


@pytest.mark.parametrize(
    ("account", "day", "match"),
    [
        ("b" * 64, "20260825", "account identity"),
        ("a" * 64, "20260824", "trading day"),
    ],
)
def test_lifecycle_session_gate_rejects_query_identity_drift(
    tmp_path: Path,
    account: str,
    day: str,
    match: str,
) -> None:
    from afuture.cli import _require_lifecycle_session_trade_ownership

    class DriftBroker:
        def refresh_session_activity(self, *, timeout_seconds: float):
            del timeout_seconds
            return _empty_complete_session_evidence(account=account, day=day)

        def require_session_activity_evidence_current(self, _current) -> None:
            return None

        def get_session_activity_account_identity_digest(self) -> str:
            return "a" * 64

        def get_account_identity_digest(self) -> str:
            return "a" * 64

        def get_trading_day(self) -> str:
            return "20260825"

        def get_session_trades(self) -> list[object]:
            return []

        def owns_order(self, _order_id: str) -> bool:
            return False

    with pytest.raises(RuntimeError, match=match):
        _require_lifecycle_session_trade_ownership(
            DriftBroker(),
            runtime_dir=tmp_path,
            timeout_seconds=0.1,
        )


def test_lifecycle_full_audit_accepts_an_empty_order_journal(tmp_path: Path) -> None:
    from afuture.cli import _require_stress90_order_journal_full_audit

    _require_stress90_order_journal_full_audit(tmp_path)


def test_lifecycle_full_audit_fails_closed_on_corrupt_current_journal(tmp_path: Path) -> None:
    from afuture.broker.ctp_order_journal import CtpOrderJournalIntegrityError
    from afuture.cli import _require_stress90_order_journal_full_audit

    (tmp_path / "stress90_ctp_orders.json").write_text("{}", encoding="utf-8")

    with pytest.raises(CtpOrderJournalIntegrityError):
        _require_stress90_order_journal_full_audit(tmp_path)


def test_lifecycle_full_audit_rejects_unresolved_durable_order(tmp_path: Path) -> None:
    from afuture.broker.ctp_order_journal import (
        CtpOrderJournalIntegrityError,
        CtpOrderSubmissionEntry,
        CtpOrderSubmissionJournal,
    )
    from afuture.cli import _require_stress90_order_journal_full_audit
    from afuture.models import Offset, OrderRequest, OrderSide, OrderType

    request = OrderRequest(
        "A2701",
        "DCE",
        OrderSide.BUY,
        Offset.OPEN,
        1,
        1_001.0,
        OrderType.FAK,
        "directional:stress90:" + "d" * 12 + ":A",
    )
    CtpOrderSubmissionJournal(tmp_path / "stress90_ctp_orders.json").prepare(
        CtpOrderSubmissionEntry(
            sequence=1,
            account_identity_digest="1" * 64,
            policy_id="directional.stress90",
            policy_definition_digest="2" * 64,
            products_manifest_digest="3" * 64,
            target_trading_day="20260825",
            daily_decision_digest="4" * 64,
            execution_intent_digest="5" * 64,
            transition={"freeze_authorized_lots": {}, "transitions": []},
            front_id=7,
            session_id=11,
            order_ref=101,
            order_id="CTP.7_11_101",
            request=request,
            status="prepared",
        )
    )

    with pytest.raises(CtpOrderJournalIntegrityError, match="unresolved"):
        _require_stress90_order_journal_full_audit(tmp_path)


@pytest.mark.parametrize(
    ("operation", "environment_name", "confirmation_name", "confirmation_value"),
    [
        (
            "activation",
            "AFUTURE_STRESS90_ACTIVATION_ACK",
            "confirm_activation",
            "I_CONFIRM_STRESS90_POLICY_ACTIVATION",
        ),
        (
            "account_rebase",
            "AFUTURE_STRESS90_REBASE_ACK",
            "confirm_rebase",
            "RESET_STRESS90_ACCOUNT_PATH",
        ),
        (
            "migration",
            "AFUTURE_DIRECTIONAL_POLICY_MIGRATION_ACK",
            "confirm_migration",
            "I_CONFIRM_DIRECTIONAL_POLICY_MIGRATION",
        ),
    ],
)
def test_each_lifecycle_command_runs_full_order_journal_audit_under_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    environment_name: str,
    confirmation_name: str,
    confirmation_value: str,
) -> None:
    from afuture.cli import (
        _run_directional_policy_migrate,
        _run_stress90_account_rebase,
        _run_stress90_activate,
    )
    from afuture.directional import DirectionalConfig
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS

    class AuditBarrier(RuntimeError):
        pass

    class FakeBroker:
        def __init__(self, credentials) -> None:
            self.credentials = credentials

        def get_account_identity_digest(self) -> str:
            return "a" * 64

        def stop(self) -> None:
            return None

    audit_calls: list[Path] = []

    def fail_after_audit(runtime_dir: Path) -> None:
        audit_calls.append(runtime_dir)
        raise AuditBarrier("audit barrier reached")

    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", FakeBroker)
    monkeypatch.setattr(
        "afuture.cli._require_stress90_order_journal_full_audit",
        fail_after_audit,
    )
    monkeypatch.setenv(environment_name, confirmation_value)

    migrating = operation == "migration"
    config = SimpleNamespace(
        mode="live",
        ctp=SimpleNamespace(
            environment="test",
            account_id="account-a",
            currency_id="CNY",
        ),
        risk=RiskConfig(margin_estimate_buffer=1.25),
        contracts={},
        directional=DirectionalConfig(
            enabled=True,
            policy="execution_aligned" if migrating else "stress90",
            products=FROZEN_PRODUCTS,
            account_exclusive=True,
        ),
        state_path=str(tmp_path / "state.json"),
        account_registry_path=str(tmp_path / ".account-runtime-registry.json"),
        journal_path=str(tmp_path / "audit.jsonl"),
    )
    args = SimpleNamespace(
        to="execution_aligned",
        confirm_live=False,
        confirm_activation=False,
        confirm_rebase=False,
        confirm_migration=False,
        startup_timeout=0.1,
        snapshot_wait=0.1,
        runtime_dir="",
        operator_reason="audit gate regression",
        operation_id="f" * 64,
        shadow_account=False,
    )
    setattr(args, confirmation_name, True)
    runner = {
        "activation": _run_stress90_activate,
        "account_rebase": _run_stress90_account_rebase,
        "migration": _run_directional_policy_migrate,
    }[operation]
    if operation == "account_rebase":
        from afuture.directional_policy_activation import (
            STRESS90_ACTIVATION_CONFIRMATION,
            activate_stress90_policy,
        )
        from afuture.directional_stress90_policy import (
            STRESS90_POLICY,
            Stress90CandidateState,
        )
        from afuture.directional_stress90_state import (
            Stress90BootstrapSeed,
            Stress90PolicyState,
            Stress90PolicyStateStore,
            Stress90SeedStore,
        )
        from afuture.models import RuntimeMode
        from afuture.state import RuntimeState, StateStore

        candidate = replace(
            Stress90CandidateState.initial(STRESS90_POLICY),
            last_completed_target_day="20260824",
            last_decision_digest="a" * 64,
        )
        seed = Stress90BootstrapSeed.from_candidate_state(
            candidate,
            bootstrap_source_manifest={"fixed": "1" * 64},
            bootstrap_through_day="20260824",
            last_completed_input_day="20260821",
        )
        Stress90SeedStore(tmp_path / "stress90_bootstrap_seed.json").save_new(seed)
        Stress90PolicyStateStore(tmp_path / "stress90_policy_state.json").save(
            Stress90PolicyState.from_seed(seed)
        )
        StateStore(tmp_path / "state.json").save(
            activate_stress90_policy(
                RuntimeState(
                    kill_switch=True,
                    reconciled=True,
                    runtime_mode=RuntimeMode.HALTED.value,
                ),
                broker_flat=True,
                local_flat=True,
                no_active_orders=True,
                reconciled=True,
                bootstrap_seed_digest=seed.seed_digest,
                account_identity_digest="a" * 64,
                risk_overlay_digest="e" * 64,
                operator_reason="commissioned",
                strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
            )
        )

    with pytest.raises(AuditBarrier, match="audit barrier reached"):
        runner(config, args)

    assert audit_calls == [tmp_path]


def test_order_journal_rollover_parser_requires_explicit_operation_identity() -> None:
    from afuture.cli import build_parser

    args = build_parser().parse_args(
        [
            "stress90-order-journal-rollover",
            "--config",
            "live.toml",
            "--confirm-rollover",
            "--operator-reason",
            "capacity rollover",
            "--operation-id",
            _OPERATION_ID,
        ]
    )

    assert args.command == "stress90-order-journal-rollover"
    assert args.operation_id == _OPERATION_ID
    assert args.confirm_rollover is True


def test_order_journal_rollover_rejects_current_day_or_nonempty_session_evidence() -> None:
    from afuture.cli import _require_stress90_order_journal_rollover_cutoff

    empty = SimpleNamespace(orders=(), trades=())
    with pytest.raises(RuntimeError, match="strictly prior"):
        _require_stress90_order_journal_rollover_cutoff(
            current_trading_day="20260825",
            session_evidence=empty,
            entries=(SimpleNamespace(target_trading_day="20260825"),),
        )
    with pytest.raises(RuntimeError, match="empty current CTP session"):
        _require_stress90_order_journal_rollover_cutoff(
            current_trading_day="20260825",
            session_evidence=SimpleNamespace(orders=(object(),), trades=()),
            entries=(SimpleNamespace(target_trading_day="20260824"),),
        )

    _require_stress90_order_journal_rollover_cutoff(
        current_trading_day="20260825",
        session_evidence=empty,
        entries=(SimpleNamespace(target_trading_day="20260824"),),
    )


def test_order_journal_rollover_rejects_pending_lifecycle_before_broker_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.cli import _run_stress90_order_journal_rollover
    from afuture.directional import DirectionalConfig
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS

    class PendingBarrier(RuntimeError):
        pass

    class FakeBroker:
        started = False

        def __init__(self, credentials) -> None:
            self.credentials = credentials

        def get_account_identity_digest(self) -> str:
            return "b" * 64

        def start(self) -> None:
            type(self).started = True

        def stop(self) -> None:
            return None

    def reject_pending(runtime_dir: Path) -> None:
        assert runtime_dir == tmp_path
        raise PendingBarrier("prepared lifecycle blocks rollover")

    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", FakeBroker)
    monkeypatch.setattr(
        "afuture.stress90_lifecycle_transaction.require_no_pending_stress90_lifecycle_transaction",
        reject_pending,
    )
    monkeypatch.setenv(
        "AFUTURE_STRESS90_ORDER_EPOCH_ACK",
        "I_CONFIRM_STRESS90_CTP_ORDER_JOURNAL_EPOCH_ROLLOVER",
    )
    config = SimpleNamespace(
        mode="live",
        ctp=SimpleNamespace(environment="test"),
        risk=RiskConfig(margin_estimate_buffer=1.25),
        contracts={},
        directional=DirectionalConfig(
            enabled=True,
            policy="stress90",
            products=FROZEN_PRODUCTS,
            account_exclusive=True,
        ),
        state_path=str(tmp_path / "state.json"),
        account_registry_path=str(tmp_path / ".account-runtime-registry.json"),
        journal_path=str(tmp_path / "audit.jsonl"),
    )
    args = SimpleNamespace(
        confirm_live=False,
        confirm_rollover=True,
        operator_reason="must not poison lifecycle",
        operation_id=_OPERATION_ID,
        runtime_dir="",
        startup_timeout=0.1,
        snapshot_wait=0.1,
    )

    with pytest.raises(PendingBarrier, match="blocks rollover"):
        _run_stress90_order_journal_rollover(config, args)
    assert FakeBroker.started is False


def test_order_journal_rollover_is_zero_order_and_keeps_runtime_halted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from afuture.cli import _run_stress90_order_journal_rollover
    from afuture.directional import DirectionalConfig
    from afuture.directional_policy_activation import (
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
    )
    from afuture.directional_stress90_policy import STRESS90_POLICY, Stress90CandidateState
    from afuture.directional_stress90_state import (
        Stress90BootstrapSeed,
        Stress90PolicyState,
        Stress90PolicyStateStore,
        Stress90SeedStore,
        bind_stress90_account_identity,
    )
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.models import AccountSnapshot, RuntimeMode
    from afuture.state import RuntimeState, StateStore

    candidate = replace(
        Stress90CandidateState.initial(STRESS90_POLICY),
        last_completed_target_day="20260824",
        last_decision_digest="a" * 64,
    )
    seed = Stress90BootstrapSeed.from_candidate_state(
        candidate,
        bootstrap_source_manifest={"fixed": "1" * 64},
        bootstrap_through_day="20260824",
        last_completed_input_day="20260821",
    )
    Stress90SeedStore(tmp_path / "stress90_bootstrap_seed.json").save_new(seed)
    account_epoch = "d" * 64
    policy = bind_stress90_account_identity(
        Stress90PolicyState.from_seed(seed),
        "b" * 64,
        account_epoch=account_epoch,
    )
    Stress90PolicyStateStore(tmp_path / "stress90_policy_state.json").save(policy)
    halted = RuntimeState(
        kill_switch=True,
        kill_reason="capacity rollover",
        reconciled=True,
        runtime_mode=RuntimeMode.HALTED.value,
        last_order_id="CTP.OLD-ORDER",
        last_trade_id="CTP.OLD-FILL",
        recent_trade_ids=["20260824:DCE:CTP.OLD-FILL"],
    )
    StateStore(tmp_path / "state.json").save(
        activate_stress90_policy(
            halted,
            broker_flat=True,
            local_flat=True,
            no_active_orders=True,
            reconciled=True,
            bootstrap_seed_digest=seed.seed_digest,
            account_identity_digest="b" * 64,
            risk_overlay_digest="e" * 64,
            operator_reason="commissioned",
            strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
        )
    )
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,
        AccountRuntimeRegistry,
    )
    from afuture.trading_day_evidence import TradingDayEvidenceStore

    registry = AccountRuntimeRegistry(tmp_path / ".account-runtime-registry.json")
    registry.initialize(strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION)
    registry_operation = "e" * 64
    registry.bind_new(
        "b" * 64,
        tmp_path,
        account_epoch,
        registry_operation,
    )
    TradingDayEvidenceStore(tmp_path / "ctp_trading_day_evidence.json").bind_for_lifecycle(
        transaction=SimpleNamespace(
            operation="activation",
            status="committed",
            transaction_id="c" * 64,
            operation_nonce=registry_operation,
            source_account_identity_digest="",
            source_account_epoch="",
            account_identity_digest="b" * 64,
            trading_day="20260825",
            policy_target=SimpleNamespace(
                live_account_identity_digest="b" * 64,
                live_account_epoch=account_epoch,
            ),
        ),
        runtime_dir=tmp_path,
        binding_evidence=registry.require_binding_evidence(
            "b" * 64,
            tmp_path,
            account_epoch,
        ),
    )

    class FakeBroker:
        sent = 0

        def __init__(self, credentials) -> None:
            self.credentials = credentials

        def get_account_identity_digest(self) -> str:
            return "b" * 64

        def configure_order_submission_journal(self, path, **identity) -> None:
            assert Path(path) == tmp_path / "stress90_ctp_orders.json"
            assert identity["policy_id"] == "directional.stress90"

        def set_raw_tick_observer(self, _observer) -> None:
            return None

        def seed_trade_identities(self, identities) -> None:
            assert list(identities) == ["20260824:DCE:CTP.OLD-FILL"]

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def is_ready(self) -> bool:
            return True

        def snapshot_marker(self) -> tuple[int, int]:
            return (0, 0)

        def snapshot_ready(self, marker) -> bool:
            return marker == (0, 0)

        def get_account(self) -> AccountSnapshot:
            return AccountSnapshot(
                500_000,
                500_000,
                500_000,
                0,
                0,
                0,
                "20260825",
                previous_settlement_equity=500_000,
                settlement_verified=True,
                settlement_id=42,
            )

        def get_positions(self) -> list[object]:
            return []

        def get_active_orders(self) -> list[object]:
            return []

        def get_session_trades(self) -> list[object]:
            return []

        def refresh_session_activity(self, *, timeout_seconds: float):
            assert timeout_seconds == 0.1
            return _empty_complete_session_evidence(
                account="b" * 64,
                day="20260825",
            )

        def require_session_activity_evidence_current(self, _current) -> None:
            return None

        def owns_order(self, _order_id: str) -> bool:
            return False

        def get_trading_day(self) -> str:
            return "20260825"

        def send_order(self, request) -> None:
            del request
            type(self).sent += 1
            raise AssertionError("order journal rollover must never send an order")

    sealed: list[dict[str, object]] = []

    def seal_epoch(self, **kwargs):
        del self
        sealed.append(kwargs)
        return SimpleNamespace(
            sequence=1,
            checksum="c" * 64,
            sealed_epochs=(SimpleNamespace(),),
        )

    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", FakeBroker)
    monkeypatch.setattr(
        "afuture.broker.ctp_order_journal.CtpOrderSubmissionJournal.seal_epoch",
        seal_epoch,
    )
    monkeypatch.setenv(
        "AFUTURE_STRESS90_ORDER_EPOCH_ACK",
        "I_CONFIRM_STRESS90_CTP_ORDER_JOURNAL_EPOCH_ROLLOVER",
    )
    config = SimpleNamespace(
        mode="live",
        ctp=SimpleNamespace(environment="test"),
        risk=RiskConfig(margin_estimate_buffer=1.25),
        contracts={},
        directional=DirectionalConfig(
            enabled=True,
            policy="stress90",
            products=FROZEN_PRODUCTS,
            account_exclusive=True,
        ),
        state_path=str(tmp_path / "state.json"),
        account_registry_path=str(tmp_path / ".account-runtime-registry.json"),
        journal_path=str(tmp_path / "audit.jsonl"),
    )
    args = SimpleNamespace(
        confirm_live=False,
        confirm_rollover=True,
        operator_reason="capacity rollover",
        operation_id=_OPERATION_ID,
        runtime_dir="",
        startup_timeout=0.1,
        snapshot_wait=0.1,
    )

    assert _run_stress90_order_journal_rollover(config, args) == 0
    assert FakeBroker.sent == 0
    assert sealed[0]["transaction_id"] == _OPERATION_ID
    assert sealed[0]["halted"] is True
    assert sealed[0]["broker_flat"] is True
    assert sealed[0]["local_flat"] is True
    assert sealed[0]["no_active_orders"] is True
    assert sealed[0]["reconciled"] is True
    output = capsys.readouterr().out
    assert '"orders_sent": 0' in output
    assert '"runtime_mode": "HALTED"' in output
    saved = StateStore(tmp_path / "state.json").load()
    assert saved.kill_switch is True
    assert saved.last_order_id == ""
    assert saved.last_trade_id == ""
    assert saved.recent_trade_ids == []
