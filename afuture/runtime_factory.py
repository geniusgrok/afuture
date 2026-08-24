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
        from .execution_aligned_runtime import (
            ExecutionAlignedDirectionalPortfolioManager,
        )

        activity_path = Path(state_store.path).with_name("directional_activity.json")
        manager = ExecutionAlignedDirectionalPortfolioManager(
            config.directional,
            broker,
            risk_manager,
            aggressive_ticks=config.aggressive_ticks,
            metadata_timeout_seconds=config.metadata_timeout_seconds,
            static_specs=config.contracts,
            activity_store_path=activity_path,
            quality_recorder=quality_recorder,
        )
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
