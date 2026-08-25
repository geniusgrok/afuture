from types import SimpleNamespace

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
    pass


def _config(enabled: bool, *, policy: str = ""):
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
    )


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


def test_cli_engine_builder_routes_explicit_stress90_without_point25_wrapper(tmp_path):
    from afuture.directional_stress90_runtime import Stress90DirectionalPortfolioManager

    engine = _build_cli_engine(
        _config(True, policy="stress90"),
        _Broker(),
        StateStore(tmp_path / "directional.json"),
    )

    assert isinstance(engine, DirectionalTradingEngine)
    assert type(engine.directional_manager) is Stress90DirectionalPortfolioManager
    assert (
        engine.directional_manager.policy_risk_response_mode
        is DirectionalRiskResponseMode.FREEZE_NEW_RISK
    )
    assert not isinstance(engine.directional_manager.policy, DirectionalRiskScaledPolicy)


def test_cli_engine_builder_preserves_plain_trading_engine_when_directional_disabled(tmp_path):
    engine = _build_cli_engine(
        _config(False),
        _Broker(),
        StateStore(tmp_path / "plain.json"),
    )
    assert type(engine) is TradingEngine
