from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from afuture.models import RuntimeMode
from afuture.risk import RiskConfig
from afuture.state import RuntimeState

_OPERATION_ID = "f" * 64


def _empty_session_evidence(account_identity_digest: str, trading_day: str):
    from afuture.broker.ctp_session_query import build_ctp_session_activity_evidence

    return build_ctp_session_activity_evidence(
        account_identity_digest=account_identity_digest,
        trading_day=trading_day,
        order_request_id=11,
        trade_request_id=12,
        orders=(),
        trades=(),
        critical_generation=0,
    )


def _halted_state() -> RuntimeState:
    return RuntimeState(
        kill_switch=True,
        kill_reason="commissioning",
        reconciled=True,
        metadata_verified=True,
        runtime_mode=RuntimeMode.HALTED.value,
    )


def _issue_synthetic_technical_permit(runtime_dir: Path):
    from afuture.stress90_activation_permit import (
        Stress90ActivationEvidence,
        Stress90ActivationPermitStore,
    )

    evidence = Stress90ActivationEvidence(
        account_identity_digest="1" * 64,
        account_snapshot_digest="0" * 64,
        policy_account_epoch="f" * 64,
        account_registry_sequence=1,
        account_registry_checksum="3" * 64,
        account_registry_runtime_identity_digest="4" * 64,
        ctp_trading_day="20260825",
        policy_id="directional.stress90",
        policy_definition_digest="2" * 64,
        policy_manifest_digest="3" * 64,
        products_manifest_digest="4" * 64,
        bootstrap_seed_digest="5" * 64,
        generic_state_sequence=1,
        generic_state_checksum="6" * 64,
        policy_state_sequence=1,
        policy_state_checksum="7" * 64,
        policy_last_decision_digest="8" * 64,
        policy_last_completed_target_day="20260824",
        ohlc_content_digest="9" * 64,
        oi_state_sequence=1,
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
    )
    store = Stress90ActivationPermitStore(runtime_dir / "stress90_activation_permit.json")
    store.issue(evidence)
    return store


def test_activation_requires_every_lifecycle_gate_and_strong_confirmation():
    from afuture.directional_policy_activation import (
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
    )

    common = dict(
        state=_halted_state(),
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        bootstrap_seed_digest="a" * 64,
        account_identity_digest="b" * 64,
        operator_reason="first test-counter activation",
        strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
    )
    activated = activate_stress90_policy(**common)
    marker = activated.strategy_states["directional_policy_identity"]
    assert marker["policy_id"] == "directional.stress90"
    assert marker["bootstrap_seed_digest"] == "a" * 64
    assert marker["account_identity_digest"] == "b" * 64

    for override in (
        {"broker_flat": False},
        {"local_flat": False},
        {"no_active_orders": False},
        {"reconciled": False},
        {"strong_confirmation": "yes"},
        {"state": replace(_halted_state(), runtime_mode=RuntimeMode.RUNNING.value)},
    ):
        with pytest.raises(RuntimeError, match="activation"):
            activate_stress90_policy(**{**common, **override})


def test_runtime_policy_identity_rejects_old_or_mismatched_state():
    from afuture.directional_policy_activation import (
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
        require_directional_policy_identity,
    )
    from afuture.directional_stress90_policy import STRESS90_POLICY

    with pytest.raises(RuntimeError, match="explicit activation"):
        require_directional_policy_identity(
            RuntimeState(),
            policy_id=STRESS90_POLICY.policy_id,
            policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
        )

    activated = activate_stress90_policy(
        _halted_state(),
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        bootstrap_seed_digest="a" * 64,
        account_identity_digest="b" * 64,
        operator_reason="validated migration",
        strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
    )
    require_directional_policy_identity(
        activated,
        policy_id=STRESS90_POLICY.policy_id,
        policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
        products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
        bootstrap_seed_digest="a" * 64,
        account_identity_digest="b" * 64,
    )
    with pytest.raises(RuntimeError, match="bootstrap identity"):
        require_directional_policy_identity(
            activated,
            policy_id=STRESS90_POLICY.policy_id,
            policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
            products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
            bootstrap_seed_digest="b" * 64,
            account_identity_digest="b" * 64,
        )
    with pytest.raises(RuntimeError, match="account identity"):
        require_directional_policy_identity(
            activated,
            policy_id=STRESS90_POLICY.policy_id,
            policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
            products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
            bootstrap_seed_digest="a" * 64,
            account_identity_digest="c" * 64,
        )
    with pytest.raises(RuntimeError, match="product manifest"):
        require_directional_policy_identity(
            activated,
            policy_id=STRESS90_POLICY.policy_id,
            policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
            products_manifest_digest="c" * 64,
        )
    with pytest.raises(RuntimeError, match="identity mismatch"):
        require_directional_policy_identity(
            activated,
            policy_id="execution_aligned",
            policy_definition_digest="",
        )


def test_stress90_to_execution_aligned_migration_is_explicit_flat_and_halted() -> None:
    from afuture.directional_policy_activation import (
        DIRECTIONAL_POLICY_MIGRATION_CONFIRMATION,
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
        migrate_stress90_to_execution_aligned,
        require_directional_policy_identity,
    )

    activated = activate_stress90_policy(
        _halted_state(),
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        bootstrap_seed_digest="a" * 64,
        account_identity_digest="b" * 64,
        operator_reason="commissioned",
        strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
    )
    migrated = migrate_stress90_to_execution_aligned(
        activated,
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        account_identity_digest="b" * 64,
        operator_reason="retire Stress-90 account policy",
        strong_confirmation=DIRECTIONAL_POLICY_MIGRATION_CONFIRMATION,
    )

    require_directional_policy_identity(
        migrated,
        policy_id="execution_aligned",
        policy_definition_digest="",
        account_identity_digest="b" * 64,
    )
    assert migrated.kill_switch is True
    assert migrated.runtime_mode == RuntimeMode.HALTED.value
    marker = migrated.strategy_states["directional_policy_identity"]
    assert marker["migrated_from_policy_id"] == "directional.stress90"

    with pytest.raises(RuntimeError, match="migration lifecycle gates"):
        migrate_stress90_to_execution_aligned(
            activated,
            broker_flat=False,
            local_flat=True,
            no_active_orders=True,
            reconciled=True,
            account_identity_digest="b" * 64,
            operator_reason="unsafe",
            strong_confirmation=DIRECTIONAL_POLICY_MIGRATION_CONFIRMATION,
        )


