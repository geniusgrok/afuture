from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from afuture.models import RuntimeMode
from afuture.state import RuntimeState


def _halted_state() -> RuntimeState:
    return RuntimeState(
        kill_switch=True,
        kill_reason="commissioning",
        reconciled=True,
        metadata_verified=True,
        runtime_mode=RuntimeMode.HALTED.value,
    )


def test_activation_requires_every_lifecycle_gate_and_strong_confirmation():
    from afuture.directional_policy_activation import (
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
    )

    common = dict(
        state=_halted_state(),
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        bootstrap_seed_digest="a" * 64,
        operator_reason="first test-counter activation",
        strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
    )
    activated = activate_stress90_policy(**common)
    marker = activated.strategy_states["directional_policy_identity"]
    assert marker["policy_id"] == "directional.stress90"
    assert marker["bootstrap_seed_digest"] == "a" * 64

    for override in (
        {"broker_flat": False},
        {"local_flat": False},
        {"no_active_orders": False},
        {"reconciled": False},
        {"strong_confirmation": "yes"},
        {"state": replace(_halted_state(), runtime_mode=RuntimeMode.RUNNING.value)},
    ):
        with pytest.raises(RuntimeError, match="activation"):
            activate_stress90_policy(**{**common, **override})


def test_runtime_policy_identity_rejects_old_or_mismatched_state():
    from afuture.directional_policy_activation import (
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
        require_directional_policy_identity,
    )
    from afuture.directional_stress90_policy import STRESS90_POLICY

    with pytest.raises(RuntimeError, match="explicit activation"):
        require_directional_policy_identity(
            RuntimeState(),
            policy_id=STRESS90_POLICY.policy_id,
            policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
        )

    activated = activate_stress90_policy(
        _halted_state(),
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        bootstrap_seed_digest="a" * 64,
        operator_reason="validated migration",
        strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
    )
    require_directional_policy_identity(
        activated,
        policy_id=STRESS90_POLICY.policy_id,
        policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
        products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
        bootstrap_seed_digest="a" * 64,
    )
    with pytest.raises(RuntimeError, match="bootstrap identity"):
        require_directional_policy_identity(
            activated,
            policy_id=STRESS90_POLICY.policy_id,
            policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
            products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
            bootstrap_seed_digest="b" * 64,
        )
    with pytest.raises(RuntimeError, match="product manifest"):
        require_directional_policy_identity(
            activated,
            policy_id=STRESS90_POLICY.policy_id,
            policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
            products_manifest_digest="c" * 64,
        )
    with pytest.raises(RuntimeError, match="identity mismatch"):
        require_directional_policy_identity(
            activated,
            policy_id="execution_aligned",
            policy_definition_digest="",
        )


def _write_bootstrap_artifacts(runtime_dir: Path):
    from afuture.directional_stress90_policy import (
        STRESS90_POLICY,
        Stress90CandidateState,
    )
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
    )
    seed = Stress90BootstrapSeed.from_candidate_state(
        candidate,
        bootstrap_source_manifest={"fixed": "1" * 64},
        bootstrap_through_day="20260824",
        last_completed_input_day="20260821",
    )
    Stress90SeedStore(runtime_dir / "stress90_bootstrap_seed.json").save_new(seed)
    Stress90PolicyStateStore(runtime_dir / "stress90_policy_state.json").save(
        Stress90PolicyState.from_seed(seed)
    )
    return seed


def test_activation_cli_commissions_fresh_flat_state_but_leaves_it_halted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from afuture.cli import _run_stress90_activate
    from afuture.directional import DirectionalConfig
    from afuture.directional_policy_activation import (
        POLICY_IDENTITY_STATE_KEY,
        STRESS90_ACTIVATION_CONFIRMATION,
    )
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.models import AccountSnapshot
    from afuture.state import StateStore

    seed = _write_bootstrap_artifacts(tmp_path)

    class FakeBroker:
        def __init__(self, credentials):
            self.credentials = credentials

        def seed_trade_identities(self, identities):
            assert identities == []

        def start(self):
            return None

        def stop(self):
            return None

        def is_ready(self):
            return True

        def snapshot_marker(self):
            return (0, 0)

        def snapshot_ready(self, marker):
            return True

        def get_account(self):
            return AccountSnapshot(500_000, 500_000, 500_000, 0, 0, 0, "20260825")

        def get_positions(self):
            return []

        def get_active_orders(self):
            return []

        def get_trading_day(self):
            return "20260825"

    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", FakeBroker)
    monkeypatch.setenv(
        "AFUTURE_STRESS90_ACTIVATION_ACK",
        STRESS90_ACTIVATION_CONFIRMATION,
    )
    config = SimpleNamespace(
        mode="live",
        ctp=SimpleNamespace(environment="test"),
        directional=DirectionalConfig(
            enabled=True,
            policy="stress90",
            products=FROZEN_PRODUCTS,
            account_exclusive=True,
        ),
        state_path=str(tmp_path / "state.json"),
        journal_path=str(tmp_path / "audit.jsonl"),
    )
    args = SimpleNamespace(
        confirm_live=False,
        confirm_activation=True,
        startup_timeout=0.1,
        snapshot_wait=0.1,
        runtime_dir="",
        operator_reason="initial test-counter commissioning",
    )

    assert _run_stress90_activate(config, args) == 0
    state = StateStore(config.state_path).load()
    marker = state.strategy_states[POLICY_IDENTITY_STATE_KEY]
    assert marker["bootstrap_seed_digest"] == seed.seed_digest
    assert state.runtime_mode == RuntimeMode.HALTED.value
    assert state.kill_switch is True
    assert state.reconciled is True
    assert "stress90_policy_activation_completed" in Path(config.journal_path).read_text(
        encoding="utf-8"
    )


