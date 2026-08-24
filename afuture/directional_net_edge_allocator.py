"""Research-only monetary net-edge bridge to the deterministic integer optimizer."""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Mapping

from .directional_integer_optimizer import (
    IntegerOptimizationResult,
    optimize_integer_targets,
)


@dataclass(frozen=True)
class NetEdgeAllocationResult:
    optimization: IntegerOptimizationResult
    expected_gross_alpha: float
    transition_cost: float
    objective_value: float


def optimize_net_edge_targets(
    *,
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
    expected_product_returns: Mapping[str, float],
    cost_rate: float,
) -> NetEdgeAllocationResult:
    """Choose legal integer lots by expected gross alpha less exact transition cost.

    ``expected_product_returns`` are already direction-conditioned causal edge estimates.
    Therefore the objective uses absolute lot exposure and never inverts caller-provided
    directional intent.  Risk authority stays in the existing optimizer/downstream path.
    """
    rate = float(cost_rate)
    if not isfinite(rate) or rate < 0.0:
        raise ValueError("cost_rate must be finite and nonnegative")

    symbols = sorted(
        set(str(item) for item in reference_lots)
        | set(str(item) for item in requested_lots)
        | set(str(item) for item in current_lots)
    )
    products = {str(symbol): str(product).upper() for symbol, product in symbol_products.items()}
    missing_products = [symbol for symbol in symbols if symbol not in products]
    if missing_products:
        raise ValueError(f"missing symbol product mapping: {missing_products}")

    edges: dict[str, float] = {}
    for raw_product, raw_value in expected_product_returns.items():
        value = float(raw_value)
        if not isfinite(value):
            raise ValueError(f"non-finite expected product return: {raw_product}")
        edges[str(raw_product).upper()] = value

    notionals = {str(symbol): float(value) for symbol, value in lot_notionals.items()}
    current = {
        str(symbol): int(volume)
        for symbol, volume in current_lots.items()
        if int(volume)
    }

    def expected_gross_alpha(target: Mapping[str, int]) -> float:
        total = 0.0
        for symbol, volume in target.items():
            symbol = str(symbol)
            product = products[symbol]
            total += (
                abs(int(volume))
                * notionals[symbol]
                * float(edges.get(product, 0.0))
            )
        return float(total)

    def transition_cost(target: Mapping[str, int]) -> float:
        total = 0.0
        for symbol in set(current) | set(str(item) for item in target):
            delta = abs(int(target.get(symbol, 0)) - int(current.get(symbol, 0)))
            if delta:
                total += delta * notionals[symbol] * rate
        return float(total)

    def objective(target: Mapping[str, int]) -> float:
        return expected_gross_alpha(target) - transition_cost(target)

    optimization = optimize_integer_targets(
        reference_lots=reference_lots,
        requested_lots=requested_lots,
        current_lots=current_lots,
        lot_notionals=lot_notionals,
        per_lot_margin=per_lot_margin,
        equity=equity,
        soft_margin_budget=soft_margin_budget,
        max_gross_ratio=max_gross_ratio,
        max_abs_lots=max_abs_lots,
        objective=objective,
    )
    final = optimization.target_lots
    gross = expected_gross_alpha(final)
    cost = transition_cost(final)
    return NetEdgeAllocationResult(
        optimization=optimization,
        expected_gross_alpha=gross,
        transition_cost=cost,
        objective_value=gross - cost,
    )
