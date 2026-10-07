"""Completed-account-return observer for research target pause and recovery."""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np


class DrawdownRecoveryGovernor:
    """Scale future targets; never own fills, cash or an account high-water mark.

    A10% decline in the observer's current risk segment pauses targets for20
    subsequent sessions, then permits25% targets for20 probation sessions. The
    segment anchor resets at recovery, while the authoritative account's lifetime
    drawdown, solvency and margin rules continue independently. The observer reads
    completed account returns only, including flat returns while paused.
    """

    def __init__(self, market_scale: float):
        self.market_scale = market_scale

    def scale(self, completed_returns: Iterable[float]) -> float:
        values = np.array(list(completed_returns), dtype=float)
        if not np.isfinite(values).all() or (values <= -1).any():
            raise ValueError("recovery requires valid completed account returns")
        if not np.isfinite(self.market_scale) or not 0 <= self.market_scale <= 1:
            raise ValueError("market scale must be in [0,1]")
        wealth = peak = 1.0
        pause, probation = 0, 0
        for value in values:
            wealth *= 1.0 + value
            if not np.isfinite(wealth) or wealth <= 0:
                raise ValueError("invalid compounded account observation")
            if pause:
                pause -= 1
                if pause == 0:
                    probation = 20
                    peak = wealth
                continue
            peak = max(peak, wealth)
            if 1.0 - wealth / peak >= 0.10 - 1e-12:
                pause, probation = 20, 0
            elif probation:
                probation -= 1
        return self.market_scale * (0.0 if pause else 0.25 if probation else 1.0)
