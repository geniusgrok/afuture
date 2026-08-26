from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from afuture.models import AccountSnapshot, ContractSpec, Tick

_CHINA = ZoneInfo("Asia/Shanghai")


def _account(equity: float = 100_000.0) -> AccountSnapshot:
    return AccountSnapshot(
        balance=equity,
        equity=equity,
        available=equity,
        margin=0.0,
        realized_pnl=0.0,
        unrealized_pnl=0.0,
        trading_day="20260825",
    )


def _tick(symbol: str, product: str, price: float = 1_000.0) -> Tick:
    exchange = "SHFE" if product == "AG" else "DCE"
    return Tick(
        symbol=symbol,
        exchange=exchange,
        timestamp=datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA),
        bid_price=price,
        ask_price=price,
        last_price=price,
        bid_volume=1_000.0,
        ask_volume=1_000.0,
        trading_day="20260825",
        volume=10_000.0,
        open_interest=20_000.0,
    )


def test_freeze_primitive_blocks_only_entry_and_same_sign_add():
    from afuture.directional_stress90_planner import freeze_new_risk_target

    current = {"A2612": 2, "M2612": -2, "I2612": 2, "AG2612": 2, "C2612": 1}
    target = {
        "A2612": 4,
        "M2612": -1,
        "I2612": -3,
        "AG2702": 3,
        "Y2612": 2,
    }
    products = {
        "A2612": "A",
        "M2612": "M",
        "I2612": "I",
        "AG2612": "AG",
        "AG2702": "AG",
        "C2612": "C",
        "Y2612": "Y",
    }

    frozen = freeze_new_risk_target(
        current_lots=current,
        target_lots=target,
        symbol_products=products,
        triggered=True,
        lot_notionals={symbol: 1.0 for symbol in set(current) | set(target)},
    )

    assert frozen == {"A2612": 2, "AG2702": 2, "I2612": -3, "M2612": -1}
    assert (
        freeze_new_risk_target(
            current_lots=current,
            target_lots=target,
            symbol_products=products,
            triggered=False,
        )
        == target
    )


def test_freeze_requires_real_current_and_target_notionals_for_changed_same_sign_risk():
    from afuture.directional_stress90_planner import freeze_new_risk_target

    common = dict(
        current_lots={"A2612": 5},
        target_lots={"A2701": 5},
        symbol_products={"A2612": "A", "A2701": "A"},
        triggered=True,
    )

    # A missing current or replacement notional can never authorize the opening leg.
    assert (
        freeze_new_risk_target(
            **common,
            lot_notionals={"A2701": 20_000.0},
        )
        == {}
    )
    assert (
        freeze_new_risk_target(
            **common,
            lot_notionals={"A2612": 10_000.0},
        )
        == {}
    )


def test_freeze_keeps_exact_unchanged_incumbent_without_notional_but_allows_reduction():
    from afuture.directional_stress90_planner import freeze_new_risk_target

    assert freeze_new_risk_target(
        current_lots={"A2612": 5},
        target_lots={"A2612": 5},
        symbol_products={"A2612": "A"},
        triggered=True,
        lot_notionals={},
    ) == {"A2612": 5}
    assert freeze_new_risk_target(
        current_lots={"A2612": 5},
        target_lots={"A2612": 2, "A2701": 3},
        symbol_products={"A2612": "A", "A2701": "A"},
        triggered=True,
        lot_notionals={},
    ) == {"A2612": 2}


def test_persisted_transition_authorization_preserves_reversal_after_reduction_fill():
    from afuture.directional_stress90_planner import freeze_new_risk_target

    assert freeze_new_risk_target(
        current_lots={},
        target_lots={"I2612": -3},
        symbol_products={"I2612": "I"},
        triggered=True,
        authorized_transition_products={"I"},
    ) == {"I2612": -3}


def test_persisted_typed_roll_authorization_preserves_only_replacement_after_reduction():
    from afuture.directional_stress90_planner import build_stress90_rebalance_stages

    new_tick = _tick("A2701", "A", price=2_000.0)
    spec = ContractSpec("A2701", "DCE", 10.0, 1.0, 0.10, 0.10)
    stages = build_stress90_rebalance_stages(
        account=_account(),
        product_weights={"A": 2.0},
        product_ticks={"A": new_tick},
        specs={"A2701": spec},
        current_lots={},
        symbol_products={"A2701": "A"},
        max_contract_volume=35,
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
        max_daily_loss_ratio=0.05,
        margin_estimate_buffer=1.25,
        completed_returns=(),
        drawdown_reserve_freeze=True,
        concentration_freeze=False,
        persisted_margin_fitted_lots={"A2701": 10},
        persisted_freeze_authorized_lots={"A2701": 2},
        authorized_transition_kinds={"A": "same_product_roll"},
    )

    assert stages.final_frozen_lots == {"A2701": 2}
    assert stages.openings == {"A2701": 2}
    assert stages.action_categories == {"A2701": "same_product_roll"}


