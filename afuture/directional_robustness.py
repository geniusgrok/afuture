"""Robust production-mechanics adapters for the execution-aligned directional path."""
from __future__ import annotations

from typing import Mapping

from .directional import adaptive_margin_sizing_share, fit_target_lots_to_margin_budget
from .directional_acceptance import DirectionalProductionAcceptance, PRODUCT_MULTIPLIERS
from .directional_efficiency import stabilize_one_lot_increases


class MarginAwareDirectionalProductionAcceptance(DirectionalProductionAcceptance):
    """Production proxy whose integer target is feasible before opening hard gates."""

    def target_lots(
        self,
        *,
        equity: float,
        product_weights: Mapping[str, float],
        product_open_prices: Mapping[str, float],
        selected_symbols: Mapping[str, str],
        current_lots: Mapping[str, int] | None = None,
        completed_returns: tuple[float, ...] = (),
    ) -> dict[str, int]:
        requested = super().target_lots(
            equity=equity,
            product_weights=product_weights,
            product_open_prices=product_open_prices,
            selected_symbols=selected_symbols,
            current_lots=current_lots,
            completed_returns=completed_returns,
        )
        if not requested or equity <= 0:
            return {}
        symbol_product = {str(symbol): str(product).upper() for product, symbol in selected_symbols.items()}
        per_lot_margin: dict[str, float] = {}
        lot_notionals: dict[str, float] = {}
        for symbol in requested:
            product = symbol_product.get(str(symbol))
            if product is None:
                raise ValueError(f"missing target product for margin estimate: {symbol}")
            price = float(product_open_prices.get(product, 0.0))
            multiplier = PRODUCT_MULTIPLIERS.get(product)
            if price <= 0 or multiplier is None:
                raise ValueError(f"missing positive target margin evidence: {symbol}")
            lot_notionals[str(symbol)] = price * float(multiplier)
            per_lot_margin[str(symbol)] = lot_notionals[str(symbol)] * float(self.config.margin_rate_proxy) * float(self.config.margin_estimate_buffer)
        sizing_share = adaptive_margin_sizing_share(
            max_margin_ratio=self.config.max_margin_ratio,
            min_available_ratio=self.config.min_available_ratio,
            max_daily_loss_ratio=self.config.max_daily_loss_ratio,
            completed_returns=completed_returns,
        )
        fitted = fit_target_lots_to_margin_budget(requested, per_lot_margin, margin_budget=float(equity) * sizing_share)
        current = {str(symbol): int(volume) for symbol, volume in (current_lots or {}).items() if int(volume)}
        if not current or not set(current).issubset(lot_notionals):
            return fitted
        return stabilize_one_lot_increases(
            current_lots=current,
            target_lots=fitted,
            lot_notionals=lot_notionals,
            per_lot_margin=per_lot_margin,
            equity=float(equity),
            soft_margin_share=sizing_share,
            max_gross_ratio=self.config.max_realized_gross_ratio,
        )
