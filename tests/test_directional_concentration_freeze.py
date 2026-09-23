def _api():
    from afuture.directional_concentration_freeze import (
        ExpandingMedianConcentrationFreezeDirectionalProductionAcceptance,
        target_weight_concentration,
    )

    return (
        ExpandingMedianConcentrationFreezeDirectionalProductionAcceptance,
        target_weight_concentration,
    )


def test_target_weight_concentration_is_standard_hhi_and_inactive_is_none():
    _, concentration = _api()

    assert concentration({"A": 1.0, "AG": -1.0}) == 0.5
    assert concentration({"A": 2.0}) == 1.0
    assert concentration({"A": 0.0, "AG": 0.0}) is None


def test_winner_retains_full_account_path_for_the_fixed_drawdown_reserve():
    adapter_type, _ = _api()
    completed = [0.01, -0.02, 0.03, -0.04]

    assert adapter_type().retain_completed_returns(completed) == completed


def test_low_hhi_is_observed_without_vetoing_entry():
    adapter_type, _ = _api()
    adapter = adapter_type(completed_concentrations=(0.4, 0.6))

    adapter.observe_target_state(day=None, product_weights={"A": 0.5, "AG": 0.5})
    assert adapter.concentration_freeze_triggered is False
    assert adapter._completed_concentrations[-1] == 0.5

    adapter.observe_target_state(day=None, product_weights={"A": 1.0})
    assert adapter.concentration_freeze_triggered is False


def test_low_hhi_allows_entry_and_increase_but_preserves_reduction():
    adapter_type, _ = _api()
    adapter = adapter_type(completed_concentrations=(0.6,))
    adapter.observe_target_state(day=None, product_weights={"A": 0.5, "AG": 0.5})

    increased = adapter.target_lots(
        equity=500_000.0,
        product_weights={"A": 0.5, "AG": 0.5},
        product_open_prices={"A": 100.0, "AG": 100.0},
        selected_symbols={"A": "A2501", "AG": "AG2501"},
        current_lots={"A2501": 5},
    )
    reduced = adapter.target_lots(
        equity=500_000.0,
        product_weights={"A": 0.05, "AG": 0.05},
        product_open_prices={"A": 100.0, "AG": 100.0},
        selected_symbols={"A": "A2501", "AG": "AG2501"},
        current_lots={"A2501": 35},
    )

    assert increased == {"A2501": 35, "AG2501": 35}
    assert reduced == {"A2501": 25, "AG2501": 16}


def test_old_hhi_checkpoint_is_rejected():
    import pytest

    adapter_type, _ = _api()
    adapter = adapter_type()
    state = adapter._checkpoint_strategy_state()
    assert state["concentration_freeze_triggered"] is False
    adapter._restore_checkpoint_strategy_state(state)
    with pytest.raises(ValueError, match="checkpoint concentration state"):
        adapter._restore_checkpoint_strategy_state(
            {key: value for key, value in state.items() if key != "policy_definition_digest"}
        )


def test_strong_leadership_passes_exact_stress80_target():
    adapter_type, _ = _api()
    adapter = adapter_type(completed_concentrations=(0.5,))
    adapter.observe_target_state(day=None, product_weights={"A": 1.0})

    assert adapter.target_lots(
        equity=500_000.0,
        product_weights={"A": 1.0},
        product_open_prices={"A": 100.0},
        selected_symbols={"A": "A2501"},
        current_lots={},
    ) == {"A2501": 35}
