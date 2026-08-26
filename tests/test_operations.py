import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import afuture.operations as operations
from afuture.directional import DirectionalConfig
from afuture.directional_activity import (
    ContractActivity,
    DirectionalActivitySnapshot,
    DirectionalActivityStore,
)
from afuture.directional_ohlc_cache import DirectionalOHLCCacheStore
from afuture.models import (
    AccountSnapshot,
    ContractInfo,
    ContractPosition,
    ContractSpec,
    FeeSpec,
    RuntimeMode,
    Tick,
)
from afuture.operations import build_doctor_report, build_local_status
from afuture.risk import RiskConfig
from afuture.state import RuntimeState, StateStore


def _config(tmp_path: Path, *, directional: bool = False):
    spec = ContractSpec("m2609", "DCE", 10, 1, 0.12, 0.12, FeeSpec(open_fixed=1))
    return SimpleNamespace(
        state_path=str(tmp_path / "state.json"),
        log_path=str(tmp_path / "afuture.log"),
        report_path=str(tmp_path / "report.json"),
        journal_path=str(tmp_path / "audit.jsonl"),
        alert_path=str(tmp_path / "alerts.jsonl"),
        contracts={spec.symbol: spec},
        directional=DirectionalConfig(
            enabled=directional,
            products=("M",) if directional else (),
        ),
        risk=RiskConfig(max_margin_ratio=0.35, min_available_ratio=0.25),
    )


def _stress90_config(tmp_path: Path):
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS

    config = _config(tmp_path)
    config.directional = DirectionalConfig(
        enabled=True,
        policy="stress90",
        products=FROZEN_PRODUCTS,
        account_exclusive=True,
    )
    config.risk = RiskConfig(
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
        max_daily_loss_ratio=0.05,
        max_total_drawdown_ratio=0.30,
        max_contract_volume=35,
        margin_estimate_buffer=1.25,
    )
    return config


def _write_stress90_seed_state(tmp_path: Path):
    from dataclasses import replace

    from afuture.directional_stress90_policy import STRESS90_POLICY, Stress90CandidateState
    from afuture.directional_stress90_state import (
        Stress90BootstrapSeed,
        Stress90PolicyState,
        Stress90PolicyStateStore,
        Stress90SeedStore,
    )

    candidate = replace(
        Stress90CandidateState.initial(STRESS90_POLICY),
        last_completed_target_day="20260824",
        last_decision_digest="a" * 64,
        completed_concentrations=(0.5,),
    )
    seed = Stress90BootstrapSeed.from_candidate_state(
        candidate,
        bootstrap_source_manifest={"fixed": "1" * 64},
        bootstrap_through_day="20260824",
        last_completed_input_day="20260821",
    )
    seed_path = tmp_path / "stress90_bootstrap_seed.json"
    state_path = tmp_path / "stress90_policy_state.json"
    Stress90SeedStore(seed_path).save_new(seed)
    Stress90PolicyStateStore(state_path).save(Stress90PolicyState.from_seed(seed))
    return seed_path, state_path


def test_status_rejects_active_execution_intent_from_stale_account_epoch(
    tmp_path: Path,
) -> None:
    from afuture.directional_stress90_execution import (
        Stress90ExecutionIntentStore,
        prepare_stress90_execution_intent,
    )
    from afuture.directional_stress90_state import (
        Stress90PolicyStateStore,
        bind_stress90_account_identity,
    )

    config = _stress90_config(tmp_path)
    _write_stress90_seed_state(tmp_path)
    policy_store = Stress90PolicyStateStore(tmp_path / "stress90_policy_state.json")
    policy = policy_store.load_required_record()
    policy_store.save(
        bind_stress90_account_identity(
            policy.state,
            "b" * 64,
            account_epoch="e" * 64,
        ),
        expected_sequence=policy.sequence,
    )
    prepare_stress90_execution_intent(
        Stress90ExecutionIntentStore(tmp_path / "stress90_execution_intent.json"),
        target_trading_day="20260825",
        daily_decision_digest="a" * 64,
        account_identity_digest="b" * 64,
        account_epoch="f" * 64,
        current_lots={},
        margin_fitted_lots={},
        symbol_products={},
    )

    report = build_local_status(config, min_free_bytes=1)

    assert not _checks(report)["stress90_execution_intent_integrity"]
    assert (
        "stress90_execution_intent_lifecycle"
        in (report.facts["stress90"]["remaining_blocker_reasons"])
    )


