"""Causal Product x Alpha edge estimation from completed opportunity labels."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite, sqrt

import numpy as np
import pandas as pd

from .directional_opportunity_ledger import ALLOWED_HORIZONS, completed_opportunities


@dataclass(frozen=True)
class CausalEdgeEstimate:
    product: str
    family: str
    horizon: int
    decision_date: pd.Timestamp
    support: int
    family_support: int
    global_support: int
    prior_support: float
    product_family_mean: float
    family_mean: float
    global_mean: float
    family_weight: float
    product_weight: float
    family_prior: float
    expected_gross_return: float
    standard_error: float
    insufficient_evidence: bool = False


def _finite_values(frame: pd.DataFrame) -> np.ndarray:
    values = pd.to_numeric(
        frame["future_specific_contract_gross_return"], errors="coerce"
    ).to_numpy(float)
    return values[np.isfinite(values)]


def _weight(support: int, prior_support: float) -> float:
    if support <= 0:
        return 0.0
    if prior_support <= 0.0:
        return 1.0
    return float(support) / (float(support) + float(prior_support))


def _standard_error(*candidates: np.ndarray) -> float:
    for values in candidates:
        if len(values) >= 2:
            return float(np.std(values, ddof=1) / sqrt(len(values)))
    return 0.0


def _insufficient(*, product: str, family: str, horizon: int, decision_date) -> CausalEdgeEstimate:
    nan = float("nan")
    return CausalEdgeEstimate(
        product=product,
        family=family,
        horizon=horizon,
        decision_date=pd.Timestamp(decision_date).normalize(),
        support=0,
        family_support=0,
        global_support=0,
        prior_support=0.0,
        product_family_mean=nan,
        family_mean=nan,
        global_mean=nan,
        family_weight=0.0,
        product_weight=0.0,
        family_prior=nan,
        expected_gross_return=nan,
        standard_error=nan,
        insufficient_evidence=True,
    )


def estimate_product_family_edge(
    ledger: pd.DataFrame,
    *,
    decision_date,
    product: str,
    family: str,
    horizon: int,
) -> CausalEdgeEstimate:
    """Estimate one edge using only labels available strictly before decision_date."""
    horizon = int(horizon)
    if horizon not in ALLOWED_HORIZONS:
        raise ValueError("edge horizon must be one of 5, 10, 20 sessions")
    decision = pd.to_datetime(decision_date, errors="coerce")
    if pd.isna(decision):
        raise ValueError("decision_date must be a valid date")
    product = str(product).upper()
    family = str(family).strip().lower()
    completed = completed_opportunities(ledger, decision_date=decision)
    if completed.empty:
        return _insufficient(
            product=product, family=family, horizon=horizon, decision_date=decision
        )

    frame = completed[
        pd.to_numeric(
            completed["forward_horizon_sessions"], errors="coerce"
        ).eq(horizon)
    ].copy()
    if frame.empty:
        return _insufficient(
            product=product, family=family, horizon=horizon, decision_date=decision
        )
    frame["product"] = frame["product"].astype(str).str.upper()
    frame["family"] = frame["family"].astype(str).str.lower()
    frame["future_specific_contract_gross_return"] = pd.to_numeric(
        frame["future_specific_contract_gross_return"], errors="coerce"
    )
    frame = frame[np.isfinite(frame["future_specific_contract_gross_return"].to_numpy(float))]
    if frame.empty:
        return _insufficient(
            product=product, family=family, horizon=horizon, decision_date=decision
        )

    global_values = _finite_values(frame)
    if not len(global_values):
        return _insufficient(
            product=product, family=family, horizon=horizon, decision_date=decision
        )
    global_mean = float(global_values.mean())
    global_support = int(len(global_values))

    supports = (
        frame.groupby(["product", "family"], sort=True)
        ["future_specific_contract_gross_return"]
        .count()
        .astype(float)
    )
    positive_support = supports[supports > 0.0]
    prior_support = (
        float(positive_support.median()) if len(positive_support) else 0.0
    )

    family_frame = frame[frame["family"] == family]
    family_values = _finite_values(family_frame) if not family_frame.empty else np.array([])
    family_support = int(len(family_values))
    family_mean = float(family_values.mean()) if family_support else global_mean
    family_weight = _weight(family_support, prior_support)
    family_prior = (
        family_weight * family_mean + (1.0 - family_weight) * global_mean
    )

    pf_frame = family_frame[family_frame["product"] == product]
    pf_values = _finite_values(pf_frame) if not pf_frame.empty else np.array([])
    support = int(len(pf_values))
    product_family_mean = float(pf_values.mean()) if support else family_prior
    product_weight = _weight(support, prior_support)
    expected = (
        product_weight * product_family_mean
        + (1.0 - product_weight) * family_prior
    )
    standard_error = _standard_error(pf_values, family_values, global_values)

    if not all(
        isfinite(value)
        for value in (
            global_mean,
            family_mean,
            family_prior,
            product_family_mean,
            expected,
            standard_error,
        )
    ):
        return _insufficient(
            product=product, family=family, horizon=horizon, decision_date=decision
        )

    return CausalEdgeEstimate(
        product=product,
        family=family,
        horizon=horizon,
        decision_date=pd.Timestamp(decision).normalize(),
        support=support,
        family_support=family_support,
        global_support=global_support,
        prior_support=prior_support,
        product_family_mean=product_family_mean,
        family_mean=family_mean,
        global_mean=global_mean,
        family_weight=family_weight,
        product_weight=product_weight,
        family_prior=family_prior,
        expected_gross_return=expected,
        standard_error=standard_error,
        insufficient_evidence=False,
    )


def estimate_current_edges(
    ledger: pd.DataFrame,
    *,
    decision_date,
) -> pd.DataFrame:
    """Estimate every product/family/horizon combination present in the ledger."""
    required = {"product", "family", "forward_horizon_sessions"}
    if ledger.empty:
        return pd.DataFrame()
    missing = required - set(ledger.columns)
    if missing:
        raise ValueError(f"opportunity ledger missing columns: {sorted(missing)}")
    combinations = ledger[list(required)].drop_duplicates()
    rows: list[dict] = []
    for item in combinations.itertuples(index=False):
        values = item._asdict()
        estimate = estimate_product_family_edge(
            ledger,
            decision_date=decision_date,
            product=values["product"],
            family=values["family"],
            horizon=int(values["forward_horizon_sessions"]),
        )
        rows.append(asdict(estimate))
    return pd.DataFrame(rows)
