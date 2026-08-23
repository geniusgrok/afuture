"""Causal execution-efficiency primitives for the directional production path.

This module never owns Alpha, account state or risk authority. It only explains or
suppresses economically low-value changes that the frozen directional policy already
requested, while hard risk actions remain authoritative elsewhere.
"""
from __future__ import annotations

from typing import Mapping


TURNOVER_BUCKETS = ("roll", "resize", "reversal", "entry_exit")


def attribute_rebalance_deltas(
    *,
    original_lots: Mapping[str, int],
    target_lots: Mapping[str, int],
    executed_deltas: Mapping[str, int],
    lot_notionals: Mapping[str, float],
    symbol_products: Mapping[str, str],
) -> dict[str, float]:
    """Classify executed normal-rebalance turnover without changing the rebalance.

    Classification is based on the pre-rebalance holdings and the final requested target,
    so a reduction-first close and its later opening are assigned to the same roll or
    reversal family. The function is deliberately accounting-only.
    """
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
            if (original[1] > 0) != (target[1] > 0):
                bucket = "reversal"
            else:
                bucket = "resize"
        else:
            bucket = "entry_exit"
        result[bucket] += abs(delta) * notional

    return result