def _account() -> AccountSnapshot:
    return AccountSnapshot(500_000, 500_000, 440_000, 60_000, 0, 0, "20260825")


def _catalog() -> list[ContractInfo]:
    return [ContractInfo("m2609", "DCE", "M", "2026-12-31", "2026-01-01")]


def _checks(report) -> dict[str, bool]:
    return {item.name: item.passed for item in report.checks}


def _resign_activity(envelope: dict) -> None:
    unsigned = {key: value for key, value in envelope.items() if key != "checksum"}
    encoded = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    envelope["checksum"] = sha256(encoded).hexdigest()


def _write_ohlc_cache(tmp_path: Path, *, end: str = "2026-08-24") -> str:
    dates = pd.date_range(end=end, periods=140, freq="B")
    close = pd.DataFrame({"M": range(100, 240)}, index=dates, dtype=float)
    open_prices = close.shift(1).fillna(close.iloc[0])
    entry = DirectionalOHLCCacheStore(tmp_path / "directional_ohlc_cache.json").save(
        ("M",),
        open_prices,
        close,
    )
    return entry.content_digest


def _resign_ohlc_cache(envelope: dict) -> None:
    content = envelope["content"]
    envelope["content_digest"] = sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    unsigned = {key: value for key, value in envelope.items() if key != "checksum"}
    envelope["checksum"] = sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _write_completed_activity(tmp_path: Path) -> None:
    DirectionalActivityStore(tmp_path / "directional_activity.json").save(
        DirectionalActivitySnapshot(
            "20260824",
            {
                "m2609": ContractActivity(
                    "m2609",
                    "DCE",
                    "M",
                    "20260824",
                    10_000,
                    20_000,
                    datetime(2026, 8, 24, tzinfo=timezone.utc),
                )
            },
        )
    )


