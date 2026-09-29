"""Bounded download probe for official SHFE historical daily warrant originals.

This is a research input probe, not a point-in-time publication certificate.
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

DATES = (
    "20220930",  # older archive
    "20240208",  # before Spring Festival
    "20240930",  # before National Day
    "20250630",  # last day before site migration
    "20250930",  # new archive, before National Day
    "20260925",  # current archive
)
OLD_URL = "https://tsite.shfe.com.cn/data/dailydata/{day}dailystock.dat"
NEW_URL = "https://www.shfe.com.cn/data/tradedata/future/dailydata/{day}dailystock.dat"
MAX_BYTES = 2_000_000


def probe(output: Path) -> list[dict[str, object]]:
    output.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, object]] = []
    for day in DATES:
        url = (OLD_URL if day <= "20250630" else NEW_URL).format(day=day)
        row: dict[str, object] = {"day": day, "source_url": url}
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "afuture-research/1.0"})
            with urllib.request.urlopen(request, timeout=20) as response:
                row["http_status"] = response.status
                row["content_type"] = response.headers.get("Content-Type", "")
                row["last_modified"] = response.headers.get("Last-Modified", "")
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("official file exceeds probe bound")
            payload = json.loads(raw)
            if not isinstance(payload, dict) or not isinstance(payload.get("o_cursor"), list):
                raise ValueError("official response is not a daily warrant JSON table")
            name = f"{day}dailystock.dat"
            (output / name).write_bytes(raw)
            row.update(
                file=name,
                bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
                top_level_keys=sorted(payload),
                rows=len(payload["o_cursor"]),
                first_row_keys=(
                    sorted(payload["o_cursor"][0])
                    if payload["o_cursor"] and isinstance(payload["o_cursor"][0], dict)
                    else []
                ),
            )
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, TypeError) as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        records.append(row)
    (output / "probe.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return records


if __name__ == "__main__":
    result = probe(Path(sys.argv[1] if len(sys.argv) > 1 else "shfe-warrant-probe"))
    print(json.dumps(result, ensure_ascii=False))
    if not any("sha256" in row for row in result):
        sys.exit("no official original could be downloaded")
