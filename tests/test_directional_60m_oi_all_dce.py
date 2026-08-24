import pandas as pd


def _api():
    from afuture.directional_60m_oi_all_dce import (
        ALL_DCE_OI_PRODUCTS,
        REMAINING_DCE_PRODUCTS,
        audit_remaining_dce_coverage,
        build_all_dce_candidate_weights,
    )

    return (
        ALL_DCE_OI_PRODUCTS,
        REMAINING_DCE_PRODUCTS,
        audit_remaining_dce_coverage,
        build_all_dce_candidate_weights,
    )


def test_supported_set_is_structurally_complete_dce_plus_existing_ta():
    supported, remaining, _, _ = _api()

    assert remaining == ("B", "CS", "EB", "LH", "PG")
    assert supported == (
        "A", "B", "C", "CS", "EB", "EG", "I", "J", "JM", "L", "LH",
        "M", "P", "PG", "PP", "TA", "V", "Y",
    )


def test_remaining_coverage_requires_each_product_in_prior_and_recent():
    _, _, audit, _ = _api()
    calendar_rows = []
    bar_rows = []
    for product in ("B", "CS", "EB", "LH", "PG"):
        for day in ("2023-01-03", "2023-01-04", "2025-01-02", "2025-01-03"):
            calendar_rows.append({"date": day, "product": product})
            bar_rows.append(
                {
                    "datetime": f"{day} 10:00",
                    "product": product,
                    "symbol": f"{product}2501",
                    "open": 100.0,
                    "close": 101.0,
                    "volume": 100.0,
                    "hold": 1000.0,
                }
            )
    report = audit(
        calendar=pd.DataFrame(calendar_rows),
        remaining_bars=pd.DataFrame(bar_rows),
        threshold=0.80,
    )

    assert report["passed"] is True
    assert report["usable_products"] == ["B", "CS", "EB", "LH", "PG"]


def test_remaining_coverage_fails_closed_when_one_era_missing():
    _, _, audit, _ = _api()
    calendar_rows = []
    bar_rows = []
    for product in ("B", "CS", "EB", "LH", "PG"):
        for day in ("2023-01-03", "2025-01-02"):
            calendar_rows.append({"date": day, "product": product})
        bar_rows.append(
            {
                "datetime": "2023-01-03 10:00",
                "product": product,
                "symbol": f"{product}2301",
                "open": 100.0,
                "close": 101.0,
                "volume": 100.0,
                "hold": 1000.0,
            }
        )
        if product != "LH":
            bar_rows.append(
                {
                    "datetime": "2025-01-02 10:00",
                    "product": product,
                    "symbol": f"{product}2501",
                    "open": 100.0,
                    "close": 101.0,
                    "volume": 100.0,
                    "hold": 1000.0,
                }
            )
    report = audit(
        calendar=pd.DataFrame(calendar_rows),
        remaining_bars=pd.DataFrame(bar_rows),
        threshold=0.80,
    )

    assert report["passed"] is False
    assert "LH" not in report["usable_products"]


def test_all_dce_overlay_reuses_same_confirmation_rule_and_never_adds_gross():
    supported, _, _, build = _api()
    index = pd.to_datetime(["2026-01-05", "2026-01-06"])
    raw = pd.DataFrame(
        {"LH": [0.0, 1.0], "AG": [0.0, 1.0]},
        index=index,
    )
    bars = pd.DataFrame(
        [
            {
                "datetime": "2026-01-05 10:00",
                "product": "LH",
                "symbol": "LH2605",
                "open": 100.0,
                "close": 99.0,
                "volume": 100.0,
                "hold": 1000.0,
            },
            {
                "datetime": "2026-01-05 15:00",
                "product": "LH",
                "symbol": "LH2605",
                "open": 99.0,
                "close": 98.0,
                "volume": 120.0,
                "hold": 1100.0,
            },
        ]
    )
    candidate, lagged = build(raw_weights=raw, bars_60m=bars)

    assert "LH" in supported
    assert candidate.loc[index[-1], "LH"] == 0.0
    assert candidate["AG"].equals(raw["AG"])
    assert lagged.loc[index[-1], "LH"] == -1.0
    assert bool((candidate.abs().sum(axis=1) <= raw.abs().sum(axis=1) + 1e-12).all())
