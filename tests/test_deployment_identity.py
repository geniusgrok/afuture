from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from afuture.deployment_identity import (
    DeploymentIdentityError,
    DeploymentIdentityStore,
    DeploymentVerification,
    require_matching_deployment,
    seal_deployment,
    verify_deployment,
)
from afuture.provenance import (
    ProvenanceError,
    git_head,
    production_source_tree_digest,
    require_clean_tracked_worktree,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _minimal_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "afuture").mkdir()
    (repo / "config").mkdir()
    (repo / "constraints").mkdir()
    (repo / "afuture" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "config" / "live.toml").write_text("[system]\nmode='live'\n", encoding="utf-8")
    (repo / "constraints" / "core-dev.txt").write_text("pytest==9.0.2\n", encoding="utf-8")
    (repo / "constraints" / "live.txt").write_text("vnpy_ctp==6.7.11.4\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\nname='afuture'\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "ci@example.invalid")
    _git(repo, "config", "user.name", "CI")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    return repo


def _identity(tmp_path: Path) -> dict[str, object]:
    return {
        "source_commit": "a" * 40,
        "production_source_tree_digest": "b" * 64,
        "production_config_sha256": "c" * 64,
        "core_constraints_sha256": "d" * 64,
        "live_constraints_sha256": "e" * 64,
        "python_implementation": "CPython",
        "python_version": "3.10.19",
        "os": "Linux",
        "architecture": "x86_64",
        "executable": "/usr/bin/python3",
        "repository_path": str(tmp_path / "repo"),
        "runtime_path": str(tmp_path / "runtime"),
        "account_registry_path": str(tmp_path / "registry.json"),
        "bundle_path": str(tmp_path / "bootstrap.tar"),
        "bundle_digest": "f" * 64,
        "bundle_archive_sha256": "1" * 64,
        "seed_digest": "2" * 64,
        "policy_definition_digest": "3" * 64,
        "products_manifest_digest": "4" * 64,
        "risk_overlay_digest": "5" * 64,
        "account_continuity_mode": "operator_managed",
        "deployment_role": "live",
        "vnpy_ctp": None,
    }


def test_clean_checkout_has_full_head_and_stable_source_digest(tmp_path: Path) -> None:
    repo = _minimal_repo(tmp_path)
    require_clean_tracked_worktree(repo)
    assert len(git_head(repo)) == 40
    first, entries = production_source_tree_digest(repo)
    second, repeated_entries = production_source_tree_digest(repo)
    assert first == second
    assert entries == repeated_entries
    assert entries


def test_dirty_tracked_checkout_is_rejected(tmp_path: Path) -> None:
    repo = _minimal_repo(tmp_path)
    (repo / "pyproject.toml").write_text("[project]\nname='changed'\n", encoding="utf-8")
    with pytest.raises(ProvenanceError, match="dirty"):
        require_clean_tracked_worktree(repo)


@pytest.mark.parametrize(
    "changed_field",
    [
        "source_commit",
        "production_source_tree_digest",
        "production_config_sha256",
        "core_constraints_sha256",
        "live_constraints_sha256",
        "python_implementation",
        "python_version",
        "os",
        "architecture",
        "executable",
        "repository_path",
        "runtime_path",
        "account_registry_path",
        "bundle_digest",
        "bundle_archive_sha256",
        "seed_digest",
        "policy_definition_digest",
        "products_manifest_digest",
        "risk_overlay_digest",
        "account_continuity_mode",
        "vnpy_ctp",
    ],
)
def test_deployment_verify_reports_every_bound_identity_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_field: str,
) -> None:
    import afuture.deployment_identity as deployment_module

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    expected = _identity(tmp_path)
    DeploymentIdentityStore(runtime / "deployment_identity.json").seal(expected)
    current = dict(expected)
    current[changed_field] = "changed"
    monkeypatch.setattr(deployment_module, "_deployment_payload", lambda **_kwargs: current)

    result = verify_deployment(
        config=SimpleNamespace(),
        config_path=tmp_path / "config.toml",
        runtime_dir=runtime,
        account_registry_path=tmp_path / "registry.json",
    )
    assert result.passed is False
    assert result.changes == (changed_field,)


def test_deployment_store_rejects_secret_like_fields(tmp_path: Path) -> None:
    store = DeploymentIdentityStore(tmp_path / "deployment_identity.json")
    with pytest.raises(DeploymentIdentityError, match="secret-like"):
        store.seal({"source_commit": "a" * 40, "ctp_password": "not-allowed"})
    assert not store.path.exists()


def test_deployment_current_corruption_never_falls_back_to_prev(tmp_path: Path) -> None:
    store = DeploymentIdentityStore(tmp_path / "deployment_identity.json")
    store.seal({"source_commit": "a" * 40})
    store.seal({"source_commit": "b" * 40})
    store.path.write_bytes(b"corrupt")
    with pytest.raises(DeploymentIdentityError):
        store.load_required()
    assert store.previous_path.exists()


def test_deployment_missing_current_with_prev_is_incident(tmp_path: Path) -> None:
    store = DeploymentIdentityStore(tmp_path / "deployment_identity.json")
    store.seal({"source_commit": "a" * 40})
    store.seal({"source_commit": "b" * 40})
    store.path.unlink()
    with pytest.raises(DeploymentIdentityError, match="current is missing"):
        store.load_required()


def test_low_level_seal_does_not_create_activation_permit(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    DeploymentIdentityStore(runtime / "deployment_identity.json").seal({"source_commit": "a" * 40})
    assert not (runtime / "stress90_activation_permit.json").exists()


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
