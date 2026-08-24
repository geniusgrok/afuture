"""Research-only nearest floor/ceil realization for validated survivor targets.

The continuous Product target remains authoritative. This adapter changes only the
continuous-target -> signed integer-lot translation: after the normal floor construction,
it may recover at most one lot per nonzero target whose fractional ideal-lot remainder is
strictly greater than one half. Recovery is deterministic by largest fractional remainder
then symbol and is allowed only while both the continuous target gross and the existing
causal soft-margin budget remain unexceeded.

No product support, sign, Alpha, risk threshold, hard account gate or 35-lot cap changes.
The existing one-lot stabilizer and freeze-new-risk response are applied after margin fit.
"""
from __future__ import annotations

from math import floor
from typing import Mapping

import numpy as np

from .directional import adaptive_margin_sizing_share, fit_target_lots_to_margin_budget
from .directional_acceptance import (
    DirectionalProductionAcceptance,
    PRODUCT_MULTIPLIERS,
    TargetLotStages,
)
from .directional_efficiency import stabilize_one_lot_increases
from .directional_freeze_new_risk import (
    FreezeNewRiskDirectionalProductionAcceptance,
    freeze_new_risk_target,
)

_EPS = 1e-12


def recover_floor_ceil_rounding(
    *,
    floor_lots: Mapping[str, int],
    ideal_lots: Mapping[str, float],
    lot_notionals: Mapping[str, float],
    per_lot_margin: Mapping[str, float],
    gross_budget: float,
    margin_budget: float,
    max_contract_volume: int,
) -> dict[str, int]:
    """Recover only >half-lot residuals without exceeding continuous gross or soft margin."""
    if gross_budget < -_EPS:
        raise ValueError("gross_budget cannot be negative")
    if margin_budget < -_EPS:
        raise ValueError("margin_budget cannot be negative")
    if int(max_contract_volume) <= 0:
        raise ValueError("max_contract_volume must be positive")

    result: dict[str, int] = {
        str(symbol): int(volume)
        for symbol, volume in floor_lots.items()
        if int(volume) != 0
    }
    used_gross = 0.0
    used_margin = 0.0
    for symbol, volume in result.items():
        ideal = float(ideal_lots.get(symbol, 0.0))
        notional = float(lot_notionals.get(symbol, 0.0))
        margin = float(per_lot_margin.get(symbol, 0.0))
        if not np.isfinite(ideal) or notional <= 0.0 or margin <= 0.0:
            raise ValueError(f"invalid integer evidence: {symbol}")
        if np.sign(volume) != np.sign(ideal):
            raise ValueError(f"floor lot sign differs from ideal target: {symbol}")
        expected_floor = min(int(max_contract_volume), floor(abs(ideal)))
        if abs(int(volume)) != expected_floor:
            raise ValueError(f"floor lots do not match ideal target: {symbol}")
        used_gross += abs(int(volume)) * notional
        used_margin += abs(int(volume)) * margin

    candidates: list[tuple[float, str]] = []
    for raw_symbol, raw_ideal in ideal_lots.items():
        symbol = str(raw_symbol)
        ideal = float(raw_ideal)
        if not np.isfinite(ideal) or abs(ideal) <= _EPS:
            continue
        notional = float(lot_notionals.get(symbol, 0.0))
        margin = float(per_lot_margin.get(symbol, 0.0))
        if notional <= 0.0 or margin <= 0.0:
            raise ValueError(f"missing positive lot evidence: {symbol}")
        magnitude = abs(ideal)
        floored = min(int(max_contract_volume), floor(magnitude))
        existing = abs(int(result.get(symbol, 0)))
        if existing != floored:
            if existing == 0 and floored == 0:
                pass
            else:
                raise ValueError(f"floor lots do not match ideal target: {symbol}")
        if floored >= int(max_contract_volume):
            continue
        fraction = magnitude - floor(magnitude)
        if fraction > 0.5 + _EPS:
            candidates.append((fraction, symbol))

    for _fraction, symbol in sorted(candidates, key=lambda item: (-item[0], item[1])):
        notional = float(lot_notionals[symbol])
        margin = float(per_lot_margin[symbol])
        if used_gross + notional > float(gross_budget) + 1e-10:
            continue
        if used_margin + margin > float(margin_budget) + 1e-10:
            continue
        ideal = float(ideal_lots[symbol])
        magnitude = abs(int(result.get(symbol, 0))) + 1
        if magnitude > int(max_contract_volume):
            continue
        result[symbol] = magnitude if ideal > 0.0 else -magnitude
        used_gross += notional
        used_margin += margin

    if used_gross > float(gross_budget) + 1e-10:
        raise AssertionError("floor/ceil recovery exceeded continuous gross")
    if used_margin > float(margin_budget) + 1e-10:
        # Existing floor lots may already exceed the soft margin budget; callers are then
        # expected to run the unchanged margin fitter. Recovery itself must never be the
        # operation that crosses the budget.
        floor_margin = sum(
            abs(int(volume)) * float(per_lot_margin[str(symbol)])
            for symbol, volume in floor_lots.items()
            if int(volume) != 0
        )
        if floor_margin <= float(margin_budget) + 1e-10:
            raise AssertionError("floor/ceil recovery exceeded soft margin")
    return {symbol: volume for symbol, volume in result.items() if int(volume) != 0}


