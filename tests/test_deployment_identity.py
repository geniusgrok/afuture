from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from afuture.deployment_identity import (
    DeploymentIdentityError,
    DeploymentIdentityStore,
    DeploymentVerification,
    require_matching_deployment,
    seal_deployment,
)


def test_deployment_current_corruption_never_falls_back_to_prev(tmp_path: Path) -> None:
    store = DeploymentIdentityStore(tmp_path / "deployment_identity.json")
    store.seal({"source_commit": "a" * 40})
    store.seal({"source_commit": "b" * 40})
    store.path.write_bytes(b"corrupt")
    with pytest.raises(DeploymentIdentityError):
        store.load_required()
    assert store.previous_path.exists()


def test_high_level_reseal_invalidates_existing_permit_before_new_seal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import afuture.deployment_identity as deployment_module
    from afuture.stress90_activation_permit import Stress90ActivationPermitStore

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    permit = runtime / "stress90_activation_permit.json"
    permit.write_text("placeholder", encoding="utf-8")
    calls: list[str] = []

    monkeypatch.setattr(deployment_module, "repository_root", lambda _repo=None: tmp_path / "repo")
    monkeypatch.setattr(deployment_module, "require_clean_tracked_worktree", lambda _repo: None)
    monkeypatch.setattr(
        deployment_module,
        "_deployment_payload",
        lambda **_kwargs: {"source_commit": "a" * 40, "deployment_role": "live"},
    )
    monkeypatch.setattr(
        Stress90ActivationPermitStore,
        "invalidate",
        lambda _self, reason: calls.append(reason),
    )

    seal_deployment(
        config=SimpleNamespace(),
        config_path=tmp_path / "config.toml",
        bundle_path=tmp_path / "bundle.tar",
        runtime_dir=runtime,
        account_registry_path=tmp_path / "registry.json",
    )
    assert calls and "fresh Doctor permit" in calls[0]


def test_live_and_shadow_seals_cannot_cross_roles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import afuture.deployment_identity as deployment_module

    verification = DeploymentVerification(
        passed=True,
        seal_sequence=1,
        seal_checksum="a" * 64,
        changes=(),
        expected={"deployment_role": "live"},
        current={"deployment_role": "live"},
    )
    monkeypatch.setattr(deployment_module, "verify_deployment", lambda **_kwargs: verification)
    with pytest.raises(DeploymentIdentityError, match="role mismatch"):
        require_matching_deployment(
            config=SimpleNamespace(),
            config_path=tmp_path / "config.toml",
            runtime_dir=tmp_path / "shadow",
            account_registry_path=tmp_path / "registry.json",
            expected_role="shadow",
        )
