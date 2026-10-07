"""Causal attribution and execution-data boundaries for the offline selector."""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from product_attribution_research import product_attributed_targets


def test_product_losses_do_not_disqualify_another_products_history():
    dates = pd.date_range("2020-01-01", periods=30)
    opens = pd.DataFrame(100.0, index=dates, columns=["A", "B"])
    close = opens.copy()
    close["A"] = 98.0
    close["B"] = 101.0
    path = pd.DataFrame(1.0, index=dates, columns=opens.columns)
    result = product_attributed_targets(opens, close, {"one": path, "duplicate": path})
    assert result.A.eq(0).all()
    assert result.B.iloc[11:].eq(1 / 3).all()
    # Duplicate templates cannot consume additional product slots or refill cash.
    assert result.abs().sum(axis=1).max() == 1 / 3
    changed = close.copy()
    changed.loc[dates[20] :, "B"] = 90.0
    replay = product_attributed_targets(opens, changed, {"one": path, "duplicate": path})
    pd.testing.assert_frame_equal(result.loc[: dates[20]], replay.loc[: dates[20]])


def test_active_invalid_quote_remains_rejected():
    dates = pd.date_range("2020-01-01", periods=15)
    prices = pd.DataFrame(100.0, index=dates, columns=["A"])
    path = prices * 0 + 1
    invalid = prices.copy()
    invalid.iloc[3, 0] = 0
    with pytest.raises(ValueError, match="non_positive_open"):
        product_attributed_targets(invalid, prices, {"one": path})
