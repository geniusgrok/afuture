from dataclasses import replace
from types import SimpleNamespace

import pytest

from afuture.cli import _build_cli_engine
from afuture.directional import DirectionalConfig
from afuture.directional_engine import DirectionalTradingEngine
from afuture.directional_risk import DirectionalRiskResponseMode, DirectionalRiskScaledPolicy
from afuture.engine import TradingEngine
from afuture.execution_aligned_runtime import (
    FROZEN_PRODUCTS,
    ExecutionAlignedDirectionalPortfolioManager,
)
from afuture.risk import RiskConfig
from afuture.state import StateStore


class _Broker:
    def __init__(self):
        self.order_journal_config = None
        self.session_capability_required = False

    def configure_order_submission_journal(self, path, **identity):
        self.order_journal_config = (path, identity)

    def require_stress90_session_startup_capability(self):
        self.session_capability_required = True

    def get_account_identity_digest(self):
        return "1" * 64


def _config(enabled: bool, *, policy: str = "", account_registry_path: str = ""):
    return SimpleNamespace(
        risk=RiskConfig(margin_estimate_buffer=1.25 if policy == "stress90" else 1.20),
        auto_flatten_imbalance=True,
        aggressive_ticks=1,
        slippage_ticks=1,
        legging_timeout_seconds=2.0,
        require_live_metadata=False,
        metadata_timeout_seconds=10.0,
        directional=DirectionalConfig(
            enabled=enabled,
            products=FROZEN_PRODUCTS if enabled else (),
            exchanges=("DCE", "CZCE", "SHFE", "INE"),
            signal_max_age_hours=120.0,
            policy=policy,
        ),
        pairs=[],
        contracts={},
        account_registry_path=account_registry_path,
    )


def _write_bound_stress90_policy(runtime_dir, registry_path):
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,
        AccountRuntimeRegistry,
    )
    from afuture.directional_stress90_policy import STRESS90_POLICY, Stress90CandidateState
    from afuture.directional_stress90_state import (
        Stress90BootstrapSeed,
        Stress90PolicyState,
        Stress90PolicyStateStore,
        bind_stress90_account_identity,
    )

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
    state = bind_stress90_account_identity(
        Stress90PolicyState.from_seed(seed),
        "1" * 64,
        account_epoch="3" * 64,
    )
    Stress90PolicyStateStore(runtime_dir / "stress90_policy_state.json").save(state)
    registry = AccountRuntimeRegistry(registry_path)
    registry.initialize(strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION)
    registry.bind_new("1" * 64, runtime_dir, "3" * 64, "4" * 64)
    return state


def test_cli_engine_builder_routes_directional_mode_through_exact_execution_aligned_manager(
    tmp_path,
):
    engine = _build_cli_engine(
        _config(True),
        _Broker(),
        StateStore(tmp_path / "directional.json"),
    )
    assert isinstance(engine, DirectionalTradingEngine)
    assert type(engine.directional_manager) is ExecutionAlignedDirectionalPortfolioManager
    assert engine.directional_manager.signal_cache_path == (
        tmp_path / "directional_ohlc_cache.json"
    )
    assert (
        engine.directional_manager.policy_risk_response_mode
        is DirectionalRiskResponseMode.TARGET_SCALE
    )
    assert isinstance(engine.directional_manager.policy, DirectionalRiskScaledPolicy)
    assert engine.requires_technical_activation_permit is False


def test_cli_engine_builder_routes_explicit_stress90_without_point25_wrapper(tmp_path):
    from afuture.directional_stress90_runtime import Stress90DirectionalPortfolioManager

    broker = _Broker()
    registry_path = tmp_path / "machine" / "registry.json"
    _write_bound_stress90_policy(tmp_path, registry_path)
    engine = _build_cli_engine(
        _config(
            True,
            policy="stress90",
            account_registry_path=str(registry_path),
        ),
        broker,
        StateStore(tmp_path / "directional.json"),
    )

    assert isinstance(engine, DirectionalTradingEngine)
    assert type(engine.directional_manager) is Stress90DirectionalPortfolioManager
    assert (
        engine.directional_manager.policy_risk_response_mode
        is DirectionalRiskResponseMode.FREEZE_NEW_RISK
    )
    assert not isinstance(engine.directional_manager.policy, DirectionalRiskScaledPolicy)
    assert engine.requires_technical_activation_permit is True
    assert broker.order_journal_config == (
        tmp_path / "stress90_ctp_orders.json",
        {
            "policy_id": "directional.stress90",
            "policy_definition_digest": engine.directional_manager.runtime_policy_definition_digest,
            "products_manifest_digest": engine.directional_manager.runtime_products_manifest_digest,
        },
    )
    assert broker.session_capability_required is True


def test_stress90_runtime_copy_cannot_bypass_machine_account_binding(tmp_path):
    from afuture.account_runtime_registry import AccountRuntimeRegistryError
    from afuture.directional_stress90_state import Stress90PolicyStateStore

    registry_path = tmp_path / "machine" / "registry.json"
    runtime_a = tmp_path / "runtime-a"
    runtime_b = tmp_path / "runtime-b"
    state = _write_bound_stress90_policy(runtime_a, registry_path)
    Stress90PolicyStateStore(runtime_b / "stress90_policy_state.json").save(state)

    with pytest.raises(AccountRuntimeRegistryError, match="different runtime"):
        _build_cli_engine(
            _config(
                True,
                policy="stress90",
                account_registry_path=str(registry_path),
            ),
            _Broker(),
            StateStore(runtime_b / "state.json"),
        )


