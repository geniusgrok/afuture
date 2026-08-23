from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import types

import numpy as np
import pandas as pd

from afuture.directional import DirectionalConfig
from afuture.directional_opportunity_lineage import build_opportunity_weight_lineage
from afuture.execution_aligned_policy import ExecutionAlignedAggressivePolicy
from afuture.opportunity_aligned_policy import OpportunityAlignedAggressivePolicy
from afuture.opportunity_aligned_runtime import (
    OpportunityAlignedDirectionalPortfolioManager,
    OpportunitySignalHistory,
    SinaOpportunityOHLCVOIProvider,
)
from afuture.models import AccountSnapshot
from afuture.risk import RiskConfig, RiskManager


NOW = datetime(2026, 8, 24, 13, 1, tzinfo=timezone.utc)


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


def test_sina_opportunity_provider_preserves_volume_and_hold(monkeypatch):
    dates = pd.date_range("2026-01-01", periods=4, freq="B")
    fake = types.ModuleType("akshare")
    fake.futures_zh_daily_sina = lambda symbol: pd.DataFrame(
        {
            "date": dates,
            "open": [100.0, 101.0, 102.0, 103.0],
            "close": [101.0, 102.0, 103.0, 104.0],
            "volume": [1000.0, 1100.0, 1200.0, 1300.0],
            "hold": [10000.0, 10100.0, 10200.0, 10300.0],
        }
    )
    monkeypatch.setitem(sys.modules, "akshare", fake)

    frame = SinaOpportunityOHLCVOIProvider._load_one("A")

    assert list(frame.columns) == ["open", "close", "volume", "hold"]
    assert float(frame.iloc[-1]["volume"]) == 1300.0
    assert float(frame.iloc[-1]["hold"]) == 10300.0


class _Provider:
    def __init__(self):
        open_prices, close, volume, open_interest = _history(180)
        self.history = OpportunitySignalHistory(
            open_prices,
            close,
            volume=volume,
            open_interest=open_interest,
        )

    def load(self, products):
        return self.history


class _Policy:
    def __init__(self):
        self.activity = None

    def target_weights(
        self,
        open_prices,
        close,
        *,
        volume=None,
        open_interest=None,
    ):
        self.activity = (volume.copy(), open_interest.copy())
        assert open_prices.index.equals(close.index)
        assert volume.index.equals(close.index)
        assert open_interest.index.equals(close.index)
        return {"A": 1.0}


class _Broker:
    def is_ready(self):
        return True

    def get_account(self):
        return AccountSnapshot(
            balance=100000,
            equity=100000,
            available=100000,
            margin=0,
            realized_pnl=0,
            unrealized_pnl=0,
            trading_day="20260825",
        )

    def get_positions(self):
        return []

    def get_active_orders(self):
        return []


def test_opportunity_runtime_passes_completed_activity_to_policy_synthetic_target():
    policy = _Policy()
    manager = OpportunityAlignedDirectionalPortfolioManager(
        DirectionalConfig(
            enabled=True,
            products=("A", "M", "RB", "CU"),
            exchanges=("DCE",),
            signal_max_age_hours=20000.0,
        ),
        _Broker(),
        RiskManager(RiskConfig()),
        signal_provider=_Provider(),
        policy=policy,
    )

    history = manager._load_signal(NOW)
    weights = manager._next_target_weights(history)

    assert weights == {"A": 1.0}
    passed_volume, passed_oi = policy.activity
    assert len(passed_volume) == len(history.close) + 1
    assert len(passed_oi) == len(history.close) + 1
    pd.testing.assert_series_equal(
        passed_volume.iloc[-1], history.volume.iloc[-1], check_names=False
    )
    pd.testing.assert_series_equal(
        passed_oi.iloc[-1], history.open_interest.iloc[-1], check_names=False
    )


def test_opportunity_l4_generator_matches_production_policy(monkeypatch):
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
