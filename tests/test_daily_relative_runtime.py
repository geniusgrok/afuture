from datetime import date, datetime, timezone
from pathlib import Path

from afuture.auto import AutoConfig, AutoPairManager
from afuture.models import ContractInfo, Tick
from afuture.sample_store import MarketSampleStore


def tick(symbol: str, when: datetime, mid: float) -> Tick:
    return Tick(
        symbol=symbol,
        exchange="DCE",
        timestamp=when,
        bid_price=mid - 0.5,
        ask_price=mid + 0.5,
        last_price=mid,
        bid_volume=100,
        ask_volume=100,
        trading_day="20260821",
        volume=20000,
        open_interest=80000,
    )


def test_daily_auto_history_survives_restart_through_bounded_sample_store(
    tmp_path: Path,
):
    config = AutoConfig(
        enabled=True,
        products=("m",),
        exchanges=("DCE",),
        lookback=4,
        daily_sample_window="14:55-15:00",
    )
    catalog = [
        ContractInfo("m2609", "DCE", "m", "2026-09-15"),
        ContractInfo("m2701", "DCE", "m", "2027-01-15"),
    ]
    store = MarketSampleStore(tmp_path / "samples", max_samples=24)
    manager = AutoPairManager(config, sample_store=store)
    manager.prepare_catalog(catalog, date(2026, 8, 21))
    manager.observe(tick("m2609", datetime(2026, 8, 21, 6, 56, tzinfo=timezone.utc), 3100))
    manager.observe(tick("m2701", datetime(2026, 8, 21, 6, 56, tzinfo=timezone.utc), 3000))
    manager.observe(tick("m2609", datetime(2026, 8, 21, 6, 59, tzinfo=timezone.utc), 3110))
    manager.close()

    restored = AutoPairManager(config, sample_store=store)
    try:
        restored.prepare_catalog(catalog, date(2026, 8, 21))
        snapshot = restored.snapshot_history()
        assert snapshot["m2609"][-1]["last_price"] == 3110
        assert snapshot["m2701"][-1]["last_price"] == 3000
    finally:
        restored.close()
