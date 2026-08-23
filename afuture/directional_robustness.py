"""Robust production-mechanics adapters for the execution-aligned directional path."""
from __future__ import annotations

from typing import Mapping

from .directional import fit_target_lots_to_margin_budget, margin_sizing_share
from .directional_acceptance import (
    DirectionalProductionAcceptance,
    PRODUCT_MULTIPLIERS,
)


class MarginAwareDirectionalProductionAcceptance(DirectionalProductionAcceptance):
    """Production proxy whose integer target is feasible before opening hard gates."""

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

        sizing_share = margin_sizing_share(
            max_margin_ratio=self.config.max_margin_ratio,
            min_available_ratio=self.config.min_available_ratio,
            max_daily_loss_ratio=self.config.max_daily_loss_ratio,
        )
        return fit_target_lots_to_margin_budget(
            requested,
            per_lot_margin,
            margin_budget=float(equity) * sizing_share,
        )