def test_cli_parser_exposes_explicit_activation_and_account_rebase_commands():
    from afuture.cli import build_parser

    activate = build_parser().parse_args(
        [
            "stress90-activate",
            "--config",
            "live.toml",
            "--confirm-activation",
            "--operator-reason",
            "commissioning",
        ]
    )
    rebase = build_parser().parse_args(
        [
            "stress90-account-rebase",
            "--config",
            "live.toml",
            "--confirm-rebase",
            "--operator-reason",
            "cash transfer",
        ]
    )
    compare = build_parser().parse_args(
        [
            "stress90-oi-compare",
            "--config",
            "live.toml",
            "--trading-day",
            "20260825",
            "--vendor",
            "vendor.json",
        ]
    )

    assert activate.command == "stress90-activate"
    assert rebase.command == "stress90-account-rebase"
    assert compare.command == "stress90-oi-compare"


def test_account_rebase_cli_resets_only_soft_path_and_records_operator_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from afuture.cli import _run_stress90_account_rebase
    from afuture.directional import DirectionalConfig
    from afuture.directional_policy_activation import (
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
    )
    from afuture.directional_stress90_state import (
        REBASE_CONFIRMATION,
        Stress90PolicyStateStore,
        record_completed_account_day,
    )
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.models import AccountSnapshot
    from afuture.state import StateStore

    seed = _write_bootstrap_artifacts(tmp_path)
    generic = activate_stress90_policy(
        _halted_state(),
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        bootstrap_seed_digest=seed.seed_digest,
        operator_reason="commissioned",
        strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
    )
    StateStore(tmp_path / "state.json").save(generic)
    policy_store = Stress90PolicyStateStore(tmp_path / "stress90_policy_state.json")
    policy_store.save(
        record_completed_account_day(
            policy_store.load_required(),
            "20260825",
            -0.10,
        )
    )

    class FakeBroker:
        def __init__(self, credentials):
            self.credentials = credentials

        def seed_trade_identities(self, identities):
            return None

        def start(self):
            return None

        def stop(self):
            return None

        def is_ready(self):
            return True

        def snapshot_marker(self):
            return (0, 0)

        def snapshot_ready(self, marker):
            return True

        def get_account(self):
            return AccountSnapshot(700_000, 700_000, 700_000, 0, 0, 0, "20260826")

        def get_positions(self):
            return []

        def get_active_orders(self):
            return []

        def get_trading_day(self):
            return "20260826"

    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", FakeBroker)
    monkeypatch.setenv("AFUTURE_STRESS90_REBASE_ACK", REBASE_CONFIRMATION)
    config = SimpleNamespace(
        mode="live",
        ctp=SimpleNamespace(environment="test"),
        directional=DirectionalConfig(
            enabled=True,
            policy="stress90",
            products=FROZEN_PRODUCTS,
            account_exclusive=True,
        ),
        state_path=str(tmp_path / "state.json"),
        journal_path=str(tmp_path / "audit.jsonl"),
    )
    args = SimpleNamespace(
        confirm_live=False,
        confirm_rebase=True,
        startup_timeout=0.1,
        snapshot_wait=0.1,
        runtime_dir="",
        operator_reason="verified cash withdrawal",
    )

    assert _run_stress90_account_rebase(config, args) == 0
    rebased = policy_store.load_required()
    assert rebased.completed_account_wealth == 1.0
    assert rebased.completed_account_high_watermark == 1.0
    assert rebased.last_completed_account_day is None
    assert rebased.live_inception_day == "20260826"
    audit_text = Path(config.journal_path).read_text(encoding="utf-8")
    assert "stress90_account_rebase_completed" in audit_text
    assert "verified cash withdrawal" in audit_text
