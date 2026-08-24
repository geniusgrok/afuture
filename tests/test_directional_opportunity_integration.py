from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from afuture.directional_opportunity_lineage import build_opportunity_weight_lineage
from afuture.execution_aligned_policy import ExecutionAlignedAggressivePolicy
from afuture.opportunity_aligned_policy import OpportunityAlignedAggressivePolicy


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
    indexer = np.arange(periods, dtype=float)
    volume = pd.DataFrame(
        {
            "A": 1000.0 + indexer * 11.0,
            "M": 3000.0 - indexer * 5.0,
            "RB": 1800.0 + (indexer % 17) * 90.0,
            "CU": 2200.0 + (indexer % 9) * 130.0,
        },
        index=dates,
    )
    open_interest = pd.DataFrame(
        {
            "A": 12000.0 + indexer * 31.0,
            "M": 26000.0 - indexer * 21.0,
            "RB": 18000.0 + (indexer % 23) * 210.0,
            "CU": 15000.0 + (indexer % 13) * 170.0,
        },
        index=dates,
    )
    return open_prices, close, volume, open_interest


def test_opportunity_policy_is_exact_core_fallback_and_risk_only_deemphasis():
    open_prices, close, volume, open_interest = _history()
    products = tuple(close.columns)
    core = ExecutionAlignedAggressivePolicy(products=products)
    policy = OpportunityAlignedAggressivePolicy(products=products)

    raw = core.weight_history(open_prices, close)
    fallback = policy.weight_history(open_prices, close)
    adjusted = policy.weight_history(
        open_prices,
        close,
        volume=volume,
        open_interest=open_interest,
    )

    pd.testing.assert_frame_equal(fallback, raw)
    assert bool((adjusted.abs() <= raw.abs() + 1e-12).all().all())
    assert bool(((adjusted * raw) >= -1e-12).all().all())
    assert float(adjusted.abs().sum(axis=1).max()) <= 2.0 + 1e-12
    assert not adjusted.equals(raw)


def test_opportunity_policy_is_causal_for_price_volume_and_open_interest():
    open_prices, close, volume, open_interest = _history()
    policy = OpportunityAlignedAggressivePolicy(products=tuple(close.columns))
    baseline = policy.weight_history(
        open_prices,
        close,
        volume=volume,
        open_interest=open_interest,
    )

    changed_open = open_prices.copy()
    changed_close = close.copy()
    changed_volume = volume.copy()
    changed_oi = open_interest.copy()
    changed_open.iloc[-1] *= [2.0, 0.5, 1.8, 0.6]
    changed_close.iloc[-1] *= [3.0, 0.4, 2.5, 0.5]
    changed_volume.iloc[-1] *= [10.0, 0.1, 8.0, 0.2]
    changed_oi.iloc[-1] *= [0.1, 10.0, 0.2, 8.0]
    changed = policy.weight_history(
        changed_open,
        changed_close,
        volume=changed_volume,
        open_interest=changed_oi,
    )

    pd.testing.assert_series_equal(baseline.iloc[-1], changed.iloc[-1])


def test_opportunity_lineage_closes_exact_adjusted_product_weights():
    open_prices, close, volume, open_interest = _history()
    policy = OpportunityAlignedAggressivePolicy(products=tuple(close.columns))

    actual, meta, lineage = build_opportunity_weight_lineage(
        policy,
        open_prices,
        close,
        volume=volume,
        open_interest=open_interest,
    )
    expected = policy.weight_history(
        open_prices,
        close,
        volume=volume,
        open_interest=open_interest,
    )

    pd.testing.assert_frame_equal(actual, expected)
    reconstructed = (
        lineage.groupby(["date", "product"], sort=True)["contribution_weight"]
        .sum()
        .unstack("product")
        .reindex(index=actual.index, columns=actual.columns)
        .fillna(0.0)
    )
    pd.testing.assert_frame_equal(reconstructed, actual, check_names=False)
    assert {"opportunity_scale", "aggregate_weight"} <= set(lineage.columns)
    assert lineage["opportunity_scale"].dropna().between(0.75, 1.0).all()
    assert not meta.empty


def test_opportunity_l4_generator_matches_research_policy(monkeypatch):
    tools_dir = Path(__file__).resolve().parents[1] / "tools"
    monkeypatch.syspath_prepend(str(tools_dir))
    import evaluate_opportunity_aligned_target as l4

    open_prices, close, volume, open_interest = _history()
    rows = []
    for timestamp in close.index:
        for product in close.columns:
            rows.append(
                {
                    "date": timestamp,
                    "product": product,
                    "open": float(open_prices.loc[timestamp, product]),
                    "close": float(close.loc[timestamp, product]),
                    "volume": float(volume.loc[timestamp, product]),
                    "hold": float(open_interest.loc[timestamp, product]),
                }
            )
    continuous = pd.DataFrame(rows)
    products = tuple(sorted(close.columns))
    monkeypatch.setattr(l4, "REQUIRED_PRODUCTS", products)

    generated = l4.generate_execution_signal_weights(continuous)
    expected = OpportunityAlignedAggressivePolicy(products=products).weight_history(
        open_prices.reindex(columns=products),
        close.reindex(columns=products),
        volume=volume.reindex(columns=products),
        open_interest=open_interest.reindex(columns=products),
    )
    pd.testing.assert_frame_equal(
        generated,
        expected,
        check_names=False,
        check_freq=False,
    )