class SurvivorIntegerDirectionalProductionAcceptance(
    FreezeNewRiskDirectionalProductionAcceptance
):
    """Freeze-new-risk Production mechanics with nearest floor/ceil integer recovery."""

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
        # Bypass MarginAware/Freeze target construction here only to replace its initial
        # per-product floor stage. All subsequent margin, stabilizer and freeze semantics
        # are reproduced unchanged below.
        floor_stage = DirectionalProductionAcceptance.target_lot_stages(
            self,
            equity=equity,
            product_weights=product_weights,
            product_open_prices=product_open_prices,
            selected_symbols=selected_symbols,
            current_lots=current_lots,
            completed_returns=completed_returns,
        )
        if equity <= 0.0:
            return floor_stage

        sizing_share = adaptive_margin_sizing_share(
            max_margin_ratio=self.config.max_margin_ratio,
            min_available_ratio=self.config.min_available_ratio,
            max_daily_loss_ratio=self.config.max_daily_loss_ratio,
            completed_returns=completed_returns,
        )
        symbol_product = {
            str(symbol): str(product).upper()
            for product, symbol in selected_symbols.items()
        }
        ideal_lots: dict[str, float] = {}
        lot_notionals: dict[str, float] = {}
        per_lot_margin: dict[str, float] = {}
        available_desired_notional = 0.0

        for raw_product, raw_weight in sorted(product_weights.items()):
            product = str(raw_product).upper()
            weight = float(raw_weight)
            if abs(weight) <= 1e-15:
                continue
            symbol = selected_symbols.get(product)
            price = float(product_open_prices.get(product, 0.0))
            multiplier = PRODUCT_MULTIPLIERS.get(product)
            if symbol is None or price <= 0.0 or multiplier is None:
                continue
            lot_notional = price * float(multiplier)
            desired = float(equity) * abs(weight)
            ideal = desired / lot_notional
            signed_ideal = ideal if weight > 0.0 else -ideal
            symbol = str(symbol)
            ideal_lots[symbol] = signed_ideal
            lot_notionals[symbol] = lot_notional
            per_lot_margin[symbol] = (
                lot_notional
                * float(self.config.margin_rate_proxy)
                * float(self.config.margin_estimate_buffer)
            )
            # Do not reallocate max-volume clipping residual into another product. Only
            # genuine integer-rounding residual is eligible for floor/ceil recovery.
            capped_desired = min(
                desired,
                int(self.config.max_contract_volume) * lot_notional,
            )
            available_desired_notional += capped_desired

        requested = recover_floor_ceil_rounding(
            floor_lots=floor_stage.raw_integer_lots,
            ideal_lots=ideal_lots,
            lot_notionals=lot_notionals,
            per_lot_margin=per_lot_margin,
            gross_budget=available_desired_notional,
            margin_budget=float(equity) * sizing_share,
            max_contract_volume=self.config.max_contract_volume,
        )
        fitted = fit_target_lots_to_margin_budget(
            requested,
            per_lot_margin,
            margin_budget=float(equity) * sizing_share,
        )

        current = {
            str(symbol): int(volume)
            for symbol, volume in (current_lots or {}).items()
            if int(volume) != 0
        }
        stabilized = dict(fitted)
        if current and set(current).issubset(lot_notionals):
            stabilized = stabilize_one_lot_increases(
                current_lots=current,
                target_lots=fitted,
                lot_notionals=lot_notionals,
                per_lot_margin=per_lot_margin,
                equity=float(equity),
                soft_margin_share=sizing_share,
                max_gross_ratio=self.config.max_realized_gross_ratio,
            )

        final = dict(stabilized)
        if self.freeze_triggered(completed_returns):
            symbols = set(current) | set(stabilized)
            products = {
                symbol: symbol_product.get(symbol, self._product(symbol))
                for symbol in symbols
            }
            final = freeze_new_risk_target(
                current_lots=current,
                target_lots=stabilized,
                symbol_products=products,
                triggered=True,
            )

        def gross(lots: Mapping[str, int]) -> float:
            return float(
                sum(
                    abs(int(volume)) * float(lot_notionals.get(str(symbol), 0.0))
                    for symbol, volume in lots.items()
                )
            )

        raw_integer_notional = gross(requested)
        margin_fitted_notional = gross(fitted)
        final_notional = gross(final)
        rounding_loss = max(
            0.0,
            float(available_desired_notional) - raw_integer_notional,
        )
        return TargetLotStages(
            raw_integer_lots=dict(requested),
            margin_fitted_lots=dict(fitted),
            final_lots=dict(final),
            desired_notional=float(floor_stage.desired_notional),
            raw_integer_notional=raw_integer_notional,
            margin_fitted_notional=margin_fitted_notional,
            final_notional=final_notional,
            integer_rounding_loss_notional=rounding_loss,
            max_volume_clipping_notional=float(
                floor_stage.max_volume_clipping_notional
            ),
            unavailable_contract_notional=float(
                floor_stage.unavailable_contract_notional
            ),
            soft_margin_share=sizing_share,
        )
