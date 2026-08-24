import math

import pandas as pd
import pytest


def _api():
    from afuture.directional_causal_edge import estimate_product_family_edge

    return estimate_product_family_edge


def _row(*, signal_date, available, product, family, horizon, gross):
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


def test_future_labels_do_not_change_current_estimate():
    estimate = _api()
    rows = [
        _row(
            signal_date="2026-01-02",
            available="2026-01-09",
            product="AG",
            family="breakout",
            horizon=5,
            gross=0.04,
        ),
        _row(
            signal_date="2026-01-05",
            available="2026-01-12",
            product="CU",
            family="breakout",
            horizon=5,
            gross=0.02,
        ),
    ]
    decision = pd.Timestamp("2026-01-20")
    base = estimate(
        pd.DataFrame(rows),
        decision_date=decision,
        product="AG",
        family="breakout",
        horizon=5,
    )
    future = pd.DataFrame(
        rows
        + [
            _row(
                signal_date="2026-01-06",
                available=decision,
                product="AG",
                family="breakout",
                horizon=5,
                gross=999999.0,
            )
        ]
    )
    with_future = estimate(
        future,
        decision_date=decision,
        product="AG",
        family="breakout",
        horizon=5,
    )

    assert with_future.expected_gross_return == base.expected_gross_return
    assert with_future.support == base.support
    assert with_future.global_mean == base.global_mean


def test_sparse_product_family_shrinks_to_family_then_global_instead_of_zero():
    estimate = _api()
    rows = [
        _row(
            signal_date="2026-01-02",
            available="2026-01-09",
            product="AG",
            family="breakout",
            horizon=5,
            gross=0.30,
        )
    ]
    for index, value in enumerate([0.08, 0.10, 0.12, 0.14]):
        rows.append(
            _row(
                signal_date=f"2026-01-{3 + index:02d}",
                available=f"2026-01-{10 + index:02d}",
                product="CU",
                family="breakout",
                horizon=5,
                gross=value,
            )
        )
    for index, value in enumerate([-0.02, 0.00, 0.02]):
        rows.append(
            _row(
                signal_date=f"2026-01-{8 + index:02d}",
                available=f"2026-01-{15 + index:02d}",
                product="RB",
                family="reversal",
                horizon=5,
                gross=value,
            )
        )

    result = estimate(
        pd.DataFrame(rows),
        decision_date=pd.Timestamp("2026-02-01"),
        product="AG",
        family="breakout",
        horizon=5,
    )

    assert result.insufficient_evidence is False
    assert result.support == 1
    assert 0.0 < result.product_weight < 1.0
    assert result.family_prior < 0.30
    assert result.family_prior < result.expected_gross_return < 0.30
    assert math.isfinite(result.standard_error)
    assert result.standard_error >= 0.0


def test_prior_support_is_median_positive_product_family_support_for_horizon():
    estimate = _api()
    rows = []
    specs = [
        ("AG", "breakout", 1, 0.05),
        ("CU", "breakout", 3, 0.03),
        ("RB", "reversal", 5, 0.01),
    ]
    day = 1
    for product, family, count, value in specs:
        for _ in range(count):
            rows.append(
                _row(
                    signal_date=f"2026-01-{day:02d}",
                    available=f"2026-02-{day:02d}",
                    product=product,
                    family=family,
                    horizon=10,
                    gross=value,
                )
            )
            day += 1

    result = estimate(
        pd.DataFrame(rows),
        decision_date=pd.Timestamp("2026-03-01"),
        product="AG",
        family="breakout",
        horizon=10,
    )

    assert result.prior_support == 3.0
    assert result.support == 1


def test_no_completed_global_evidence_is_explicitly_insufficient():
    estimate = _api()
    ledger = pd.DataFrame(
        [
            _row(
                signal_date="2026-01-02",
                available="2026-02-01",
                product="AG",
                family="breakout",
                horizon=20,
                gross=0.10,
            )
        ]
    )
    result = estimate(
        ledger,
        decision_date=pd.Timestamp("2026-02-01"),
        product="AG",
        family="breakout",
        horizon=20,
    )
    assert result.insufficient_evidence is True
    assert math.isnan(result.expected_gross_return)
