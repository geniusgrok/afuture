from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

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
    )

    assert frozen == {"A2612": 2, "AG2702": 3, "I2612": -3, "M2612": -1}
    assert (
        freeze_new_risk_target(
            current_lots=current,
            target_lots=target,
            symbol_products=products,
            triggered=False,
        )
        == target
    )


def test_persisted_transition_authorization_preserves_reversal_after_reduction_fill():
    from afuture.directional_stress90_planner import freeze_new_risk_target

    assert freeze_new_risk_target(
        current_lots={},
        target_lots={"I2612": -3},
        symbol_products={"I2612": "I"},
        triggered=True,
        authorized_transition_products={"I"},
    ) == {"I2612": -3}


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


def test_persisted_margin_target_is_reused_after_partial_fill_and_account_change():
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
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
        max_daily_loss_ratio=0.05,
        margin_estimate_buffer=1.25,
        completed_returns=(),
        drawdown_reserve_freeze=False,
        concentration_freeze=False,
        persisted_margin_fitted_lots={"A2612": 10},
    )

    assert stages.margin_fitted_lots == {"A2612": 10}
    assert stages.final_frozen_lots == {"A2612": 10}
    assert stages.openings == {"A2612": 6}