def test_local_status_reports_current_and_previous_verified_state(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = StateStore(config.state_path)
    store.save(RuntimeState(trading_day="20260824", last_order_id="old"))
    store.save(
        RuntimeState(
            trading_day="20260825",
            reconciled=True,
            metadata_verified=True,
            last_order_id="new",
        )
    )

    report = build_local_status(config, min_free_bytes=1)
    payload = report.to_dict()

    assert report.passed
    assert payload["facts"]["state"]["trading_day"] == "20260825"
    assert payload["facts"]["state"]["last_order_id"] == "new"
    assert payload["facts"]["previous_state"]["trading_day"] == "20260824"
    assert payload["facts"]["paths"]["audit"]["size_bytes"] == 0


def test_local_status_fails_closed_on_corrupt_current_but_exposes_previous(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = StateStore(config.state_path)
    store.save(RuntimeState(trading_day="20260824"))
    store.save(RuntimeState(trading_day="20260825"))
    store.path.write_text("{broken", encoding="utf-8")

    report = build_local_status(config, min_free_bytes=1)

    assert not report.passed
    assert not _checks(report)["state_integrity"]
    assert report.facts["state"]["valid"] is False
    assert report.facts["previous_state"]["valid"] is True


def test_local_status_fails_closed_when_current_is_missing_with_previous_evidence(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = StateStore(config.state_path)
    store.save(RuntimeState(kill_switch=True))
    store.save(RuntimeState(kill_switch=True, kill_reason="checkpoint"))
    store.path.unlink()

    report = build_local_status(config, min_free_bytes=1)

    assert not report.passed
    assert not _checks(report)["state_integrity"]
    assert "current runtime state is missing" in report.facts["state"]["error"]


def test_stress90_status_surfaces_policy_account_and_data_blockers(tmp_path: Path) -> None:
    from afuture.directional_stress90_policy import STRESS90_POLICY

    config = _stress90_config(tmp_path)
    _write_stress90_seed_state(tmp_path)

    report = build_local_status(config, min_free_bytes=1)
    facts = report.facts["stress90"]

    assert not report.passed
    assert facts["policy_id"] == STRESS90_POLICY.policy_id
    assert facts["policy_version"] == STRESS90_POLICY.definition_version
    assert facts["policy_definition_digest"] == STRESS90_POLICY.policy_definition_digest
    assert facts["bootstrap_through_day"] == "20260824"
    assert facts["last_completed_target_day"] == "20260824"
    assert facts["completed_account_wealth"] == 1.0
    assert facts["completed_account_high_watermark"] == 1.0
    assert facts["completed_drawdown"] == 0.0
    assert facts["drawdown_reserve_freeze"] is False
    assert facts["live_eligibility"] is False
    assert "stress90_oi_evidence" in facts["remaining_blocker_reasons"]


def test_stress90_status_never_falls_back_to_previous_policy_state(tmp_path: Path) -> None:
    from afuture.directional_stress90_state import Stress90PolicyStateStore

    config = _stress90_config(tmp_path)
    _seed_path, state_path = _write_stress90_seed_state(tmp_path)
    store = Stress90PolicyStateStore(state_path)
    first = store.load_required()
    store.save(first)
    state_path.write_text("{corrupt", encoding="utf-8")

    report = build_local_status(config, min_free_bytes=1)

    assert not _checks(report)["stress90_policy_state_integrity"]
    assert report.facts["stress90"]["policy_state_valid"] is False
    assert report.facts["stress90"]["last_completed_target_day"] is None


def test_stress90_status_fails_closed_on_runtime_policy_identity_mismatch(tmp_path: Path):
    from afuture.directional_policy_activation import POLICY_IDENTITY_STATE_KEY

    config = _stress90_config(tmp_path)
    _write_stress90_seed_state(tmp_path)
    StateStore(config.state_path).save(
        RuntimeState(
            strategy_states={
                POLICY_IDENTITY_STATE_KEY: {
                    "policy_id": "execution_aligned",
                    "policy_definition_digest": "",
                }
            }
        )
    )

    report = build_local_status(config, min_free_bytes=1)

    assert not _checks(report)["stress90_runtime_policy_identity"]
    assert (
        "stress90_runtime_policy_identity" in report.facts["stress90"]["remaining_blocker_reasons"]
    )


def test_stress90_live_cost_gate_reports_each_fee_tick_spread_and_depth_component():
    from afuture.operations import estimate_stress90_contract_cost

    tick = Tick(
        "m2609",
        "DCE",
        datetime(2026, 8, 24, 13, 0, tzinfo=timezone.utc),
        99.0,
        101.0,
        100.0,
        50.0,
        40.0,
        "20260825",
        volume=10_000.0,
        open_interest=20_000.0,
    )
    spec = ContractSpec(
        "m2609",
        "DCE",
        10.0,
        1.0,
        0.12,
        0.12,
        FeeSpec(open_fixed=2.0, close_fixed=3.0, close_today_fixed=8.0),
    )

    estimate = estimate_stress90_contract_cost(tick, spec, hurdle_bps=15.0)

    assert estimate["open_fee_bps"] == pytest.approx(20.0)
    assert estimate["close_yesterday_fee_bps"] == pytest.approx(30.0)
    assert estimate["close_today_fee_bps"] == pytest.approx(80.0)
    assert estimate["one_tick_bps"] == pytest.approx(100.0)
    assert estimate["bid_ask_bps"] == pytest.approx(200.0)
    assert estimate["bid_depth"] == 50.0
    assert estimate["ask_depth"] == 40.0
    assert estimate["historical_15bp_compatible"] is False


def test_stress90_live_cost_gate_blocks_expensive_close_route_even_when_open_is_cheap():
    tick = Tick(
        "m2609",
        "DCE",
        datetime(2026, 8, 25, tzinfo=timezone.utc),
        9999.9,
        10000.1,
        10000.0,
        100.0,
        100.0,
        "20260825",
    )
    spec = ContractSpec(
        "m2609",
        "DCE",
        10.0,
        0.2,
        0.12,
        0.12,
        FeeSpec(close_today_fixed=200.0),
    )

    estimate = operations.estimate_stress90_contract_cost(tick, spec)

    assert estimate["open_route_bps"] < 15.0
    assert estimate["close_today_route_bps"] > 15.0
    assert estimate["minimum_reasonable_one_way_bps"] == estimate["close_today_route_bps"]
    assert estimate["historical_15bp_compatible"] is False


def test_stress90_doctor_keeps_orders_zero_and_blocks_cost_above_historical_hurdle(
    tmp_path: Path,
):
    config = _stress90_config(tmp_path)
    _write_stress90_seed_state(tmp_path)
    tick = Tick(
        "m2609",
        "DCE",
        datetime(2026, 8, 24, 13, 0, tzinfo=timezone.utc),
        2999.0,
        3001.0,
        3000.0,
        100.0,
        100.0,
        "20260825",
        volume=10_000.0,
        open_interest=20_000.0,
    )
    expensive = ContractSpec(
        "m2609",
        "DCE",
        10.0,
        1.0,
        0.12,
        0.12,
        FeeSpec(open_fixed=100.0),
    )

    report = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=[],
        active_order_count=0,
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata={"m2609": expensive},
        quotes={"m2609": tick},
        min_free_bytes=1,
    )

    assert report.facts["broker"]["orders_sent"] == 0
    assert report.facts["stress90"]["stress90_ready"] is False
    assert not _checks(report)["stress90_live_cost_compatibility"]
    assert report.facts["stress90"]["live_costs"]["m2609"]["historical_15bp_compatible"] is False
    assert "historical_15bp_cost" in report.facts["stress90"]["remaining_blocker_reasons"]


def test_stress90_doctor_accepts_existing_matching_activation_identity(tmp_path: Path):
    from afuture.directional_policy_activation import POLICY_IDENTITY_STATE_KEY
    from afuture.directional_stress90_policy import STRESS90_POLICY
    from afuture.directional_stress90_state import Stress90SeedStore

    config = _stress90_config(tmp_path)
    seed_path, _state_path = _write_stress90_seed_state(tmp_path)
    seed = Stress90SeedStore(seed_path).load_required()
    StateStore(config.state_path).save(
        RuntimeState(
            reconciled=True,
            metadata_verified=True,
            runtime_mode=RuntimeMode.RUNNING.value,
            strategy_states={
                POLICY_IDENTITY_STATE_KEY: {
                    "policy_id": STRESS90_POLICY.policy_id,
                    "policy_definition_digest": STRESS90_POLICY.policy_definition_digest,
                    "products_manifest_digest": STRESS90_POLICY.products_manifest_digest,
                    "bootstrap_seed_digest": seed.seed_digest,
                    "operator_reason": "commissioned",
                }
            },
        )
    )
    tick = Tick(
        "m2609",
        "DCE",
        datetime(2026, 8, 25, tzinfo=timezone.utc),
        2999.9,
        3000.1,
        3000,
        100,
        100,
        "20260825",
    )
    spec = ContractSpec("m2609", "DCE", 10, 0.2, 0.12, 0.12)

    report = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=[],
        active_order_count=0,
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata={"m2609": spec},
        quotes={"m2609": tick},
        min_free_bytes=1,
    )

    assert _checks(report)["stress90_first_activation_gates"]
    assert (
        "stress90_first_activation_gates"
        not in report.facts["stress90"]["remaining_blocker_reasons"]
    )


def test_stress90_doctor_previews_shared_integer_planner_stages(tmp_path: Path):
    from afuture.directional_stress90_policy import STRESS90_POLICY
    from afuture.directional_stress90_state import (
        Stress90DecisionInputs,
        Stress90PolicyStateStore,
        prepare_stress90_decision,
    )

    config = _stress90_config(tmp_path)
    _write_stress90_seed_state(tmp_path)
    _write_completed_activity(tmp_path)
    policy_store = Stress90PolicyStateStore(tmp_path / "stress90_policy_state.json")
    base = {product: 0.0 for product in STRESS90_POLICY.products}
    base["M"] = 1.0
    flow = {product: 0 for product in STRESS90_POLICY.oi_products}
    flow["M"] = 1
    close_path = [100.0]
    for _ in range(20):
        close_path.append(close_path[-1] * 1.001)
    prepare_stress90_decision(
        policy_store,
        Stress90DecisionInputs(
            previous_target_trading_day="20260824",
            target_trading_day="20260825",
            base_weights=base,
            completed_close_history={
                product: tuple(close_path) for product in STRESS90_POLICY.products
            },
            completed_oi_flow=flow,
            completed_close_day="20260824",
            completed_oi_day="20260824",
        ),
    )
    tick = Tick(
        "m2609",
        "DCE",
        datetime(2026, 8, 25, tzinfo=timezone.utc),
        2999,
        3001,
        3000,
        100,
        100,
        "20260825",
    )
    spec = ContractSpec("m2609", "DCE", 10, 1, 0.12, 0.12)

    report = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=[],
        active_order_count=0,
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata={"m2609": spec},
        quotes={"m2609": tick},
        min_free_bytes=1,
    )

    facts = report.facts["stress90"]
    assert facts["integer_target_stages"]["raw_integer_lots"] == {"m2609": 16}
    assert facts["integer_target_stages"]["margin_fitted_lots"] == {"m2609": 16}
    assert facts["target_lots"] == {"m2609": 16}
    assert facts["actual_gross"] == 0.0
    assert facts["target_gross"] == pytest.approx(0.96)
    assert facts["integer_tracking_error"] == pytest.approx(0.04)
    assert facts["single_product_actual_concentration"] == 1.0


