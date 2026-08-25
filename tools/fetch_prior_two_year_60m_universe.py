"""Fetch a prior broad 60-minute futures universe for causal walk-forward research.

The primary evaluation window remains 2024-08-21 through 2026-08-20. This older
history supplies only information that would have been available before each primary
window decision, so product promotion/demotion can be tested without look-ahead.
"""

from __future__ import annotations

import re
from pathlib import Path

import akshare as ak
import pandas as pd

START = pd.Timestamp("2022-08-21")
END = pd.Timestamp("2024-08-20 23:59:59")
KEY_MONTH_PRODUCTS = (
    "M",
    "C",
    "P",
    "A",
    "Y",
    "I",
    "PP",
    "EG",
    "TA",
    "MA",
    "FG",
    "RM",
    "OI",
    "SA",
)
KEY_MONTH_CONTRACTS = ("2209", "2301", "2305", "2309", "2401", "2405", "2409", "2501")
RB_CONTRACTS = ("2210", "2301", "2305", "2310", "2401", "2405", "2410", "2501")


def product_of(symbol: str) -> str:
    match = re.match(r"[A-Za-z]+", symbol)
    return match.group(0).upper() if match else ""


def symbols() -> list[str]:
    rows = [
        f"{product}{contract}" for product in KEY_MONTH_PRODUCTS for contract in KEY_MONTH_CONTRACTS
    ]
    rows.extend(f"RB{contract}" for contract in RB_CONTRACTS)
    return rows


def main() -> None:
    frames: list[pd.DataFrame] = []
    failures: list[str] = []
    for symbol in symbols():
        try:
            frame = ak.futures_zh_minute_sina(symbol=symbol, period="60").copy()
        except Exception as exc:
            failures.append(f"{symbol}: {type(exc).__name__}: {exc}")
            continue
        if frame.empty:
            failures.append(f"{symbol}: empty")
            continue
        column = "datetime" if "datetime" in frame.columns else str(frame.columns[0])
        frame[column] = pd.to_datetime(frame[column], errors="coerce")
        frame = frame.loc[(frame[column] >= START) & (frame[column] <= END)].copy()
        if frame.empty:
            continue
        frame.rename(columns={column: "datetime"}, inplace=True)
        frame["symbol"] = symbol
        frame["product"] = product_of(symbol)
        frames.append(frame)

    if not frames:
        raise RuntimeError("prior 60m research universe returned no history")

    data = pd.concat(frames, ignore_index=True).sort_values(["datetime", "product", "symbol"])
    output = Path("runtime")
    output.mkdir(parents=True, exist_ok=True)
    data.to_csv(output / "prior_two_year_broad_60m.csv", index=False)

    summary = []
    for product, group in data.groupby("product"):
        summary.append(
            {
                "product": product,
                "rows": int(len(group)),
                "symbols": int(group["symbol"].nunique()),
                "days": int(group["datetime"].dt.date.nunique()),
                "first": group["datetime"].min().isoformat(),
                "last": group["datetime"].max().isoformat(),
            }
        )
    summary_frame = pd.DataFrame(summary).sort_values("product")
    summary_frame.to_csv(output / "prior_two_year_broad_60m_summary.csv", index=False)
    (output / "prior_two_year_broad_60m_failures.txt").write_text(
        "\n".join(failures), encoding="utf-8"
    )
    print(summary_frame.to_string(index=False))
    if failures:
        print("\nUnavailable symbols (excluded from the causal research pool):")
        print("\n".join(failures))


if __name__ == "__main__":
    main()
