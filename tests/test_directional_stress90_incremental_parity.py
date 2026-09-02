import hashlib

import pandas as pd
import pytest


def _synthetic_inputs():
    from afuture.directional_stress90_policy import STRESS90_POLICY

    close_days = pd.bdate_range("2026-01-01", periods=28)
    target_days = close_days[-7:]
    close = pd.DataFrame(index=close_days, columns=STRESS90_POLICY.products, dtype=float)
    for offset, product in enumerate(STRESS90_POLICY.products):
        close[product] = [100.0 + offset + 0.2 * index for index in range(len(close_days))]

    base = pd.DataFrame(0.0, index=target_days, columns=STRESS90_POLICY.products)
    base.loc[target_days[0], ["A", "AG"]] = [1.0, 1.0]
    base.loc[target_days[1], ["A", "AG"]] = [1.5, 0.5]
    base.loc[target_days[2], ["A", "AG"]] = [0.5, 1.5]
    base.loc[target_days[3], ["A", "AG"]] = [-1.0, 1.0]
    base.loc[target_days[4], ["A", "AG"]] = [-0.5, 1.5]
    base.loc[target_days[5], ["A", "AG"]] = [0.0, 2.0]
    base.loc[target_days[6], ["A", "AG"]] = [1.0, 1.0]

    flow = pd.DataFrame(0, index=target_days, columns=STRESS90_POLICY.oi_products, dtype=int)
    flow["A"] = [1, 1, 0, -1, 0, 0, 1]
    return base, close, flow


def test_batch_wrapper_matches_explicit_incremental_steps_for_every_layer():
    from afuture.directional_stress90_policy import (
        STRESS90_POLICY,
        Stress90CandidateState,
        build_stress90_candidate_path,
        step_stress90_candidate,
    )

    base, close, flow = _synthetic_inputs()
    batch = build_stress90_candidate_path(
        base_weights=base,
        completed_close_prices=close,
        confirming_flow=flow,
    )

    state = Stress90CandidateState.initial(STRESS90_POLICY)
    incremental = []
    for target_day in base.index:
        history = close.loc[close.index < target_day]
        input_day = history.index[-1].strftime("%Y%m%d")
        decision = step_stress90_candidate(
            prior_state=state,
            target_trading_day=target_day.strftime("%Y%m%d"),
            base_weights=base.loc[target_day].to_dict(),
            completed_close_history={
                product: tuple(history[product]) for product in STRESS90_POLICY.products
            },
            completed_oi_flow=flow.loc[target_day].to_dict(),
            completed_close_day=input_day,
            completed_oi_day=input_day,
        )
        incremental.append(decision)
        state = decision.post_state

    for row, decision in zip(base.index, incremental, strict=True):
        for product in STRESS90_POLICY.products:
            assert batch.base_weights.at[row, product] == decision.base_weights[product]
            assert (
                batch.oi_confirmed_weights.at[row, product]
                == decision.oi_confirmed_weights[product]
            )
            assert (
                batch.cost_approved_weights.at[row, product]
                == decision.cost_approved_weights[product]
            )
            assert (
                abs(batch.survivor_weights.at[row, product] - decision.survivor_weights[product])
                <= 1e-14
            )
        assert batch.current_hhi.at[row] == decision.current_hhi
        assert batch.prior_hhi_median.at[row] == decision.prior_hhi_median
        assert bool(batch.concentration_freeze.at[row]) is decision.concentration_freeze
        assert batch.decisions[base.index.get_loc(row)].daily_decision_digest == (
            decision.daily_decision_digest
        )
    assert batch.final_state == state


def test_future_input_change_cannot_rewrite_earlier_daily_decision_digests():
    from afuture.directional_stress90_policy import build_stress90_candidate_path

    base, close, flow = _synthetic_inputs()
    baseline = build_stress90_candidate_path(
        base_weights=base,
        completed_close_prices=close,
        confirming_flow=flow,
    )
    changed = base.copy()
    changed.loc[changed.index[-1], ["A", "AG"]] = [0.5, 1.5]
    replay = build_stress90_candidate_path(
        base_weights=changed,
        completed_close_prices=close,
        confirming_flow=flow,
    )

    assert [item.daily_decision_digest for item in baseline.decisions[:-1]] == [
        item.daily_decision_digest for item in replay.decisions[:-1]
    ]
    assert (
        baseline.decisions[-1].daily_decision_digest != replay.decisions[-1].daily_decision_digest
    )


