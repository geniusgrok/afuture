"""Fetch a broad Chinese futures continuous daily universe for factor screening.

L3 breadth evidence only. Requests are independent provider I/O, so they use a bounded
thread pool; every failed root remains explicit in the evidence instead of being filled.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

START = pd.Timestamp("2022-08-22")
END = pd.Timestamp("2026-08-20")
PRODUCTS = (
    "A",
    "B",
    "C",
    "CS",
    "EB",
    "EG",
    "I",
    "J",
    "JM",
    "L",
    "LH",
    "M",
    "P",
    "PG",
    "PP",
    "V",
    "Y",
    "AP",
    "CF",
    "CJ",
    "FG",
    "MA",
    "OI",
    "PF",
    "PK",
    "RM",
    "SA",
    "SF",
    "SM",
    "SR",
    "TA",
    "UR",
    "AG",
    "AL",
    "AU",
    "BU",
    "CU",
    "FU",
    "HC",
    "NI",
    "PB",
    "RB",
    "RU",
    "SN",
    "SP",
    "SS",
    "ZN",
    "BC",
    "LU",
    "NR",
)
MIN_ROWS = 400
MAX_WORKERS = 8


def _download(product: str) -> tuple[pd.DataFrame | None, str | None]:
    import akshare as ak

    symbol = f"{product}0"
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            frame = ak.futures_zh_daily_sina(symbol=symbol).copy()
            if not frame.empty:
                break
        except Exception as exc:
            last_error = exc
            frame = pd.DataFrame()
        if attempt < 2:
            time.sleep(0.2 * (2**attempt))
    if frame.empty:
        if last_error is not None:
            return None, f"{symbol}: {type(last_error).__name__}: {last_error}"
        return None, f"{symbol}: empty"
    if "date" not in frame.columns or "close" not in frame.columns:
        return None, f"{symbol}: missing date/close columns"
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume", "hold", "settle"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.loc[(frame["date"] >= START) & (frame["date"] <= END)].copy()
    frame.dropna(subset=["date", "close"], inplace=True)
    frame.drop_duplicates(["date"], keep="last", inplace=True)
    frame.sort_values("date", inplace=True)
    if len(frame) < MIN_ROWS:
        return None, f"{symbol}: only {len(frame)} rows in requested window"
    frame["product"] = product
    frame["symbol"] = symbol
    return frame, None


def main() -> None:
    output = Path("runtime")
    output.mkdir(parents=True, exist_ok=True)
    frames: list[pd.DataFrame] = []
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_download, product): product for product in PRODUCTS}
        completed = 0
        for future in as_completed(futures):
            frame, failure = future.result()
            completed += 1
            if frame is not None:
                frames.append(frame)
            if failure:
                failures.append(failure)
            if completed % 10 == 0 or completed == len(PRODUCTS):
                print(
                    f"downloaded={completed}/{len(PRODUCTS)} usable={len(frames)} failures={len(failures)}",
                    flush=True,
                )
    if not frames:
        raise RuntimeError("broad daily futures universe returned no usable history")
    data = pd.concat(frames, ignore_index=True)
    data.sort_values(["date", "product"], inplace=True)
    data.to_csv(output / "broad_daily_universe.csv", index=False)
    summary = (
        data.groupby("product")
        .agg(
            rows=("date", "size"),
            first=("date", "min"),
            last=("date", "max"),
            median_volume=("volume", "median"),
            median_hold=("hold", "median"),
        )
        .reset_index()
        .sort_values("product")
    )
    summary.to_csv(output / "broad_daily_universe_summary.csv", index=False)
    (output / "broad_daily_universe_failures.txt").write_text(
        "\n".join(sorted(failures)), encoding="utf-8"
    )
    print(summary.to_string(index=False))
    print(f"\nusable_products={len(summary)} requested_products={len(PRODUCTS)}")
    if failures:
        print("\nUnavailable/insufficient roots (excluded from screen):")
        print("\n".join(sorted(failures)))


if __name__ == "__main__":
    main()
