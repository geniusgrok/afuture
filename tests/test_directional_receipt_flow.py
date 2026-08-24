import pandas as pd
import pytest


def _api():
    from afuture.directional_receipt_flow import (
        aggregate_czce_receipt_flow,
        receipt_flow_weights,
    )

    return aggregate_czce_receipt_flow, receipt_flow_weights


def test_czce_receipt_uses_only_explicit_total_row():
    aggregate, _ = _api()
    payload = {
        "AP": pd.DataFrame(
            [
                {"仓库编号": "1801", "仓单数量": 100, "当日增减": 50},
                {"仓库编号": "小计", "仓单数量": 150, "当日增减": 60},
                {"仓库编号": "总计", "仓单数量": 200, "当日增减": 20},
            ]
        )
    }

    result = aggregate(payload, products=("AP",))

    # Yesterday = 200 - 20 = 180; rising receipts are bearish.
    assert result["AP"] == pytest.approx(-20.0 / 200.0)


def test_receipt_flow_normalizes_against_larger_of_today_and_yesterday():
    aggregate, _ = _api()
    payload = {
        "CF": pd.DataFrame(
            [{"仓库编号": "总计", "仓单数量": "80", "当日增减": "-20"}]
        )
    }
    result = aggregate(payload, products=("CF",))
    # Yesterday = 100, receipt draw is bullish: -(-20)/100.
    assert result["CF"] == pytest.approx(0.20)


def test_missing_total_or_invalid_values_are_not_fabricated():
    aggregate, _ = _api()
    payload = {
        "AP": pd.DataFrame([{"仓库编号": "小计", "仓单数量": 10, "当日增减": 1}]),
        "CF": pd.DataFrame([{"仓库编号": "总计", "仓单数量": "-", "当日增减": 1}]),
    }
    assert aggregate(payload, products=("AP", "CF", "SR")) == {}


def test_receipt_flow_weights_are_signed_proportional_and_capped_at_2x():
    _, weights = _api()
    result = weights({"AP": 0.02, "CF": -0.01})
    assert result == pytest.approx({"AP": 4.0 / 3.0, "CF": -2.0 / 3.0})
    assert sum(abs(value) for value in result.values()) == pytest.approx(2.0)


def test_receipt_flow_weights_are_flat_without_real_pressure():
    _, weights = _api()
    assert weights({"AP": 0.0}) == {}
