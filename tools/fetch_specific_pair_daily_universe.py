"""Fetch targeted specific-contract daily history for the L4 economic-pair check.

Only roots that survived the broad L3 rotation screen are downloaded. Sina returns the
full history for one concrete contract per request. Downloads use a small bounded thread
pool because requests are independent network I/O; retries remain per-contract and every
failure is retained in the evidence instead of being silently filled.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import akshare as ak
import pandas as pd

START = pd.Timestamp("2022-05-01")
END = pd.Timestamp("2026-08-20")

DCE_PRODUCTS = ("P", "Y", "PP", "V", "J", "JM")
SHFE_PRODUCTS = ("AL", "ZN", "BU", "FU", "CU")
KEY_MONTHS = (1, 5, 9)
MAX_WORKERS = 8


def _month_range(start: pd.Timestamp, end: pd.Timestamp):
    current = pd.Timestamp(start.year, start.month, 1)
    final = pd.Timestamp(end.year, end.month, 1)
    while current <= final:
        yield current.year, current.month
        current += pd.offsets.MonthBegin(1)


def contract_symbols() -> list[tuple[str, str, str]]:
    """Return (product, exchange, concrete symbol) without future-data selection."""
    rows: list[tuple[str, str, str]] = []
    for product in DCE_PRODUCTS:
        for year in range(START.year, END.year + 2):
            for month in KEY_MONTHS:
                delivery = pd.Timestamp(year, month, 1)
                if delivery < START - pd.DateOffset(months=4):
                    continue
                if delivery > END + pd.DateOffset(months=6):
                    continue
                rows.append((product, "DCE", f"{product}{year % 100:02d}{month:02d}"))
    for product in SHFE_PRODUCTS:
        for year, month in _month_range(
            START - pd.DateOffset(months=3),
            END + pd.DateOffset(months=5),
        ):
            rows.append((product, "SHFE", f"{product}{year % 100:02d}{month:02d}"))
    return rows


def delivery_date(symbol: str) -> pd.Timestamp:
    digits = "".join(character for character in symbol if character.isdigit())[-4:]
    if len(digits) != 4:
        raise ValueError(f"cannot infer delivery month from {symbol}")
    year = 2000 + int(digits[:2])
    month = int(digits[2:])
    return pd.Timestamp(date(year, month, 15))


def _download(symbol: str) -> pd.DataFrame:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            frame = ak.futures_zh_daily_sina(symbol=symbol).copy()
            if not frame.empty:
                return frame
        except Exception as exc:  # network/provider failures are recorded below
            last_error = exc
        if attempt < 2:
            time.sleep(0.20 * (2**attempt))
    if last_error is not None:
        raise last_error
    return pd.DataFrame()


def _fetch_one(item: tuple[str, str, str]) -> tuple[pd.DataFrame | None, str | None]:
    product, exchange, symbol = item
    try:
        frame = _download(symbol)
    except Exception as exc:
        return None, f"{symbol}: {type(exc).__name__}: {exc}"
    if frame.empty:
        return None, f"{symbol}: empty"
    if "date" not in frame.columns or "close" not in frame.columns:
        return None, f"{symbol}: missing date/close"

    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume", "hold", "settle"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.loc[(frame["date"] >= START) & (frame["date"] <= END)].copy()
    frame.dropna(subset=["date", "close"], inplace=True)
    frame = frame[frame["close"] > 0]
    if frame.empty:
        return None, None
    frame["symbol"] = symbol
    frame["product"] = product
    frame["exchange"] = exchange
    frame["delivery"] = delivery_date(symbol)
    return frame, None


def main() -> None:
    output = Path("runtime")
    output.mkdir(parents=True, exist_ok=True)
    frames: list[pd.DataFrame] = []
    failures: list[str] = []
    requests = contract_symbols()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_fetch_one, item): item for item in requests}
        completed = 0
        for future in as_completed(futures):
            frame, failure = future.result()
            completed += 1
            if frame is not None:
                frames.append(frame)
            if failure:
                failures.append(failure)
            if completed % 50 == 0 or completed == len(requests):
                print(
                    f"downloaded={completed}/{len(requests)} usable={len(frames)} "
                    f"failures={len(failures)}",
                    flush=True,
                )

    if not frames:
        raise RuntimeError("specific-contract daily fetch returned no usable rows")

    data = pd.concat(frames, ignore_index=True)
    data.drop_duplicates(["product", "symbol", "date"], keep="last", inplace=True)
    data.sort_values(["date", "product", "symbol"], inplace=True)
    data.to_csv(output / "specific_pair_daily_contracts.csv", index=False)

    summary = (
        data.groupby("product")
        .agg(
            rows=("date", "size"),
            contracts=("symbol", "nunique"),
            trading_days=("date", "nunique"),
            first=("date", "min"),
            last=("date", "max"),
        )
        .reset_index()
        .sort_values("product")
    )
    summary.to_csv(output / "specific_pair_daily_summary.csv", index=False)
    (output / "specific_pair_daily_failures.txt").write_text(
        "\n".join(sorted(failures)), encoding="utf-8"
    )
    print(summary.to_string(index=False))
    print(
        f"\nrows={len(data)} contracts={data['symbol'].nunique()} "
        f"products={data['product'].nunique()} failures={len(failures)}",
        flush=True,
    )


if __name__ == "__main__":
    main()
