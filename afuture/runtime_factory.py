"""Construct the single runtime engine selected by the validated account strategy mode."""

from __future__ import annotations

from pathlib import Path

from .engine import TradingEngine
from .risk import RiskManager


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

            runtime_dir = Path(state_store.path).parent
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
