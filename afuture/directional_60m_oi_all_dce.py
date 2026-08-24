"""Structurally complete DCE extension of the validated 60m OI confirmation overlay.

The signal rule is unchanged. This module only expands the supported information surface
from the validated 13-product set to every DCE root in the frozen 50-product universe,
plus the already-covered TA root. B/CS/EB/LH/PG are included because they are exactly the
remaining DCE products and have independent prior/recent 60m coverage evidence; no root is
selected from strategy outcome.
"""
from __future__ import annotations

import pandas as pd

from .directional_60m_oi_confirmation import (
    apply_oi_confirmation_to_weights,
    build_daily_price_oi_flow,
    lag_flow_to_target_days,
)
from .directional_60m_oi_dce_extension import EXTENDED_SUPPORTED_PRODUCTS

REMAINING_DCE_PRODUCTS = ("B", "CS", "EB", "LH", "PG")
ALL_DCE_OI_PRODUCTS = tuple(
    sorted(set(EXTENDED_SUPPORTED_PRODUCTS) | set(REMAINING_DCE_PRODUCTS))
)
COVERAGE_ERAS = {
    "prior": (pd.Timestamp("2022-08-21"), pd.Timestamp("2024-08-20")),
    "recent": (pd.Timestamp("2024-08-21"), pd.Timestamp("2026-08-20")),
}


def audit_remaining_dce_coverage(
    *,
    calendar: pd.DataFrame,
    remaining_bars: pd.DataFrame,
    threshold: float = 0.80,
) -> dict:
    """Fail closed unless every remaining structural DCE root clears both eras."""
    if not 0.0 < float(threshold) <= 1.0:
        raise ValueError("coverage threshold must be in (0, 1]")
    missing_calendar = {"date", "product"} - set(calendar.columns)
    if missing_calendar:
        raise ValueError(
            f"coverage calendar missing columns: {sorted(missing_calendar)}"
        )
    missing_bars = {"datetime", "product"} - set(remaining_bars.columns)
    if missing_bars:
        raise ValueError(
            f"remaining DCE bars missing columns: {sorted(missing_bars)}"
        )

    expected = calendar.copy()
    expected["date"] = pd.to_datetime(expected["date"], errors="coerce").dt.normalize()
    expected["product"] = expected["product"].astype(str).str.upper()
    expected = expected.dropna(subset=["date", "product"])

    observed = remaining_bars.copy()
    observed["datetime"] = pd.to_datetime(
        observed["datetime"], errors="coerce"
    )
    observed["date"] = observed["datetime"].dt.normalize()
    observed["product"] = observed["product"].astype(str).str.upper()
    observed = observed.dropna(subset=["date", "product"])

    rows: list[dict] = []
    coverage_by_product: dict[str, dict[str, float]] = {}
    for product in REMAINING_DCE_PRODUCTS:
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
        for product in REMAINING_DCE_PRODUCTS
        if all(
            coverage_by_product[product].get(era, 0.0) >= float(threshold)
            for era in COVERAGE_ERAS
        )
    )
    return {
        "role": "remaining structural DCE 60m coverage gate",
        "strategy_evaluation": False,
        "threshold_per_era": float(threshold),
        "remaining_products": list(REMAINING_DCE_PRODUCTS),
        "usable_products": usable,
        "passed": len(usable) == len(REMAINING_DCE_PRODUCTS),
        "rows": rows,
    }


def build_all_dce_candidate_weights(
    *,
    raw_weights: pd.DataFrame,
    bars_60m: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Apply the unchanged OI confirmation rule to all structurally covered DCE roots."""
    base = raw_weights.copy().astype(float)
    base.index = pd.DatetimeIndex(
        pd.to_datetime(base.index, errors="coerce")
    ).normalize()
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
        supported_products=ALL_DCE_OI_PRODUCTS,
    )
    candidate = candidate.reindex(index=base.index, columns=base.columns).fillna(0.0)
    if bool(
        (candidate.abs().sum(axis=1) > base.abs().sum(axis=1) + 1e-12).any()
    ):
        raise AssertionError("all-DCE OI confirmation increased raw target gross")
    return candidate, lagged