def test_migrated_execution_aligned_reactivation_requires_both_strong_confirmations() -> None:
    from afuture.directional_policy_activation import (
        DIRECTIONAL_POLICY_MIGRATION_CONFIRMATION,
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
        migrate_stress90_to_execution_aligned,
        reactivate_stress90_policy,
    )
    from afuture.directional_stress90_state import REBASE_CONFIRMATION

    activated = activate_stress90_policy(
        replace(
            _halted_state(),
            trading_day="20260825",
            day_start_equity=500_000.0,
            equity_high_watermark=600_000.0,
        ),
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        bootstrap_seed_digest="a" * 64,
        account_identity_digest="b" * 64,
        operator_reason="commissioned",
        strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
    )
    migrated = migrate_stress90_to_execution_aligned(
        activated,
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        account_identity_digest="b" * 64,
        operator_reason="explicit retirement",
        strong_confirmation=DIRECTIONAL_POLICY_MIGRATION_CONFIRMATION,
    )
    common = dict(
        state=migrated,
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        bootstrap_seed_digest="a" * 64,
        account_identity_digest="b" * 64,
        operator_reason="same-day return to Stress-90",
        activation_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
        rebase_confirmation=REBASE_CONFIRMATION,
    )

    reactivated = reactivate_stress90_policy(**common)

    assert reactivated.strategy_states["directional_policy_identity"]["policy_id"] == (
        "directional.stress90"
    )
    assert reactivated.day_start_equity == migrated.day_start_equity
    assert reactivated.equity_high_watermark == migrated.equity_high_watermark
    with pytest.raises(RuntimeError, match="reactivation confirmation"):
        reactivate_stress90_policy(**{**common, "rebase_confirmation": "unsafe"})


def _write_bootstrap_artifacts(runtime_dir: Path):
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
    Stress90SeedStore(runtime_dir / "stress90_bootstrap_seed.json").save_new(seed)
    Stress90PolicyStateStore(runtime_dir / "stress90_policy_state.json").save(
        Stress90PolicyState.from_seed(seed)
    )
    return seed


def test_activation_cli_commissions_fresh_flat_state_but_leaves_it_halted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from afuture.cli import _run_stress90_activate
    from afuture.directional import DirectionalConfig
    from afuture.directional_policy_activation import (
        POLICY_IDENTITY_STATE_KEY,
        STRESS90_ACTIVATION_CONFIRMATION,
    )
    from afuture.directional_stress90_state import Stress90PolicyStateStore
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.models import AccountSnapshot
    from afuture.state import StateStore

    seed = _write_bootstrap_artifacts(tmp_path)
    permit_store = _issue_synthetic_technical_permit(tmp_path)
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,
        AccountRuntimeRegistry,
    )

    AccountRuntimeRegistry(tmp_path / ".account-runtime-registry.json").initialize(
        strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION
    )

    class FakeBroker:
        def __init__(self, credentials):
            self.credentials = credentials
            self.order_journal_configured = False
            self.fence_entries = 0

        def configure_order_submission_journal(self, _path, **_identity):
            self.order_journal_configured = True

        def refresh_session_activity(self, *, timeout_seconds):
            assert timeout_seconds == 0.1
            return _empty_session_evidence("b" * 64, "20260825")

        def require_session_activity_evidence_current(self, evidence):
            assert evidence == _empty_session_evidence("b" * 64, "20260825")

        def set_raw_tick_observer(self, _observer):
            return None

        @contextmanager
        def lifecycle_state_commit_fence(self):
            self.fence_entries += 1
            yield

        def seed_trade_identities(self, identities):
            assert identities == []

        def start(self):
            return None

        def stop(self):
            return None

        def is_ready(self):
            return True

        def snapshot_marker(self):
            return (0, 0)

        def snapshot_ready(self, marker):
            return True

        def get_account(self):
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

        def get_positions(self):
            return []

        def get_active_orders(self):
            return []

        def get_session_trades(self):
            return []

        def owns_order(self, _order_id):
            return False

        def get_trading_day(self):
            return "20260825"

        def get_account_identity_digest(self):
            return "b" * 64

    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", FakeBroker)
    monkeypatch.setenv(
        "AFUTURE_STRESS90_ACTIVATION_ACK",
        STRESS90_ACTIVATION_CONFIRMATION,
    )
    config = SimpleNamespace(
        mode="live",
        ctp=SimpleNamespace(environment="test"),
        risk=RiskConfig(margin_estimate_buffer=1.25),
        directional=DirectionalConfig(
            enabled=True,
            policy="stress90",
            products=FROZEN_PRODUCTS,
            account_exclusive=True,
        ),
        state_path=str(tmp_path / "state.json"),
        journal_path=str(tmp_path / "audit.jsonl"),
    )
    args = SimpleNamespace(
        confirm_live=False,
        confirm_activation=True,
        startup_timeout=0.1,
        snapshot_wait=0.1,
        runtime_dir="",
        operator_reason="initial test-counter commissioning",
        operation_id=_OPERATION_ID,
    )

    assert _run_stress90_activate(config, args) == 0
    state = StateStore(config.state_path).load()
    marker = state.strategy_states[POLICY_IDENTITY_STATE_KEY]
    assert marker["bootstrap_seed_digest"] == seed.seed_digest
    assert marker["account_identity_digest"] == "b" * 64
    assert state.runtime_mode == RuntimeMode.HALTED.value
    assert state.kill_switch is True
    assert state.reconciled is True
    assert (
        Stress90PolicyStateStore(tmp_path / "stress90_policy_state.json")
        .load_required()
        .live_inception_day
        == "20260825"
    )
    assert permit_store.load_required_record().permit.status == "invalidated"
    from afuture.stress90_lifecycle_transaction import Stress90LifecycleTransactionStore

    assert (
        Stress90LifecycleTransactionStore(tmp_path / "stress90_lifecycle_transaction.json")
        .load_required()
        .status
        == "committed"
    )
    assert "stress90_policy_activation_completed" in Path(config.journal_path).read_text(
        encoding="utf-8"
    )


