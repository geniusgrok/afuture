import pandas as pd
import pytest


def _api():
    from afuture.directional_member_flow import (
        aggregate_member_flow,
        member_flow_weights,
    )

    return aggregate_member_flow, member_flow_weights


def _rank_frame(product, symbol, rows):
    return pd.DataFrame(
        [
            {
                "rank": index + 1,
                "symbol": symbol,
                "variety": product,
                "long_open_interest": long_oi,
                "long_open_interest_chg": long_chg,
                "short_open_interest": short_oi,
                "short_open_interest_chg": short_chg,
            }
            for index, (long_oi, long_chg, short_oi, short_chg) in enumerate(rows)
        ]
    )


def test_czce_prefers_exchange_published_product_summary_without_double_counting_contracts():
    aggregate, _ = _api()
    payload = {
        "AP": _rank_frame("AP", "AP", [(100, 20, 80, 0), (50, 10, 70, -10)]),
        "AP701": _rank_frame("AP", "AP701", [(999, 999, 999, -999)]),
    }

    result = aggregate(payload, exchange="CZCE", products=("AP",))

    # numerator = (20 + 10) - (0 - 10) = 40; denominator = 300.
    assert result["AP"] == pytest.approx(40.0 / 300.0)


def test_shfe_sums_contract_top20_and_ignores_rank_beyond_20():
    aggregate, _ = _api()
    ag1 = _rank_frame("AG", "AG2610", [(100, 10, 80, 0)] * 20 + [(10_000, 10_000, 1, 0)])
    ag2 = _rank_frame("AG", "AG2612", [(50, -5, 40, 5)] * 20)
    payload = {"ag2610": ag1, "ag2612": ag2}

    result = aggregate(payload, exchange="SHFE", products=("AG",))

    numerator = (20 * 10 + 20 * -5) - (20 * 0 + 20 * 5)
    denominator = 20 * (100 + 80) + 20 * (50 + 40)
    assert result["AG"] == pytest.approx(numerator / denominator)


def test_missing_or_zero_ranked_open_interest_is_not_fabricated():
    aggregate, _ = _api()
    payload = {
        "AG2612": _rank_frame("AG", "AG2612", [(0, 50, 0, -50)]),
    }
    result = aggregate(payload, exchange="SHFE", products=("AG", "CU"))
    assert result == {}


def test_member_flow_weights_are_parameter_free_proportional_and_gross_capped():
    _, weights = _api()
    result = weights({"AG": 0.03, "CU": -0.01, "RB": 0.0})
    assert result == pytest.approx({"AG": 1.5, "CU": -0.5})
    assert sum(abs(value) for value in result.values()) == pytest.approx(2.0)


def test_member_flow_weights_do_not_invent_direction_when_all_pressure_is_zero():
    _, weights = _api()
    assert weights({"AG": 0.0, "CU": 0.0}) == {}
