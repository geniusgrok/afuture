"""Coverage-only audit for exchange settlement/margin parameters.

The objective is to test whether the historical Production simulator can replace its
blanket Stress margin proxy with point-in-time exchange-published speculative margin
ratios while retaining the existing safety buffer and hard account gates.  This file
performs no return backtest and defines no Alpha rule.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import traceback

import numpy as np
import pandas as pd

WINDOWS = {
    "prior1": ("2022-08-22", "2023-08-20"),
    "prior2": ("2023-08-21", "2024-08-20"),
    "train": ("2024-08-21", "2025-08-20"),
    "validation": ("2025-08-21", "2026-02-20"),
    "oos": ("2026-02-21", "2026-08-20"),
}
SUPPORTED_MARKETS = ("SHFE", "INE", "CZCE", "GFEX")
RATIO_COLUMNS = ("spec_long_margin_ratio", "spec_short_margin_ratio")


def deterministic_probe_dates(continuous_raw: pd.DataFrame) -> dict[str, str]:
    frame = continuous_raw.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    dates = pd.DatetimeIndex(sorted(frame["date"].dropna().unique()))
    result: dict[str, str] = {}
    for name, (start, end) in WINDOWS.items():
        window = dates[(dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))]
        if len(window) == 0:
            raise ValueError(f"no frozen trading dates for {name}")
        result[name] = pd.Timestamp(window[len(window) // 2]).strftime("%Y%m%d")
    return result


def _normalize_symbol(value) -> str:
    return str(value).strip().upper().replace(" ", "")


def _normalize_variety(value) -> str:
    text = _normalize_symbol(value)
    return "".join(character for character in text if character.isalpha())


def _summarize_ratios(frame: pd.DataFrame) -> dict:
    values: list[float] = []
    for column in RATIO_COLUMNS:
        if column in frame.columns:
            numeric = pd.to_numeric(frame[column], errors="coerce")
            values.extend(float(item) for item in numeric if pd.notna(item))
    finite = np.asarray([item for item in values if np.isfinite(item)], dtype=float)
    if not len(finite):
        return {"count": 0, "min": None, "median": None, "max": None}
    return {
        "count": int(len(finite)),
        "min": float(np.min(finite)),
        "median": float(np.median(finite)),
        "max": float(np.max(finite)),
    }


def probe(
    *,
    continuous_raw: pd.DataFrame,
    specific_raw: pd.DataFrame,
) -> dict:
    import akshare as ak

    dates = deterministic_probe_dates(continuous_raw)
    specific = specific_raw.copy()
    specific["date"] = pd.to_datetime(specific["date"], errors="coerce").dt.normalize()
    specific["product"] = specific["product"].astype(str).str.upper()
    specific["exchange"] = specific["exchange"].astype(str).str.upper()
    symbol_column = next(
        (name for name in ("symbol", "contract", "contract_symbol") if name in specific.columns),
        None,
    )
    if symbol_column is None:
        raise ValueError("specific-contract input has no symbol/contract column")
    specific["_symbol"] = specific[symbol_column].map(_normalize_symbol)

    exchange_products = {
        exchange: tuple(
            sorted(
                specific.loc[specific["exchange"] == exchange, "product"]
                .dropna()
                .astype(str)
                .unique()
            )
        )
        for exchange in sorted(specific["exchange"].dropna().unique())
    }

    results: list[dict] = []
    for window, date_text in dates.items():
        date = pd.Timestamp(date_text)
        for market in SUPPORTED_MARKETS:
            frozen_products = exchange_products.get(market, ())
            if not frozen_products:
                continue
            row = {
                "window": window,
                "date": date_text,
                "market": market,
                "frozen_products": list(frozen_products),
            }
            try:
                frame = ak.futures_settle(date=date_text, market=market)
                if not isinstance(frame, pd.DataFrame) or frame.empty:
                    raise ValueError("futures_settle returned empty/non-DataFrame payload")
                required = {"symbol", "variety", *RATIO_COLUMNS}
                missing = required - set(frame.columns)
                if missing:
                    raise ValueError(f"settlement schema missing columns: {sorted(missing)}")
                normalized = frame.copy()
                normalized["_symbol"] = normalized["symbol"].map(_normalize_symbol)
                normalized["_variety"] = normalized["variety"].map(_normalize_variety)
                for column in RATIO_COLUMNS:
                    normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
                product_set = set(frozen_products)
                matched = normalized[normalized["_variety"].isin(product_set)].copy()
                valid = matched[
                    matched[list(RATIO_COLUMNS)].notna().all(axis=1)
                    & (matched[list(RATIO_COLUMNS)] > 0.0).all(axis=1)
                    & (matched[list(RATIO_COLUMNS)] <= 1.0).all(axis=1)
                ]

                day_specific = specific[
                    (specific["date"] == date)
                    & (specific["exchange"] == market)
                    & specific["product"].isin(product_set)
                ]
                frozen_symbols = set(day_specific["_symbol"].dropna().tolist())
                valid_symbols = set(valid["_symbol"].dropna().tolist())
                exact_symbols = sorted(frozen_symbols & valid_symbols)
                products_with_valid_ratio = sorted(set(valid["_variety"].tolist()))
                row.update(
                    {
                        "ok": True,
                        "rows": int(len(frame)),
                        "matched_rows": int(len(matched)),
                        "valid_ratio_rows": int(len(valid)),
                        "products_with_valid_ratio": products_with_valid_ratio,
                        "product_coverage": (
                            len(products_with_valid_ratio) / len(product_set)
                            if product_set
                            else 0.0
                        ),
                        "frozen_symbols_on_date": len(frozen_symbols),
                        "exact_frozen_symbols_with_margin": len(exact_symbols),
                        "exact_symbol_coverage": (
                            len(exact_symbols) / len(frozen_symbols)
                            if frozen_symbols
                            else None
                        ),
                        "ratio_summary": _summarize_ratios(valid),
                        "sample": valid[
                            ["symbol", "variety", *RATIO_COLUMNS]
                        ].head(8).to_dict("records"),
                    }
                )
            except Exception as exc:
                row.update(
                    {
                        "ok": False,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:2000],
                        "traceback_tail": traceback.format_exc().splitlines()[-8:],
                    }
                )
            results.append(row)

    summary: dict[str, dict] = {}
    for market in SUPPORTED_MARKETS:
        rows = [item for item in results if item["market"] == market]
        if not rows:
            continue
        successful = [item for item in rows if item.get("ok")]
        exact_values = [
            float(item["exact_symbol_coverage"])
            for item in successful
            if item.get("exact_symbol_coverage") is not None
        ]
        product_values = [float(item["product_coverage"]) for item in successful]
        summary[market] = {
            "attempts": len(rows),
            "ok": len(successful),
            "min_product_coverage": min(product_values) if product_values else 0.0,
            "min_exact_symbol_coverage": min(exact_values) if exact_values else None,
            "errors": sorted(
                {str(item.get("error_type")) for item in rows if not item.get("ok")}
            ),
        }

    unsupported = {
        exchange: list(products)
        for exchange, products in exchange_products.items()
        if exchange not in SUPPORTED_MARKETS
    }
    return {
        "role": "coverage-only PIT exchange settlement margin audit",
        "strategy_evaluation": False,
        "akshare_version": str(getattr(ak, "__version__", "unknown")),
        "api": "futures_settle",
        "dce_supported_by_api": False,
        "margin_columns": list(RATIO_COLUMNS),
        "ratio_validity_rule": "0 < speculative margin ratio <= 1",
        "candidate_if_coverage_passes": "exchange speculative side margin ratio * existing 1.25 buffer; missing/DCE keeps existing Stress proxy; hard 35% margin/25% available/2x gross unchanged",
        "probe_dates": dates,
        "exchange_products": {key: list(value) for key, value in exchange_products.items()},
        "unsupported_exchange_products": unsupported,
        "summary": summary,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--continuous", default="runtime/broad_daily_universe.csv")
    parser.add_argument("--specific", default="runtime/return_target_specific_contracts.csv")
    parser.add_argument("--output", default="runtime/settlement_margin_probe.json")
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
    print(json.dumps({"probe_dates": report["probe_dates"], "summary": report["summary"], "unsupported": report["unsupported_exchange_products"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
