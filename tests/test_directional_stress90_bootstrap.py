from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

_CHINA = ZoneInfo("Asia/Shanghai")


def _concurrent_bootstrap_worker(
    runtime_text: str,
    through: str,
    input_sha256: dict[str, str],
    candidate_weight_sha256: str,
    pause_before_ohlc,
    release_ohlc,
    pause_before_oi,
    release_oi,
    done,
    results,
) -> None:
    from types import MappingProxyType

    import afuture.directional_stress90_bootstrap as bootstrap_module
    from afuture.directional_ohlc_cache import DirectionalOHLCCacheStore
    from afuture.directional_stress90_bootstrap import Stress90BootstrapExpectations

    original_policy = bootstrap_module.STRESS90_POLICY

    class PolicyProxy:
        historical_candidate_weight_sha256 = candidate_weight_sha256

        def __getattr__(self, name):
            return getattr(original_policy, name)

    bootstrap_module.FIXED_STRESS90_INPUT_SHA256 = MappingProxyType(input_sha256)
    bootstrap_module.STRESS90_POLICY = PolicyProxy()
    expectations = Stress90BootstrapExpectations(
        input_sha256=input_sha256,
        candidate_weight_sha256=candidate_weight_sha256,
        official_historical_profile=True,
    )
    if pause_before_ohlc is not None:
        method_name = "save_new" if hasattr(DirectionalOHLCCacheStore, "save_new") else "save"
        real_save = getattr(DirectionalOHLCCacheStore, method_name)

        def pause_then_save(self, *args, **kwargs):
            pause_before_ohlc.set()
            if not release_ohlc.wait(30):
                raise RuntimeError("timed out waiting to release concurrent bootstrap")
            return real_save(self, *args, **kwargs)

        setattr(DirectionalOHLCCacheStore, method_name, pause_then_save)
    if pause_before_oi is not None:
        validation_name = (
            "creation_token_matches_unlocked"
            if hasattr(bootstrap_module, "creation_token_matches_unlocked")
            else "creation_token_matches"
        )
        real_validate = getattr(bootstrap_module, validation_name)
        paused = False

        def pause_then_validate(*args, **kwargs):
            nonlocal paused
            if not paused:
                paused = True
                pause_before_oi.set()
                if not release_oi.wait(30):
                    raise RuntimeError("timed out waiting to release pre-OI validation")
            return real_validate(*args, **kwargs)

        setattr(bootstrap_module, validation_name, pause_then_validate)
    try:
        result = bootstrap_module.bootstrap_stress90(
            runtime_dir=runtime_text,
            through_day=through,
            expectations=expectations,
            write_artifacts=True,
        )
        results.put(("success", str(result.oi_evidence_path)))
    except BaseException as exc:
        results.put(("error", type(exc).__name__, str(exc)))
    finally:
        done.set()


def _ordinary_prerequisite_writer(kind: str, runtime_text: str, done, results) -> None:
    from afuture.directional_activity import DirectionalActivityStore
    from afuture.directional_ohlc_cache import DirectionalOHLCCacheStore
    from afuture.directional_stress90_policy import STRESS90_POLICY
    from afuture.directional_stress90_state import Stress90PolicyStateStore

    runtime = Path(runtime_text)
    try:
        if kind == "ohlc":
            store = DirectionalOHLCCacheStore(runtime / "directional_ohlc_cache.json")
            entry = store.load(STRESS90_POLICY.products)
            assert entry is not None
            store.save(entry.products, entry.open, entry.close)
        elif kind == "activity":
            store = DirectionalActivityStore(runtime / "directional_activity.json")
            store.save_state(store.load_state())
        elif kind == "policy":
            store = Stress90PolicyStateStore(runtime / "stress90_policy_state.json")
            record = store.load_required_record()
            store.save(record.state, expected_sequence=record.sequence)
        else:  # pragma: no cover - test helper contract
            raise AssertionError(f"unsupported prerequisite writer: {kind}")
        results.put((kind, "success"))
    except BaseException as exc:
        results.put((kind, "error", type(exc).__name__, str(exc)))
    finally:
        done.set()


def _activity_retarget_writer(
    path_text: str,
    trading_day: str,
    pause_after_lock,
    release_write,
    done,
    results,
) -> None:
    from afuture.directional_activity import (
        ContractActivity,
        DirectionalActivitySnapshot,
        DirectionalActivityStore,
    )

    store = DirectionalActivityStore(path_text)
    if pause_after_lock is not None:
        real_replace = store._replace_encoded

        def pause_then_replace(encoded: bytes) -> None:
            pause_after_lock.set()
            if not release_write.wait(30):
                raise RuntimeError("timed out waiting to release retarget writer")
            real_replace(encoded)

        store._replace_encoded = pause_then_replace
    try:
        activity = ContractActivity(
            symbol="A2612",
            exchange="DCE",
            product="A",
            trading_day=trading_day,
            volume=1.0,
            open_interest=1.0,
            timestamp=datetime.fromisoformat(
                f"{trading_day[:4]}-{trading_day[4:6]}-{trading_day[6:]}T15:00:00+08:00"
            ),
        )
        store.save(DirectionalActivitySnapshot(trading_day, {activity.symbol: activity}))
        results.put((trading_day, "success", str(store.path)))
    except BaseException as exc:
        results.put((trading_day, "error", type(exc).__name__, str(exc)))
    finally:
        done.set()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_synthetic_archive(runtime: Path):
    from afuture.directional_60m_oi_confirmation import (
        build_daily_price_oi_flow,
        lag_flow_to_target_days,
    )
    from afuture.directional_stress90_bootstrap import Stress90BootstrapExpectations
    from afuture.directional_stress90_policy import (
        STRESS90_POLICY,
        build_stress90_candidate_path,
        candidate_weight_digest,
    )
    from afuture.execution_aligned_policy import ExecutionAlignedAggressivePolicy

    runtime.mkdir(parents=True)
    days = pd.bdate_range("2026-01-02", periods=44)
    open_prices = pd.DataFrame(index=days, columns=STRESS90_POLICY.products, dtype=float)
    close_prices = open_prices.copy()
    rows: list[dict[str, object]] = []
    positions = np.arange(len(days), dtype=float)
    for offset, product in enumerate(STRESS90_POLICY.products):
        close_values = (
            100.0
            + offset
            + np.cumsum(
                0.18 * np.sin(positions * ((offset % 7) + 1) / 9.0) + 0.025 * ((offset % 3) - 1)
            )
        )
        open_values = close_values / (1.0 + 0.0015 * np.sin(positions + offset))
        open_prices[product] = open_values
        close_prices[product] = close_values
        rows.extend(
            {
                "date": day,
                "product": product,
                "open": float(open_value),
                "close": float(close_value),
            }
            for day, open_value, close_value in zip(days, open_values, close_values, strict=True)
        )
    pd.DataFrame(rows).to_csv(runtime / "broad_daily_universe.csv", index=False)

    rebuilt = ExecutionAlignedAggressivePolicy(STRESS90_POLICY.products).weight_history(
        open_prices, close_prices
    )
    target_days = days[-12:]
    archived = rebuilt.loc[target_days].stack(future_stack=True).rename("weight").reset_index()
    archived.columns = ["level_0", "level_1", "weight"]
    archived.to_csv(runtime / "execution_aligned_weights.csv", index=False)

    contracts: list[dict[str, object]] = []
    for day in target_days:
        for product in STRESS90_POLICY.products:
            contracts.append(
                {
                    "date": day,
                    "delivery": day + pd.Timedelta(days=120),
                    "product": product,
                    "symbol": f"{product}2612",
                    "open": float(open_prices.at[day, product]),
                    "close": float(close_prices.at[day, product]),
                    "volume": 2_000.0,
                    "hold": 6_000.0,
                }
            )
    pd.DataFrame(contracts).to_csv(runtime / "return_target_specific_contracts.csv", index=False)

    source_days = [days[days.get_loc(target_days[0]) - 1], *target_days[:-1]]
    fixed_60m_days = [*source_days, target_days[-1]]
    bars: list[dict[str, object]] = []
    for source_day in fixed_60m_days:
        for product in STRESS90_POLICY.oi_products:
            bars.extend(
                [
                    {
                        "datetime": source_day + pd.Timedelta(hours=9),
                        "product": product,
                        "symbol": f"{product}2612",
                        "open": 100.0,
                        "close": 100.0,
                        "volume": 10.0,
                        "hold": 100.0,
                    },
                    {
                        "datetime": source_day + pd.Timedelta(hours=14),
                        "product": product,
                        "symbol": f"{product}2612",
                        "open": 100.0,
                        "close": 100.0,
                        "volume": 20.0,
                        "hold": 100.0,
                    },
                ]
            )
    midpoint = len(bars) // 2
    pd.DataFrame(bars[:midpoint]).to_csv(runtime / "prior_two_year_broad_60m.csv", index=False)
    pd.DataFrame(bars[midpoint:]).to_csv(runtime / "two_year_broad_60m.csv", index=False)

    flow = build_daily_price_oi_flow(pd.DataFrame(bars))
    lagged = lag_flow_to_target_days(
        flow,
        target_days=target_days,
        products=STRESS90_POLICY.oi_products,
    )
    candidate = build_stress90_candidate_path(
        base_weights=rebuilt.loc[target_days],
        completed_close_prices=close_prices,
        confirming_flow=lagged,
    ).survivor_weights
    source_manifest = {
        name: _sha256(runtime / name)
        for name in (
            "broad_daily_universe.csv",
            "return_target_specific_contracts.csv",
            "execution_aligned_weights.csv",
            "prior_two_year_broad_60m.csv",
            "two_year_broad_60m.csv",
        )
    }
    expectations = Stress90BootstrapExpectations(
        input_sha256=source_manifest,
        candidate_weight_sha256=candidate_weight_digest(candidate),
        official_historical_profile=False,
    )
    return target_days[-1].strftime("%Y%m%d"), expectations


