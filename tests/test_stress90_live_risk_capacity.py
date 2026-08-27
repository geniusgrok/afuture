from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from afuture.directional import DirectionalConfig
from afuture.models import AccountSnapshot, ContractSpec, FeeSpec, Tick
from afuture.risk import RiskConfig

_CHINA = ZoneInfo("Asia/Shanghai")


def _account(equity: float = 100_000.0) -> AccountSnapshot:
    return AccountSnapshot(
        balance=equity,
        equity=equity,
        available=equity,
        margin=0.0,
        realized_pnl=0.0,
        unrealized_pnl=0.0,
        trading_day="20260827",
    )


def _tick(symbol: str = "A2612", price: float = 1_000.0) -> Tick:
    return Tick(
        symbol=symbol,
        exchange="DCE",
        timestamp=datetime(2026, 8, 27, 21, 1, tzinfo=_CHINA),
        bid_price=price - 0.5,
        ask_price=price + 0.5,
        last_price=price,
        bid_volume=1000,
        ask_volume=1000,
        trading_day="20260827",
        volume=10000,
        open_interest=20000,
    )


def _spec() -> ContractSpec:
    return ContractSpec(
        "A2612",
        "DCE",
        10.0,
        1.0,
        0.10,
        0.12,
        FeeSpec(open_fixed=2.0, close_fixed=2.0, close_today_fixed=3.0),
    )


def test_live_risk_scale_default_and_validation():
    assert DirectionalConfig().live_risk_scale == 1.0
    for value in (0.0, -0.1, float("nan"), float("inf"), 1.000001):
        with pytest.raises(ValueError):
            DirectionalConfig(live_risk_scale=value).validate()
    DirectionalConfig(live_risk_scale=0.05).validate()


def test_scale_helper_is_monotone_and_direction_preserving():
    from afuture.stress90_risk_overlay import scale_stress90_product_weights

    raw = {"A": 0.8, "M": -0.5, "I": 0.0}
    scaled = scale_stress90_product_weights(raw, 0.05)
    assert scaled == {"A": pytest.approx(0.04), "M": pytest.approx(-0.025), "I": 0.0}
    for product, value in raw.items():
        assert abs(scaled[product]) <= abs(value)
        if value:
            assert (scaled[product] > 0) == (value > 0)


def test_stress90_scale_is_before_integer_and_margin_fit_and_can_zero_targets():
    from afuture.directional_stress90_planner import build_stress90_rebalance_stages

    common = dict(
        account=_account(),
        product_weights={"A": 1.0},
        product_ticks={"A": _tick()},
        specs={"A2612": _spec()},
        current_lots={},
        symbol_products={"A2612": "A"},
        max_contract_volume=35,
        max_gross_leverage=2.0,
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
        max_daily_loss_ratio=0.05,
        margin_estimate_buffer=1.25,
        completed_returns=(),
        drawdown_reserve_freeze=False,
        concentration_freeze=False,
    )
    baseline = build_stress90_rebalance_stages(**common)
    scaled = build_stress90_rebalance_stages(**common, live_risk_scale=0.10)
    tiny = build_stress90_rebalance_stages(**common, live_risk_scale=0.001)
    assert baseline.raw_integer_lots == baseline.scaled_integer_lots
    assert baseline.margin_fitted_lots == {"A2612": 10}
    assert scaled.raw_integer_lots == {"A2612": 10}
    assert scaled.scaled_integer_lots == {"A2612": 1}
    assert scaled.margin_fitted_lots == {"A2612": 1}
    assert tiny.scaled_integer_lots == {}
    assert tiny.final_frozen_lots == {}


def test_scale_never_suppresses_reduction_or_reversal_close():
    from afuture.directional_stress90_planner import build_stress90_rebalance_stages

    stages = build_stress90_rebalance_stages(
        account=_account(),
        product_weights={"A": -1.0},
        product_ticks={"A": _tick()},
        specs={"A2612": _spec()},
        current_lots={"A2612": 5},
        symbol_products={"A2612": "A"},
        max_contract_volume=35,
        max_gross_leverage=2.0,
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
        max_daily_loss_ratio=0.05,
        margin_estimate_buffer=1.25,
        completed_returns=(),
        drawdown_reserve_freeze=False,
        concentration_freeze=False,
        live_risk_scale=0.001,
    )
    assert stages.reductions == {"A2612": -5}
    assert stages.openings == {}


def test_risk_overlay_digest_is_stable_sensitive_and_separate_from_policy_digest():
    from afuture.directional_stress90_policy import STRESS90_POLICY
    from afuture.stress90_risk_overlay import stress90_risk_overlay_digest

    directional = DirectionalConfig(enabled=True, policy="stress90", products=("A",))
    risk = RiskConfig()
    first = stress90_risk_overlay_digest(directional, risk)
    assert first == stress90_risk_overlay_digest(directional, risk)
    assert first != stress90_risk_overlay_digest(replace(directional, live_risk_scale=0.5), risk)
    assert first != stress90_risk_overlay_digest(
        directional, replace(risk, max_orders_per_minute=risk.max_orders_per_minute + 1)
    )
    assert first != STRESS90_POLICY.policy_definition_digest


