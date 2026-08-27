from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(
    "failure_point",
    ["partial-write", "file-fsync", "path-verification", "parent-fsync", "descriptor-close"],
)
def test_post_exclusive_create_failure_cleans_exact_file_and_allows_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    import afuture.durable_file_creation as durable

    path = tmp_path / "artifact.json"
    payload = b'{"durable":true}\n'
    real_open = durable.os.open
    real_write = durable.os.write
    real_fsync = durable.os.fsync
    real_stat = durable.os.stat
    real_parent_fsync = durable._fsync_parent
    real_close = durable.os.close
    created_descriptor: int | None = None
    injected = False

    def tracked_open(target, flags, *args, **kwargs):
        nonlocal created_descriptor
        descriptor = real_open(target, flags, *args, **kwargs)
        if Path(target) == path and flags & os.O_EXCL:
            created_descriptor = descriptor
        return descriptor

    def fail_write(descriptor: int, data: bytes) -> int:
        nonlocal injected
        if failure_point == "partial-write" and descriptor == created_descriptor and not injected:
            injected = True
            real_write(descriptor, data[: max(1, len(data) // 2)])
            raise OSError("injected partial write failure")
        return real_write(descriptor, data)

    def fail_fsync(descriptor: int) -> None:
        nonlocal injected
        if failure_point == "file-fsync" and descriptor == created_descriptor and not injected:
            injected = True
            raise OSError("injected file fsync failure")
        real_fsync(descriptor)

    def fail_stat(target, *args, **kwargs):
        nonlocal injected
        result = real_stat(target, *args, **kwargs)
        if failure_point == "path-verification" and Path(target) == path and not injected:
            injected = True
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_dev=result.st_dev,
                st_ino=result.st_ino + 1,
            )
        return result

    def fail_parent_fsync(target: Path) -> None:
        nonlocal injected
        if failure_point == "parent-fsync" and not injected:
            injected = True
            raise OSError("injected parent fsync failure")
        real_parent_fsync(target)

    def fail_close(descriptor: int) -> None:
        nonlocal injected
        if (
            failure_point == "descriptor-close"
            and descriptor == created_descriptor
            and not injected
        ):
            injected = True
            real_close(descriptor)
            raise OSError("injected descriptor close failure")
        real_close(descriptor)

    monkeypatch.setattr(durable.os, "open", tracked_open)
    monkeypatch.setattr(durable.os, "write", fail_write)
    monkeypatch.setattr(durable.os, "fsync", fail_fsync)
    monkeypatch.setattr(durable.os, "stat", fail_stat)
    monkeypatch.setattr(durable, "_fsync_parent", fail_parent_fsync)
    monkeypatch.setattr(durable.os, "close", fail_close)

    with pytest.raises(durable.DurableFileError, match="creation failed") as caught:
        durable.create_durable_file_exclusive(path, payload)
    assert caught.value.__cause__ is not None
    assert not path.exists()

    token = durable.create_durable_file_exclusive(path, payload)
    assert durable.creation_token_matches(token)
    assert path.read_bytes() == payload


def test_post_exclusive_cleanup_failure_preserves_incident_and_primary_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import afuture.durable_file_creation as durable

    path = tmp_path / "artifact.json"
    real_open = durable.os.open
    real_fsync = durable.os.fsync
    real_unlink = durable.os.unlink
    target_descriptor: int | None = None
    primary_injected = False

    def tracked_open(target, flags, *args, **kwargs):
        nonlocal target_descriptor
        descriptor = real_open(target, flags, *args, **kwargs)
        if Path(target) == path and flags & os.O_EXCL:
            target_descriptor = descriptor
        return descriptor

    def fail_file_fsync(descriptor: int) -> None:
        nonlocal primary_injected
        if descriptor == target_descriptor and not primary_injected:
            primary_injected = True
            raise OSError("injected primary fsync failure")
        real_fsync(descriptor)

    def fail_cleanup_unlink(target) -> None:
        if Path(target) == path:
            raise OSError("injected cleanup unlink failure")
        real_unlink(target)

    monkeypatch.setattr(durable.os, "open", tracked_open)
    monkeypatch.setattr(durable.os, "fsync", fail_file_fsync)
    monkeypatch.setattr(durable.os, "unlink", fail_cleanup_unlink)

    with pytest.raises(
        durable.DurableFileError,
        match=r"primary fsync failure.*cleanup.*unlink failure",
    ) as caught:
        durable.create_durable_file_exclusive(path, b"incident")
    assert isinstance(caught.value.__cause__, OSError)
    assert "primary fsync failure" in str(caught.value.__cause__)
    assert path.is_file()
