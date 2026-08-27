from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest


def _frames(products: tuple[str, ...], periods: int = 140):
    index = pd.date_range("2026-01-01", periods=periods, freq="D")
    values = [[100.0 + row + column for column in range(len(products))] for row in range(periods)]
    close = pd.DataFrame(values, index=index, columns=products)
    return close - 1.0, close


def _bound_authority(runtime: Path, *, account: str = "a" * 64):
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,
        AccountRuntimeRegistry,
    )
    from afuture.trading_day_evidence import TradingDayEvidenceStore

    runtime.mkdir(parents=True, exist_ok=True)
    registry = AccountRuntimeRegistry(runtime / ".account-runtime-registry.json")
    registry.initialize(strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION)
    epoch = "b" * 64
    operation = "c" * 64
    registry.bind_new(account, runtime, epoch, operation)
    store = TradingDayEvidenceStore(runtime / "ctp_trading_day_evidence.json")
    store.bind_for_lifecycle(
        transaction=SimpleNamespace(
            operation="activation",
            status="committed",
            transaction_id="d" * 64,
            operation_nonce=operation,
            source_account_identity_digest="",
            source_account_epoch="",
            account_identity_digest=account,
            trading_day="20260825",
            policy_target=SimpleNamespace(
                live_account_identity_digest=account,
                live_account_epoch=epoch,
            ),
        ),
        runtime_dir=runtime,
        binding_evidence=registry.require_binding_evidence(account, runtime, epoch),
    )
    return store, SimpleNamespace(
        live_account_identity_digest=account,
        live_account_epoch=epoch,
    )


def test_stress90_order_path_loads_only_verified_cache_and_requires_completed_day(tmp_path):
    from afuture.directional_ohlc_cache import DirectionalOHLCCacheStore
    from afuture.directional_ohlc_refresh import load_stress90_completed_ohlc

    products = ("A", "M")
    store = DirectionalOHLCCacheStore(tmp_path / "ohlc.json")
    open_prices, close = _frames(products)
    store.save(products, open_prices, close)

    entry = load_stress90_completed_ohlc(
        store,
        products=products,
        current_ctp_trading_day="20260525",
        authoritative_ctp_trading_day="20260525",
        required_completed_day=close.index[-1].strftime("%Y%m%d"),
    )
    assert entry.content_digest

    with pytest.raises(RuntimeError, match="required completed day"):
        load_stress90_completed_ohlc(
            store,
            products=products,
            current_ctp_trading_day="20260525",
            authoritative_ctp_trading_day="20260525",
            required_completed_day="20260524",
        )

    with pytest.raises(RuntimeError, match="mismatches Broker-derived"):
        load_stress90_completed_ohlc(
            store,
            products=products,
            current_ctp_trading_day="20260525",
            authoritative_ctp_trading_day="20260526",
        )


def test_explicit_refresh_is_append_only_and_rejects_current_day_or_revision(tmp_path):
    from afuture.directional_ohlc_cache import DirectionalOHLCCacheStore
    from afuture.directional_ohlc_refresh import refresh_directional_ohlc_cache

    products = ("A", "M")
    store = DirectionalOHLCCacheStore(tmp_path / "ohlc.json")
    open_prices, close = _frames(products)
    store.save(products, open_prices.iloc[:-1], close.iloc[:-1])

    refreshed = refresh_directional_ohlc_cache(
        store,
        provider=SimpleNamespace(
            load=lambda _products: SimpleNamespace(open=open_prices, close=close)
        ),
        products=products,
        current_ctp_trading_day="20260525",
        authoritative_ctp_trading_day="20260525",
    )
    assert refreshed.row_count == 140

    revised = open_prices.copy()
    revised.iloc[0, 0] += 1.0
    with pytest.raises(RuntimeError, match="revised values"):
        refresh_directional_ohlc_cache(
            store,
            provider=SimpleNamespace(
                load=lambda _products: SimpleNamespace(open=revised, close=close)
            ),
            products=products,
            current_ctp_trading_day="20260525",
            authoritative_ctp_trading_day="20260525",
        )

    with pytest.raises(RuntimeError, match="Broker-derived"):
        refresh_directional_ohlc_cache(
            store,
            provider=SimpleNamespace(
                load=lambda _products: SimpleNamespace(open=open_prices, close=close)
            ),
            products=products,
            current_ctp_trading_day="20260526",
            authoritative_ctp_trading_day="20260525",
        )

    future_open = pd.concat(
        [
            open_prices,
            pd.DataFrame([[999.0, 999.0]], index=[pd.Timestamp("2026-05-25")], columns=products),
        ]
    )
    future_close = pd.concat(
        [
            close,
            pd.DataFrame([[999.0, 999.0]], index=[pd.Timestamp("2026-05-25")], columns=products),
        ]
    )
    with pytest.raises(RuntimeError, match="current/future"):
        refresh_directional_ohlc_cache(
            store,
            provider=SimpleNamespace(
                load=lambda _products: SimpleNamespace(open=future_open, close=future_close)
            ),
            products=products,
            current_ctp_trading_day="20260525",
            authoritative_ctp_trading_day="20260525",
        )