def test_policy_identity_requires_matching_risk_overlay_without_changing_policy_digest():
    from afuture.directional_policy_activation import (
        POLICY_IDENTITY_STATE_KEY,
        require_directional_policy_identity,
    )
    from afuture.directional_stress90_policy import STRESS90_POLICY
    from afuture.state import RuntimeState

    marker = {
        "policy_id": STRESS90_POLICY.policy_id,
        "policy_definition_digest": STRESS90_POLICY.policy_definition_digest,
        "products_manifest_digest": STRESS90_POLICY.products_manifest_digest,
        "bootstrap_seed_digest": "a" * 64,
        "account_identity_digest": "b" * 64,
        "risk_overlay_digest": "c" * 64,
        "operator_reason": "test",
    }
    state = RuntimeState(strategy_states={POLICY_IDENTITY_STATE_KEY: marker})
    require_directional_policy_identity(
        state,
        policy_id=STRESS90_POLICY.policy_id,
        policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
        products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
        bootstrap_seed_digest="a" * 64,
        account_identity_digest="b" * 64,
        risk_overlay_digest="c" * 64,
    )
    with pytest.raises(RuntimeError, match="risk overlay"):
        require_directional_policy_identity(
            state,
            policy_id=STRESS90_POLICY.policy_id,
            policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
            products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
            bootstrap_seed_digest="a" * 64,
            account_identity_digest="b" * 64,
            risk_overlay_digest="d" * 64,
        )


def test_execution_intent_binds_risk_overlay_and_same_day_cannot_be_reinterpreted(tmp_path):
    from afuture.directional_stress90_execution import (
        Stress90ExecutionIntentIntegrityError,
        Stress90ExecutionIntentStore,
        prepare_stress90_execution_intent,
    )

    store = Stress90ExecutionIntentStore(tmp_path / "intent.json")
    kwargs = dict(
        target_trading_day="20260827",
        daily_decision_digest="1" * 64,
        account_identity_digest="2" * 64,
        account_epoch="3" * 64,
        current_lots={},
        margin_fitted_lots={"A2612": 1},
        freeze_authorized_lots={"A2612": 1},
        symbol_products={"A2612": "A"},
        lot_notionals={"A2612": 10_000.0},
    )
    intent = prepare_stress90_execution_intent(store, risk_overlay_digest="4" * 64, **kwargs)
    assert intent.risk_overlay_digest == "4" * 64
    with pytest.raises(Stress90ExecutionIntentIntegrityError, match="risk overlay"):
        prepare_stress90_execution_intent(store, risk_overlay_digest="5" * 64, **kwargs)


def test_cost_preview_can_require_real_commission_evidence():
    from afuture.operations import estimate_stress90_contract_cost

    with pytest.raises(ValueError, match="commission evidence"):
        estimate_stress90_contract_cost(
            _tick(),
            replace(_spec(), fee=FeeSpec()),
            require_commission_evidence=True,
        )
    row = estimate_stress90_contract_cost(_tick(), _spec(), require_commission_evidence=True)
    assert row["historical_hurdle_bps"] == 15.0
    assert row["minimum_reasonable_one_way_bps"] > 0.0


def test_capacity_payload_is_read_only_projection_of_doctor_facts():
    from afuture.operations import OperationalReport
    from afuture.stress90_capacity import build_stress90_capacity_payload

    report = OperationalReport()
    report.add("stress90_risk_overlay_identity", True, "ok")
    report.facts["stress90"] = {
        "risk_overlay_digest": "a" * 64,
        "configured_live_risk_scale": 0.05,
        "raw_product_weights": {"A": 1.0},
        "scaled_product_weights": {"A": 0.05},
        "raw_target_gross": 1.0,
        "scaled_target_gross": 0.05,
        "selected_contracts": {"A": "A2612"},
        "contract_capacity": {"A": {"symbol": "A2612"}},
        "integer_target_stages": {
            "raw_integer_lots": {"A2612": 10},
            "scaled_integer_lots": {},
            "margin_fitted_lots": {},
            "drawdown_frozen_lots": {},
            "hhi_frozen_lots": {},
            "final_frozen_lots": {},
            "reductions": {},
            "openings": {},
        },
        "nonzero_product_count": 0,
        "largest_product_absolute_gross_share": 0.0,
        "product_hhi": 0.0,
        "integer_tracking_error": 0.05,
        "estimated_margin_ratio": 0.0,
        "estimated_available_ratio": 1.0,
        "contract_cap_hits": [],
        "clipped_products": {"integer": ["A"]},
        "live_costs": {"A2612": {"historical_15bp_compatible": True}},
        "risk_manager_preview": {"allowed": True, "reason": ""},
        "execution_chain_commissioning_only": True,
        "portfolio_representation_warning": "scaled target rounds to zero",
    }
    payload = build_stress90_capacity_payload(
        report,
        account_identity_digest="b" * 64,
        ctp_trading_day="20260827",
    )
    assert payload["orders_sent"] == 0
    assert payload["cancels_sent"] == 0
    assert payload["risk_overlay_digest"] == "a" * 64
    assert payload["raw_lots"] == {"A2612": 10}
    assert payload["scaled_lots"] == {}


def test_capacity_command_is_present_and_has_required_arguments():
    from afuture.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "stress90-capacity-report",
            "--config",
            "x.toml",
            "--confirm-live",
            "--output",
            "out.json",
        ]
    )
    assert args.command == "stress90-capacity-report"
    assert args.confirm_live is True
    assert args.output == "out.json"
