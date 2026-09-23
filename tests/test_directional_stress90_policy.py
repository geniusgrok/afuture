import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from statistics import median

import pytest

EXPECTED_CANDIDATE_SHA = "8e38dbf6441b561dd1728df08665b94b15cc3358823257505c2fcb9d63f09f28"
OLD_POLICY_DEFINITION_DIGEST = "97435aae770aea87e8313b17ea5af730a4c540f14b95a561c8da5f90e2db5cf6"


def test_stress90_definition_has_distinct_definition_and_historical_digests():
    from afuture.directional_stress90_policy import STRESS90_POLICY

    assert STRESS90_POLICY.policy_id == "directional.stress90"
    assert STRESS90_POLICY.definition_version == 2
    assert STRESS90_POLICY.oi_products == ("A", "C", "EG", "I", "M", "P", "PP", "TA", "Y")
    assert STRESS90_POLICY.historical_candidate_weight_sha256 == EXPECTED_CANDIDATE_SHA
    assert STRESS90_POLICY.policy_definition_digest != OLD_POLICY_DEFINITION_DIGEST
    assert STRESS90_POLICY.policy_definition_digest != EXPECTED_CANDIDATE_SHA
    assert len(STRESS90_POLICY.policy_definition_digest) == 64
    assert len(STRESS90_POLICY.policy_manifest_digest) == 64
    assert len(STRESS90_POLICY.products_manifest_digest) == 64
    assert len(STRESS90_POLICY.session_manifest_digest) == 64
    assert len(STRESS90_POLICY.products) == 50
    assert len(STRESS90_POLICY.template_ids) == 96
    assert STRESS90_POLICY.meta_lookback == 11
    assert STRESS90_POLICY.meta_rebalance == 3
    assert STRESS90_POLICY.meta_count == 3
    assert STRESS90_POLICY.base_cost_bps == 5.0
    assert STRESS90_POLICY.stress_cost_bps == 15.0
    assert STRESS90_POLICY.completed_lookback_sessions == 20
    assert STRESS90_POLICY.benefit_horizon_sessions == 3
    assert STRESS90_POLICY.cost_hurdle_bps == 15.0
    assert STRESS90_POLICY.max_gross_leverage == 2.0
    assert STRESS90_POLICY.drawdown_reserve_ratio == pytest.approx(0.25)
    assert STRESS90_POLICY.hard_risk_envelope == {
        "max_target_gross": 2.0,
        "max_realized_gross": 2.0,
        "max_margin_ratio": 0.35,
        "min_available_ratio": 0.25,
        "max_daily_loss_ratio": 0.05,
        "max_total_drawdown_ratio": 0.30,
        "max_contract_lots": 35,
        "margin_estimate_buffer": 1.25,
    }


