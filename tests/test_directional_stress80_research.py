from pathlib import Path

import numpy as np
import pandas as pd
import pytest


def _api():
    from afuture.directional_stress80_research import (
        ALLOWED_FAMILIES,
        Stress80DirectionalProductionAcceptance,
        combine_family_edge_signals,
        composite_family_edge,
        family_signal_history,
    )

    return (
        ALLOWED_FAMILIES,
        Stress80DirectionalProductionAcceptance,
        combine_family_edge_signals,
        composite_family_edge,
        family_signal_history,
    )


def _ledger_row(*, signal_date, available, product, family, horizon, gross):
    return {
        "signal_date": pd.Timestamp(signal_date),
        "product": product,
        "family": family,
        "signal_direction": 1,
        "signal_strength": 1.0,
        "forward_horizon_sessions": horizon,
        "label_available_date": pd.Timestamp(available),
        "raw_future_specific_contract_return": gross,
        "future_specific_contract_gross_return": gross,
        "future_specific_contract_net_return_15bp": gross - 0.003,
    }


def test_family_signal_history_emits_only_the_six_frozen_families_and_respects_2x_gross():
    allowed, _, _, _, family_signal_history = _api()
    assert allowed == (
        "breakout",
        "tsmom",
        "momentum",
        "moving_average",
        "reversal",
        "acceleration",
    )
    index = pd.date_range("2025-01-01", periods=150, freq="B")
    base = np.linspace(100.0, 150.0, len(index))
    close = pd.DataFrame(
        {"AG": base, "CU": base[::-1] + 50.0}, index=index
    )
    open_prices = close.shift(1).fillna(close.iloc[0])

    paths = family_signal_history(open_prices, close, products=("AG", "CU"))

    assert tuple(paths) == allowed
    for family, frame in paths.items():
        assert list(frame.columns) == ["AG", "CU"]
        assert frame.index.equals(index)
        assert bool((frame.abs().sum(axis=1) <= 2.0 + 1e-10).all()), family


def test_composite_family_edge_equal_averages_fixed_5_10_20_per_session_edges():
    _, _, _, composite_family_edge, _ = _api()
    ledger = pd.DataFrame(
        [
            _ledger_row(
                signal_date="2026-01-02",
                available="2026-01-12",
                product="AG",
                family="breakout",
                horizon=5,
                gross=0.05,
            ),
            _ledger_row(
                signal_date="2026-01-02",
                available="2026-01-19",
                product="AG",
                family="breakout",
                horizon=10,
                gross=0.10,
            ),
            _ledger_row(
                signal_date="2026-01-02",
                available="2026-02-02",
                product="AG",
                family="breakout",
                horizon=20,
                gross=0.20,
            ),
        ]
    )

    value = composite_family_edge(
        ledger,
        decision_date=pd.Timestamp("2026-02-10"),
        product="AG",
        family="breakout",
    )
    assert value == pytest.approx(0.01)


def test_negative_edge_suppresses_family_signal_and_never_inverts_direction():
    _, _, combine, _, _ = _api()
    weights, product_edges = combine(
        family_signals={
            "breakout": {"AG": 1.0, "CU": -1.0},
            "reversal": {"AG": -1.0},
        },
        family_edges={
            ("AG", "breakout"): 0.02,
            ("CU", "breakout"): 0.03,
            ("AG", "reversal"): -0.50,
        },
    )

    assert weights["AG"] > 0.0
    assert weights["CU"] < 0.0
    assert sum(abs(value) for value in weights.values()) == pytest.approx(2.0)
    assert product_edges == pytest.approx({"AG": 0.02, "CU": 0.03})


def test_research_adapter_exactly_falls_back_without_decision_edge_evidence():
    _, adapter_type, _, _, _ = _api()
    from afuture.directional_robustness import MarginAwareDirectionalProductionAcceptance

    kwargs = dict(
        equity=500_000.0,
        product_weights={"AG": 1.0},
        product_open_prices={"AG": 8_000.0},
        selected_symbols={"AG": "AG2612"},
        current_lots={},
        completed_returns=(),
    )
    baseline = MarginAwareDirectionalProductionAcceptance().target_lot_stages(**kwargs)
    candidate = adapter_type(pd.DataFrame()).target_lot_stages(**kwargs)
    assert candidate == baseline


def test_research_adapter_uses_same_margin_envelope_but_can_reject_costly_entry():
    _, adapter_type, _, _, _ = _api()
    day = pd.Timestamp("2026-08-20")
    adapter = adapter_type(pd.DataFrame({"AG": [0.0001]}, index=[day]))
    adapter._stress80_decision_day = day
    adapter._stress80_cost_rate = 0.0015

    result = adapter.target_lot_stages(
        equity=500_000.0,
        product_weights={"AG": 1.0},
        product_open_prices={"AG": 8_000.0},
        selected_symbols={"AG": "AG2612"},
        current_lots={},
        completed_returns=(),
    )

    assert result.soft_margin_share is not None
    assert result.soft_margin_share <= 0.35
    assert result.final_lots == {}


def test_live_runtime_does_not_import_research_or_future_label_modules():
    _api()
    forbidden = (
        "directional_stress80_research",
        "directional_opportunity_ledger",
        "directional_causal_edge",
    )
    for path in (
        Path("afuture/directional_runtime.py"),
        Path("afuture/execution_aligned_runtime.py"),
        Path("afuture/runtime_factory.py"),
    ):
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in source, (path, token)
