import numpy as np
import pandas as pd
import pytest

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
    from afuture.directional_lineage import build_weight_lineage

    open_prices, close = _history()
    policy = ExecutionAlignedAggressivePolicy(products=tuple(close.columns))
    expected = policy.weight_history(open_prices, close)

    actual, meta, lineage = build_weight_lineage(policy, open_prices, close)

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
    from afuture.directional_lineage import build_weight_lineage

    open_prices, close = _history()
    policy = ExecutionAlignedAggressivePolicy(products=tuple(close.columns))
    baseline, baseline_meta, baseline_lineage = build_weight_lineage(
        policy, open_prices, close
    )
    changed_open = open_prices.copy()
    changed_close = close.copy()
    changed_open.iloc[-1] *= [2.0, 0.5, 1.8, 0.6]
    changed_close.iloc[-1] *= [3.0, 0.4, 2.5, 0.5]
    changed, changed_meta, changed_lineage = build_weight_lineage(
        policy, changed_open, changed_close
    )

    pd.testing.assert_series_equal(baseline.iloc[-1], changed.iloc[-1])
    assert baseline_meta.iloc[-1]["selected_templates"] == changed_meta.iloc[-1]["selected_templates"]
    baseline_last = baseline_lineage[baseline_lineage["date"] == baseline.index[-1]].reset_index(drop=True)
    changed_last = changed_lineage[changed_lineage["date"] == changed.index[-1]].reset_index(drop=True)
    pd.testing.assert_frame_equal(baseline_last, changed_last)


def test_exact_lineage_join_reconstructs_realized_position_and_exact_product_cost():
    from afuture.directional_lineage import (
        build_exact_lineage_attribution,
        build_weight_lineage,
    )

    open_prices, close = _history()
    policy = ExecutionAlignedAggressivePolicy(products=tuple(close.columns))
    weights, meta, lineage = build_weight_lineage(policy, open_prices, close)
    active = weights.iloc[-1]
    product = next(
        product for product, value in active.items() if abs(float(value)) > 1e-15
    )
    day = weights.index[-1]
    target_weight = float(weights.loc[day, product])
    signed_lots = 3 if target_weight > 0 else -3
    events = pd.DataFrame([
        {
            "date": day,
            "kind": "trade",
            "action": "entry",
            "product": product,
            "symbol": f"{product}2609",
            "side": "long" if signed_lots > 0 else "short",
            "lots_before": 0,
            "lots_after": signed_lots,
            "delta_lots": signed_lots,
            "price": 100.0,
            "turnover_notional": 3000.0,
            "transaction_cost": 4.5,
            "gross_pnl": 0.0,
        }
    ])

    result = build_exact_lineage_attribution(
        meta=meta,
        template_product=lineage,
        events=events,
    )
    product_execution = result["product_execution"]
    template_product = result["template_product"]
    row = product_execution[
        (product_execution["date"] == day)
        & (product_execution["product"] == product)
    ].iloc[0]
    assert row["target_weight"] == pytest.approx(target_weight)
    assert row["realized_lots_after"] == signed_lots
    assert row["turnover_notional"] == pytest.approx(3000.0)
    assert row["transaction_cost"] == pytest.approx(4.5)
    assert template_product["transaction_cost"].isna().all()
    assert template_product["turnover_notional"].isna().all()


def test_exact_lineage_carries_position_forward_on_no_trade_days():
    from afuture.directional_lineage import build_exact_lineage_attribution

    dates = pd.to_datetime(["2026-08-21", "2026-08-24"])
    meta = pd.DataFrame(
        {"date": dates, "selected_templates": [("t1",), ("t1",)]}
    )
    lineage = pd.DataFrame([
        {"date": dates[0], "template_id": "t1", "product": "A", "raw_template_weight": 1.0, "contribution_weight": 1.0, "aggregate_weight": 1.0},
        {"date": dates[1], "template_id": "t1", "product": "A", "raw_template_weight": 1.0, "contribution_weight": 1.0, "aggregate_weight": 1.0},
    ])
    events = pd.DataFrame([
        {"date": dates[0], "kind": "trade", "action": "entry", "product": "A", "symbol": "A2609", "side": "long", "lots_before": 0, "lots_after": 2, "delta_lots": 2, "price": 100.0, "turnover_notional": 2000.0, "transaction_cost": 3.0, "gross_pnl": 0.0},
    ])
    result = build_exact_lineage_attribution(
        meta=meta, template_product=lineage, events=events
    )["product_execution"]
    second = result[
        (result["date"] == dates[1]) & (result["product"] == "A")
    ].iloc[0]
    assert second["realized_lots_after"] == 2
    assert second["turnover_notional"] == 0.0
    assert second["transaction_cost"] == 0.0
