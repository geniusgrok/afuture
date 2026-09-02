from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest


def _market_stores(
    *, include_intermediate_ohlc: bool = False, include_intermediate_oi: bool = False
):
    from afuture.directional_stress90_policy import STRESS90_POLICY

    source = pd.Timestamp("2026-08-28")
    index = pd.date_range(end=source, periods=140, freq="D")
    if include_intermediate_ohlc:
        index = index.append(pd.DatetimeIndex([pd.Timestamp("2026-08-30")]))
    frame = pd.DataFrame(
        100.0,
        index=index,
        columns=list(STRESS90_POLICY.products),
    )
    entry = SimpleNamespace(
        row_count=len(index),
        close=frame,
        content_digest="1" * 64,
    )
    ohlc_store = SimpleNamespace(load=lambda _products: entry)
    completed = [
        SimpleNamespace(
            trading_day="20260828",
            complete=True,
            flows={product: 0 for product in STRESS90_POLICY.oi_products},
            evidence_digest="2" * 64,
        )
    ]
    if include_intermediate_oi:
        completed.append(
            SimpleNamespace(
                trading_day="20260830",
                complete=True,
                flows={product: 0 for product in STRESS90_POLICY.oi_products},
                evidence_digest="3" * 64,
            )
        )
    transition = SimpleNamespace(
        source_trading_day="20260828",
        target_trading_day="20260831",
        completed_oi_evidence_digest="2" * 64,
    )
    oi_record = SimpleNamespace(
        state=SimpleNamespace(
            completed=tuple(completed),
            observed_transitions=(transition,),
        ),
        checksum="4" * 64,
    )
    oi_store = SimpleNamespace(load_required_record=lambda: oi_record)
    return ohlc_store, oi_store


def test_operator_market_continuity_accepts_weekend_gap_without_calendar_guessing():
    from afuture.stress90_operator_continuity import (
        load_stress90_operator_account_day_continuity_evidence,
    )

    ohlc_store, oi_store = _market_stores()
    evidence = load_stress90_operator_account_day_continuity_evidence(
        ohlc_store,
        oi_store,
        completed_account_day="20260828",
        current_ctp_trading_day="20260831",
    )

    assert evidence.completed_account_day == "20260828"
    assert evidence.current_ctp_trading_day == "20260831"
    assert evidence.natural_day_gap == 3
    assert evidence.ohlc_content_digest == "1" * 64
    assert evidence.oi_store_checksum == "4" * 64
    assert len(evidence.continuity_digest) == 64


@pytest.mark.parametrize(
    ("intermediate_ohlc", "intermediate_oi"),
    [(True, False), (False, True)],
)
def test_operator_market_continuity_refuses_to_skip_existing_intermediate_session_data(
    intermediate_ohlc: bool,
    intermediate_oi: bool,
):
    from afuture.stress90_operator_continuity import (
        Stress90OperatorContinuityError,
        load_stress90_operator_account_day_continuity_evidence,
    )

    ohlc_store, oi_store = _market_stores(
        include_intermediate_ohlc=intermediate_ohlc,
        include_intermediate_oi=intermediate_oi,
    )
    with pytest.raises(Stress90OperatorContinuityError, match="contiguous|intermediate"):
        load_stress90_operator_account_day_continuity_evidence(
            ohlc_store,
            oi_store,
            completed_account_day="20260828",
            current_ctp_trading_day="20260831",
        )


def test_operator_roll_forward_parser_exposes_strong_confirmation_and_operation_identity():
    from afuture.cli import build_parser

    args = build_parser().parse_args(
        [
            "stress90-operator-roll-forward",
            "--config",
            "config.toml",
            "--confirm-live",
            "--confirm-operator-continuity",
            "--operation-id",
            "a" * 64,
            "--operator-reason",
            "exclusive account weekend rollover",
        ]
    )
    assert args.command == "stress90-operator-roll-forward"
    assert args.confirm_live is True
    assert args.confirm_operator_continuity is True
    assert args.operation_id == "a" * 64


def test_operator_roll_forward_missing_operator_ack_fails_before_broker_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from afuture.cli import _run_stress90_operator_roll_forward
    from afuture.directional import DirectionalConfig
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS

    constructed = False

    class ForbiddenBroker:
        def __init__(self, _credentials):
            nonlocal constructed
            constructed = True
            raise AssertionError("Broker must not be constructed before confirmation validation")

    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", ForbiddenBroker)
    monkeypatch.setattr(
        "afuture.account_runtime_registry.PRODUCTION_ACCOUNT_RUNTIME_REGISTRY_PATH",
        tmp_path / ".account-runtime-registry.json",
    )
    monkeypatch.delenv("AFUTURE_OPERATOR_CONTINUITY_ACK", raising=False)
    config = SimpleNamespace(
        mode="live",
        ctp=SimpleNamespace(environment="test"),
        directional=DirectionalConfig(
            enabled=True,
            policy="stress90",
            products=FROZEN_PRODUCTS,
            account_exclusive=True,
            account_continuity_mode="operator_managed",
        ),
        state_path=str(tmp_path / "state.json"),
        account_registry_path=str(tmp_path / ".account-runtime-registry.json"),
        journal_path=str(tmp_path / "audit.jsonl"),
    )
    args = SimpleNamespace(
        command="stress90-operator-roll-forward",
        config="config.toml",
        confirm_live=False,
        confirm_operator_continuity=True,
        operation_id="a" * 64,
        operator_reason="exclusive account weekend rollover",
        startup_timeout=0.1,
        snapshot_wait=0.1,
    )

    with pytest.raises(RuntimeError, match="OPERATOR_CONTINUITY_ACK|confirmation"):
        _run_stress90_operator_roll_forward(config, args)
    assert constructed is False
