from pathlib import Path


def _api():
    from afuture.directional_lot_feasibility_reallocation import (
        LotFeasibleDirectionalProductionAcceptance,
        reallocate_sub_one_lot_weights,
    )

    return LotFeasibleDirectionalProductionAcceptance, reallocate_sub_one_lot_weights


def test_sub_one_lot_weight_moves_only_to_existing_feasible_survivor():
    _, reallocate = _api()
    result = reallocate(
        equity=500_000.0,
        product_weights={"AU": 0.10, "A": 0.60, "M": -0.40},
        product_open_prices={"AU": 600.0, "A": 3000.0, "M": 3000.0},
        selected_symbols={"AU": "AU2512", "A": "A2601", "M": "M2601"},
        product_multipliers={"AU": 1000.0, "A": 10.0, "M": 10.0},
        max_contract_volume=35,
    )
    # AU desired notional is 50k < one 600k lot, so its 0.10 gross is donated.
    # A/M are both feasible and receive it in their original 60/40 absolute proportions.
    assert abs(result["AU"]) < 1e-12
    assert abs(result["A"] - 0.66) < 1e-12
    assert abs(result["M"] + 0.44) < 1e-12
    assert abs(sum(abs(v) for v in result.values()) - 1.10) < 1e-12


def test_unavailable_product_is_not_reallocated_from_without_execution_evidence():
    _, reallocate = _api()
    result = reallocate(
        equity=500_000.0,
        product_weights={"AU": 0.10, "A": 0.60},
        product_open_prices={"A": 3000.0},
        selected_symbols={"A": "A2601"},
        product_multipliers={"AU": 1000.0, "A": 10.0},
        max_contract_volume=35,
    )
    assert result == {"AU": 0.10, "A": 0.60}


def test_capacity_limit_leaves_unallocated_donor_weight_in_original_product():
    _, reallocate = _api()
    result = reallocate(
        equity=100_000.0,
        product_weights={"X": 0.40, "A": 0.50},
        product_open_prices={"X": 100.0, "A": 1000.0},
        selected_symbols={"X": "X1", "A": "A1"},
        product_multipliers={"X": 1000.0, "A": 10.0},
        max_contract_volume=6,
    )
    # X is sub-one-lot (40k desired vs 100k lot). A has only 0.10 gross capacity
    # before the 6-lot cap, so only that portion moves; the rest stays fail-closed in X.
    assert abs(result["X"] - 0.30) < 1e-12
    assert abs(result["A"] - 0.60) < 1e-12
    assert abs(sum(abs(v) for v in result.values()) - 0.90) < 1e-12


def test_reallocation_preserves_sign_support_and_never_increases_gross():
    _, reallocate = _api()
    original = {"AU": -0.08, "A": 0.50, "M": -0.50}
    result = reallocate(
        equity=500_000.0,
        product_weights=original,
        product_open_prices={"AU": 600.0, "A": 3000.0, "M": 3000.0},
        selected_symbols={"AU": "AU2512", "A": "A2601", "M": "M2601"},
        product_multipliers={"AU": 1000.0, "A": 10.0, "M": 10.0},
        max_contract_volume=35,
    )
    assert set(result) <= set(original)
    assert result["A"] > 0 and result["M"] < 0
    assert sum(abs(v) for v in result.values()) <= sum(abs(v) for v in original.values()) + 1e-12


def test_research_lot_feasible_adapter_is_not_live_wired():
    for path in (
        Path("afuture/runtime_factory.py"),
        Path("afuture/execution_aligned_runtime.py"),
        Path("afuture/directional_runtime.py"),
    ):
        if path.exists():
            assert "directional_lot_feasibility_reallocation" not in path.read_text(encoding="utf-8")