def _refreshed_expectations(runtime: Path, old):
    from dataclasses import replace

    return replace(
        old,
        input_sha256={name: _sha256(runtime / name) for name in old.input_sha256},
    )


def _promote_synthetic_profile(monkeypatch: pytest.MonkeyPatch, expectations):
    from dataclasses import replace
    from types import MappingProxyType

    import afuture.directional_stress90_bootstrap as bootstrap_module

    original_policy = bootstrap_module.STRESS90_POLICY

    class PolicyProxy:
        historical_candidate_weight_sha256 = expectations.candidate_weight_sha256

        def __init__(self, original):
            self._original = original

        def __getattr__(self, name):
            return getattr(self._original, name)

    policy = PolicyProxy(original_policy)
    monkeypatch.setattr(
        bootstrap_module,
        "FIXED_STRESS90_INPUT_SHA256",
        MappingProxyType(dict(expectations.input_sha256)),
    )
    monkeypatch.setattr(bootstrap_module, "STRESS90_POLICY", policy)
    return replace(expectations, official_historical_profile=True)


def test_bootstrap_accepts_only_the_exact_declared_daily_cells(tmp_path: Path):
    from afuture.directional_stress90_bootstrap import (
        Stress90BootstrapError,
        _load_continuous_panels,
    )

    runtime = tmp_path / "runtime"
    _through, _expectations = _write_synthetic_archive(runtime)
    path = runtime / "broad_daily_universe.csv"
    rows = pd.read_csv(path)
    days = sorted(rows["date"].unique())
    allowed = frozenset({(pd.Timestamp(days[3]), "AP"), (pd.Timestamp(days[5]), "CF")})
    for day, product in allowed:
        rows = rows.loc[~((pd.to_datetime(rows["date"]) == day) & (rows["product"] == product))]
    rows.to_csv(path, index=False)

    _raw, open_prices, close_prices = _load_continuous_panels(
        path,
        allowed_missing=allowed,
    )
    assert {
        (day, product)
        for day, product in allowed
        if pd.isna(open_prices.at[day, product]) and pd.isna(close_prices.at[day, product])
    } == allowed

    with pytest.raises(Stress90BootstrapError, match="declared gaps"):
        _load_continuous_panels(path, allowed_missing=frozenset())


def test_bootstrap_accepts_only_the_exact_declared_specific_contract_gaps(tmp_path: Path):
    from afuture.directional_stress90_bootstrap import (
        Stress90BootstrapError,
        _load_archived_weights,
        _validate_specific_contracts,
    )

    runtime = tmp_path / "runtime"
    through, _expectations = _write_synthetic_archive(runtime)
    targets = _load_archived_weights(
        runtime / "execution_aligned_weights.csv",
        pd.Timestamp(datetime.strptime(through, "%Y%m%d")),
    ).index
    path = runtime / "return_target_specific_contracts.csv"
    rows = pd.read_csv(path)
    missing = frozenset({(targets[2], "AP"), (targets[4], "CF")})
    for day, product in missing:
        rows = rows.loc[~((pd.to_datetime(rows["date"]) == day) & (rows["product"] == product))]
    rows.to_csv(path, index=False)

    _validate_specific_contracts(path, pd.DatetimeIndex(targets), allowed_missing=missing)
    with pytest.raises(Stress90BootstrapError, match="declared gaps"):
        _validate_specific_contracts(path, pd.DatetimeIndex(targets))


def test_bootstrap_accepts_only_the_exact_declared_target_skip():
    from afuture.directional_stress90_bootstrap import (
        Stress90BootstrapError,
        _validate_target_continuity,
    )

    sessions = pd.bdate_range("2026-01-05", periods=6)
    missing = sessions[3]
    targets = sessions[1:].delete(2)
    _validate_target_continuity(
        targets,
        sessions,
        allowed_missing_sessions=frozenset({missing}),
    )
    with pytest.raises(Stress90BootstrapError, match="gap cannot be skipped"):
        _validate_target_continuity(targets, sessions)


def test_bootstrap_accepts_only_the_exact_declared_oi_source_gap(tmp_path: Path):
    from afuture.directional_stress90_bootstrap import (
        Stress90BootstrapError,
        _build_lagged_flow,
        _load_archived_weights,
        _load_continuous_panels,
    )

    runtime = tmp_path / "runtime"
    through, _expectations = _write_synthetic_archive(runtime)
    _raw, _open, close = _load_continuous_panels(runtime / "broad_daily_universe.csv")
    targets = _load_archived_weights(
        runtime / "execution_aligned_weights.csv",
        pd.Timestamp(datetime.strptime(through, "%Y%m%d")),
    ).index
    bars = pd.concat(
        [
            pd.read_csv(runtime / "prior_two_year_broad_60m.csv"),
            pd.read_csv(runtime / "two_year_broad_60m.csv"),
        ],
        ignore_index=True,
    )
    bars["datetime"] = pd.to_datetime(bars["datetime"])
    source_day = targets[3]
    bars = bars.loc[
        ~((bars["datetime"].dt.normalize() == source_day) & (bars["product"].str.upper() == "TA"))
    ]
    allowed = frozenset({(source_day, "TA")})

    lagged, _source_days = _build_lagged_flow(
        bars,
        pd.DatetimeIndex(targets),
        close.index,
        allowed_missing=allowed,
    )
    assert pd.isna(lagged.at[targets[4], "TA"])
    with pytest.raises(Stress90BootstrapError, match="OI coverage incomplete"):
        _build_lagged_flow(bars, pd.DatetimeIndex(targets), close.index)