def test_persisted_transition_budget_survives_partial_fill_and_reprices_replacement():
    from afuture.directional_stress90_planner import freeze_new_risk_target

    common = dict(
        target_lots={"A2701": 2},
        symbol_products={"A2701": "A"},
        triggered=True,
        authorized_transition_kinds={"A": "same_product_roll"},
        authorized_transition_max_replacement_notionals={"A": 50_000.0},
        persisted_authorized_target_lots={"A2701": 2},
    )

    assert freeze_new_risk_target(
        **common,
        current_lots={"A2701": 1},
        lot_notionals={"A2701": 20_000.0},
    ) == {"A2701": 2}
    assert freeze_new_risk_target(
        **common,
        current_lots={},
        lot_notionals={"A2701": 40_000.0},
    ) == {"A2701": 1}


def test_roll_plus_add_freeze_caps_replacement_by_initial_incumbent_notional():
    from afuture.directional_stress90_planner import build_stress90_rebalance_stages

    old_tick = _tick("A2612", "A", price=1_000.0)
    new_tick = _tick("A2701", "A", price=2_000.0)
    specs = {
        "A2612": ContractSpec("A2612", "DCE", 10.0, 1.0, 0.10, 0.10),
        "A2701": ContractSpec("A2701", "DCE", 10.0, 1.0, 0.10, 0.10),
    }
    common = dict(
        account=_account(200_000.0),
        product_weights={"A": 1.0},
        product_ticks={"A": new_tick},
        specs=specs,
        incumbent_ticks={"A2612": old_tick},
        current_lots={"A2612": 5},
        symbol_products={"A2612": "A", "A2701": "A"},
        max_contract_volume=35,
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
        max_daily_loss_ratio=0.05,
        margin_estimate_buffer=1.25,
        completed_returns=(),
    )

    drawdown = build_stress90_rebalance_stages(
        **common,
        drawdown_reserve_freeze=True,
        concentration_freeze=False,
    )
    hhi = build_stress90_rebalance_stages(
        **common,
        drawdown_reserve_freeze=False,
        concentration_freeze=True,
    )

    # Five old lots are 50,000 notional; each new lot is 20,000, so at most two replace.
    for stages in (drawdown, hhi):
        assert stages.margin_fitted_lots == {"A2701": 10}
        assert stages.final_frozen_lots == {"A2701": 2}
        assert stages.reductions == {"A2612": -5}
        assert stages.openings == {}


def test_stress90_lot_stages_use_raw_1x_then_margin_then_independent_freezes():
    from afuture.directional_stress90_planner import build_stress90_rebalance_stages

    tick = _tick("A2612", "A")
    spec = ContractSpec("A2612", "DCE", 10.0, 1.0, 0.15, 0.15)
    common = dict(
        account=_account(),
        product_weights={"A": 2.0},
        product_ticks={"A": tick},
        specs={"A2612": spec},
        current_lots={"A2612": 10},
        symbol_products={"A2612": "A"},
        max_contract_volume=35,
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
        max_daily_loss_ratio=0.05,
        margin_estimate_buffer=1.25,
        completed_returns=(),
    )

    normal = build_stress90_rebalance_stages(
        **common,
        drawdown_reserve_freeze=False,
        concentration_freeze=False,
    )
    drawdown = build_stress90_rebalance_stages(
        **common,
        drawdown_reserve_freeze=True,
        concentration_freeze=False,
    )
    hhi = build_stress90_rebalance_stages(
        **common,
        drawdown_reserve_freeze=False,
        concentration_freeze=True,
    )

    assert normal.raw_integer_lots == {"A2612": 20}
    assert normal.margin_fitted_lots == {"A2612": 16}
    assert normal.final_frozen_lots == {"A2612": 16}
    assert normal.openings == {"A2612": 6}
    assert drawdown.drawdown_frozen_lots == {"A2612": 10}
    assert drawdown.hhi_frozen_lots == {"A2612": 10}
    assert drawdown.final_frozen_lots == {"A2612": 10}
    assert hhi.drawdown_frozen_lots == {"A2612": 16}
    assert hhi.hhi_frozen_lots == {"A2612": 10}


