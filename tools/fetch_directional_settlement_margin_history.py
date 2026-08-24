"""Collect point-in-time SHFE/INE speculative settlement margin history.

This is a coverage/data-lineage tool only. It fetches exchange-published settlement
parameters for frozen full_recent trading dates and stores the conservative side ratio
max(spec_long_margin_ratio, spec_short_margin_ratio). Missing calls/rows are recorded and
never forward-filled. DCE/CZCE remain explicit fallback-to-proxy boundaries.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd

WINDOWS = {
    "train": ("2024-08-21", "2025-08-20"),
    "validation": ("2025-08-21", "2026-02-20"),
    "oos": ("2026-02-21", "2026-08-20"),
}
MARKETS = ("SHFE", "INE")
RATIO_COLUMNS = ("spec_long_margin_ratio", "spec_short_margin_ratio")


def _normalize_symbol(value) -> str:
    return str(value).strip().upper().replace(" ", "")


def _normalize_variety(value) -> str:
    text = _normalize_symbol(value)
    return "".join(character for character in text if character.isalpha())


def _window_dates(specific_raw: pd.DataFrame, window: str) -> pd.DatetimeIndex:
    if window not in WINDOWS:
        raise ValueError(f"unknown window: {window}")
    frame = specific_raw.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    start, end = map(pd.Timestamp, WINDOWS[window])
    dates = pd.DatetimeIndex(
        sorted(frame.loc[frame["date"].between(start, end), "date"].dropna().unique())
    )
    if not len(dates):
        raise ValueError(f"no frozen specific-contract dates for {window}")
    return dates


def collect_window(
    *,
    specific_raw: pd.DataFrame,
    window: str,
    sleep_seconds: float = 0.05,
) -> tuple[pd.DataFrame, dict]:
    import akshare as ak

    specific = specific_raw.copy()
    specific["date"] = pd.to_datetime(specific["date"], errors="coerce").dt.normalize()
    specific["exchange"] = specific["exchange"].astype(str).str.upper()
    symbol_col = next(
        (name for name in ("symbol", "contract", "contract_symbol") if name in specific.columns),
        None,
    )
    if symbol_col is None:
        raise ValueError("specific-contract input has no symbol column")
    specific["_symbol"] = specific[symbol_col].map(_normalize_symbol)
    dates = _window_dates(specific, window)

    rows: list[dict] = []
    calls: list[dict] = []
    for day in dates:
        date_text = pd.Timestamp(day).strftime("%Y%m%d")
        for market in MARKETS:
            expected = set(
                specific.loc[
                    (specific["date"] == day) & (specific["exchange"] == market),
                    "_symbol",
                ].dropna()
            )
            call = {
                "date": pd.Timestamp(day).date().isoformat(),
                "market": market,
                "expected_symbols": len(expected),
            }
            try:
                frame = ak.futures_settle(date=date_text, market=market)
                if not isinstance(frame, pd.DataFrame) or frame.empty:
                    raise ValueError("futures_settle returned empty/non-DataFrame payload")
                required = {"symbol", "variety", *RATIO_COLUMNS}
                missing = required - set(frame.columns)
                if missing:
                    raise ValueError(f"settlement schema missing columns: {sorted(missing)}")
                data = frame.copy()
                data["symbol"] = data["symbol"].map(_normalize_symbol)
                data["variety"] = data["variety"].map(_normalize_variety)
                for column in RATIO_COLUMNS:
                    data[column] = pd.to_numeric(data[column], errors="coerce")
                valid = data[
                    data[list(RATIO_COLUMNS)].notna().all(axis=1)
                    & (data[list(RATIO_COLUMNS)] > 0.0).all(axis=1)
                    & (data[list(RATIO_COLUMNS)] <= 1.0).all(axis=1)
                ].copy()
                valid["conservative_margin_ratio"] = valid[list(RATIO_COLUMNS)].max(axis=1)
                valid_symbols = set(valid["symbol"])
                matched_expected = expected & valid_symbols
                call.update(
                    {
                        "ok": True,
                        "rows": int(len(frame)),
                        "valid_rows": int(len(valid)),
                        "matched_expected_symbols": len(matched_expected),
                        "exact_symbol_coverage": (
                            len(matched_expected) / len(expected) if expected else None
                        ),
                    }
                )
                for item in valid[["symbol", "variety", *RATIO_COLUMNS, "conservative_margin_ratio"]].to_dict("records"):
                    rows.append(
                        {
                            "date": pd.Timestamp(day).date().isoformat(),
                            "market": market,
                            **item,
                        }
                    )
            except Exception as exc:
                call.update(
                    {
                        "ok": False,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:1000],
                        "matched_expected_symbols": 0,
                        "exact_symbol_coverage": 0.0 if expected else None,
                    }
                )
            calls.append(call)
            if sleep_seconds > 0:
                time.sleep(float(sleep_seconds))

    history = pd.DataFrame(rows)
    if not history.empty:
        history.sort_values(["date", "market", "symbol"], inplace=True)
        history.drop_duplicates(["date", "market", "symbol"], keep="last", inplace=True)
    exact_values = [
        float(item["exact_symbol_coverage"])
        for item in calls
        if item.get("exact_symbol_coverage") is not None
    ]
    market_summary = {}
    for market in MARKETS:
        subset = [item for item in calls if item["market"] == market]
        values = [
            float(item["exact_symbol_coverage"])
            for item in subset
            if item.get("exact_symbol_coverage") is not None
        ]
        market_summary[market] = {
            "attempts": len(subset),
            "ok": sum(bool(item.get("ok")) for item in subset),
            "mean_exact_symbol_coverage": float(np.mean(values)) if values else 0.0,
            "min_exact_symbol_coverage": float(np.min(values)) if values else 0.0,
            "full_coverage_days": sum(abs(value - 1.0) <= 1e-12 for value in values),
            "errors": sorted({str(item.get("error_type")) for item in subset if not item.get("ok")}),
        }
    report = {
        "role": "coverage-only full_recent settlement margin history",
        "strategy_evaluation": False,
        "window": window,
        "start": WINDOWS[window][0],
        "end": WINDOWS[window][1],
        "trading_days": len(dates),
        "markets": list(MARKETS),
        "akshare_version": str(getattr(ak, "__version__", "unknown")),
        "ratio_rule": "max(spec_long_margin_ratio, spec_short_margin_ratio)",
        "no_forward_fill": True,
        "fallback_boundary": "missing symbol/day plus DCE/CZCE use existing scenario proxy",
        "overall_mean_exact_symbol_coverage": float(np.mean(exact_values)) if exact_values else 0.0,
        "market_summary": market_summary,
        "calls": calls,
    }
    return history, report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", choices=sorted(WINDOWS), required=True)
    parser.add_argument("--specific", default="runtime/return_target_specific_contracts.csv")
    parser.add_argument("--output-dir", default="runtime/settlement_margin")
    parser.add_argument("--sleep-seconds", type=float, default=0.05)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    history, report = collect_window(
        specific_raw=pd.read_csv(args.specific),
        window=args.window,
        sleep_seconds=args.sleep_seconds,
    )
    history.to_csv(output_dir / f"settlement_margin_{args.window}.csv", index=False)
    (output_dir / f"settlement_margin_{args.window}_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps({"window": args.window, "trading_days": report["trading_days"], "market_summary": report["market_summary"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