def test_dry_run_rebuilds_base_and_replays_incremental_candidate_exactly(tmp_path: Path):
    from afuture.directional_stress90_bootstrap import bootstrap_stress90

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)

    result = bootstrap_stress90(
        runtime_dir=runtime,
        through_day=through,
        expectations=expectations,
        write_artifacts=False,
    )

    assert result.source_manifest == expectations.input_sha256
    assert result.candidate_weight_sha256 == expectations.candidate_weight_sha256
    assert result.base_max_abs_error <= 5e-15
    assert result.batch_incremental_max_abs_error <= 1e-14
    assert result.last_target_day == through
    assert result.last_input_day < through
    assert result.historical_candidate_parity is False
    assert result.seed_path is None
    assert result.state_path is None
    assert not (runtime / "stress90_bootstrap_seed.json").exists()


def test_live_bootstrap_persists_verified_through_day_oi_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from afuture.directional_activity import DirectionalActivityStore
    from afuture.directional_ohlc_cache import DirectionalOHLCCacheStore
    from afuture.directional_stress90_bootstrap import bootstrap_stress90
    from afuture.directional_stress90_oi_runtime import Stress90OiEvidenceStore
    from afuture.directional_stress90_policy import STRESS90_POLICY
    from afuture.directional_stress90_state import Stress90PolicyStateStore

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)
    expectations = _promote_synthetic_profile(monkeypatch, expectations)

    result = bootstrap_stress90(
        runtime_dir=runtime,
        through_day=through,
        expectations=expectations,
        write_artifacts=True,
    )

    assert result.oi_evidence_path == runtime / "stress90_oi_evidence.json"
    record = Stress90OiEvidenceStore(result.oi_evidence_path).load_required_record()
    assert record.sequence == 1
    assert len(record.state.completed) == 1
    evidence = record.state.completed[0]
    assert evidence.source == "fixed_historical_60m"
    assert evidence.trading_day == through
    assert evidence.complete is True
    assert set(evidence.flows) == {"A", "C", "EG", "I", "M", "P", "PP", "TA", "Y"}
    policy = Stress90PolicyStateStore(result.state_path).load_required()
    assert policy.prepared_decision is not None
    assert policy.prepared_decision.target_trading_day == through
    assert policy.prepared_decision.daily_decision_digest == policy.last_decision_digest
    ohlc = DirectionalOHLCCacheStore(runtime / "directional_ohlc_cache.json").load(
        STRESS90_POLICY.products
    )
    assert ohlc is not None
    assert ohlc.latest_date.strftime("%Y%m%d") == through
    activity = DirectionalActivityStore(runtime / "directional_activity.json").load()
    assert activity is not None
    assert activity.trading_day == through
    assert {row.product for row in activity.contracts.values()} == set(STRESS90_POLICY.products)
    assert result.to_dict()["oi_evidence_path"] == str(result.oi_evidence_path)


@pytest.mark.parametrize("failure_after", ["ohlc", "activity", "seed", "policy"])
def test_bootstrap_pre_oi_artifact_failure_rolls_back_and_retries_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_after: str,
) -> None:
    from afuture.directional_activity import DirectionalActivityStore
    from afuture.directional_ohlc_cache import DirectionalOHLCCacheStore
    from afuture.directional_stress90_bootstrap import (
        Stress90BootstrapError,
        bootstrap_stress90,
    )
    from afuture.directional_stress90_oi_runtime import Stress90OiEvidenceStore
    from afuture.directional_stress90_state import (
        Stress90PolicyStateStore,
        Stress90SeedStore,
    )

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)
    expectations = _promote_synthetic_profile(monkeypatch, expectations)
    targets = {
        "ohlc": (DirectionalOHLCCacheStore, "save_new"),
        "activity": (DirectionalActivityStore, "save_new"),
        "seed": (Stress90SeedStore, "save_new"),
        "policy": (Stress90PolicyStateStore, "save_new"),
    }
    owner, method_name = targets[failure_after]
    real_save = getattr(owner, method_name)

    def save_then_fail(self, *args, **kwargs):
        real_save(self, *args, **kwargs)
        raise OSError(f"injected failure after {failure_after} write")

    monkeypatch.setattr(owner, method_name, save_then_fail)
    with pytest.raises(Stress90BootstrapError, match="failed to persist"):
        bootstrap_stress90(
            runtime_dir=runtime,
            through_day=through,
            expectations=expectations,
            write_artifacts=True,
        )

    oi_store = Stress90OiEvidenceStore(runtime / "stress90_oi_evidence.json")
    assert not oi_store.path.exists()
    assert not oi_store.previous_path.exists()
    assert not oi_store.lineage_path.exists()
    assert not oi_store.lock_path.exists()
    for name in (
        "directional_ohlc_cache.json",
        "directional_activity.json",
        "stress90_bootstrap_seed.json",
        "stress90_policy_state.json",
    ):
        assert not (runtime / name).exists()

    monkeypatch.setattr(owner, method_name, real_save)
    result = bootstrap_stress90(
        runtime_dir=runtime,
        through_day=through,
        expectations=expectations,
        write_artifacts=True,
    )
    oi = Stress90OiEvidenceStore(result.oi_evidence_path).load_required_record()
    assert oi.sequence == 1
    assert oi.parent_checksum is None
    assert not oi_store.previous_path.exists()


def test_concurrent_bootstrap_aliases_serialize_without_split_authority(
    tmp_path: Path,
) -> None:
    from multiprocessing import get_context

    from afuture.directional_activity import DirectionalActivityStore
    from afuture.directional_ohlc_cache import DirectionalOHLCCacheStore
    from afuture.directional_stress90_oi_runtime import Stress90OiEvidenceStore
    from afuture.directional_stress90_policy import STRESS90_POLICY
    from afuture.directional_stress90_state import (
        Stress90PolicyStateStore,
        Stress90SeedStore,
    )

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)
    lexical_component = tmp_path / "lexical-component"
    lexical_component.mkdir()
    lexical_alias = lexical_component / ".." / runtime.name
    symlink_parent = tmp_path / "safe-parent-alias"
    symlink_parent.symlink_to(tmp_path, target_is_directory=True)
    symlink_alias = symlink_parent / runtime.name
    context = get_context("spawn")
    first_paused = context.Event()
    release_first = context.Event()
    first_done = context.Event()
    second_done = context.Event()
    results = context.Queue()
    worker_args = (
        through,
        dict(expectations.input_sha256),
        expectations.candidate_weight_sha256,
    )
    first = context.Process(
        target=_concurrent_bootstrap_worker,
        args=(
            str(lexical_alias),
            *worker_args,
            first_paused,
            release_first,
            None,
            None,
            first_done,
            results,
        ),
    )
    second = context.Process(
        target=_concurrent_bootstrap_worker,
        args=(
            str(symlink_alias),
            *worker_args,
            None,
            None,
            None,
            None,
            second_done,
            results,
        ),
    )

    first.start()
    assert first_paused.wait(20)
    second.start()
    second_finished_while_first_was_paused = second_done.wait(10)
    release_first.set()
    first.join(30)
    second.join(30)
    assert first.exitcode == 0
    assert second.exitcode == 0
    outcomes = [results.get(timeout=5), results.get(timeout=5)]

    assert second_finished_while_first_was_paused is False
    assert sum(outcome[0] == "success" for outcome in outcomes) == 1, outcomes
    assert sum(outcome[0] == "error" for outcome in outcomes) == 1, outcomes
    assert (
        DirectionalOHLCCacheStore(runtime / "directional_ohlc_cache.json").load(
            STRESS90_POLICY.products
        )
        is not None
    )
    assert DirectionalActivityStore(runtime / "directional_activity.json").load() is not None
    Stress90SeedStore(runtime / "stress90_bootstrap_seed.json").load_required(
        expected_source_manifest=expectations.input_sha256
    )
    assert (
        Stress90PolicyStateStore(runtime / "stress90_policy_state.json")
        .load_required_record()
        .sequence
        == 1
    )
    assert (
        Stress90OiEvidenceStore(runtime / "stress90_oi_evidence.json")
        .load_required_record()
        .sequence
        == 1
    )


