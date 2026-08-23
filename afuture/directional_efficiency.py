"""Causal execution-efficiency primitives for the directional production path.

This module never owns Alpha, account state or risk authority. It only explains or
suppresses economically low-value changes that the frozen directional policy already
requested, while hard risk actions remain authoritative elsewhere.
"""
from __future__ import annotations

from typing import Mapping

import numpy as np
import pandas as pd


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


def audit_policy_weight_history(policy, open_prices: pd.DataFrame, close: pd.DataFrame):
    """Reproduce the frozen policy path while recording selection and weight turnover.

    This is intentionally an audit adapter rather than a second production policy. It
    calls the same private frozen primitives and constants as `ExecutionAlignedAggressivePolicy`
    and is regression-tested against `weight_history()` before any efficiency behavior is
    enabled.
    """
    from . import execution_aligned_policy as frozen

    cleaned_close = frozen._clean_prices(close, policy.products)
    cleaned_open = frozen._clean_prices(open_prices, policy.products).reindex(
        cleaned_close.index
    )
    returns = cleaned_close.pct_change(fill_method=None)
    returns = returns.mask(returns.abs() > frozen.MAX_ABS_DAILY_RETURN)

    base_streams: dict[str, pd.Series] = {}
    stress_streams: dict[str, pd.Series] = {}
    paths: dict[str, pd.DataFrame] = {}
    for template_id, template in zip(policy.template_ids, frozen._EXECUTION_TEMPLATES):
        weights = frozen._template_weight_path(returns, template)
        paths[template_id] = weights
        base_streams[template_id] = frozen._intraday_proxy_stream(
            cleaned_open,
            cleaned_close,
            weights,
            cost_bps=frozen.BASE_COST_BPS,
        )
        stress_streams[template_id] = frozen._intraday_proxy_stream(
            cleaned_open,
            cleaned_close,
            weights,
            cost_bps=frozen.STRESS_COST_BPS,
        )

    base_frame = pd.DataFrame(base_streams).sort_index().fillna(0.0)
    stress_frame = pd.DataFrame(stress_streams).reindex(
        index=base_frame.index,
        columns=base_frame.columns,
    ).fillna(0.0)
    scores = frozen._robust_trailing_scores(
        base_frame,
        stress_frame,
        lookback=policy.meta_lookback,
    )
    names = list(base_frame.columns)
    final = pd.DataFrame(0.0, index=cleaned_close.index, columns=cleaned_close.columns)
    selected: list[int] = []
    selected_rows: list[tuple[str, ...]] = []
    switch_rows: list[bool] = []

    for position, timestamp in enumerate(cleaned_close.index):
        switched = False
        if position >= policy.meta_lookback and (
            not selected or position % policy.meta_rebalance == 0
        ):
            previous = tuple(selected)
            row = scores[position]
            valid = np.flatnonzero(np.isfinite(row))
            selected = (
                [
                    int(item)
                    for item in valid[
                        np.argsort(-row[valid], kind="stable")
                    ][: policy.meta_count]
                ]
                if valid.size
                else []
            )
            switched = bool(previous) and tuple(selected) != previous
        if selected:
            rows = [paths[names[item]].loc[timestamp] for item in selected]
            final.loc[timestamp] = pd.concat(rows, axis=1).mean(axis=1)
        selected_rows.append(tuple(names[item] for item in selected))
        switch_rows.append(switched)

    gross = final.abs().sum(axis=1)
    if bool((gross > frozen.MAX_GROSS_LEVERAGE + 1e-10).any()):
        raise AssertionError("execution-aligned policy audit exceeded 2x gross")

    turnover = final.diff().abs().sum(axis=1)
    if len(turnover):
        turnover.iloc[0] = float(final.iloc[0].abs().sum())
    audit = pd.DataFrame(
        {
            "signal_turnover": turnover.astype(float),
            "meta_switch": switch_rows,
            "selected_templates": selected_rows,
        },
        index=final.index,
    )
    return final, audit
