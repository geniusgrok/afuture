import importlib.util
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from afuture.directional_acceptance import ProductionMechanicsConfig
from afuture.directional_concentration_freeze import (
    ExpandingMedianConcentrationFreezeDirectionalProductionAcceptance,
)
from afuture.supply_demand import SupplyObservation, specific_carry_pairs, warrant_signal

COLLECTOR_SPEC = importlib.util.spec_from_file_location(
    "collect_supply_demand", Path(__file__).resolve().parents[1] / "tools/collect_supply_demand.py"
)
assert COLLECTOR_SPEC is not None and COLLECTOR_SPEC.loader is not None
collector = importlib.util.module_from_spec(COLLECTOR_SPEC)
COLLECTOR_SPEC.loader.exec_module(collector)
capture = collector.capture


def observations():
    days = [date(y, 6, 1) + timedelta(days=7 * i) for y in (2022, 2023) for i in range(4)]
    days += [date(2024, 5, 31), date(2024, 6, 28)]
    return tuple(
        SupplyObservation(
            "RB",
            day,
            100.0 if day != days[-1] else 50.0,
            datetime.combine(day, datetime.min.time(), timezone.utc),
            "a" * 64,
            str(day),
        )
        for day in days
    )


def signal(rows, **changes):
    kwargs = dict(
        product="RB",
        completed_day=date(2024, 6, 28),
        decision_at=datetime(2024, 6, 28, 7, tzinfo=timezone.utc),
        carry=0.05,
    )
    kwargs.update(changes)
    return warrant_signal(rows, **kwargs)


def test_versions_season_and_staleness_are_causal():
    rows = observations()
    assert signal(rows) == (1.0, "scarcity_confirmed")
    later = replace(rows[-1], value=150.0, available_at=datetime(2024, 6, 29, tzinfo=timezone.utc))
    assert signal(rows + (later,)) == signal(rows)
    assert signal(rows + (later,), decision_at=later.available_at)[0] == 0
    invalid = replace(later, qualified=False)
    assert (
        signal(rows + (invalid,), decision_at=later.available_at)[1] == "untrusted_latest_version"
    )
    assert signal(rows, completed_day=date(2024, 7, 9))[1] == "stale"
    assert signal(rows, carry=-0.05)[0] == 0
    assert signal(rows, carry=float("nan"))[1] == "no_actual_term_structure"
    assert signal(tuple(r for r in rows if r.statistical_day.year != 2022))[1] == "seasonal_warmup"
    future_year = tuple(
        replace(r, statistical_day=r.statistical_day.replace(year=2025)) for r in rows[:4]
    )
    assert signal(rows[4:] + future_year)[1] == "seasonal_warmup"
    with pytest.raises(ValueError, match="simultaneous"):
        signal(rows + (replace(rows[-1], value=999.0),))
    with pytest.raises(ValueError, match="timezone"):
        signal(rows, decision_at=datetime(2024, 6, 28))
    with pytest.raises(ValueError, match="invalid supply"):
        replace(rows[-1], value=-1)


def test_actual_pairs_and_missing_signal_close_integer_positions_with_cost():
    dates = pd.date_range("2024-01-02", periods=3, freq="D")
    raw = pd.DataFrame(
        [
            dict(
                date=day,
                symbol=symbol,
                product="RB",
                exchange="SHFE",
                open=price,
                high=price + 2,
                low=price - 2,
                close=price,
                settle=price,
                volume=10000,
                hold=20000,
                delivery=pd.Timestamp(delivery),
            )
            for day in dates
            for symbol, price, delivery in [
                ("RB2405", 3112.0, "2024-05-15"),
                ("RB2410", 3120.0, "2024-10-15"),
            ]
        ]
    )
    pairs = specific_carry_pairs(raw)
    assert len(pairs) == 3 and pairs.symbol_near.eq("RB2405").all()
    assert pairs.annualized_carry.lt(0).all()
    assert specific_carry_pairs(raw.loc[raw.symbol.eq("RB2405")]).empty
    account = ExpandingMedianConcentrationFreezeDirectionalProductionAcceptance(
        ProductionMechanicsConfig(initial_capital=500000.0)
    )
    weights = pd.DataFrame({"RB": [0.2, 0.0]}, index=dates[1:])
    result = account.simulate(raw, weights, cost_bps=5.0)
    trades = result.events.loc[result.events.kind.eq("trade")]
    assert trades.delta_lots.abs().eq(3).all()
    assert len(trades) == 2 and trades.iloc[-1].lots_after == 0
    assert result.daily.equity.iloc[-1] == pytest.approx(500000 - 2 * 3 * 3112 * 10 * 5 / 10000)


def test_capture_keeps_failed_original_without_backdating_availability(tmp_path, monkeypatch):
    import hashlib
    import io
    import urllib.error
    import urllib.request

    raw = b"historical source not found"
    url = "https://example.org/20220101.dat"
    error = urllib.error.HTTPError(
        url, 404, "missing", {"Last-Modified": "Sat, 01 Jan 2022 00:00:00 GMT"}, io.BytesIO(raw)
    )

    def unavailable(*args, **kwargs):
        raise error

    monkeypatch.setattr(urllib.request, "urlopen", unavailable)
    (tmp_path / "originals").mkdir()
    result = capture({"id": "old", "url": url}, tmp_path, 1.0)
    assert result["http_status"] == 404
    assert (tmp_path / result["file"]).read_bytes() == raw
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["historical_availability_certified"] is False
    assert datetime.fromisoformat(result["observed_at"]).tzinfo is not None
    with pytest.raises(ValueError, match="safe basename"):
        capture({"id": "../escape", "url": url}, tmp_path, 1.0)
