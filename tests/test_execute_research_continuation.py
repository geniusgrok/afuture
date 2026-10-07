"""Structural continuation changes budgets without rewriting acceptance."""

import hashlib
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from execute_research_continuation import (
    account_exposures,
    confidence_budget,
    core_gates,
    next_experiment,
    reference_inputs,
    verify_risk_reasons,
)


def test_missing_quote_halt_cannot_be_used_as_economic_failure():
    daily = pd.DataFrame(
        {
            "risk_reason": [
                None,
                "drawdown limit reached",
                "combined margin ratio would exceed limit",
                "combined cash reserve would fall below limit",
                "contract volume limit reached",
            ]
        }
    )
    verify_risk_reasons(daily)
    daily.loc[1, "risk_reason"] = "missing same-contract next price: A2601"
    with pytest.raises(ValueError, match="non-economic"):
        verify_risk_reasons(daily)


def test_consensus_restores_only_supported_extra_budget_symmetrically():
    dates = pd.bdate_range("2024-01-01", periods=2)
    cost = pd.DataFrame({"A": [0.2, -0.2]}, index=dates)
    survivor = cost * 3
    families = [survivor.copy() for _ in range(4)] + [-survivor, -survivor]
    weights, agreement = confidence_budget(cost, survivor, families)
    assert agreement.A.tolist() == pytest.approx([1 / 3, 1 / 3])
    assert weights.A.tolist() == pytest.approx([1 / 3, -1 / 3])
    opposite, _ = confidence_budget(cost, survivor, [-survivor] * 6)
    pd.testing.assert_frame_equal(opposite, cost)
    full, _ = confidence_budget(cost, survivor, [survivor] * 6)
    pd.testing.assert_frame_equal(full, survivor)
    with pytest.raises(ValueError, match="nested"):
        confidence_budget(cost * 4, survivor, families)
    with pytest.raises(ValueError, match="incomplete family"):
        confidence_budget(cost, survivor, [survivor.iloc[1:]] + families[1:])


def test_comparison_must_match_previously_preserved_original(tmp_path):
    manifest = {}
    for window in ("historical", "recent"):
        for pool in ("full", "exAG"):
            for cost in ("base", "stress"):
                name = f"E1_{window}/E1_{pool}_{cost}/daily.csv"
                path = tmp_path / name
                path.parent.mkdir(parents=True)
                raw = b"date,equity\n2026-01-02,500000\n"
                path.write_bytes(raw)
                manifest[name] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    (tmp_path / "FILES.json").write_text(json.dumps({"files": manifest}))
    assert len(reference_inputs(tmp_path)) == 9
    path.write_bytes(b"changed comparator\n")
    with pytest.raises(ValueError, match="preserved original"):
        reference_inputs(tmp_path)


def test_failure_automatically_selects_a_different_structural_rule():
    first = next_experiment([])
    assert first["id"] == "P1"
    failed = {
        "spec": first,
        "status": "economic_failed",
        "attempt": "/trial/P1",
        "result": {
            "component_net_positive": True,
            "component_protection_observed": False,
        },
    }
    second = next_experiment([failed])
    assert second["id"] == "C1" and second["parent_attempt"] == failed["attempt"]
    another = {
        "spec": second,
        "status": "invalid_evidence",
        "attempt": "/trial/C1",
        "result": {"stage": "validate", "error": "ledger mismatch"},
    }
    mixture = next_experiment([failed, another])
    assert mixture["id"] == "MIX" and mixture["component"] == "P1"
    assert next_experiment([{**failed, "status": "candidate_passed"}]) is None


def test_exhausted_budget_revisions_change_to_relative_sector_signal():
    history = [
        {
            "spec": {"id": candidate},
            "status": "economic_failed",
            "attempt": candidate,
            "result": {
                "component_net_positive": False,
                "component_protection_observed": False,
                "diagnostics": {"exAG_stress": {"gross_profit": -1000}},
            },
        }
        for candidate in ("P1", "C1")
    ]
    assert next_experiment(history)["id"] == "R1"


def test_sector_exposure_reconstructs_integer_fills_and_rejects_missing_gross():
    days = pd.bdate_range("2026-01-01", periods=2)
    daily = pd.DataFrame({"gross_notional": [1000.0, 0.0]}, index=days)
    events = pd.DataFrame(
        {
            "date": days,
            "kind": "trade",
            "symbol": "A2605",
            "product": "A",
            "lots_after": [2, 0],
        }
    )
    market = pd.DataFrame({"date": days, "symbol": "A2605", "close": [50.0, 51.0]})
    actual = account_exposures(daily, events, market)
    assert actual.agri_soft_net.tolist() == [1000.0, 0.0]
    daily.iloc[0, 0] = 999.0
    with pytest.raises(ValueError, match="exposure does not reconcile"):
        account_exposures(daily, events, market)


def test_low_drawdown_cannot_compensate_for_original_return_floor_failure():
    original, candidate = {}, {}
    for pool in ("full", "exAG"):
        for cost in ("base", "stress"):
            key = pool + "_" + cost
            original["B0_" + key] = {"final_equity": 400000.0, "mdd": 0.25}
            candidate[key] = {"final_equity": 600000.0, "net_profit": 100000.0, "mdd": 0.01}
    passed, cells = core_gates(candidate, original)
    assert not passed
    assert not cells["full_base"]["return_floor"] and cells["full_base"]["drawdown_cap"]
