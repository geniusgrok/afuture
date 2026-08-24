"""Research-only rejected generic opportunity overlay for directional selection.

This module is retained only to reproduce Candidate A evidence. ``runtime_factory`` never
wires this policy into live/Shadow production after its fixed Production L3 rejection.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .directional_opportunity import apply_opportunity_overlay, build_opportunity_score
from .execution_aligned_policy import (
    MAX_GROSS_LEVERAGE,
    ExecutionAlignedAggressivePolicy,
)


@dataclass(frozen=True)
class OpportunityAlignedAggressivePolicy:
    """Reproduce the rejected lower-opportunity de-emphasis candidate for research."""

    products: tuple[str, ...]
    core: ExecutionAlignedAggressivePolicy = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "core",
            ExecutionAlignedAggressivePolicy(products=self.products),
        )

    @property
    def meta_lookback(self) -> int:
        return self.core.meta_lookback

    @property
    def meta_rebalance(self) -> int:
        return self.core.meta_rebalance

    @property
    def meta_count(self) -> int:
        return self.core.meta_count

    @property
    def meta_score_source(self) -> str:
        return self.core.meta_score_source

    @property
    def template_ids(self) -> tuple[str, ...]:
        return self.core.template_ids

    def weight_history(
        self,
        open_prices: pd.DataFrame,
        close: pd.DataFrame,
        *,
        volume: pd.DataFrame | None = None,
        open_interest: pd.DataFrame | None = None,
    ) -> pd.DataFrame:
        raw = self.core.weight_history(open_prices, close)
        score = build_opportunity_score(close, volume, open_interest)
        adjusted = apply_opportunity_overlay(raw, score)
        gross = adjusted.abs().sum(axis=1)
        if bool((gross > MAX_GROSS_LEVERAGE + 1e-10).any()):
            raise AssertionError("opportunity policy exceeded 2x gross")
        return adjusted

    def target_weights(
        self,
        open_prices: pd.DataFrame,
        close: pd.DataFrame,
        *,
        volume: pd.DataFrame | None = None,
        open_interest: pd.DataFrame | None = None,
    ) -> dict[str, float]:
        history = self.weight_history(
            open_prices,
            close,
            volume=volume,
            open_interest=open_interest,
        )
        if history.empty:
            return {}
        latest = history.iloc[-1]
        return {
            str(product): float(weight)
            for product, weight in latest.items()
            if abs(float(weight)) > 1e-15
        }