def test_current_runtime_imports_do_not_load_offline_tools_evaluators():
    script = """
import sys
import afuture.cli
import afuture.directional_runtime
import afuture.runtime_factory
assert not any(name == "tools" or name.startswith("tools.") for name in sys.modules)
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_stress90_definition_is_immutable():
    from afuture.directional_stress90_policy import STRESS90_POLICY

    with pytest.raises(FrozenInstanceError):
        STRESS90_POLICY.cost_hurdle_bps = 10.0  # type: ignore[misc]


@pytest.mark.parametrize(
    "change",
    [
        {"products": ("A",)},
        {"oi_products": ("A",)},
        {"template_ids": ("changed",)},
        {"meta_lookback": 12},
        {"cost_hurdle_bps": 10.0},
        {"session_manifest_digest": "0" * 64},
        {"max_gross_leverage": 2.1},
        {"max_margin_ratio": 0.36},
        {"historical_candidate_weight_sha256": "0" * 64},
    ],
)
def test_stress90_definition_rejects_economic_or_hard_risk_mutation(change):
    from afuture.directional_stress90_policy import STRESS90_POLICY

    with pytest.raises(ValueError, match="frozen"):
        replace(STRESS90_POLICY, **change)


@pytest.mark.parametrize(
    ("current", "target", "flow", "expected"),
    [
        (0.0, 1.0, 1, 1.0),
        (0.5, 1.0, 0, 0.5),
        (1.0, 0.5, -1, 0.5),
        (1.0, 0.0, -1, 0.0),
        (1.0, -1.0, -1, -1.0),
        (1.0, -1.0, 0, 0.0),
    ],
)
def test_oi_confirmation_covers_entry_add_reduction_exit_and_reversal(
    current,
    target,
    flow,
    expected,
):
    from afuture.directional_stress90_policy import apply_oi_confirmation_row

    result = apply_oi_confirmation_row(
        raw_weights={"A": target},
        prior_applied={"A": current},
        completed_flow={"A": flow},
        supported_products=("A",),
    )

    assert result == {"A": expected}


def test_oi_confirmation_leaves_unsupported_product_unchanged():
    from afuture.directional_stress90_policy import apply_oi_confirmation_row

    result = apply_oi_confirmation_row(
        raw_weights={"AG": -0.75},
        prior_applied={"AG": 1.0},
        completed_flow={},
        supported_products=("A",),
    )

    assert result == {"AG": -0.75}


def test_missing_oi_flow_is_not_legal_zero_flow():
    from afuture.directional_stress90_policy import (
        Stress90InputIncomplete,
        apply_oi_confirmation_row,
    )

    with pytest.raises(Stress90InputIncomplete, match="missing completed OI flow: A"):
        apply_oi_confirmation_row(
            raw_weights={"A": 1.0},
            prior_applied={"A": 0.0},
            completed_flow={"A": None},
            supported_products=("A",),
        )

    assert apply_oi_confirmation_row(
        raw_weights={"A": 1.0},
        prior_applied={"A": 0.0},
        completed_flow={"A": 0},
        supported_products=("A",),
    ) == {"A": 0.0}


def test_missing_oi_flow_allows_only_zero_risk_and_reduction():
    from afuture.directional_stress90_policy import apply_oi_confirmation_row

    assert apply_oi_confirmation_row(
        raw_weights={"A": 0.0, "TA": 0.0},
        prior_applied={"A": 1.0, "TA": 0.0},
        completed_flow={"A": None, "TA": None},
        supported_products=("A", "TA"),
    ) == {"A": 0.0, "TA": 0.0}


@pytest.mark.parametrize(("current", "target"), [(0.0, 1.0), (0.5, 1.0), (1.0, -1.0)])
def test_missing_oi_flow_still_rejects_entry_add_and_reversal(current, target):
    from afuture.directional_stress90_policy import (
        Stress90InputIncomplete,
        apply_oi_confirmation_row,
    )

    with pytest.raises(Stress90InputIncomplete, match="missing completed OI flow: A"):
        apply_oi_confirmation_row(
            raw_weights={"A": target},
            prior_applied={"A": current},
            completed_flow={"A": None},
            supported_products=("A",),
        )


def _close_path(daily_return: float, sessions: int = 20) -> tuple[float, ...]:
    values = [100.0]
    for _ in range(sessions):
        values.append(values[-1] * (1.0 + daily_return))
    return tuple(values)


def test_cost_gate_requires_twenty_completed_returns_and_three_day_benefit_above_15bp():
    from afuture.directional_stress90_policy import apply_cost_gate_row

    insufficient = apply_cost_gate_row(
        oi_weights={"AG": 1.0},
        prior_approved={"AG": 0.0},
        completed_close_history={"AG": _close_path(0.001, sessions=19)},
    )
    exact_hurdle = apply_cost_gate_row(
        oi_weights={"AG": 1.0},
        prior_approved={"AG": 0.0},
        completed_close_history={"AG": _close_path(0.0005)},
    )
    strictly_above = apply_cost_gate_row(
        oi_weights={"AG": 1.0},
        prior_approved={"AG": 0.0},
        completed_close_history={"AG": _close_path(0.00050001)},
    )

    assert insufficient == {"AG": 0.0}
    assert exact_hurdle == {"AG": 0.0}
    assert strictly_above == {"AG": 1.0}


def test_cost_gate_blocks_only_entry_and_same_sign_add():
    from afuture.directional_stress90_policy import apply_cost_gate_row

    weak = {product: _close_path(0.0) for product in ("A", "AG", "AL", "AU")}
    result = apply_cost_gate_row(
        oi_weights={"A": 1.0, "AG": 1.0, "AL": 0.25, "AU": -1.0},
        prior_approved={"A": 0.0, "AG": 0.5, "AL": 0.5, "AU": 1.0},
        completed_close_history=weak,
    )

    assert result == {"A": 0.0, "AG": 0.5, "AL": 0.25, "AU": -1.0}


def test_missing_close_in_active_window_blocks_increase_but_not_reduction():
    from afuture.directional_stress90_policy import apply_cost_gate_row

    incomplete = list(_close_path(0.001))
    incomplete[-2] = float("nan")

    assert apply_cost_gate_row(
        oi_weights={"A": 1.0, "AG": 0.5},
        prior_approved={"A": 0.0, "AG": 1.0},
        completed_close_history={"A": incomplete, "AG": incomplete},
    ) == {"A": 0.0, "AG": 0.5}


def test_missing_close_outside_twenty_session_window_no_longer_blocks_increase():
    from afuture.directional_stress90_policy import apply_cost_gate_row

    recovered = (float("nan"), *_close_path(0.001))

    assert apply_cost_gate_row(
        oi_weights={"A": 1.0},
        prior_approved={"A": 0.0},
        completed_close_history={"A": recovered},
    ) == {"A": 1.0}


def test_survivor_reallocation_preserves_support_sign_gross_and_turnover_tie_break():
    from afuture.directional_stress90_policy import reallocate_survivor_row

    first = reallocate_survivor_row(
        oi_weights={"A": 1.0, "M": 1.0, "C": 0.0},
        approved_weights={"A": 1.0, "M": 0.0, "C": 0.0},
        prior_survivor={"A": 0.0, "M": 0.0, "C": 0.0},
    )
    second = reallocate_survivor_row(
        oi_weights={"A": 0.5, "M": 0.5, "C": 1.0},
        approved_weights={"A": 0.5, "M": 0.5, "C": 0.0},
        prior_survivor=first,
    )

    assert first == {"A": 2.0, "M": 0.0, "C": 0.0}
    assert second == {"A": 1.5, "M": 0.5, "C": 0.0}
    assert sum(abs(value) for value in second.values()) == pytest.approx(2.0)


def test_concentration_uses_strictly_prior_median_then_appends_current_hhi():
    from afuture.directional_stress90_policy import advance_concentration_history

    first = advance_concentration_history((), {"A": 1.0, "AG": -1.0})
    equal = advance_concentration_history((0.4, 0.6), {"A": 1.0, "AG": -1.0})
    below = advance_concentration_history((0.6,), {"A": 1.0, "AG": -1.0})
    above = advance_concentration_history((0.4,), {"A": 1.0})

    assert first == (0.5, None, False, (0.5,))
    assert equal == (0.5, median((0.4, 0.6)), False, (0.4, 0.6, 0.5))
    assert below == (0.5, 0.6, False, (0.6, 0.5))
    assert above == (1.0, 0.4, False, (0.4, 1.0))


def _full_weights(**overrides: float) -> dict[str, float]:
    from afuture.directional_stress90_policy import STRESS90_POLICY

    result = {product: 0.0 for product in STRESS90_POLICY.products}
    result.update(overrides)
    return result


def _full_close_history(daily_return: float = 0.001) -> dict[str, tuple[float, ...]]:
    from afuture.directional_stress90_policy import STRESS90_POLICY

    return {product: _close_path(daily_return) for product in STRESS90_POLICY.products}


def _full_oi_flow(value: int = 0) -> dict[str, int]:
    from afuture.directional_stress90_policy import STRESS90_POLICY

    return {product: value for product in STRESS90_POLICY.oi_products}


def test_incremental_step_exposes_every_layer_and_advances_hhi_without_side_effects():
    from afuture.directional_stress90_policy import (
        STRESS90_POLICY,
        Stress90CandidateState,
        step_stress90_candidate,
    )

    state = Stress90CandidateState.initial(STRESS90_POLICY)
    flow = _full_oi_flow()
    flow["A"] = 1
    decision = step_stress90_candidate(
        prior_state=state,
        target_trading_day="20260825",
        base_weights=_full_weights(A=1.0, AG=1.0),
        completed_close_history=_full_close_history(),
        completed_oi_flow=flow,
        completed_close_day="20260824",
        completed_oi_day="20260824",
    )

    assert decision.base_weights["A"] == 1.0
    assert decision.oi_confirmed_weights["A"] == 1.0
    assert decision.cost_approved_weights["A"] == 1.0
    assert decision.survivor_weights["A"] == 1.0
    assert decision.current_hhi == 0.5
    assert decision.prior_hhi_median is None
    assert decision.concentration_freeze is False
    assert decision.input_days == {"completed_close": "20260824", "completed_oi": "20260824"}
    assert set(decision.input_digests) == {
        "prior_state",
        "base_weights",
        "completed_close",
        "completed_oi",
    }
    assert set(decision.layer_digests) == {"base", "oi", "cost", "survivor"}
    assert len(decision.daily_decision_digest) == 64
    assert decision.daily_decision_digest != STRESS90_POLICY.policy_definition_digest
    assert decision.daily_decision_digest != STRESS90_POLICY.historical_candidate_weight_sha256
    assert state.completed_concentrations == ()
    assert decision.post_state.completed_concentrations == (0.5,)
    assert decision.post_state.last_completed_target_day == "20260825"


def test_daily_digest_commits_to_the_complete_prior_policy_state():
    from afuture.directional_stress90_policy import (
        STRESS90_POLICY,
        Stress90CandidateState,
        step_stress90_candidate,
    )

    initial = Stress90CandidateState.initial(STRESS90_POLICY)
    first_state = replace(initial, completed_concentrations=(0.4, 0.6))
    second_state = replace(initial, completed_concentrations=(0.5,))
    arguments = {
        "target_trading_day": "20260825",
        "base_weights": _full_weights(A=1.0, AG=1.0),
        "completed_close_history": _full_close_history(),
        "completed_oi_flow": {**_full_oi_flow(), "A": 1},
        "completed_close_day": "20260824",
        "completed_oi_day": "20260824",
    }

    first = step_stress90_candidate(prior_state=first_state, **arguments)
    second = step_stress90_candidate(prior_state=second_state, **arguments)

    assert first.prior_hhi_median == second.prior_hhi_median == 0.5
    assert first.survivor_weights == second.survivor_weights
    assert first.daily_decision_digest != second.daily_decision_digest


@pytest.mark.parametrize(
    ("base_overrides", "message"),
    [
        ({"A": float("nan")}, "finite"),
        ({"A": 1.1, "AG": 1.0}, "2x gross"),
        ({"NOT_A_PRODUCT": 1.0}, "product manifest"),
    ],
)
def test_incremental_step_rejects_invalid_weights(base_overrides, message):
    from afuture.directional_stress90_policy import (
        STRESS90_POLICY,
        Stress90CandidateState,
        Stress90InvariantError,
        step_stress90_candidate,
    )

    weights = _full_weights()
    weights.update(base_overrides)
    with pytest.raises(Stress90InvariantError, match=message):
        step_stress90_candidate(
            prior_state=Stress90CandidateState.initial(STRESS90_POLICY),
            target_trading_day="20260825",
            base_weights=weights,
            completed_close_history=_full_close_history(),
            completed_oi_flow=_full_oi_flow(),
            completed_close_day="20260824",
            completed_oi_day="20260824",
        )
