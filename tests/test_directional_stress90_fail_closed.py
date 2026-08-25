from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from afuture.models import ContractPosition

_CHINA = ZoneInfo("Asia/Shanghai")


def _manager(tmp_path: Path, positions: list[ContractPosition]):
    from afuture.directional import DirectionalConfig
    from afuture.directional_activity import ContractActivity, DirectionalActivitySnapshot
    from afuture.directional_stress90_policy import STRESS90_POLICY, Stress90CandidateState
    from afuture.directional_stress90_runtime import Stress90DirectionalPortfolioManager
    from afuture.directional_stress90_state import (
        Stress90BootstrapSeed,
        Stress90PolicyState,
        Stress90PolicyStateStore,
        Stress90SeedStore,
    )
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.risk import RiskConfig, RiskManager

    candidate = replace(
        Stress90CandidateState.initial(STRESS90_POLICY),
        last_completed_target_day="20260824",
        last_decision_digest="a" * 64,
    )
    seed = Stress90BootstrapSeed.from_candidate_state(
        candidate,
        bootstrap_source_manifest={"fixed": "1" * 64},
        bootstrap_through_day="20260824",
        last_completed_input_day="20260823",
    )
    seed_path = tmp_path / "stress90_bootstrap_seed.json"
    state_path = tmp_path / "stress90_policy_state.json"
    Stress90SeedStore(seed_path).save_new(seed)
    Stress90PolicyStateStore(state_path).save(Stress90PolicyState.from_seed(seed))

    class Broker:
        def __init__(self):
            self.orders = []

        def is_ready(self):
            return True

        def get_active_orders(self):
            return []

        def get_positions(self):
            return list(positions)

        def get_trading_day(self):
            return "20260825"

        def send_order(self, request):
            self.orders.append(request)
            return f"order-{len(self.orders)}"

    broker = Broker()
    manager = Stress90DirectionalPortfolioManager(
        DirectionalConfig(enabled=True, policy="stress90", products=FROZEN_PRODUCTS),
        broker,
        RiskManager(RiskConfig(margin_estimate_buffer=1.25)),
        policy_state_path=state_path,
        seed_path=seed_path,
        oi_evidence_path=tmp_path / "missing_oi.json",
        activity_tracker=SimpleNamespace(
            completed_snapshot=DirectionalActivitySnapshot(
                "20260824",
                {
                    "A2612": ContractActivity(
                        "A2612",
                        "DCE",
                        "A",
                        "20260824",
                        10_000.0,
                        20_000.0,
                        datetime(2026, 8, 24, 14, 59, tzinfo=_CHINA),
                    )
                },
            )
        ),
    )
    manager._initialized = True
    return manager, broker, state_path


def test_missing_ohlc_or_oi_sends_zero_orders_and_is_risk_off_only_when_exposed(tmp_path: Path):
    flat, flat_broker, _ = _manager(tmp_path / "flat", [])
    flat_result = flat.maybe_rebalance(datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA))
    assert flat_result.action == "reject"
    assert "OHLC" in flat_result.reason
    assert flat_broker.orders == []

    position = ContractPosition("A2612", "DCE", long_yesterday=2, long_price=100.0)
    exposed, exposed_broker, _ = _manager(tmp_path / "exposed", [position])
    exposed_result = exposed.maybe_rebalance(datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA))
    assert exposed_result.action == "risk_off"
    assert "OHLC" in exposed_result.reason
    assert exposed_broker.orders == []


def test_invalid_policy_state_and_invalid_ctp_day_raise_for_engine_halt(tmp_path: Path):
    manager, _broker, state_path = _manager(tmp_path / "state", [])
    state_path.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(RuntimeError, match="state JSON"):
        manager.maybe_rebalance(datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA))

    manager, broker, _ = _manager(tmp_path / "day", [])
    broker.get_trading_day = lambda: "2026-08-25"
    with pytest.raises(RuntimeError, match="current CTP trading day"):
        manager.maybe_rebalance(datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA))


def test_active_orders_wait_before_any_candidate_or_broker_mutation(tmp_path: Path):
    manager, broker, _ = _manager(tmp_path, [])
    broker.get_active_orders = lambda: [object()]

    result = manager.maybe_rebalance(datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA))

    assert result.action == "wait"
    assert broker.orders == []