def test_stricter_commissioning_gross_and_daily_stop_can_only_reduce_sizing():
    from afuture.directional_stress90_planner import build_stress90_rebalance_stages

    tick = _tick("A2612", "A")
    spec = ContractSpec("A2612", "DCE", 10.0, 1.0, 0.15, 0.15)
    common = dict(
        account=_account(),
        product_weights={"A": 2.0},
        product_ticks={"A": tick},
        specs={"A2612": spec},
        current_lots={},
        symbol_products={"A2612": "A"},
        max_contract_volume=35,
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
        margin_estimate_buffer=1.25,
        completed_returns=(),
        drawdown_reserve_freeze=False,
        concentration_freeze=False,
    )

    historical = build_stress90_rebalance_stages(
        **common,
        max_gross_leverage=2.0,
        max_daily_loss_ratio=0.05,
    )
    stricter_daily_stop = build_stress90_rebalance_stages(
        **common,
        max_gross_leverage=2.0,
        max_daily_loss_ratio=0.02,
    )
    stricter_gross = build_stress90_rebalance_stages(
        **common,
        max_gross_leverage=1.0,
        max_daily_loss_ratio=0.05,
    )

    assert historical.margin_fitted_lots == {"A2612": 16}
    assert stricter_daily_stop.margin_fitted_lots == historical.margin_fitted_lots
    assert stricter_gross.margin_fitted_lots == {"A2612": 10}


def test_reversal_is_reduction_first_then_authorized_opening_even_under_both_freezes():
    from afuture.directional_stress90_planner import build_stress90_rebalance_stages

    tick = _tick("A2612", "A")
    spec = ContractSpec("A2612", "DCE", 10.0, 1.0, 0.10, 0.10)
    common = dict(
        account=_account(),
        product_weights={"A": -1.0},
        product_ticks={"A": tick},
        specs={"A2612": spec},
        symbol_products={"A2612": "A"},
        max_contract_volume=35,
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
        max_daily_loss_ratio=0.05,
        margin_estimate_buffer=1.25,
        completed_returns=(),
        drawdown_reserve_freeze=True,
        concentration_freeze=True,
        authorized_transition_products={"A"},
    )
    first = build_stress90_rebalance_stages(**common, current_lots={"A2612": 2})
    second = build_stress90_rebalance_stages(**common, current_lots={})

    assert first.reductions == {"A2612": -2}
    assert first.openings == {}
    assert first.action_categories["A2612"] == "reversal"
    assert second.reductions == {}
    assert second.openings == {"A2612": -10}
    assert second.action_categories["A2612"] == "reversal_open"


def test_cross_contract_reversal_source_and_target_keep_typed_action_categories():
    from afuture.directional_stress90_planner import build_stress90_rebalance_stages

    new_tick = _tick("A2701", "A")
    stages = build_stress90_rebalance_stages(
        account=_account(),
        product_weights={},
        product_ticks={"A": new_tick},
        specs={"A2701": ContractSpec("A2701", "DCE", 10.0, 1.0, 0.10, 0.10)},
        current_lots={"A2612": 2},
        symbol_products={"A2612": "A", "A2701": "A"},
        max_contract_volume=35,
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
        max_daily_loss_ratio=0.05,
        margin_estimate_buffer=1.25,
        completed_returns=(),
        drawdown_reserve_freeze=False,
        concentration_freeze=False,
        persisted_margin_fitted_lots={"A2701": -5},
        authorized_transition_kinds={"A": "reversal_open"},
    )

    assert stages.action_categories == {
        "A2612": "reversal",
        "A2701": "reversal_open",
    }


