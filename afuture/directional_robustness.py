"""Robust production-mechanics adapters for the execution-aligned directional path."""
from __future__ import annotations

from typing import Mapping

from .directional import fit_target_lots_to_margin_budget
from .directional_acceptance import (
    DirectionalProductionAcceptance,
    PRODUCT_MULTIPLIERS,
)


class MarginAwareDirectionalProductionAcceptance(DirectionalProductionAcceptance):
    """Production proxy whose integer target is feasible before opening hard gates.

    The base simulator still owns all account state, costs, daily circuit, hard HALT and
    realized-gross semantics. This subclass changes only target construction: a signal
    may request up to the frozen 2x gross cap, while the executable integer target is
    proportionally reduced when the explicit margin proxy cannot fit the unchanged
    account margin/cash envelope.
    """

    def target_lots(
        self,
        *,
        equity: float,
        product_weights: Mapping[str, float],
        product_open_prices: Mapping[str, float],
        selected_symbols: Mapping[str, str],
    ) -> dict[str, int]:
        requested = super().target_lots(
            equity=equity,
            product_weights=product_weights,
            product_open_prices=product_open_prices,
            selected_symbols=selected_symbols,
        )
        if not requested or equity <= 0:
            return {}

        symbol_product = {
            str(symbol): str(product).upper()
            for product, symbol in selected_symbols.items()
        }
        per_lot_margin: dict[str, float] = {}
        for symbol in requested:
            product = symbol_product.get(str(symbol))
            if product is None:
                raise ValueError(f"missing target product for margin estimate: {symbol}")
            price = float(product_open_prices.get(product, 0.0))
            multiplier = PRODUCT_MULTIPLIERS.get(product)
            if price <= 0 or multiplier is None:
                raise ValueError(f"missing positive target margin evidence: {symbol}")
            per_lot_margin[str(symbol)] = (
                price
                * float(multiplier)
                * float(self.config.margin_rate_proxy)
                * float(self.config.margin_estimate_buffer)
            )

        hard_margin_share = min(
            float(self.config.max_margin_ratio),
            1.0 - float(self.config.min_available_ratio),
        )
        return fit_target_lots_to_margin_budget(
            requested,
            per_lot_margin,
            margin_budget=float(equity) * max(0.0, hard_margin_share),
        )
