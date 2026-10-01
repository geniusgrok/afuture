from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

SOURCE = "ef6589f0f36383e64900b20c07ad3b0087872a88"
TREE = "369e536cb140aa1a7112adc7d03b4142ee1e0ab8"
BASE = "0472f7c3f4a5abfa98bded051fc7ad78bc516edd"
PARENT = "11613a25474bd9da30763d1e895ecb8e6fdcdf1b"
PACK_HASH = "46e250f349549c20140d6e2cea72a6ce10ccc615e17229d7f94e1c3cdb5015fc"
DESTINATION = "refs/heads/codex/import-afuture-dayend-20261001"


def git(*args: str, data: bytes | None = None) -> str:
    return subprocess.check_output(["git", *args], input=data).decode().strip()


def main() -> None:
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["schema"] == 1
    assert manifest["source_commit"] == SOURCE
    assert manifest["source_tree"] == TREE
    assert manifest["base_commit"] == BASE
    assert manifest["source_parent"] == PARENT
    assert manifest["pack_sha256"] == PACK_HASH
    offset = 0
    digest = hashlib.sha256()
    with tempfile.TemporaryFile() as archive:
        for index, part in enumerate(manifest["parts"]):
            assert part["name"] == f"part-{index:04d}.b64"
            assert part["offset"] == offset
            encoded = (root / part["name"]).read_bytes()
            assert len(encoded) == part["encoded_length"]
            assert hashlib.sha256(encoded).hexdigest() == part["encoded_sha256"]
            header = f"blob {len(encoded)}\0".encode()
            assert hashlib.sha1(header + encoded).hexdigest() == part["git_blob_sha"]
            raw = base64.b64decode(encoded, validate=True)
            assert len(raw) == part["length"]
            assert hashlib.sha256(raw).hexdigest() == part["raw_sha256"]
            digest.update(raw)
            archive.write(raw)
            offset += len(raw)
        assert offset == manifest["pack_length"]
        assert digest.hexdigest() == PACK_HASH
        archive.seek(0)
        subprocess.run(["git", "index-pack", "--stdin"], stdin=archive, check=True)
    assert git("rev-parse", f"{SOURCE}^{{tree}}") == TREE
    assert git("show", "-s", "--format=%P", SOURCE) == PARENT
    subprocess.run(["git", "merge-base", "--is-ancestor", BASE, SOURCE], check=True)
    assert git("cat-file", "-t", SOURCE) == "commit"
    subprocess.run(["git", "push", "origin", f"{SOURCE}:{DESTINATION}"], check=True)
    print(json.dumps({"source_commit": SOURCE, "source_tree": TREE, "pack_sha256": PACK_HASH}))


if __name__ == "__main__":
    main()
