import pandas as pd


def _events():
    return pd.DataFrame(
        [
            {"date": "2026-01-05", "kind": "pnl", "action": "intraday", "product": "AG", "symbol": "AG2606", "lots_before": 1, "gross_pnl": 300.0, "turnover_notional": 0.0, "transaction_cost": 0.0},
            {"date": "2026-01-05", "kind": "trade", "action": "resize", "product": "AG", "symbol": "AG2606", "lots_before": 0, "gross_pnl": 0.0, "turnover_notional": 10000.0, "transaction_cost": 15.0},
            {"date": "2026-01-05", "kind": "pnl", "action": "intraday", "product": "CU", "symbol": "CU2606", "lots_before": 1, "gross_pnl": -100.0, "turnover_notional": 0.0, "transaction_cost": 0.0},
            {"date": "2026-01-05", "kind": "trade", "action": "resize", "product": "CU", "symbol": "CU2606", "lots_before": 0, "gross_pnl": 0.0, "turnover_notional": 10000.0, "transaction_cost": 15.0},
        ]
    )


def _kwargs(shadow_events, decision_date="2026-01-06"):
    return dict(
        shadow_events=shadow_events,
        decision_date=pd.Timestamp(decision_date),
        reference_lots={"CU2606": 1},
        requested_lots={"AG2606": 1, "CU2606": 1},
        current_lots={"CU2606": 1},
        symbol_products={"AG2606": "AG", "CU2606": "CU"},
        lot_notionals={"AG2606": 10000.0, "CU2606": 10000.0},
        per_lot_margin={"AG2606": 2000.0, "CU2606": 2000.0},
        equity=10000.0,
        soft_margin_budget=2000.0,
        max_gross_ratio=2.0,
        max_abs_lots=35,
        cost_rate=0.0015,
    )


def test_shadow_value_uses_exogenous_baseline_events_even_when_candidate_has_no_events():
    from afuture.directional_shadow_mpv import optimize_with_shadow_mpv

    result = optimize_with_shadow_mpv(**_kwargs(_events()))

    assert result.shadow_event_count == 4
    assert result.optimization.target_lots == {"AG2606": 1}
    assert result.optimization.improvement > 0.0


def test_same_day_shadow_outcome_is_excluded_from_decision():
    from afuture.directional_shadow_mpv import optimize_with_shadow_mpv

    events = _events()
    future = pd.DataFrame(
        [{"date": "2026-01-06", "kind": "pnl", "action": "intraday", "product": "CU", "symbol": "CU2606", "lots_before": 1, "gross_pnl": 999999.0, "turnover_notional": 0.0, "transaction_cost": 0.0}]
    )
    baseline = optimize_with_shadow_mpv(**_kwargs(events))
    changed = optimize_with_shadow_mpv(**_kwargs(pd.concat([events, future], ignore_index=True)))

    assert changed.optimization.target_lots == baseline.optimization.target_lots
    assert changed.shadow_event_count == baseline.shadow_event_count


def test_no_completed_shadow_evidence_falls_back_exactly_to_reference():
    from afuture.directional_shadow_mpv import optimize_with_shadow_mpv

    result = optimize_with_shadow_mpv(**_kwargs(pd.DataFrame()))

    assert result.optimization.target_lots == {"CU2606": 1}
    assert result.optimization.fallback_reason == "insufficient causal MPV evidence"


def test_candidate_owned_events_are_not_an_input_surface():
    from inspect import signature
    from afuture.directional_shadow_mpv import optimize_with_shadow_mpv

    assert "observed_events" not in signature(optimize_with_shadow_mpv).parameters
    assert "candidate_events" not in signature(optimize_with_shadow_mpv).parameters