def test_bootstrap_pre_oi_prerequisite_locks_block_ordinary_writers_until_commit(
    tmp_path: Path,
) -> None:
    from multiprocessing import get_context

    from afuture.directional_stress90_oi_runtime import Stress90OiEvidenceStore
    from afuture.directional_stress90_state import Stress90PolicyStateStore

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)
    context = get_context("spawn")
    pre_oi_paused = context.Event()
    release_oi = context.Event()
    bootstrap_done = context.Event()
    results = context.Queue()
    bootstrap = context.Process(
        target=_concurrent_bootstrap_worker,
        args=(
            str(runtime),
            through,
            dict(expectations.input_sha256),
            expectations.candidate_weight_sha256,
            None,
            None,
            pre_oi_paused,
            release_oi,
            bootstrap_done,
            results,
        ),
    )
    bootstrap.start()
    assert pre_oi_paused.wait(20)

    writer_done = {kind: context.Event() for kind in ("ohlc", "activity", "policy")}
    writers = [
        context.Process(
            target=_ordinary_prerequisite_writer,
            args=(kind, str(runtime), writer_done[kind], results),
        )
        for kind in writer_done
    ]
    for writer in writers:
        writer.start()
    completed_before_commit = {kind: event.wait(3) for kind, event in writer_done.items()}
    release_oi.set()
    bootstrap.join(30)
    for writer in writers:
        writer.join(30)

    assert completed_before_commit == {"ohlc": False, "activity": False, "policy": False}
    assert bootstrap.exitcode == 0
    assert all(writer.exitcode == 0 for writer in writers)
    outcomes = [results.get(timeout=5) for _ in range(4)]
    assert sum(outcome[0] == "success" for outcome in outcomes) == 1, outcomes
    assert {(outcome[0], outcome[1]) for outcome in outcomes if outcome[0] != "success"} == {
        ("ohlc", "success"),
        ("activity", "success"),
        ("policy", "success"),
    }
    assert (
        Stress90OiEvidenceStore(runtime / "stress90_oi_evidence.json")
        .load_required_record()
        .sequence
        == 1
    )
    assert (
        Stress90PolicyStateStore(runtime / "stress90_policy_state.json")
        .load_required_record()
        .sequence
        == 2
    )


def test_bootstrap_final_oi_is_compare_and_swap_from_sequence_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.directional_stress90_bootstrap import (
        Stress90BootstrapError,
        bootstrap_stress90,
    )
    from afuture.directional_stress90_oi_runtime import (
        Stress90OiEvidenceState,
        Stress90OiEvidenceStore,
    )
    from afuture.directional_stress90_state import Stress90PolicyStateStore

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)
    expectations = _promote_synthetic_profile(monkeypatch, expectations)
    oi_store = Stress90OiEvidenceStore(runtime / "stress90_oi_evidence.json")
    foreign_state = Stress90OiEvidenceState(raw_ticks_observed=7)
    real_save_new = Stress90PolicyStateStore.save_new

    def save_policy_then_create_foreign_oi(self, *args, **kwargs):
        result = real_save_new(self, *args, **kwargs)
        oi_store.save_state(foreign_state)
        return result

    monkeypatch.setattr(Stress90PolicyStateStore, "save_new", save_policy_then_create_foreign_oi)
    with pytest.raises(Stress90BootstrapError, match=r"failed to persist.*sequence changed"):
        bootstrap_stress90(
            runtime_dir=runtime,
            through_day=through,
            expectations=expectations,
            write_artifacts=True,
        )

    oi_record = oi_store.load_required_record()
    assert oi_record.sequence == 1
    assert oi_record.state == foreign_state
    assert not oi_store.previous_path.exists()
    for name in (
        "directional_ohlc_cache.json",
        "directional_activity.json",
        "stress90_bootstrap_seed.json",
        "stress90_policy_state.json",
    ):
        assert (runtime / name).is_file()


def test_owner_paths_are_frozen_to_canonical_parent_at_construction(tmp_path: Path) -> None:
    from afuture.directional_activity import DirectionalActivityStore
    from afuture.directional_ohlc_cache import DirectionalOHLCCacheStore
    from afuture.directional_stress90_oi_runtime import Stress90OiEvidenceStore
    from afuture.directional_stress90_state import (
        Stress90PolicyStateStore,
        Stress90SeedStore,
    )

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    alias = tmp_path / "runtime-alias"
    alias.symlink_to(first, target_is_directory=True)
    stores = (
        DirectionalOHLCCacheStore(alias / "ohlc.json"),
        DirectionalActivityStore(alias / "activity.json"),
        Stress90SeedStore(alias / "seed.json"),
        Stress90PolicyStateStore(alias / "policy.json"),
        Stress90OiEvidenceStore(alias / "oi.json"),
    )
    alias.unlink()
    alias.symlink_to(second, target_is_directory=True)

    assert all(store.path.parent == first for store in stores)
    policy = stores[3]
    assert isinstance(policy, Stress90PolicyStateStore)
    assert policy.previous_path.parent == first
    assert policy.lock_path.parent == first


def test_activity_writer_cannot_lock_one_parent_then_write_retargeted_parent(
    tmp_path: Path,
) -> None:
    from multiprocessing import get_context

    from afuture.directional_activity import DirectionalActivityStore

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    alias = tmp_path / "runtime-alias"
    alias.symlink_to(first, target_is_directory=True)
    context = get_context("spawn")
    locked = context.Event()
    release_first = context.Event()
    first_done = context.Event()
    second_done = context.Event()
    results = context.Queue()
    first_writer = context.Process(
        target=_activity_retarget_writer,
        args=(
            str(alias / "activity.json"),
            "20260820",
            locked,
            release_first,
            first_done,
            results,
        ),
    )
    first_writer.start()
    assert locked.wait(20)
    alias.unlink()
    alias.symlink_to(second, target_is_directory=True)
    second_writer = context.Process(
        target=_activity_retarget_writer,
        args=(
            str(second / "activity.json"),
            "20260821",
            None,
            None,
            second_done,
            results,
        ),
    )
    second_writer.start()
    assert second_done.wait(20)
    release_first.set()
    first_writer.join(30)
    second_writer.join(30)

    assert first_writer.exitcode == 0
    assert second_writer.exitcode == 0
    outcomes = [results.get(timeout=5), results.get(timeout=5)]
    assert all(outcome[1] == "success" for outcome in outcomes), outcomes
    assert DirectionalActivityStore(first / "activity.json").load().trading_day == "20260820"
    assert DirectionalActivityStore(second / "activity.json").load().trading_day == "20260821"


@pytest.mark.parametrize(
    ("owner_path", "owner_import", "owner_name"),
    [
        ("directional_ohlc_cache.json", "ohlc", "DirectionalOHLCCacheStore"),
        ("directional_activity.json", "activity", "DirectionalActivityStore"),
        ("stress90_bootstrap_seed.json", "state", "Stress90SeedStore"),
        ("stress90_policy_state.json", "state", "Stress90PolicyStateStore"),
    ],
)
def test_bootstrap_owner_create_only_write_preserves_artifact_appearing_after_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner_path: str,
    owner_import: str,
    owner_name: str,
) -> None:
    from afuture import directional_activity, directional_ohlc_cache, directional_stress90_state
    from afuture.directional_stress90_bootstrap import (
        Stress90BootstrapError,
        bootstrap_stress90,
    )

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)
    expectations = _promote_synthetic_profile(monkeypatch, expectations)
    modules = {
        "activity": directional_activity,
        "ohlc": directional_ohlc_cache,
        "state": directional_stress90_state,
    }
    owner = getattr(modules[owner_import], owner_name)
    method_name = "save_new" if hasattr(owner, "save_new") else "save"
    real_save = getattr(owner, method_name)
    path = runtime / owner_path
    appeared_bytes = b"artifact from a concurrent writer"

    def appear_then_save(self, *args, **kwargs):
        path.write_bytes(appeared_bytes)
        return real_save(self, *args, **kwargs)

    monkeypatch.setattr(owner, method_name, appear_then_save)
    with pytest.raises(Stress90BootstrapError, match="failed to persist"):
        bootstrap_stress90(
            runtime_dir=runtime,
            through_day=through,
            expectations=expectations,
            write_artifacts=True,
        )
    assert path.read_bytes() == appeared_bytes
    assert not (runtime / "stress90_oi_evidence.json").exists()