def test_cli_parser_exposes_explicit_activation_rebase_and_policy_migration_commands():
    from afuture.cli import build_parser

    activate = build_parser().parse_args(
        [
            "stress90-activate",
            "--config",
            "live.toml",
            "--confirm-activation",
            "--confirm-rebase",
            "--operator-reason",
            "commissioning",
            "--operation-id",
            _OPERATION_ID,
        ]
    )
    rebase = build_parser().parse_args(
        [
            "stress90-account-rebase",
            "--config",
            "live.toml",
            "--confirm-rebase",
            "--operator-reason",
            "cash transfer",
            "--operation-id",
            _OPERATION_ID,
        ]
    )
    settlement = build_parser().parse_args(
        [
            "stress90-settlement-roll-forward",
            "--config",
            "live.toml",
            "--confirm-roll-forward",
            "--operator-reason",
            "advance verified CTP settlement",
            "--operation-id",
            _OPERATION_ID,
        ]
    )
    prepare = build_parser().parse_args(
        [
            "stress90-prepare-decision",
            "--config",
            "live.toml",
            "--runtime-dir",
            "runtime",
        ]
    )
    collect = build_parser().parse_args(
        [
            "stress90-oi-collect",
            "--config",
            "live.toml",
            "--runtime-dir",
            "runtime",
            "--once",
        ]
    )
    migrate = build_parser().parse_args(
        [
            "directional-policy-migrate",
            "--config",
            "live.toml",
            "--to",
            "execution_aligned",
            "--confirm-migration",
            "--operator-reason",
            "retire Stress-90 identity",
            "--operation-id",
            _OPERATION_ID,
        ]
    )
    compare = build_parser().parse_args(
        [
            "stress90-oi-compare",
            "--config",
            "live.toml",
            "--trading-day",
            "20260825",
            "--vendor",
            "vendor.json",
        ]
    )
    doctor = build_parser().parse_args(
        [
            "doctor",
            "--config",
            "live.toml",
            "--confirm-live",
            "--issue-stress90-permit",
            "--shadow-account",
        ]
    )

    assert activate.command == "stress90-activate"
    assert activate.confirm_rebase is True
    assert rebase.command == "stress90-account-rebase"
    assert settlement.command == "stress90-settlement-roll-forward"
    assert settlement.confirm_roll_forward is True
    assert prepare.command == "stress90-prepare-decision"
    assert collect.command == "stress90-oi-collect"
    assert collect.once is True
    assert migrate.command == "directional-policy-migrate"
    assert migrate.to == "execution_aligned"
    assert migrate.confirm_migration is True
    assert compare.command == "stress90-oi-compare"
    assert doctor.issue_stress90_permit is True
    assert doctor.shadow_account is True


def test_cli_main_dispatches_explicit_directional_policy_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture import cli

    config = object()
    observed: list[tuple[object, object]] = []
    monkeypatch.setattr(cli, "load_config", lambda *args, **kwargs: config)
    monkeypatch.setattr(
        cli,
        "_run_directional_policy_migrate",
        lambda loaded, args: observed.append((loaded, args)) or 17,
    )

    result = cli.main(
        [
            "directional-policy-migrate",
            "--config",
            "live.toml",
            "--to",
            "execution_aligned",
            "--operator-reason",
            "retire Stress-90 identity",
            "--operation-id",
            _OPERATION_ID,
        ]
    )

    assert result == 17
    assert observed[0][0] is config
    assert observed[0][1].command == "directional-policy-migrate"


def test_cli_main_dispatches_stress90_oi_collection_without_falling_into_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture import cli

    config = object()
    observed: list[tuple[object, object]] = []
    monkeypatch.setattr(cli, "load_config", lambda *args, **kwargs: config)
    monkeypatch.setattr(cli, "configure_logging", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        cli,
        "_run_stress90_oi_collect",
        lambda loaded, args: observed.append((loaded, args)) or 19,
        raising=False,
    )
    monkeypatch.setattr(
        cli,
        "_run_live",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("evidence collection must never enter live order runtime")
        ),
    )

    result = cli.main(
        [
            "stress90-oi-collect",
            "--config",
            "live.toml",
            "--runtime-dir",
            "runtime",
            "--once",
        ]
    )

    assert result == 19
    assert observed[0][0] is config
    assert observed[0][1].command == "stress90-oi-collect"


