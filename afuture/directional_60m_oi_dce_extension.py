"""Lifecycle-defined extension of the validated 60m Price x OI confirmation overlay.

The signal implementation is intentionally reused verbatim from
``directional_60m_oi_confirmation``. This module only expands the supported information
surface with DCE J/JM/L/V after an independent coverage-only audit showed that these
products share the same 01/05/09 contract lifecycle and have adequate prior/recent 60m
history. No product is added by historical return ranking.
"""
from __future__ import annotations

from typing import Iterable

import pandas as pd

from .directional_60m_oi_confirmation import (
    SUPPORTED_PRODUCTS,
    apply_oi_confirmation_to_weights,
    build_daily_price_oi_flow,
    lag_flow_to_target_days,
)

DCE_EXTENSION_PRODUCTS = ("J", "JM", "L", "V")
EXTENDED_SUPPORTED_PRODUCTS = tuple(
    sorted(set(SUPPORTED_PRODUCTS) | set(DCE_EXTENSION_PRODUCTS))
)
COVERAGE_ERAS = {
    "prior": (pd.Timestamp("2022-08-21"), pd.Timestamp("2024-08-20")),
    "recent": (pd.Timestamp("2024-08-21"), pd.Timestamp("2026-08-20")),
}


def audit_dce_extension_coverage(
    *,
    calendar: pd.DataFrame,
    extension_bars: pd.DataFrame,
    threshold: float = 0.80,
) -> dict:
    """Fail closed unless every lifecycle-defined product clears both era coverages."""
    if not 0.0 < float(threshold) <= 1.0:
        raise ValueError("coverage threshold must be in (0, 1]")
    required_calendar = {"date", "product"}
    missing_calendar = required_calendar - set(calendar.columns)
    if missing_calendar:
        raise ValueError(
            f"coverage calendar missing columns: {sorted(missing_calendar)}"
        )
    required_bars = {"datetime", "product"}
    missing_bars = required_bars - set(extension_bars.columns)
    if missing_bars:
        raise ValueError(
            f"extension bars missing columns: {sorted(missing_bars)}"
        )

    expected = calendar.copy()
    expected["date"] = pd.to_datetime(expected["date"], errors="coerce").dt.normalize()
    expected["product"] = expected["product"].astype(str).str.upper()
    expected = expected.dropna(subset=["date", "product"])

    observed = extension_bars.copy()
    observed["datetime"] = pd.to_datetime(
        observed["datetime"], errors="coerce"
    )
    observed["date"] = observed["datetime"].dt.normalize()
    observed["product"] = observed["product"].astype(str).str.upper()
    observed = observed.dropna(subset=["date", "product"])

    rows: list[dict] = []
    coverage_by_product: dict[str, dict[str, float]] = {}
    for product in DCE_EXTENSION_PRODUCTS:
        coverage_by_product[product] = {}
        for era, (start, end) in COVERAGE_ERAS.items():
            expected_days = set(
                expected.loc[
                    (expected["product"] == product)
                    & expected["date"].between(start, end),
                    "date",
                ]
            )
            observed_days = set(
                observed.loc[
                    (observed["product"] == product)
                    & observed["date"].between(start, end),
                    "date",
                ]
            )
            covered = len(expected_days & observed_days)
            coverage = covered / max(len(expected_days), 1)
            coverage_by_product[product][era] = float(coverage)
            rows.append(
                {
                    "era": era,
                    "product": product,
                    "expected_days": int(len(expected_days)),
                    "covered_days": int(covered),
                    "coverage": float(coverage),
                }
            )

    usable = sorted(
        product
        for product in DCE_EXTENSION_PRODUCTS
        if all(
            coverage_by_product[product].get(era, 0.0) >= float(threshold)
            for era in COVERAGE_ERAS
        )
    )
    return {
        "role": "DCE 01/05/09 lifecycle 60m coverage gate",
        "strategy_evaluation": False,
        "threshold_per_era": float(threshold),
        "extension_products": list(DCE_EXTENSION_PRODUCTS),
        "usable_products": usable,
        "passed": len(usable) == len(DCE_EXTENSION_PRODUCTS),
        "rows": rows,
    }


def build_extended_candidate_weights(
    *,
    raw_weights: pd.DataFrame,
    bars_60m: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Apply the unchanged OI confirmation rule to the expanded supported product set."""
    base = raw_weights.copy().astype(float)
    base.index = pd.to_datetime(base.index, errors="coerce")
    base = base[~base.index.isna()].sort_index().fillna(0.0)
    base.columns = [str(column).upper() for column in base.columns]

    flow = build_daily_price_oi_flow(bars_60m)
    lagged = lag_flow_to_target_days(
        flow,
        target_days=base.index,
        products=base.columns,
    )
    candidate = apply_oi_confirmation_to_weights(
        raw_weights=base,
        confirming_flow=lagged,
        supported_products=EXTENDED_SUPPORTED_PRODUCTS,
    )
    candidate = candidate.reindex(index=base.index, columns=base.columns).fillna(0.0)
    if bool(
        (candidate.abs().sum(axis=1) > base.abs().sum(axis=1) + 1e-12).any()
    ):
        raise AssertionError("extended OI confirmation increased raw target gross")
    return candidate, lagged