def test_bootstrap_pre_oi_revalidation_preserves_changed_foreign_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.directional_stress90_bootstrap import (
        Stress90BootstrapError,
        bootstrap_stress90,
    )
    from afuture.directional_stress90_state import Stress90PolicyStateStore

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)
    expectations = _promote_synthetic_profile(monkeypatch, expectations)
    ohlc_path = runtime / "directional_ohlc_cache.json"
    foreign_bytes = b"replacement from a concurrent writer"
    method_name = "save_new" if hasattr(Stress90PolicyStateStore, "save_new") else "save"
    real_policy_save = getattr(Stress90PolicyStateStore, method_name)

    def save_policy_then_replace_ohlc(self, *args, **kwargs):
        result = real_policy_save(self, *args, **kwargs)
        ohlc_path.write_bytes(foreign_bytes)
        return result

    monkeypatch.setattr(Stress90PolicyStateStore, method_name, save_policy_then_replace_ohlc)
    with pytest.raises(
        Stress90BootstrapError,
        match=r"prerequisite.*changed.*rollback cleanup errors",
    ):
        bootstrap_stress90(
            runtime_dir=runtime,
            through_day=through,
            expectations=expectations,
            write_artifacts=True,
        )
    assert ohlc_path.read_bytes() == foreign_bytes
    assert not (runtime / "stress90_oi_evidence.json").exists()


@pytest.mark.parametrize("sidecar", ["previous", "lock"])
def test_bootstrap_preflight_preserves_orphan_policy_sidecar_on_every_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sidecar: str,
) -> None:
    from afuture.directional_stress90_bootstrap import (
        Stress90BootstrapError,
        bootstrap_stress90,
    )
    from afuture.directional_stress90_state import Stress90PolicyStateStore

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)
    expectations = _promote_synthetic_profile(monkeypatch, expectations)
    store = Stress90PolicyStateStore(runtime / "stress90_policy_state.json")
    incident_path = store.previous_path if sidecar == "previous" else store.lock_path
    incident_bytes = f"preexisting policy {sidecar} incident".encode()
    incident_path.write_bytes(incident_bytes)
    evidence_name = "prev" if sidecar == "previous" else "lock"

    for _ in range(2):
        with pytest.raises(
            Stress90BootstrapError,
            match=rf"existing Stress-90 policy state incident.*{evidence_name}",
        ):
            bootstrap_stress90(
                runtime_dir=runtime,
                through_day=through,
                expectations=expectations,
                write_artifacts=True,
            )
        assert incident_path.read_bytes() == incident_bytes
        assert not store.path.exists()
        assert not (runtime / "directional_ohlc_cache.json").exists()
        assert not (runtime / "directional_activity.json").exists()
        assert not (runtime / "stress90_bootstrap_seed.json").exists()
        assert not (runtime / "stress90_oi_evidence.json").exists()


@pytest.mark.parametrize(
    ("owner", "filename"),
    [
        ("OHLC cache", "directional_ohlc_cache.json"),
        ("activity", "directional_activity.json"),
        ("bootstrap seed", "stress90_bootstrap_seed.json"),
        ("policy state", "stress90_policy_state.json"),
    ],
)
def test_bootstrap_preflight_validates_corrupt_prerequisite_through_owning_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner: str,
    filename: str,
) -> None:
    from afuture.directional_stress90_bootstrap import (
        Stress90BootstrapError,
        bootstrap_stress90,
    )

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)
    expectations = _promote_synthetic_profile(monkeypatch, expectations)
    path = runtime / filename
    corrupt = b"{not-json"
    path.write_bytes(corrupt)

    with pytest.raises(
        Stress90BootstrapError,
        match=rf"existing Stress-90 {owner} incident",
    ):
        bootstrap_stress90(
            runtime_dir=runtime,
            through_day=through,
            expectations=expectations,
            write_artifacts=True,
        )
    assert path.read_bytes() == corrupt


def test_bootstrap_preflight_rejects_dangling_policy_sidecar_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.directional_stress90_bootstrap import (
        Stress90BootstrapError,
        bootstrap_stress90,
    )
    from afuture.directional_stress90_state import Stress90PolicyStateStore

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)
    expectations = _promote_synthetic_profile(monkeypatch, expectations)
    store = Stress90PolicyStateStore(runtime / "stress90_policy_state.json")
    store.lock_path.symlink_to(runtime / "missing-policy-lock-target")

    for _ in range(2):
        with pytest.raises(
            Stress90BootstrapError,
            match=r"existing Stress-90 policy state incident.*symlink.*lock",
        ):
            bootstrap_stress90(
                runtime_dir=runtime,
                through_day=through,
                expectations=expectations,
                write_artifacts=True,
            )
        assert store.lock_path.is_symlink()


@pytest.mark.parametrize("sidecar", ["previous", "lineage", "lock"])
def test_bootstrap_preflight_rejects_oi_sidecar_without_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sidecar: str,
) -> None:
    from afuture.directional_stress90_bootstrap import (
        Stress90BootstrapError,
        bootstrap_stress90,
    )
    from afuture.directional_stress90_oi_runtime import Stress90OiEvidenceStore

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)
    expectations = _promote_synthetic_profile(monkeypatch, expectations)
    store = Stress90OiEvidenceStore(runtime / "stress90_oi_evidence.json")
    path = {
        "previous": store.previous_path,
        "lineage": store.lineage_path,
        "lock": store.lock_path,
    }[sidecar]
    path.write_text("durable incident evidence", encoding="utf-8")

    with pytest.raises(
        Stress90BootstrapError,
        match=rf"existing Stress-90 OI evidence incident.*{sidecar}",
    ):
        bootstrap_stress90(
            runtime_dir=runtime,
            through_day=through,
            expectations=expectations,
            write_artifacts=True,
        )

    assert not (runtime / "directional_ohlc_cache.json").exists()
    assert not (runtime / "directional_activity.json").exists()
    assert not (runtime / "stress90_bootstrap_seed.json").exists()
    assert not (runtime / "stress90_policy_state.json").exists()


@pytest.mark.parametrize("incident", ["noncurrent_schema", "invalid_chain"])
def test_bootstrap_preflight_uses_oi_store_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    incident: str,
) -> None:
    import json

    from afuture.directional_stress90_bootstrap import (
        Stress90BootstrapError,
        bootstrap_stress90,
    )
    from afuture.directional_stress90_oi_runtime import (
        OI_EVIDENCE_KIND,
        Stress90OiEvidenceState,
        Stress90OiEvidenceStore,
    )

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)
    expectations = _promote_synthetic_profile(monkeypatch, expectations)
    store = Stress90OiEvidenceStore(runtime / "stress90_oi_evidence.json")
    if incident == "noncurrent_schema":
        unsigned = {
            "kind": OI_EVIDENCE_KIND,
            "schema_version": 2,
            "sequence": 9,
            "parent_checksum": "1" * 64,
            "state": {
                "completed": [],
                "in_progress": None,
                "observed_transitions": [],
                "raw_ticks_observed": 0,
                "duplicate_ticks": 0,
                "volume_resets": 0,
            },
        }
        checksum = hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        store.path.write_text(json.dumps({**unsigned, "checksum": checksum}), encoding="utf-8")
        expected_error = "schema is unsupported"
    else:
        first = store.save_state(Stress90OiEvidenceState())
        store.save_state(
            Stress90OiEvidenceState(raw_ticks_observed=1),
            expected_sequence=first.sequence,
        )
        store.previous_path.unlink()
        expected_error = "previous predecessor"

    with pytest.raises(
        Stress90BootstrapError,
        match=rf"existing Stress-90 OI evidence incident.*{expected_error}",
    ):
        bootstrap_stress90(
            runtime_dir=runtime,
            through_day=through,
            expectations=expectations,
            write_artifacts=True,
        )


