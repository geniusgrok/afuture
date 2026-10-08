from dataclasses import replace
from types import SimpleNamespace

import pytest

from afuture.directional import DirectionalConfig
from afuture.directional_engine import DirectionalTradingEngine
from afuture.directional_risk import DirectionalRiskResponseMode, DirectionalRiskScaledPolicy
from afuture.engine import TradingEngine
from afuture.execution_aligned_policy import FROZEN_PRODUCTS
from afuture.execution_aligned_runtime import ExecutionAlignedDirectionalPortfolioManager
from afuture.risk import RiskConfig
from afuture.runtime_factory import build_runtime_engine
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


def _config(
    enabled: bool,
    *,
    policy: str = "execution_aligned",
    account_registry_path: str = "",
):
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
        mode="replay",
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


def test_cli_engine_builder_routes_explicit_stress90_without_point25_wrapper(tmp_path):
    from afuture.directional_stress90_runtime import Stress90DirectionalPortfolioManager

    broker = _Broker()
    registry_path = tmp_path / "machine" / "registry.json"
    _write_bound_stress90_policy(tmp_path, registry_path)
    engine = build_runtime_engine(
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
        build_runtime_engine(
            _config(
                True,
                policy="stress90",
                account_registry_path=str(registry_path),
            ),
            _Broker(),
            StateStore(runtime_b / "state.json"),
        )


def test_cli_engine_builder_preserves_plain_trading_engine_when_directional_disabled(tmp_path):
    engine = build_runtime_engine(
        _config(False),
        _Broker(),
        StateStore(tmp_path / "plain.json"),
    )
    assert type(engine) is TradingEngine


def test_cli_engine_builder_routes_directional_mode_through_exact_execution_aligned_manager(
    tmp_path,
    monkeypatch,
):
    engine = build_runtime_engine(
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

    # The factory-selected engine scales only its completed account return history.
    wrapped = engine.directional_manager.policy
    monkeypatch.setattr(
        type(wrapped.policy), "target_weights", lambda *args, **kwargs: {"A": 1.2, "CU": -0.8}
    )
    assert wrapped.target_weights() == {"A": 1.2, "CU": -0.8}
    engine.state.recent_daily_returns = [-0.02]
    assert wrapped.target_weights() == {"A": 0.3, "CU": -0.2}
