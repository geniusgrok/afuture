from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
spec = importlib.util.spec_from_file_location(
    "mature_forecast_research", TOOLS / "mature_forecast_research.py"
)
assert spec is not None and spec.loader is not None
research = importlib.util.module_from_spec(spec)
spec.loader.exec_module(research)


def inputs():
    calendar = pd.date_range("2025-01-01", periods=40, freq="B")
    price = 100 + np.arange(40) / 5 + np.sin(np.arange(40))
    market = pd.DataFrame(
        {
            "date": calendar,
            "symbol": "M2509",
            "product": "M",
            "open": price,
            "high": price + 2,
            "low": price - 2,
            "close": price + 0.5,
            "hold": 10000 + np.arange(40),
            "volume": 2000,
        }
    )
    selected = pd.DataFrame(
        {
            "date": calendar[25:],
            "selection_through": calendar[24:-1],
            "symbol": "M2509",
            "product": "M",
        }
    )
    return market, selected, calendar


def test_features_and_volatility_are_known_before_entry_and_labels_mature_later():
    market, selected, calendar = inputs()
    original = research.build_observations(market, selected, calendar)
    row = original.iloc[0]
    assert row.feature_status == "eligible"
    assert row.feature_through == calendar[24]
    assert row.maturity_day == calendar[30]
    assert np.isclose(row.gross_return, market.open.iloc[30] / market.open.iloc[25] - 1)
    changed = market.copy()
    changed.loc[changed.date >= calendar[25], ["open", "high", "low", "close"]] *= 1.5
    mutated = research.build_observations(changed, selected, calendar).iloc[0]
    for column in ("x1", "x2", "x3", "volatility"):
        assert mutated[column] == row[column]
    assert original.tail(5).maturity_day.isna().all()


def test_missing_endpoint_retains_selected_observation_and_does_not_fill_label():
    market, selected, calendar = inputs()
    missing = market.loc[market.date != calendar[30]]
    rows = research.build_observations(missing, selected.iloc[:1], calendar)
    assert len(rows) == 1
    assert rows.iloc[0].symbol == "M2509"
    assert rows.iloc[0].feature_status == "eligible"
    assert rows.iloc[0].label_status == "unavailable_endpoint"
    assert np.isnan(rows.iloc[0].gross_return)
