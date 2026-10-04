"""Research-only joint integer projection before the original account risk chain."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from math import floor, isfinite, nextafter

from adaptive_alpha_research import PRODUCT_MULTIPLIERS
from holding_exit_research import HoldingExitAccount


@dataclass(frozen=True)
class IntegerProjection:
    weights: dict[str, float]
    lots: dict[str, int]
    original_budget: float
    available_budget: float
    projected_notional: float
    floor_notional: float
    unavailable_notional: float
    volume_clipping_notional: float
    floor_error: float
    projected_error: float
    rounded_up: tuple[str, ...]


def _weight_for_lots(quantity: int, lot_notional: float, equity: float) -> float:
    """Represent an integer request without losing one lot to float division."""
    if not quantity:
        return 0.0
    absolute = abs(quantity)
    weight = absolute * lot_notional / equity
    for _ in range(8):
        realized = floor(equity * weight / lot_notional)
        if realized == absolute:
            return weight if quantity > 0 else -weight
        if realized > absolute:
            raise ValueError("integer projection weight exceeds requested lots")
        weight = nextafter(weight, float("inf"))
    raise ValueError("integer projection cannot be represented faithfully")


def project_integer_targets(
    *,
    equity: float,
    product_weights: Mapping[str, float],
    product_open_prices: Mapping[str, float],
    selected_symbols: Mapping[str, str],
    multipliers: Mapping[str, float],
    max_lots: int = 35,
) -> IntegerProjection:
    """Greedily improve squared notional tracking error within original gross.

    Start with the original per-product floor. A supported product can receive at
    most one additional lot, only if floor-to-ceil reduces its squared RMB error.
    Improvements are sorted descending, then by product/symbol, without returns.
    This is a deterministic greedy projection, not a claim of global optimality.
    Missing-contract budgets and whole lots removed by the 35-lot cap stay idle.
    """
    if not isfinite(equity) or equity <= 0 or not 0 < max_lots <= 35:
        raise ValueError("invalid projection equity or lot cap")
    weights = {str(p).upper(): float(w) for p, w in product_weights.items()}
    if any(not isfinite(w) for w in weights.values()):
        raise ValueError("projection requires finite weights")
    if sum(abs(w) for w in weights.values()) > 2.0 + 1e-10:
        raise ValueError("projection input exceeds original 2x gross")

    original = sum(abs(w) for w in weights.values()) * equity
    result_weights = dict(weights)
    lots: dict[str, int] = {}
    rows: list[tuple[str, str, float, float, int]] = []
    candidates: list[tuple[float, str, str, float, int]] = []
    unavailable = clipped = floor_notional = floor_error = 0.0
    for product, weight in sorted(weights.items()):
        if abs(weight) <= 1e-15:
            result_weights[product] = 0.0
            continue
        desired = equity * abs(weight)
        symbol = selected_symbols.get(product)
        price = float(product_open_prices.get(product, 0.0))
        if symbol is None or not isfinite(price) or price <= 0:
            unavailable += desired
            continue
        multiplier = float(multipliers.get(product, 0.0))
        if not isfinite(multiplier) or multiplier <= 0:
            raise ValueError(f"projection lacks a verified positive multiplier: {product}")
        notional = price * multiplier
        if not isfinite(notional):
            raise ValueError("non-finite lot notional")
        if symbol in {row[1] for row in rows}:
            raise ValueError("two products select the same contract")
        unconstrained = floor(desired / notional)
        quantity = min(unconstrained, max_lots)
        sign = 1 if weight > 0 else -1
        if quantity:
            lots[symbol] = sign * quantity
        floor_notional += quantity * notional
        clipped += (unconstrained - quantity) * notional
        error = (desired - quantity * notional) ** 2
        floor_error += error
        rows.append((product, symbol, desired, notional, sign))
        improvement = error - (desired - (quantity + 1) * notional) ** 2
        if quantity < max_lots and improvement > 0:
            candidates.append((improvement, product, symbol, notional, sign))

    available = min(original, equity * 2.0) - unavailable - clipped
    projected = floor_notional
    rounded_up = []
    for _improvement, product, symbol, notional, sign in sorted(
        candidates, key=lambda row: (-row[0], row[1], row[2])
    ):
        if projected + notional > available:
            continue
        lots[symbol] = lots.get(symbol, 0) + sign
        projected += notional
        rounded_up.append(product)
    projected_error = 0.0
    for product, symbol, desired, notional, _sign in rows:
        quantity = lots.get(symbol, 0)
        result_weights[product] = _weight_for_lots(quantity, notional, equity)
        projected_error += (desired - abs(quantity) * notional) ** 2
    if projected > available + 1e-7 or projected_error > floor_error + 1e-6:
        raise ValueError("projection violated its budget or tracking objective")
    return IntegerProjection(
        weights=result_weights,
        lots=lots,
        original_budget=original,
        available_budget=available,
        projected_notional=projected,
        floor_notional=floor_notional,
        unavailable_notional=unavailable,
        volume_clipping_notional=clipped,
        floor_error=floor_error,
        projected_error=projected_error,
        rounded_up=tuple(rounded_up),
    )


class LotProjectionAccount(HoldingExitAccount):
    """P1 changes raw lot construction; the inherited engine still owns all fills."""

    def __init__(self, market, config=None, *, completed_concentrations=()):
        super().__init__(market, config, completed_concentrations=completed_concentrations)
        self.projection_audit = []

    def target_lot_stages(self, **kwargs):
        projection = project_integer_targets(
            equity=float(kwargs["equity"]),
            product_weights=kwargs["product_weights"],
            product_open_prices=kwargs["product_open_prices"],
            selected_symbols=kwargs["selected_symbols"],
            multipliers=PRODUCT_MULTIPLIERS,
            max_lots=self.config.max_contract_volume,
        )
        projected_kwargs = dict(kwargs, product_weights=projection.weights)
        baseline = super().target_lot_stages(**projected_kwargs)
        if baseline.raw_integer_lots != projection.lots:
            raise ValueError("original lot construction does not reproduce projection")
        self.projection_audit.append(
            {
                "date": self.day,
                "original_continuous_budget": projection.original_budget,
                "available_budget": projection.available_budget,
                "floor_notional": projection.floor_notional,
                "projected_notional": projection.projected_notional,
                "floor_squared_rmb_error": projection.floor_error,
                "projected_squared_rmb_error": projection.projected_error,
                "rounded_up_products": ",".join(projection.rounded_up),
                "margin_fitted_notional": baseline.margin_fitted_notional,
                "final_notional": baseline.final_notional,
                "concentration_freeze": self.concentration_freeze_triggered,
                "reserve_freeze": self.freeze_triggered(kwargs.get("completed_returns", ())),
            }
        )
        return replace(
            baseline,
            desired_notional=projection.original_budget,
            integer_rounding_loss_notional=max(
                0.0, projection.available_budget - projection.projected_notional
            ),
            max_volume_clipping_notional=projection.volume_clipping_notional,
            unavailable_contract_notional=projection.unavailable_notional,
        )
