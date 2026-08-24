from copy import deepcopy


def _passing_results():
    def item(annualized, *, net=1_200_000.0, efficiency=31.1):
        return {
            "stats": {
                "annualized_return": annualized,
                "max_drawdown": -0.29,
                "halted": False,
                "max_realized_gross_notional_ratio": 1.9,
                "margin_reject_days": 0,
            },
            "economics": {
                "net_alpha": net,
                "net_alpha_per_turnover_bps": efficiency,
            },
        }

    return {
        ("base", "full_recent"): item(1.01),
        ("stress", "train"): item(0.01),
        ("stress", "validation"): item(0.01),
        ("stress", "oos"): item(0.01),
        ("stress", "full_recent"): item(0.85),
    }


def test_gate_accepts_exact_minimums_and_strict_efficiency_gain():
    from afuture.directional_stress90_gate import evaluate_stress90_gate

    gate = evaluate_stress90_gate(_passing_results(), max_contract_lots=35)

    assert gate == {"passed": True, "reasons": []}


def test_gate_rejects_headline_and_hard_gate_failures():
    from afuture.directional_stress90_gate import evaluate_stress90_gate

    results = deepcopy(_passing_results())
    results[("base", "full_recent")]["stats"]["annualized_return"] = 0.999
    results[("stress", "full_recent")]["stats"]["annualized_return"] = 0.849
    results[("stress", "validation")]["stats"]["max_drawdown"] = -0.301
    results[("stress", "oos")]["stats"]["margin_reject_days"] = 1
    results[("stress", "train")]["stats"]["halted"] = True

    reasons = evaluate_stress90_gate(results, max_contract_lots=35)["reasons"]

    assert "base_full_recent_below_100pct" in reasons
    assert "stress_full_recent_below_85pct" in reasons
    assert "stress_validation_dd_exceeds_30pct" in reasons
    assert "stress_oos_margin_rejects_nonzero" in reasons
    assert "stress_train_halted" in reasons


def test_gate_requires_positive_splits_and_efficiency_improvement():
    from afuture.directional_stress90_gate import evaluate_stress90_gate

    results = deepcopy(_passing_results())
    results[("stress", "oos")]["stats"]["annualized_return"] = 0.0
    results[("stress", "full_recent")]["economics"][
        "net_alpha_per_turnover_bps"
    ] = 30.990722

    reasons = evaluate_stress90_gate(results, max_contract_lots=35)["reasons"]

    assert "stress_oos_not_positive" in reasons
    assert "stress_net_alpha_per_turnover_not_above_30_990722bps" in reasons


def test_gate_rejects_changed_lot_cap():
    from afuture.directional_stress90_gate import evaluate_stress90_gate

    gate = evaluate_stress90_gate(_passing_results(), max_contract_lots=36)

    assert gate["passed"] is False
    assert gate["reasons"] == ["max_contract_lots_not_35"]
