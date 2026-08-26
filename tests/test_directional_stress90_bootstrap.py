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
        "ohlc": (DirectionalOHLCCacheStore, "save"),
        "activity": (DirectionalActivityStore, "save"),
        "seed": (Stress90SeedStore, "save_new"),
        "policy": (Stress90PolicyStateStore, "save"),
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


@pytest.mark.parametrize("incident", ["schema2", "invalid_chain"])
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
    if incident == "schema2":
        unsigned = {
            "kind": OI_EVIDENCE_KIND,
            "schema_version": 2,
            "sequence": 9,
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
        expected_error = "schema 2"
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
        assert not (runtime / name).exists()

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


def test_halted_raw_evidence_sidecar_uses_ctp_market_chain_with_zero_orders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from afuture.cli import _run_stress90_oi_collect
    from afuture.directional import DirectionalConfig
    from afuture.directional_sessions import PRODUCT_SESSION_MANIFEST
    from afuture.directional_stress90_bootstrap import bootstrap_stress90
    from afuture.directional_stress90_policy import STRESS90_POLICY
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.models import AccountSnapshot, ContractInfo

    runtime = tmp_path / "runtime"
    from afuture.runtime_lease import AccountExclusiveRuntimeLease

    lease_acquired = False
    original_acquire = AccountExclusiveRuntimeLease.acquire

    def acquire_then_mark(self):
        nonlocal lease_acquired
        original_acquire(self)
        lease_acquired = True

    monkeypatch.setattr(AccountExclusiveRuntimeLease, "acquire", acquire_then_mark)
    through, expectations = _write_synthetic_archive(runtime)
    expectations = _promote_synthetic_profile(monkeypatch, expectations)
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

    from afuture.cli import build_parser, main
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
        main(
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
