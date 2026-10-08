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
from afuture.supply_demand_cost import SupplyEpisode, episode_cost_gate, net_supply_with_baseline

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


def test_execution_freshness_uses_chinese_calendar_and_preserves_old_spec():
    rows = observations()
    kwargs = dict(
        completed_day=date(2024, 7, 8), decision_at=datetime(2024, 7, 8, 7, tzinfo=timezone.utc)
    )
    assert signal(rows, **kwargs)[0] == 1
    assert signal(rows, **kwargs, execution_at=datetime(2024, 7, 8, 16, tzinfo=timezone.utc))[
        1
    ] == ("stale_at_execution")
    with pytest.raises(ValueError, match="later than decision"):
        signal(rows, execution_at=datetime(2024, 6, 28, 7, tzinfo=timezone.utc))


def test_episode_estimator_uses_closed_prior_processes_and_complete_roll_costs():
    episodes = tuple(
        SupplyEpisode(
            product="RB",
            direction=1,
            entered_at=datetime(2024, 1, i, 1, tzinfo=timezone.utc),
            completed_at=datetime(2024, 1, i + 1, 1, tzinfo=timezone.utc),
            known_at=datetime(2024, 1, i + 1, 7, tzinfo=timezone.utc),
            entry_notional=10000,
            gross_pnl=40,
            turnover_notional=20000,
            episode_id=str(i),
        )
        for i in (1, 3, 5, 7)
    )
    kwargs = dict(targets={"RB": 0.2}, prior_approved={"RB": 0}, episodes=episodes)
    assert episode_cost_gate(**kwargs, decision_at=episodes[-1].known_at)[0] == {"RB": 0.2}
    before = episodes[-1].known_at - timedelta(seconds=1)
    output, audit = episode_cost_gate(**kwargs, decision_at=before)
    assert output == {"RB": 0} and audit[0]["completed_samples"] == 3
    rolled = tuple(replace(e, turnover_notional=40000) for e in episodes)
    assert episode_cost_gate(**dict(kwargs, episodes=rolled), decision_at=episodes[-1].known_at)[
        0
    ] == {"RB": 0}
    assert episode_cost_gate(
        targets={"RB": 0.1}, prior_approved={"RB": 0.2}, episodes=(), decision_at=before
    )[0] == {"RB": 0.1}
    assert episode_cost_gate(
        targets={"RB": -0.2}, prior_approved={"RB": 0.2}, episodes=(), decision_at=before
    )[0] == {"RB": 0}
    with pytest.raises(ValueError, match="duplicate"):
        episode_cost_gate(**dict(kwargs, episodes=episodes + episodes[:1]), decision_at=before)


def test_shared_targets_cancel_before_rounding_and_use_one_account():
    spec = importlib.util.spec_from_file_location(
        "supply_demand_episodes",
        Path(__file__).resolve().parents[1] / "tools/supply_demand_episodes.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    build_supply_episodes = module.build_supply_episodes

    baseline = {"RB": 1.8, "AL": 0.2}
    assert net_supply_with_baseline(baseline, {"RB": -0.2, "AL": 0.2}) == {
        "RB": 1.6,
        "AL": pytest.approx(0.4),
    }
    assert net_supply_with_baseline(baseline, {"RB": 0.2, "AL": 0.2}) == baseline
    assert net_supply_with_baseline(baseline, {"RB": 0, "AL": 0}) == baseline
    with pytest.raises(ValueError, match="support"):
        net_supply_with_baseline(baseline, {"RB": 0})
    dates = pd.date_range("2024-01-02", periods=4)
    raw = pd.DataFrame(
        [
            dict(
                date=day,
                symbol="RB2405",
                product="RB",
                exchange="SHFE",
                open=3100.0,
                close=3102.0,
                volume=10000,
                hold=20000,
                delivery=pd.Timestamp("2024-05-15"),
            )
            for day in dates
        ]
    )
    weights = pd.DataFrame({"RB": [0.2, 0.2, 0]}, index=dates[1:])
    episodes, rejected = build_supply_episodes(raw, weights)
    assert not rejected and len(episodes) == 1
    assert episodes[0].gross_pnl == pytest.approx(0)
    assert episodes[0].turnover_notional == pytest.approx(62000)
    # Net opposite requests first: 0.2 - 0.1 fits one lot, not rounded3 - rounded1.
    combined = net_supply_with_baseline({"RB": 0.2}, {"RB": -0.1})
    unified = pd.DataFrame([combined, {"RB": 0}], index=dates[1:3])
    result = ExpandingMedianConcentrationFreezeDirectionalProductionAcceptance().simulate(
        raw,
        unified,
        cost_bps=15,
    )
    trades = result.events.loc[result.events.kind.eq("trade")]
    assert trades.delta_lots.abs().eq(1).all()
    assert result.daily.equity.iloc[-1] == pytest.approx(500000 - 2 * 31000 * 15 / 10000)


def test_paper_episode_counts_both_roll_legs_and_rejects_missing_mark():
    spec = importlib.util.spec_from_file_location(
        "supply_demand_episodes",
        Path(__file__).resolve().parents[1] / "tools/supply_demand_episodes.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    dates = pd.date_range("2024-01-02", periods=4)
    raw = pd.DataFrame(
        [
            dict(
                date=day,
                symbol=symbol,
                product="RB",
                exchange="SHFE",
                open=op,
                close=cl,
                hold=hold,
                volume=volume,
                delivery=pd.Timestamp(delivery),
            )
            for i, day in enumerate(dates)
            for symbol, delivery, op, cl, hold, volume in [
                (
                    "RB2405",
                    "2024-05-15",
                    [3100, 3100, 3120, 3120][i],
                    [3100, 3110, 3120, 3120][i],
                    20000 if i == 0 else 5000,
                    10000 if i == 0 else 2000,
                ),
                (
                    "RB2410",
                    "2024-10-15",
                    [3200, 3200, 3200, 3215][i],
                    [3200, 3200, 3210, 3215][i],
                    5000 if i == 0 else 30000,
                    2000 if i == 0 else 30000,
                ),
            ]
        ]
    )
    weights = pd.DataFrame({"RB": [0.2, 0.2, 0]}, index=dates[1:])
    episodes, rejected = module.build_supply_episodes(raw, weights)
    assert not rejected and len(episodes) == 1
    assert episodes[0].gross_pnl == 350
    assert episodes[0].turnover_notional == 31000 + 31200 + 32000 + 32150
    missing = raw.loc[~(raw.date.eq(dates[2]) & raw.symbol.eq("RB2405"))]
    episodes, rejected = module.build_supply_episodes(missing, weights)
    assert not episodes and rejected[0]["reason"] == "missing episode mark"


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
