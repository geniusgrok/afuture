def _api():
    from tools.evaluate_directional_60m_oi_confirmation import production_promotion_gate

    return production_promotion_gate


def _endpoint(annualized, drawdown=-0.20, halted=False, gross=1.5):
    return {
        "annualized_return": annualized,
        "max_drawdown": drawdown,
        "halted": halted,
        "max_realized_gross_notional_ratio": gross,
    }


def test_production_gate_requires_full_80_80_and_live_validation_oos():
    gate = _api()
    base = {"windows": {"full_recent": _endpoint(0.90)}}
    stress = {
        "windows": {
            "full_recent": _endpoint(0.81, -0.29),
            "validation": _endpoint(0.10, -0.20),
            "oos": _endpoint(0.05, -0.25),
        }
    }
    result = gate(base=base, stress=stress)
    assert result["passed"] is True
    assert result["reasons"] == []


def test_production_gate_rejects_each_hard_failure():
    gate = _api()
    base = {"windows": {"full_recent": _endpoint(0.79)}}
    stress = {
        "windows": {
            "full_recent": _endpoint(0.79, -0.31, halted=True, gross=2.01),
            "validation": _endpoint(-0.01, -0.31),
            "oos": _endpoint(0.01, -0.31, halted=True),
        }
    }
    result = gate(base=base, stress=stress)
    assert result["passed"] is False
    assert set(result["reasons"]) == {
        "base_full_recent_below_80pct",
        "stress_full_recent_below_80pct",
        "stress_full_recent_dd_above_30pct",
        "stress_full_recent_halted",
        "stress_full_recent_gross_above_2x",
        "validation_stress_not_positive",
        "validation_stress_dd_above_30pct",
        "oos_stress_dd_above_30pct",
        "oos_stress_halted",
    }