def test_migrated_execution_aligned_runtime_copy_keeps_registry_gate(tmp_path):
    from afuture.account_runtime_registry import AccountRuntimeRegistryError
    from afuture.directional_policy_activation import (
        DIRECTIONAL_POLICY_MIGRATION_CONFIRMATION,
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
        migrate_stress90_to_execution_aligned,
    )
    from afuture.directional_stress90_state import Stress90PolicyStateStore
    from afuture.models import RuntimeMode
    from afuture.state import RuntimeState

    registry_path = tmp_path / "machine" / "registry.json"
    runtime_a = tmp_path / "runtime-a"
    runtime_b = tmp_path / "runtime-b"
    policy = _write_bound_stress90_policy(runtime_a, registry_path)
    Stress90PolicyStateStore(runtime_b / "stress90_policy_state.json").save(policy)
    halted = RuntimeState(
        kill_switch=True,
        kill_reason="commissioning",
        reconciled=True,
        runtime_mode=RuntimeMode.HALTED.value,
    )
    activated = activate_stress90_policy(
        halted,
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        bootstrap_seed_digest=policy.bootstrap_seed_digest,
        account_identity_digest="1" * 64,
        operator_reason="fixture activation",
        strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
    )
    migrated = migrate_stress90_to_execution_aligned(
        activated,
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        account_identity_digest="1" * 64,
        operator_reason="fixture migration",
        strong_confirmation=DIRECTIONAL_POLICY_MIGRATION_CONFIRMATION,
    )
    StateStore(runtime_b / "state.json").save(migrated)

    with pytest.raises(AccountRuntimeRegistryError, match="different runtime"):
        _build_cli_engine(
            _config(
                True,
                policy="execution_aligned",
                account_registry_path=str(registry_path),
            ),
            _Broker(),
            StateStore(runtime_b / "state.json"),
        )


def test_live_migrated_execution_aligned_runtime_requires_fixed_machine_registry(tmp_path):
    from afuture.directional_policy_activation import (
        DIRECTIONAL_POLICY_MIGRATION_CONFIRMATION,
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
        migrate_stress90_to_execution_aligned,
    )
    from afuture.models import RuntimeMode
    from afuture.state import RuntimeState

    registry_path = tmp_path / "alternate-machine-registry.json"
    policy = _write_bound_stress90_policy(tmp_path, registry_path)
    activated = activate_stress90_policy(
        RuntimeState(
            kill_switch=True,
            reconciled=True,
            runtime_mode=RuntimeMode.HALTED.value,
        ),
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        bootstrap_seed_digest=policy.bootstrap_seed_digest,
        account_identity_digest="1" * 64,
        operator_reason="fixture activation",
        strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
    )
    StateStore(tmp_path / "state.json").save(
        migrate_stress90_to_execution_aligned(
            activated,
            broker_flat=True,
            local_flat=True,
            no_active_orders=True,
            reconciled=True,
            account_identity_digest="1" * 64,
            operator_reason="fixture migration",
            strong_confirmation=DIRECTIONAL_POLICY_MIGRATION_CONFIRMATION,
        )
    )
    config = _config(
        True,
        policy="execution_aligned",
        account_registry_path=str(registry_path),
    )
    config.mode = "live"

    with pytest.raises(RuntimeError, match="fixed machine"):
        _build_cli_engine(
            config,
            _Broker(),
            StateStore(tmp_path / "state.json"),
        )


def test_migrated_execution_aligned_runtime_rejects_noncanonical_identity_marker(tmp_path):
    from afuture.directional_policy_activation import (
        DIRECTIONAL_POLICY_MIGRATION_CONFIRMATION,
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
        migrate_stress90_to_execution_aligned,
    )
    from afuture.models import RuntimeMode
    from afuture.state import RuntimeState

    registry_path = tmp_path / "machine-registry.json"
    policy = _write_bound_stress90_policy(tmp_path, registry_path)
    activated = activate_stress90_policy(
        RuntimeState(
            kill_switch=True,
            reconciled=True,
            runtime_mode=RuntimeMode.HALTED.value,
        ),
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        bootstrap_seed_digest=policy.bootstrap_seed_digest,
        account_identity_digest="1" * 64,
        operator_reason="fixture activation",
        strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
    )
    migrated = migrate_stress90_to_execution_aligned(
        activated,
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        account_identity_digest="1" * 64,
        operator_reason="fixture migration",
        strong_confirmation=DIRECTIONAL_POLICY_MIGRATION_CONFIRMATION,
    )
    marker = dict(migrated.strategy_states["directional_policy_identity"])
    marker["products_manifest_digest"] = "f" * 64
    marker["unexpected_authority"] = True
    migrated.strategy_states["directional_policy_identity"] = marker
    StateStore(tmp_path / "state.json").save(migrated)

    with pytest.raises(RuntimeError, match="provenance identity"):
        _build_cli_engine(
            _config(
                True,
                policy="execution_aligned",
                account_registry_path=str(registry_path),
            ),
            _Broker(),
            StateStore(tmp_path / "state.json"),
        )


def test_cli_engine_builder_preserves_plain_trading_engine_when_directional_disabled(tmp_path):
    engine = _build_cli_engine(
        _config(False),
        _Broker(),
        StateStore(tmp_path / "plain.json"),
    )
    assert type(engine) is TradingEngine