@pytest.mark.parametrize("reactivation_crash_point", ["after_policy", "after_generic"])
def test_policy_migration_cli_retires_stress90_identity_but_remains_halted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reactivation_crash_point: str,
) -> None:
    from afuture.cli import _run_directional_policy_migrate, _run_stress90_activate
    from afuture.directional import DirectionalConfig
    from afuture.directional_policy_activation import (
        DIRECTIONAL_POLICY_MIGRATION_CONFIRMATION,
        POLICY_IDENTITY_STATE_KEY,
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
    )
    from afuture.directional_stress90_state import (
        REBASE_CONFIRMATION,
        Stress90PolicyStateStore,
        bind_stress90_account_identity,
    )
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.models import AccountSnapshot
    from afuture.state import StateStore

    seed = _write_bootstrap_artifacts(tmp_path)
    generic_store = StateStore(tmp_path / "state.json")
    migration_source = replace(
        _halted_state(),
        trading_day="20260825",
        day_start_equity=500_000.0,
        equity_high_watermark=500_000.0,
        last_account_equity=500_000.0,
        last_account_trading_day="20260825",
        last_account_deposit=0.0,
        last_account_withdrawal=0.0,
        last_account_cash_flow_verified=True,
        last_account_settlement_id=42,
        recent_daily_returns=[-0.08, 0.03],
    )
    generic_store.save(
        activate_stress90_policy(
            migration_source,
            broker_flat=True,
            local_flat=True,
            no_active_orders=True,
            reconciled=True,
            bootstrap_seed_digest=seed.seed_digest,
            account_identity_digest="b" * 64,
            operator_reason="commissioned",
            strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
        )
    )
    policy_store = Stress90PolicyStateStore(tmp_path / "stress90_policy_state.json")
    policy_record = policy_store.load_required_record()
    policy_store.save(
        bind_stress90_account_identity(
            policy_record.state,
            "c" * 64,
            account_epoch="e" * 64,
        ),
        expected_sequence=policy_record.sequence,
    )
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,
        AccountRuntimeRegistry,
    )

    registry = AccountRuntimeRegistry(tmp_path / ".account-runtime-registry.json")
    registry.initialize(strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION)
    registry.bind_new("b" * 64, tmp_path, "e" * 64, "9" * 64)
    permit_store = _issue_synthetic_technical_permit(tmp_path)
    cumulative_deposit = 100_000.0

    class FakeBroker:
        def __init__(self, credentials):
            self.credentials = credentials
            self.order_journal_configured = False
            self.fence_entries = 0

        def configure_order_submission_journal(self, _path, **_identity):
            self.order_journal_configured = True

        def refresh_session_activity(self, *, timeout_seconds):
            assert timeout_seconds == 0.1
            return _empty_session_evidence("b" * 64, "20260825")

        def require_session_activity_evidence_current(self, evidence):
            assert evidence == _empty_session_evidence("b" * 64, "20260825")

        def set_raw_tick_observer(self, _observer):
            return None

        @contextmanager
        def lifecycle_state_commit_fence(self):
            self.fence_entries += 1
            yield

        def seed_trade_identities(self, identities):
            assert list(identities) == []

        def start(self):
            return None

        def stop(self):
            return None

        def is_ready(self):
            return True

        def snapshot_marker(self):
            return (0, 0)

        def snapshot_ready(self, marker):
            return marker == (0, 0)

        def get_account(self):
            return AccountSnapshot(
                500_000,
                500_000,
                500_000,
                0,
                0,
                0,
                "20260825",
                deposit=cumulative_deposit,
                previous_settlement_equity=500_000,
                settlement_verified=True,
                settlement_id=42,
            )

        def get_positions(self):
            return []

        def get_active_orders(self):
            return []

        def get_session_trades(self):
            return []

        def owns_order(self, _order_id):
            return False

        def get_trading_day(self):
            return "20260825"

        def get_account_identity_digest(self):
            return "b" * 64

    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", FakeBroker)
    monkeypatch.setenv(
        "AFUTURE_DIRECTIONAL_POLICY_MIGRATION_ACK",
        DIRECTIONAL_POLICY_MIGRATION_CONFIRMATION,
    )
    config = SimpleNamespace(
        mode="live",
        ctp=SimpleNamespace(
            environment="test",
            account_id="account-a",
            currency_id="CNY",
        ),
        risk=RiskConfig(margin_estimate_buffer=1.25),
        directional=DirectionalConfig(
            enabled=True,
            policy="execution_aligned",
            products=FROZEN_PRODUCTS,
            account_exclusive=True,
        ),
        state_path=str(tmp_path / "state.json"),
        journal_path=str(tmp_path / "audit.jsonl"),
    )
    args = SimpleNamespace(
        to="execution_aligned",
        confirm_live=False,
        confirm_migration=True,
        startup_timeout=0.1,
        snapshot_wait=0.1,
        runtime_dir="",
        operator_reason="retire Stress-90 after flat reconcile",
        operation_id=_OPERATION_ID,
        shadow_account=False,
    )

    monkeypatch.setattr(
        "afuture.cli._adopt_stress90_lifecycle_crash_fills",
        lambda _config, _broker, *, state, **_kwargs: state,
    )

    with pytest.raises(RuntimeError, match="policy/account identity"):
        _run_directional_policy_migrate(config, args)
    mismatched_record = policy_store.load_required_record()
    policy_store.save(
        bind_stress90_account_identity(
            mismatched_record.state,
            "b" * 64,
            account_epoch="e" * 64,
            allow_rebind=True,
        ),
        expected_sequence=mismatched_record.sequence,
    )
    with pytest.raises(RuntimeError, match="(cash.flow|rebase)"):
        _run_directional_policy_migrate(config, args)
    cumulative_deposit = 0.0
    assert _run_directional_policy_migrate(config, args) == 0
    migrated = generic_store.load()
    marker = migrated.strategy_states[POLICY_IDENTITY_STATE_KEY]
    assert marker["policy_id"] == "execution_aligned"
    assert marker["migrated_from_policy_id"] == "directional.stress90"
    assert migrated.runtime_mode == RuntimeMode.HALTED.value
    assert migrated.kill_switch is True
    assert migrated.metadata_verified is False
    assert policy_store.load_required().live_account_identity_digest == "b" * 64
    assert permit_store.load_required_record().permit.status == "invalidated"
    from afuture.stress90_lifecycle_transaction import Stress90LifecycleTransactionStore

    transaction = Stress90LifecycleTransactionStore(
        tmp_path / "stress90_lifecycle_transaction.json"
    ).load_required()
    assert transaction.operation == "stress90_to_execution_aligned"
    assert transaction.status == "committed"
    audit = Path(config.journal_path).read_text(encoding="utf-8")
    assert "directional_policy_migration_completed" in audit
    assert "retire Stress-90 after flat reconcile" in audit

    migrated_policy = policy_store.load_required()
    source_epoch = migrated_policy.live_account_epoch
    config.directional = replace(config.directional, policy="stress90")
    monkeypatch.setenv(
        "AFUTURE_STRESS90_ACTIVATION_ACK",
        STRESS90_ACTIVATION_CONFIRMATION,
    )
    monkeypatch.setenv("AFUTURE_STRESS90_REBASE_ACK", REBASE_CONFIRMATION)
    reactivate_args = SimpleNamespace(
        confirm_live=False,
        confirm_activation=True,
        confirm_rebase=True,
        startup_timeout=0.1,
        snapshot_wait=0.1,
        runtime_dir="",
        operator_reason="explicit same-day Stress-90 reactivation",
        operation_id="a" * 64,
        shadow_account=False,
    )

    original_mark_committed = Stress90LifecycleTransactionStore.mark_committed
    original_generic_save = StateStore.save

    def crash_after_both_targets(self, transaction):
        raise OSError("injected reactivation commit-marker crash")

    def crash_before_generic_target(self, *args, **kwargs):
        if self.path == generic_store.path:
            raise OSError("injected reactivation generic-target crash")
        return original_generic_save(self, *args, **kwargs)

    if reactivation_crash_point == "after_policy":
        monkeypatch.setattr(StateStore, "save", crash_before_generic_target)
        crash_message = "reactivation generic-target crash"
    else:
        monkeypatch.setattr(
            Stress90LifecycleTransactionStore,
            "mark_committed",
            crash_after_both_targets,
        )
        crash_message = "reactivation commit-marker crash"
    with pytest.raises(OSError, match=crash_message):
        _run_stress90_activate(config, reactivate_args)
    prepared = Stress90LifecycleTransactionStore(
        tmp_path / "stress90_lifecycle_transaction.json"
    ).load_required()
    assert prepared.operation == "reactivation"
    assert prepared.status == "prepared"
    assert prepared.generic_target.recent_daily_returns == []
    assert prepared.policy_target.recent_daily_returns_for_adaptive_margin == ()
    expected_intermediate_policy = (
        "execution_aligned"
        if reactivation_crash_point == "after_policy"
        else "directional.stress90"
    )
    assert generic_store.load().strategy_states[POLICY_IDENTITY_STATE_KEY]["policy_id"] == (
        expected_intermediate_policy
    )
    monkeypatch.setattr(StateStore, "save", original_generic_save)
    monkeypatch.setattr(
        Stress90LifecycleTransactionStore,
        "mark_committed",
        original_mark_committed,
    )

    assert _run_stress90_activate(config, reactivate_args) == 0
    reactivated = generic_store.load()
    reactivated_policy = policy_store.load_required()
    marker = reactivated.strategy_states[POLICY_IDENTITY_STATE_KEY]
    assert marker["policy_id"] == "directional.stress90"
    assert reactivated.runtime_mode == RuntimeMode.HALTED.value
    assert reactivated.kill_switch is True
    assert reactivated.day_start_equity == migrated.day_start_equity
    assert reactivated.equity_high_watermark == migrated.equity_high_watermark
    assert reactivated.recent_daily_returns == []
    assert reactivated_policy.completed_account_wealth == 1.0
    assert reactivated_policy.completed_account_high_watermark == 1.0
    assert reactivated_policy.last_completed_account_day is None
    assert reactivated_policy.live_account_epoch != source_epoch
    transaction = Stress90LifecycleTransactionStore(
        tmp_path / "stress90_lifecycle_transaction.json"
    ).load_required()
    assert transaction.operation == "reactivation"
    assert transaction.status == "committed"


