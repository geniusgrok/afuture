"""Robust production-mechanics adapters for the execution-aligned directional path."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass

import pandas as pd

from .directional import adaptive_margin_sizing_share, fit_target_lots_to_margin_budget
from .directional_acceptance import (
    PRODUCT_MULTIPLIERS,
    DirectionalProductionAcceptance,
    ProductionMechanicsConfig,
    TargetLotStages,
)
from .directional_data_validation import validate_daily_index
from .directional_efficiency import stabilize_one_lot_increases
from .directional_risk import covariance_risk_budget


class MarginAwareDirectionalProductionAcceptance(DirectionalProductionAcceptance):
    """Production proxy whose integer target is feasible before opening hard gates."""

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
        raw = super().target_lot_stages(
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
            str(symbol): str(product).upper() for product, symbol in selected_symbols.items()
        }
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
            per_lot_margin[str(symbol)] = (
                lot_notionals[str(symbol)]
                * float(self.config.margin_rate_proxy)
                * float(self.config.margin_estimate_buffer)
            )
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
                    abs(int(volume)) * lot_notionals[str(symbol)] for symbol, volume in lots.items()
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
        return self.target_lot_stages(
            equity=equity,
            product_weights=product_weights,
            product_open_prices=product_open_prices,
            selected_symbols=selected_symbols,
            current_lots=current_lots,
            completed_returns=completed_returns,
        ).final_lots


@dataclass(frozen=True)
class _ObservedCovarianceScale:
    value: float

    def scale(self, completed_returns: Iterable[float]) -> float:
        # Forecasts come from completed market observations, not scaled account PnL.
        del completed_returns
        return self.value


class CovarianceBudgetDirectionalProductionAcceptance(MarginAwareDirectionalProductionAcceptance):
    """Replace HHI/reserve/exit soft rules with a prior-market covariance budget.

    The 63-session, 15%-annualized forecast enters the original simulator's target
    scaling stage. Integer construction, margin feasibility, fills, costs and hard
    account gates remain owned by the existing account implementation. Missing risk
    evidence yields a zero target, so incumbents can be reduced by that same path.
    This optional acceptance class does not change live policy selection.
    """

    def __init__(
        self,
        config: ProductionMechanicsConfig | None = None,
        *,
        market_returns: pd.DataFrame,
    ) -> None:
        super().__init__(config)
        frame = market_returns.copy()
        if len(frame.index):
            # A sentinel column also validates a dated frame with no product columns.
            validate_daily_index(pd.DataFrame({"row": 0}, index=frame.index), name="market returns")
        frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index, errors="raise")).normalize()
        if frame.index.tz is not None:
            raise ValueError("market returns require timezone-naive trading-day labels")
        frame.columns = [str(product).upper() for product in frame.columns]
        if frame.columns.has_duplicates:
            raise ValueError("market returns contain duplicate products")
        self.market_returns = frame
        self.risk_audit: list[dict] = []
        self.risk_governor = _ObservedCovarianceScale(0.0)

    def observe_target_state(
        self,
        *,
        day: pd.Timestamp,
        product_weights: Mapping[str, float],
    ) -> None:
        day = pd.Timestamp(day)
        if pd.isna(day) or day.tzinfo is not None or day != day.normalize():
            raise ValueError("target day must be a valid timezone-naive trading-day label")
        completed = self.market_returns.loc[self.market_returns.index < day].iloc[-63:]
        decision = covariance_risk_budget(product_weights, completed)
        self.risk_governor = _ObservedCovarianceScale(decision.scale)
        self.risk_audit.append(
            {
                "target_day": day,
                "prior_through": completed.index[-1] if len(completed) else None,
                **asdict(decision),
            }
        )
