import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

spec = importlib.util.spec_from_file_location(
    "return_target",
    Path(__file__).with_name("evaluate_return_target_portfolio.py"),
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def synthetic_returns(periods: int = 180) -> tuple[pd.DataFrame, dict[str, str]]:
    dates = pd.date_range("2024-01-01", periods=periods, freq="B")
    even = np.arange(len(dates)) % 2 == 0
    values = pd.DataFrame(
        {
            "A": np.where(even, 0.011, 0.009),
            "B": np.where(even, 0.004, 0.002),
            "C": np.where(even, -0.001, -0.003),
            "D": np.where(even, -0.008, -0.010),
        },
        index=dates,
    )
    return values, {name: "DCE" for name in values.columns}


def test_causal_momentum_pairing_and_gross_cap() -> None:
    returns, exchange_map = synthetic_returns()
    template = module.AlphaTemplate(
        family="momentum",
        slow=20,
        fast=0,
        vol_window=20,
        rebalance=1,
        max_pairs=1,
        gross_leverage=1.0,
    )
    series, audit = module.simulate_template(
        returns,
        exchange_map,
        template,
        cost_bps=0.0,
    )
    assert series.iloc[:20].abs().sum() == 0.0
    assert series.iloc[25:].mean() > 0.0
    rows = [row for row in audit if row["gross"] is not None]
    assert rows
    assert max(float(row["gross"]) for row in rows) <= 1.0 + 1e-12
    for row in rows:
        legs = list(row.get("legs", []))
        assert len(legs) == len(set(legs))


def test_turnover_cost_reduces_return() -> None:
    returns, exchange_map = synthetic_returns()
    template = module.AlphaTemplate("momentum", 20, 0, 20, 1, 1, 1.0)
    free, _ = module.simulate_template(returns, exchange_map, template, cost_bps=0.0)
    costed, _ = module.simulate_template(returns, exchange_map, template, cost_bps=30.0)
    assert costed.sum() < free.sum()


def test_primary_grid_is_bounded_and_multi_family() -> None:
    templates = module.primary_templates()
    families = {item.family for item in templates}
    assert {"momentum", "reversal", "slow_fast", "breakout"} <= families
    assert all(item.gross_leverage <= 2.0 for item in templates)
    assert len(templates) <= 160


def test_template_selection_uses_only_calibration_slice() -> None:
    dates = pd.date_range("2025-01-01", periods=180, freq="B")
    left = pd.Series([0.01] * 120 + [-0.01] * 60, index=dates)
    right = pd.Series([0.002] * 180, index=dates)
    selected = module.choose_templates(
        {"left": left, "right": right},
        start=dates[0],
        end=dates[119],
        count=1,
    )
    assert selected == ["left"]


def test_fail_closed_when_no_positive_calibration_template() -> None:
    dates = pd.date_range("2025-01-01", periods=80, freq="B")
    streams = {
        "left": pd.Series([-0.002] * 80, index=dates),
        "right": pd.Series([-0.001] * 80, index=dates),
    }
    assert module.choose_templates(streams, start=dates[0], end=dates[-1], count=2) == []


def test_dynamic_rotation_adapts_using_trailing_history() -> None:
    dates = pd.date_range("2025-01-01", periods=160, freq="B")
    streams = {
        "early": pd.Series([0.010] * 80 + [-0.010] * 80, index=dates),
        "late": pd.Series([-0.005] * 80 + [0.008] * 80, index=dates),
    }
    rotated, audit = module.dynamic_rotate(
        streams,
        meta_lookback=20,
        rebalance=5,
        count=1,
        switch_cost_bps=0.0,
    )
    assert rotated.iloc[:20].abs().sum() == 0.0
    assert rotated.iloc[30:70].mean() > 0.0
    assert rotated.iloc[110:].mean() > 0.0
    assert any(row["selected"] == ["early"] for row in audit[:80])
    assert any(row["selected"] == ["late"] for row in audit[80:])


def test_build_panel_infers_exchange_without_future_metadata() -> None:
    raw = pd.DataFrame(
        [
            {"date": "2025-01-02", "product": "A", "close": 100.0},
            {"date": "2025-01-03", "product": "A", "close": 101.0},
            {"date": "2025-01-02", "product": "RB", "close": 3000.0},
            {"date": "2025-01-03", "product": "RB", "close": 3030.0},
        ]
    )
    returns, exchanges, coverage = module.build_panel(raw)
    assert exchanges["A"] == "DCE"
    assert exchanges["RB"] == "SHFE"
    assert list(returns.columns) == ["A", "RB"]
    assert coverage["products"] == 2


def test_evaluate_reports_explicit_non_pristine_target_and_leverage_cap() -> None:
    dates = pd.date_range("2025-01-01", periods=320, freq="B")
    even = np.arange(len(dates)) % 2 == 0
    returns = {
        "A": np.where(even, 0.006, 0.004),
        "M": np.where(even, 0.003, 0.001),
        "P": np.where(even, -0.001, -0.003),
        "Y": np.where(even, -0.004, -0.006),
    }
    rows = []
    for product, values in returns.items():
        close = 100.0 * np.cumprod(1.0 + values)
        rows.extend(
            {"date": day, "product": product, "close": price}
            for day, price in zip(dates, close, strict=True)
        )
    report = module.evaluate(pd.DataFrame(rows))
    assert report["pristine_final_oos"] is False
    assert report["template_count"] == len(module.primary_templates())
    assert report["target"]["annualized_return"] == 1.0
    assert report["target"]["gross_leverage_cap"] == 2.0
    assert report["selection"]["effective_gross_leverage"] <= 2.0 + 1e-12


if __name__ == "__main__":
    test_causal_momentum_pairing_and_gross_cap()
    test_turnover_cost_reduces_return()
    test_primary_grid_is_bounded_and_multi_family()
    test_template_selection_uses_only_calibration_slice()
    test_fail_closed_when_no_positive_calibration_template()
    test_dynamic_rotation_adapts_using_trailing_history()
    test_build_panel_infers_exchange_without_future_metadata()
    test_evaluate_reports_explicit_non_pristine_target_and_leverage_cap()