def test_account_rebase_cli_resets_only_soft_path_and_records_operator_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from afuture.cli import _run_stress90_account_rebase
    from afuture.directional import DirectionalConfig
    from afuture.directional_policy_activation import (
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
    )
    from afuture.directional_stress90_state import (
        REBASE_CONFIRMATION,
        Stress90PolicyStateStore,
        bind_stress90_account_identity,
        record_completed_account_day,
    )
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.models import AccountSnapshot
    from afuture.state import StateStore

    seed = _write_bootstrap_artifacts(tmp_path)
    account_source = replace(
        _halted_state(),
        trading_day="20260825",
        day_start_equity=500_000.0,
        equity_high_watermark=500_000.0,
        last_account_equity=500_000.0,
        last_account_trading_day="20260825",
        last_account_cash_flow_verified=True,
        last_account_settlement_id=42,
    )
    generic = activate_stress90_policy(
        account_source,
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        bootstrap_seed_digest=seed.seed_digest,
        account_identity_digest="b" * 64,
        operator_reason="commissioned",
        strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
    )
    StateStore(tmp_path / "state.json").save(generic)
    policy_store = Stress90PolicyStateStore(tmp_path / "stress90_policy_state.json")
    policy_store.save(
        record_completed_account_day(
            bind_stress90_account_identity(
                policy_store.load_required(),
                "b" * 64,
                account_epoch="e" * 64,
            ),
            "20260825",
            -0.10,
        )
    )
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,
        AccountRuntimeRegistry,
    )

    registry = AccountRuntimeRegistry(tmp_path / ".account-runtime-registry.json")
    registry.initialize(strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION)
    registry.bind_new("b" * 64, tmp_path, "e" * 64, "a" * 64)
    from afuture.trading_day_evidence import TradingDayEvidenceStore

    TradingDayEvidenceStore(tmp_path / "ctp_trading_day_evidence.json").bind_for_lifecycle(
        transaction=SimpleNamespace(
            operation="activation",
            status="committed",
            transaction_id="d" * 64,
            operation_nonce="a" * 64,
            source_account_identity_digest="",
            source_account_epoch="",
            account_identity_digest="b" * 64,
            trading_day="20260825",
            policy_target=SimpleNamespace(
                live_account_identity_digest="b" * 64,
                live_account_epoch="e" * 64,
            ),
        ),
        runtime_dir=tmp_path,
        binding_evidence=registry.require_binding_evidence("b" * 64, tmp_path, "e" * 64),
    )
    permit_store = _issue_synthetic_technical_permit(tmp_path)

    class FakeBroker:
        def __init__(self, credentials):
            self.credentials = credentials
            self.order_journal_configured = False
            self.fence_entries = 0

        def configure_order_submission_journal(self, _path, **_identity):
            self.order_journal_configured = True

        def refresh_session_activity(self, *, timeout_seconds):
            assert timeout_seconds == 0.1
            return _empty_session_evidence("c" * 64, "20260826")

        def require_session_activity_evidence_current(self, evidence):
            assert evidence == _empty_session_evidence("c" * 64, "20260826")

        def set_raw_tick_observer(self, _observer):
            return None

        @contextmanager
        def lifecycle_state_commit_fence(self):
            self.fence_entries += 1
            yield

        def seed_trade_identities(self, identities):
            return None

        def start(self):
            return None

        def stop(self):
            return None

        def is_ready(self):
            return True

        def snapshot_marker(self):
            return (0, 0)

        def snapshot_ready(self, marker):
            return True

        def get_account(self):
            return AccountSnapshot(
                700_000,
                700_000,
                700_000,
                0,
                0,
                0,
                "20260826",
                previous_settlement_equity=700_000,
                settlement_verified=True,
                settlement_id=43,
            )

        def get_positions(self):
            return []

        def get_active_orders(self):
            return []

        def get_session_trades(self):
            return []

        def owns_order(self, _order_id):
            return False

        def get_trading_day(self):
            return "20260826"

        def get_account_identity_digest(self):
            return "c" * 64

    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", FakeBroker)
    monkeypatch.setenv("AFUTURE_STRESS90_REBASE_ACK", REBASE_CONFIRMATION)
    config = SimpleNamespace(
        mode="live",
        ctp=SimpleNamespace(environment="test"),
        directional=DirectionalConfig(
            enabled=True,
            policy="stress90",
            products=FROZEN_PRODUCTS,
            account_exclusive=True,
        ),
        state_path=str(tmp_path / "state.json"),
        journal_path=str(tmp_path / "audit.jsonl"),
    )
    args = SimpleNamespace(
        confirm_live=False,
        confirm_rebase=True,
        startup_timeout=0.1,
        snapshot_wait=0.1,
        runtime_dir="",
        operator_reason="verified cash withdrawal",
        operation_id=_OPERATION_ID,
    )

    assert _run_stress90_account_rebase(config, args) == 0
    rebased = policy_store.load_required()
    assert rebased.completed_account_wealth == 1.0
    assert rebased.completed_account_high_watermark == 1.0
    assert rebased.last_completed_account_day is None
    assert rebased.live_inception_day == "20260826"
    assert rebased.live_account_identity_digest == "c" * 64
    assert permit_store.load_required_record().permit.status == "invalidated"
    from afuture.stress90_lifecycle_transaction import Stress90LifecycleTransactionStore

    assert (
        Stress90LifecycleTransactionStore(tmp_path / "stress90_lifecycle_transaction.json")
        .load_required()
        .status
        == "committed"
    )
    audit_text = Path(config.journal_path).read_text(encoding="utf-8")
    assert "stress90_account_rebase_completed" in audit_text
    assert "verified cash withdrawal" in audit_text


