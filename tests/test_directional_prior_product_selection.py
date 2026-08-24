import pandas as pd


def _api():
    from afuture.directional_prior_product_selection import (
        freeze_positive_prior_products,
        redistribute_to_frozen_products,
    )

    return freeze_positive_prior_products, redistribute_to_frozen_products


def test_product_must_have_positive_net_alpha_per_turnover_in_both_prior_windows():
    freeze, _ = _api()
    prior1 = {
        "AG": {"net_alpha_per_turnover_bps": 8.0},
        "CU": {"net_alpha_per_turnover_bps": 2.0},
        "RB": {"net_alpha_per_turnover_bps": -1.0},
    }
    prior2 = {
        "AG": {"net_alpha_per_turnover_bps": 3.0},
        "CU": {"net_alpha_per_turnover_bps": -0.5},
        "RB": {"net_alpha_per_turnover_bps": 7.0},
    }

    selected = freeze(prior1=prior1, prior2=prior2)

    assert selected == ("AG",)


def test_zero_or_missing_prior_evidence_fails_closed():
    freeze, _ = _api()
    prior1 = {
        "AG": {"net_alpha_per_turnover_bps": 1.0},
        "CU": {"net_alpha_per_turnover_bps": 0.0},
    }
    prior2 = {
        "CU": {"net_alpha_per_turnover_bps": 2.0},
    }

    assert freeze(prior1=prior1, prior2=prior2) == ()


def test_redistribution_keeps_original_daily_gross_and_signs():
    _, redistribute = _api()
    index = pd.to_datetime(["2026-01-05", "2026-01-06"])
    raw = pd.DataFrame(
        {
            "AG": [0.5, -0.4],
            "CU": [0.5, 0.4],
            "RB": [-1.0, 0.0],
        },
        index=index,
    )

    result = redistribute(raw, frozen_products=("AG", "CU"))

    assert abs(result.loc[index[0], "AG"] - 1.0) < 1e-12
    assert abs(result.loc[index[0], "CU"] - 1.0) < 1e-12
    assert result.loc[index[0], "RB"] == 0.0
    assert abs(result.loc[index[0]].abs().sum() - 2.0) < 1e-12

    assert result.loc[index[1], "AG"] < 0.0
    assert result.loc[index[1], "CU"] > 0.0
    assert abs(result.loc[index[1]].abs().sum() - 0.8) < 1e-12


def test_no_surviving_active_product_means_flat_not_fallback_to_excluded_products():
    _, redistribute = _api()
    index = pd.to_datetime(["2026-01-05"])
    raw = pd.DataFrame({"AG": [0.0], "RB": [2.0]}, index=index)

    result = redistribute(raw, frozen_products=("AG",))

    assert float(result.abs().sum(axis=1).iloc[0]) == 0.0


def test_redistribution_never_exceeds_two_x_or_original_gross():
    _, redistribute = _api()
    index = pd.to_datetime(["2026-01-05", "2026-01-06"])
    raw = pd.DataFrame(
        {"AG": [0.2, 1.0], "CU": [0.3, -1.0], "RB": [0.5, 0.0]},
        index=index,
    )

    result = redistribute(raw, frozen_products=("AG", "CU"))

    original_gross = raw.abs().sum(axis=1)
    result_gross = result.abs().sum(axis=1)
    assert bool((result_gross <= original_gross + 1e-12).all())
    assert float(result_gross.max()) <= 2.0 + 1e-12