def test_local_status_reports_invalid_utf8_previous_as_warning(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = StateStore(config.state_path)
    store.save(RuntimeState(trading_day="20260824"))
    store.save(RuntimeState(trading_day="20260825"))
    store.previous_path.write_bytes(b"\xff")

    report = build_local_status(config, min_free_bytes=1)

    assert report.passed
    assert report.facts["previous_state"]["valid"] is False
    assert "invalid state UTF-8" in report.warnings[0]


def test_local_status_rejects_runtime_target_or_parent_that_is_not_a_directory(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    Path(config.log_path).mkdir()

    report = build_local_status(config, min_free_bytes=1)

    assert not report.passed
    assert not _checks(report)["runtime_paths_writable"]


def test_local_status_rejects_dangling_runtime_symlink(tmp_path: Path) -> None:
    config = _config(tmp_path)
    Path(config.alert_path).symlink_to(tmp_path / "missing-alert-target")

    report = build_local_status(config, min_free_bytes=1)

    assert not report.passed
    assert "symlink is not allowed" in next(
        item.detail for item in report.checks if item.name == "runtime_paths_writable"
    )


def test_local_status_requires_searchable_ancestors_and_writable_report(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _config(tmp_path)
    report_path = Path(config.report_path)
    report_path.write_text("existing", encoding="utf-8")
    real_access = operations.os.access

    def selective_access(path, mode):
        candidate = Path(path)
        if candidate == tmp_path and mode == operations.os.W_OK | operations.os.X_OK:
            return False
        if candidate == report_path and mode == operations.os.W_OK:
            return False
        return real_access(path, mode)

    monkeypatch.setattr(operations.os, "access", selective_access)

    report = build_local_status(config, min_free_bytes=1)

    detail = next(item.detail for item in report.checks if item.name == "runtime_paths_writable")
    assert not report.passed
    assert "ancestor is not writable/searchable" in detail
    assert "write target is not writable" in detail


def test_local_status_surfaces_verified_directional_ohlc_cache_metadata(tmp_path: Path) -> None:
    config = _config(tmp_path, directional=True)
    digest = _write_ohlc_cache(tmp_path)

    report = build_local_status(config, min_free_bytes=1)

    assert _checks(report)["directional_ohlc_cache_integrity"]
    assert report.facts["directional_ohlc_cache"] == {
        "path": str(tmp_path / "directional_ohlc_cache.json"),
        "present": True,
        "valid": True,
        "schema_version": 1,
        "content_digest": digest,
        "latest_date": "2026-08-24",
        "products": ["M"],
        "product_count": 1,
        "row_count": 140,
    }


def test_local_status_fails_closed_on_tampered_directional_ohlc_cache(tmp_path: Path) -> None:
    config = _config(tmp_path, directional=True)
    _write_ohlc_cache(tmp_path)
    cache_path = tmp_path / "directional_ohlc_cache.json"
    envelope = json.loads(cache_path.read_text(encoding="utf-8"))
    envelope["content"]["close"][0][0] += 1.0
    cache_path.write_text(json.dumps(envelope), encoding="utf-8")

    report = build_local_status(config, min_free_bytes=1)

    assert not _checks(report)["directional_ohlc_cache_integrity"]
    assert report.facts["directional_ohlc_cache"]["valid"] is False
    assert "digest mismatch" in report.facts["directional_ohlc_cache"]["error"]


@pytest.mark.parametrize(
    ("field", "malformed"),
    [("date", "0001-01-01"), ("value", 10**1000)],
)
def test_status_and_doctor_fail_closed_on_cache_range_corruption(
    tmp_path: Path,
    field: str,
    malformed: object,
) -> None:
    config = _config(tmp_path, directional=True)
    _write_completed_activity(tmp_path)
    _write_ohlc_cache(tmp_path)
    cache_path = tmp_path / "directional_ohlc_cache.json"
    envelope = json.loads(cache_path.read_text(encoding="utf-8"))
    if field == "date":
        envelope["content"]["dates"][0] = malformed
    else:
        envelope["content"]["close"][0][0] = malformed
    _resign_ohlc_cache(envelope)
    cache_path.write_text(json.dumps(envelope), encoding="utf-8")

    status = build_local_status(config, min_free_bytes=1)
    doctor = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=[],
        active_order_count=0,
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata=config.contracts,
        min_free_bytes=1,
    )

    assert not _checks(status)["directional_ohlc_cache_integrity"]
    assert status.facts["directional_ohlc_cache"]["valid"] is False
    assert not _checks(doctor)["directional_ohlc_cache_integrity"]
    assert not _checks(doctor)["directional_ohlc_cache_required_day"]


def test_status_and_doctor_full_audit_ctp_order_journal_cold_chain(
    tmp_path: Path,
) -> None:
    from afuture.broker.ctp_order_journal import (
        CtpOrderSubmissionEntry,
        CtpOrderSubmissionJournal,
    )
    from afuture.directional_stress90_policy import STRESS90_POLICY
    from afuture.models import Offset, OrderRequest, OrderSide, OrderType

    config = _stress90_config(tmp_path)
    journal_path = tmp_path / "stress90_ctp_orders.json"
    journal = CtpOrderSubmissionJournal(journal_path)
    entry = journal.prepare(
        CtpOrderSubmissionEntry(
            sequence=1,
            account_identity_digest="c" * 64,
            policy_id=STRESS90_POLICY.policy_id,
            policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
            products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
            target_trading_day="20260825",
            daily_decision_digest="d" * 64,
            execution_intent_digest="e" * 64,
            transition={"freeze_authorized_lots": {"m2609": 1}, "transitions": []},
            front_id=1,
            session_id=2,
            order_ref=3,
            order_id="CTP.1_2_3",
            request=OrderRequest(
                "m2609",
                "DCE",
                OrderSide.BUY,
                Offset.OPEN,
                1,
                1_000.0,
                OrderType.FAK,
                "directional:stress90:dddddddddddd:M",
            ),
            status="prepared",
        )
    )
    journal.update_status(entry.order_id, "aborted_before_send")
    assert journal.compact_terminal() == 1

    valid = build_local_status(config, min_free_bytes=1)
    assert _checks(valid)["stress90_ctp_order_journal_integrity"]
    order_facts = valid.facts["stress90"]["ctp_order_journal"]
    assert order_facts["archive_entry_count"] == 1
    assert order_facts["archive_head_digest"]
    assert order_facts["valid"] is True

    from afuture.broker.ctp_order_journal import CTP_ORDER_JOURNAL_EPOCH_CONFIRMATION

    journal.seal_epoch(
        transaction_id="f" * 64,
        source_account_identity_digest="c" * 64,
        target_account_identity_digest="c" * 64,
        trading_day="20260825",
        operator_reason="bounded operator archive rollover",
        halted=True,
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        strong_confirmation=CTP_ORDER_JOURNAL_EPOCH_CONFIRMATION,
    )
    sealed = build_local_status(config, min_free_bytes=1)
    sealed_facts = sealed.facts["stress90"]["ctp_order_journal"]
    assert sealed_facts["sealed_epoch_count"] == 1
    assert sealed_facts["sealed_entry_count"] == 1
    segment = next(
        (tmp_path / "stress90_ctp_orders.json.epochs").glob(
            "epoch-*/stress90_ctp_orders.json.archive.*.json"
        )
    )
    segment.write_text("{}", encoding="utf-8")
    invalid = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=[],
        active_order_count=0,
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata=config.contracts,
        min_free_bytes=1,
    )
    assert not _checks(invalid)["stress90_ctp_order_journal_integrity"]
    assert invalid.facts["stress90"]["ctp_order_journal"]["valid"] is False
    assert invalid.facts["stress90"]["stress90_ready"] is False


def test_doctor_passes_verified_flat_preflight(tmp_path: Path) -> None:
    config = _config(tmp_path)
    StateStore(config.state_path).save(
        RuntimeState(
            runtime_mode=RuntimeMode.RUNNING.value,
            reconciled=True,
            metadata_verified=True,
            trading_day="20260825",
        )
    )

    report = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=[],
        active_order_count=0,
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata=config.contracts,
        min_free_bytes=1,
    )

    assert report.passed
    assert all(_checks(report).values())
    assert report.facts["broker"]["orders_sent"] == 0


