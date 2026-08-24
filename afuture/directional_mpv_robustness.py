"""Research-only Production simulator using causal MPV integer allocation.

This module is deliberately not imported by live runtime wiring. It subclasses the
validated margin-aware acceptance simulator so Broker/RiskManager-equivalent mechanics,
reduction-first sequencing, and hard gates remain unchanged during research.
"""
from __future__ import annotations

from typing import Mapping

import pandas as pd

from .directional_acceptance import PRODUCT_MULTIPLIERS, TargetLotStages
from .directional_mpv_research import MPVResearchOptimization, optimize_with_causal_mpv
from .directional_robustness import MarginAwareDirectionalProductionAcceptance


class MPVDirectionalProductionAcceptance(MarginAwareDirectionalProductionAcceptance):
    """Research adapter that reallocates only within the validated target envelope."""

    def __init__(self, config=None, *, historical_seed_events: pd.DataFrame | None = None) -> None:
        super().__init__(config)
        self._mpv_historical_seed_events = (
            pd.DataFrame() if historical_seed_events is None else historical_seed_events.copy()
        )
        self._mpv_observed_event_rows: list[dict] = []
        self._mpv_cost_rate = 0.0
        self.last_mpv_optimization: MPVResearchOptimization | None = None

    def _seed_rows_before(self, decision_start) -> list[dict]:
        if self._mpv_historical_seed_events.empty:
            return []
        frame = self._mpv_historical_seed_events.copy()
        if "date" not in frame.columns:
            return []
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
        cutoff = pd.Timestamp(decision_start).normalize()
        frame = frame[frame["date"].notna() & (frame["date"] < cutoff)]
        return [dict(row) for row in frame.to_dict(orient="records")]

    def simulate(self, raw, weights, *, cost_bps: float, prepared=None):
        index = pd.DatetimeIndex(
            pd.to_datetime(getattr(weights, "index", []), errors="coerce")
        )
        valid = index[~index.isna()]
        self._mpv_observed_event_rows = (
            self._seed_rows_before(valid.min()) if len(valid) else []
        )
        self._mpv_cost_rate = float(cost_bps) / 10000.0
        self.last_mpv_optimization = None
        return super().simulate(raw, weights, cost_bps=cost_bps, prepared=prepared)

    def _pnl_audit_row(self, **kwargs) -> dict:
        row = super()._pnl_audit_row(**kwargs)
        self._mpv_observed_event_rows.append(dict(row))
        return row

    def _trade_audit_rows(self, **kwargs) -> list[dict]:
        rows = super()._trade_audit_rows(**kwargs)
        self._mpv_observed_event_rows.extend(dict(row) for row in rows)
        return rows

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
        requested = dict(baseline.raw_integer_lots)
        reference = dict(baseline.final_lots)
        if not requested or baseline.soft_margin_share is None or equity <= 0.0:
            return baseline

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
            per_lot_margin[symbol] = (
                notional
                * float(self.config.margin_rate_proxy)
                * float(self.config.margin_estimate_buffer)
            )

        current = {
            str(symbol): int(volume)
            for symbol, volume in (current_lots or {}).items()
            if int(volume)
        }
        # On a roll day the incumbent concrete contract is outside the selected-symbol
        # evidence map. Do not synthesize margin/notional evidence for it; exact fallback.
        if not set(current).issubset(lot_notionals):
            return baseline

        observed = pd.DataFrame(self._mpv_observed_event_rows)
        result = optimize_with_causal_mpv(
            observed_events=observed,
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
            cost_rate=float(self._mpv_cost_rate),
        )
        self.last_mpv_optimization = result
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