@pytest.mark.parametrize("blocked_path", ["cache-symlink", "pending-witness"])
def test_refresh_rejects_unsafe_artifact_before_provider_effects(
    tmp_path: Path,
    blocked_path: str,
) -> None:
    from afuture.directional_ohlc_cache import (
        DirectionalOHLCCacheIntegrityError,
        DirectionalOHLCCacheStore,
    )
    from afuture.directional_ohlc_refresh import refresh_directional_ohlc_cache

    path = tmp_path / "directional_ohlc_cache.json"
    store = DirectionalOHLCCacheStore(path)
    if blocked_path == "cache-symlink":
        target = tmp_path / "other.json"
        target.write_text("not authority", encoding="utf-8")
        path.symlink_to(target)
    else:
        store.pending_path.write_text("ambiguous prior mutation", encoding="utf-8")
    provider_calls = 0

    def provider_factory():
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("provider must not be constructed")

    with pytest.raises(DirectionalOHLCCacheIntegrityError, match="symlink|pending"):
        refresh_directional_ohlc_cache(
            store,
            provider_factory=provider_factory,
            products=("A", "M"),
            current_ctp_trading_day="20260525",
            authoritative_ctp_trading_day="20260525",
        )
    assert provider_calls == 0


def test_refresh_holds_one_artifact_lock_through_provider_and_cache_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import contextmanager

    from afuture import directional_ohlc_cache as cache_module
    from afuture.directional_ohlc_cache import DirectionalOHLCCacheStore
    from afuture.directional_ohlc_refresh import refresh_directional_ohlc_cache

    products = ("A", "M")
    open_prices, close = _frames(products)
    store = DirectionalOHLCCacheStore(tmp_path / "directional_ohlc_cache.json")
    lock_depth = 0
    provider_saw_lock = False
    commit_saw_lock = False
    real_lock = cache_module.durable_file_lock
    real_commit = store.commit_encoded_unlocked

    @contextmanager
    def tracked_lock(path):
        nonlocal lock_depth
        with real_lock(path) as canonical:
            lock_depth += 1
            try:
                yield canonical
            finally:
                lock_depth -= 1

    def provider_factory():
        nonlocal provider_saw_lock
        provider_saw_lock = lock_depth == 1
        return SimpleNamespace(
            load=lambda _products: SimpleNamespace(open=open_prices, close=close)
        )

    def tracked_commit(*args, **kwargs):
        nonlocal commit_saw_lock
        commit_saw_lock = lock_depth == 1
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(cache_module, "durable_file_lock", tracked_lock)
    monkeypatch.setattr(store, "commit_encoded_unlocked", tracked_commit)
    refresh_directional_ohlc_cache(
        store,
        provider_factory=provider_factory,
        products=products,
        current_ctp_trading_day="20260525",
        authoritative_ctp_trading_day="20260525",
    )

    assert provider_saw_lock is True
    assert commit_saw_lock is True
    assert lock_depth == 0


