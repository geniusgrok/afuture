import math

import pandas as pd
import pytest


def _api():
    from afuture.directional_opportunity_ledger import (
        ALLOWED_HORIZONS,
        build_opportunity_ledger,
        completed_opportunities,
    )

    return ALLOWED_HORIZONS, build_opportunity_ledger, completed_opportunities


def test_untraded_opportunity_is_labeled_and_hidden_until_strictly_after_availability():
    _, build_opportunity_ledger, completed_opportunities = _api()
    index = pd.DatetimeIndex(["2026-01-02"])
    signals = {"breakout": pd.DataFrame({"AG": [1.0]}, index=index)}
    forward = {5: pd.DataFrame({"AG": [0.04]}, index=index)}
    available = {
        5: pd.DataFrame({"AG": [pd.Timestamp("2026-01-09")]}, index=index)
    }

    ledger = build_opportunity_ledger(
        signals=signals,
        forward_returns=forward,
        label_available_dates=available,
        horizons=(5,),
    )

    assert len(ledger) == 1
    row = ledger.iloc[0]
    assert row["product"] == "AG"
    assert row["family"] == "breakout"
    assert row["signal_direction"] == 1
    assert row["signal_strength"] == 1.0
    assert row["future_specific_contract_gross_return"] == 0.04
    assert row["future_specific_contract_net_return_15bp"] == pytest.approx(0.037)
    assert completed_opportunities(
        ledger, decision_date=pd.Timestamp("2026-01-09")
    ).empty
    visible = completed_opportunities(
        ledger, decision_date=pd.Timestamp("2026-01-12")
    )
    assert len(visible) == 1


def test_signal_direction_is_applied_to_raw_future_return():
    _, build_opportunity_ledger, _ = _api()
    index = pd.DatetimeIndex(["2026-01-02"])
    signals = {"reversal": pd.DataFrame({"CU": [-2.5]}, index=index)}
    forward = {10: pd.DataFrame({"CU": [0.03]}, index=index)}
    available = {
        10: pd.DataFrame({"CU": [pd.Timestamp("2026-01-16")]}, index=index)
    }

    ledger = build_opportunity_ledger(
        signals=signals,
        forward_returns=forward,
        label_available_dates=available,
        horizons=(10,),
    )

    row = ledger.iloc[0]
    assert row["signal_direction"] == -1
    assert row["signal_strength"] == 2.5
    assert row["future_specific_contract_gross_return"] == pytest.approx(-0.03)
    assert row["future_specific_contract_net_return_15bp"] == pytest.approx(-0.033)


def test_invalid_signals_or_labels_are_not_fabricated():
    _, build_opportunity_ledger, _ = _api()
    index = pd.DatetimeIndex(["2026-01-02", "2026-01-05", "2026-01-06"])
    signals = {
        "momentum": pd.DataFrame(
            {"AG": [0.0, math.nan, 1.0], "CU": [1.0, 1.0, 1.0]}, index=index
        )
    }
    forward = {
        5: pd.DataFrame(
            {"AG": [0.01, 0.02, math.nan], "CU": [math.nan, 0.03, 0.04]},
            index=index,
        )
    }
    available = {
        5: pd.DataFrame(
            {
                "AG": [pd.Timestamp("2026-01-09")] * 3,
                "CU": [pd.NaT, pd.Timestamp("2026-01-12"), pd.Timestamp("2026-01-13")],
            },
            index=index,
        )
    }

    ledger = build_opportunity_ledger(
        signals=signals,
        forward_returns=forward,
        label_available_dates=available,
        horizons=(5,),
    )

    assert list(ledger["product"]) == ["CU", "CU"]
    assert not ledger.isna().any().any()


def test_horizon_set_is_frozen_to_5_10_20():
    allowed, build_opportunity_ledger, _ = _api()
    assert allowed == (5, 10, 20)
    index = pd.DatetimeIndex(["2026-01-02"])
    with pytest.raises(ValueError, match="horizon"):
        build_opportunity_ledger(
            signals={"breakout": pd.DataFrame({"AG": [1.0]}, index=index)},
            forward_returns={7: pd.DataFrame({"AG": [0.01]}, index=index)},
            label_available_dates={
                7: pd.DataFrame({"AG": [pd.Timestamp("2026-01-13")]}, index=index)
            },
            horizons=(7,),
        )
