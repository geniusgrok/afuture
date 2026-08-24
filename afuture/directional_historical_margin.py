"""Research-only historical exchange-margin adapter for Production mechanics.

This adapter never changes hard account limits. For simulation day D it may use only the
latest exchange-published conservative speculative margin ratio whose record date is
strictly earlier than D. Missing history falls back to the scenario's existing margin
proxy. The existing 1.25 estimate buffer, 35% hard margin, 25% available floor, 30%-style
soft sizing envelope, 2x gross and 35-lot limits remain authoritative.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from typing import Mapping

import pandas as pd

from .directional import adaptive_margin_sizing_share, fit_target_lots_to_margin_budget
from .directional_acceptance import (
    PRODUCT_MULTIPLIERS,
    ProductionMechanicsConfig,
    TargetLotStages,
)
from .directional_efficiency import stabilize_one_lot_increases
from .directional_robustness import MarginAwareDirectionalProductionAcceptance


class HistoricalMarginAwareDirectionalProductionAcceptance(
    MarginAwareDirectionalProductionAcceptance
):
    """Production-mechanics research adapter with causally lagged exchange margin."""

    def __init__(
        self,
        config: ProductionMechanicsConfig,
        *,
        margin_history: pd.DataFrame,
    ) -> None:
        super().__init__(config)
        self._active_day: pd.Timestamp | None = None
        frame = margin_history.copy()
        required = {"date", "symbol", "conservative_margin_ratio"}
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"historical margin missing columns: {sorted(missing)}")
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
        frame["symbol"] = frame["symbol"].astype(str).str.upper()
        frame["conservative_margin_ratio"] = pd.to_numeric(
            frame["conservative_margin_ratio"], errors="coerce"
        )
        frame = frame.dropna(subset=["date", "symbol", "conservative_margin_ratio"])
        frame = frame[
            (frame["conservative_margin_ratio"] > 0.0)
            & (frame["conservative_margin_ratio"] <= 1.0)
        ].copy()
        frame.sort_values(["symbol", "date"], inplace=True)
        frame.drop_duplicates(["symbol", "date"], keep="last", inplace=True)

        dates: dict[str, list[pd.Timestamp]] = defaultdict(list)
        ratios: dict[str, list[float]] = defaultdict(list)
        for row in frame.itertuples(index=False):
            symbol = str(row.symbol).upper()
            dates[symbol].append(pd.Timestamp(row.date).normalize())
            ratios[symbol].append(float(row.conservative_margin_ratio))
        self._margin_dates = dict(dates)
        self._margin_ratios = dict(ratios)

    def _on_simulation_day(self, day: pd.Timestamp) -> None:
        self._active_day = pd.Timestamp(day).normalize()

    def margin_rate(self, symbol: str) -> float:
        """Return latest strictly-prior exchange ratio or the scenario fallback proxy."""
        fallback = float(self.config.margin_rate_proxy)
        if self._active_day is None:
            return fallback
        key = str(symbol).upper()
        dates = self._margin_dates.get(key)
        ratios = self._margin_ratios.get(key)
        if not dates or not ratios:
            return fallback
        position = bisect_left(dates, self._active_day) - 1
        if position < 0:
            return fallback
        value = float(ratios[position])
        return value if 0.0 < value <= 1.0 else fallback

    def per_lot_margin(self, symbol: str, price: float) -> float:
        value = float(price)
        if value <= 0.0:
            raise ValueError(f"missing positive target margin price: {symbol}")
        product = self._product(str(symbol))
        multiplier = PRODUCT_MULTIPLIERS[product]
        return (
            value
            * float(multiplier)
            * self.margin_rate(str(symbol))
            * float(self.config.margin_estimate_buffer)
        )

    def _margin(
        self,
        lots: Mapping[str, int],
        prices: Mapping[str, float],
    ) -> float:
        return float(
            sum(
                abs(int(volume)) * self.per_lot_margin(symbol, prices.get(symbol, 0.0))
                for symbol, volume in lots.items()
            )
        )

    def check_opening_batch(
        self,
        *,
        equity: float,
        current_margin: float,
        current_lots: Mapping[str, int],
        openings: Mapping[str, int],
        open_prices: Mapping[str, float],
    ) -> tuple[bool, str, float]:
        if equity <= 0:
            return False, "equity is not positive", 0.0
        estimated = 0.0
        for symbol, delta in openings.items():
            requested = abs(int(delta))
            if requested <= 0:
                continue
            existing = abs(int(current_lots.get(symbol, 0)))
            if existing + requested > self.config.max_contract_volume:
                return False, "contract volume limit reached", estimated
            price = float(open_prices.get(symbol, 0.0))
            if price <= 0:
                return False, f"missing opening price: {symbol}", estimated
            estimated += requested * self.per_lot_margin(symbol, price)
        post_margin = float(current_margin) + estimated
        if post_margin / equity > self.config.max_margin_ratio:
            return False, "combined margin ratio would exceed limit", float(estimated)
        if (equity - post_margin) / equity < self.config.min_available_ratio:
            return False, "combined cash reserve would fall below limit", float(estimated)
        return True, "", float(estimated)

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
        # Obtain the unchanged raw integer target from the non-margin-aware parent.
        raw = super(MarginAwareDirectionalProductionAcceptance, self).target_lot_stages(
            equity=equity,
            product_weights=product_weights,
            product_open_prices=product_open_prices,
            selected_symbols=selected_symbols,
            current_lots=current_lots,
            completed_returns=completed_returns,
        )
        sizing_share = adaptive_margin_sizing_share(
            max_margin_ratio=self.config.max_margin_ratio,
            min_available_ratio=self.config.min_available_ratio,
            max_daily_loss_ratio=self.config.max_daily_loss_ratio,
            completed_returns=completed_returns,
        )
        requested = raw.raw_integer_lots
        if not requested or equity <= 0:
            return TargetLotStages(
                raw_integer_lots=dict(requested),
                margin_fitted_lots={},
                final_lots={},
                desired_notional=raw.desired_notional,
                raw_integer_notional=raw.raw_integer_notional,
                margin_fitted_notional=0.0,
                final_notional=0.0,
                integer_rounding_loss_notional=raw.integer_rounding_loss_notional,
                max_volume_clipping_notional=raw.max_volume_clipping_notional,
                unavailable_contract_notional=raw.unavailable_contract_notional,
                soft_margin_share=sizing_share,
            )

        symbol_product = {
            str(symbol): str(product).upper()
            for product, symbol in selected_symbols.items()
        }
        per_lot_margin: dict[str, float] = {}
        lot_notionals: dict[str, float] = {}
        for symbol in requested:
            product = symbol_product.get(str(symbol))
            if product is None:
                raise ValueError(f"missing target product for margin estimate: {symbol}")
            price = float(product_open_prices.get(product, 0.0))
            multiplier = PRODUCT_MULTIPLIERS.get(product)
            if price <= 0.0 or multiplier is None:
                raise ValueError(f"missing positive target margin evidence: {symbol}")
            lot_notionals[str(symbol)] = price * float(multiplier)
            per_lot_margin[str(symbol)] = self.per_lot_margin(str(symbol), price)

        fitted = fit_target_lots_to_margin_budget(
            requested,
            per_lot_margin,
            margin_budget=float(equity) * sizing_share,
        )
        current = {
            str(symbol): int(volume)
            for symbol, volume in (current_lots or {}).items()
            if int(volume)
        }
        final = dict(fitted)
        if current and set(current).issubset(lot_notionals):
            final = stabilize_one_lot_increases(
                current_lots=current,
                target_lots=fitted,
                lot_notionals=lot_notionals,
                per_lot_margin=per_lot_margin,
                equity=float(equity),
                soft_margin_share=sizing_share,
                max_gross_ratio=self.config.max_realized_gross_ratio,
            )

        def gross(lots: Mapping[str, int]) -> float:
            return float(
                sum(
                    abs(int(volume)) * lot_notionals[str(symbol)]
                    for symbol, volume in lots.items()
                )
            )

        return TargetLotStages(
            raw_integer_lots=dict(requested),
            margin_fitted_lots=dict(fitted),
            final_lots=dict(final),
            desired_notional=raw.desired_notional,
            raw_integer_notional=raw.raw_integer_notional,
            margin_fitted_notional=gross(fitted),
            final_notional=gross(final),
            integer_rounding_loss_notional=raw.integer_rounding_loss_notional,
            max_volume_clipping_notional=raw.max_volume_clipping_notional,
            unavailable_contract_notional=raw.unavailable_contract_notional,
            soft_margin_share=sizing_share,
        )
