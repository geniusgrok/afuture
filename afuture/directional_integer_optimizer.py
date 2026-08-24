"""Deterministic research-only optimizer for Production integer targets.

The optimizer does not generate Alpha and does not own risk authority. It searches only
inside raw directional intent and a caller-supplied soft margin envelope; downstream
RiskManager/Broker gates remain authoritative.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Callable, Mapping

HARD_MAX_GROSS_RATIO = 2.0
HARD_MAX_ABS_LOTS = 35
_EPS = 1e-10


@dataclass(frozen=True)
class IntegerOptimizationResult:
    target_lots: dict[str, int]
    reference_objective: float
    objective_value: float
    iterations: int = 0
    fallback_reason: str = ""

    @property
    def improvement(self) -> float:
        return float(self.objective_value - self.reference_objective)


def _normalized(lots: Mapping[str, int]) -> dict[str, int]:
    return {str(symbol): int(volume) for symbol, volume in lots.items() if int(volume)}


def _within_requested_intent(position: int, requested: int, cap: int) -> bool:
    if requested > 0:
        return 0 <= position <= min(requested, cap)
    if requested < 0:
        return max(requested, -cap) <= position <= 0
    return position == 0


def _legal_deltas(position: int, requested: int, cap: int) -> tuple[int, ...]:
    result: list[int] = []
    for delta in (-1, 1):
        if _within_requested_intent(position + delta, requested, cap):
            result.append(delta)
    return tuple(result)


def _objective_value(
    objective: Callable[[Mapping[str, int]], float],
    target: Mapping[str, int],
) -> float | None:
    try:
        value = float(objective(dict(target)))
    except Exception:
        return None
    return value if isfinite(value) else None


def optimize_integer_targets(
    *,
    reference_lots: Mapping[str, int],
    requested_lots: Mapping[str, int],
    current_lots: Mapping[str, int],
    lot_notionals: Mapping[str, float],
    per_lot_margin: Mapping[str, float],
    equity: float,
    soft_margin_budget: float,
    max_gross_ratio: float,
    max_abs_lots: int,
    objective: Callable[[Mapping[str, int]], float],
) -> IntegerOptimizationResult:
    reference = _normalized(reference_lots)
    requested = {str(symbol): int(volume) for symbol, volume in requested_lots.items()}
    current = {str(symbol): int(volume) for symbol, volume in current_lots.items()}

    if not isfinite(float(equity)) or float(equity) <= 0.0:
        raise ValueError("equity must be finite and positive")
    if not isfinite(float(soft_margin_budget)) or float(soft_margin_budget) < 0.0:
        raise ValueError("soft_margin_budget must be finite and nonnegative")
    if float(soft_margin_budget) > float(equity) * 0.35 + _EPS:
        raise ValueError("soft_margin_budget cannot exceed the hard 35% margin gate")
    if not isfinite(float(max_gross_ratio)) or not 0.0 < float(max_gross_ratio) <= HARD_MAX_GROSS_RATIO:
        raise ValueError("max_gross_ratio must be in (0, 2.0]")
    if int(max_abs_lots) <= 0:
        raise ValueError("max_abs_lots must be positive")
    cap = min(int(max_abs_lots), HARD_MAX_ABS_LOTS)

    symbols = sorted(set(reference) | set(requested) | set(current))
    notionals: dict[str, float] = {}
    margins: dict[str, float] = {}
    for symbol in symbols:
        notional = float(lot_notionals.get(symbol, 0.0))
        margin = float(per_lot_margin.get(symbol, 0.0))
        if not isfinite(notional) or notional <= 0.0:
            raise ValueError(f"missing positive lot notional: {symbol}")
        if not isfinite(margin) or margin <= 0.0:
            raise ValueError(f"missing positive per-lot margin: {symbol}")
        notionals[symbol] = notional
        margins[symbol] = margin

    def feasible(target: Mapping[str, int]) -> bool:
        normalized = _normalized(target)
        for symbol in set(normalized) | set(requested):
            if not _within_requested_intent(
                int(normalized.get(symbol, 0)), int(requested.get(symbol, 0)), cap
            ):
                return False
        gross = sum(abs(volume) * notionals[symbol] for symbol, volume in normalized.items())
        if gross > float(equity) * float(max_gross_ratio) + _EPS:
            return False
        margin = sum(abs(volume) * margins[symbol] for symbol, volume in normalized.items())
        return margin <= float(soft_margin_budget) + _EPS

    reference_value = _objective_value(objective, reference)
    if reference_value is None:
        return IntegerOptimizationResult(
            target_lots=dict(reference),
            reference_objective=0.0,
            objective_value=0.0,
            fallback_reason="non-finite objective",
        )
    if not feasible(reference):
        return IntegerOptimizationResult(
            target_lots=dict(reference),
            reference_objective=reference_value,
            objective_value=reference_value,
            fallback_reason="reference target is outside optimizer feasibility",
        )

    target = dict(reference)
    target_value = reference_value
    iterations = 0

    def evaluate(candidate: Mapping[str, int]) -> float | None:
        if not feasible(candidate):
            return None
        return _objective_value(objective, _normalized(candidate))

    while True:
        best: tuple[float, str, int, dict[str, int], float] | None = None
        for symbol in sorted(requested):
            position = int(target.get(symbol, 0))
            request = int(requested[symbol])
            for delta in _legal_deltas(position, request, cap):
                candidate = dict(target)
                proposed = position + delta
                if proposed:
                    candidate[symbol] = proposed
                else:
                    candidate.pop(symbol, None)
                value = evaluate(candidate)
                if value is None:
                    if feasible(candidate):
                        return IntegerOptimizationResult(
                            target_lots=dict(reference),
                            reference_objective=reference_value,
                            objective_value=reference_value,
                            fallback_reason="non-finite objective",
                        )
                    continue
                improvement = value - target_value
                if improvement <= _EPS:
                    continue
                item = (improvement, symbol, delta, _normalized(candidate), value)
                if best is None or improvement > best[0] + _EPS:
                    best = item
        if best is not None:
            target = best[3]
            target_value = best[4]
            iterations += 1
            continue

        # A higher-value lot can be blocked by margin/gross capacity even when removing
        # the incumbent lot alone is not beneficial. Evaluate deterministic two-leg
        # swaps only after no positive one-lot move remains.
        pair_best = None
        symbols_with_moves = [
            (symbol, delta)
            for symbol in sorted(requested)
            for delta in _legal_deltas(
                int(target.get(symbol, 0)), int(requested[symbol]), cap
            )
        ]
        for index, (first_symbol, first_delta) in enumerate(symbols_with_moves):
            for second_symbol, second_delta in symbols_with_moves[index + 1 :]:
                if first_symbol == second_symbol:
                    continue
                candidate = dict(target)
                for symbol, delta in (
                    (first_symbol, first_delta),
                    (second_symbol, second_delta),
                ):
                    proposed = int(candidate.get(symbol, 0)) + delta
                    if proposed:
                        candidate[symbol] = proposed
                    else:
                        candidate.pop(symbol, None)
                if not feasible(candidate):
                    continue
                value = _objective_value(objective, _normalized(candidate))
                if value is None:
                    return IntegerOptimizationResult(
                        target_lots=dict(reference),
                        reference_objective=reference_value,
                        objective_value=reference_value,
                        fallback_reason="non-finite objective",
                    )
                improvement = value - target_value
                if improvement <= _EPS:
                    continue
                item = (
                    improvement,
                    first_symbol,
                    first_delta,
                    second_symbol,
                    second_delta,
                    _normalized(candidate),
                    value,
                )
                if pair_best is None or improvement > pair_best[0] + _EPS:
                    pair_best = item
        if pair_best is None:
            break
        target = pair_best[5]
        target_value = pair_best[6]
        iterations += 1

    return IntegerOptimizationResult(
        target_lots=_normalized(target),
        reference_objective=reference_value,
        objective_value=target_value,
        iterations=iterations,
    )
