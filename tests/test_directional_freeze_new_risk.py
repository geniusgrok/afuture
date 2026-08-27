from pathlib import Path


def _api():
    from afuture.directional_freeze_new_risk import (
        FreezeNewRiskDirectionalProductionAcceptance,
        freeze_new_risk_target,
        risk_freeze_triggered,
    )

    return (
        FreezeNewRiskDirectionalProductionAcceptance,
        freeze_new_risk_target,
        risk_freeze_triggered,
    )


def test_trigger_uses_existing_directional_governor_loss_and_volatility_rules():
    _, _, triggered = _api()

    assert triggered((-0.021,)) is True
    assert triggered((-0.019,)) is False
    assert triggered((-0.03, 0.03)) is True
    assert triggered((0.001, 0.002)) is False


def test_triggered_freeze_blocks_new_entries_and_same_sign_increases_only():
    _, freeze, _ = _api()
    result = freeze(
        current_lots={"A2501": 2, "M2501": -2, "I2501": 2},
        target_lots={"A2501": 4, "M2501": -1, "I2501": -3, "C2501": 2},
        symbol_products={
            "A2501": "A",
            "M2501": "M",
            "I2501": "I",
            "C2501": "C",
        },
        triggered=True,
    )

    assert result == {"A2501": 2, "M2501": -1, "I2501": -3}


def test_reduction_exit_reversal_and_contract_roll_are_never_blocked():
    _, freeze, _ = _api()
    result = freeze(
        current_lots={"AG2606": 2, "M2501": 3, "I2501": -2, "C2501": 1},
        target_lots={"AG2608": 3, "M2501": 1, "I2501": 2},
        symbol_products={
            "AG2606": "AG",
            "AG2608": "AG",
            "M2501": "M",
            "I2501": "I",
            "C2501": "C",
        },
        triggered=True,
        lot_notionals={
            "AG2606": 10.0,
            "AG2608": 5.0,
            "M2501": 10.0,
            "I2501": 10.0,
            "C2501": 10.0,
        },
    )

    # AG is a same-product contract roll and therefore remains executable.
    assert result["AG2608"] == 3
    # Same-sign reduction and reversal remain exact targets.
    assert result["M2501"] == 1
    assert result["I2501"] == 2
    # C exit is represented by absence from the target and is not resurrected.
    assert "C2501" not in result


def test_no_trigger_is_exact_target_passthrough():
    _, freeze, _ = _api()
    target = {"A2501": 4, "M2501": -1, "C2501": 2}
    result = freeze(
        current_lots={"A2501": 2, "M2501": -2},
        target_lots=target,
        symbol_products={"A2501": "A", "M2501": "M", "C2501": "C"},
        triggered=False,
    )
    assert result == target


def test_research_acceptance_disables_global_point25_scaling_but_keeps_trigger():
    Acceptance, _, _ = _api()
    simulator = Acceptance()

    # The simulator must pass the raw target into target construction; the separate
    # unchanged governor trigger is applied as a freeze rather than a global 0.25 scale.
    assert simulator.risk_governor.scale((-0.03,)) == 1.0
    assert simulator.freeze_triggered((-0.03,)) is True


def test_live_runtime_does_not_import_freeze_new_risk_research_adapter():
    for path in (
        Path("afuture/runtime_factory.py"),
        Path("afuture/execution_aligned_runtime.py"),
        Path("afuture/directional_runtime.py"),
    ):
        if path.exists():
            assert "directional_freeze_new_risk" not in path.read_text(encoding="utf-8")
