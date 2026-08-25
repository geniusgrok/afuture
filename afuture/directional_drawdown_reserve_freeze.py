"""Research-only soft defense triggered by the hard drawdown reserve boundary.

The soft trigger is derived exclusively from existing hard limits:

    reserve_drawdown = max_total_drawdown_ratio - max_daily_loss_ratio

With the validated 30% total-drawdown and 5% daily-loss limits this is 25%. Completed
account returns are compounded from a 1.0 starting wealth with a running high-watermark.
Once completed drawdown reaches the reserve boundary, the existing strict freeze-new-risk
response blocks new entries and same-sign increases while reductions, exits, reversals and
same-product rolls remain executable.

No threshold is fit from backtest results. The 5% daily-loss, 30% hard drawdown, margin,
available-cash, 2x gross and 35-lot gates remain authoritative downstream. This module is
research-only and is not imported by live runtime wiring.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np

from .directional_freeze_new_risk import FreezeNewRiskDirectionalProductionAcceptance

_EPS = 1e-12


def drawdown_reserve_triggered(
    completed_returns: Iterable[float],
    *,
    hard_drawdown: float,
    daily_loss: float,
) -> bool:
    """Trigger when compounded completed drawdown consumes the hard-DD daily-loss reserve."""
    hard = float(hard_drawdown)
    daily = float(daily_loss)
    if not np.isfinite(hard) or not np.isfinite(daily):
        raise ValueError("hard drawdown and daily loss must be finite")
    if not 0.0 < daily < hard < 1.0:
        raise ValueError("hard drawdown must exceed a positive daily-loss reserve")
    reserve = hard - daily
    if reserve <= _EPS:
        raise ValueError("drawdown reserve must be positive")

    values = tuple(float(value) for value in completed_returns)
    if not values:
        return False
    if any(not np.isfinite(value) or value <= -1.0 for value in values):
        raise ValueError("completed returns must be finite and greater than -100%")

    wealth = 1.0
    high_watermark = 1.0
    for value in values:
        wealth *= 1.0 + value
        high_watermark = max(high_watermark, wealth)
    drawdown = wealth / high_watermark - 1.0
    return bool(drawdown <= -reserve + _EPS)


class DrawdownReserveFreezeDirectionalProductionAcceptance(
    FreezeNewRiskDirectionalProductionAcceptance
):
    """Strict freeze-new-risk response driven only by the derived drawdown reserve."""

    def freeze_triggered(self, completed_returns: Iterable[float]) -> bool:
        return drawdown_reserve_triggered(
            completed_returns,
            hard_drawdown=float(self.config.max_total_drawdown_ratio),
            daily_loss=float(self.config.max_daily_loss_ratio),
        )


class FullPathDrawdownReserveFreezeDirectionalProductionAcceptance(
    DrawdownReserveFreezeDirectionalProductionAcceptance
):
    """Reserve adapter retaining the full causal account return path."""

    def retain_completed_returns(self, completed_returns: list[float]) -> list[float]:
        """Preserve the causal account path required by running-high-watermark DD."""
        return list(completed_returns)
