"""Coverage-only audit for AKShare/99QH long-history commodity inventory.

This source is third-party rather than exchange-native.  The probe intentionally does
not define or backtest an Alpha rule; it only records historical coverage against the
frozen directional trading calendar and preserves data-provenance caveats.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
import traceback

import pandas as pd

WINDOWS = {
    "prior1": ("2022-08-22", "2023-08-20"),
    "prior2": ("2023-08-21", "2024-08-20"),
    "train": ("2024-08-21", "2025-08-20"),
    "validation": ("2025-08-21", "2026-02-20"),
    "oos": ("2026-02-21", "2026-08-20"),
}


def _calendar(raw: pd.DataFrame) -> pd.DatetimeIndex:
    frame = raw.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    return pd.DatetimeIndex(sorted(frame["date"].dropna().unique()))


def probe(
    *,
    continuous_raw: pd.DataFrame,
    specific_raw: pd.DataFrame,
    pause_seconds: float = 0.05,
) -> dict:
    import akshare as ak

    calendar = _calendar(continuous_raw)
    specific = specific_raw.copy()
    specific["product"] = specific["product"].astype(str).str.upper()
    products = tuple(sorted(specific["product"].dropna().unique()))
    results: dict[str, dict] = {}
    for product in products:
        try:
            frame = ak.futures_inventory_99(symbol=product)
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                raise ValueError("inventory99 returned empty payload")
            date_column = "日期" if "日期" in frame.columns else None
            inventory_column = "库存" if "库存" in frame.columns else None
            if date_column is None or inventory_column is None:
                raise ValueError(
                    f"unexpected inventory99 schema: {list(frame.columns)}"
                )
            dates = pd.to_datetime(frame[date_column], errors="coerce").dt.normalize()
            inventory = pd.to_numeric(frame[inventory_column], errors="coerce")
            clean = pd.DataFrame({"date": dates, "inventory": inventory}).dropna()
            clean = clean[~clean["date"].duplicated(keep="last")].sort_values("date")
            if clean.empty:
                raise ValueError("inventory99 had no valid dated inventory rows")
            date_set = set(clean["date"].tolist())
            window_coverage: dict[str, dict] = {}
            for name, (start, end) in WINDOWS.items():
                trading_days = calendar[
                    (calendar >= pd.Timestamp(start)) & (calendar <= pd.Timestamp(end))
                ]
                exact = sum(pd.Timestamp(day) in date_set for day in trading_days)
                observed = clean[
                    (clean["date"] >= pd.Timestamp(start))
                    & (clean["date"] <= pd.Timestamp(end))
                ]
                window_coverage[name] = {
                    "trading_days": int(len(trading_days)),
                    "exact_observation_days": int(exact),
                    "exact_observation_ratio": (
                        float(exact / len(trading_days)) if len(trading_days) else 0.0
                    ),
                    "source_rows": int(len(observed)),
                }
            results[product] = {
                "ok": True,
                "rows": int(len(clean)),
                "start": clean["date"].iloc[0].strftime("%Y-%m-%d"),
                "end": clean["date"].iloc[-1].strftime("%Y-%m-%d"),
                "windows": window_coverage,
            }
        except Exception as exc:
            results[product] = {
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc)[:1500],
                "traceback_tail": traceback.format_exc().splitlines()[-5:],
            }
        if pause_seconds > 0:
            time.sleep(float(pause_seconds))

    successful = {product: row for product, row in results.items() if row.get("ok")}
    covers_full_range = [
        product
        for product, row in successful.items()
        if pd.Timestamp(row["start"]) <= pd.Timestamp(WINDOWS["prior1"][0])
        and pd.Timestamp(row["end"]) >= pd.Timestamp(WINDOWS["oos"][1])
    ]
    return {
        "role": "coverage-only third-party inventory source audit",
        "strategy_evaluation": False,
        "source": "99QH via AKShare futures_inventory_99",
        "exchange_native": False,
        "historical_publication_timestamps_available": False,
        "revision_risk": "source is fetched retrospectively; treat point-in-time provenance as weaker than exchange-native daily publications",
        "akshare_version": str(getattr(ak, "__version__", "unknown")),
        "products_attempted": len(products),
        "products_successful": len(successful),
        "products_covering_full_study_range": len(covers_full_range),
        "full_range_products": sorted(covers_full_range),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--continuous", default="runtime/broad_daily_universe.csv")
    parser.add_argument(
        "--specific", default="runtime/return_target_specific_contracts.csv"
    )
    parser.add_argument("--output", default="runtime/inventory99_probe.json")
    args = parser.parse_args()
    report = probe(
        continuous_raw=pd.read_csv(args.continuous),
        specific_raw=pd.read_csv(args.specific),
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "attempted": report["products_attempted"],
                "successful": report["products_successful"],
                "full_range": report["products_covering_full_study_range"],
                "full_range_products": report["full_range_products"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
