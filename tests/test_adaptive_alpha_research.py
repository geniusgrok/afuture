"""Financial and causal boundaries for the offline adaptive research tools."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from adaptive_alpha_research import holding_episodes
from holding_exit_research import trailing_step
from pair_episode_research import paper_episodes, readiness, spread_signal


def fixture_market():
    dates = pd.bdate_range("2024-01-01", periods=70)
    rows = []
    for index, day in enumerate(dates):
        spread = float(index % 2) if index < 62 else 20.0 if index == 62 else 0.0
        for symbol, price, delivery in (
            ("A2411", 1000.0 + spread, "2024-11-15"),
            ("A2501", 1000.0, "2025-01-15"),
        ):
            rows.append(
                {
                    "date": day,
                    "symbol": symbol,
                    "product": "A",
                    "open": price,
                    "close": price,
                    "volume": 10000,
                    "hold": 20000,
                    "delivery": pd.Timestamp(delivery),
                }
            )
    market = pd.DataFrame(rows)
    picks = pd.DataFrame(
        {"date": dates, "product": "A", "symbol_near": "A2411", "symbol_far": "A2501"}
    )
    return dates, market, picks


def test_entry_uses_prior_close_and_both_legs_are_costed():
    dates, market, picks = fixture_market()
    # The signal is short at the prior close, even though today's close is flat.
    episodes, events, _, checks = paper_episodes(
        market, picks, dates[63:], {"A": 10.0}, mechanism="fixture"
    )
    first = episodes.iloc[0]
    assert first.direction == -1 and first.signal_day == dates[62]
    assert first.entry_day == dates[63] and first.complete
    trades = events.loc[(events.episode == first.id) & events.action.isin(["entry", "exit"])]
    assert len(trades) == 4 and set(trades.symbol) == {"A2411", "A2501"}
    assert first.stress_cost == pytest.approx(trades.turnover.sum() * 0.0015)
    assert checks["event_and_price_identity_passed"]
    altered = market.copy()
    altered.loc[(altered.date == dates[63]) & (altered.symbol == "A2411"), "close"] = 1400.0
    changed, _, _, _ = paper_episodes(
        altered, picks, dates[63:64], {"A": 10.0}, mechanism="fixture"
    )
    assert changed.iloc[0].direction == first.direction


def test_missing_held_leg_fails_and_no_terminal_exit_is_invented():
    dates, market, picks = fixture_market()
    episodes, events, _, _ = paper_episodes(
        market, picks, dates[63:64], {"A": 10.0}, mechanism="fixture"
    )
    assert not episodes.iloc[0].complete
    assert "exit" not in set(events.action)
    missing = market.loc[~((market.date == dates[64]) & (market.symbol == "A2411"))]
    with pytest.raises(ValueError, match="missing held-contract"):
        paper_episodes(missing, picks, dates[63:65], {"A": 10.0}, mechanism="fixture")


def test_integer_pair_budget_does_not_allow_one_unfunded_leg():
    dates, market, picks = fixture_market()
    costly = market.copy()
    costly.loc[costly.date == dates[63], "open"] = 10000.0
    episodes, events, audit, _ = paper_episodes(
        costly, picks, dates[63:64], {"A": 10.0}, mechanism="fixture"
    )
    assert episodes.empty and events.empty
    assert audit.iloc[0].reason == "one_pair_exceeds_budget"


def test_invalid_signal_inputs_and_small_sample_are_not_certified():
    with pytest.raises(ValueError, match="63 complete"):
        spread_signal(np.zeros(62), 1.0, 10.0)
    with pytest.raises(ValueError, match="finite and positive"):
        spread_signal(np.zeros(63), float("nan"), 10.0)
    assert not readiness([10.0, 20.0, 30.0])["ready"]
    assert not readiness([-10.0, -20.0, -30.0, -40.0])["ready"]


def test_holding_attribution_preserves_open_episode_and_fees():
    events = pd.DataFrame(
        [
            {
                "date": "2024-01-01",
                "product": "A",
                "symbol": "A2411",
                "side": "long",
                "kind": "trade",
                "action": "entry",
                "lots_before": 0,
                "lots_after": 1,
                "transaction_cost": 5.0,
                "gross_pnl": 0.0,
            },
            {
                "date": "2024-01-01",
                "product": "A",
                "symbol": "A2411",
                "side": "long",
                "kind": "pnl",
                "action": "intraday",
                "lots_before": 1,
                "lots_after": 1,
                "transaction_cost": 0.0,
                "gross_pnl": 25.0,
            },
        ]
    )
    output = holding_episodes(events)
    assert output.iloc[0].net_pnl == 20.0
    assert not output.iloc[0].complete and output.iloc[0].exit_day == ""


def test_completed_close_trailing_requires_favorable_move_and_is_symmetric():
    peak, armed, trigger = trailing_step(90.0, 1, 100.0, 100.0, 5.0, False)
    assert not armed and not trigger
    peak, armed, trigger = trailing_step(110.0, 1, 100.0, peak, 5.0, armed)
    assert armed and not trigger
    _, _, trigger = trailing_step(104.0, 1, 100.0, peak, 5.0, armed)
    assert trigger
    peak, armed, trigger = trailing_step(90.0, -1, 100.0, -100.0, 5.0, False)
    assert armed and not trigger
    assert trailing_step(96.0, -1, 100.0, peak, 5.0, armed)[2]
