def _api():
    from tools.evaluate_directional_stress80_final import promotion_gate

    return promotion_gate


def test_historical_research_authorization_boundary_is_fixed_and_fresh():
    from tools.evaluate_directional_stress80_final import historical_research_metadata

    first = historical_research_metadata()
    second = historical_research_metadata()

    assert first == {
        "evidence_scope": "historical_research_only",
        "historical_replay_commit": "9c51195042393304eb05d783d1895a165f99b0a7",
        "live_authorized": False,
        "risk_increase_authorized": False,
        "prospective_evidence": False,
    }
    first["live_authorized"] = True
    assert second["live_authorized"] is False


def _entry(annualized=0.90, dd=-0.20, halted=False, gross=1.5, rejects=0, net_bps=20.0):
    return {
        "stats": {
            "annualized_return": annualized,
            "max_drawdown": dd,
            "halted": halted,
            "max_realized_gross_notional_ratio": gross,
            "margin_reject_days": rejects,
        },
        "economics": {"net_alpha_per_turnover_bps": net_bps},
    }


def test_final_gate_accepts_only_complete_80_80_cross_window_candidate():
    gate = _api()
    results = {
        ("base", "full_recent"): _entry(1.20, -0.28),
        ("stress", "train"): _entry(0.10, -0.24),
        ("stress", "validation"): _entry(2.0, -0.17),
        ("stress", "oos"): _entry(0.20, -0.24),
        ("stress", "full_recent"): _entry(0.81, -0.297, net_bps=30.0),
    }
    assert gate(results) == {"passed": True, "reasons": []}


def test_final_gate_rejects_return_risk_halt_margin_and_efficiency_failures():
    gate = _api()
    results = {
        ("base", "full_recent"): _entry(0.79, -0.31, halted=True, gross=2.01, rejects=1),
        ("stress", "train"): _entry(-0.01, -0.31),
        ("stress", "validation"): _entry(0.01, -0.20),
        ("stress", "oos"): _entry(0.01, -0.20),
        ("stress", "full_recent"): _entry(
            0.79, -0.31, halted=True, gross=2.01, rejects=1, net_bps=12.0
        ),
    }
    result = gate(results)
    assert result["passed"] is False
    reasons = set(result["reasons"])
    assert "base_full_recent_below_80pct" in reasons
    assert "stress_full_recent_below_80pct" in reasons
    assert "base_full_recent_dd_exceeds_30pct" in reasons
    assert "stress_full_recent_dd_exceeds_30pct" in reasons
    assert "base_full_recent_halted" in reasons
    assert "stress_full_recent_halted" in reasons
    assert "base_full_recent_gross_exceeds_2x" in reasons
    assert "stress_full_recent_gross_exceeds_2x" in reasons
    assert "base_full_recent_margin_rejects_nonzero" in reasons
    assert "stress_full_recent_margin_rejects_nonzero" in reasons
    assert "stress_train_not_positive" in reasons
    assert "stress_net_alpha_per_turnover_not_above_baseline" in reasons