def test_doctor_rejects_kill_switch_active_orders_and_position_mismatch(tmp_path: Path) -> None:
    config = _config(tmp_path)
    StateStore(config.state_path).save(
        RuntimeState(
            kill_switch=True,
            runtime_mode=RuntimeMode.HALTED.value,
            reconciled=False,
            metadata_verified=False,
            positions=[],
        )
    )
    remote = [ContractPosition("m2609", "DCE", long_today=1, long_price=3000)]

    report = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=remote,
        active_order_count=1,
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata=config.contracts,
        min_free_bytes=1,
    )
    checks = _checks(report)

    assert not report.passed
    assert not checks["kill_switch_clear"]
    assert not checks["runtime_mode_running"]
    assert not checks["no_active_orders"]
    assert not checks["position_reconciliation"]
    assert not checks["persisted_safety_gates"]


def test_doctor_rejects_invalid_trading_day_and_breached_account_limits(tmp_path: Path) -> None:
    config = _config(tmp_path)
    account = AccountSnapshot(500_000, 500_000, 300_000, 200_000, 0, 0, "20260825")

    report = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="bad-day",
        account=account,
        positions=[],
        active_order_count=0,
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata=config.contracts,
        min_free_bytes=1,
    )

    checks = _checks(report)
    assert not checks["trading_day_consistent"]
    assert not checks["account_risk_limits"]


