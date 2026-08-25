"""Research bridge from observed Production evidence to integer target optimization.

This module is intentionally outside live runtime. It may aggregate already-observed audit
events, but it never consumes ex-post counterfactual labels.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite

import pandas as pd

from .directional_integer_optimizer import IntegerOptimizationResult, optimize_integer_targets
from .directional_mpv import CausalProductValueEstimate, estimate_causal_product_value
from .directional_mpv_attribution import observed_product_evidence


@dataclass(frozen=True)
class MPVResearchOptimization:
    optimization: IntegerOptimizationResult
    product_estimates: dict[str, CausalProductValueEstimate]
    evidence: pd.DataFrame


def optimize_with_causal_mpv(
    *,
    observed_events: pd.DataFrame,
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
) -> MPVResearchOptimization:
    """Optimize immediate open-to-close Production value using observed intraday evidence."""
    if not isfinite(float(cost_rate)) or float(cost_rate) < 0.0:
        raise ValueError("cost_rate must be finite and nonnegative")
    evidence = observed_product_evidence(
        events=observed_events,
        pnl_actions=("intraday",),
    )
    total_support = float(evidence["lot_segment_exposure"].sum()) if not evidence.empty else 0.0
    products = sorted({str(value).upper() for value in symbol_products.values()})
    estimates = {
        product: estimate_causal_product_value(evidence=evidence, product=product)
        for product in products
    }
    reference = {
        str(symbol): int(volume) for symbol, volume in reference_lots.items() if int(volume)
    }
    if total_support <= 0.0:
        fallback = IntegerOptimizationResult(
            target_lots=dict(reference),
            reference_objective=0.0,
            objective_value=0.0,
            fallback_reason="insufficient causal MPV evidence",
        )
        return MPVResearchOptimization(
            optimization=fallback,
            product_estimates=estimates,
            evidence=evidence,
        )

    current = {str(symbol): int(volume) for symbol, volume in current_lots.items() if int(volume)}
    product_by_symbol = {
        str(symbol): str(product).upper() for symbol, product in symbol_products.items()
    }

    def objective(target: Mapping[str, int]) -> float:
        gross_alpha = 0.0
        for symbol, volume in target.items():
            product = product_by_symbol[str(symbol)]
            estimate = estimates[product]
            if estimate.insufficient_evidence:
                return float("nan")
            gross_alpha += abs(int(volume)) * estimate.expected_gross_alpha_per_lot_segment
        turnover_cost = 0.0
        for symbol in set(current) | set(target):
            delta = int(target.get(symbol, 0)) - int(current.get(symbol, 0))
            if not delta:
                continue
            turnover_cost += abs(delta) * float(lot_notionals[symbol]) * float(cost_rate)
        return float(gross_alpha - turnover_cost)

    optimization = optimize_integer_targets(
        reference_lots=reference,
        requested_lots=requested_lots,
        current_lots=current,
        lot_notionals=lot_notionals,
        per_lot_margin=per_lot_margin,
        equity=float(equity),
        soft_margin_budget=float(soft_margin_budget),
        max_gross_ratio=float(max_gross_ratio),
        max_abs_lots=int(max_abs_lots),
        objective=objective,
    )
    return MPVResearchOptimization(
        optimization=optimization,
        product_estimates=estimates,
        evidence=evidence,
    )
