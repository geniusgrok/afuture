"""Construct the single runtime engine selected by the validated account strategy mode."""

from __future__ import annotations

from pathlib import Path

from .engine import TradingEngine
from .risk import RiskManager


def _require_stress90_account_runtime_binding(config, broker, runtime_dir: Path):
    """Bind Broker, policy epoch, and canonical runtime before constructing authority."""

    from .account_runtime_registry import (
        PRODUCTION_ACCOUNT_RUNTIME_REGISTRY_PATH,
        AccountRuntimeRegistry,
    )
    from .directional_stress90_state import Stress90PolicyStateStore

    policy = Stress90PolicyStateStore(runtime_dir / "stress90_policy_state.json").load_required()
    account = policy.live_account_identity_digest
    epoch = policy.live_account_epoch
    if account is None or epoch is None:
        raise RuntimeError("Stress-90 policy account registry identity is missing")
    get_account_identity = getattr(broker, "get_account_identity_digest", None)
    if not callable(get_account_identity):
        raise RuntimeError("Stress-90 Broker account identity capability is missing")
    if get_account_identity() != account:
        raise RuntimeError("Stress-90 Broker/policy account identity mismatch")
    registry_path = Path(str(getattr(config, "account_registry_path", "")))
    if (
        getattr(config, "mode", None) == "live"
        and registry_path != PRODUCTION_ACCOUNT_RUNTIME_REGISTRY_PATH
    ):
        raise RuntimeError("Stress-90 lineage runtime requires the fixed machine account registry")
    return AccountRuntimeRegistry(registry_path).require_binding_evidence(
        account,
        runtime_dir,
        epoch,
    )


def _is_stress90_migrated_execution_aligned_state(state_store) -> bool:
    from .directional_policy_activation import POLICY_IDENTITY_STATE_KEY
    from .directional_stress90_policy import STRESS90_POLICY

    record = state_store.load_record()
    if record is None:
        return False
    marker = record.state.strategy_states.get(POLICY_IDENTITY_STATE_KEY)
    if not isinstance(marker, dict):
        return False
    provenance_fields = {
        "migrated_from_policy_id",
        "migrated_from_policy_definition_digest",
    }
    if not provenance_fields.intersection(marker):
        return False
    expected_fields = {
        "policy_id",
        "policy_definition_digest",
        "products_manifest_digest",
        "account_identity_digest",
        "operator_reason",
        "migrated_from_policy_id",
        "migrated_from_policy_definition_digest",
    }
    from .directional_stress90_state import Stress90PolicyStateStore

    policy = Stress90PolicyStateStore(
        Path(state_store.path).with_name("stress90_policy_state.json")
    ).load_required()
    if (
        set(marker) != expected_fields
        or marker.get("policy_id") != "execution_aligned"
        or marker.get("policy_definition_digest") != ""
        or marker.get("products_manifest_digest") != STRESS90_POLICY.products_manifest_digest
        or marker.get("account_identity_digest") != policy.live_account_identity_digest
        or not isinstance(marker.get("operator_reason"), str)
        or not str(marker.get("operator_reason", "")).strip()
        or marker.get("migrated_from_policy_id") != STRESS90_POLICY.policy_id
        or marker.get("migrated_from_policy_definition_digest")
        != STRESS90_POLICY.policy_definition_digest
    ):
        raise RuntimeError("directional migration provenance identity is invalid")
    return True


