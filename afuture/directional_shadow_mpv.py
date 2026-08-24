"""Research-only exogenous shadow-Production value bridge.

Unlike candidate-owned MPV learning, this module receives a fixed baseline shadow event
ledger. Candidate allocation therefore cannot censor the evidence stream that will be
available at later decisions. Only shadow events strictly before the decision date are
visible; same-day/future outcomes are excluded.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import pandas as pd

from .directional_mpv_research import MPVResearchOptimization, optimize_with_causal_mpv


@dataclass(frozen=True)
class ShadowMPVOptimization:
    optimization: object
    research: MPVResearchOptimization
    shadow_event_count: int
    evidence_through: pd.Timestamp | None


def optimize_with_shadow_mpv(
    *,
    shadow_events: pd.DataFrame,
    decision_date,
    reference_lots: Mapping[str, int],
    requested_lots: Mapping[str, int],
    current_lots: Mapping[str, int],
    symbol_products: Mapping[str, str],
    lot_notionals: Mapping[str, float],
    per_lot_margin: Mapping[str, float],
    equity: float,
    soft_margin_budget: float,
    max_gross_ratio: float,
    max_abs_lots: int,
    cost_rate: float,
) -> ShadowMPVOptimization:
    """Optimize from a causal slice of a fixed shadow baseline event stream."""
    cutoff = pd.Timestamp(decision_date).normalize()
    frame = shadow_events.copy()
    evidence_through: pd.Timestamp | None = None
    if frame.empty or "date" not in frame.columns:
        causal = pd.DataFrame()
    else:
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
        causal = frame[frame["date"].notna() & (frame["date"] < cutoff)].copy()
        if not causal.empty:
            evidence_through = pd.Timestamp(causal["date"].max()).normalize()

    research = optimize_with_causal_mpv(
        observed_events=causal,
        reference_lots=reference_lots,
        requested_lots=requested_lots,
        current_lots=current_lots,
        symbol_products=symbol_products,
        lot_notionals=lot_notionals,
        per_lot_margin=per_lot_margin,
        equity=float(equity),
        soft_margin_budget=float(soft_margin_budget),
        max_gross_ratio=float(max_gross_ratio),
        max_abs_lots=int(max_abs_lots),
        cost_rate=float(cost_rate),
    )
    return ShadowMPVOptimization(
        optimization=research.optimization,
        research=research,
        shadow_event_count=int(len(causal)),
        evidence_through=evidence_through,
    )
