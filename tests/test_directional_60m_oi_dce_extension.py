import pandas as pd


def _api():
    from afuture.directional_60m_oi_dce_extension import (
        EXTENDED_SUPPORTED_PRODUCTS,
        DCE_EXTENSION_PRODUCTS,
        audit_dce_extension_coverage,
        build_extended_candidate_weights,
    )

    return (
        EXTENDED_SUPPORTED_PRODUCTS,
        DCE_EXTENSION_PRODUCTS,
        audit_dce_extension_coverage,
        build_extended_candidate_weights,
    )


def test_extension_is_exact_lifecycle_union_not_outcome_selected():
    supported, extension, _, _ = _api()

    assert extension == ("J", "JM", "L", "V")
    assert supported == (
        "A", "C", "EG", "I", "J", "JM", "L", "M", "P", "PP", "TA", "V", "Y"
    )


def test_coverage_gate_requires_each_extension_product_in_both_eras():
    _, _, audit, _ = _api()
    calendar = pd.DataFrame(
        {
            "date": pd.to_datetime(
                [
                    "2023-01-03", "2023-01-04", "2025-01-02", "2025-01-03",
                ] * 4
            ),
            "product": sum(([product] * 4 for product in ("J", "JM", "L", "V")), []),
        }
    )
    rows = []
    for product in ("J", "JM", "L", "V"):
        for day in ("2023-01-03", "2023-01-04", "2025-01-02", "2025-01-03"):
            rows.append(
                {
                    "datetime": f"{day} 10:00",
                    "product": product,
                    "symbol": f"{product}2501",
                    "open": 100.0,
                    "close": 101.0,
                    "volume": 100,
                    "hold": 1000,
                }
            )
    bars = pd.DataFrame(rows)

    report = audit(calendar=calendar, extension_bars=bars, threshold=0.80)

    assert report["passed"] is True
    assert report["usable_products"] == ["J", "JM", "L", "V"]


def test_coverage_gate_fails_closed_when_one_era_is_missing():
    _, _, audit, _ = _api()
    calendar = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2023-01-03", "2025-01-02"] * 4
            ),
            "product": sum(([product] * 2 for product in ("J", "JM", "L", "V")), []),
        }
    )
    rows = []
    for product in ("J", "JM", "L", "V"):
        rows.append(
            {
                "datetime": "2023-01-03 10:00",
                "product": product,
                "symbol": f"{product}2301",
                "open": 100.0,
                "close": 101.0,
                "volume": 100,
                "hold": 1000,
            }
        )
        if product != "JM":
            rows.append(
                {
                    "datetime": "2025-01-02 10:00",
                    "product": product,
                    "symbol": f"{product}2501",
                    "open": 100.0,
                    "close": 101.0,
                    "volume": 100,
                    "hold": 1000,
                }
            )
    report = audit(
        calendar=calendar,
        extension_bars=pd.DataFrame(rows),
        threshold=0.80,
    )

    assert report["passed"] is False
    assert "JM" not in report["usable_products"]


def test_extended_candidate_reuses_same_oi_filter_and_changes_only_extension_products():
    supported, _, _, build = _api()
    index = pd.to_datetime(["2026-01-05", "2026-01-06"])
    raw = pd.DataFrame(
        {
            "JM": [0.0, 1.0],
            "AG": [0.0, 1.0],
        },
        index=index,
    )
    bars = pd.DataFrame(
        [
            {
                "datetime": "2026-01-05 10:00",
                "product": "JM",
                "symbol": "JM2605",
                "open": 100.0,
                "close": 99.0,
                "volume": 100,
                "hold": 1000,
            },
            {
                "datetime": "2026-01-05 15:00",
                "product": "JM",
                "symbol": "JM2605",
                "open": 99.0,
                "close": 98.0,
                "volume": 120,
                "hold": 1100,
            },
        ]
    )

    candidate, lagged = build(raw_weights=raw, bars_60m=bars)

    assert "JM" in supported
    assert candidate.loc[index[-1], "JM"] == 0.0
    assert candidate["AG"].equals(raw["AG"])
    assert lagged.loc[index[-1], "JM"] == -1.0
    assert bool((candidate.abs().sum(axis=1) <= raw.abs().sum(axis=1) + 1e-12).all())