def build_runtime_engine(
    config,
    broker,
    state_store,
    *,
    journal=None,
    alert_manager=None,
    auto_manager=None,
    quality_recorder=None,
    health_clock=None,
    historical_mode: bool = False,
):
    # A prepared Stress-90 lifecycle transaction means the generic runtime and
    # policy state may temporarily describe different revisions.  Every engine
    # built from this runtime directory is order-capable, including plain and
    # auto modes, so none may start until the exact lifecycle operation resumes.
    from .stress90_lifecycle_transaction import (
        require_no_pending_stress90_lifecycle_transaction,
    )

    require_no_pending_stress90_lifecycle_transaction(Path(state_store.path).parent)
    risk_manager = RiskManager(config.risk)
    common = dict(
        auto_flatten_imbalance=config.auto_flatten_imbalance,
        aggressive_ticks=config.aggressive_ticks,
        slippage_ticks=config.slippage_ticks,
        legging_timeout_seconds=config.legging_timeout_seconds,
        journal=journal,
        alert_manager=alert_manager,
        auto_manager=auto_manager,
        quality_recorder=quality_recorder,
        require_live_metadata=config.require_live_metadata,
        metadata_timeout_seconds=config.metadata_timeout_seconds,
        historical_mode=historical_mode,
    )
    if health_clock is not None:
        common["health_clock"] = health_clock

    if config.directional.enabled:
        from .directional_engine import DirectionalTradingEngine

        activity_path = Path(state_store.path).with_name("directional_activity.json")
        ohlc_cache_path = Path(state_store.path).with_name("directional_ohlc_cache.json")
        policy_name = config.directional.policy or "execution_aligned"
        manager_common = dict(
            aggressive_ticks=config.aggressive_ticks,
            metadata_timeout_seconds=config.metadata_timeout_seconds,
            static_specs=config.contracts,
            activity_store_path=activity_path,
            ohlc_cache_path=ohlc_cache_path,
            quality_recorder=quality_recorder,
        )
        if policy_name == "execution_aligned":
            if _is_stress90_migrated_execution_aligned_state(state_store):
                _require_stress90_account_runtime_binding(
                    config,
                    broker,
                    Path(state_store.path).parent.resolve(strict=False),
                )
            from .execution_aligned_runtime import (
                ExecutionAlignedDirectionalPortfolioManager,
            )

            manager = ExecutionAlignedDirectionalPortfolioManager(
                config.directional,
                broker,
                risk_manager,
                **manager_common,
            )
        elif policy_name == "stress90":
            from .directional_stress90_runtime import (
                Stress90DirectionalPortfolioManager,
            )

            runtime_dir = Path(state_store.path).parent.resolve(strict=False)
            _require_stress90_account_runtime_binding(
                config,
                broker,
                runtime_dir,
            )
            from .stress90_activation_permit import (
                Stress90TechnicalActivationAuthority,
            )

            manager = Stress90DirectionalPortfolioManager(
                config.directional,
                broker,
                risk_manager,
                policy_state_path=runtime_dir / "stress90_policy_state.json",
                seed_path=runtime_dir / "stress90_bootstrap_seed.json",
                oi_evidence_path=runtime_dir / "stress90_oi_evidence.json",
                execution_intent_path=runtime_dir / "stress90_execution_intent.json",
                **manager_common,
            )
            configure_order_journal = getattr(broker, "configure_order_submission_journal", None)
            if callable(configure_order_journal):
                configure_order_journal(
                    runtime_dir / "stress90_ctp_orders.json",
                    policy_id=manager.runtime_policy_id,
                    policy_definition_digest=manager.runtime_policy_definition_digest,
                    products_manifest_digest=manager.runtime_products_manifest_digest,
                )
            require_session_capability = getattr(
                broker,
                "require_stress90_session_startup_capability",
                None,
            )
            if not callable(require_session_capability):
                raise RuntimeError("Stress-90 Broker lacks the complete-session startup capability")
            require_session_capability()
            common["technical_activation_authority"] = Stress90TechnicalActivationAuthority(
                runtime_dir,
                account_registry_path=config.account_registry_path,
                account_continuity_mode=config.directional.account_continuity_mode,
            )
        else:  # validated config and direct-construction defense in depth
            raise ValueError(f"unsupported directional policy: {policy_name}")
        return DirectionalTradingEngine(
            broker,
            config.pairs,
            config.contracts,
            risk_manager,
            state_store,
            directional_manager=manager,
            **common,
        )

    return TradingEngine(
        broker,
        config.pairs,
        config.contracts,
        risk_manager,
        state_store,
        **common,
    )
