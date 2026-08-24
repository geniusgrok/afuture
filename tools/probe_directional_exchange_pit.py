"""Bounded per-exchange PIT source coverage probe for Stress-80 Phase 2.

This is a source audit, not a trading strategy.  It intentionally avoids the legacy
``get_receipt`` and ``get_rank_sum_daily`` aggregation wrappers that previously failed.
The probe calls exchange-specific AKShare interfaces on deterministic trading dates from
the already-frozen directional history and records raw shape/schema/error evidence.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import traceback
from typing import Callable, Mapping

import pandas as pd

WINDOWS = {
    "prior1": ("2022-08-22", "2023-08-20"),
    "prior2": ("2023-08-21", "2024-08-20"),
    "train": ("2024-08-21", "2025-08-20"),
    "validation": ("2025-08-21", "2026-02-20"),
    "oos": ("2026-02-21", "2026-08-20"),
}
SUPPORTED_EXCHANGES = ("CZCE", "DCE", "SHFE")


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


def _summarize_frame(frame: pd.DataFrame) -> dict:
    return {
        "kind": "dataframe",
        "rows": int(len(frame)),
        "columns": [str(item) for item in frame.columns],
        "nonempty": bool(len(frame)),
        "sample": frame.head(2).astype(object).where(pd.notna(frame.head(2)), None).to_dict("records"),
    }


def summarize_payload(payload) -> dict:
    if payload is None:
        return {"kind": "none", "rows": 0, "nonempty": False}
    if payload is False:
        return {"kind": "false", "rows": 0, "nonempty": False}
    if isinstance(payload, pd.DataFrame):
        return _summarize_frame(payload)
    if isinstance(payload, Mapping):
        items: dict[str, dict] = {}
        total_rows = 0
        for raw_key, raw_value in payload.items():
            key = str(raw_key)
            if isinstance(raw_value, pd.DataFrame):
                summary = _summarize_frame(raw_value)
                total_rows += int(summary["rows"])
                items[key] = summary
            else:
                items[key] = {
                    "kind": type(raw_value).__name__,
                    "repr": repr(raw_value)[:500],
                }
        return {
            "kind": "mapping",
            "keys": sorted(items),
            "key_count": int(len(items)),
            "rows": int(total_rows),
            "nonempty": bool(total_rows or items),
            "items": items,
        }
    return {
        "kind": type(payload).__name__,
        "rows": 0,
        "nonempty": False,
        "repr": repr(payload)[:1000],
    }


def _safe_call(name: str, fn: Callable, *args, **kwargs) -> dict:
    try:
        payload = fn(*args, **kwargs)
        return {
            "api": name,
            "ok": True,
            "payload": summarize_payload(payload),
        }
    except Exception as exc:  # source audit must preserve the exact interface failure
        return {
            "api": name,
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc)[:2000],
            "traceback_tail": traceback.format_exc().splitlines()[-8:],
        }


def probe(
    *,
    continuous_raw: pd.DataFrame,
    specific_raw: pd.DataFrame,
) -> dict:
    import akshare as ak

    dates = deterministic_probe_dates(continuous_raw)
    specific = specific_raw.copy()
    specific["product"] = specific["product"].astype(str).str.upper()
    specific["exchange"] = specific["exchange"].astype(str).str.upper()
    product_exchange = (
        specific[["product", "exchange"]]
        .drop_duplicates()
        .sort_values(["exchange", "product"])
    )
    exchange_products = {
        exchange: tuple(
            product_exchange.loc[product_exchange["exchange"] == exchange, "product"]
            .astype(str)
            .tolist()
        )
        for exchange in sorted(product_exchange["exchange"].unique())
    }

    interface_names = {
        "CZCE": {
            "receipt": "futures_warehouse_receipt_czce",
            "rank": "get_rank_table_czce",
        },
        "DCE": {
            "receipt": "futures_warehouse_receipt_dce",
            "rank": "futures_dce_position_rank",
        },
        "SHFE": {
            "receipt": "futures_shfe_warehouse_receipt",
            "rank": "get_shfe_rank_table",
        },
    }

    availability = {
        exchange: {
            role: {
                "api": api_name,
                "exists": bool(hasattr(ak, api_name)),
            }
            for role, api_name in roles.items()
        }
        for exchange, roles in interface_names.items()
    }
    results: list[dict] = []
    for window, date in dates.items():
        for exchange in SUPPORTED_EXCHANGES:
            products = list(exchange_products.get(exchange, ()))
            roles = interface_names[exchange]
            for role, api_name in roles.items():
                row = {
                    "window": window,
                    "date": date,
                    "exchange": exchange,
                    "role": role,
                    "frozen_products": products,
                }
                fn = getattr(ak, api_name, None)
                if fn is None:
                    row.update(
                        {
                            "api": api_name,
                            "ok": False,
                            "error_type": "MissingInterface",
                            "error": f"akshare has no {api_name}",
                        }
                    )
                    results.append(row)
                    continue
                if role == "receipt":
                    outcome = _safe_call(api_name, fn, date=date)
                elif exchange == "CZCE":
                    # Current CZCE rank API publishes all contracts for the date and has
                    # no vars_list argument.
                    outcome = _safe_call(api_name, fn, date=date)
                else:
                    outcome = _safe_call(api_name, fn, date=date, vars_list=products)
                row.update(outcome)
                results.append(row)

    summary: dict[str, dict] = {}
    for exchange in SUPPORTED_EXCHANGES:
        summary[exchange] = {}
        for role in ("receipt", "rank"):
            subset = [
                item
                for item in results
                if item["exchange"] == exchange and item["role"] == role
            ]
            summary[exchange][role] = {
                "attempts": len(subset),
                "ok": sum(bool(item.get("ok")) for item in subset),
                "nonempty": sum(
                    bool(item.get("payload", {}).get("nonempty")) for item in subset
                ),
                "errors": sorted(
                    {
                        str(item.get("error_type"))
                        for item in subset
                        if not item.get("ok")
                    }
                ),
            }

    unsupported = {
        exchange: list(products)
        for exchange, products in exchange_products.items()
        if exchange not in SUPPORTED_EXCHANGES
    }
    return {
        "role": "bounded per-exchange PIT source coverage audit",
        "strategy_evaluation": False,
        "akshare_version": str(getattr(ak, "__version__", "unknown")),
        "legacy_wrappers_used": False,
        "probe_dates": dates,
        "exchange_products": {key: list(value) for key, value in exchange_products.items()},
        "unsupported_exchange_products": unsupported,
        "interfaces": availability,
        "summary": summary,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--continuous", default="runtime/broad_daily_universe.csv")
    parser.add_argument(
        "--specific", default="runtime/return_target_specific_contracts.csv"
    )
    parser.add_argument("--output", default="runtime/exchange_pit_probe.json")
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