def test_doctor_uses_persisted_equity_anchors_for_loss_and_drawdown(tmp_path: Path) -> None:
    config = _config(tmp_path)
    StateStore(config.state_path).save(
        RuntimeState(
            trading_day="20260825",
            day_start_equity=550_000,
            equity_high_watermark=600_000,
            reconciled=True,
            metadata_verified=True,
        )
    )

    report = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=[],
        active_order_count=0,
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata=config.contracts,
        min_free_bytes=1,
    )

    check = next(item for item in report.checks if item.name == "account_risk_limits")
    assert not check.passed
    assert "daily loss" in check.detail
    assert "drawdown" in check.detail


def test_doctor_requires_completed_directional_activity_evidence(tmp_path: Path) -> None:
    config = _config(tmp_path, directional=True)

    report = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=[],
        active_order_count=0,
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata=config.contracts,
        min_free_bytes=1,
    )

    assert not _checks(report)["directional_activity_ready"]


def test_doctor_requires_ohlc_cache_to_cover_completed_activity_day(tmp_path: Path) -> None:
    config = _config(tmp_path, directional=True)
    DirectionalActivityStore(tmp_path / "directional_activity.json").save(
        DirectionalActivitySnapshot(
            "20260824",
            {
                "m2609": ContractActivity(
                    "m2609",
                    "DCE",
                    "M",
                    "20260824",
                    10_000,
                    20_000,
                    datetime(2026, 8, 24, tzinfo=timezone.utc),
                )
            },
        )
    )
    _write_ohlc_cache(tmp_path, end="2026-08-21")

    expired = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=[],
        active_order_count=0,
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata=config.contracts,
        min_free_bytes=1,
    )

    check = next(
        item for item in expired.checks if item.name == "directional_ohlc_cache_required_day"
    )
    assert not check.passed
    assert "2026-08-24" in check.detail

    _write_ohlc_cache(tmp_path, end="2026-08-24")
    ready = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=[],
        active_order_count=0,
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata=config.contracts,
        min_free_bytes=1,
    )

    assert _checks(ready)["directional_ohlc_cache_required_day"]