def test_ambiguous_cache_truncate_preserves_pending_witness_and_blocks_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from afuture.directional_ohlc_cache import (
        DirectionalOHLCCacheIntegrityError,
        DirectionalOHLCCacheStore,
    )
    from afuture.directional_ohlc_refresh import refresh_directional_ohlc_cache

    products = ("A", "M")
    open_prices, close = _frames(products)
    store = DirectionalOHLCCacheStore(tmp_path / "directional_ohlc_cache.json")
    store.save(products, open_prices.iloc[:-1], close.iloc[:-1])
    real_ftruncate = os.ftruncate
    provider_calls = 0

    def truncate_then_fail(descriptor: int, length: int) -> None:
        real_ftruncate(descriptor, length)
        raise OSError("injected ambiguous truncate failure")

    def provider_factory():
        nonlocal provider_calls
        provider_calls += 1
        return SimpleNamespace(
            load=lambda _products: SimpleNamespace(open=open_prices, close=close)
        )

    monkeypatch.setattr(os, "ftruncate", truncate_then_fail)
    with pytest.raises(DirectionalOHLCCacheIntegrityError, match="commit failed"):
        refresh_directional_ohlc_cache(
            store,
            provider_factory=provider_factory,
            products=products,
            current_ctp_trading_day="20260525",
            authoritative_ctp_trading_day="20260525",
        )
    assert provider_calls == 1
    assert store.pending_path.is_file()

    with pytest.raises(DirectionalOHLCCacheIntegrityError, match="pending"):
        refresh_directional_ohlc_cache(
            store,
            provider_factory=provider_factory,
            products=products,
            current_ctp_trading_day="20260525",
            authoritative_ctp_trading_day="20260525",
        )
    assert provider_calls == 1
    with pytest.raises(DirectionalOHLCCacheIntegrityError, match="pending"):
        store.load(products)
    with pytest.raises(DirectionalOHLCCacheIntegrityError, match="pending"):
        store.save(products, open_prices, close)


@pytest.mark.parametrize("restoration_fsync_failure", [False, True])
def test_pending_cleanup_failure_restores_or_retains_a_durable_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restoration_fsync_failure: bool,
) -> None:
    from afuture.directional_ohlc_cache import (
        DirectionalOHLCCacheIntegrityError,
        DirectionalOHLCCacheStore,
    )

    products = ("A", "M")
    open_prices, close = _frames(products)
    store = DirectionalOHLCCacheStore(tmp_path / "directional_ohlc_cache.json")
    store.save(products, open_prices.iloc[:-1], close.iloc[:-1])
    real_fsync = os.fsync
    directory_fsyncs = 0

    def fail_final_cleanup_and_optional_restoration(descriptor: int) -> None:
        nonlocal directory_fsyncs
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_fsyncs += 1
            if directory_fsyncs == 4 or (restoration_fsync_failure and directory_fsyncs == 5):
                real_fsync(descriptor)
                raise OSError(f"injected directory fsync {directory_fsyncs}")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_final_cleanup_and_optional_restoration)
    expected = "restoration failed" if restoration_fsync_failure else "cleanup failed"
    with pytest.raises(DirectionalOHLCCacheIntegrityError, match=expected):
        store.save(products, open_prices, close)

    assert directory_fsyncs >= 5
    assert store.pending_path.exists() or store.cleanup_guard_path.exists()
    with pytest.raises(DirectionalOHLCCacheIntegrityError, match="pending"):
        store.load(products)


