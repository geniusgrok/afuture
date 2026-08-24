import pandas as pd


def test_combination_applies_oi_confirmation_before_fixed_cost_filter(monkeypatch):
    import tools.evaluate_directional_oi_freeze_cost_combo as combo

    index = pd.to_datetime(["2026-01-05", "2026-01-06"])
    raw = pd.DataFrame({"A": [0.0, 1.0]}, index=index)
    oi = pd.DataFrame({"A": [0.0, 0.5]}, index=index)
    final = pd.DataFrame({"A": [0.0, 0.25]}, index=index)
    close = pd.DataFrame({"A": [100.0, 101.0]}, index=index)
    bars = pd.DataFrame({"sentinel": [1]})
    calls = []

    def fake_oi(weights, bars_60m):
        assert weights is raw
        assert bars_60m is bars
        calls.append("oi")
        return oi, pd.DataFrame(index=index)

    def fake_cost(*, weights, close_prices, initial_weights=None):
        assert weights is oi
        assert close_prices is close
        assert initial_weights is None
        calls.append("cost")
        return final

    monkeypatch.setattr(combo.oi_gate, "build_candidate_weights", fake_oi)
    monkeypatch.setattr(combo, "apply_cost_aware_no_trade", fake_cost)

    result = combo.build_combined_weights(
        base_weights=raw,
        bars_60m=bars,
        close_prices=close,
    )

    assert calls == ["oi", "cost"]
    assert result.equals(final)


def test_combination_never_adds_gross_after_two_suppress_only_layers(monkeypatch):
    import tools.evaluate_directional_oi_freeze_cost_combo as combo

    index = pd.to_datetime(["2026-01-05", "2026-01-06"])
    raw = pd.DataFrame({"A": [1.0, 1.0], "M": [-1.0, -1.0]}, index=index)
    oi = pd.DataFrame({"A": [0.5, 1.0], "M": [-1.0, -0.5]}, index=index)
    close = pd.DataFrame({"A": [100.0, 101.0], "M": [200.0, 199.0]}, index=index)

    monkeypatch.setattr(
        combo.oi_gate,
        "build_candidate_weights",
        lambda weights, bars: (oi, pd.DataFrame(index=index)),
    )
    monkeypatch.setattr(
        combo,
        "apply_cost_aware_no_trade",
        lambda *, weights, close_prices, initial_weights=None: weights * 0.5,
    )

    result = combo.build_combined_weights(
        base_weights=raw,
        bars_60m=pd.DataFrame(),
        close_prices=close,
    )

    assert bool((result.abs().sum(axis=1) <= raw.abs().sum(axis=1) + 1e-12).all())
