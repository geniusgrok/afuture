"""Fetch concrete daily contracts for the 100% return-target roll-safe L4.

The L3 directional target fit uses 50 continuous commodity roots. L4 downloads concrete
contracts for the exact same roots, then the evaluator chooses the dominant eligible
contract point-in-time from observed open interest/volume. Empty or unavailable contract
months are evidence gaps, never forward-filled synthetic contracts.
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
DELIVERY_BUFFER_MONTHS = 6
MAX_WORKERS = 16

EXCHANGE_PRODUCTS = {
    "DCE": (
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
    ),
    "CZCE": (
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
    ),
    "SHFE": (
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
    ),
    "INE": ("BC", "LU", "NR"),
}
PRODUCTS = tuple(
    product
    for exchange in ("DCE", "CZCE", "SHFE", "INE")
    for product in EXCHANGE_PRODUCTS[exchange]
)
PRODUCT_EXCHANGE = {
    product: exchange for exchange, products in EXCHANGE_PRODUCTS.items() for product in products
}


def _month_range(start: pd.Timestamp, end: pd.Timestamp):
    current = pd.Timestamp(start.year, start.month, 1)
    final = pd.Timestamp(end.year, end.month, 1)
    while current <= final:
        yield current.year, current.month
        current += pd.offsets.MonthBegin(1)


def contract_symbols() -> list[tuple[str, str, str]]:
    """Enumerate concrete YYMM contracts without using future winner information."""
    rows: list[tuple[str, str, str]] = []
    first_delivery = START - pd.DateOffset(months=2)
    last_delivery = END + pd.DateOffset(months=DELIVERY_BUFFER_MONTHS)
    for product in PRODUCTS:
        exchange = PRODUCT_EXCHANGE[product]
        for year, month in _month_range(first_delivery, last_delivery):
            rows.append(
                (
                    product,
                    exchange,
                    f"{product}{year % 100:02d}{month:02d}",
                )
            )
    return rows


def delivery_date(symbol: str) -> pd.Timestamp:
    digits = "".join(character for character in str(symbol) if character.isdigit())[-4:]
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
        except Exception as exc:  # provider/network evidence is retained below
            last_error = exc
        if attempt < 2:
            time.sleep(0.15 * (2**attempt))
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
        return None, None
    required = {"date", "close", "hold", "volume"}
    if not required.issubset(frame.columns):
        return None, f"{symbol}: missing {sorted(required - set(frame.columns))}"

    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume", "hold", "settle"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.loc[(frame["date"] >= START) & (frame["date"] <= END)].copy()
    frame.dropna(subset=["date", "close", "hold"], inplace=True)
    frame = frame[(frame["close"] > 0) & (frame["hold"] >= 0)]
    if frame.empty:
        return None, None
    frame["volume"] = frame["volume"].fillna(0.0)
    frame["symbol"] = symbol
    frame["product"] = product
    frame["exchange"] = exchange
    frame["delivery"] = delivery_date(symbol)
    return frame, None


def main() -> None:
    output = Path("runtime")
    output.mkdir(parents=True, exist_ok=True)
    requests = contract_symbols()
    frames: list[pd.DataFrame] = []
    failures: list[str] = []

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
            if completed % 100 == 0 or completed == len(requests):
                print(
                    f"downloaded={completed}/{len(requests)} usable_contracts={len(frames)} "
                    f"failures={len(failures)}",
                    flush=True,
                )

    if not frames:
        raise RuntimeError("return-target specific-contract fetch returned no usable data")

    data = pd.concat(frames, ignore_index=True)
    data.drop_duplicates(["product", "symbol", "date"], keep="last", inplace=True)
    data.sort_values(["date", "product", "symbol"], inplace=True)
    data.to_csv(output / "return_target_specific_contracts.csv", index=False)

    summary = (
        data.groupby(["exchange", "product"])
        .agg(
            rows=("date", "size"),
            contracts=("symbol", "nunique"),
            trading_days=("date", "nunique"),
            first=("date", "min"),
            last=("date", "max"),
            median_volume=("volume", "median"),
            median_hold=("hold", "median"),
        )
        .reset_index()
        .sort_values(["exchange", "product"])
    )
    summary.to_csv(output / "return_target_specific_summary.csv", index=False)
    (output / "return_target_specific_failures.txt").write_text(
        "\n".join(sorted(failures)), encoding="utf-8"
    )

    missing_products = sorted(set(PRODUCTS) - set(data["product"].unique()))
    if missing_products:
        raise RuntimeError(f"specific-contract fetch missing products: {missing_products}")

    print(summary.to_string(index=False))
    print(
        f"\nrequests={len(requests)} usable_contracts={data['symbol'].nunique()} "
        f"rows={len(data)} products={data['product'].nunique()} failures={len(failures)}",
        flush=True,
    )


if __name__ == "__main__":
    main()
