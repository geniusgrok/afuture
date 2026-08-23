from datetime import datetime, timezone

import pytest

from afuture.directional import (
    fit_target_lots_to_margin_budget,
    margin_sizing_share,
)
from afuture.directional_acceptance import ProductionMechanicsConfig
from afuture.directional_robustness import MarginAwareDirectionalProductionAcceptance
from afuture.models import AccountSnapshot, ContractSpec, Tick


def _margin(lots, per_lot):
    return sum(abs(int(volume)) * float(per_lot[symbol]) for symbol, volume in lots.items())


def test_margin_budget_fits_stress_target_without_relaxing_hard_cap():
    # 20 lots * 1,875 margin/lot = 37,500, but the hard account budget is 35,000.
    requested = {"A2609": 20}
    per_lot = {"A2609": 1000.0 * 10.0 * 0.15 * 1.25}

    fitted = fit_target_lots_to_margin_budget(
        requested,
        per_lot,
        margin_budget=35000.0,
    )

    assert fitted == {"A2609": 18}
    assert _margin(fitted, per_lot) <= 35000.0
    assert abs(fitted["A2609"]) <= abs(requested["A2609"])


def test_margin_sizing_share_reserves_full_existing_daily_loss_budget():
    # The 35% hard halt is unchanged. Keep the configured 5% daily-loss budget as
    # absolute equity headroom between normal target sizing and the hard margin gate.
    assert margin_sizing_share(
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
        max_daily_loss_ratio=0.05,
    ) == pytest.approx(0.30)


def test_margin_budget_preserves_sign_and_allocates_integer_residual_deterministically():
    requested = {"A2609": 5, "M2609": -5}
    per_lot = {"A2609": 100.0, "M2609": 200.0}

    fitted = fit_target_lots_to_margin_budget(
        requested,
        per_lot,
        margin_budget=1000.0,
    )

    assert fitted == {"A2609": 4, "M2609": -3}
    assert fitted["A2609"] > 0
    assert fitted["M2609"] < 0
    assert _margin(fitted, per_lot) <= 1000.0
    for symbol in requested:
        assert abs(fitted.get(symbol, 0)) <= abs(requested[symbol])


def test_margin_budget_fails_closed_when_positive_target_has_no_margin_estimate():
    with pytest.raises(ValueError, match="missing positive per-lot margin: A2609"):
        fit_target_lots_to_margin_budget(
            {"A2609": 1},
            {},
            margin_budget=35000.0,
        )


def test_margin_budget_rejects_invalid_budget():
    with pytest.raises(ValueError, match="margin_budget cannot be negative"):
        fit_target_lots_to_margin_budget(
            {"A2609": 1},
            {"A2609": 100.0},
            margin_budget=-1.0,
        )


def test_acceptance_target_lots_leave_full_daily_loss_margin_headroom_before_hard_gate():
    sim = MarginAwareDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=100000.0,
            margin_rate_proxy=0.15,
            margin_estimate_buffer=1.25,
            max_margin_ratio=0.35,
            min_available_ratio=0.25,
            max_contract_volume=100,
            max_daily_loss_ratio=0.05,
        )
    )

    target = sim.target_lots(
        equity=100000.0,
        product_weights={"A": 2.0},
        product_open_prices={"A": 1000.0},
        selected_symbols={"A": "A2609"},
    )

    assert target == {"A2609": 16}
    allowed, reason, estimated = sim.check_opening_batch(
        equity=100000.0,
        current_margin=0.0,
        current_lots={},
        openings=target,
        open_prices={"A2609": 1000.0},
    )
    assert allowed
    assert reason == ""
    assert estimated == 30000.0
    assert estimated / 100000.0 == pytest.approx(0.30)


def test_live_target_builder_uses_side_specific_margin_and_full_daily_loss_headroom():
    from afuture.directional import build_margin_aware_target_lots

    account = AccountSnapshot(
        balance=100000.0,
        equity=100000.0,
        available=100000.0,
        margin=0.0,
        realized_pnl=0.0,
        unrealized_pnl=0.0,
        trading_day="20260825",
    )
    tick = Tick(
        symbol="A2609",
        exchange="DCE",
        timestamp=datetime(2026, 8, 25, tzinfo=timezone.utc),
        bid_price=999.0,
        ask_price=1001.0,
        last_price=1000.0,
        bid_volume=1000.0,
        ask_volume=1000.0,
        trading_day="20260825",
        volume=5000.0,
        open_interest=30000.0,
    )
    spec = ContractSpec(
        symbol="A2609",
        exchange="DCE",
        multiplier=10.0,
        price_tick=1.0,
        margin_rate_long=0.15,
        margin_rate_short=0.20,
    )

    common = dict(
        max_contract_volume=100,
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
        max_daily_loss_ratio=0.05,
        margin_estimate_buffer=1.25,
    )
    long_target = build_margin_aware_target_lots(
        account,
        {"A": 2.0},
        {"A": tick},
        {"A2609": spec},
        **common,
    )
    short_target = build_margin_aware_target_lots(
        account,
        {"A": -2.0},
        {"A": tick},
        {"A2609": spec},
        **common,
    )

    assert long_target == {"A2609": 16}
    assert short_target == {"A2609": -12}
