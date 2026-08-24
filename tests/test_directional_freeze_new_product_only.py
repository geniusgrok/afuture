from pathlib import Path


def _api():
    from afuture.directional_freeze_new_product_only import (
        FreezeNewProductOnlyDirectionalProductionAcceptance,
        freeze_new_product_target,
    )

    return FreezeNewProductOnlyDirectionalProductionAcceptance, freeze_new_product_target


def test_triggered_response_blocks_only_brand_new_product_entry():
    _, freeze = _api()
    result = freeze(
        current_lots={"A2601": 2, "M2601": -2},
        target_lots={"A2601": 4, "M2601": -3, "C2601": 2},
        symbol_products={"A2601": "A", "M2601": "M", "C2601": "C"},
        triggered=True,
    )
    assert result == {"A2601": 4, "M2601": -3}


def test_reduction_exit_reversal_and_same_product_roll_bypass():
    _, freeze = _api()
    result = freeze(
        current_lots={"AG2606": 2, "M2601": 3, "I2601": -2, "C2601": 1},
        target_lots={"AG2608": 3, "M2601": 1, "I2601": 2},
        symbol_products={
            "AG2606": "AG", "AG2608": "AG", "M2601": "M", "I2601": "I", "C2601": "C"
        },
        triggered=True,
    )
    assert result["AG2608"] == 3
    assert result["M2601"] == 1
    assert result["I2601"] == 2
    assert "C2601" not in result


def test_no_trigger_is_exact_passthrough():
    _, freeze = _api()
    target = {"A2601": 4, "M2601": -3, "C2601": 2}
    assert freeze(
        current_lots={"A2601": 2, "M2601": -2},
        target_lots=target,
        symbol_products={"A2601": "A", "M2601": "M", "C2601": "C"},
        triggered=False,
    ) == target


def test_adapter_uses_unchanged_trigger_without_global_point25_scaling():
    Acceptance, _ = _api()
    simulator = Acceptance()
    assert simulator.risk_governor.scale((-0.03,)) == 1.0
    assert simulator.freeze_triggered((-0.03,)) is True


def test_research_adapter_is_not_live_wired():
    for path in (
        Path("afuture/runtime_factory.py"),
        Path("afuture/execution_aligned_runtime.py"),
        Path("afuture/directional_runtime.py"),
    ):
        if path.exists():
            assert "directional_freeze_new_product_only" not in path.read_text(encoding="utf-8")
