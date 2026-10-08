from __future__ import annotations

import io
import tarfile

import pytest


def _unsafe_archive(*, name: str, data: bytes = b"x", kind: str = "file") -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        info = tarfile.TarInfo(name)
        info.mtime = 0
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        info.mode = 0o600
        if kind == "symlink":
            info.type = tarfile.SYMTYPE
            info.linkname = "target"
            archive.addfile(info)
        elif kind == "hardlink":
            info.type = tarfile.LNKTYPE
            info.linkname = "target"
            archive.addfile(info)
        else:
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return stream.getvalue()


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("../escape", "file"),
        ("/absolute", "file"),
        ("seed.json", "symlink"),
        ("seed.json", "hardlink"),
    ],
)
def test_archive_rejects_path_and_link_attacks(name: str, kind: str) -> None:
    from afuture.secure_archive import ArchiveSecurityError, read_deterministic_archive

    payload = _unsafe_archive(name=name, kind=kind)
    with pytest.raises(ArchiveSecurityError):
        read_deterministic_archive(payload, allowed_members={"manifest.json", "seed.json"})


def test_archive_rejects_truncation_append_and_tamper() -> None:
    from afuture.secure_archive import (
        ArchiveSecurityError,
        create_deterministic_archive,
        read_deterministic_archive,
    )

    payload = create_deterministic_archive({"manifest.json": b'{"a":1}', "seed.json": b"seed"})
    variants = [
        payload[:-512],
        payload + b"garbage",
        payload[:1024] + bytes([payload[1024] ^ 1]) + payload[1025:],
    ]
    for variant in variants:
        with pytest.raises(ArchiveSecurityError):
            read_deterministic_archive(
                variant,
                allowed_members={"manifest.json", "seed.json"},
            )
