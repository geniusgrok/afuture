"""Bounded throwaway probe for historical directional PIT data-source coverage.

This script does not build a warehouse or generate Alpha. It samples deterministic
trading days from the frozen Production artifact and checks whether AKShare can return
basis, registered-receipt and member-position data for the frozen 50-product universe.
The script/workflow are removed after the evidence is recorded.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import akshare as ak
import pandas as pd

WINDOWS = {
    "prior1": ("2022-08-22", "2023-08-20"),
    "prior2": ("2023-08-21", "2024-08-20"),
    "train": ("2024-08-21", "2025-08-20"),
    "validation": ("2025-08-21", "2026-02-20"),
    "oos": ("2026-02-21", "2026-08-20"),
}


def _sample_days(frame: pd.DataFrame) -> dict[str, list[str]]:
    days = pd.DatetimeIndex(sorted(pd.to_datetime(frame["date"]).dropna().unique()))
    result: dict[str, list[str]] = {}
    for name, (start, end) in WINDOWS.items():
        subset = days[(days >= pd.Timestamp(start)) & (days <= pd.Timestamp(end))]
        if len(subset) < 3:
            result[name] = []
            continue
        positions = sorted({int((len(subset) - 1) * fraction) for fraction in (0.2, 0.5, 0.8)})
        result[name] = [pd.Timestamp(subset[pos]).strftime("%Y%m%d") for pos in positions]
    return result


def _infer_products(frame: pd.DataFrame, universe: set[str]) -> list[str]:
    if frame.empty:
        return []
    candidates = (
        "symbol", "var", "variety", "品种代码", "品种", "合约品种", "商品代码"
    )
    best: set[str] = set()
    for column in candidates:
        if column not in frame.columns:
            continue
        values = {str(value).strip().upper() for value in frame[column].dropna()}
        overlap = values & universe
        if len(overlap) > len(best):
            best = overlap
    return sorted(best)


def _probe(
    *,
    name: str,
    date: str,
    universe: list[str],
    call: Callable[[], pd.DataFrame],
) -> dict:
    try:
        frame = call()
        if not isinstance(frame, pd.DataFrame):
            frame = pd.DataFrame(frame)
        return {
            "source": name,
            "date": date,
            "rows": int(len(frame)),
            "columns": [str(column) for column in frame.columns],
            "products": _infer_products(frame, set(universe)),
            "error": "",
        }
    except Exception as exc:  # Evidence probe must preserve failures instead of hiding them.
        return {
            "source": name,
            "date": date,
            "rows": 0,
            "columns": [],
            "products": [],
            "error": f"{type(exc).__name__}: {exc}",
        }


def main() -> None:
    runtime = Path("runtime")
    source = runtime / "return_target_specific_contracts.csv"
    if not source.exists():
        raise SystemExit(f"frozen Production input missing: {source}")
    raw = pd.read_csv(source, usecols=["date", "product"])
    raw["product"] = raw["product"].astype(str).str.upper()
    universe = sorted(raw["product"].dropna().unique())
    samples = _sample_days(raw)

    probes: list[dict] = []
    for window, dates in samples.items():
        for date in dates:
            basis = _probe(
                name="basis",
                date=date,
                universe=universe,
                call=lambda date=date: ak.futures_spot_price_daily(
                    start_day=date, end_day=date, vars_list=universe
                ),
            )
            basis["window"] = window
            probes.append(basis)

            receipt = _probe(
                name="registered_receipt",
                date=date,
                universe=universe,
                call=lambda date=date: ak.get_receipt(
                    start_date=date, end_date=date, vars_list=universe
                ),
            )
            receipt["window"] = window
            probes.append(receipt)

            rank = _probe(
                name="member_rank_sum",
                date=date,
                universe=universe,
                call=lambda date=date: ak.get_rank_sum_daily(
                    start_day=date, end_day=date, vars_list=universe
                ),
            )
            rank["window"] = window
            probes.append(rank)

    report = {
        "role": "bounded historical PIT source coverage probe only",
        "akshare_version": getattr(ak, "__version__", "unknown"),
        "universe": universe,
        "sample_days": samples,
        "probes": probes,
    }
    for source_name in ("basis", "registered_receipt", "member_rank_sum"):
        rows = [item for item in probes if item["source"] == source_name]
        report[source_name] = {
            "successful_samples": sum(not item["error"] for item in rows),
            "nonempty_samples": sum(item["rows"] > 0 for item in rows),
            "sample_count": len(rows),
            "covered_products_union": sorted(
                {product for item in rows for product in item["products"]}
            ),
            "errors": [
                {"window": item["window"], "date": item["date"], "error": item["error"]}
                for item in rows if item["error"]
            ],
        }

    output = runtime / "directional_pit_source_probe.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