def test_cache_refresh_cli_reacquires_lease_when_authoritative_account_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.cli import _run_directional_ohlc_refresh
    from afuture.runtime_lease import AccountExclusiveRuntimeLease

    runtime = tmp_path / "runtime"
    account_a = "1" * 64
    account_b = "a" * 64
    _store, policy_b = _bound_authority(runtime, account=account_b)
    policy_a = SimpleNamespace(
        live_account_identity_digest=account_a,
        live_account_epoch="2" * 64,
    )
    policy_reads = iter((policy_a, policy_b, policy_b))

    class SequencedPolicyStore:
        def __init__(self, _path: Path) -> None:
            pass

        def load_required(self):
            return next(policy_reads)

    acquired_accounts: list[str] = []

    class RecordingLease(AccountExclusiveRuntimeLease):
        def acquire(self) -> None:
            acquired_accounts.append(self.account_identity_digest)
            super().acquire()

    provider_calls = 0
    open_prices, close = _frames(("A", "M"))

    def provider_factory():
        nonlocal provider_calls
        provider_calls += 1
        assert acquired_accounts[-1] == account_b
        return SimpleNamespace(
            load=lambda _products: SimpleNamespace(open=open_prices, close=close)
        )

    monkeypatch.setattr(
        "afuture.directional_stress90_state.Stress90PolicyStateStore",
        SequencedPolicyStore,
    )
    monkeypatch.setattr("afuture.runtime_lease.AccountExclusiveRuntimeLease", RecordingLease)
    monkeypatch.setattr(
        "afuture.execution_aligned_runtime.SinaContinuousOHLCProvider",
        provider_factory,
    )
    config = SimpleNamespace(
        state_path=str(runtime / "state.json"),
        directional=SimpleNamespace(policy="stress90", products=("A", "M")),
    )
    args = SimpleNamespace(
        cache="",
        trading_day_evidence="",
        current_trading_day="20260825",
    )

    assert _run_directional_ohlc_refresh(config, args) == 0
    assert acquired_accounts == [account_a, account_b]
    assert provider_calls == 1


def test_cache_refresh_cli_rejects_evidence_symlink_swap_before_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture import trading_day_evidence as evidence_module
    from afuture.cli import _run_directional_ohlc_refresh
    from afuture.trading_day_evidence import TradingDayEvidenceError

    runtime = tmp_path / "runtime"
    evidence_store, policy = _bound_authority(runtime)
    target = runtime / "racing-evidence-target.json"
    target.write_bytes(evidence_store.path.read_bytes())
    reads = 0
    swapped = False
    real_os_open = os.open
    real_path_open = Path.open

    def race_on_second_read() -> None:
        nonlocal reads, swapped
        reads += 1
        if reads == 2:
            evidence_store.path.unlink()
            evidence_store.path.symlink_to(target)
            swapped = True

    def racing_os_open(path, flags, *args, **kwargs):
        if Path(path) == evidence_store.path:
            race_on_second_read()
        return real_os_open(path, flags, *args, **kwargs)

    def racing_path_open(self: Path, *args, **kwargs):
        if self == evidence_store.path:
            race_on_second_read()
        return real_path_open(self, *args, **kwargs)

    class PolicyStore:
        def __init__(self, _path: Path) -> None:
            pass

        def load_required(self):
            return policy

    provider_calls = 0

    def provider_factory():
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("provider must not be constructed after evidence path race")

    monkeypatch.setattr(evidence_module.os, "open", racing_os_open)
    monkeypatch.setattr(Path, "open", racing_path_open)
    monkeypatch.setattr(
        "afuture.directional_stress90_state.Stress90PolicyStateStore",
        PolicyStore,
    )
    monkeypatch.setattr(
        "afuture.execution_aligned_runtime.SinaContinuousOHLCProvider",
        provider_factory,
    )
    config = SimpleNamespace(
        state_path=str(runtime / "state.json"),
        directional=SimpleNamespace(policy="stress90", products=("A", "M")),
    )
    args = SimpleNamespace(
        cache="",
        trading_day_evidence="",
        current_trading_day="20260825",
    )

    with pytest.raises(TradingDayEvidenceError, match="opened safely|regular file"):
        _run_directional_ohlc_refresh(config, args)
    assert swapped is True
    assert provider_calls == 0


