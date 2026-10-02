"""Capture bounded public originals and observed times; never certify historical PIT.

Input: JSON list of {id, url, ...provenance labels}. No credentials or orders.
Each invocation is an immutable capture directory, including unsuccessful bodies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

MAX_BYTES = 4_000_000


def capture(spec: dict, output: Path, timeout: float) -> dict:
    identity = spec["id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", identity):
        raise ValueError("capture id must be a safe basename")
    url = spec["url"]
    if not url.startswith("https://"):
        raise ValueError("public collection requires HTTPS")
    row = {**spec, "request_started_at": datetime.now(timezone.utc).isoformat()}
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        try:
            response = urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            raw = response.read(MAX_BYTES + 1)
            row.update(
                http_status=response.code,
                response_url=response.geturl(),
                headers={
                    key: response.headers.get(key)
                    for key in ("Content-Type", "Date", "Last-Modified", "ETag", "Memento-Datetime")
                },
            )
        row["complete"] = len(raw) <= MAX_BYTES
        row["file"] = f"originals/{identity}.bin"
        (output / row["file"]).write_bytes(raw)
        row["bytes"] = len(raw)
        row["sha256"] = hashlib.sha256(raw).hexdigest()
        row["git_blob"] = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
    row["observed_at"] = datetime.now(timezone.utc).isoformat()
    row["historical_availability_certified"] = False
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=12.0)
    args = parser.parse_args()
    specs = json.loads(args.spec.read_text(encoding="utf-8"))
    ids = [row["id"] for row in specs]
    if len(ids) != len(set(ids)) or not 1 <= args.workers <= 8 or args.timeout <= 0:
        parser.error("ids must be unique; workers 1..8; timeout positive")
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "originals").mkdir()
    (args.output / "request_spec.json").write_bytes(args.spec.read_bytes())
    records = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for record in pool.map(lambda spec: capture(spec, args.output, args.timeout), specs):
            records.append(record)
            # Persist after every result; a process interruption is recoverable.
            (args.output / "capture.json").write_text(
                json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(
                record["id"],
                record.get("http_status", "error"),
                record.get("bytes", 0),
                flush=True,
            )


if __name__ == "__main__":
    main()
