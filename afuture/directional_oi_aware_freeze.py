"""Research-only OI-aware selective freeze response.

This candidate starts from the already-validated 9-product 60m Price x OI confirmation
surface. The existing completed-return defense trigger remains unchanged. While the
trigger is active, new/same-sign increases remain frozen for every product *except* the
nine products whose upstream target has already survived the causal D -> D+1 OI
confirmation gate. Reductions, exits, reversals and same-product rolls always bypass.

No live runtime imports this module and no hard risk limit changes.
"""
from __future__ import annotations

from typing import Iterable, Mapping

from .directional_60m_oi_confirmation import SUPPORTED_PRODUCTS
from .directional_acceptance import PRODUCT_MULTIPLIERS, TargetLotStages
from .directional_freeze_new_risk import (
    FreezeNewRiskDirectionalProductionAcceptance,
)
from .directional_robustness import MarginAwareDirectionalProductionAcceptance


def oi_aware_freeze_target(
    *,
    current_lots: Mapping[str, int],
    target_lots: Mapping[str, int],
    symbol_products: Mapping[str, str],
    trusted_products: Iterable[str],
    triggered: bool,
) -> dict[str, int]:
    """Freeze untrusted risk increases while allowing already OI-confirmed targets."""
    target = {
        str(symbol): int(volume)
        for symbol, volume in target_lots.items()
        if int(volume) != 0
    }
    if not triggered:
        return dict(target)

    current = {
        str(symbol): int(volume)
        for symbol, volume in current_lots.items()
        if int(volume) != 0
    }
    products = {
        str(symbol): str(product).upper()
        for symbol, product in symbol_products.items()
    }
    trusted = {str(product).upper() for product in trusted_products}

    by_product: dict[str, list[str]] = {}
    for symbol in current:
        product = products.get(symbol)
        if not product:
            raise ValueError(f"missing current symbol product: {symbol}")
        by_product.setdefault(product, []).append(symbol)

    result: dict[str, int] = {}
    for symbol, wanted in sorted(target.items()):
        product = products.get(symbol)
        if not product:
            raise ValueError(f"missing target symbol product: {symbol}")

        # The upstream 60m OI overlay has already causally accepted this target. Preserve
        # it exactly instead of applying a second, redundant freeze to the same evidence.
        if product in trusted:
            result[symbol] = wanted
            continue

        have = int(current.get(symbol, 0))
        if have != 0:
            # Untrusted reversals and same-sign reductions remain exact targets.
            if (have > 0) != (wanted > 0) or abs(wanted) <= abs(have):
                result[symbol] = wanted
            else:
                # Untrusted same-contract, same-sign increases stay frozen.
                result[symbol] = have
            continue

        # A different contract of an already-held product is a roll rather than fresh
        # product-level risk and remains executable even for an untrusted product.
        if by_product.get(product):
            result[symbol] = wanted
            continue

        # Otherwise this is an untrusted new product risk entry and is suppressed.

    return {symbol: volume for symbol, volume in result.items() if volume != 0}


class OIAwareFreezeDirectionalProductionAcceptance(
    FreezeNewRiskDirectionalProductionAcceptance
):
    """Freeze-only Production proxy that exempts upstream OI-confirmed products."""

    trusted_products = tuple(SUPPORTED_PRODUCTS)

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
        # Bypass FreezeNewRiskDirectionalProductionAcceptance.target_lot_stages here so
        # trusted OI-confirmed increases are not already removed before selective logic.
        baseline = MarginAwareDirectionalProductionAcceptance.target_lot_stages(
            self,
            equity=equity,
            product_weights=product_weights,
            product_open_prices=product_open_prices,
            selected_symbols=selected_symbols,
            current_lots=current_lots,
            completed_returns=completed_returns,
        )
        if not self.freeze_triggered(completed_returns):
            return baseline

        current = {
            str(symbol): int(volume)
            for symbol, volume in (current_lots or {}).items()
            if int(volume) != 0
        }
        symbols = set(current) | set(baseline.final_lots)
        symbol_products = {symbol: self._product(symbol) for symbol in symbols}
        frozen = oi_aware_freeze_target(
            current_lots=current,
            target_lots=baseline.final_lots,
            symbol_products=symbol_products,
            trusted_products=self.trusted_products,
            triggered=True,
        )

        product_for_symbol = {
            str(symbol): str(product).upper()
            for product, symbol in selected_symbols.items()
        }
        final_notional = 0.0
        for symbol, volume in frozen.items():
            product = product_for_symbol.get(str(symbol), self._product(symbol))
            price = float(product_open_prices.get(product, 0.0))
            multiplier = PRODUCT_MULTIPLIERS.get(product)
            if price <= 0.0 or multiplier is None:
                continue
            final_notional += abs(int(volume)) * price * float(multiplier)

        return TargetLotStages(
            raw_integer_lots=dict(baseline.raw_integer_lots),
            margin_fitted_lots=dict(baseline.margin_fitted_lots),
            final_lots=dict(frozen),
            desired_notional=float(baseline.desired_notional),
            raw_integer_notional=float(baseline.raw_integer_notional),
            margin_fitted_notional=float(baseline.margin_fitted_notional),
            final_notional=float(final_notional),
            integer_rounding_loss_notional=float(
                baseline.integer_rounding_loss_notional
            ),
            max_volume_clipping_notional=float(
                baseline.max_volume_clipping_notional
            ),
            unavailable_contract_notional=float(
                baseline.unavailable_contract_notional
            ),
            soft_margin_share=baseline.soft_margin_share,
        )