def test_cache_refresh_cli_is_explicit_and_never_requires_ctp_credentials(
    tmp_path,
    monkeypatch,
    capsys,
):
    import afuture.cli as cli

    parsed = cli.build_parser().parse_args(
        [
            "directional-ohlc-refresh",
            "--config",
            "stress90.toml",
            "--current-trading-day",
            "20260825",
        ]
    )
    assert parsed.current_trading_day == "20260825"

    config = SimpleNamespace(
        state_path=str(tmp_path / "runtime" / "directional_state.json"),
        directional=SimpleNamespace(policy="stress90", products=("A", "M")),
    )
    credential_flags = []

    def fake_load_config(_path, *, require_ctp_credentials):
        credential_flags.append(require_ctp_credentials)
        return config

    calls = []

    def fake_refresh(
        store,
        *,
        provider_factory,
        products,
        current_ctp_trading_day,
        authoritative_ctp_trading_day,
    ):
        calls.append(
            (
                store.path,
                provider_factory,
                products,
                current_ctp_trading_day,
                authoritative_ctp_trading_day,
            )
        )
        return SimpleNamespace(
            latest_date=pd.Timestamp("2026-08-24").date(),
            row_count=170,
            content_digest="a" * 64,
        )

    monkeypatch.setattr(cli, "load_config", fake_load_config)
    monkeypatch.setattr(
        "afuture.directional_ohlc_refresh.refresh_directional_ohlc_cache",
        fake_refresh,
    )
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,
        AccountRuntimeRegistry,
    )
    from afuture.trading_day_evidence import TradingDayEvidenceStore

    registry = AccountRuntimeRegistry(tmp_path / "runtime" / ".account-runtime-registry.json")
    registry.initialize(strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION)
    policy_state = SimpleNamespace(
        live_account_identity_digest=None,
        live_account_epoch=None,
    )
    monkeypatch.setattr(
        "afuture.directional_stress90_state.Stress90PolicyStateStore",
        lambda _path: SimpleNamespace(load_required=lambda: policy_state),
    )
    TradingDayEvidenceStore(
        tmp_path / "runtime" / "ctp_trading_day_evidence.json"
    ).save_observation(
        trading_day="20260825",
        account_identity_digest="a" * 64,
        runtime_dir=tmp_path / "runtime",
        policy_state=policy_state,
        registry=registry,
    )

    assert (
        cli.main(
            [
                "directional-ohlc-refresh",
                "--config",
                "stress90.toml",
                "--current-trading-day",
                "20260825",
            ]
        )
        == 0
    )
    assert credential_flags == [False]
    assert calls[0][0] == tmp_path / "runtime" / "directional_ohlc_cache.json"
    assert calls[0][2:] == (("A", "M"), "20260825", "20260825")
    assert '"latest_completed_day": "20260824"' in capsys.readouterr().out


def test_cache_refresh_cli_rejects_policy_epoch_mismatch_before_provider_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.account_runtime_registry import (
        ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,
        AccountRuntimeRegistry,
    )
    from afuture.cli import _run_directional_ohlc_refresh
    from afuture.trading_day_evidence import TradingDayEvidenceStore

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    registry = AccountRuntimeRegistry(runtime / ".account-runtime-registry.json")
    registry.initialize(strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION)
    account = "a" * 64
    epoch = "b" * 64
    operation = "c" * 64
    registry.bind_new(account, runtime, epoch, operation)
    TradingDayEvidenceStore(runtime / "ctp_trading_day_evidence.json").bind_for_lifecycle(
        transaction=SimpleNamespace(
            operation="activation",
            status="committed",
            transaction_id="d" * 64,
            operation_nonce=operation,
            source_account_identity_digest="",
            source_account_epoch="",
            account_identity_digest=account,
            trading_day="20260825",
            policy_target=SimpleNamespace(
                live_account_identity_digest=account,
                live_account_epoch=epoch,
            ),
        ),
        runtime_dir=runtime,
        binding_evidence=registry.require_binding_evidence(account, runtime, epoch),
    )
    mismatched_policy = SimpleNamespace(
        live_account_identity_digest=account,
        live_account_epoch="e" * 64,
    )
    monkeypatch.setattr(
        "afuture.directional_stress90_state.Stress90PolicyStateStore",
        lambda _path: SimpleNamespace(load_required=lambda: mismatched_policy),
    )
    provider_constructions = 0

    def provider_factory():
        nonlocal provider_constructions
        provider_constructions += 1
        raise AssertionError("provider must not be constructed")

    monkeypatch.setattr(
        "afuture.execution_aligned_runtime.SinaContinuousOHLCProvider",
        provider_factory,
    )
    config = SimpleNamespace(
        state_path=str(runtime / "state.json"),
        directional=SimpleNamespace(policy="stress90", products=("A", "M")),
    )
    args = SimpleNamespace(
        cache="",
        trading_day_evidence="",
        current_trading_day="20260825",
    )

    with pytest.raises(RuntimeError, match="policy account/epoch mismatch"):
        _run_directional_ohlc_refresh(config, args)
    assert provider_constructions == 0
