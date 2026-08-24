"""Production-aligned marginal-value primitives for directional research.

This module is decision-side only. It accepts point-in-time evidence and expresses every
marginal value component in account currency. Ex-post counterfactual labels belong in
``directional_mpv_attribution`` and must never be imported here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import isfinite


def _legal_one_lot_transition(*, current: int, requested: int, delta: int) -> bool:
    proposed = current + delta
    if requested == 0:
        return abs(proposed) < abs(current) if current else False

    requested_sign = 1 if requested > 0 else -1
    current_sign = 0 if current == 0 else (1 if current > 0 else -1)
    proposed_sign = 0 if proposed == 0 else (1 if proposed > 0 else -1)

    if current_sign and current_sign != requested_sign:
        return abs(proposed) < abs(current) and proposed_sign in (current_sign, 0)

    if proposed_sign not in (0, requested_sign):
        return False
    if abs(current) > abs(requested):
        return abs(proposed) < abs(current)
    return abs(proposed) <= abs(requested)


@dataclass(frozen=True)
class MarginalLotAction:
    decision_date: date
    evidence_through: date
    product: str
    symbol: str
    delta_lots: int
    current_lots: int
    requested_lots: int
    lot_notional: float
    per_lot_margin: float
    equity: float
    margin_utilization: float
    available_ratio: float
    governor_scale: float
    roll_required: bool

    def __post_init__(self) -> None:
        if self.evidence_through >= self.decision_date:
            raise ValueError("evidence_through must precede decision_date")
        if int(self.delta_lots) not in (-1, 1) or int(self.delta_lots) != self.delta_lots:
            raise ValueError("delta_lots must be exactly -1 or +1")
        if int(self.current_lots) != self.current_lots or int(self.requested_lots) != self.requested_lots:
            raise ValueError("current_lots and requested_lots must be integers")
        if not str(self.product).strip() or not str(self.symbol).strip():
            raise ValueError("product and symbol are required")
        for name in ("lot_notional", "per_lot_margin", "equity"):
            value = float(getattr(self, name))
            if not isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")
        for name in ("margin_utilization", "available_ratio", "governor_scale"):
            value = float(getattr(self, name))
            if not isfinite(value):
                raise ValueError(f"{name} must be finite")
        if not 0.0 <= float(self.margin_utilization) <= 1.0:
            raise ValueError("margin_utilization must be in [0, 1]")
        if not 0.0 <= float(self.available_ratio) <= 1.0:
            raise ValueError("available_ratio must be in [0, 1]")
        if not 0.0 <= float(self.governor_scale) <= 1.0:
            raise ValueError("governor_scale must be in [0, 1]")
        if not _legal_one_lot_transition(
            current=int(self.current_lots),
            requested=int(self.requested_lots),
            delta=int(self.delta_lots),
        ):
            raise ValueError("marginal action violates requested directional intent")


@dataclass(frozen=True)
class MarginalProductionValue:
    action: MarginalLotAction
    expected_incremental_gross_alpha: float
    expected_incremental_transaction_cost: float
    margin_opportunity_cost: float
    turnover_penalty: float
    downside_risk_penalty: float
    correlation_penalty: float
    concentration_penalty: float
    roll_execution_penalty: float

    @property
    def total_value(self) -> float:
        return float(
            self.expected_incremental_gross_alpha
            - self.expected_incremental_transaction_cost
            - self.margin_opportunity_cost
            - self.turnover_penalty
            - self.downside_risk_penalty
            - self.correlation_penalty
            - self.concentration_penalty
            - self.roll_execution_penalty
        )


def score_marginal_production_value(
    action: MarginalLotAction,
    *,
    expected_incremental_gross_alpha: float,
    expected_incremental_transaction_cost: float,
    margin_opportunity_cost: float,
    turnover_penalty: float,
    downside_risk_penalty: float,
    correlation_penalty: float,
    concentration_penalty: float,
    roll_execution_penalty: float,
) -> MarginalProductionValue:
    components = {
        "expected_incremental_gross_alpha": float(expected_incremental_gross_alpha),
        "expected_incremental_transaction_cost": float(expected_incremental_transaction_cost),
        "margin_opportunity_cost": float(margin_opportunity_cost),
        "turnover_penalty": float(turnover_penalty),
        "downside_risk_penalty": float(downside_risk_penalty),
        "correlation_penalty": float(correlation_penalty),
        "concentration_penalty": float(concentration_penalty),
        "roll_execution_penalty": float(roll_execution_penalty),
    }
    for name, value in components.items():
        if not isfinite(value):
            raise ValueError(f"{name} must be finite")
        if name != "expected_incremental_gross_alpha" and value < 0.0:
            raise ValueError(f"{name} must be nonnegative")
    return MarginalProductionValue(action=action, **components)


@dataclass(frozen=True)
class CausalProductValueEstimate:
    product: str
    product_support: float
    prior_support: float
    product_weight: float
    global_fallback_weight: float
    product_rate_gross: float
    global_rate_gross: float
    product_rate_cost: float
    global_rate_cost: float
    product_rate_net: float
    global_rate_net: float
    expected_gross_alpha_per_lot_segment: float
    expected_transaction_cost_per_lot_segment: float
    expected_net_alpha_per_lot_segment: float
    insufficient_evidence: bool = False


def estimate_causal_product_value(*, evidence, product: str) -> CausalProductValueEstimate:
    """Shrink completed product Production economics toward completed global evidence.

    The prior strength is the cross-sectional median completed lot-segment support. It is
    derived from the evidence snapshot rather than a searched lookback/threshold knob.
    """
    required = {
        "gross_pnl",
        "transaction_cost",
        "net_alpha",
        "lot_segment_exposure",
    }
    if evidence is None or not required.issubset(set(getattr(evidence, "columns", ()))):
        return CausalProductValueEstimate(
            product=str(product).upper(),
            product_support=0.0,
            prior_support=0.0,
            product_weight=0.0,
            global_fallback_weight=1.0,
            product_rate_gross=0.0,
            global_rate_gross=0.0,
            product_rate_cost=0.0,
            global_rate_cost=0.0,
            product_rate_net=0.0,
            global_rate_net=0.0,
            expected_gross_alpha_per_lot_segment=0.0,
            expected_transaction_cost_per_lot_segment=0.0,
            expected_net_alpha_per_lot_segment=0.0,
            insufficient_evidence=True,
        )

    frame = evidence.copy()
    frame.index = [str(value).upper() for value in frame.index]
    support = frame["lot_segment_exposure"].astype(float).clip(lower=0.0)
    positive_support = support[support > 0.0]
    total_support = float(positive_support.sum())
    if total_support <= 0.0:
        return CausalProductValueEstimate(
            product=str(product).upper(),
            product_support=0.0,
            prior_support=0.0,
            product_weight=0.0,
            global_fallback_weight=1.0,
            product_rate_gross=0.0,
            global_rate_gross=0.0,
            product_rate_cost=0.0,
            global_rate_cost=0.0,
            product_rate_net=0.0,
            global_rate_net=0.0,
            expected_gross_alpha_per_lot_segment=0.0,
            expected_transaction_cost_per_lot_segment=0.0,
            expected_net_alpha_per_lot_segment=0.0,
            insufficient_evidence=True,
        )

    global_gross = float(frame.loc[support > 0.0, "gross_pnl"].astype(float).sum()) / total_support
    global_cost = float(frame.loc[support > 0.0, "transaction_cost"].astype(float).sum()) / total_support
    global_net = float(frame.loc[support > 0.0, "net_alpha"].astype(float).sum()) / total_support
    prior_support = float(positive_support.median())

    key = str(product).upper()
    product_support = float(support.get(key, 0.0))
    if product_support > 0.0:
        row = frame.loc[key]
        if getattr(row, "ndim", 1) != 1:
            row = row.iloc[-1]
        product_gross = float(row["gross_pnl"]) / product_support
        product_cost = float(row["transaction_cost"]) / product_support
        product_net = float(row["net_alpha"]) / product_support
        product_weight = product_support / (product_support + prior_support)
    else:
        product_gross = global_gross
        product_cost = global_cost
        product_net = global_net
        product_weight = 0.0
    global_weight = 1.0 - product_weight

    return CausalProductValueEstimate(
        product=key,
        product_support=product_support,
        prior_support=prior_support,
        product_weight=product_weight,
        global_fallback_weight=global_weight,
        product_rate_gross=product_gross,
        global_rate_gross=global_gross,
        product_rate_cost=product_cost,
        global_rate_cost=global_cost,
        product_rate_net=product_net,
        global_rate_net=global_net,
        expected_gross_alpha_per_lot_segment=(
            product_weight * product_gross + global_weight * global_gross
        ),
        expected_transaction_cost_per_lot_segment=(
            product_weight * product_cost + global_weight * global_cost
        ),
        expected_net_alpha_per_lot_segment=(
            product_weight * product_net + global_weight * global_net
        ),
        insufficient_evidence=False,
    )