def test_bootstrap_final_oi_failure_preserves_incident_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.directional_stress90_bootstrap import (
        Stress90BootstrapError,
        bootstrap_stress90,
    )
    from afuture.directional_stress90_oi_runtime import Stress90OiEvidenceStore

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)
    expectations = _promote_synthetic_profile(monkeypatch, expectations)
    oi_path = runtime / "stress90_oi_evidence.json"
    real_replace = Stress90OiEvidenceStore._atomic_replace

    def fail_before_oi_current(target: Path, payload: bytes) -> None:
        if target == oi_path:
            raise OSError("injected failure before OI current replace")
        real_replace(target, payload)

    monkeypatch.setattr(
        Stress90OiEvidenceStore,
        "_atomic_replace",
        staticmethod(fail_before_oi_current),
    )
    with pytest.raises(Stress90BootstrapError, match="failed to persist"):
        bootstrap_stress90(
            runtime_dir=runtime,
            through_day=through,
            expectations=expectations,
            write_artifacts=True,
        )

    store = Stress90OiEvidenceStore(oi_path)
    assert not store.path.exists()
    assert store.lineage_path.is_file()
    assert store.lock_path.is_file()
    for name in (
        "directional_ohlc_cache.json",
        "directional_activity.json",
        "stress90_bootstrap_seed.json",
        "stress90_policy_state.json",
    ):
        assert (runtime / name).is_file()

    monkeypatch.setattr(
        Stress90OiEvidenceStore,
        "_atomic_replace",
        staticmethod(real_replace),
    )
    with pytest.raises(
        Stress90BootstrapError,
        match="existing Stress-90 OI evidence incident.*lineage.*missing",
    ):
        bootstrap_stress90(
            runtime_dir=runtime,
            through_day=through,
            expectations=expectations,
            write_artifacts=True,
        )


@pytest.mark.parametrize("failure_point", ["after-current-replace", "current-parent-fsync"])
def test_bootstrap_ambiguous_oi_commit_preserves_all_prerequisites_byte_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    import os

    import afuture.directional_stress90_oi_runtime as oi_runtime
    from afuture.directional_stress90_bootstrap import (
        Stress90BootstrapError,
        bootstrap_stress90,
    )
    from afuture.directional_stress90_oi_runtime import Stress90OiEvidenceStore

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)
    expectations = _promote_synthetic_profile(monkeypatch, expectations)
    oi_path = runtime / "stress90_oi_evidence.json"
    prerequisite_paths = tuple(
        runtime / name
        for name in (
            "directional_ohlc_cache.json",
            "directional_activity.json",
            "stress90_bootstrap_seed.json",
            "stress90_policy_state.json",
        )
    )
    before_oi: dict[Path, bytes] = {}
    real_save_state = Stress90OiEvidenceStore.save_state
    real_replace = os.replace
    real_fsync_parent = Stress90OiEvidenceStore._fsync_parent_directory
    current_replaced = False

    def save_state_with_snapshot(self, *args, **kwargs):
        before_oi.update({path: path.read_bytes() for path in prerequisite_paths})
        return real_save_state(self, *args, **kwargs)

    def replace_then_maybe_fail(source, target):
        nonlocal current_replaced
        real_replace(source, target)
        if Path(target) == oi_path:
            current_replaced = True
            if failure_point == "after-current-replace":
                raise OSError("injected failure after OI current replace")

    def fsync_parent_then_maybe_fail(path: Path) -> None:
        if current_replaced and failure_point == "current-parent-fsync":
            raise OSError("injected failure at OI current parent fsync")
        real_fsync_parent(path)

    monkeypatch.setattr(Stress90OiEvidenceStore, "save_state", save_state_with_snapshot)
    monkeypatch.setattr(oi_runtime.os, "replace", replace_then_maybe_fail)
    monkeypatch.setattr(
        Stress90OiEvidenceStore,
        "_fsync_parent_directory",
        staticmethod(fsync_parent_then_maybe_fail),
    )

    with pytest.raises(Stress90BootstrapError, match="failed to persist"):
        bootstrap_stress90(
            runtime_dir=runtime,
            through_day=through,
            expectations=expectations,
            write_artifacts=True,
        )

    assert before_oi
    assert {path: path.read_bytes() for path in prerequisite_paths} == before_oi
    store = Stress90OiEvidenceStore(oi_path)
    assert store.path.is_file()
    assert store.lineage_path.is_file()
    assert store.lock_path.is_file()

    monkeypatch.setattr(oi_runtime.os, "replace", real_replace)
    monkeypatch.setattr(
        Stress90OiEvidenceStore,
        "_fsync_parent_directory",
        staticmethod(real_fsync_parent),
    )
    with pytest.raises(Stress90BootstrapError, match="refuses to overwrite existing live"):
        bootstrap_stress90(
            runtime_dir=runtime,
            through_day=through,
            expectations=expectations,
            write_artifacts=True,
        )
    assert {path: path.read_bytes() for path in prerequisite_paths} == before_oi


def test_bootstrap_cleanup_errors_do_not_mask_primary_failure_or_stop_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import afuture.directional_stress90_bootstrap as bootstrap_module
    from afuture.directional_stress90_bootstrap import (
        Stress90BootstrapError,
        bootstrap_stress90,
    )
    from afuture.directional_stress90_state import Stress90SeedStore

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)
    expectations = _promote_synthetic_profile(monkeypatch, expectations)
    seed_path = runtime / "stress90_bootstrap_seed.json"
    ohlc_path = runtime / "directional_ohlc_cache.json"
    activity_path = runtime / "directional_activity.json"
    real_save = Stress90SeedStore.save_new
    real_unlink = bootstrap_module.unlink_created_file
    cleanup_attempts: list[Path] = []

    def save_then_fail(self, *args, **kwargs):
        real_save(self, *args, **kwargs)
        raise OSError("injected primary seed persistence failure")

    def unlink_with_one_failure(token):
        if token.path in {seed_path, ohlc_path, activity_path}:
            cleanup_attempts.append(token.path)
        if token.path == seed_path:
            raise OSError("injected seed cleanup failure")
        return real_unlink(token)

    monkeypatch.setattr(Stress90SeedStore, "save_new", save_then_fail)
    monkeypatch.setattr(bootstrap_module, "unlink_created_file", unlink_with_one_failure)
    with pytest.raises(
        Stress90BootstrapError,
        match=r"failed to persist.*rollback cleanup errors.*seed cleanup failure",
    ) as caught:
        bootstrap_stress90(
            runtime_dir=runtime,
            through_day=through,
            expectations=expectations,
            write_artifacts=True,
        )

    assert isinstance(caught.value.__cause__, OSError)
    assert str(caught.value.__cause__) == "injected primary seed persistence failure"
    assert set(cleanup_attempts) == {seed_path, ohlc_path, activity_path}
    assert seed_path.is_file()
    assert not ohlc_path.exists()
    assert not activity_path.exists()


