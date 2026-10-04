"""Causal selection and fixed sector budgets for the registered R1 revision."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from relative_sector_research import SECTOR_GROUPS, relative_sector_weights


def prices():
    index = pd.bdate_range("2024-01-01", periods=95)
    return pd.DataFrame(
        {
            product: 100 * np.exp(rate * np.arange(len(index)))
            for product, rate in {
                "AG": 0.003,
                "AL": 0.002,
                "CU": 0.001,
                "A": 0.003,
                "C": 0.001,
                "BU": -0.001,
                "EB": -0.002,
                "FG": 0.003,
                "HC": 0.001,
            }.items()
        },
        index=index,
    )


def test_four_sector_targets_have_fixed_opposing_budgets_after_complete_history():
    weights, audit = relative_sector_weights(prices())
    assert weights.iloc[:64].eq(0).all().all()
    assert weights.iloc[-1].sum() == 0 and weights.iloc[-1].abs().sum() == 2
    assert weights.iloc[-1].AG == 0.25 and weights.iloc[-1].CU == -0.25
    # Relative selection includes opposing positions even when both prices trend down.
    assert weights.iloc[-1].BU == 0.25 and weights.iloc[-1].EB == -0.25
    for group in SECTOR_GROUPS.values():
        present = weights.columns.intersection(group)
        assert weights[present].abs().sum(axis=1).le(0.5).all()
    chosen = audit.loc[audit.reason == "selected"]
    assert (chosen.source_end < chosen.target_day).all()


def test_current_and_future_prices_cannot_change_current_or_previous_targets():
    original = prices()
    old, _ = relative_sector_weights(original)
    target = original.index[70]
    assert target.weekday() == 0
    changed = original.copy()
    changed.loc[target:, "CU"] *= 10
    new, _ = relative_sector_weights(changed)
    pd.testing.assert_frame_equal(old.loc[:target], new.loc[:target])
    # A known change after Monday's decision takes effect only the next week.
    pd.testing.assert_frame_equal(old.iloc[70:75], new.iloc[70:75])
    assert new.iloc[75].CU == 0.25 and old.iloc[75].CU == -0.25


def test_exag_is_removed_before_ranking_and_ties_idle_whole_sector():
    original = prices()
    full, _ = relative_sector_weights(original)
    excluded, _ = relative_sector_weights(original, pool="exAG")
    assert full.iloc[-1].AG == 0.25
    assert excluded.AG.eq(0).all()
    assert excluded.iloc[-1].AL == 0.25 and excluded.iloc[-1].CU == -0.25
    original["AL"] = original["AG"]
    tied, audit = relative_sector_weights(original)
    assert tied[["AG", "AL", "CU"]].eq(0).all().all()
    assert audit.loc[audit.sector == "metals", "reason"].iloc[-1] == "tied_extreme"


def test_missing_intermediate_price_is_not_backfilled_into_rank():
    original = prices()[["AG", "AL"]]
    original.iloc[40, 0] = np.nan
    weights, audit = relative_sector_weights(original)
    assert weights.eq(0).all().all()
    assert audit.loc[audit.sector == "metals", "eligible_products"].max() == 1


def test_holiday_week_rebalances_on_first_actual_session_and_rejects_duplicates():
    original = prices().drop(pd.Timestamp("2024-04-08"))
    _, audit = relative_sector_weights(original)
    assert pd.Timestamp("2024-04-09") in audit.target_day.to_list()
    assert pd.Timestamp("2024-04-10") not in audit.target_day.to_list()
    with pytest.raises(ValueError, match="unique ordered"):
        relative_sector_weights(pd.concat([original, original.iloc[-1:]]))
