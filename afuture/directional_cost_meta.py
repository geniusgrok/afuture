"""Research-only transaction-cost-aware META candidate for directional weights.

This module deliberately does not modify the production policy. It reuses the frozen
96-template paths and robust META score, then asks whether a scheduled template rotation
has positive expected net benefit after its implied product-level transition cost.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .execution_aligned_policy import (
    BASE_COST_BPS,
    MAX_ABS_DAILY_RETURN,
    MAX_GROSS_LEVERAGE,
    META_REBALANCE,
    STRESS_COST_BPS,
    _EXECUTION_TEMPLATES,
    _clean_prices,
    _intraday_proxy_stream,
    _robust_trailing_scores,
    _template_weight_path,
)


def net_switch_benefit(
    *,
    incumbent_expected_daily: float,
    candidate_expected_daily: float,
    transition_turnover: float,
    horizon_days: int,
    cost_bps: float,
) -> float:
    """Return expected horizon alpha improvement minus one-way transition cost."""
    horizon = max(1, int(horizon_days))
    turnover = max(0.0, float(transition_turnover))
    cost = turnover * max(0.0, float(cost_bps)) / 10000.0
    alpha = (
        float(candidate_expected_daily) - float(incumbent_expected_daily)
    ) * horizon
    return float(alpha - cost)


def _aggregate(
    *,
    indices: list[int],
    names: list[str],
    paths: dict[str, pd.DataFrame],
    timestamp,
) -> dict[str, float]:
    if not indices:
        return {}
    rows = [paths[names[item]].loc[timestamp] for item in indices]
    series = pd.concat(rows, axis=1).mean(axis=1)
    return {
        str(product): float(value)
        for product, value in series.items()
        if abs(float(value)) > 1e-15
    }


def _transition_turnover(
    incumbent: dict[str, float], candidate: dict[str, float]
) -> float:
    products = set(incumbent) | set(candidate)
    return float(
        sum(
            abs(float(candidate.get(product, 0.0)) - float(incumbent.get(product, 0.0)))
            for product in products
        )
    )


def _expected_daily(
    frame: pd.DataFrame,
    indices: list[int],
    *,
    position: int,
    lookback: int,
) -> float:
    if not indices or position <= 0:
        return 0.0
    start = max(0, position - max(1, int(lookback)))
    history = frame.iloc[start:position, indices]
    if history.empty:
        return 0.0
    return float(history.mean(axis=0).mean())


def cost_aware_weight_history(
    policy,
    open_prices: pd.DataFrame,
    close: pd.DataFrame,
    *,
    transition_cost_bps: float = STRESS_COST_BPS,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return frozen-policy weights with a causal NetSwitchBenefit META veto and audit."""
    close = _clean_prices(close, policy.products)
    open_prices = _clean_prices(open_prices, policy.products).reindex(close.index)
    returns = close.pct_change(fill_method=None)
    returns = returns.mask(returns.abs() > MAX_ABS_DAILY_RETURN)

    base_streams: dict[str, pd.Series] = {}
    stress_streams: dict[str, pd.Series] = {}
    paths: dict[str, pd.DataFrame] = {}
    for template_id, template in zip(policy.template_ids, _EXECUTION_TEMPLATES):
        weights = _template_weight_path(returns, template)
        paths[template_id] = weights
        base_streams[template_id] = _intraday_proxy_stream(
            open_prices, close, weights, cost_bps=BASE_COST_BPS
        )
        stress_streams[template_id] = _intraday_proxy_stream(
            open_prices, close, weights, cost_bps=STRESS_COST_BPS
        )

    base_frame = pd.DataFrame(base_streams).sort_index().fillna(0.0)
    stress_frame = pd.DataFrame(stress_streams).reindex(
        index=base_frame.index, columns=base_frame.columns
    ).fillna(0.0)
    scores = _robust_trailing_scores(
        base_frame, stress_frame, lookback=policy.meta_lookback
    )
    names = list(base_frame.columns)
    final = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    selected: list[int] = []
    audit_rows: list[dict] = []

    for position, timestamp in enumerate(close.index):
        scheduled = position >= policy.meta_lookback and (
            not selected or position % policy.meta_rebalance == 0
        )
        if scheduled:
            row = scores[position]
            valid = np.flatnonzero(np.isfinite(row))
            candidate = (
                [
                    int(item)
                    for item in valid[
                        np.argsort(-row[valid], kind="stable")[: policy.meta_count]
                    ]
                ]
                if valid.size
                else []
            )
            incumbent_before = list(selected)
            incumbent_target = _aggregate(
                indices=incumbent_before,
                names=names,
                paths=paths,
                timestamp=timestamp,
            )
            candidate_target = _aggregate(
                indices=candidate,
                names=names,
                paths=paths,
                timestamp=timestamp,
            )
            turnover = _transition_turnover(incumbent_target, candidate_target)
            incumbent_expected = _expected_daily(
                base_frame,
                incumbent_before,
                position=position,
                lookback=policy.meta_lookback,
            )
            candidate_expected = _expected_daily(
                base_frame,
                candidate,
                position=position,
                lookback=policy.meta_lookback,
            )
            expected_alpha_improvement = (
                candidate_expected - incumbent_expected
            ) * max(1, int(policy.meta_rebalance))
            expected_transition_cost = (
                turnover * max(0.0, float(transition_cost_bps)) / 10000.0
            )
            benefit = net_switch_benefit(
                incumbent_expected_daily=incumbent_expected,
                candidate_expected_daily=candidate_expected,
                transition_turnover=turnover,
                horizon_days=policy.meta_rebalance,
                cost_bps=transition_cost_bps,
            )

            # Empty candidate is a contraction to flat and must not be held back by cost.
            # The zero-cost endpoint intentionally reproduces legacy switching exactly.
            if not incumbent_before:
                should_switch = candidate != incumbent_before
            elif not candidate:
                should_switch = candidate != incumbent_before
            elif float(transition_cost_bps) <= 0.0:
                should_switch = candidate != incumbent_before
            else:
                should_switch = candidate != incumbent_before and benefit > 0.0
            if should_switch:
                selected = candidate

            selected_target = _aggregate(
                indices=selected,
                names=names,
                paths=paths,
                timestamp=timestamp,
            )
            audit_rows.append(
                {
                    "timestamp": pd.Timestamp(timestamp),
                    "incumbent_templates": tuple(names[item] for item in incumbent_before),
                    "candidate_templates": tuple(names[item] for item in candidate),
                    "selected_templates": tuple(names[item] for item in selected),
                    "candidate_turnover": float(turnover),
                    "expected_alpha_improvement": float(expected_alpha_improvement),
                    "expected_transition_cost": float(expected_transition_cost),
                    "net_switch_benefit": float(benefit),
                    "switched": bool(should_switch),
                    "candidate_product_targets": candidate_target,
                    "selected_product_targets": selected_target,
                }
            )

        raw = _aggregate(
            indices=selected,
            names=names,
            paths=paths,
            timestamp=timestamp,
        )
        if raw:
            final.loc[timestamp] = pd.Series(raw).reindex(final.columns).fillna(0.0)

    gross = final.abs().sum(axis=1)
    if bool((gross > MAX_GROSS_LEVERAGE + 1e-10).any()):
        raise AssertionError("cost-aware META candidate exceeded 2x gross")
    audit = pd.DataFrame(audit_rows)
    return final, audit


