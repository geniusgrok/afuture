"""Fetch/cache CZCE registered-receipt flow history for one fixed window."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
import traceback

import pandas as pd

from afuture.directional_receipt_flow import aggregate_czce_receipt_flow

WINDOWS = {
    "prior1": ("2022-08-22", "2023-08-20"),
    "prior2": ("2023-08-21", "2024-08-20"),
    "train": ("2024-08-21", "2025-08-20"),
    "validation": ("2025-08-21", "2026-02-20"),
    "oos": ("2026-02-21", "2026-08-20"),
}


def _trading_dates(raw: pd.DataFrame, start: str, end: str) -> list[pd.Timestamp]:
    frame = raw.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    dates = pd.DatetimeIndex(sorted(frame["date"].dropna().unique()))
    dates = dates[(dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))]
    return [pd.Timestamp(item) for item in dates]


def fetch_window(
    *,
    window: str,
    continuous_raw: pd.DataFrame,
    specific_raw: pd.DataFrame,
    pause_seconds: float = 0.05,
) -> tuple[pd.DataFrame, dict]:
    import akshare as ak

    if window not in WINDOWS:
        raise ValueError(f"unknown receipt-flow window: {window}")
    start, end = WINDOWS[window]
    dates = _trading_dates(continuous_raw, start, end)
    specific = specific_raw.copy()
    specific["product"] = specific["product"].astype(str).str.upper()
    specific["exchange"] = specific["exchange"].astype(str).str.upper()
    products = tuple(
        sorted(
            specific.loc[specific["exchange"] == "CZCE", "product"]
            .dropna()
            .astype(str)
            .unique()
        )
    )
    rows: list[dict] = []
    failures: list[dict] = []
    success_days = 0
    for date in dates:
        date_text = date.strftime("%Y%m%d")
        pressure: dict[str, float] = {}
        try:
            payload = ak.futures_warehouse_receipt_czce(date=date_text)
            if not isinstance(payload, dict):
                raise TypeError(
                    f"CZCE receipt payload is {type(payload).__name__}, expected dict"
                )
            pressure = aggregate_czce_receipt_flow(payload, products=products)
            if pressure:
                success_days += 1
            else:
                failures.append(
                    {
                        "date": date_text,
                        "error_type": "EmptyNormalizedPayload",
                        "error": "receipt API returned no explicit usable product totals",
                    }
                )
        except Exception as exc:
            failures.append(
                {
                    "date": date_text,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:1000],
                    "traceback_tail": traceback.format_exc().splitlines()[-5:],
                }
            )
        row = {"date": date.strftime("%Y-%m-%d")}
        row.update({product: pressure.get(product) for product in products})
        rows.append(row)
        if pause_seconds > 0:
            time.sleep(float(pause_seconds))
    panel = pd.DataFrame(rows)
    audit = {
        "role": "fixed PIT CZCE registered-receipt flow cache",
        "window": window,
        "start": start,
        "end": end,
        "akshare_version": str(getattr(ak, "__version__", "unknown")),
        "legacy_wrappers_used": False,
        "trading_dates": len(dates),
        "products": list(products),
        "success_days": success_days,
        "coverage": success_days / len(dates) if dates else 0.0,
        "failure_count": len(failures),
        "failures": failures,
    }
    return panel, audit


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", choices=tuple(WINDOWS), required=True)
    parser.add_argument("--continuous", default="runtime/broad_daily_universe.csv")
    parser.add_argument(
        "--specific", default="runtime/return_target_specific_contracts.csv"
    )
    parser.add_argument("--output-dir", default="runtime/receipt_flow")
    args = parser.parse_args()
    panel, audit = fetch_window(
        window=args.window,
        continuous_raw=pd.read_csv(args.continuous),
        specific_raw=pd.read_csv(args.specific),
    )
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    panel.to_csv(output / f"receipt_flow_{args.window}.csv", index=False)
    (output / f"receipt_flow_{args.window}_audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps({"window": args.window, "rows": len(panel), "coverage": audit["coverage"], "failure_count": audit["failure_count"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
