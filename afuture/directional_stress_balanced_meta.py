"""Research-only 50/50 Base/Stress Meta candidate recorded before PR #17.

This module changes exactly one economic choice relative to the validated execution-
aligned policy: among templates whose completed Base and Stress trailing scores are both
finite/positive, rank by the equal average of Base and Stress scores instead of Base score
alone. The frozen 96-template pool, 11-session lookback, 3-session rebalance, top-3 count
and 2x gross ceiling are unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .execution_aligned_policy import (
    BASE_COST_BPS,
    MAX_ABS_DAILY_RETURN,
    MAX_GROSS_LEVERAGE,
    META_COUNT,
    META_LOOKBACK,
    META_REBALANCE,
    STRESS_COST_BPS,
    _EXECUTION_TEMPLATE_IDS,
    _EXECUTION_TEMPLATES,
    _clean_prices,
    _intraday_proxy_stream,
    _template_weight_path,
    _trailing_scores,
)

STRESS_BALANCED_SCORE_SOURCE = "continuous_intraday_equal_base_stress"


def combine_completed_base_stress_scores(
    base_scores: np.ndarray,
    stress_scores: np.ndarray,
) -> np.ndarray:
    base = np.asarray(base_scores, dtype=float)
    stress = np.asarray(stress_scores, dtype=float)
    if base.shape != stress.shape:
        raise ValueError("Base and Stress score matrices must have identical shape")
    valid = np.isfinite(base) & np.isfinite(stress)
    result = np.full_like(base, np.nan, dtype=float)
    result[valid] = 0.5 * base[valid] + 0.5 * stress[valid]
    return result


@dataclass(frozen=True)
class StressBalancedMetaPolicy:
    products: tuple[str, ...]
    meta_lookback: int = META_LOOKBACK
    meta_rebalance: int = META_REBALANCE
    meta_count: int = META_COUNT
    score_source: str = STRESS_BALANCED_SCORE_SOURCE
    template_ids: tuple[str, ...] = _EXECUTION_TEMPLATE_IDS

    def __post_init__(self) -> None:
        if not self.products:
            raise ValueError("stress-balanced policy products cannot be empty")
        if self.template_ids != _EXECUTION_TEMPLATE_IDS:
            raise ValueError("stress-balanced template pool must remain frozen")
        if (
            self.meta_lookback != META_LOOKBACK
            or self.meta_rebalance != META_REBALANCE
            or self.meta_count != META_COUNT
            or self.score_source != STRESS_BALANCED_SCORE_SOURCE
        ):
            raise ValueError("stress-balanced Meta parameters are frozen")

    def weight_history(
        self,
        open_prices: pd.DataFrame,
        close: pd.DataFrame,
    ) -> pd.DataFrame:
        close = _clean_prices(close, self.products)
        open_prices = _clean_prices(open_prices, self.products).reindex(close.index)
        returns = close.pct_change(fill_method=None)
        returns = returns.mask(returns.abs() > MAX_ABS_DAILY_RETURN)

        base_streams: dict[str, pd.Series] = {}
        stress_streams: dict[str, pd.Series] = {}
        paths: dict[str, pd.DataFrame] = {}
        for template_id, template in zip(self.template_ids, _EXECUTION_TEMPLATES):
            weights = _template_weight_path(returns, template)
            paths[template_id] = weights
            base_streams[template_id] = _intraday_proxy_stream(
                open_prices,
                close,
                weights,
                cost_bps=BASE_COST_BPS,
            )
            stress_streams[template_id] = _intraday_proxy_stream(
                open_prices,
                close,
                weights,
                cost_bps=STRESS_COST_BPS,
            )

        base_frame = pd.DataFrame(base_streams).sort_index().fillna(0.0)
        stress_frame = pd.DataFrame(stress_streams).reindex(
            index=base_frame.index,
            columns=base_frame.columns,
        ).fillna(0.0)
        scores = combine_completed_base_stress_scores(
            _trailing_scores(base_frame, self.meta_lookback),
            _trailing_scores(stress_frame, self.meta_lookback),
        )
        names = list(base_frame.columns)
        final = pd.DataFrame(0.0, index=close.index, columns=close.columns)
        selected: list[int] = []

        for position, timestamp in enumerate(close.index):
            if position >= self.meta_lookback and (
                not selected or position % self.meta_rebalance == 0
            ):
                row = scores[position]
                valid = np.flatnonzero(np.isfinite(row))
                selected = (
                    [
                        int(item)
                        for item in valid[
                            np.argsort(-row[valid], kind="stable")
                        ][: self.meta_count]
                    ]
                    if valid.size
                    else []
                )
            if selected:
                rows = [paths[names[item]].loc[timestamp] for item in selected]
                final.loc[timestamp] = pd.concat(rows, axis=1).mean(axis=1)

        gross = final.abs().sum(axis=1)
        if bool((gross > MAX_GROSS_LEVERAGE + 1e-10).any()):
            raise AssertionError("stress-balanced Meta exceeded 2x gross")
        return final

    def target_weights(
        self,
        open_prices: pd.DataFrame,
        close: pd.DataFrame,
    ) -> dict[str, float]:
        history = self.weight_history(open_prices, close)
        if history.empty:
            return {}
        latest = history.iloc[-1]
        return {
            str(product): float(weight)
            for product, weight in latest.items()
            if abs(float(weight)) > 1e-15
        }
