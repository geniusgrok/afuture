import pandas as pd
import pytest


def _result(scenario, window, annualized):
    return {
        "scenario": scenario,
        "window": window,
        "stats": {
            "annualized_return": annualized,
            "max_drawdown": -0.20,
            "halted": False,
            "max_realized_gross_notional_ratio": 1.8,
            "margin_reject_days": 0,
        },
        "economics": {
            "net_alpha": 1_000_000.0,
            "net_alpha_per_turnover_bps": 40.0,
        },
    }


def _payload(scenario, window, annualized):
    from tools.evaluate_directional_stress90_final import (
        EXPECTED_CANDIDATE_WEIGHT_SHA256,
        EXPECTED_CONSTRAINTS,
    )

    return {
        "role": "final fixed Stress90 Production evidence",
        "parameter_search": False,
        "production_wiring": False,
        "candidate": {
            "candidate_weight_sha256": EXPECTED_CANDIDATE_WEIGHT_SHA256,
        },
        "constraints": EXPECTED_CONSTRAINTS,
        "result": _result(scenario, window, annualized),
    }


def test_concentration_seed_is_strictly_before_window_and_skips_inactive_targets():
    from tools.evaluate_directional_stress90_final import (
        completed_concentrations_before,
    )

    index = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    weights = pd.DataFrame(
        {"A": [1.0, 0.0, 1.0], "AG": [-1.0, 0.0, 0.0]},
        index=index,
    )

    assert completed_concentrations_before(
        weights,
        start=pd.Timestamp("2024-01-04"),
    ) == (0.5,)


def test_final_matrix_assembly_applies_frozen_gate_to_independent_windows():
    from tools.evaluate_directional_stress90_final import assemble_matrix_payload

    payloads = [
        _payload("base", "full_recent", 1.20),
        _payload("stress", "train", 0.10),
        _payload("stress", "validation", 1.0),
        _payload("stress", "oos", 0.10),
        _payload("stress", "full_recent", 0.90),
    ]

    matrix = assemble_matrix_payload(payloads)

    assert matrix["gate"] == {"passed": True, "reasons": []}
    assert len(matrix["results"]) == 5


def test_final_matrix_assembly_rejects_duplicate_window():
    from tools.evaluate_directional_stress90_final import assemble_matrix_payload

    payload = _payload("base", "full_recent", 1.20)

    with pytest.raises(ValueError, match="duplicate"):
        assemble_matrix_payload([payload, payload])


def test_final_matrix_assembly_rejects_altered_hard_constraints():
    from tools.evaluate_directional_stress90_final import assemble_matrix_payload

    payload = _payload("base", "full_recent", 1.20)
    payload["constraints"] = dict(payload["constraints"])
    payload["constraints"]["total_drawdown_ratio"] = 0.31

    with pytest.raises(ValueError, match="constraints"):
        assemble_matrix_payload([payload])