def test_doctor_requires_activity_and_catalog_coverage_for_configured_products(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, directional=True)
    activity_path = tmp_path / "directional_activity.json"
    DirectionalActivityStore(activity_path).save(
        DirectionalActivitySnapshot(
            "20260824",
            {
                "rb2610": ContractActivity(
                    "rb2610",
                    "SHFE",
                    "RB",
                    "20260824",
                    10_000,
                    20_000,
                    datetime(2026, 8, 24, tzinfo=timezone.utc),
                )
            },
        )
    )

    unrelated = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=[],
        active_order_count=0,
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata=config.contracts,
        min_free_bytes=1,
    )
    assert not _checks(unrelated)["directional_activity_ready"]

    DirectionalActivityStore(activity_path).save(
        DirectionalActivitySnapshot(
            "20260824",
            {
                "m2609": ContractActivity(
                    "m2609",
                    "DCE",
                    "M",
                    "20260824",
                    10_000,
                    20_000,
                    datetime(2026, 8, 24, tzinfo=timezone.utc),
                )
            },
        )
    )
    covered = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=[],
        active_order_count=0,
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata=config.contracts,
        min_free_bytes=1,
    )
    assert _checks(covered)["directional_activity_ready"]

    DirectionalActivityStore(activity_path).save(
        DirectionalActivitySnapshot(
            "20260824",
            {
                "m2609": ContractActivity(
                    "m2609",
                    "DCE",
                    "M",
                    "20260824",
                    10,
                    20,
                    datetime(2026, 8, 24, tzinfo=timezone.utc),
                )
            },
        )
    )
    below_threshold = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=[],
        active_order_count=0,
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata=config.contracts,
        min_free_bytes=1,
    )
    assert not _checks(below_threshold)["directional_activity_ready"]