def test_lifecycle_precommit_rechecks_bound_account_day_continuity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.cli import _require_stress90_lifecycle_precommit_current

    calls: list[str] = []
    transaction = SimpleNamespace(
        account_day_continuity_digest="d" * 64,
        account_day_continuity_source_day="20260825",
        trading_day="20260826",
    )
    monkeypatch.setattr(
        "afuture.cli._require_lifecycle_mechanical_snapshot_current",
        lambda broker, snapshot: calls.append("mechanical"),
    )
    monkeypatch.setattr(
        "afuture.cli._require_stress90_account_day_continuity",
        lambda **kwargs: SimpleNamespace(continuity_digest="d" * 64),
    )

    _require_stress90_lifecycle_precommit_current(
        object(),
        object(),
        runtime_dir=tmp_path,
        lifecycle_transaction=transaction,
    )

    assert calls == ["mechanical", "mechanical"]
    monkeypatch.setattr(
        "afuture.cli._require_stress90_account_day_continuity",
        lambda **kwargs: SimpleNamespace(continuity_digest="e" * 64),
    )
    with pytest.raises(RuntimeError, match="continuity evidence changed"):
        _require_stress90_lifecycle_precommit_current(
            object(),
            object(),
            runtime_dir=tmp_path,
            lifecycle_transaction=transaction,
        )


def test_settlement_roll_forward_requires_strong_confirmation_before_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.cli import _run_stress90_settlement_roll_forward
    from afuture.directional import DirectionalConfig
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS

    class ForbiddenBroker:
        def __init__(self, credentials):
            del credentials
            raise AssertionError("confirmation must be checked before Broker construction")

    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", ForbiddenBroker)
    config = SimpleNamespace(
        mode="live",
        ctp=SimpleNamespace(environment="test"),
        directional=DirectionalConfig(
            enabled=True,
            policy="stress90",
            products=FROZEN_PRODUCTS,
            account_exclusive=True,
        ),
        state_path=str(tmp_path / "state.json"),
        journal_path=str(tmp_path / "audit.jsonl"),
    )
    args = SimpleNamespace(
        confirm_live=False,
        confirm_roll_forward=True,
        startup_timeout=0.1,
        snapshot_wait=0.1,
        runtime_dir="",
        operator_reason="verified CTP settlement rollover",
        operation_id=_OPERATION_ID,
        shadow_account=False,
    )

    with pytest.raises(RuntimeError, match="ROLL_FORWARD_STRESS90_SETTLEMENT"):
        _run_stress90_settlement_roll_forward(config, args)


