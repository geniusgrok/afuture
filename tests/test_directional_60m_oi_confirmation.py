import pandas as pd


def _api():
    from afuture.directional_60m_oi_confirmation import (
        SUPPORTED_PRODUCTS,
        apply_oi_confirmation_to_weights,
        build_daily_price_oi_flow,
        lag_flow_to_target_days,
    )

    return (
        SUPPORTED_PRODUCTS,
        build_daily_price_oi_flow,
        lag_flow_to_target_days,
        apply_oi_confirmation_to_weights,
    )


def test_supported_products_are_frozen_from_the_60m_coverage_gate():
    supported, _, _, _ = _api()
    assert supported == ("A", "C", "EG", "I", "M", "P", "PP", "TA", "Y")


def test_daily_flow_uses_same_day_highest_final_oi_contract_and_requires_oi_growth():
    _, build_flow, _, _ = _api()
    raw = pd.DataFrame(
        [
            {
                "datetime": "2026-01-05 10:00",
                "product": "AG",
                "symbol": "AG2606",
                "open": 100.0,
                "close": 101.0,
                "volume": 10,
                "hold": 100,
            },
            {
                "datetime": "2026-01-05 15:00",
                "product": "AG",
                "symbol": "AG2606",
                "open": 101.0,
                "close": 103.0,
                "volume": 20,
                "hold": 130,
            },
            {
                "datetime": "2026-01-05 10:00",
                "product": "AG",
                "symbol": "AG2608",
                "open": 200.0,
                "close": 199.0,
                "volume": 50,
                "hold": 150,
            },
            {
                "datetime": "2026-01-05 15:00",
                "product": "AG",
                "symbol": "AG2608",
                "open": 199.0,
                "close": 198.0,
                "volume": 50,
                "hold": 140,
            },
        ]
    )

    flow = build_flow(raw)

    assert flow.loc[pd.Timestamp("2026-01-05"), "AG"] == 0.0


def test_daily_flow_direction_is_first_open_to_last_close_when_oi_increases():
    _, build_flow, _, _ = _api()
    raw = pd.DataFrame(
        [
            {
                "datetime": "2026-01-05 10:00",
                "product": "RB",
                "symbol": "RB2605",
                "open": 3500.0,
                "close": 3490.0,
                "volume": 100,
                "hold": 1000,
            },
            {
                "datetime": "2026-01-05 15:00",
                "product": "RB",
                "symbol": "RB2605",
                "open": 3490.0,
                "close": 3450.0,
                "volume": 120,
                "hold": 1100,
            },
        ]
    )
    flow = build_flow(raw)
    assert flow.loc[pd.Timestamp("2026-01-05"), "RB"] == -1.0


def test_flow_is_shifted_exactly_one_frozen_target_session():
    _, _, lag_flow, _ = _api()
    flow = pd.DataFrame(
        {"AG": [1.0, -1.0]},
        index=pd.to_datetime(["2026-01-05", "2026-01-06"]),
    )
    target_days = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"])

    result = lag_flow(flow, target_days=target_days, products=("AG",))

    assert pd.isna(result.loc[pd.Timestamp("2026-01-05"), "AG"])
    assert result.loc[pd.Timestamp("2026-01-06"), "AG"] == 1.0
    assert result.loc[pd.Timestamp("2026-01-07"), "AG"] == -1.0


def test_missing_flow_remains_distinct_from_legal_zero_flow():
    _, _, lag_flow, _ = _api()
    flow = pd.DataFrame(
        {"A": [0.0], "C": [float("nan")]},
        index=pd.to_datetime(["2026-01-05"]),
    )
    target_days = pd.to_datetime(["2026-01-05", "2026-01-06"])

    result = lag_flow(flow, target_days=target_days, products=("A", "C"))

    assert result.loc[pd.Timestamp("2026-01-06"), "A"] == 0.0
    assert pd.isna(result.loc[pd.Timestamp("2026-01-06"), "C"])


def test_first_target_can_use_explicitly_available_prior_completed_session():
    _, _, lag_flow, _ = _api()
    flow = pd.DataFrame(
        {"A": [-1.0, 1.0]},
        index=pd.to_datetime(["2026-01-02", "2026-01-05"]),
    )

    result = lag_flow(
        flow,
        target_days=pd.to_datetime(["2026-01-05", "2026-01-06"]),
        products=("A",),
    )

    assert result.loc[pd.Timestamp("2026-01-05"), "A"] == -1.0
    assert result.loc[pd.Timestamp("2026-01-06"), "A"] == 1.0


def test_matching_flow_allows_new_risk_but_disagreement_blocks_it():
    _, _, _, apply_filter = _api()
    index = pd.to_datetime(["2026-01-05", "2026-01-06"])
    raw = pd.DataFrame({"AG": [0.0, 1.0], "CU": [0.0, -1.0]}, index=index)
    flow = pd.DataFrame({"AG": [0.0, 1.0], "CU": [0.0, 1.0]}, index=index)

    result = apply_filter(raw_weights=raw, confirming_flow=flow, supported_products=("AG", "CU"))

    assert result.loc[index[-1], "AG"] == 1.0
    assert result.loc[index[-1], "CU"] == 0.0


def test_same_sign_increase_is_blocked_and_unconfirmed_reversal_exits_to_flat():
    _, _, _, apply_filter = _api()
    index = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"])
    raw = pd.DataFrame({"AG": [1.0, 2.0, 0.5, -1.0]}, index=index)
    flow = pd.DataFrame({"AG": [1.0, -1.0, -1.0, 1.0]}, index=index)

    result = apply_filter(raw_weights=raw, confirming_flow=flow, supported_products=("AG",))

    assert list(result["AG"]) == [1.0, 1.0, 0.5, 0.0]


def test_unsupported_products_are_exactly_unchanged_and_gross_never_exceeds_raw():
    _, _, _, apply_filter = _api()
    index = pd.to_datetime(["2026-01-05", "2026-01-06"])
    raw = pd.DataFrame({"AG": [1.0, 1.0], "AU": [-1.0, -1.0]}, index=index)
    flow = pd.DataFrame({"AG": [1.0, -1.0]}, index=index)

    result = apply_filter(raw_weights=raw, confirming_flow=flow, supported_products=("AG",))

    assert result["AU"].equals(raw["AU"])
    assert bool((result.abs().sum(axis=1) <= raw.abs().sum(axis=1) + 1e-12).all())
