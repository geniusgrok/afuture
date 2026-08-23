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


def weight_turnover(left: Mapping[str, float], right: Mapping[str, float]) -> float:
    """One-way L1 target-weight turnover between two portfolio states."""
    return float(
        sum(
            abs(float(right.get(key, 0.0)) - float(left.get(key, 0.0)))
            for key in set(left) | set(right)
        )
    )


def should_switch_meta(
    *,
    incumbent_mean_return: float,
    candidate_mean_return: float,
    incumbent_weights: Mapping[str, float],
    candidate_weights: Mapping[str, float],
    horizon: int,
    cost_bps: float,
    incumbent_survives: bool,
) -> bool:
    """Require completed-history edge improvement to pay modeled switch cost.

    An incumbent that no longer survives the Stress endpoint is replaced immediately.
    Otherwise the candidate must pay the one-way portfolio transition cost over the
    already-frozen meta holding horizon. No fitted score threshold is introduced.
    """
    if not incumbent_survives:
        return True
    if horizon <= 0 or cost_bps < 0:
        raise ValueError("meta horizon must be positive and cost non-negative")
    transition = weight_turnover(incumbent_weights, candidate_weights)
    if transition <= 1e-15:
        return False
    expected_gain = (
        float(candidate_mean_return) - float(incumbent_mean_return)
    ) * float(horizon)
    modeled_cost = transition * float(cost_bps) / 10000.0
    return expected_gain > modeled_cost + 1e-15


def stabilize_same_direction_weights(
    previous: Mapping[str, float],
    candidate: Mapping[str, float],
    *,
    trailing_mean_returns: Mapping[str, float],
    horizon: int,
    cost_bps: float,
) -> dict[str, float]:
    """Suppress only low-value same-direction risk increases.

    Any exit, sign reversal, same-direction reduction, or new entry passes through
    unchanged. The persistence gate may therefore reduce turnover only by declining a
    proposed increase; it can never keep more risk than the frozen candidate requested.
    """
    if horizon <= 0 or cost_bps < 0:
        raise ValueError("product horizon must be positive and cost non-negative")
    result = {str(key): float(value) for key, value in candidate.items()}
    for product in set(previous) | set(candidate):
        old = float(previous.get(product, 0.0))
        new = float(candidate.get(product, 0.0))
        if old == 0.0 or new == 0.0 or (old > 0) != (new > 0) or old == new:
            continue
        if abs(new) <= abs(old):
            continue
        delta = new - old
        mean_return = float(trailing_mean_returns.get(product, 0.0))
        expected_gain = delta * mean_return * float(horizon)
        modeled_round_trip = abs(delta) * 2.0 * float(cost_bps) / 10000.0
        if expected_gain <= modeled_round_trip + 1e-15:
            result[product] = old
    return {key: value for key, value in result.items() if abs(value) > 1e-15}


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


def audit_policy_weight_history(policy, open_prices: pd.DataFrame, close: pd.DataFrame):
    """Reproduce the pre-efficiency policy path for behavior/economic attribution."""
    from . import execution_aligned_policy as frozen

    cleaned_close = frozen._clean_prices(close, policy.products)
    cleaned_open = frozen._clean_prices(open_prices, policy.products).reindex(cleaned_close.index)
    returns = cleaned_close.pct_change(fill_method=None)
    returns = returns.mask(returns.abs() > frozen.MAX_ABS_DAILY_RETURN)
    base_streams: dict[str, pd.Series] = {}
    stress_streams: dict[str, pd.Series] = {}
    paths: dict[str, pd.DataFrame] = {}
    for template_id, template in zip(policy.template_ids, frozen._EXECUTION_TEMPLATES):
        weights = frozen._template_weight_path(returns, template)
        paths[template_id] = weights
        base_streams[template_id] = frozen._intraday_proxy_stream(
            cleaned_open, cleaned_close, weights, cost_bps=frozen.BASE_COST_BPS
        )
        stress_streams[template_id] = frozen._intraday_proxy_stream(
            cleaned_open, cleaned_close, weights, cost_bps=frozen.STRESS_COST_BPS
        )
    base_frame = pd.DataFrame(base_streams).sort_index().fillna(0.0)
    stress_frame = pd.DataFrame(stress_streams).reindex(index=base_frame.index, columns=base_frame.columns).fillna(0.0)
    scores = frozen._robust_trailing_scores(base_frame, stress_frame, lookback=policy.meta_lookback)
    names = list(base_frame.columns)
    final = pd.DataFrame(0.0, index=cleaned_close.index, columns=cleaned_close.columns)
    selected: list[int] = []
    selected_rows: list[tuple[str, ...]] = []
    switch_rows: list[bool] = []
    for position, timestamp in enumerate(cleaned_close.index):
        switched = False
        if position >= policy.meta_lookback and (not selected or position % policy.meta_rebalance == 0):
            previous = tuple(selected)
            row = scores[position]
            valid = np.flatnonzero(np.isfinite(row))
            selected = ([int(item) for item in valid[np.argsort(-row[valid], kind="stable")][: policy.meta_count]] if valid.size else [])
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
        {"signal_turnover": turnover.astype(float), "meta_switch": switch_rows, "selected_templates": selected_rows},
        index=final.index,
    )
    return final, audit
