from pathlib import Path

from afuture.directional import DirectionalConfig
from afuture.execution_aligned_policy import FROZEN_PRODUCTS
from afuture.models import AccountSnapshot
from afuture.operations import build_doctor_report, build_local_status
from afuture.risk import RiskConfig


def _config(tmp_path: Path, *, mode: str):
    class Config:
        pass

    config = Config()
    config.state_path = str(tmp_path / "state.json")
    config.log_path = str(tmp_path / "afuture.log")
    config.report_path = str(tmp_path / "report.json")
    config.journal_path = str(tmp_path / "audit.jsonl")
    config.alert_path = str(tmp_path / "alerts.jsonl")
    config.account_registry_path = str(tmp_path / ".account-runtime-registry.json")
    config.contracts = {}
    config.directional = DirectionalConfig(
        enabled=True,
        policy="stress90",
        products=FROZEN_PRODUCTS,
        account_exclusive=True,
        account_continuity_mode=mode,
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


def test_strict_status_keeps_external_blockers_and_marks_operator_evidence_not_applicable(
    tmp_path: Path,
):
    report = build_local_status(_config(tmp_path, mode="strict"), min_free_bytes=1)
    stress = report.facts["stress90"]

    assert stress["account_continuity_mode"] == "strict"
    assert stress["operator_continuity"]["applicable"] is False
    assert stress["requires_operator_roll_forward"] is False
    assert stress["external_activation_gates_completed"] is False
    assert "prior_day_final_funding_settlement_witness" in stress["external_blocker_reasons"]
    assert "authoritative_nonadjacent_session_ledger" in stress["external_blocker_reasons"]


def test_operator_status_distinguishes_missing_operator_receipt_from_strict_external_blockers(
    tmp_path: Path,
):
    report = build_local_status(_config(tmp_path, mode="operator_managed"), min_free_bytes=1)
    stress = report.facts["stress90"]
    operator = stress["operator_continuity"]

    assert stress["account_continuity_mode"] == "operator_managed"
    assert operator["applicable"] is True
    assert operator["path"].endswith("stress90_operator_continuity.json")
    assert operator["evidence_authority"] == "operator_trust"
    assert operator["authoritative_broker_or_exchange_evidence"] is False
    assert stress["requires_operator_roll_forward"] is True
    assert stress["requires_account_rebase"] is False
    assert stress["requires_new_permit"] is True
    assert stress["external_activation_gates_completed"] is False
    assert "prior_day_final_funding_settlement_witness" in stress["external_blocker_reasons"]


def test_doctor_always_reports_zero_order_and_cancel_writes(tmp_path: Path):
    config = _config(tmp_path, mode="strict")
    account = AccountSnapshot(
        500_000.0,
        500_000.0,
        500_000.0,
        0.0,
        0.0,
        0.0,
        "20260825",
    )
    report = build_doctor_report(
        config,
        broker_ready=True,
        fresh_snapshot=True,
        trading_day="20260825",
        account=account,
        positions=[],
        active_order_count=0,
        catalog=[],
        requested_symbols=[],
        metadata={},
        quotes={},
        min_free_bytes=1,
    )
    assert report.facts["broker"]["orders_sent"] == 0
    assert report.facts["broker"]["cancels_sent"] == 0