def test_batch_wrapper_rejects_non_discrete_oi_flow_instead_of_coercing_it_to_zero():
    from afuture.directional_stress90_policy import (
        Stress90InvariantError,
        build_stress90_candidate_path,
    )

    base, close, flow = _synthetic_inputs()
    altered = flow.astype(float)
    altered.at[altered.index[0], "A"] = 0.5

    with pytest.raises(Stress90InvariantError, match="invalid completed OI flow"):
        build_stress90_candidate_path(
            base_weights=base,
            completed_close_prices=close,
            confirming_flow=altered,
        )


def test_batch_wrapper_matches_current_dataframe_adapters():
    from afuture.directional_60m_oi_reversal_confirmation import (
        apply_oi_confirmation_to_direction_changes,
    )
    from afuture.directional_cost_aware_no_trade import apply_cost_aware_no_trade
    from afuture.directional_stress90_policy import (
        STRESS90_POLICY,
        build_stress90_candidate_path,
    )
    from afuture.directional_turnover_aware_survivor_reallocation import (
        reallocate_survivors_lexicographically,
    )

    base, close, flow = _synthetic_inputs()
    batch = build_stress90_candidate_path(
        base_weights=base,
        completed_close_prices=close,
        confirming_flow=flow,
    )
    oi = apply_oi_confirmation_to_direction_changes(
        raw_weights=base,
        confirming_flow=flow,
        supported_products=STRESS90_POLICY.oi_products,
    )
    cost = apply_cost_aware_no_trade(weights=oi, close_prices=close)
    survivor = reallocate_survivors_lexicographically(
        original_weights=oi,
        approved_weights=cost,
    )

    pd.testing.assert_frame_equal(batch.oi_confirmed_weights, oi, atol=0.0, rtol=0.0)
    pd.testing.assert_frame_equal(batch.cost_approved_weights, cost, atol=0.0, rtol=0.0)
    pd.testing.assert_frame_equal(batch.survivor_weights, survivor, atol=1e-14, rtol=0.0)


def test_candidate_weight_digest_uses_the_archived_stable_text_contract():
    from afuture.directional_stress90_policy import candidate_weight_digest

    weights = pd.DataFrame(
        {"AG": [-0.5], "A": [1.0]},
        index=pd.to_datetime(["2026-01-05"]),
    )
    literal = b"2026-01-05|A|1\n2026-01-05|AG|-0.5\n"

    assert candidate_weight_digest(weights) == hashlib.sha256(literal).hexdigest()


def test_offline_final_evaluator_exposes_shared_policy_and_daily_decision_identity():
    from afuture.directional_stress90_policy import (
        STRESS90_POLICY,
        build_stress90_candidate_path,
    )
    from tools.evaluate_directional_stress80_final import build_final_candidate_weights

    base, close, flow = _synthetic_inputs()
    source_days = [close.index[close.index < base.index[0]][-1], *base.index[:-1]]
    bars = []
    for target_day, source_day in zip(base.index, source_days, strict=True):
        for product in STRESS90_POLICY.oi_products:
            direction = int(flow.at[target_day, product])
            bars.extend(
                [
                    {
                        "datetime": source_day + pd.Timedelta(hours=10),
                        "product": product,
                        "symbol": f"{product}2609",
                        "open": 100.0,
                        "close": 100.0,
                        "volume": 10.0,
                        "hold": 100.0,
                    },
                    {
                        "datetime": source_day + pd.Timedelta(hours=14),
                        "product": product,
                        "symbol": f"{product}2609",
                        "open": 100.0,
                        "close": 100.0 + direction,
                        "volume": 20.0,
                        "hold": 110.0 if direction else 100.0,
                    },
                ]
            )
    continuous = close.stack().rename("close").reset_index()
    continuous.columns = ["date", "product", "close"]

    candidate, audit = build_final_candidate_weights(
        base_weights=base,
        bars_60m=pd.DataFrame(bars),
        continuous_raw=continuous,
    )
    shared = build_stress90_candidate_path(
        base_weights=base,
        completed_close_prices=close,
        confirming_flow=flow,
    )

    pd.testing.assert_frame_equal(candidate, shared.survivor_weights, atol=1e-14, rtol=0.0)
    assert audit["policy_definition_digest"] == STRESS90_POLICY.policy_definition_digest
    assert audit["last_daily_decision_digest"] == shared.decisions[-1].daily_decision_digest
