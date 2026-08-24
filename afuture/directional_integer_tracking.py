"""Research-only integer target tracking for directional Production mechanics.

This module changes no live wiring. It converts the already-approved signed continuous
product target into a bounded integer target without introducing outcome information,
new products, sign flips, extra gross, extra margin, or a higher per-contract lot cap.
The primary objective is to use as much of the existing target gross as the current
soft-margin envelope permits; ties minimize absolute product-level tracking error.
"""
from __future__ import annotations

from math import ceil, floor, isfinite
from typing import Mapping

from .directional import adaptive_margin_sizing_share, fit_target_lots_to_margin_budget
from .directional_acceptance import (
    DirectionalProductionAcceptance,
    PRODUCT_MULTIPLIERS,
    TargetLotStages,
)
from .directional_efficiency import stabilize_one_lot_increases
from .directional_robustness import MarginAwareDirectionalProductionAcceptance

_EPS = 1e-10
_EXACT_EXTRA_LIMIT = 20


def _signed(magnitude: int, desired: float) -> int:
    return int(magnitude) if desired > 0 else -int(magnitude)


def _tracking_error(
    magnitudes: Mapping[str, int],
    desired_lots: Mapping[str, float],
    lot_notionals: Mapping[str, float],
) -> float:
    return float(
        sum(
            abs(abs(float(desired_lots[symbol])) * float(lot_notionals[symbol])
                - int(magnitudes.get(symbol, 0)) * float(lot_notionals[symbol]))
            for symbol in desired_lots
        )
    )


def optimize_integer_tracking_target(
    *,
    desired_lots: Mapping[str, float],
    lot_notionals: Mapping[str, float],
    per_lot_margin: Mapping[str, float],
    target_gross_budget: float,
    margin_budget: float,
    max_contract_volume: int,
) -> dict[str, int]:
    """Return a deterministic signed integer target inside existing risk budgets.

    Each requested symbol may use only the sign of its continuous target and no more than
    ``ceil(abs(desired_lots))`` (capped by ``max_contract_volume``). When the ordinary
    floors fit, the only search is whether to allocate the one rounding lot above each
    floor. That bounded subset is solved exactly for up to 20 active symbols. If the floor
    itself does not fit the soft margin budget, the repository's existing proportional
    fail-closed fitter reduces it first; subsequent additions remain bounded by the same
    ceil neighborhood and both budgets.
    """
    gross_budget = float(target_gross_budget)
    soft_margin_budget = float(margin_budget)
    if gross_budget < 0 or soft_margin_budget < 0:
        raise ValueError("integer tracking budgets cannot be negative")
    if max_contract_volume <= 0:
        raise ValueError("max_contract_volume must be positive")

    desired: dict[str, float] = {}
    notionals: dict[str, float] = {}
    margins: dict[str, float] = {}
    floors: dict[str, int] = {}
    uppers: dict[str, int] = {}
    for raw_symbol in sorted(desired_lots):
        symbol = str(raw_symbol)
        value = float(desired_lots[raw_symbol])
        if not isfinite(value) or abs(value) <= 1e-15:
            continue
        notional = float(lot_notionals.get(symbol, 0.0))
        unit_margin = float(per_lot_margin.get(symbol, 0.0))
        if not isfinite(notional) or notional <= 0:
            raise ValueError(f"missing positive lot notional: {symbol}")
        if not isfinite(unit_margin) or unit_margin <= 0:
            raise ValueError(f"missing positive per-lot margin: {symbol}")
        magnitude = abs(value)
        lower = min(max_contract_volume, floor(magnitude + 1e-15))
        upper = min(max_contract_volume, ceil(magnitude - 1e-15))
        desired[symbol] = value
        notionals[symbol] = notional
        margins[symbol] = unit_margin
        floors[symbol] = int(lower)
        uppers[symbol] = int(max(lower, upper))

    if not desired or gross_budget <= _EPS or soft_margin_budget <= _EPS:
        return {}

    floor_signed = {
        symbol: _signed(volume, desired[symbol])
        for symbol, volume in floors.items()
        if volume > 0
    }
    floor_gross = sum(floors[symbol] * notionals[symbol] for symbol in desired)
    floor_margin = sum(floors[symbol] * margins[symbol] for symbol in desired)

    if floor_gross <= gross_budget + _EPS and floor_margin <= soft_margin_budget + _EPS:
        magnitudes = dict(floors)
    else:
        fitted = fit_target_lots_to_margin_budget(
            floor_signed,
            margins,
            margin_budget=soft_margin_budget,
        )
        magnitudes = {symbol: abs(int(fitted.get(symbol, 0))) for symbol in desired}
        # The continuous target itself is already <= the caller's gross cap, but a very
        # heterogeneous external margin map can still require an extra gross fail-closed
        # reduction after margin fitting. Remove largest notionals first until legal.
        while sum(magnitudes[s] * notionals[s] for s in desired) > gross_budget + _EPS:
            candidates = [s for s in desired if magnitudes[s] > 0]
            if not candidates:
                break
            symbol = max(candidates, key=lambda s: (notionals[s], s))
            magnitudes[symbol] -= 1

    candidates = [s for s in desired if magnitudes[s] < uppers[s]]
    base_gross = sum(magnitudes[s] * notionals[s] for s in desired)
    base_margin = sum(magnitudes[s] * margins[s] for s in desired)

    if candidates and len(candidates) <= _EXACT_EXTRA_LIMIT:
        best = tuple()
        best_gross = base_gross
        best_error = _tracking_error(magnitudes, desired, notionals)
        count = len(candidates)
        for mask in range(1 << count):
            extra_gross = 0.0
            extra_margin = 0.0
            chosen: list[str] = []
            legal = True
            for index, symbol in enumerate(candidates):
                if not (mask >> index) & 1:
                    continue
                extra_gross += notionals[symbol]
                extra_margin += margins[symbol]
                if (
                    base_gross + extra_gross > gross_budget + _EPS
                    or base_margin + extra_margin > soft_margin_budget + _EPS
                ):
                    legal = False
                    break
                chosen.append(symbol)
            if not legal:
                continue
            trial = dict(magnitudes)
            for symbol in chosen:
                trial[symbol] += 1
            gross = base_gross + extra_gross
            error = _tracking_error(trial, desired, notionals)
            key = tuple(chosen)
            if (
                gross > best_gross + _EPS
                or (
                    abs(gross - best_gross) <= _EPS
                    and error < best_error - _EPS
                )
                or (
                    abs(gross - best_gross) <= _EPS
                    and abs(error - best_error) <= _EPS
                    and key < best
                )
            ):
                best = key
                best_gross = gross
                best_error = error
        for symbol in best:
            magnitudes[symbol] += 1
    elif candidates:
        # Deterministic fallback for unexpectedly broad live/research target breadth.
        # Prefer additions that reduce tracking error, then smaller unit margin/notional
        # so the existing budgets are not stranded by one coarse contract.
        ordered = sorted(
            candidates,
            key=lambda s: (
                -(
                    abs(abs(desired[s]) - magnitudes[s])
                    - abs(abs(desired[s]) - (magnitudes[s] + 1))
                ),
                margins[s],
                notionals[s],
                s,
            ),
        )
        for symbol in ordered:
            if magnitudes[symbol] >= uppers[symbol]:
                continue
            gross = sum(magnitudes[s] * notionals[s] for s in desired)
            margin = sum(magnitudes[s] * margins[s] for s in desired)
            if (
                gross + notionals[symbol] <= gross_budget + _EPS
                and margin + margins[symbol] <= soft_margin_budget + _EPS
            ):
                magnitudes[symbol] += 1

    result = {
        symbol: _signed(magnitude, desired[symbol])
        for symbol, magnitude in magnitudes.items()
        if magnitude > 0
    }
    for symbol, volume in result.items():
        if (volume > 0) != (desired[symbol] > 0):
            raise AssertionError("integer tracking changed target direction")
        if abs(volume) > uppers[symbol] or abs(volume) > max_contract_volume:
            raise AssertionError("integer tracking exceeded target neighborhood")
    if sum(abs(result.get(s, 0)) * notionals[s] for s in desired) > gross_budget + _EPS:
        raise AssertionError("integer tracking exceeded gross budget")
    if sum(abs(result.get(s, 0)) * margins[s] for s in desired) > soft_margin_budget + _EPS:
        raise AssertionError("integer tracking exceeded margin budget")
    return result


