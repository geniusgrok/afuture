from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from afuture.auto import AutoConfig, AutoPairManager
from afuture.broker.sim import SimBroker
from afuture.models import ContractInfo, ContractSpec, Tick
from afuture.quality import ExecutionQualityRecorder


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


def test_auto_selector_records_candidate_statistics_and_reject_reason(tmp_path: Path):
    cfg = AutoConfig(
        enabled=True,
        products=("m",),
        exchanges=("DCE",),
        max_active_pairs=1,
        max_contracts_per_product=2,
        min_days_to_expiry=10,
        scan_interval_seconds=0,
        lookback=4,
        entry_z=0.8,
        exit_z=0.2,
        stop_z=4,
        max_pair_volume=1,
        sample_seconds=0,
        min_volume=100,
        min_open_interest=100,
        min_liquidity_score=0.1,
        min_stationarity_score=0.8,
        max_half_life=1000,
        min_net_edge=0,
        slippage_ticks=0,
        session_windows=("09:00-15:00",),
    )
    specs = {
        "m2609": ContractSpec("m2609", "DCE", 10, 1, 0.1, 0.1),
        "m2701": ContractSpec("m2701", "DCE", 10, 1, 0.1, 0.1),
    }
    catalog = [
        ContractInfo("m2609", "DCE", "m", "2026-09-15"),
        ContractInfo("m2701", "DCE", "m", "2027-01-15"),
    ]
    broker = SimBroker(500000, specs, contract_catalog=catalog)
    broker.start()
    evidence = ExecutionQualityRecorder(tmp_path / "evidence.jsonl")
    manager = AutoPairManager(cfg, evidence_recorder=evidence)
    manager.bootstrap(broker, date(2026, 8, 21), {})
    base = datetime(2026, 8, 21, 9, tzinfo=timezone.utc)
    for minute, spread in enumerate([1.0, 1.4, 1.5, 1.55, 1.8]):
        manager.observe(tick("m2609", base + timedelta(minutes=minute), 3000 + spread))
        manager.observe(tick("m2701", base + timedelta(minutes=minute), 3000))
    manager.select(broker, now=base + timedelta(minutes=4), protected_pair_ids=set())
    manager.close()

    rows = [
        json.loads(line)
        for line in (tmp_path / "evidence.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    candidate = next(row for row in rows if row.get("event") == "candidate")
    for key in (
        "pair_id",
        "zscore",
        "stationarity",
        "half_life",
        "volume",
        "open_interest",
        "depth",
        "expected_net_edge",
        "candidate_score",
        "reject_reason",
    ):
        assert key in candidate
    # The evidence wire key remains compatible, while the human rejection reason
    # must not describe this heuristic as a stationarity test.
    assert candidate["reject_reason"] == "mean-reversion heuristic below minimum"
