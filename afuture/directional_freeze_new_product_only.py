"""Research-only freeze-new-product soft risk response.

The existing completed-return DirectionalRiskGovernor trigger is unchanged. When defense
is active, this candidate blocks only entry into a product that is not currently held.
Existing product exposure may still increase on the same contract, while reductions,
exits, reversals and same-product contract rolls remain executable. The global 0.25x soft
scaling stays disabled exactly as in the validated freeze-new-risk adapter.

Hard account gates, margin limits, available-cash floor, 2x gross, 35-lot cap and
reduction-first execution remain owned by the existing Production mechanics / RiskManager
path. This module is research-only and is not imported by live runtime wiring.
"""
from __future__ import annotations

from typing import Mapping

from .directional_acceptance import PRODUCT_MULTIPLIERS, TargetLotStages
from .directional_freeze_new_risk import FreezeNewRiskDirectionalProductionAcceptance
from .directional_robustness import MarginAwareDirectionalProductionAcceptance


def freeze_new_product_target(
    *,
    current_lots: Mapping[str, int],
    target_lots: Mapping[str, int],
    symbol_products: Mapping[str, str],
    triggered: bool,
) -> dict[str, int]:
    """Block only brand-new product entries while defense is triggered."""
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
    held_products: set[str] = set()
    for symbol in current:
        product = products.get(symbol)
        if not product:
            raise ValueError(f"missing current symbol product: {symbol}")
        held_products.add(product)

    result: dict[str, int] = {}
    for symbol, wanted in sorted(target.items()):
        product = products.get(symbol)
        if not product:
            raise ValueError(f"missing target symbol product: {symbol}")

        # Any target in an already-held product is allowed. This includes same-contract
        # same-sign increases, reductions, reversals and a target on a successor contract
        # during a same-product roll. An absent target still represents an unrestricted
        # exit because this helper never resurrects current-only symbols.
        if product in held_products:
            result[symbol] = wanted
            continue

        # Product not held at all: suppress the brand-new risk entry.

    return {symbol: volume for symbol, volume in result.items() if volume != 0}


class FreezeNewProductOnlyDirectionalProductionAcceptance(
    FreezeNewRiskDirectionalProductionAcceptance
):
    """Margin-aware Production mechanics with new-product-only soft defense."""

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
        # Deliberately bypass FreezeNewRiskDirectionalProductionAcceptance's stricter
        # same-sign-increase freeze. The inherited __init__ already replaced the global
        # 0.25x governor scale with a unit-scale governor while preserving the exact
        # trigger separately in self._freeze_trigger_governor.
        baseline = MarginAwareDirectionalProductionAcceptance.target_lot_stages(
            self,
            equity=equity,
            product_weights=product_weights,
            product_open_prices=product_open_prices,
            selected_symbols=selected_symbols,
            current_lots=current_lots,
            completed_returns=completed_returns,
        )
        triggered = self.freeze_triggered(completed_returns)
        if not triggered:
            return baseline

        current = {
            str(symbol): int(volume)
            for symbol, volume in (current_lots or {}).items()
            if int(volume) != 0
        }
        symbols = set(current) | set(baseline.final_lots)
        symbol_products = {symbol: self._product(symbol) for symbol in symbols}
        frozen = freeze_new_product_target(
            current_lots=current,
            target_lots=baseline.final_lots,
            symbol_products=symbol_products,
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
                # Current-only incumbents are not created by this stage. Fail closed for
                # accounting rather than inventing an execution price.
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