def test_halted_raw_evidence_sidecar_uses_ctp_market_chain_with_zero_orders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,
        AccountRuntimeRegistry,
    )
    from afuture.cli import _run_stress90_oi_collect
    from afuture.directional import DirectionalConfig
    from afuture.directional_sessions import PRODUCT_SESSION_MANIFEST
    from afuture.directional_stress90_bootstrap import bootstrap_stress90
    from afuture.directional_stress90_policy import STRESS90_POLICY
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.models import AccountSnapshot, ContractInfo

    runtime = tmp_path / "runtime"
    from afuture.runtime_lease import AccountExclusiveRuntimeLease

    monkeypatch.setattr(
        "afuture.account_runtime_registry.PRODUCTION_ACCOUNT_RUNTIME_REGISTRY_PATH",
        runtime / ".account-runtime-registry.json",
    )
    lease_acquired = False
    original_acquire = AccountExclusiveRuntimeLease.acquire

    def acquire_then_mark(self):
        nonlocal lease_acquired
        original_acquire(self)
        lease_acquired = True

    monkeypatch.setattr(AccountExclusiveRuntimeLease, "acquire", acquire_then_mark)
    through, expectations = _write_synthetic_archive(runtime)
    expectations = _promote_synthetic_profile(monkeypatch, expectations)
    AccountRuntimeRegistry(runtime / ".account-runtime-registry.json").initialize(
        strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION
    )
    bootstrap_stress90(
        runtime_dir=runtime,
        through_day=through,
        expectations=expectations,
        write_artifacts=True,
    )
    catalog = [
        ContractInfo(
            f"{product}2612",
            PRODUCT_SESSION_MANIFEST[product].exchange,
            product,
            "2026-12-15",
        )
        for product in FROZEN_PRODUCTS
    ]

    class FakeBroker:
        instance = None

        def __init__(self, _credentials) -> None:
            type(self).instance = self
            self.observer = None
            self.subscriptions: list[tuple[str, str]] = []
            self.sent = 0

        def get_account_identity_digest(self) -> str:
            return "a" * 64

        def configure_order_submission_journal(self, path, **identity) -> None:
            assert lease_acquired
            assert path == runtime / "stress90_ctp_orders.json"
            assert identity["policy_id"] == "directional.stress90"

        def refresh_session_activity(self, *, timeout_seconds: float):
            from afuture.broker.ctp_session_query import build_ctp_session_activity_evidence

            assert timeout_seconds == 0.1
            return build_ctp_session_activity_evidence(
                account_identity_digest="a" * 64,
                trading_day=through,
                order_request_id=11,
                trade_request_id=12,
                orders=(),
                trades=(),
                critical_generation=0,
            )

        def require_session_activity_evidence_current(self, evidence):
            assert evidence.account_identity_digest == "a" * 64
            assert evidence.trading_day == through

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def is_ready(self) -> bool:
            return True

        def snapshot_marker(self):
            return (0, 0)

        def snapshot_ready(self, _marker) -> bool:
            return True

        def get_account(self) -> AccountSnapshot:
            return AccountSnapshot(
                500_000,
                500_000,
                500_000,
                0,
                0,
                0,
                through,
                previous_settlement_equity=500_000,
                settlement_verified=True,
                settlement_id=1,
            )

        def get_trading_day(self) -> str:
            return through

        def get_positions(self):
            return []

        def get_active_orders(self):
            return []

        def get_session_trades(self):
            return []

        def owns_order(self, _order_id: str) -> bool:
            return False

        def refresh_contract_catalog(self, *, timeout_seconds: float):
            assert timeout_seconds > 0
            return SimpleNamespace(trading_day=through)

        def contract_catalog_verified_for_day(self, day: str) -> bool:
            return day == through

        def get_contract_catalog(self):
            return list(catalog)

        def set_raw_tick_observer(self, observer) -> None:
            self.observer = observer

        def subscribe(self, symbol: str, exchange: str) -> None:
            self.subscriptions.append((symbol, exchange))

        def poll_events(self):
            return []

        def health_error(self):
            return None

        def send_order(self, _request):
            self.sent += 1
            raise AssertionError("raw evidence sidecar must never send")

    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", FakeBroker)
    config = SimpleNamespace(
        mode="live",
        ctp=SimpleNamespace(environment="test"),
        directional=DirectionalConfig(
            enabled=True,
            policy="stress90",
            account_exclusive=True,
            products=FROZEN_PRODUCTS,
        ),
        state_path=str(runtime / "state.json"),
        account_registry_path=str(runtime / ".account-runtime-registry.json"),
        journal_path=str(runtime / "audit.jsonl"),
        metadata_timeout_seconds=0.1,
    )
    args = SimpleNamespace(
        confirm_live=False,
        runtime_dir=str(runtime),
        startup_timeout=0.1,
        snapshot_wait=0.1,
        checkpoint_interval=0.01,
        once=True,
        shadow_account=False,
    )

    assert _run_stress90_oi_collect(config, args) == 0
    broker = FakeBroker.instance
    assert broker is not None
    assert broker.sent == 0
    assert broker.observer is None
    assert len(broker.subscriptions) == len(FROZEN_PRODUCTS)
    assert {symbol for symbol, _exchange in broker.subscriptions} == {
        f"{product}2612" for product in STRESS90_POLICY.products
    }


def test_live_bootstrap_rejects_incomplete_through_day_oi_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from afuture.directional_stress90_bootstrap import (
        Stress90BootstrapError,
        bootstrap_stress90,
    )

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)
    paths = [
        runtime / "prior_two_year_broad_60m.csv",
        runtime / "two_year_broad_60m.csv",
    ]
    bars = pd.concat([pd.read_csv(path) for path in paths], ignore_index=True)
    day = pd.to_datetime(bars["datetime"]).dt.strftime("%Y%m%d")
    bars = bars[~((day == through) & (bars["product"] == "A"))]
    split = len(bars) // 2
    bars.iloc[:split].to_csv(paths[0], index=False)
    bars.iloc[split:].to_csv(paths[1], index=False)
    expectations = _refreshed_expectations(runtime, expectations)
    expectations = _promote_synthetic_profile(monkeypatch, expectations)

    with pytest.raises(Stress90BootstrapError, match=rf"{through}.*missing=.*A"):
        bootstrap_stress90(
            runtime_dir=runtime,
            through_day=through,
            expectations=expectations,
            write_artifacts=True,
        )

    assert not (runtime / "stress90_bootstrap_seed.json").exists()
    assert not (runtime / "stress90_policy_state.json").exists()
    assert not (runtime / "stress90_oi_evidence.json").exists()


