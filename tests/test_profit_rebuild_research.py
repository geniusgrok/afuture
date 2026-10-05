import copy
import importlib.util
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
SPEC = importlib.util.spec_from_file_location(
    "profit_rebuild_research", TOOLS / "profit_rebuild_research.py"
)
assert SPEC and SPEC.loader
research = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(research)


def results():
    parent = {
        "any_risk_halt": False,
        "metrics": {
            "historical": {
                pool + "_stress": {"net_profit": 10000, "mdd": 0.1} for pool in ("full", "exAG")
            },
            "recent": {
                pool + "_stress": {"net_profit": 100, "mdd": 0.01} for pool in ("full", "exAG")
            },
        },
    }
    improved = copy.deepcopy(parent)
    for cell in improved["metrics"]["historical"].values():
        cell["net_profit"] = 15000
    for cell in improved["metrics"]["recent"].values():
        cell["net_profit"] = 200
    return parent, improved


def test_cash_improvement_never_promotes_a_control_or_claims_final_goal():
    parent, improved = results()
    assessed = research.profit_assessment(improved, parent)
    assert assessed["paired_profit_component_achieved"]
    assert not assessed["economic_goal_achieved"] and not assessed["independent_sample"]
    control = research.profit_assessment(improved, parent, control_only=True)
    assert not control["paired_profit_component_achieved"] and not control["eligible_profit_claim"]


@pytest.mark.parametrize("failure", ["recent_empty", "exAG_loses", "higher_drawdown", "halted"])
def test_one_failed_cash_requirement_blocks_a_substantive_profit_claim(failure):
    parent, improved = results()
    if failure == "recent_empty":
        improved["metrics"]["recent"]["full_stress"]["net_profit"] = 0
    elif failure == "exAG_loses":
        improved["metrics"]["historical"]["exAG_stress"]["net_profit"] = -1
    elif failure == "higher_drawdown":
        improved["metrics"]["historical"]["full_stress"]["mdd"] = 0.10001
    else:
        improved["any_risk_halt"] = True
    assert not research.profit_assessment(improved, parent)["paired_profit_component_achieved"]