def test_missed_entry_window_blocks_add_but_not_authorized_reversal():
    from afuture.directional_stress90_planner import build_stress90_rebalance_stages

    tick = _tick("A2612", "A")
    spec = ContractSpec("A2612", "DCE", 10.0, 1.0, 0.10, 0.10)
    blocked = build_stress90_rebalance_stages(
        account=_account(),
        product_weights={"A": 1.0},
        product_ticks={"A": tick},
        specs={"A2612": spec},
        current_lots={"A2612": 2},
        symbol_products={"A2612": "A"},
        max_contract_volume=35,
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
        max_daily_loss_ratio=0.05,
        margin_estimate_buffer=1.25,
        completed_returns=(),
        drawdown_reserve_freeze=False,
        concentration_freeze=False,
        entry_blocked_products={"A"},
    )
    reversal = build_stress90_rebalance_stages(
        account=_account(),
        product_weights={"A": -1.0},
        product_ticks={"A": tick},
        specs={"A2612": spec},
        current_lots={},
        symbol_products={"A2612": "A"},
        max_contract_volume=35,
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
        max_daily_loss_ratio=0.05,
        margin_estimate_buffer=1.25,
        completed_returns=(),
        drawdown_reserve_freeze=False,
        concentration_freeze=False,
        entry_blocked_products={"A"},
        authorized_transition_products={"A"},
    )

    assert blocked.final_frozen_lots == {"A2612": 2}
    assert blocked.openings == {}
    assert reversal.openings == {"A2612": -10}


def test_live_planner_and_production_acceptance_share_every_lot_stage():
    from afuture.directional_acceptance import (
        ProductionMechanicsConfig,
        Stress90ProductionAcceptance,
    )
    from afuture.directional_stress90_planner import build_stress90_rebalance_stages

    account = _account(500_000.0)
    ticks = {"A": _tick("A2612", "A", 2_000.0), "M": _tick("M2612", "M", 3_000.0)}
    specs = {
        "A2612": ContractSpec("A2612", "DCE", 10.0, 1.0, 0.12, 0.14),
        "M2612": ContractSpec("M2612", "DCE", 10.0, 1.0, 0.11, 0.13),
    }
    direct = build_stress90_rebalance_stages(
        account=account,
        product_weights={"A": 1.2, "M": -0.8},
        product_ticks=ticks,
        specs=specs,
        current_lots={"A2612": 5, "M2612": -10},
        symbol_products={"A2612": "A", "M2612": "M"},
        max_contract_volume=35,
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
        max_daily_loss_ratio=0.05,
        margin_estimate_buffer=1.25,
        completed_returns=(-0.01, 0.01),
        drawdown_reserve_freeze=False,
        concentration_freeze=True,
    )
    acceptance = Stress90ProductionAcceptance(
        ProductionMechanicsConfig(initial_capital=500_000.0)
    ).target_lot_stages(
        equity=account.equity,
        product_weights={"A": 1.2, "M": -0.8},
        product_open_prices={"A": 2_000.0, "M": 3_000.0},
        selected_symbols={"A": "A2612", "M": "M2612"},
        live_margin_rates={"A2612": (0.12, 0.14), "M2612": (0.11, 0.13)},
        current_lots={"A2612": 5, "M2612": -10},
        completed_returns=(-0.01, 0.01),
        drawdown_reserve_freeze=False,
        concentration_freeze=True,
    )

    assert acceptance == direct


def test_persisted_margin_target_is_recapped_after_account_change():
    from afuture.directional_stress90_planner import build_stress90_rebalance_stages

    tick = _tick("A2612", "A")
    spec = ContractSpec("A2612", "DCE", 10.0, 1.0, 0.10, 0.10)
    stages = build_stress90_rebalance_stages(
        account=_account(10_000.0),
        product_weights={"A": 2.0},
        product_ticks={"A": tick},
        specs={"A2612": spec},
        current_lots={"A2612": 4},
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
        persisted_margin_fitted_lots={"A2612": 10},
    )

    assert stages.margin_fitted_lots == {"A2612": 2}
    assert stages.final_frozen_lots == {"A2612": 2}
    assert stages.reductions == {"A2612": -2}
    assert stages.openings == {}


def test_persisted_margin_target_is_reclamped_to_current_hard_contract_cap():
    """A restart with 35 persisted lots must reduce to a newly tightened cap of 10."""

    from afuture.directional_stress90_planner import build_stress90_rebalance_stages

    tick = _tick("A2612", "A", price=100.0)
    spec = ContractSpec("A2612", "DCE", 10.0, 1.0, 0.01, 0.01)
    stages = build_stress90_rebalance_stages(
        account=_account(1_000_000.0),
        product_weights={"A": 2.0},
        product_ticks={"A": tick},
        specs={"A2612": spec},
        current_lots={"A2612": 35},
        symbol_products={"A2612": "A"},
        max_contract_volume=10,
        max_gross_leverage=2.0,
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
        max_daily_loss_ratio=0.05,
        margin_estimate_buffer=1.25,
        completed_returns=(),
        drawdown_reserve_freeze=False,
        concentration_freeze=False,
        persisted_margin_fitted_lots={"A2612": 35},
    )

    assert stages.margin_fitted_lots == {"A2612": 10}
    assert stages.final_frozen_lots == {"A2612": 10}
    assert stages.reductions == {"A2612": -25}
    assert stages.openings == {}


