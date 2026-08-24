import pandas as pd

from afuture.directional_mpv_research import optimize_with_causal_mpv


def events_for_rates(ag_rate=300.0, cu_rate=50.0, n=10):
    rows = []
    for offset in range(n):
        day = pd.Timestamp("2026-01-01") + pd.Timedelta(days=offset)
        rows.append(
            {
                "date": day,
                "kind": "pnl",
                "action": "intraday",
                "product": "AG",
                "lots_before": 1,
                "gross_pnl": ag_rate,
                "turnover_notional": 0.0,
                "transaction_cost": 0.0,
            }
        )
        rows.append(
            {
                "date": day,
                "kind": "pnl",
                "action": "intraday",
                "product": "CU",
                "lots_before": 1,
                "gross_pnl": cu_rate,
                "turnover_notional": 0.0,
                "transaction_cost": 0.0,
            }
        )
    return pd.DataFrame(rows)


def common(**overrides):
    values = dict(
        observed_events=events_for_rates(),
        reference_lots={"CU2610": 2},
        requested_lots={"AG2612": 2, "CU2610": 2},
        current_lots={"CU2610": 2},
        symbol_products={"AG2612": "AG", "CU2610": "CU"},
        lot_notionals={"AG2612": 100000.0, "CU2610": 100000.0},
        per_lot_margin={"AG2612": 20000.0, "CU2610": 20000.0},
        equity=500000.0,
        soft_margin_budget=40000.0,
        max_gross_ratio=2.0,
        max_abs_lots=35,
        cost_rate=0.0,
    )
    values.update(overrides)
    return values


def test_research_optimizer_prefers_higher_completed_intraday_mpv_under_same_capacity():
    result = optimize_with_causal_mpv(**common())
    assert result.optimization.target_lots == {"AG2612": 2}
    assert result.product_estimates[
        "AG"
    ].expected_gross_alpha_per_lot_segment > result.product_estimates[
        "CU"
    ].expected_gross_alpha_per_lot_segment


def test_research_optimizer_falls_back_exactly_without_completed_exposure_evidence():
    reference = {"CU2610": 2}
    result = optimize_with_causal_mpv(
        **common(observed_events=pd.DataFrame(), reference_lots=reference)
    )
    assert result.optimization.target_lots == reference
    assert result.optimization.fallback_reason == "insufficient causal MPV evidence"


def test_research_optimizer_charges_exact_one_way_turnover_cost():
    events = events_for_rates(ag_rate=100.0, cu_rate=100.0, n=10)
    result = optimize_with_causal_mpv(
        **common(
            observed_events=events,
            reference_lots={},
            requested_lots={"AG2612": 1},
            current_lots={},
            symbol_products={"AG2612": "AG"},
            lot_notionals={"AG2612": 100000.0},
            per_lot_margin={"AG2612": 15000.0},
            soft_margin_budget=15000.0,
            cost_rate=0.0015,
        )
    )
    assert result.optimization.target_lots == {}
