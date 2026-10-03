"""Pure S1 warehouse-warrant signal; no account state or live activation.

The caller verifies original bytes and provides independently established version
availability. A report date or HTTP Last-Modified is never availability evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import isfinite, log1p
from statistics import median
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class SupplyObservation:
    product: str
    statistical_day: date
    value: float
    available_at: datetime
    source_sha256: str
    source_id: str
    scope: str = "SHFE_warehouse_registered_warrant_tonnes"
    qualified: bool = True

    def __post_init__(self) -> None:
        if (
            not self.product.isalpha()
            or self.product != self.product.upper()
            or not isfinite(self.value)
            or self.value < 0
            or self.available_at.tzinfo is None
            or self.available_at.utcoffset() is None
            or len(self.source_sha256) != 64
            or any(c not in "0123456789abcdef" for c in self.source_sha256)
            or not self.source_id
            or not self.scope
            or type(self.qualified) is not bool
        ):
            raise ValueError("invalid supply observation or availability evidence identity")


def warrant_signal(
    observations: tuple[SupplyObservation, ...],
    *,
    product: str,
    completed_day: date,
    decision_at: datetime,
    carry: float,
    execution_at: datetime | None = None,
) -> tuple[float, str]:
    """One causal seasonal-state/change/carry hypothesis; missing input is neutral.

    Latest known version wins, including a disqualified revision. Seasonal years
    and four-week comparison refer to the latest statistical day, never future
    years or an eventual final version. No quantities are compared across products.
    """
    if decision_at.tzinfo is None or decision_at.utcoffset() is None:
        raise ValueError("decision time requires an explicit timezone")
    if execution_at is not None and (
        execution_at.tzinfo is None
        or execution_at.utcoffset() is None
        or execution_at <= decision_at
    ):
        raise ValueError("execution time must be aware and later than decision")
    versions: dict[date, SupplyObservation] = {}
    for row in sorted(observations, key=lambda r: (r.available_at, r.statistical_day)):
        if (
            row.product != product
            or row.available_at > decision_at
            or row.statistical_day > completed_day
        ):
            continue
        prior = versions.get(row.statistical_day)
        if prior is not None and row.available_at == prior.available_at and row != prior:
            raise ValueError("ambiguous simultaneous supply versions")
        versions[row.statistical_day] = row
    if not versions:
        return 0.0, "no_available_version"
    latest = versions[max(versions)]
    if not latest.qualified:
        return 0.0, "untrusted_latest_version"
    if (completed_day - latest.statistical_day).days > 10:
        return 0.0, "stale"
    if (
        execution_at is not None
        and (
            execution_at.astimezone(ZoneInfo("Asia/Shanghai")).date() - latest.statistical_day
        ).days
        > 10
    ):
        return 0.0, "stale_at_execution"
    history = [r for r in versions.values() if r.qualified and r.scope == latest.scope]
    seasonal: list[float] = []
    for year in (latest.statistical_day.year - 2, latest.statistical_day.year - 1):
        samples = [
            r.value
            for r in history
            if r.statistical_day.year == year
            and min(
                abs(
                    r.statistical_day.isocalendar().week - latest.statistical_day.isocalendar().week
                ),
                52
                - abs(
                    r.statistical_day.isocalendar().week - latest.statistical_day.isocalendar().week
                ),
            )
            <= 4
        ]
        if len(samples) < 4:
            return 0.0, "seasonal_warmup"
        seasonal.append(median(samples))
    older = [
        r
        for r in history
        if timedelta(days=21) <= latest.statistical_day - r.statistical_day <= timedelta(days=35)
    ]
    if not older:
        return 0.0, "change_warmup"
    reference = min(
        older,
        key=lambda r: (
            abs((latest.statistical_day - r.statistical_day).days - 28),
            r.statistical_day,
        ),
    )
    anomaly = log1p(latest.value) - log1p(median(seasonal))
    change = log1p(latest.value) - log1p(reference.value)
    if not isfinite(carry):
        return 0.0, "no_actual_term_structure"
    if anomaly < 0 and change < 0 and carry > 0:
        return 1.0, "scarcity_confirmed"
    if anomaly > 0 and change > 0 and carry < 0:
        return -1.0, "abundance_confirmed"
    return 0.0, "state_change_curve_conflict"


def specific_carry_pairs(raw: pd.DataFrame) -> pd.DataFrame:
    """Reuse G's frozen concrete-contract pair rule, without its failed carry budget."""
    eligible = raw.loc[
        raw.volume.ge(1000)
        & raw.hold.ge(5000)
        & raw.settle.gt(0)
        & raw.close.gt(0)
        & (raw.delivery - raw.date).dt.days.ge(20)
    ].copy()
    eligible.sort_values(
        ["date", "product", "hold", "volume", "symbol"],
        ascending=[True, True, False, False, True],
        inplace=True,
    )
    top = eligible.groupby(["date", "product"], sort=False).head(4)
    pairs = top.merge(top, on=["date", "product"], suffixes=("_near", "_far"))
    pairs["gap_days"] = (pairs.delivery_far - pairs.delivery_near).dt.days
    pairs = pairs.loc[pairs.gap_days.ge(30)].copy()
    pairs["oi_sum"] = pairs.hold_near + pairs.hold_far
    pairs.sort_values(
        ["date", "product", "oi_sum", "delivery_near", "delivery_far", "symbol_near", "symbol_far"],
        ascending=[True, True, False, True, True, True, True],
        inplace=True,
    )
    pairs = pairs.drop_duplicates(["date", "product"])
    pairs["annualized_carry"] = 365 * np.log(pairs.settle_near / pairs.settle_far) / pairs.gap_days
    return pairs
