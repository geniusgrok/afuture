from pathlib import Path


def _api():
    from afuture.directional_drawdown_reserve_freeze import (
        DrawdownReserveFreezeDirectionalProductionAcceptance,
        drawdown_reserve_triggered,
    )

    return DrawdownReserveFreezeDirectionalProductionAcceptance, drawdown_reserve_triggered


def test_reserve_threshold_is_derived_from_hard_dd_minus_daily_loss():
    _, triggered = _api()
    assert triggered((0.10, -0.10, -0.10, -0.10), hard_drawdown=0.30, daily_loss=0.05) is True
    assert triggered((0.10, -0.05, -0.05), hard_drawdown=0.30, daily_loss=0.05) is False


def test_trigger_uses_compounded_completed_account_drawdown_not_single_day_loss():
    _, triggered = _api()
    # Two losses compound to a >25% drawdown even though neither is a 25% one-day loss.
    assert triggered((-0.14, -0.14), hard_drawdown=0.30, daily_loss=0.05) is True
    # A recovery to a fresh high resets completed drawdown.
    assert triggered((-0.14, -0.14, 0.40), hard_drawdown=0.30, daily_loss=0.05) is False


def test_no_history_does_not_trigger_soft_defense():
    _, triggered = _api()
    assert triggered((), hard_drawdown=0.30, daily_loss=0.05) is False


def test_full_path_adapter_does_not_change_default_two_return_buffer():
    from afuture.directional_acceptance import DirectionalProductionAcceptance
    from afuture.directional_drawdown_reserve_freeze import (
        DrawdownReserveFreezeDirectionalProductionAcceptance,
        FullPathDrawdownReserveFreezeDirectionalProductionAcceptance,
    )

    completed = [0.01, -0.02, 0.03, -0.04]

    assert DirectionalProductionAcceptance().retain_completed_returns(completed) == [
        0.03,
        -0.04,
    ]
    assert DrawdownReserveFreezeDirectionalProductionAcceptance().retain_completed_returns(
        completed
    ) == [0.03, -0.04]
    assert FullPathDrawdownReserveFreezeDirectionalProductionAcceptance().retain_completed_returns(
        completed
    ) == completed


def test_invalid_hard_limits_fail_closed():
    _, triggered = _api()
    for hard_dd, daily in ((0.0, 0.05), (0.30, 0.0), (0.05, 0.05), (0.04, 0.05)):
        try:
            triggered((0.0,), hard_drawdown=hard_dd, daily_loss=daily)
        except ValueError:
            pass
        else:
            raise AssertionError("expected invalid hard-limit rejection")


def test_research_adapter_is_not_live_wired():
    for path in (
        Path("afuture/runtime_factory.py"),
        Path("afuture/execution_aligned_runtime.py"),
        Path("afuture/directional_runtime.py"),
    ):
        if path.exists():
            assert "directional_drawdown_reserve_freeze" not in path.read_text(encoding="utf-8")