def test_synthetic_bootstrap_bridge_is_consumed_once_by_first_live_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from afuture.directional import DirectionalConfig
    from afuture.directional_ohlc_cache import DirectionalOHLCCacheStore
    from afuture.directional_sessions import PRODUCT_SESSION_MANIFEST
    from afuture.directional_stress90_bootstrap import bootstrap_stress90
    from afuture.directional_stress90_oi_runtime import (
        ObservedTradingDayTransition,
        Stress90OiEvidenceStore,
    )
    from afuture.directional_stress90_policy import STRESS90_POLICY
    from afuture.directional_stress90_runtime import Stress90DirectionalPortfolioManager
    from afuture.directional_stress90_state import Stress90PolicyStateStore
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.models import ContractInfo
    from afuture.risk import RiskConfig, RiskManager

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)
    expectations = _promote_synthetic_profile(monkeypatch, expectations)
    bootstrap = bootstrap_stress90(
        runtime_dir=runtime,
        through_day=through,
        expectations=expectations,
        write_artifacts=True,
    )
    through_timestamp = pd.Timestamp(datetime.strptime(through, "%Y%m%d"))
    next_target = (through_timestamp + pd.offsets.BDay(1)).strftime("%Y%m%d")
    index = pd.bdate_range(end=through_timestamp, periods=170)
    close = pd.DataFrame(
        {
            product: [100.0 * (1.001**row) for row in range(len(index))]
            for product in FROZEN_PRODUCTS
        },
        index=index,
    )
    ohlc_path = runtime / "directional_ohlc_cache.json"
    DirectionalOHLCCacheStore(ohlc_path).save(FROZEN_PRODUCTS, close / 1.001, close)
    catalog = [
        ContractInfo(
            f"{product}2612",
            PRODUCT_SESSION_MANIFEST[product].exchange,
            product,
            "2026-12-15",
        )
        for product in STRESS90_POLICY.oi_products
    ]

    class Broker:
        def __init__(self):
            self.trading_day = through
            self.observer = None

        def get_trading_day(self):
            return self.trading_day

        def get_contract_catalog(self):
            return list(catalog)

        def set_raw_tick_observer(self, observer):
            self.observer = observer

        def subscribe(self, symbol, exchange):
            del symbol, exchange

    broker = Broker()
    manager = Stress90DirectionalPortfolioManager(
        DirectionalConfig(enabled=True, policy="stress90", products=FROZEN_PRODUCTS),
        broker,
        RiskManager(RiskConfig(margin_estimate_buffer=1.25)),
        policy_state_path=bootstrap.state_path,
        seed_path=bootstrap.seed_path,
        oi_evidence_path=bootstrap.oi_evidence_path,
        ohlc_cache_path=ohlc_path,
    )

    class Base:
        def target_weights(self, open_prices, close_prices):
            del open_prices, close_prices
            return {
                product: (1.0 if product in {"A", "AG"} else 0.0) for product in FROZEN_PRODUCTS
            }

    manager.policy = Base()
    manager.bootstrap(through_timestamp.to_pydatetime().replace(tzinfo=_CHINA))
    oi_store = Stress90OiEvidenceStore(bootstrap.oi_evidence_path)
    initial_oi = oi_store.load_required_record()
    completed = next(item for item in initial_oi.state.completed if item.trading_day == through)
    preseeded_transition = ObservedTradingDayTransition(
        source_trading_day=through,
        target_trading_day=next_target,
        completed_oi_evidence_digest=completed.evidence_digest,
    )
    oi_store.save_state(
        replace(initial_oi.state, observed_transitions=(preseeded_transition,)),
        expected_sequence=initial_oi.sequence,
    )
    before_oi = oi_store.load_required_record()
    broker.trading_day = next_target

    first = manager._prepare_decision_for_current_day(next_target)
    second = manager._prepare_decision_for_current_day(next_target)

    after_state = Stress90PolicyStateStore(bootstrap.state_path).load_required_record()
    after_oi = Stress90OiEvidenceStore(bootstrap.oi_evidence_path).load_required_record()
    assert first == second
    assert first.previous_target_trading_day == through
    assert first.input_days == {"completed_close": through, "completed_oi": through}
    assert after_state.sequence == 2
    assert after_oi == before_oi
    assert after_oi.state.completed[0].source == "fixed_historical_60m"
    assert after_oi.state.observed_transitions == (preseeded_transition,)


def test_bootstrap_hashes_every_fixed_input_before_parsing(tmp_path: Path):
    from afuture.directional_stress90_bootstrap import (
        Stress90BootstrapError,
        bootstrap_stress90,
    )

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)
    with (runtime / "broad_daily_universe.csv").open("a", encoding="utf-8") as handle:
        handle.write("corrupt-after-fixed-hash\n")

    with pytest.raises(Stress90BootstrapError, match="SHA-256 mismatch"):
        bootstrap_stress90(
            runtime_dir=runtime,
            through_day=through,
            expectations=expectations,
            write_artifacts=False,
        )


def test_bootstrap_rejects_base_weight_drift_even_with_updated_input_hash(tmp_path: Path):
    from afuture.directional_stress90_bootstrap import (
        Stress90BootstrapError,
        bootstrap_stress90,
    )

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)
    path = runtime / "execution_aligned_weights.csv"
    weights = pd.read_csv(path)
    weights.loc[0, "weight"] = float(weights.loc[0, "weight"]) + 0.01
    weights.to_csv(path, index=False)
    expectations = _refreshed_expectations(runtime, expectations)

    with pytest.raises(Stress90BootstrapError, match="base policy parity"):
        bootstrap_stress90(
            runtime_dir=runtime,
            through_day=through,
            expectations=expectations,
            write_artifacts=False,
        )


def test_bootstrap_rejects_missing_supported_oi_coverage_not_as_zero(tmp_path: Path):
    from afuture.directional_stress90_bootstrap import (
        Stress90BootstrapError,
        bootstrap_stress90,
    )

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)
    paths = [
        runtime / "prior_two_year_broad_60m.csv",
        runtime / "two_year_broad_60m.csv",
    ]
    frames = [pd.read_csv(path) for path in paths]
    bars = pd.concat(frames, ignore_index=True)
    first_day = pd.to_datetime(bars["datetime"]).dt.normalize().min()
    bars = bars[
        ~((pd.to_datetime(bars["datetime"]).dt.normalize() == first_day) & (bars["product"] == "A"))
    ]
    split = len(bars) // 2
    bars.iloc[:split].to_csv(paths[0], index=False)
    bars.iloc[split:].to_csv(paths[1], index=False)
    expectations = _refreshed_expectations(runtime, expectations)

    with pytest.raises(Stress90BootstrapError, match="60m OI coverage"):
        bootstrap_stress90(
            runtime_dir=runtime,
            through_day=through,
            expectations=expectations,
            write_artifacts=False,
        )


def test_nonhistorical_profile_can_never_write_live_seed_or_state(tmp_path: Path):
    from afuture.directional_stress90_bootstrap import (
        Stress90BootstrapError,
        bootstrap_stress90,
    )

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)

    with pytest.raises(Stress90BootstrapError, match="official immutable historical profile"):
        bootstrap_stress90(
            runtime_dir=runtime,
            through_day=through,
            expectations=expectations,
            write_artifacts=True,
        )
    assert not (runtime / "stress90_bootstrap_seed.json").exists()
    assert not (runtime / "stress90_policy_state.json").exists()


def test_cli_exposes_bootstrap_arguments_and_does_not_require_ctp_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    from types import SimpleNamespace

    from afuture.cli import build_parser, run_command
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS

    parsed = build_parser().parse_args(
        [
            "stress90-bootstrap",
            "--config",
            "live.toml",
            "--runtime-dir",
            "runtime",
            "--through",
            "20260820",
        ]
    )
    assert parsed.runtime_dir == "runtime"
    assert parsed.through == "20260820"

    config_path = tmp_path / "bootstrap.toml"
    config_path.write_text(
        """
[system]
mode = "live"
initial_capital = 500000

[ctp]
environment = "test"
td_address = "tcp://trade.example"
md_address = "tcp://market.example"

[directional]
enabled = true
policy = "stress90"
products = [{products}]

[paths]
state = "{state}"
log = "{log}"
report = "{report}"
journal = "{journal}"
alert = "{alert}"
""".format(
            state=tmp_path / "state.json",
            log=tmp_path / "afuture.log",
            report=tmp_path / "report.json",
            journal=tmp_path / "audit.jsonl",
            alert=tmp_path / "alerts.jsonl",
            products=", ".join(f'"{product}"' for product in FROZEN_PRODUCTS),
        ),
        encoding="utf-8",
    )
    for name in ("AFUTURE_CTP_USER", "AFUTURE_CTP_PASSWORD", "AFUTURE_CTP_BROKER"):
        monkeypatch.delenv(name, raising=False)
    calls = []

    def fake_bootstrap_stress90(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(to_dict=lambda: {"historical_candidate_parity": True})

    monkeypatch.setattr(
        "afuture.directional_stress90_bootstrap.bootstrap_stress90",
        fake_bootstrap_stress90,
    )
    assert (
        run_command(
            [
                "stress90-bootstrap",
                "--config",
                str(config_path),
                "--runtime-dir",
                str(tmp_path / "runtime"),
                "--through",
                "20260820",
            ]
        )
        == 0
    )
    assert calls == [
        {
            "runtime_dir": str(tmp_path / "runtime"),
            "through_day": "20260820",
            "write_artifacts": True,
        }
    ]
    assert '"historical_candidate_parity": true' in capsys.readouterr().out
