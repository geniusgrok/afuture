"""Offline acceptance adapter for the shared causal Stress-90 HHI primitives."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from math import isfinite

import pandas as pd

from .directional_acceptance import PRODUCT_MULTIPLIERS, TargetLotStages
from .directional_drawdown_reserve_freeze import (
    FullPathDrawdownReserveFreezeDirectionalProductionAcceptance,
)
from .directional_freeze_new_risk import freeze_new_risk_target
from .directional_stress90_policy import advance_concentration_history
from .directional_stress90_policy import (
    target_weight_concentration as target_weight_concentration,
)


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
        self._completed_concentrations = [float(value) for value in completed_concentrations]
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
        (
            _current,
            _prior_median,
            self.concentration_freeze_triggered,
            updated,
        ) = advance_concentration_history(
            self._completed_concentrations,
            product_weights,
        )
        self._completed_concentrations = list(updated)

    def _checkpoint_strategy_state(self) -> dict[str, object]:
        return {
            "completed_concentrations": tuple(self._completed_concentrations),
            "concentration_freeze_triggered": self.concentration_freeze_triggered,
        }

    def _restore_checkpoint_strategy_state(self, state: Mapping[str, object]) -> None:
        if set(state) != {"completed_concentrations", "concentration_freeze_triggered"}:
            raise ValueError("checkpoint concentration state is invalid")
        raw_history = state["completed_concentrations"]
        if not isinstance(raw_history, (list, tuple)):
            raise ValueError("checkpoint concentration history is invalid")
        history = [float(value) for value in raw_history]
        if any(not isfinite(value) or not 0.0 < value <= 1.0 for value in history):
            raise ValueError("checkpoint concentration history is invalid")
        triggered = state["concentration_freeze_triggered"]
        if not isinstance(triggered, bool):
            raise ValueError("checkpoint concentration freeze state is invalid")
        self._completed_concentrations = history
        self.concentration_freeze_triggered = triggered

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
            str(symbol): str(product).upper() for product, symbol in selected_symbols.items()
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
