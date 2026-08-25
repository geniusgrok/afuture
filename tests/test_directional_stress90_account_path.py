import random
from dataclasses import replace
from datetime import date, timedelta

import pytest


def _state():
    from afuture.directional_stress90_policy import (
        STRESS90_POLICY,
        Stress90CandidateState,
    )
    from afuture.directional_stress90_state import (
        Stress90BootstrapSeed,
        Stress90PolicyState,
    )

    candidate = replace(
        Stress90CandidateState.initial(STRESS90_POLICY),
        last_completed_target_day="20260824",
        last_decision_digest="a" * 64,
        completed_concentrations=(0.5,),
    )
    sources = {
        "broad_daily_universe.csv": "1" * 64,
        "return_target_specific_contracts.csv": "2" * 64,
        "execution_aligned_weights.csv": "3" * 64,
        "prior_two_year_broad_60m.csv": "4" * 64,
        "two_year_broad_60m.csv": "5" * 64,
    }
    seed = Stress90BootstrapSeed.from_candidate_state(
        candidate,
        bootstrap_source_manifest=sources,
        bootstrap_through_day="20260824",
        last_completed_input_day="20260821",
    )
    return Stress90PolicyState.from_seed(seed)


def test_sufficient_account_statistics_equal_full_path_after_every_completed_day():
    from afuture.directional_stress90_state import (
        completed_account_drawdown,
        record_completed_account_day,
    )

    rng = random.Random(9025)
    returns = [0.01, -0.02, 0.03, -0.04, *[rng.uniform(-0.05, 0.05) for _ in range(500)]]
    state = _state()
    wealth = 1.0
    high_watermark = 1.0
    day = date(2026, 8, 25)
    for offset, daily_return in enumerate(returns):
        trading_day = (day + timedelta(days=offset)).strftime("%Y%m%d")
        state = record_completed_account_day(state, trading_day, daily_return)
        wealth *= 1.0 + daily_return
        high_watermark = max(high_watermark, wealth)

        assert state.completed_account_wealth == wealth
        assert state.completed_account_high_watermark == high_watermark
        assert completed_account_drawdown(state) == 1.0 - wealth / high_watermark
    assert state.recent_daily_returns_for_adaptive_margin == tuple(returns[-2:])
    assert state.live_inception_day == "20260825"


def test_drawdown_reserve_triggers_at_exact_25_percent_completed_boundary_only():
    from afuture.directional_stress90_state import (
        drawdown_reserve_triggered_from_state,
        record_completed_account_day,
    )

    exact = record_completed_account_day(_state(), "20260825", -0.25)
    below = record_completed_account_day(_state(), "20260825", -0.249999999)

    assert drawdown_reserve_triggered_from_state(exact) is True
    assert drawdown_reserve_triggered_from_state(below) is False


def test_reserve_uses_only_recorded_completed_days_not_current_unfinished_pnl():
    from afuture.directional_stress90_state import (
        drawdown_reserve_triggered_from_state,
        record_completed_account_day,
    )

    state = record_completed_account_day(_state(), "20260825", -0.20)
    current_unfinished_return = -0.50

    assert current_unfinished_return < -0.25
    assert drawdown_reserve_triggered_from_state(state) is False


def test_completed_account_day_rejects_duplicate_backward_and_invalid_returns():
    from afuture.directional_stress90_state import (
        Stress90StateIntegrityError,
        record_completed_account_day,
    )

    state = record_completed_account_day(_state(), "20260825", 0.01)
    for day in ("20260825", "20260824"):
        with pytest.raises(Stress90StateIntegrityError, match="strictly advance"):
            record_completed_account_day(state, day, 0.01)
    for invalid in (float("nan"), float("inf"), -1.0, True):
        with pytest.raises(Stress90StateIntegrityError, match="completed account return"):
            record_completed_account_day(_state(), "20260825", invalid)


def test_sufficient_statistics_trigger_matches_full_return_list_each_day():
    from afuture.directional_drawdown_reserve_freeze import drawdown_reserve_triggered
    from afuture.directional_stress90_state import (
        drawdown_reserve_triggered_from_state,
        record_completed_account_day,
    )

    returns = (0.10, -0.10, -0.10, -0.10, 0.20, -0.05)
    state = _state()
    completed = []
    for offset, daily_return in enumerate(returns):
        completed.append(daily_return)
        state = record_completed_account_day(
            state,
            f"202609{offset + 1:02d}",
            daily_return,
        )
        assert drawdown_reserve_triggered_from_state(state) is drawdown_reserve_triggered(
            completed,
            hard_drawdown=0.30,
            daily_loss=0.05,
        )


def test_explicit_rebase_resets_only_live_account_soft_path_and_writes_audit():
    from afuture.directional_stress90_policy import candidate_state_digest
    from afuture.directional_stress90_state import (
        REBASE_CONFIRMATION,
        rebase_stress90_account,
        record_completed_account_day,
    )

    state = record_completed_account_day(_state(), "20260825", -0.10)
    candidate_digest = candidate_state_digest(state.candidate_state())
    rebased, audit = rebase_stress90_account(
        state,
        account_trading_day="20260826",
        account_equity=750_000.0,
        operator_reason="capital withdrawal and verified account reset",
        halted=True,
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        strong_confirmation=REBASE_CONFIRMATION,
    )

    assert rebased.completed_account_wealth == 1.0
    assert rebased.completed_account_high_watermark == 1.0
    assert rebased.last_completed_account_day is None
    assert rebased.recent_daily_returns_for_adaptive_margin == ()
    assert rebased.live_inception_day == "20260826"
    assert candidate_state_digest(rebased.candidate_state()) == candidate_digest
    assert audit.operator_reason == "capital withdrawal and verified account reset"
    assert audit.account_equity == 750_000.0
    assert audit.old_completed_account_wealth == state.completed_account_wealth


@pytest.mark.parametrize(
    "override",
    [
        {"halted": False},
        {"broker_flat": False},
        {"local_flat": False},
        {"no_active_orders": False},
        {"reconciled": False},
        {"strong_confirmation": "wrong"},
        {"operator_reason": ""},
        {"account_equity": float("nan")},
    ],
)
def test_rebase_fails_closed_unless_every_lifecycle_gate_passes(override):
    from afuture.directional_stress90_state import (
        REBASE_CONFIRMATION,
        Stress90StateIntegrityError,
        rebase_stress90_account,
    )

    arguments = {
        "account_trading_day": "20260826",
        "account_equity": 750_000.0,
        "operator_reason": "verified account lifecycle change",
        "halted": True,
        "broker_flat": True,
        "local_flat": True,
        "no_active_orders": True,
        "reconciled": True,
        "strong_confirmation": REBASE_CONFIRMATION,
    }
    arguments.update(override)

    with pytest.raises(Stress90StateIntegrityError, match="rebase"):
        rebase_stress90_account(_state(), **arguments)