def test_doctor_rejects_tampered_directional_activity_envelope(tmp_path: Path) -> None:
    config = _config(tmp_path, directional=True)
    activity_path = tmp_path / "directional_activity.json"
    DirectionalActivityStore(activity_path).save(
        DirectionalActivitySnapshot(
            "20260824",
            {
                "m2609": ContractActivity(
                    "m2609",
                    "DCE",
                    "M",
                    "20260824",
                    10_000,
                    20_000,
                    datetime(2026, 8, 24, tzinfo=timezone.utc),
                )
            },
        )
    )
    envelope = json.loads(activity_path.read_text(encoding="utf-8"))
    completed = envelope.get("completed", envelope)
    completed["contracts"]["m2609"]["open_interest"] = 99_999
    activity_path.write_text(json.dumps(envelope), encoding="utf-8")

    report = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=[],
        active_order_count=0,
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata=config.contracts,
        min_free_bytes=1,
    )

    check = next(item for item in report.checks if item.name == "directional_activity_ready")
    assert not check.passed
    assert "invalid directional activity evidence" in check.detail


def test_doctor_rejects_activity_identity_that_disagrees_with_catalog(tmp_path: Path) -> None:
    config = _config(tmp_path, directional=True)
    DirectionalActivityStore(tmp_path / "directional_activity.json").save(
        DirectionalActivitySnapshot(
            "20260824",
            {
                "m2609": ContractActivity(
                    "m2609",
                    "SHFE",
                    "RB",
                    "20260824",
                    10_000,
                    20_000,
                    datetime(2026, 8, 24, tzinfo=timezone.utc),
                )
            },
        )
    )

    report = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=[],
        active_order_count=0,
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata=config.contracts,
        min_free_bytes=1,
    )

    assert not _checks(report)["directional_activity_ready"]


def test_doctor_rejects_internally_inconsistent_directional_activity(tmp_path: Path) -> None:
    config = _config(tmp_path, directional=True)
    activity_path = tmp_path / "directional_activity.json"
    DirectionalActivityStore(activity_path).save(
        DirectionalActivitySnapshot(
            "20260824",
            {
                "m2609": ContractActivity(
                    "m2609",
                    "DCE",
                    "M",
                    "20260824",
                    10_000,
                    20_000,
                    datetime(2026, 8, 24, tzinfo=timezone.utc),
                )
            },
        )
    )
    envelope = json.loads(activity_path.read_text(encoding="utf-8"))
    envelope["completed"]["contracts"]["m2609"]["trading_day"] = "20260823"
    _resign_activity(envelope)
    activity_path.write_text(json.dumps(envelope), encoding="utf-8")

    report = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=[],
        active_order_count=0,
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata=config.contracts,
        min_free_bytes=1,
    )

    assert not _checks(report)["directional_activity_ready"]


def test_doctor_rejects_missing_sampled_metadata(tmp_path: Path) -> None:
    config = _config(tmp_path)

    report = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=_account(),
        positions=[],
        active_order_count=0,
        catalog=_catalog(),
        requested_symbols=["m2609"],
        metadata={},
        min_free_bytes=1,
    )

    assert not _checks(report)["live_metadata_complete"]
