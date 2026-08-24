import importlib.util
from pathlib import Path

import pandas as pd
import pytest


def _tool():
    path = Path("tools/evaluate_directional_stress80.py")
    spec = importlib.util.spec_from_file_location("evaluate_directional_stress80", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _passing_report():
    return {
        "stress": {
            "full_recent": {"annualized_return": 0.40, "max_drawdown": -0.20},
            "validation": {"annualized_return": 0.10, "max_drawdown": -0.20},
            "oos": {"annualized_return": 0.10, "max_drawdown": -0.20},
        },
        "economics": {
            "stress": {
                "full_recent": {"net_alpha_per_turnover_bps": 13.0}
            }
        },
        "risk": {
            "validation": {"permanent_halt": False, "violations": []},
            "oos": {"permanent_halt": False, "violations": []},
        },
    }


def test_l3a_gate_requires_every_predeclared_condition():
    tool = _tool()
    assert tool.l3a_gate(_passing_report())["passed"] is True

    mutations = [
        (("stress", "full_recent", "annualized_return"), 0.289559),
        (("stress", "validation", "annualized_return"), 0.0),
        (("stress", "oos", "annualized_return"), 0.0),
        (("stress", "validation", "max_drawdown"), -0.300001),
        (("stress", "oos", "max_drawdown"), -0.300001),
        (("economics", "stress", "full_recent", "net_alpha_per_turnover_bps"), 12.2556),
        (("risk", "validation", "permanent_halt"), True),
        (("risk", "oos", "violations"), ["gross cap"]),
    ]
    for path, value in mutations:
        report = _passing_report()
        target = report
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        result = tool.l3a_gate(report)
        assert result["passed"] is False, path
        assert result["reasons"], path


def test_forward_labels_start_after_decision_and_become_available_on_horizon_end():
    tool = _tool()
    index = pd.date_range("2026-01-01", periods=7, freq="B")
    close_returns = pd.DataFrame({"AG": [0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06]}, index=index)

    returns, availability = tool.build_forward_label_panels(
        close_returns,
        horizons=(5,),
    )

    expected = (1.01 * 1.02 * 1.03 * 1.04 * 1.05) - 1.0
    assert returns[5].loc[index[0], "AG"] == pytest.approx(expected)
    assert availability[5].loc[index[0], "AG"] == index[5]
    # A decision on the last available day itself must not be allowed to use the label;
    # strict visibility is separately enforced by completed_opportunities().
    assert pd.isna(returns[5].loc[index[2], "AG"])
