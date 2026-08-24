"""Offline Production-evaluator adapter for causal leadership-state freezing."""
from __future__ import annotations

from dataclasses import replace
from math import isfinite
from statistics import median
from typing import Iterable, Mapping

import pandas as pd

from .directional_acceptance import PRODUCT_MULTIPLIERS, TargetLotStages
from .directional_drawdown_reserve_freeze import (
    FullPathDrawdownReserveFreezeDirectionalProductionAcceptance,
)
from .directional_freeze_new_risk import freeze_new_risk_target


def target_weight_concentration(weights: Mapping[str, float]) -> float | None:
    """Return standard HHI of absolute target weights, or None for an inactive target."""
    magnitudes = [abs(float(value)) for value in weights.values() if float(value) != 0.0]
    if any(not isfinite(value) for value in magnitudes):
        raise ValueError("target weights must be finite")
    gross = sum(magnitudes)
    if gross <= 0.0:
        return None
    return float(sum((value / gross) ** 2 for value in magnitudes))


class ExpandingMedianConcentrationFreezeDirectionalProductionAcceptance(
    FullPathDrawdownReserveFreezeDirectionalProductionAcceptance
):
    """Freeze new risk when current target HHI is not above its prior median."""

    def __init__(
        self,
        config=None,
        *,
        completed_concentrations: Iterable[float] = (),
    ) -> None:
        super().__init__(config)
        self._completed_concentrations = [
            float(value) for value in completed_concentrations
        ]
        if any(
            not isfinite(value) or not 0.0 < value <= 1.0
            for value in self._completed_concentrations
        ):
            raise ValueError("completed target concentrations must be in (0, 1]")
        self.concentration_freeze_triggered = False

    def observe_target_state(
        self,
        *,
        day: pd.Timestamp | None,
        product_weights: Mapping[str, float],
    ) -> None:
        del day
        current = target_weight_concentration(product_weights)
        if current is None:
            self.concentration_freeze_triggered = False
            return
        self.concentration_freeze_triggered = bool(
            self._completed_concentrations
            and current <= median(self._completed_concentrations)
        )
        self._completed_concentrations.append(current)

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
        baseline = super().target_lot_stages(
            equity=equity,
            product_weights=product_weights,
            product_open_prices=product_open_prices,
            selected_symbols=selected_symbols,
            current_lots=current_lots,
            completed_returns=completed_returns,
        )
        if not self.concentration_freeze_triggered:
            return baseline

        current = {
            str(symbol): int(volume)
            for symbol, volume in (current_lots or {}).items()
            if int(volume) != 0
        }
        symbols = set(current) | set(baseline.final_lots)
        symbol_products = {symbol: self._product(symbol) for symbol in symbols}
        frozen = freeze_new_risk_target(
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
            if price > 0.0 and multiplier is not None:
                final_notional += abs(int(volume)) * price * float(multiplier)

        return replace(
            baseline,
            final_lots=dict(frozen),
            final_notional=float(final_notional),
        )