class IntegerTrackingDirectionalProductionAcceptance(
    MarginAwareDirectionalProductionAcceptance
):
    """Research adapter that replaces independent floor rounding with portfolio tracking."""

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
        raw = DirectionalProductionAcceptance.target_lot_stages(
            self,
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
        if equity <= 0:
            return raw

        desired_lots: dict[str, float] = {}
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
            if symbol is None or multiplier is None or price <= 0:
                continue
            lot_notional = price * float(multiplier)
            desired_notional = float(equity) * abs(weight)
            available_desired_notional += desired_notional
            desired_lots[str(symbol)] = (
                desired_notional / lot_notional if weight > 0 else -desired_notional / lot_notional
            )
            lot_notionals[str(symbol)] = lot_notional
            per_lot_margin[str(symbol)] = (
                lot_notional
                * float(self.config.margin_rate_proxy)
                * float(self.config.margin_estimate_buffer)
            )

        optimized = optimize_integer_tracking_target(
            desired_lots=desired_lots,
            lot_notionals=lot_notionals,
            per_lot_margin=per_lot_margin,
            target_gross_budget=min(
                available_desired_notional,
                float(equity) * float(self.config.max_realized_gross_ratio),
            ),
            margin_budget=float(equity) * float(sizing_share),
            max_contract_volume=int(self.config.max_contract_volume),
        )
        current = {
            str(symbol): int(volume)
            for symbol, volume in (current_lots or {}).items()
            if int(volume)
        }
        final = dict(optimized)
        if current and set(current).issubset(lot_notionals) and set(current).issubset(per_lot_margin):
            final = stabilize_one_lot_increases(
                current_lots=current,
                target_lots=optimized,
                lot_notionals=lot_notionals,
                per_lot_margin=per_lot_margin,
                equity=float(equity),
                soft_margin_share=float(sizing_share),
                max_gross_ratio=float(self.config.max_realized_gross_ratio),
            )

        def gross(lots: Mapping[str, int]) -> float:
            return float(
                sum(
                    abs(int(volume)) * float(lot_notionals[str(symbol)])
                    for symbol, volume in lots.items()
                    if str(symbol) in lot_notionals
                )
            )

        return TargetLotStages(
            raw_integer_lots=dict(raw.raw_integer_lots),
            margin_fitted_lots=dict(optimized),
            final_lots=dict(final),
            desired_notional=raw.desired_notional,
            raw_integer_notional=raw.raw_integer_notional,
            margin_fitted_notional=gross(optimized),
            final_notional=gross(final),
            integer_rounding_loss_notional=raw.integer_rounding_loss_notional,
            max_volume_clipping_notional=raw.max_volume_clipping_notional,
            unavailable_contract_notional=raw.unavailable_contract_notional,
            soft_margin_share=float(sizing_share),
        )
