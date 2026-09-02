"""Research-only rejected completed product Alpha-efficiency selector.

It reproduces Candidate B's cheap Float and fixed Production screens for offline evidence.
Candidate B failed the predeclared Production economics gates, so this module is not
reachable from ``runtime_factory``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .directional_alpha_efficiency import (
    apply_alpha_efficiency_overlay,
    build_product_alpha_efficiency_score,
)
from .execution_aligned_policy import (
    MAX_GROSS_LEVERAGE,
    ExecutionAlignedAggressivePolicy,
)


@dataclass(frozen=True)
class AlphaEfficiencyDirectionalPolicy:
    """Reproduce the rejected completed product Alpha-efficiency overlay for research."""

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
    ) -> pd.DataFrame:
        raw = self.core.weight_history(open_prices, close)
        score = build_product_alpha_efficiency_score(
            open_prices,
            close,
            raw,
        )
        adjusted = apply_alpha_efficiency_overlay(raw, score)
        if bool((adjusted.abs().sum(axis=1) > MAX_GROSS_LEVERAGE + 1e-10).any()):
            raise AssertionError("alpha-efficiency policy exceeded 2x gross")
        return adjusted

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
