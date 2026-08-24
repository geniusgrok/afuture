from pathlib import Path


def _api():
    from afuture.directional_oi_aware_freeze import (
        OIAwareFreezeDirectionalProductionAcceptance,
        oi_aware_freeze_target,
    )

    return OIAwareFreezeDirectionalProductionAcceptance, oi_aware_freeze_target


def test_confirmed_supported_product_can_increase_during_risk_freeze():
    _, freeze = _api()
    result = freeze(
        current_lots={"M2501": 2, "AG2506": 2},
        target_lots={"M2501": 4, "AG2506": 4},
        symbol_products={"M2501": "M", "AG2506": "AG"},
        trusted_products=("M",),
        triggered=True,
    )

    assert result == {"M2501": 4, "AG2506": 2}


def test_supported_new_entry_is_allowed_because_upstream_oi_overlay_already_confirmed_it():
    _, freeze = _api()
    result = freeze(
        current_lots={},
        target_lots={"P2505": -3, "CU2505": 2},
        symbol_products={"P2505": "P", "CU2505": "CU"},
        trusted_products=("P",),
        triggered=True,
    )

    assert result == {"P2505": -3}


def test_untrusted_new_and_same_sign_increases_still_freeze():
    _, freeze = _api()
    result = freeze(
        current_lots={"AG2506": 2},
        target_lots={"AG2506": 5, "CU2505": -2},
        symbol_products={"AG2506": "AG", "CU2505": "CU"},
        trusted_products=("M", "P"),
        triggered=True,
    )

    assert result == {"AG2506": 2}


def test_reductions_exits_reversals_and_rolls_still_bypass():
    _, freeze = _api()
    result = freeze(
        current_lots={"AG2506": 3, "CU2505": -2, "M2505": 2, "P2505": 1},
        target_lots={"AG2506": 1, "CU2505": 3, "M2509": 4},
        symbol_products={
            "AG2506": "AG",
            "CU2505": "CU",
            "M2505": "M",
            "M2509": "M",
            "P2505": "P",
        },
        trusted_products=("M",),
        triggered=True,
    )

    assert result["AG2506"] == 1
    assert result["CU2505"] == 3
    assert result["M2509"] == 4
    assert "P2505" not in result


def test_no_trigger_is_exact_passthrough():
    _, freeze = _api()
    target = {"AG2506": 4, "M2505": -3}
    assert freeze(
        current_lots={"AG2506": 2},
        target_lots=target,
        symbol_products={"AG2506": "AG", "M2505": "M"},
        trusted_products=("M",),
        triggered=False,
    ) == target


def test_acceptance_keeps_existing_trigger_and_exempts_exact_nine_oi_products():
    Acceptance, _ = _api()
    simulator = Acceptance()

    assert simulator.freeze_triggered((-0.03,)) is True
    assert simulator.trusted_products == (
        "A", "C", "EG", "I", "M", "P", "PP", "TA", "Y"
    )
    assert simulator.risk_governor.scale((-0.03,)) == 1.0


def test_live_runtime_does_not_import_oi_aware_freeze_research_adapter():
    for path in (
        Path("afuture/runtime_factory.py"),
        Path("afuture/execution_aligned_runtime.py"),
        Path("afuture/directional_runtime.py"),
    ):
        if path.exists():
            assert "directional_oi_aware_freeze" not in path.read_text(encoding="utf-8")