def test_shared_planner_rejects_contract_cap_above_stress90_hard_limit():
    from afuture.directional_stress90_planner import build_stress90_rebalance_stages

    tick = _tick("A2612", "A", price=100.0)
    spec = ContractSpec("A2612", "DCE", 10.0, 1.0, 0.01, 0.01)
    with pytest.raises(ValueError, match="35"):
        build_stress90_rebalance_stages(
            account=_account(1_000_000.0),
            product_weights={"A": 2.0},
            product_ticks={"A": tick},
            specs={"A2612": spec},
            current_lots={},
            symbol_products={"A2612": "A"},
            max_contract_volume=36,
            max_gross_leverage=2.0,
            max_margin_ratio=0.35,
            min_available_ratio=0.25,
            max_daily_loss_ratio=0.05,
            margin_estimate_buffer=1.25,
            completed_returns=(),
            drawdown_reserve_freeze=False,
            concentration_freeze=False,
        )


def test_unavailable_same_direction_candidate_keeps_incumbent_without_add_or_roll():
    from afuture.directional_stress90_planner import build_stress90_rebalance_stages

    stages = build_stress90_rebalance_stages(
        account=_account(),
        product_weights={"A": 1.0},
        product_ticks={},
        specs={},
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
        unavailable_products={"A"},
    )

    assert stages.margin_fitted_lots == {"A2612": 5}
    assert stages.final_frozen_lots == {"A2612": 5}
    assert stages.reductions == {}
    assert stages.openings == {}


def test_unavailable_incumbent_is_restored_after_residual_fit_without_target_evidence():
    """The second persisted-intent pass must not price a deliberately unavailable incumbent."""

    from afuture.directional_stress90_planner import build_stress90_rebalance_stages

    stages = build_stress90_rebalance_stages(
        account=_account(),
        product_weights={},
        product_ticks={"M": _tick("M2612", "M")},
        specs={"M2612": ContractSpec("M2612", "DCE", 10.0, 1.0, 0.10, 0.10)},
        current_lots={"A2612": 5},
        symbol_products={"A2612": "A", "M2612": "M"},
        max_contract_volume=35,
        max_gross_leverage=2.0,
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
        max_daily_loss_ratio=0.05,
        margin_estimate_buffer=1.25,
        completed_returns=(),
        drawdown_reserve_freeze=False,
        concentration_freeze=False,
        unavailable_products={"A"},
        persisted_margin_fitted_lots={"A2612": 5, "M2612": 10},
    )

    assert stages.margin_fitted_lots == {"A2612": 5}
    assert stages.final_frozen_lots == {"A2612": 5}
    assert stages.reductions == {}
    assert stages.openings == {}


def test_unavailable_incumbent_exit_and_reversal_close_are_never_suppressed():
    from afuture.directional_stress90_planner import build_stress90_rebalance_stages

    common = dict(
        account=_account(),
        product_weights={},
        product_ticks={},
        specs={},
        current_lots={"A2612": 5},
        symbol_products={"A2612": "A", "A2701": "A"},
        max_contract_volume=35,
        max_gross_leverage=2.0,
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
        max_daily_loss_ratio=0.05,
        margin_estimate_buffer=1.25,
        completed_returns=(),
        drawdown_reserve_freeze=True,
        concentration_freeze=True,
        unavailable_products={"A"},
    )
    exit_stages = build_stress90_rebalance_stages(
        **common,
        persisted_margin_fitted_lots={},
    )
    reversal_stages = build_stress90_rebalance_stages(
        **common,
        persisted_margin_fitted_lots={"A2701": -5},
        authorized_transition_kinds={"A": "reversal_open"},
    )

    assert exit_stages.final_frozen_lots == {}
    assert exit_stages.reductions == {"A2612": -5}
    assert reversal_stages.final_frozen_lots == {}
    assert reversal_stages.reductions == {"A2612": -5}
    assert reversal_stages.openings == {}
    assert reversal_stages.action_categories == {"A2612": "reversal"}
