"""Coverage-only 60m collector for the remaining frozen DCE products.

The product set is structural, not outcome-selected: B, CS, EB, LH and PG are exactly the
DCE roots in the frozen 50-product universe not already covered by the validated 13-product
60m OI information surface. They are queried with the same 01/05/09 key-month contract
schedule already used successfully for the earlier DCE coverage work. No strategy return
or product performance is read here.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

import akshare as ak
import pandas as pd

PRODUCTS = ("B", "CS", "EB", "LH", "PG")
ERAS = {
    "prior": {
        "start": pd.Timestamp("2022-08-21"),
        "end": pd.Timestamp("2024-08-20 23:59:59"),
        "contracts": (
            "2209", "2301", "2305", "2309",
            "2401", "2405", "2409", "2501",
        ),
    },
    "recent": {
        "start": pd.Timestamp("2024-08-21"),
        "end": pd.Timestamp("2026-08-20 23:59:59"),
        "contracts": (
            "2409", "2501", "2505", "2509",
            "2601", "2605", "2609", "2701",
        ),
    },
}


def _product(symbol: str) -> str:
    match = re.match(r"[A-Za-z]+", str(symbol))
    return match.group(0).upper() if match else ""


def collect(era: str) -> tuple[pd.DataFrame, dict]:
    if era not in ERAS:
        raise ValueError(f"unknown era: {era}")
    spec = ERAS[era]
    frames: list[pd.DataFrame] = []
    calls: list[dict] = []
    for product in PRODUCTS:
        for contract in spec["contracts"]:
            symbol = f"{product}{contract}"
            call = {"product": product, "symbol": symbol}
            try:
                frame = ak.futures_zh_minute_sina(symbol=symbol, period="60").copy()
                if not isinstance(frame, pd.DataFrame) or frame.empty:
                    raise ValueError("empty/non-DataFrame 60m response")
                time_col = (
                    "datetime" if "datetime" in frame.columns else str(frame.columns[0])
                )
                frame[time_col] = pd.to_datetime(frame[time_col], errors="coerce")
                frame = frame.loc[
                    (frame[time_col] >= spec["start"])
                    & (frame[time_col] <= spec["end"])
                ].copy()
                if frame.empty:
                    call.update({"ok": True, "rows": 0, "first": None, "last": None})
                else:
                    frame.rename(columns={time_col: "datetime"}, inplace=True)
                    frame["symbol"] = symbol
                    frame["product"] = _product(symbol)
                    frames.append(frame)
                    call.update(
                        {
                            "ok": True,
                            "rows": int(len(frame)),
                            "first": frame["datetime"].min().isoformat(),
                            "last": frame["datetime"].max().isoformat(),
                        }
                    )
            except Exception as exc:
                call.update(
                    {
                        "ok": False,
                        "rows": 0,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:1000],
                    }
                )
            calls.append(call)

    if frames:
        data = pd.concat(frames, ignore_index=True)
        data.sort_values(["datetime", "product", "symbol"], inplace=True)
        data.drop_duplicates(
            ["datetime", "product", "symbol"], keep="last", inplace=True
        )
    else:
        data = pd.DataFrame(
            columns=[
                "datetime", "open", "high", "low", "close", "volume", "hold",
                "symbol", "product",
            ]
        )
    report = {
        "role": "remaining frozen DCE key-month 60m coverage collection only",
        "strategy_evaluation": False,
        "era": era,
        "start": spec["start"].date().isoformat(),
        "end": spec["end"].date().isoformat(),
        "products": list(PRODUCTS),
        "contracts": list(spec["contracts"]),
        "provider": "akshare.futures_zh_minute_sina(period=60)",
        "calls": calls,
    }
    return data, report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--era", choices=sorted(ERAS), required=True)
    parser.add_argument("--output-dir", default="runtime/dce_remaining_60m")
    args = parser.parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    data, report = collect(args.era)
    data.to_csv(output / f"dce_remaining_{args.era}_60m.csv", index=False)
    (output / f"dce_remaining_{args.era}_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"era": args.era, "rows": len(data), "calls": len(report["calls"])},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
