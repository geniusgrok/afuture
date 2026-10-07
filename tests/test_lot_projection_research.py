"""Integer projection preserves money, causality and inherited risk authority."""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from lot_projection_research import LotProjectionAccount, project_integer_targets


def project(weights, *, prices=None, multipliers=None, selected=None, equity=1000.0):
    return project_integer_targets(
        equity=equity,
        product_weights=weights,
        product_open_prices=prices or {p: 10.0 for p in weights},
        selected_symbols=selected or {p: p + "2411" for p in weights},
        multipliers=multipliers or {p: 10.0 for p in weights},
    )


def test_projection_uses_squared_rmb_error_and_keeps_original_total_budget():
    # Two 0.6-lot requests can fund one lot, and the deterministic tie keeps A.
    result = project({"C": -0.06, "A": 0.06})
    assert result.lots == {"A2411": 1}
    assert result.floor_notional == 0 and result.projected_notional == 100
    assert result.projected_notional <= result.original_budget == 120
    assert result.projected_error < result.floor_error
    assert result.weights["C"] == 0 and result.weights["A"] == pytest.approx(0.1)
    assert project({"A": -0.06, "C": 0.06}).lots == {"A2411": -1}
    # Budget is not spent when every ceiling worsens the intended exposure.
    assert project({"A": 0.04, "C": -0.04, "M": 0.04}).lots == {}


def test_real_multipliers_missing_contracts_and_lot_cap_are_respected():
    result = project(
        {"A": 0.06, "C": 0.06, "TA": 0.06},
        prices={"A": 10.0, "C": 10.0, "TA": 10.0},
        multipliers={"A": 10, "C": 10, "TA": 5},
    )
    assert result.lots == {"TA2411": 1, "A2411": 1}
    assert result.projected_notional == 150
    missing = project({"A": 0.06, "C": 0.06}, selected={"A": "A2411"})
    assert missing.lots == {} and missing.unavailable_notional == 60
    assert missing.weights["C"] == 0.06  # Original unavailable-contract path remains visible.
    capped = project({"A": 2.0}, prices={"A": 1.0}, multipliers={"A": 1})
    assert capped.lots == {"A2411": 35} and capped.volume_clipping_notional == 1965
    with pytest.raises(ValueError, match="2x"):
        project({"A": 2.1})
    with pytest.raises(ValueError, match="multiplier"):
        project({"A": 0.1}, multipliers={"A": 0})


def market_fixture():
    dates = pd.bdate_range("2024-01-01", periods=24)
    return pd.DataFrame(
        [
            {
                "date": day,
                "symbol": symbol,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
            }
            for day in dates
            for symbol in ("A2411", "C2411")
        ]
    ), dates


def target_kwargs():
    return {
        "equity": 500000.0,
        "product_weights": {"A": 0.0012, "C": 0.0012},
        "product_open_prices": {"A": 100.0, "C": 100.0},
        "selected_symbols": {"A": "A2411", "C": "C2411"},
        "current_lots": {},
    }


def test_original_hhi_and_drawdown_reserve_can_block_projected_new_risk():
    market, dates = market_fixture()
    kwargs = target_kwargs()
    hhi = LotProjectionAccount(market, completed_concentrations=[1.0])
    hhi.observe_target_state(day=dates[-1], product_weights=kwargs["product_weights"])
    target = hhi.target_lot_stages(**kwargs)
    assert target.raw_integer_lots == {"A2411": 1} and target.final_lots == {}
    assert hhi.projection_audit[-1]["concentration_freeze"]

    reserve = LotProjectionAccount(market)
    reserve.observe_target_state(day=dates[-1], product_weights=kwargs["product_weights"])
    target = reserve.target_lot_stages(**kwargs, completed_returns=(-0.26,))
    assert target.raw_integer_lots == {"A2411": 1} and target.final_lots == {}
    assert reserve.projection_audit[-1]["reserve_freeze"]


def test_future_prices_cannot_change_projection_or_holding_exit_decision():
    market, dates = market_fixture()
    kwargs = target_kwargs()
    kwargs["current_lots"] = {"A2411": 1}
    day = dates[-2]
    changed = market.copy()
    changed.loc[changed.date >= day, ["high", "low", "close"]] = [1100.0, 1.0, 1000.0]
    results = []
    for data in (market, changed):
        account = LotProjectionAccount(data)
        account.observe_target_state(day=day, product_weights=kwargs["product_weights"])
        results.append(account.target_lot_stages(**kwargs))
    assert results[0] == results[1]


def test_projection_does_not_resurrect_an_e1_exit_or_delay_zero_target():
    market, dates = market_fixture()
    kwargs = target_kwargs()
    kwargs["current_lots"] = {"A2411": 1}
    account = LotProjectionAccount(market)
    account.observe_target_state(day=dates[-1], product_weights=kwargs["product_weights"])
    account.blocked["A"] = 1
    result = account.target_lot_stages(**kwargs)
    assert result.raw_integer_lots == {"A2411": 1} and result.final_lots == {}
    kwargs["product_weights"] = {"A": 0.0, "C": 0.0}
    account.observe_target_state(day=dates[-1], product_weights=kwargs["product_weights"])
    assert account.target_lot_stages(**kwargs).final_lots == {}
