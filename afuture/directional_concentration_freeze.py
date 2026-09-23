"""Offline acceptance adapter retaining causal Stress-90 HHI observations."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from math import isfinite

import pandas as pd

from .directional_drawdown_reserve_freeze import (
    FullPathDrawdownReserveFreezeDirectionalProductionAcceptance,
)
from .directional_stress90_policy import STRESS90_POLICY, advance_concentration_history
from .directional_stress90_policy import (
    target_weight_concentration as target_weight_concentration,
)


class ExpandingMedianConcentrationFreezeDirectionalProductionAcceptance(
    FullPathDrawdownReserveFreezeDirectionalProductionAcceptance
):
    """Track HHI without using it to veto the inherited account target."""

    def __init__(
        self,
        config=None,
        *,
        completed_concentrations: Iterable[float] = (),
    ) -> None:
        super().__init__(config)
        self._completed_concentrations = [float(value) for value in completed_concentrations]
        if any(
            not isfinite(value) or not 0.0 < value <= 1.0
            for value in self._completed_concentrations
        ):
            raise ValueError("completed target concentrations must be in (0, 1]")
        self.concentration_freeze_triggered = False

    def observe_target_state(
        self,
        *,
        day: pd.Timestamp | None,
        product_weights: Mapping[str, float],
    ) -> None:
        del day
        (
            _current,
            _prior_median,
            self.concentration_freeze_triggered,
            updated,
        ) = advance_concentration_history(
            self._completed_concentrations,
            product_weights,
        )
        self._completed_concentrations = list(updated)

    def _checkpoint_strategy_state(self) -> dict[str, object]:
        return {
            "policy_definition_digest": STRESS90_POLICY.policy_definition_digest,
            "completed_concentrations": tuple(self._completed_concentrations),
            "concentration_freeze_triggered": self.concentration_freeze_triggered,
        }

    def _restore_checkpoint_strategy_state(self, state: Mapping[str, object]) -> None:
        if (
            set(state)
            != {
                "policy_definition_digest",
                "completed_concentrations",
                "concentration_freeze_triggered",
            }
            or state["policy_definition_digest"] != STRESS90_POLICY.policy_definition_digest
        ):
            raise ValueError("checkpoint concentration state is invalid")
        raw_history = state["completed_concentrations"]
        if not isinstance(raw_history, (list, tuple)):
            raise ValueError("checkpoint concentration history is invalid")
        history = [float(value) for value in raw_history]
        if any(not isfinite(value) or not 0.0 < value <= 1.0 for value in history):
            raise ValueError("checkpoint concentration history is invalid")
        triggered = state["concentration_freeze_triggered"]
        if triggered is not False:
            raise ValueError("checkpoint concentration freeze state is invalid")
        self._completed_concentrations = history
        self.concentration_freeze_triggered = triggered
