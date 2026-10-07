from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
spec = importlib.util.spec_from_file_location(
    "core_rebuild_research", TOOLS / "core_rebuild_research.py"
)
assert spec is not None and spec.loader is not None
research = importlib.util.module_from_spec(spec)
spec.loader.exec_module(research)


def test_market_tape_uses_prior_activity_and_same_contract_marks():
    dates = pd.date_range("2025-01-06", periods=3, freq="B")
    rows = []
    for i, day in enumerate(dates):
        for symbol, closes, holds in (
            ("M2505", (100.0, 110.0, 111.0), (10000, 10000, 10000)),
            ("M2509", (200.0, 202.0, 204.0), (9000, 11000, 11000)),
        ):
            rows.append(
                {
                    "date": day,
                    "symbol": symbol,
                    "product": "M",
                    "open": closes[i],
                    "high": closes[i],
                    "low": closes[i],
                    "close": closes[i],
                    "hold": holds[i],
                    "volume": 2000,
                    "delivery": "2025-05-15",
                }
            )
    market = pd.DataFrame(rows)
    result, audit = research.market_return_tape(market, dates)
    assert np.isnan(result.iloc[0, 0])
    assert np.isclose(result.iloc[1, 0], 0.10)
    assert np.isclose(result.iloc[2, 0], 204 / 202 - 1)
    assert audit.symbol.tolist() == ["M2505", "M2509"]
    changed = market.copy()
    changed.loc[changed.date == dates[-1], ["hold", "volume"]] = 900000
    pd.testing.assert_frame_equal(research.market_return_tape(changed, dates)[0], result)


def test_missing_future_mark_is_preserved_without_reselecting_contract():
    dates = pd.date_range("2025-01-06", periods=2, freq="B")
    market = pd.DataFrame(
        [
            {
                "date": dates[0],
                "symbol": "M2505",
                "product": "M",
                "open": 100,
                "high": 100,
                "low": 100,
                "close": 100,
                "hold": 10000,
                "volume": 2000,
                "delivery": "2025-05-15",
            },
            {
                "date": dates[1],
                "symbol": "M2509",
                "product": "M",
                "open": 110,
                "high": 110,
                "low": 110,
                "close": 110,
                "hold": 20000,
                "volume": 5000,
                "delivery": "2025-09-15",
            },
        ]
    )
    result, audit = research.market_return_tape(market, dates)
    assert result.isna().all().all()
    assert audit.iloc[0].symbol == "M2505"
    assert audit.iloc[0].status == "missing_same_contract_mark"


def test_intermediate_improvement_does_not_require_final_wealth_target():
    def result(profit, dd):
        cell = {
            "halted": False,
            "net_profit": profit,
            "mdd": dd,
            "gross_pnl": profit + 10,
            "fees": 10,
        }
        return {
            "economic_passed": False,
            "metrics": {"historical": {"full_stress": cell, "exAG_stress": cell}},
        }

    assessment = research.stage_assessment(result(-10, 0.1), result(-20, 0.2))
    assert assessment["status"] == "retain_for_rebuild"
    assert assessment["final_economic_goal_passed"] is False
    assert (
        research.stage_assessment(result(-30, 0.3), result(-20, 0.2))["status"]
        == "replace_mechanism"
    )
