"""Research-only reallocation of sub-one-lot survivor targets.

This execution adapter does not change Alpha or product eligibility. It only addresses a
mechanical Production loss: a cost-approved nonzero product target may be too small to
buy even one contract at the current selected-contract open. When exact execution
evidence is available, the unusable gross can be transferred to other already-nonzero,
same-direction survivors that are themselves at least one-lot feasible and still below
the unchanged 35-lot capacity.

Missing price/contract evidence never causes reallocation. If feasible recipients do not
have enough capacity, the untransferred donor weight remains in its original product and
therefore fails closed through the normal floor stage. Total gross cannot increase and no
new product support or sign is created. Existing soft-margin/freeze/hard-risk semantics
remain authoritative downstream.
"""
from __future__ import annotations

from typing import Mapping

import numpy as np

from .directional_acceptance import PRODUCT_MULTIPLIERS, TargetLotStages
from .directional_freeze_new_risk import FreezeNewRiskDirectionalProductionAcceptance

_EPS = 1e-12


def reallocate_sub_one_lot_weights(
    *,
    equity: float,
    product_weights: Mapping[str, float],
    product_open_prices: Mapping[str, float],
    selected_symbols: Mapping[str, str],
    product_multipliers: Mapping[str, float],
    max_contract_volume: int,
) -> dict[str, float]:
    """Move only executable sub-one-lot gross into existing feasible survivors."""
    if not np.isfinite(float(equity)) or float(equity) <= 0.0:
        return {str(k).upper(): float(v) for k, v in product_weights.items()}
    if int(max_contract_volume) <= 0:
        raise ValueError("max_contract_volume must be positive")

    original = {
        str(product).upper(): float(weight)
        for product, weight in product_weights.items()
    }
    if any(not np.isfinite(weight) for weight in original.values()):
        raise ValueError("product weights must be finite")
    original_gross = float(sum(abs(weight) for weight in original.values()))
    if original_gross > 2.0 + 1e-10:
        raise ValueError("product weights exceed 2x gross")

    prices = {
        str(product).upper(): float(price)
        for product, price in product_open_prices.items()
    }
    selected = {
        str(product).upper(): str(symbol)
        for product, symbol in selected_symbols.items()
    }
    multipliers = {
        str(product).upper(): float(multiplier)
        for product, multiplier in product_multipliers.items()
    }

    donors: dict[str, float] = {}
    recipient_capacity: dict[str, float] = {}
    for product, weight in original.items():
        if abs(weight) <= _EPS:
            continue
        # Missing execution evidence is not interpreted as economic infeasibility.
        if product not in selected:
            continue
        price = float(prices.get(product, 0.0))
        multiplier = float(multipliers.get(product, 0.0))
        if not np.isfinite(price) or not np.isfinite(multiplier) or price <= 0.0 or multiplier <= 0.0:
            continue
        lot_notional = price * multiplier
        ideal_lots = float(equity) * abs(weight) / lot_notional
        if ideal_lots < 1.0 - _EPS:
            donors[product] = abs(weight)
            continue
        max_weight = int(max_contract_volume) * lot_notional / float(equity)
        capacity = max(0.0, max_weight - abs(weight))
        if capacity > _EPS:
            recipient_capacity[product] = capacity

    donor_gross = float(sum(donors.values()))
    total_capacity = float(sum(recipient_capacity.values()))
    transfer = min(donor_gross, total_capacity)
    if transfer <= _EPS or not recipient_capacity:
        return dict(original)

    result = dict(original)
    # Reduce donors proportionally. With sufficient capacity this takes every genuinely
    # sub-one-lot target to zero; with insufficient capacity the remainder stays in place.
    donor_fraction = transfer / donor_gross
    for product, magnitude in donors.items():
        new_magnitude = magnitude * (1.0 - donor_fraction)
        sign = 1.0 if original[product] > 0.0 else -1.0
        result[product] = sign * new_magnitude

    # Allocate the transfer across feasible recipients in original-weight proportions,
    # respecting exact 35-lot capacity. Saturated names drop out and the residual is
    # redistributed over the remaining existing survivors. Symbol ordering makes exact
    # floating-point ties deterministic.
    remaining = transfer
    active = set(recipient_capacity)
    while remaining > _EPS and active:
        denominator = float(sum(abs(original[p]) for p in active))
        if denominator <= _EPS:
            break
        allocations = {
            product: remaining * abs(original[product]) / denominator
            for product in active
        }
        used = 0.0
        saturated: set[str] = set()
        for product in sorted(active):
            room = float(recipient_capacity[product])
            add = min(float(allocations[product]), room)
            if add <= _EPS:
                saturated.add(product)
                continue
            sign = 1.0 if original[product] > 0.0 else -1.0
            result[product] = float(result[product]) + sign * add
            recipient_capacity[product] = room - add
            used += add
            if recipient_capacity[product] <= _EPS or add + _EPS < allocations[product]:
                saturated.add(product)
        if used <= _EPS:
            break
        remaining -= used
        active -= saturated

    # Numerical residue that could not be allocated must be put back into donors rather
    # than disappear or create support elsewhere.
    if remaining > _EPS:
        restored_fraction = remaining / donor_gross
        for product, magnitude in donors.items():
            sign = 1.0 if original[product] > 0.0 else -1.0
            result[product] = float(result[product]) + sign * magnitude * restored_fraction

    result_gross = float(sum(abs(weight) for weight in result.values()))
    if result_gross > original_gross + 1e-10:
        raise AssertionError("lot-feasibility reallocation increased gross")
    if abs(result_gross - original_gross) > 1e-9:
        raise AssertionError("lot-feasibility reallocation lost gross")
    for product, value in result.items():
        original_value = original[product]
        if abs(value) > _EPS and abs(original_value) <= _EPS:
            raise AssertionError("lot-feasibility reallocation created new support")
        if abs(value) > _EPS and np.sign(value) != np.sign(original_value):
            raise AssertionError("lot-feasibility reallocation changed target sign")
    return {product: float(value) for product, value in result.items()}


class LotFeasibleDirectionalProductionAcceptance(
    FreezeNewRiskDirectionalProductionAcceptance
):
    """Freeze-new-risk mechanics with sub-one-lot execution-feasibility reallocation."""

    def target_lot_stages(
        self,
        *,
        equity: float,
        product_weights: Mapping[str, float],
        product_open_prices: Mapping[str, float],
        selected_symbols: Mapping[str, str],
        current_lots: Mapping[str, int] | None = None,
        completed_returns: tuple[float, ...] = (),
    ) -> TargetLotStages:
        adjusted = reallocate_sub_one_lot_weights(
            equity=float(equity),
            product_weights=product_weights,
            product_open_prices=product_open_prices,
            selected_symbols=selected_symbols,
            product_multipliers=PRODUCT_MULTIPLIERS,
            max_contract_volume=self.config.max_contract_volume,
        )
        return super().target_lot_stages(
            equity=equity,
            product_weights=adjusted,
            product_open_prices=product_open_prices,
            selected_symbols=selected_symbols,
            current_lots=current_lots,
            completed_returns=completed_returns,
        )
