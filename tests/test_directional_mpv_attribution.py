import pandas as pd

from afuture.directional_mpv_attribution import (
    build_one_lot_counterfactual_label,
    completed_product_evidence,
)


def sample_events():
    return pd.DataFrame([
        {"date": "2026-08-18", "kind": "pnl", "product": "AG", "gross_pnl": 100.0, "turnover_notional": 0.0, "transaction_cost": 0.0},
        {"date": "2026-08-19", "kind": "pnl", "product": "AG", "gross_pnl": 200.0, "turnover_notional": 0.0, "transaction_cost": 0.0},
        {"date": "2026-08-19", "kind": "trade", "product": "AG", "gross_pnl": 0.0, "turnover_notional": 100000.0, "transaction_cost": 150.0},
        {"date": "2026-08-19", "kind": "pnl", "product": "CU", "gross_pnl": -50.0, "turnover_notional": 0.0, "transaction_cost": 0.0},
        {"date": "2026-08-20", "kind": "pnl", "product": "AG", "gross_pnl": 999999.0, "turnover_notional": 0.0, "transaction_cost": 0.0},
    ])


def test_completed_product_evidence_uses_only_events_before_decision_day():
    evidence = completed_product_evidence(
        events=sample_events(),
        decision_date=pd.Timestamp("2026-08-20"),
    )
    assert evidence.loc["AG", "gross_pnl"] == 300.0
    assert evidence.loc["AG", "turnover_notional"] == 100000.0
    assert evidence.loc["AG", "transaction_cost"] == 150.0
    assert evidence.loc["AG", "net_alpha"] == 150.0
    assert evidence.loc["AG", "pnl_event_count"] == 2
    assert evidence.loc["AG", "trade_event_count"] == 1


def test_one_lot_counterfactual_label_is_explicitly_future_outcome_only():
    label = build_one_lot_counterfactual_label(
        decision_date=pd.Timestamp("2026-08-20"),
        label_end_date=pd.Timestamp("2026-08-21"),
        product="AG",
        symbol="AG2612",
        delta_lots=1,
        start_price=8000.0,
        end_price=8010.0,
        multiplier=15.0,
        cost_bps=15.0,
    )
    assert label.realized_incremental_gross_pnl == 150.0
    assert label.realized_incremental_turnover_notional == 120000.0
    assert label.realized_incremental_transaction_cost == 180.0
    assert label.realized_net_counterfactual_value == -30.0
    assert label.label_end_date > label.decision_date
