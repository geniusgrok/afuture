import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

spec = importlib.util.spec_from_file_location(
    "aggressive_directional",
    Path(__file__).with_name("evaluate_aggressive_directional.py"),
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def synthetic_panel(periods: int = 180) -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=periods, freq="B")
    even = np.arange(periods) % 2 == 0
    returns = {
        "A": np.where(even, 0.012, 0.008),
        "B": np.where(even, -0.008, -0.012),
        "C": np.where(even, 0.003, 0.001),
        "D": np.where(even, -0.001, -0.003),
    }
    rows = []
    for product, values in returns.items():
        close = 100.0 * np.cumprod(1.0 + values)
        rows.extend(
            {"date": day, "product": product, "close": price}
            for day, price in zip(dates, close, strict=True)
        )
    return pd.DataFrame(rows)


def test_directional_template_is_lagged_and_gross_capped() -> None:
    returns, _ = module.build_panel(synthetic_panel())
    template = module.DirectionalTemplate("breakout", 20, 0, 1, 5, 2.0)
    gross_pnl, turnover, audit = module.simulate_path(returns, template)
    assert gross_pnl.iloc[:20].abs().sum() == 0.0
    assert gross_pnl.iloc[30:].mean() > 0.0
    assert turnover.sum() >= 0.0
    assert audit
    assert max(row["gross"] for row in audit) <= 2.0 + 1e-12


def test_cost_is_monotonic() -> None:
    returns, _ = module.build_panel(synthetic_panel())
    template = module.DirectionalTemplate("breakout", 20, 0, 1, 5, 2.0)
    gross_pnl, turnover, _ = module.simulate_path(returns, template)
    base = module.apply_cost(gross_pnl, turnover, 5.0)
    stress = module.apply_cost(gross_pnl, turnover, 15.0)
    assert stress.sum() <= base.sum() + 1e-12


def test_template_space_is_bounded_and_low_leverage() -> None:
    templates = module.directional_templates()
    families = {item.family for item in templates}
    assert {
        "tsmom",
        "momentum",
        "reversal",
        "moving_average",
        "breakout",
        "acceleration",
    } <= families
    assert len(templates) <= 700
    assert all(item.gross_leverage <= 2.0 for item in templates)


def test_aggressive_fit_declares_selection_bias() -> None:
    raw = synthetic_panel(320)
    report = module.evaluate(raw)
    assert report["pristine_final_oos"] is False
    assert report["selection"]["selection_bias"] == "full_recent_target_fit"
    assert report["selection"]["effective_gross_leverage"] <= 2.0 + 1e-12
    assert report["target"]["annualized_return"] == 1.0
    assert report["target"]["max_drawdown"] == -0.30


if __name__ == "__main__":
    test_directional_template_is_lagged_and_gross_capped()
    test_cost_is_monotonic()
    test_template_space_is_bounded_and_low_leverage()
    test_aggressive_fit_declares_selection_bias()
