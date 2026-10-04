import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
spec = importlib.util.spec_from_file_location(
    "strategy_rebuild_research", TOOLS / "strategy_rebuild_research.py"
)
assert spec and spec.loader
research = importlib.util.module_from_spec(spec)
spec.loader.exec_module(research)


def test_outer_planner_continues_after_multiple_economic_failures_and_changes_layers():
    history = []
    ids = []
    for _ in range(8):
        proposed = research.next_hypothesis(history)
        ids.append(proposed["id"])
        history.append(
            {
                "spec": proposed,
                "status": "economic_failed",
                "result": {"research_diagnosis": {"gross_edge_in_both_pools": False}},
            }
        )
    assert ids == ["X1", "T1", "T2", "RV1", "OPT1", "REC1", "PG1", "RP1"]
    assert history[4]["spec"]["parent"] == "MATURE1"
    assert research.next_hypothesis(history) is None


def test_allocator_uses_first_registered_gross_positive_parent_not_best_equity():
    history = []
    for name in ("X1", "T1", "T2", "RV1"):
        history.append(
            {
                "spec": {"id": name},
                "status": "economic_failed",
                "result": {"research_diagnosis": {"gross_edge_in_both_pools": name == "T2"}},
            }
        )
    assert research.next_hypothesis(history)["parent"] == "T2"


def test_invalid_curve_evidence_does_not_repeat_dependent_curve_experiments():
    history = [
        {"spec": {"id": "X1"}, "status": "economic_failed", "result": {}},
        {"spec": {"id": "T1"}, "status": "invalid_evidence", "result": {}},
    ]
    proposed = research.next_hypothesis(history)
    assert proposed["id"] == "OPT1" and proposed["parent"] == "MATURE1"


def test_shared_account_combination_requires_two_supported_information_families():
    history = []
    for _ in range(8):
        proposed = research.next_hypothesis(history)
        supported = proposed["id"] in ("X1", "T1")
        history.append(
            {
                "spec": proposed,
                "status": "economic_failed",
                "result": {
                    "research_diagnosis": {
                        "status": "development_component_supported"
                        if supported
                        else "replace_mechanism"
                    }
                },
            }
        )
    assert research.next_hypothesis(history)["parents"] == ["X1", "T1"]


def test_seed_requires_recorded_receipt_unchanged_outputs_and_same_market(tmp_path):
    source, market = tmp_path / "source.py", tmp_path / "market.csv"
    source.write_text("source")
    market.write_text("market")

    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    context = {"sources": {str(source): sha(source)}, "inputs": {str(market): sha(market)}}
    research.execute_chain(
        tmp_path / "campaign",
        lambda history: None if history else {"id": "X1"},
        lambda spec, folder: (folder / "book.csv").write_text("ledger"),
        lambda spec, folder: {"economic_passed": False},
        context=context,
    )
    receipt = next((tmp_path / "campaign/attempts").glob("*/complete.json"))
    assert research.verified_seed_receipt(receipt, {str(market): sha(market)})["spec"]["id"] == "X1"
    with pytest.raises(ValueError, match="market"):
        research.verified_seed_receipt(receipt, {str(market): "0" * 64})
    raw = json.loads(receipt.read_text())
    raw["result"]["economic_passed"] = True
    receipt.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="identity"):
        research.verified_seed_receipt(receipt, {str(market): sha(market)})


def test_curve_features_are_strictly_prior_and_preserve_missing_future_labels():
    calendar = pd.bdate_range("2025-01-01", periods=30)
    rows = []
    for i, day in enumerate(calendar):
        for symbol, offset, delivery in (("M2505", 0, "2025-05-15"), ("M2509", 10, "2025-09-15")):
            rows.append(
                dict(
                    date=day,
                    product="M",
                    symbol=symbol,
                    close=100 + offset + i,
                    volume=2000,
                    hold=10000,
                    delivery=delivery,
                )
            )
    market = pd.DataFrame(rows)
    obs = pd.DataFrame(
        [
            dict(
                entry_day=calendar[25],
                feature_through=calendar[24],
                maturity_day=calendar[-1],
                product="M",
                symbol="M2505",
                volatility=0.01,
                x1=0.5,
                x2=0,
                x3=0,
                feature_status="eligible",
                label_status="unavailable_endpoint",
                gross_return=np.nan,
            )
        ]
    )
    initial = research.build_curve_observations(market, obs, calendar)
    market.loc[market.date >= calendar[25], ["close", "hold"]] *= 100
    changed = research.build_curve_observations(market, obs, calendar)
    pd.testing.assert_frame_equal(initial, changed)
    assert initial.iloc[0].curve_status == "eligible"
    assert np.isnan(initial.iloc[0].gross_return)
