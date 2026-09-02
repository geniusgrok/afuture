from datetime import date, datetime, timezone
from types import SimpleNamespace

from afuture.directional import DirectionalConfig
from afuture.execution_aligned_runtime import ExecutionAlignedDirectionalPortfolioManager
from afuture.risk import RiskConfig, RiskManager

NIGHT_SESSION = datetime(2026, 8, 24, 13, 1, tzinfo=timezone.utc)  # 21:01 Asia/Shanghai


def _manager(current_trading_day: str):
    return ExecutionAlignedDirectionalPortfolioManager(
        DirectionalConfig(
            enabled=True,
            policy="execution_aligned",
            products=("A",),
            exchanges=("DCE",),
        ),
        broker=object(),
        risk_manager=RiskManager(RiskConfig()),
        signal_provider=object(),
        policy=object(),
        activity_tracker=SimpleNamespace(current_trading_day=current_trading_day),
    )


def test_night_session_planned_date_prefers_ctp_trading_day_over_calendar_date():
    manager = _manager("20260825")
    assert manager._planned_trading_date(NIGHT_SESSION) == date(2026, 8, 25)


def test_planned_date_falls_back_to_china_calendar_date_without_ctp_day():
    manager = _manager("")
    assert manager._planned_trading_date(NIGHT_SESSION) == date(2026, 8, 24)
