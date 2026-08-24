"""Fetch/cache CZCE + SHFE Top20 member-flow history for one fixed window."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
import traceback

import pandas as pd

from afuture.directional_member_flow import aggregate_member_flow

WINDOWS = {
    "prior1": ("2022-08-22", "2023-08-20"),
    "prior2": ("2023-08-21", "2024-08-20"),
    "train": ("2024-08-21", "2025-08-20"),
    "validation": ("2025-08-21", "2026-02-20"),
    "oos": ("2026-02-21", "2026-08-20"),
}
EXCHANGES = ("CZCE", "SHFE")


def _trading_dates(continuous_raw: pd.DataFrame, start: str, end: str) -> list[pd.Timestamp]:
    frame = continuous_raw.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    dates = pd.DatetimeIndex(sorted(frame["date"].dropna().unique()))
    mask = (dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))
    return [pd.Timestamp(item) for item in dates[mask]]


def fetch_window(
    *,
    window: str,
    continuous_raw: pd.DataFrame,
    specific_raw: pd.DataFrame,
    pause_seconds: float = 0.05,
) -> tuple[pd.DataFrame, dict]:
    import akshare as ak

    if window not in WINDOWS:
        raise ValueError(f"unknown member-flow window: {window}")
    start, end = WINDOWS[window]
    dates = _trading_dates(continuous_raw, start, end)
    specific = specific_raw.copy()
    specific["product"] = specific["product"].astype(str).str.upper()
    specific["exchange"] = specific["exchange"].astype(str).str.upper()
    products = {
        exchange: tuple(
            sorted(
                specific.loc[specific["exchange"] == exchange, "product"]
                .dropna()
                .astype(str)
                .unique()
            )
        )
        for exchange in EXCHANGES
    }
    all_products = sorted(set(products["CZCE"]) | set(products["SHFE"]))
    rows: list[dict] = []
    failures: list[dict] = []
    successes = {exchange: 0 for exchange in EXCHANGES}

    for date in dates:
        date_text = date.strftime("%Y%m%d")
        day_pressure: dict[str, float] = {}
        calls = (
            (
                "CZCE",
                lambda: ak.get_rank_table_czce(date=date_text),
            ),
            (
                "SHFE",
                lambda: ak.get_shfe_rank_table(
                    date=date_text, vars_list=list(products["SHFE"])
                ),
            ),
        )
        for exchange, callback in calls:
            try:
                payload = callback()
                if not isinstance(payload, dict):
                    raise TypeError(
                        f"{exchange} member-rank payload is {type(payload).__name__}, expected dict"
                    )
                pressure = aggregate_member_flow(
                    payload,
                    exchange=exchange,
                    products=products[exchange],
                )
                if pressure:
                    successes[exchange] += 1
                    day_pressure.update(pressure)
                else:
                    failures.append(
                        {
                            "date": date_text,
                            "exchange": exchange,
                            "error_type": "EmptyNormalizedPayload",
                            "error": "rank API returned no usable frozen-product pressure",
                        }
                    )
            except Exception as exc:
                failures.append(
                    {
                        "date": date_text,
                        "exchange": exchange,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:1000],
                        "traceback_tail": traceback.format_exc().splitlines()[-5:],
                    }
                )
            if pause_seconds > 0:
                time.sleep(float(pause_seconds))
        row = {"date": date.strftime("%Y-%m-%d")}
        row.update({product: day_pressure.get(product) for product in all_products})
        rows.append(row)

    panel = pd.DataFrame(rows)
    audit = {
        "role": "fixed PIT Top20 member-flow cache",
        "window": window,
        "start": start,
        "end": end,
        "akshare_version": str(getattr(ak, "__version__", "unknown")),
        "legacy_wrappers_used": False,
        "trading_dates": len(dates),
        "products": {key: list(value) for key, value in products.items()},
        "success_days": successes,
        "coverage": {
            exchange: (
                successes[exchange] / len(dates) if dates else 0.0
            )
            for exchange in EXCHANGES
        },
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
    parser.add_argument("--output-dir", default="runtime/member_flow")
    args = parser.parse_args()
    panel, audit = fetch_window(
        window=args.window,
        continuous_raw=pd.read_csv(args.continuous),
        specific_raw=pd.read_csv(args.specific),
    )
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    panel.to_csv(output / f"member_flow_{args.window}.csv", index=False)
    (output / f"member_flow_{args.window}_audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "window": args.window,
                "rows": len(panel),
                "coverage": audit["coverage"],
                "failure_count": audit["failure_count"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
