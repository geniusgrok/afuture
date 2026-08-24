"""Research-only exact hard-feasible margin budget with freeze-new-risk defense.

This candidate changes only the normal soft sizing envelope from the baseline adaptive
30%-equivalent reserve to the exact hard-feasible account share:
``min(max_margin_ratio, 1 - min_available_ratio)``. With the frozen constraints this is
35%. The existing 35% hard margin gate, 25% available floor, 5% daily-loss gate, 30%
total-DD gate, 2x gross ceiling, 35-lot cap and freeze-new-risk trigger remain unchanged.
"""
from __future__ import annotations

from typing import Iterable

from .directional_freeze_new_risk import FreezeNewRiskDirectionalProductionAcceptance


class HardFeasibleMarginFreezeDirectionalProductionAcceptance(
    FreezeNewRiskDirectionalProductionAcceptance
):
    """Use the exact hard-feasible margin share while retaining freeze-only defense."""

    def margin_sizing_share(
        self, completed_returns: tuple[float, ...] = ()
    ) -> float:
        del completed_returns
        return min(
            float(self.config.max_margin_ratio),
            1.0 - float(self.config.min_available_ratio),
        )
