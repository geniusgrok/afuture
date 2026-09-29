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
    "20260924",  # current archive
    "20260925",
    "20260928",
)
WEEKLY_DATES = ("20220930", "20240208", "20240927", "20250627", "20250926", "20260925")
OLD_URL = "https://tsite.shfe.com.cn/data/dailydata/{day}{kind}.dat"
NEW_URL = "https://www.shfe.com.cn/data/tradedata/future/dailydata/{day}{kind}.dat"
MAX_BYTES = 2_000_000


def probe(output: Path, *, weekly_only: bool = False) -> list[dict[str, object]]:
    output.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, object]] = []
    kind = "weeklystock" if weekly_only else "dailystock"
    for day in WEEKLY_DATES if weekly_only else DATES:
        urls = [OLD_URL, NEW_URL] if day <= "20250630" else [NEW_URL]
        row: dict[str, object] = {"day": day, "kind": kind, "attempts": []}
        for template in urls:
            url = template.format(day=day, kind=kind)
            attempt: dict[str, object] = {"source_url": url}
            try:
                request = urllib.request.Request(
                    url, headers={"User-Agent": "afuture-research/1.0"}
                )
                with urllib.request.urlopen(request, timeout=20) as response:
                    attempt["http_status"] = response.status
                    attempt["content_type"] = response.headers.get("Content-Type", "")
                    attempt["last_modified"] = response.headers.get("Last-Modified", "")
                    raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError("official file exceeds probe bound")
                payload = json.loads(raw)
                if not isinstance(payload, dict) or not isinstance(payload.get("o_cursor"), list):
                    raise ValueError("official response is not a stock JSON table")
                name = f"{day}{kind}.dat"
                (output / name).write_bytes(raw)
                row.update(
                    source_url=url,
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
                attempt["error"] = f"{type(exc).__name__}: {exc}"
            attempts = row["attempts"]
            assert isinstance(attempts, list)
            attempts.append(attempt)
            if "sha256" in row:
                break
        records.append(row)
    (output / "probe.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return records


if __name__ == "__main__":
    result = probe(
        Path(sys.argv[1] if len(sys.argv) > 1 else "shfe-warrant-probe"),
        weekly_only="--weekly-only" in sys.argv[2:],
    )
    print(json.dumps(result, ensure_ascii=False))
    if not any("sha256" in row for row in result):
        sys.exit("no official original could be downloaded")
