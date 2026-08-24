"""Research-only Production adapter driven by fixed baseline signal outcomes.

The candidate account never contributes evidence to its own value estimates. Every
decision reads only baseline shadow outcomes strictly before the active decision day. The
adapter reallocates solely inside the already-requested directional intent and the
validated margin-aware target envelope; downstream hard account gates remain unchanged.
An optional precomputed causal remaining-horizon panel scales lifecycle value; without it
the adapter is exactly the original one-day Shadow objective.
"""
from __future__ import annotations

from math import isfinite
from typing import Mapping

import pandas as pd

from .directional_acceptance import PRODUCT_MULTIPLIERS, TargetLotStages
from .directional_robustness import MarginAwareDirectionalProductionAcceptance
from .directional_shadow_outcomes import (
    ShadowOutcomeOptimization,
    optimize_with_shadow_outcomes,
)

SHADOW_OBJECTIVE_COST_BPS = 15.0


class ShadowMPVDirectionalProductionAcceptance(
    MarginAwareDirectionalProductionAcceptance
):
    """Reallocate constrained lots from exogenous baseline signal outcomes."""

    def __init__(
        self,
        config=None,
        *,
        shadow_outcomes: pd.DataFrame,
        remaining_horizon_panel: pd.DataFrame | None = None,
    ) -> None:
        super().__init__(config)
        self.shadow_outcomes = shadow_outcomes.copy()
        self.remaining_horizon_panel = self._normalize_horizon_panel(
            remaining_horizon_panel
        )
        self.active_decision_day: pd.Timestamp | None = None
        self.last_shadow_optimization: ShadowOutcomeOptimization | None = None

    @staticmethod
    def _normalize_horizon_panel(
        panel: pd.DataFrame | None,
    ) -> pd.DataFrame | None:
        if panel is None:
            return None
        frame = panel.copy().astype(float)
        frame.index = pd.to_datetime(frame.index, errors="coerce").normalize()
        frame = frame.loc[~frame.index.isna()].sort_index()
        frame.columns = [str(column).upper() for column in frame.columns]
        return frame

    @property
    def shadow_objective_cost_rate(self) -> float:
        """Stress-aware decision hurdle, fixed independently of evaluation scenario."""
        return SHADOW_OBJECTIVE_COST_BPS / 10000.0

    def _on_simulation_day(self, day: pd.Timestamp) -> None:
        self.active_decision_day = pd.Timestamp(day).normalize()

    def _expected_horizons(self, products: set[str]) -> dict[str, float] | None:
        if self.remaining_horizon_panel is None or self.active_decision_day is None:
            return None
        if self.active_decision_day not in self.remaining_horizon_panel.index:
            return {product: 1.0 for product in products}
        row = self.remaining_horizon_panel.loc[self.active_decision_day]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[-1]
        result: dict[str, float] = {}
        for product in products:
            value = float(row.get(product, 1.0)) if product in row.index else 1.0
            result[product] = value if isfinite(value) and value >= 1.0 else 1.0
        return result

    def simulate(self, raw, weights, *, cost_bps: float, prepared=None):
        self.active_decision_day = None
        self.last_shadow_optimization = None
        return super().simulate(raw, weights, cost_bps=cost_bps, prepared=prepared)

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
        if (
            self.active_decision_day is None
            or equity <= 0.0
            or baseline.soft_margin_share is None
            or not baseline.raw_integer_lots
        ):
            return baseline

        requested = dict(baseline.raw_integer_lots)
        reference = dict(baseline.final_lots)
        symbol_products = {
            str(symbol): str(product).upper()
            for product, symbol in selected_symbols.items()
            if str(symbol) in requested
        }
        if set(requested) - set(symbol_products):
            return baseline

        lot_notionals: dict[str, float] = {}
        per_lot_margin: dict[str, float] = {}
        for symbol, product in symbol_products.items():
            price = float(product_open_prices.get(product, 0.0))
            multiplier = PRODUCT_MULTIPLIERS.get(product)
            if price <= 0.0 or multiplier is None:
                return baseline
            notional = price * float(multiplier)
            lot_notionals[symbol] = notional
            per_lot_margin[symbol] = self.per_lot_margin(symbol, price)

        current = {
            str(symbol): int(volume)
            for symbol, volume in (current_lots or {}).items()
            if int(volume)
        }
        # A held contract outside today's selected-symbol map is a roll boundary. Do not
        # synthesize its notional/margin/value evidence; preserve validated behavior.
        if not set(current).issubset(lot_notionals):
            return baseline

        result = optimize_with_shadow_outcomes(
            shadow_outcomes=self.shadow_outcomes,
            decision_date=self.active_decision_day,
            reference_lots=reference,
            requested_lots=requested,
            current_lots=current,
            symbol_products=symbol_products,
            lot_notionals=lot_notionals,
            per_lot_margin=per_lot_margin,
            equity=float(equity),
            soft_margin_budget=float(equity) * float(baseline.soft_margin_share),
            max_gross_ratio=float(self.config.max_realized_gross_ratio),
            max_abs_lots=min(int(self.config.max_contract_volume), 35),
            cost_rate=self.shadow_objective_cost_rate,
            expected_horizons=self._expected_horizons(set(symbol_products.values())),
        )
        self.last_shadow_optimization = result
        if result.optimization.fallback_reason:
            return baseline

        final = dict(result.optimization.target_lots)

        def gross(lots: Mapping[str, int]) -> float:
            return float(
                sum(
                    abs(int(volume)) * lot_notionals[str(symbol)]
                    for symbol, volume in lots.items()
                )
            )

        return TargetLotStages(
            raw_integer_lots=dict(baseline.raw_integer_lots),
            margin_fitted_lots=dict(baseline.margin_fitted_lots),
            final_lots=final,
            desired_notional=baseline.desired_notional,
            raw_integer_notional=baseline.raw_integer_notional,
            margin_fitted_notional=baseline.margin_fitted_notional,
            final_notional=gross(final),
            integer_rounding_loss_notional=baseline.integer_rounding_loss_notional,
            max_volume_clipping_notional=baseline.max_volume_clipping_notional,
            unavailable_contract_notional=baseline.unavailable_contract_notional,
            soft_margin_share=baseline.soft_margin_share,
        )