def test_settlement_roll_forward_reports_external_funding_witness_blocker_before_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.cli import _run_stress90_settlement_roll_forward
    from afuture.directional import DirectionalConfig
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS

    class ForbiddenBroker:
        def __init__(self, credentials):
            del credentials
            raise AssertionError("external blocker must be checked before Broker construction")

    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", ForbiddenBroker)
    monkeypatch.setenv(
        "AFUTURE_STRESS90_SETTLEMENT_ACK",
        "ROLL_FORWARD_STRESS90_SETTLEMENT",
    )
    config = SimpleNamespace(
        mode="live",
        ctp=SimpleNamespace(environment="test"),
        directional=DirectionalConfig(
            enabled=True,
            policy="stress90",
            products=FROZEN_PRODUCTS,
            account_exclusive=True,
        ),
        state_path=str(tmp_path / "state.json"),
        journal_path=str(tmp_path / "audit.jsonl"),
    )
    args = SimpleNamespace(
        confirm_live=True,
        confirm_roll_forward=True,
        startup_timeout=0.1,
        snapshot_wait=0.1,
        runtime_dir="",
        operator_reason="operator assertion is not authoritative evidence",
        operation_id=_OPERATION_ID,
        shadow_account=False,
    )

    with pytest.raises(
        RuntimeError,
        match="authoritative prior-day final funding/settlement witness is unavailable",
    ) as error:
        _run_stress90_settlement_roll_forward(config, args)

    detail = str(error.value)
    for insufficient in (
        "D+1 PreBalance/current Deposit/Withdraw",
        "CTP TransferSerial alone",
        "opaque SettlementInfo content",
        "operator assertion",
    ):
        assert insufficient in detail
    assert list(tmp_path.iterdir()) == []