def cost_aware_no_trade_weights(
    target_weights: pd.DataFrame,
    completed_returns: pd.DataFrame,
    *,
    lookback: int = 20,
    horizon_days: int = META_REBALANCE,
    cost_bps: float = STRESS_COST_BPS,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Research proxy for the lot-level no-trade region using completed product returns.

    The rule can only suppress a new opening or a same-sign absolute increase. Any
    reduction, exit, or reversal preserves the requested target. Expected edge uses only
    rows strictly before the decision timestamp, so future/current returns cannot enter.
    """
    target = target_weights.copy().astype(float).sort_index().fillna(0.0)
    target.columns = [str(column).upper() for column in target.columns]
    returns = completed_returns.copy().astype(float).sort_index()
    returns.columns = [str(column).upper() for column in returns.columns]
    returns = returns.reindex(index=target.index, columns=target.columns)
    lookback = max(1, int(lookback))
    horizon = max(1, int(horizon_days))
    cost_rate = max(0.0, float(cost_bps)) / 10000.0

    final = pd.DataFrame(0.0, index=target.index, columns=target.columns)
    incumbent = pd.Series(0.0, index=target.columns, dtype=float)
    audit_rows: list[dict] = []

    for position, timestamp in enumerate(target.index):
        requested = target.iloc[position].copy()
        selected = requested.copy()
        start = max(0, position - lookback)
        history = returns.iloc[start:position]
        expected = history.mean(axis=0, skipna=True) if not history.empty else pd.Series(
            np.nan, index=target.columns, dtype=float
        )

        for product in target.columns:
            current = float(incumbent[product])
            wanted = float(requested[product])
            if abs(wanted - current) <= 1e-15:
                continue
            # Exits, absolute reductions, and reversals always bypass the no-trade region.
            if abs(wanted) <= 1e-15:
                continue
            if abs(current) > 1e-15:
                if (current > 0) != (wanted > 0):
                    continue
                if abs(wanted) <= abs(current) + 1e-15:
                    continue

            delta = max(0.0, abs(wanted) - abs(current))
            raw_expected = float(expected.get(product, np.nan))
            directional_edge = (
                (1.0 if wanted > 0 else -1.0) * raw_expected
                if np.isfinite(raw_expected)
                else float("nan")
            )
            expected_benefit = (
                delta * directional_edge * horizon
                if np.isfinite(directional_edge)
                else float("nan")
            )
            transition_cost = delta * cost_rate
            suppress = (
                not np.isfinite(expected_benefit)
                or expected_benefit <= transition_cost + 1e-15
            )
            if suppress:
                selected[product] = current
            audit_rows.append(
                {
                    "timestamp": pd.Timestamp(timestamp),
                    "product": product,
                    "incumbent_weight": current,
                    "requested_weight": wanted,
                    "selected_weight": float(selected[product]),
                    "expected_daily_return": raw_expected,
                    "expected_benefit": float(expected_benefit),
                    "expected_transition_cost": float(transition_cost),
                    "suppressed": bool(suppress),
                }
            )

        gross_requested = float(requested.abs().sum())
        gross_selected = float(selected.abs().sum())
        if gross_selected > gross_requested + 1e-10:
            raise AssertionError("no-trade proxy increased requested gross")
        final.loc[timestamp] = selected
        incumbent = selected

    return final, pd.DataFrame(audit_rows)
