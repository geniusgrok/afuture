import numpy as np
import pytest

from afuture.recovery_risk import DrawdownRecoveryGovernor


def test_completed_loss_pauses_then_recovers_under_market_scale():
    governor = DrawdownRecoveryGovernor(0.8)
    assert governor.scale([]) == 0.8
    assert governor.scale([-0.1]) == 0
    assert governor.scale([-0.1] + [0] * 19) == 0
    assert governor.scale([-0.1] + [0] * 20) == 0.2
    assert governor.scale([-0.1] + [0] * 39) == 0.2
    assert governor.scale([-0.1] + [0] * 40) == 0.8


def test_recovered_segment_can_pause_again_without_resetting_account_truth():
    governor = DrawdownRecoveryGovernor(1)
    assert governor.scale([-0.1] + [0] * 20 + [-0.1]) == 0
    assert governor.scale([0.2, -0.08, -0.03]) == 0


@pytest.mark.parametrize("value", [np.nan, np.inf, -1, -2])
def test_invalid_account_observations_fail_closed(value):
    with pytest.raises(ValueError):
        DrawdownRecoveryGovernor(1).scale([value])
