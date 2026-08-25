"""Fetch a broader real 60-minute calendar-spread research universe.

The file is research-only. Production still discovers live contracts from CTP; this
history is used to decide which product roots and robust parameter regions deserve
promotion after train/validation/OOS testing.
"""

from __future__ import annotations

import re
from pathlib import Path

import akshare as ak
import pandas as pd

START = pd.Timestamp("2024-08-21")
END = pd.Timestamp("2026-08-20 23:59:59")

# Key-month products keep the research universe close to the lightweight personal
# calendar-spread use case. Monthly metals are deliberately excluded here because their
# contract lifecycle differs and would require a separate selector policy.
KEY_MONTH_PRODUCTS = (
    "M",
    "C",
    "P",
    "A",
    "Y",
    "I",
    "PP",
    "EG",  # DCE
    "TA",
    "MA",
    "FG",
    "RM",
    "OI",
    "SA",  # CZCE
)
KEY_MONTH_CONTRACTS = ("2409", "2501", "2505", "2509", "2601", "2605", "2609", "2701")
RB_CONTRACTS = ("2410", "2501", "2505", "2510", "2601", "2605", "2610", "2701")


def product_of(symbol: str) -> str:
    match = re.match(r"[A-Za-z]+", symbol)
    return match.group(0).upper() if match else ""


def symbols() -> list[str]:
    result = [
        f"{product}{contract}" for product in KEY_MONTH_PRODUCTS for contract in KEY_MONTH_CONTRACTS
    ]
    result.extend(f"RB{contract}" for contract in RB_CONTRACTS)
    return result


def main() -> None:
    output = Path("runtime")
    output.mkdir(parents=True, exist_ok=True)
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
        time_col = "datetime" if "datetime" in frame.columns else str(frame.columns[0])
        frame[time_col] = pd.to_datetime(frame[time_col], errors="coerce")
        frame = frame.loc[(frame[time_col] >= START) & (frame[time_col] <= END)].copy()
        if frame.empty:
            continue
        frame.rename(columns={time_col: "datetime"}, inplace=True)
        frame["symbol"] = symbol
        frame["product"] = product_of(symbol)
        frames.append(frame)

    if not frames:
        raise RuntimeError("broader 60m research universe returned no history")
    combined = pd.concat(frames, ignore_index=True)
    combined.sort_values(["datetime", "product", "symbol"], inplace=True)
    combined.to_csv(output / "two_year_broad_60m.csv", index=False)

    summary = []
    for product, group in combined.groupby("product"):
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
    pd.DataFrame(summary).sort_values("product").to_csv(
        output / "two_year_broad_60m_summary.csv", index=False
    )
    Path(output / "two_year_broad_60m_failures.txt").write_text(
        "\n".join(failures), encoding="utf-8"
    )
    print(pd.DataFrame(summary).sort_values("product").to_string(index=False))
    if failures:
        print("\nUnavailable symbols (kept out of research):")
        print("\n".join(failures))


if __name__ == "__main__":
    main()
