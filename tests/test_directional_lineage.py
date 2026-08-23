import numpy as np
import pandas as pd
import pytest

from afuture.directional_acceptance import ProductionMechanicsConfig
from afuture.directional_robustness import MarginAwareDirectionalProductionAcceptance
from afuture.execution_aligned_policy import ExecutionAlignedAggressivePolicy


def _history(periods: int = 220):
    dates = pd.date_range("2025-01-01", periods=periods, freq="B")
    close = pd.DataFrame(
        {
            "A": 100 * np.cumprod([1.006 if i % 7 else 0.98 for i in range(periods)]),
            "M": 100 * np.cumprod([0.996 if i % 5 else 1.018 for i in range(periods)]),
            "RB": 100 * np.cumprod([1.004 if i % 4 else 0.985 for i in range(periods)]),
            "CU": 100 * np.cumprod([0.997 if i % 6 else 1.016 for i in range(periods)]),
        },
        index=dates,
    )
    overnight = np.where(np.arange(periods) % 3 == 0, 1.004, 0.998)
    open_prices = close.shift(1).mul(overnight, axis=0)
    open_prices.iloc[0] = close.iloc[0]
    return open_prices, close


def test_policy_lineage_reproduces_weights_and_template_contributions_exactly():
    open_prices, close = _history()
    policy = ExecutionAlignedAggressivePolicy(products=tuple(close.columns))
    expected = policy.weight_history(open_prices, close)

    actual, meta, lineage = policy.weight_history_with_lineage(open_prices, close)

    pd.testing.assert_frame_equal(actual, expected)
    assert {"date", "selected_templates"} <= set(meta.columns)
    assert {
        "date",
        "template_id",
        "product",
        "raw_template_weight",
        "contribution_weight",
        "aggregate_weight",
    } <= set(lineage.columns)
    reconstructed = (
        lineage.groupby(["date", "product"], sort=True)["contribution_weight"]
        .sum()
        .unstack("product")
        .reindex(index=actual.index, columns=actual.columns)
        .fillna(0.0)
    )
    pd.testing.assert_frame_equal(reconstructed, actual, check_names=False)


def test_policy_lineage_is_causal_on_last_row():
    open_prices, close = _history()
    policy = ExecutionAlignedAggressivePolicy(products=tuple(close.columns))
    baseline, baseline_meta, baseline_lineage = policy.weight_history_with_lineage(
        open_prices, close
    )
    changed_open = open_prices.copy()
    changed_close = close.copy()
    changed_open.iloc[-1] *= [2.0, 0.5, 1.8, 0.6]
    changed_close.iloc[-1] *= [3.0, 0.4, 2.5, 0.5]
    changed, changed_meta, changed_lineage = policy.weight_history_with_lineage(
        changed_open, changed_close
    )

    pd.testing.assert_series_equal(baseline.iloc[-1], changed.iloc[-1])
    assert baseline_meta.iloc[-1]["selected_templates"] == changed_meta.iloc[-1]["selected_templates"]
    baseline_last = baseline_lineage[baseline_lineage["date"] == baseline.index[-1]].reset_index(drop=True)
    changed_last = changed_lineage[changed_lineage["date"] == changed.index[-1]].reset_index(drop=True)
    pd.testing.assert_frame_equal(baseline_last, changed_last)


def test_production_acceptance_records_final_integer_target_without_changing_economics():
    raw = pd.DataFrame([
        {"date":"2026-08-20","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":99,"close":99,"volume":5000,"hold":30000},
        {"date":"2026-08-21","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":110,"volume":5000,"hold":30000},
        {"date":"2026-08-24","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":111,"close":112,"volume":5000,"hold":30000},
    ])
    weights = pd.DataFrame({"A":[1.0,0.0]}, index=pd.to_datetime(["2026-08-21","2026-08-24"]))
    sim = MarginAwareDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=100000,
            margin_rate_proxy=0.01,
            margin_estimate_buffer=1.0,
            max_contract_volume=100,
            max_daily_loss_ratio=.5,
            max_total_drawdown_ratio=.8,
            max_margin_ratio=.9,
            min_available_ratio=0,
        )
    )

    result = sim.simulate(raw, weights, cost_bps=5)
    targets = result.events[result.events["kind"] == "target"]
    trades = result.events[result.events["kind"] == "trade"]

    assert list(targets["action"].unique()) == ["target"]
    first = targets[targets["date"] == pd.Timestamp("2026-08-21")].iloc[0]
    second = targets[targets["date"] == pd.Timestamp("2026-08-24")].iloc[0]
    assert first["product"] == "A"
    assert first["symbol"] == "A2609"
    assert first["lots_before"] == 0
    assert first["lots_after"] == 100
    assert first["delta_lots"] == 100
    assert second["lots_before"] == 100
    assert second["lots_after"] == 0
    assert second["delta_lots"] == -100
    assert targets["turnover_notional"].sum() == 0.0
    assert targets["transaction_cost"].sum() == 0.0
    assert trades["transaction_cost"].sum() == pytest.approx(105.5)
    assert result.final_equity == pytest.approx(110894.5)


def test_exact_lineage_join_preserves_product_execution_cost_without_fake_template_allocation():
    from afuture.directional_attribution import build_exact_lineage_attribution

    open_prices, close = _history()
    policy = ExecutionAlignedAggressivePolicy(products=tuple(close.columns))
    weights, meta, lineage = policy.weight_history_with_lineage(open_prices, close)
    day = weights.index[-1]
    product = next(
        product for product, value in weights.iloc[-1].items() if abs(float(value)) > 1e-15
    )
    target_weight = float(weights.loc[day, product])
    events = pd.DataFrame([
        {
            "date": day,
            "kind": "target",
            "action": "target",
            "product": product,
            "symbol": f"{product}2609",
            "side": "long" if target_weight > 0 else "short",
            "lots_before": 0,
            "lots_after": 3 if target_weight > 0 else -3,
            "delta_lots": 3 if target_weight > 0 else -3,
            "price": 100.0,
            "turnover_notional": 0.0,
            "transaction_cost": 0.0,
            "gross_pnl": 0.0,
        },
        {
            "date": day,
            "kind": "trade",
            "action": "entry",
            "product": product,
            "symbol": f"{product}2609",
            "side": "long" if target_weight > 0 else "short",
            "lots_before": 0,
            "lots_after": 3 if target_weight > 0 else -3,
            "delta_lots": 3 if target_weight > 0 else -3,
            "price": 100.0,
            "turnover_notional": 3000.0,
            "transaction_cost": 4.5,
            "gross_pnl": 0.0,
        },
    ])

    result = build_exact_lineage_attribution(
        meta=meta,
        template_product=lineage,
        events=events,
    )
    product_execution = result["product_execution"]
    template_product = result["template_product"]
    row = product_execution.iloc[0]
    assert row["date"] == day
    assert row["product"] == product
    assert row["target_weight"] == pytest.approx(target_weight)
    assert row["target_lots"] == (3 if target_weight > 0 else -3)
    assert row["realized_lots_after"] == (3 if target_weight > 0 else -3)
    assert row["turnover_notional"] == pytest.approx(3000.0)
    assert row["transaction_cost"] == pytest.approx(4.5)
    assert template_product["transaction_cost"].isna().all()
    assert template_product["turnover_notional"].isna().all()
