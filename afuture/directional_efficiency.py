"""Causal execution-efficiency primitives for the directional production path.

This module never owns Alpha, account state or risk authority. It only explains or
suppresses economically low-value changes that the frozen directional policy already
requested, while hard risk actions remain authoritative elsewhere.
"""
from __future__ import annotations

from typing import Iterable, Mapping


TURNOVER_BUCKETS = ("roll", "resize", "reversal", "entry_exit")


def attribute_rebalance_deltas(
    *,
    original_lots: Mapping[str, int],
    target_lots: Mapping[str, int],
    executed_deltas: Mapping[str, int],
    lot_notionals: Mapping[str, float],
    symbol_products: Mapping[str, str],
) -> dict[str, float]:
    """Classify normal-rebalance turnover without changing the requested trade."""
    result = {name: 0.0 for name in TURNOVER_BUCKETS}
    original_by_product = {
        str(symbol_products[symbol]).upper(): (symbol, int(volume))
        for symbol, volume in original_lots.items()
        if int(volume) != 0 and symbol in symbol_products
    }
    target_by_product = {
        str(symbol_products[symbol]).upper(): (symbol, int(volume))
        for symbol, volume in target_lots.items()
        if int(volume) != 0 and symbol in symbol_products
    }
    for symbol, raw_delta in executed_deltas.items():
        delta = int(raw_delta)
        if delta == 0:
            continue
        notional = float(lot_notionals.get(symbol, 0.0))
        if notional <= 0:
            raise ValueError(f"missing positive lot notional: {symbol}")
        product = str(symbol_products.get(symbol, "")).upper()
        if not product:
            raise ValueError(f"missing product classification: {symbol}")
        original = original_by_product.get(product)
        target = target_by_product.get(product)
        if original is not None and target is not None and original[0] != target[0]:
            bucket = "roll"
        elif original is not None and target is not None and original[0] == target[0]:
            bucket = "reversal" if (original[1] > 0) != (target[1] > 0) else "resize"
        else:
            bucket = "entry_exit"
        result[bucket] += abs(delta) * notional
    return result


def stabilize_one_lot_increases(
    *,
    current_lots: Mapping[str, int],
    target_lots: Mapping[str, int],
    lot_notionals: Mapping[str, float],
    per_lot_margin: Mapping[str, float],
    equity: float,
    soft_margin_share: float,
    max_gross_ratio: float,
) -> dict[str, int]:
    """Suppress only economically tiny one-lot increases; never suppress reductions.

    The incumbent portfolio must itself remain inside the soft margin envelope and hard
    gross ceiling. This helper never turns a requested reduction into a hold.
    """
    result = {str(symbol): int(volume) for symbol, volume in target_lots.items() if int(volume)}
    if equity <= 0 or not 0 <= soft_margin_share <= 1 or max_gross_ratio <= 0:
        return result
    current_margin = 0.0
    current_gross = 0.0
    for symbol, raw_volume in current_lots.items():
        volume = int(raw_volume)
        if not volume:
            continue
        margin = float(per_lot_margin.get(symbol, 0.0))
        notional = float(lot_notionals.get(symbol, 0.0))
        if margin <= 0 or notional <= 0:
            return result
        current_margin += abs(volume) * margin
        current_gross += abs(volume) * notional
    if current_margin > equity * soft_margin_share + 1e-10:
        return result
    if current_gross > equity * max_gross_ratio + 1e-10:
        return result
    for symbol, raw_target in list(result.items()):
        current = int(current_lots.get(symbol, 0))
        target = int(raw_target)
        if current == 0 or target == 0 or (current > 0) != (target > 0):
            continue
        if abs(target) == abs(current) + 1:
            result[symbol] = current
    return {symbol: volume for symbol, volume in result.items() if volume}


def cost_aware_no_trade_target(
    *,
    current_lots: Mapping[str, int],
    target_lots: Mapping[str, int],
    lot_notionals: Mapping[str, float],
    expected_daily_returns: Mapping[str, float],
    horizon_days: int,
    cost_bps: float,
    force_execute_symbols: Iterable[str] = (),
) -> dict[str, int]:
    """Suppress only non-positive-net-benefit openings and same-sign increases.

    ``expected_daily_returns`` must be point-in-time evidence supplied by the caller.
    Reductions, exits, reversals, and explicitly forced symbols (for example a safety
    roll) always preserve the requested target. The helper can only hold incumbent risk;
    it never creates additional gross exposure.
    """
    horizon = max(1, int(horizon_days))
    cost_rate = max(0.0, float(cost_bps)) / 10000.0
    forced = {str(symbol) for symbol in force_execute_symbols}
    current = {
        str(symbol): int(volume)
        for symbol, volume in current_lots.items()
        if int(volume) != 0
    }
    requested = {
        str(symbol): int(volume)
        for symbol, volume in target_lots.items()
        if int(volume) != 0
    }
    result = dict(requested)

    # Symbols absent from the target are exits. They remain absent from the returned
    # target, so downstream reduction-first planning executes them normally.
    for symbol, target in list(requested.items()):
        if symbol in forced:
            continue
        incumbent = int(current.get(symbol, 0))
        if incumbent != 0:
            same_sign = (incumbent > 0) == (target > 0)
            if not same_sign:
                # Reversal bypasses the no-trade region.
                continue
            if abs(target) <= abs(incumbent):
                # Any reduction, including partial risk reduction, bypasses the filter.
                continue
        # incumbent == 0 => new opening; same-sign absolute increase reaches here.
        lot_notional = float(lot_notionals.get(symbol, 0.0))
        if lot_notional <= 0:
            # Missing cost evidence fails closed for optional new risk: keep incumbent.
            if incumbent:
                result[symbol] = incumbent
            else:
                result.pop(symbol, None)
            continue
        expected_daily = float(expected_daily_returns.get(symbol, 0.0))
        directional_edge = (1.0 if target > 0 else -1.0) * expected_daily
        delta_lots = abs(target) - abs(incumbent)
        expected_benefit = (
            max(0, delta_lots)
            * lot_notional
            * directional_edge
            * horizon
        )
        transition_cost = max(0, delta_lots) * lot_notional * cost_rate
        if expected_benefit <= transition_cost + 1e-12:
            if incumbent:
                result[symbol] = incumbent
            else:
                result.pop(symbol, None)

    return {
        symbol: int(volume)
        for symbol, volume in result.items()
        if int(volume) != 0
    }