def test_shadow_activation_reads_canonical_persistent_account_and_rejects_position(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.cli import _run_stress90_activate
    from afuture.directional import DirectionalConfig
    from afuture.directional_policy_activation import STRESS90_ACTIVATION_CONFIRMATION
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.models import AccountSnapshot, ContractPosition

    runtime_dir = tmp_path / "shadow"
    _write_bootstrap_artifacts(runtime_dir)
    captured: list[dict[str, object]] = []

    class FakeLive:
        def __init__(self, credentials):
            self.credentials = credentials

        def get_account_identity_digest(self):
            return "b" * 64

        def seed_trade_identities(self, identities):
            assert list(identities) == []

        def configure_order_submission_journal(self, _path, **_identity):
            return None

    class FakeShadow:
        def __init__(self, live, initial_capital, **kwargs):
            self.live = live
            self.fence_entries = 0
            captured.append({"initial_capital": initial_capital, **kwargs})

        def configure_order_submission_journal(self, path, **identity):
            self.live.configure_order_submission_journal(path, **identity)

        def refresh_session_activity(self, *, timeout_seconds):
            assert timeout_seconds == 0.1
            return _empty_session_evidence(self.live.get_account_identity_digest(), "20260825")

        def get_session_activity_account_identity_digest(self):
            return self.live.get_account_identity_digest()

        def require_session_activity_evidence_current(self, evidence):
            assert evidence == _empty_session_evidence(
                self.live.get_account_identity_digest(), "20260825"
            )

        def set_raw_tick_observer(self, _observer):
            return None

        @contextmanager
        def lifecycle_state_commit_fence(self):
            self.fence_entries += 1
            yield

        def update_specs(self, specs):
            assert specs == {}

        def start(self):
            return None

        def stop(self):
            return None

        def is_ready(self):
            return True

        def snapshot_marker(self):
            return (0, 0)

        def snapshot_ready(self, marker):
            return marker == (0, 0)

        def get_account_identity_digest(self):
            return "d" * 64

        def get_account(self):
            return AccountSnapshot(
                500_000,
                500_000,
                499_000,
                1_000,
                0,
                0,
                "20260825",
                previous_settlement_equity=500_000,
                settlement_verified=True,
                settlement_id=42,
            )

        def get_trading_day(self):
            return "20260825"

        def get_positions(self):
            return [ContractPosition("M2612", "DCE", long_today=1, long_price=100.0)]

        def get_active_orders(self):
            return []

        def get_session_trades(self):
            return []

        def owns_order(self, _order_id):
            return False

    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", FakeLive)
    monkeypatch.setattr("afuture.broker.shadow.ShadowBroker", FakeShadow)
    monkeypatch.setattr(
        "afuture.cli._checkpoint_ctp_trading_day",
        lambda *args: (_ for _ in ()).throw(AssertionError("Shadow must not checkpoint live day")),
    )
    monkeypatch.setenv("AFUTURE_STRESS90_ACTIVATION_ACK", STRESS90_ACTIVATION_CONFIRMATION)
    config = SimpleNamespace(
        mode="live",
        ctp=SimpleNamespace(environment="test"),
        directional=DirectionalConfig(
            enabled=True,
            policy="stress90",
            products=FROZEN_PRODUCTS,
            account_exclusive=True,
        ),
        initial_capital=500_000,
        slippage_ticks=2,
        latency_ticks=3,
        market_impact_ticks=4,
        contracts={},
        state_path=str(tmp_path / "state.json"),
        journal_path=str(tmp_path / "audit.jsonl"),
    )
    args = SimpleNamespace(
        confirm_live=False,
        confirm_activation=True,
        startup_timeout=0.1,
        snapshot_wait=0.1,
        runtime_dir=str(runtime_dir),
        operator_reason="Shadow commissioning",
        operation_id=_OPERATION_ID,
        shadow_account=True,
    )

    monkeypatch.setattr(
        "afuture.cli._adopt_stress90_lifecycle_crash_fills",
        lambda _config, _broker, *, state, **_kwargs: state,
    )

    with pytest.raises(RuntimeError, match="reconciliation failed"):
        _run_stress90_activate(config, args)

    assert captured == [
        {
            "initial_capital": 500_000,
            "slippage_ticks": 2,
            "latency_ticks": 3,
            "market_impact_ticks": 4,
            "state_path": runtime_dir / "shadow_broker_state.json",
        }
    ]


def test_shadow_rebase_reads_canonical_persistent_account_and_rejects_active_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.cli import _run_stress90_account_rebase
    from afuture.directional import DirectionalConfig
    from afuture.directional_policy_activation import (
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
    )
    from afuture.directional_stress90_state import (
        REBASE_CONFIRMATION,
        Stress90PolicyStateStore,
        bind_stress90_account_identity,
    )
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.models import AccountSnapshot
    from afuture.state import StateStore

    runtime_dir = tmp_path / "shadow"
    seed = _write_bootstrap_artifacts(runtime_dir)
    policy_store = Stress90PolicyStateStore(runtime_dir / "stress90_policy_state.json")
    policy_record = policy_store.load_required_record()
    policy_store.save(
        bind_stress90_account_identity(
            policy_record.state,
            "d" * 64,
            account_epoch="e" * 64,
        ),
        expected_sequence=policy_record.sequence,
    )
    StateStore(runtime_dir / "state.json").save(
        activate_stress90_policy(
            replace(
                _halted_state(),
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
            account_identity_digest="d" * 64,
            operator_reason="commissioned Shadow",
            strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
        )
    )

    class FakeLive:
        def __init__(self, credentials):
            self.credentials = credentials

        def get_account_identity_digest(self):
            return "b" * 64

        def seed_trade_identities(self, identities):
            assert list(identities) == []

        def configure_order_submission_journal(self, _path, **_identity):
            return None

    class FakeShadow:
        def __init__(self, live, initial_capital, **kwargs):
            self.live = live
            self.fence_entries = 0
            del initial_capital
            assert kwargs == {
                "slippage_ticks": 2,
                "latency_ticks": 3,
                "market_impact_ticks": 4,
                "state_path": runtime_dir / "shadow_broker_state.json",
            }

        def configure_order_submission_journal(self, path, **identity):
            self.live.configure_order_submission_journal(path, **identity)

        def refresh_session_activity(self, *, timeout_seconds):
            assert timeout_seconds == 0.1
            return _empty_session_evidence(self.live.get_account_identity_digest(), "20260825")

        def get_session_activity_account_identity_digest(self):
            return self.live.get_account_identity_digest()

        def require_session_activity_evidence_current(self, evidence):
            assert evidence == _empty_session_evidence(
                self.live.get_account_identity_digest(), "20260825"
            )

        def set_raw_tick_observer(self, _observer):
            return None

        @contextmanager
        def lifecycle_state_commit_fence(self):
            self.fence_entries += 1
            yield

        def update_specs(self, specs):
            assert specs == {}

        def start(self):
            return None

        def stop(self):
            return None

        def is_ready(self):
            return True

        def snapshot_marker(self):
            return (0, 0)

        def snapshot_ready(self, marker):
            return marker == (0, 0)

        def get_account_identity_digest(self):
            return "d" * 64

        def get_account(self):
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

        def get_trading_day(self):
            return "20260825"

        def get_positions(self):
            return []

        def get_active_orders(self):
            return [object()]

        def get_session_trades(self):
            return []

        def owns_order(self, _order_id):
            return False

    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", FakeLive)
    monkeypatch.setattr("afuture.broker.shadow.ShadowBroker", FakeShadow)
    monkeypatch.setattr(
        "afuture.cli._checkpoint_ctp_trading_day",
        lambda *args: (_ for _ in ()).throw(AssertionError("Shadow must not checkpoint live day")),
    )
    monkeypatch.setenv("AFUTURE_STRESS90_REBASE_ACK", REBASE_CONFIRMATION)
    config = SimpleNamespace(
        mode="live",
        ctp=SimpleNamespace(environment="test"),
        directional=DirectionalConfig(
            enabled=True,
            policy="stress90",
            products=FROZEN_PRODUCTS,
            account_exclusive=True,
        ),
        initial_capital=500_000,
        slippage_ticks=2,
        latency_ticks=3,
        market_impact_ticks=4,
        contracts={},
        state_path=str(tmp_path / "state.json"),
        journal_path=str(tmp_path / "audit.jsonl"),
    )
    args = SimpleNamespace(
        confirm_live=False,
        confirm_rebase=True,
        startup_timeout=0.1,
        snapshot_wait=0.1,
        runtime_dir=str(runtime_dir),
        operator_reason="verify persistent active order",
        operation_id=_OPERATION_ID,
        shadow_account=True,
    )

    with pytest.raises(RuntimeError, match="lifecycle gate failed"):
        _run_stress90_account_rebase(config, args)


def test_activation_missing_current_with_previous_evidence_fails_before_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.cli import _run_stress90_activate
    from afuture.directional import DirectionalConfig
    from afuture.directional_policy_activation import STRESS90_ACTIVATION_CONFIRMATION
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.state import RuntimeState, StateIntegrityError, StateStore

    _write_bootstrap_artifacts(tmp_path)
    store = StateStore(tmp_path / "state.json")
    store.save(RuntimeState(kill_switch=True, runtime_mode=RuntimeMode.HALTED.value))
    store.path.replace(store.previous_path)

    class ForbiddenBroker:
        def __init__(self, credentials):
            del credentials
            raise AssertionError("untrusted local state must fail before CTP construction")

    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", ForbiddenBroker)
    monkeypatch.setenv("AFUTURE_STRESS90_ACTIVATION_ACK", STRESS90_ACTIVATION_CONFIRMATION)
    config = SimpleNamespace(
        mode="live",
        ctp=SimpleNamespace(environment="test"),
        directional=DirectionalConfig(
            enabled=True,
            policy="stress90",
            products=FROZEN_PRODUCTS,
            account_exclusive=True,
        ),
        state_path=str(store.path),
        journal_path=str(tmp_path / "audit.jsonl"),
    )
    args = SimpleNamespace(
        confirm_live=False,
        confirm_activation=True,
        startup_timeout=0.1,
        snapshot_wait=0.1,
        runtime_dir="",
        operator_reason="must fail closed",
        operation_id=_OPERATION_ID,
        shadow_account=False,
    )

    with pytest.raises(StateIntegrityError, match="missing while incident evidence exists"):
        _run_stress90_activate(config, args)
