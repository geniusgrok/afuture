"""Run a bounded, private-evidence Stress-90 account replay after the frozen archive.

This entrypoint keeps the old five research inputs immutable, fetches provider returns
into per-symbol raw files, extends the frozen mainline signal, maps 60m OI bars to
exchange trading days, and simulates one continuous account for each cost/margin case.
It is historical research only; it does not connect to a broker or production runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import sys
import time
import urllib.error
import urllib.request
from bisect import bisect_right
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, fields, is_dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import evaluate_directional_60m_oi_confirmation as oi_gate
import evaluate_directional_production_mechanics as mechanics
import evaluate_directional_stress80_final as stress80
import evaluate_directional_stress90_final as stress90_eval
import evaluate_execution_aligned_target as execution_target
import fetch_broad_daily_universe as broad_fetch
import fetch_return_target_specific_daily as specific_fetch
import fetch_two_year_60m_universe as oi_fetch

from afuture.directional_acceptance import (
    PRODUCT_MULTIPLIERS,
    DirectionalSimulationCheckpoint,
    ProductionMechanicsConfig,
)
from afuture.directional_concentration_freeze import (
    ExpandingMedianConcentrationFreezeDirectionalProductionAcceptance,
)
from afuture.directional_stress90_policy import (
    EXPECTED_CANDIDATE_WEIGHT_SHA256,
    STRESS90_POLICY,
    build_stress90_candidate_path,
    candidate_state_digest,
    candidate_weight_digest,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
START = pd.Timestamp("2026-08-21")
OVERLAP_START = START - pd.Timedelta(days=2)
FROZEN_CUTOFF = pd.Timestamp("2026-08-20 23:59:59")
FIXED_MAIN_SHA = "f31f0272bfdd0f4f3a28c5e78d01da716d038a06"
TASK_START_SHANGHAI = "2026-09-23T12:26:23+08:00"
OI_SYMBOL_MONTHS = oi_fetch.KEY_MONTH_CONTRACTS[-2:]
MAX_FETCH_WORKERS = 12
REQUIRED_CONTINUOUS_FIELDS = ("open", "high", "low", "close", "volume", "hold")
REQUIRED_SPECIFIC_FIELDS = ("date", "open", "close", "volume", "hold")
OI_VALUE_FIELDS = ("open", "high", "low", "close", "volume", "hold")
DAY_SESSION_LABELS = ("10:00", "11:15", "14:15", "15:00")
NIGHT_SESSION_LABELS = ("22:00", "23:00")
OFFICIAL_SOURCE_URLS = {
    "DCE": "https://www.dce.com.cn/dalianshangpin/sspz/index.html",
    "CZCE": "https://www.czce.com.cn/cn/sspz/index.shtml",
    "SHFE": "https://www.shfe.com.cn/products/",
    "INE": "https://www.ine.cn/products/",
    "DCE_2026_HOLIDAY": "https://www.dce.com.cn/dce/content/2025/ywggytz/18625615.html",
    "DCE_A": "https://www.dce.com.cn/dalianshangpin/sspz/487124/487128/1391009/index.html",
    "DCE_C": "https://www.dce.com.cn/dalianshangpin/resource/cms/article/8536039/489773/240219%E7%8E%89%E7%B1%B3%E6%9C%9F%E8%B4%A7%E5%92%8C%E6%9C%9F%E6%9D%83%E5%AE%A3%E4%BC%A0%E9%A1%B5.pdf",
    "DCE_EG": "https://www.dce.com.cn/dce/channel/list/136.html",
    "DCE_I": "https://www.dce.com.cn/dalianshangpin/sspz/487477/487481/1500303/index.html",
    "DCE_M": "https://www.dce.com.cn/dalianshangpin/sspz/487180/487184/1391326/index.html",
    "DCE_P": "https://www.dce.com.cn/dce/channel/list/124.html",
    "DCE_PP": "https://www.dce.com.cn/dalianshangpin/fgfz/6142914/6142926/6146588/index.html",
    "DCE_Y": "https://www.dce.com.cn/dalianshangpin/sspz/index.html",
    "CZCE_TA": "https://www.czce.com.cn/cn/uploadfile/2024/02/07/20240207154327860.pdf",
    "CZCE_SESSION": "https://www.czce.com.cn/cn/sspz/index.shtml",
}


def map_oi_bars_to_trading_day(
    raw: pd.DataFrame,
    *,
    trading_days: pd.DatetimeIndex | pd.Series | list,
) -> pd.DataFrame:
    """Map DCE/TA 60m timestamp labels to observed exchange trading days.

    Bars in the 21:00-23:00 night session belong to the next open day in the observed
    market calendar. Day-session bars keep their calendar date. The input timestamp is
    retained and controls chronological first-open/last-close ordering.
    """
    if "datetime" not in raw.columns:
        raise ValueError("OI minute data missing datetime")
    calendar = pd.DatetimeIndex(pd.to_datetime(list(trading_days), errors="coerce")).normalize()
    calendar = calendar[~calendar.isna()].unique().sort_values()
    if calendar.empty:
        raise ValueError("exchange trading calendar is empty")
    calendar_values = list(calendar)
    calendar_set = set(calendar_values)
    frame = raw.copy()
    frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce")
    assigned: list[pd.Timestamp | pd.NaT] = []
    status: list[str] = []
    for value in frame["datetime"]:
        if pd.isna(value):
            assigned.append(pd.NaT)
            status.append("invalid_timestamp")
            continue
        timestamp = pd.Timestamp(value)
        day = timestamp.normalize()
        minute = timestamp.hour * 60 + timestamp.minute
        is_night = 21 * 60 <= minute <= 23 * 60
        is_day = (
            9 * 60 <= minute <= 10 * 60 + 15
            or 10 * 60 + 30 <= minute <= 11 * 60 + 30
            or 13 * 60 + 30 <= minute <= 15 * 60
        )
        if is_night:
            next_position = bisect_right(calendar_values, day)
            if next_position < len(calendar_values):
                assigned.append(calendar_values[next_position])
                status.append("night_to_next_open_day")
            else:
                assigned.append(pd.NaT)
                status.append("after_last_calendar_day")
        elif is_day and day in calendar_set:
            assigned.append(day)
            status.append("same_date_day_session")
        elif is_day:
            assigned.append(pd.NaT)
            status.append("calendar_date_not_open")
        else:
            assigned.append(pd.NaT)
            status.append("outside_session")
    frame["trading_day"] = pd.to_datetime(assigned)
    frame["mapping_status"] = status
    return frame


def _json_default(value):
    if is_dataclass(value):
        return {field.name: getattr(value, field.name) for field in fields(value)}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return value.as_posix()
    raise TypeError(f"cannot JSON encode {type(value).__name__}")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default)
        + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _csv_frame(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, lineterminator="\n")


def _base_weight_prefix_audit(
    generated: pd.DataFrame,
    archived: pd.DataFrame,
    *,
    policy_start: pd.Timestamp,
    cutoff: pd.Timestamp,
    tolerance: float = 1e-12,
) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    """Compare the current fixed policy only where its candidate is frozen.

    Older archived weights remain available as warmup evidence, but pre-policy
    history came from earlier lineages and is reported separately instead of
    being treated as a current-policy parity requirement.
    """
    cutoff = pd.Timestamp(cutoff).normalize()
    policy_start = pd.Timestamp(policy_start).normalize()
    eligible_index = generated.index[
        (generated.index >= policy_start) & (generated.index <= cutoff)
    ]
    pre_policy_index = generated.index[
        (generated.index < policy_start) & (generated.index <= cutoff)
    ]
    columns = list(generated.columns)

    def mismatches(index: pd.Index) -> tuple[int, pd.DataFrame]:
        actual = generated.loc[index, columns].astype(float)
        expected = archived.reindex(index=index, columns=columns, fill_value=0.0).fillna(0.0)
        equal = np.isclose(
            actual.to_numpy(float), expected.to_numpy(float), rtol=0.0, atol=tolerance
        )
        locations = np.argwhere(~equal)
        rows = [
            {
                "date": pd.Timestamp(index[row]).date().isoformat(),
                "product": str(columns[column]),
                "generated": float(actual.iat[row, column]),
                "frozen": float(expected.iat[row, column]),
            }
            for row, column in locations
        ]
        return int(locations.shape[0]), pd.DataFrame(
            rows, columns=["date", "product", "generated", "frozen"]
        )

    mismatch_count, policy_mismatches = mismatches(eligible_index)
    pre_policy_mismatch_count, pre_policy_mismatches = mismatches(pre_policy_index)
    summary = {
        "passed": mismatch_count == 0,
        "comparison_start": eligible_index.min().date().isoformat()
        if len(eligible_index)
        else None,
        "comparison_end": eligible_index.max().date().isoformat() if len(eligible_index) else None,
        "cell_comparisons": int(len(eligible_index) * len(columns)),
        "mismatch_count": mismatch_count,
        "pre_policy_start": pre_policy_index.min().date().isoformat()
        if len(pre_policy_index)
        else None,
        "pre_policy_end": pre_policy_index.max().date().isoformat()
        if len(pre_policy_index)
        else None,
        "pre_policy_cell_comparisons": int(len(pre_policy_index) * len(columns)),
        "pre_policy_mismatch_count": pre_policy_mismatch_count,
        "tolerance": tolerance,
    }
    return summary, policy_mismatches, pre_policy_mismatches


def _month_range(start: pd.Timestamp, end: pd.Timestamp) -> list[tuple[int, int]]:
    result = []
    current = pd.Timestamp(start.year, start.month, 1)
    final = pd.Timestamp(end.year, end.month, 1)
    while current <= final:
        result.append((current.year, current.month))
        current += pd.offsets.MonthBegin(1)
    return result


def _specific_symbols(end: pd.Timestamp) -> list[tuple[str, str, str]]:
    first = (START - pd.DateOffset(months=2)).replace(day=1)
    last = (end + pd.DateOffset(months=specific_fetch.DELIVERY_BUFFER_MONTHS)).replace(day=1)
    months = _month_range(first, last)
    return [
        (product, specific_fetch.PRODUCT_EXCHANGE[product], f"{product}{year % 100:02d}{month:02d}")
        for product in specific_fetch.PRODUCTS
        for year, month in months
    ]


def _fetch_job(kind: str, symbol: str, period: str | None = None) -> dict:
    import akshare as ak

    request_started = datetime.now(SHANGHAI).isoformat()
    errors = []
    started = time.perf_counter()
    frame = pd.DataFrame()
    attempts = 0
    for attempts in (1, 2):
        try:
            if kind == "minute_oi":
                frame = ak.futures_zh_minute_sina(symbol=symbol, period="60").copy()
            else:
                frame = ak.futures_zh_daily_sina(symbol=symbol).copy()
            errors = []
            break
        except Exception as exc:  # exact error remains in the private run evidence
            errors.append(f"{type(exc).__name__}: {exc}")
            if attempts == 1:
                time.sleep(0.5)
    finished = datetime.now(SHANGHAI).isoformat()
    return {
        "kind": kind,
        "symbol": symbol,
        "period": period,
        "request_started_shanghai": request_started,
        "response_received_shanghai": finished,
        "elapsed_seconds": time.perf_counter() - started,
        "attempts": attempts,
        "frame": frame,
        "error": " | ".join(errors),
    }


def _extract_contract_spec(symbol: str, raw: pd.DataFrame) -> dict:
    values = {}
    if {"item", "value"}.issubset(raw.columns):
        values = {
            str(item).strip(): str(value).strip()
            for item, value in zip(raw["item"], raw["value"], strict=False)
        }

    def first_number(value: str | None) -> float:
        match = re.search(r"\d+(?:\.\d+)?", value or "")
        return float(match.group()) if match else np.nan

    symbol = str(symbol).upper()
    product_match = re.match(r"[A-Z]+", symbol)
    product = product_match.group() if product_match else ""
    provider_multiplier = first_number(values.get("交易单位"))
    expected_multiplier = PRODUCT_MULTIPLIERS.get(product, np.nan)
    multiplier_matches = (
        bool(np.isclose(provider_multiplier, expected_multiplier, rtol=0.0, atol=1e-12))
        if np.isfinite(provider_multiplier) and np.isfinite(expected_multiplier)
        else False
    )
    return {
        "product": product,
        "symbol": symbol,
        "provider_multiplier": provider_multiplier,
        "provider_trading_unit_raw": values.get("交易单位", ""),
        "frozen_model_multiplier": expected_multiplier,
        "model_multiplier_matches_provider": multiplier_matches,
        "price_tick": first_number(values.get("最小变动价位")),
        "price_tick_raw": values.get("最小变动价位", ""),
        "price_tick_source": "AKShare/Sina futures_contract_detail response",
        "price_tick_used_in_simulation": False,
        "last_trading_day_rule": values.get("最后交易日", ""),
        "last_delivery_day_rule": values.get("最后交割日", ""),
        "delivery_months_rule": values.get("合约交割月份", ""),
        "trading_hours_rule": values.get("交易时间", ""),
        "official_rule_url": OFFICIAL_SOURCE_URLS.get(
            f"{specific_fetch.PRODUCT_EXCHANGE.get(product, '')}_{product}",
            OFFICIAL_SOURCE_URLS.get(specific_fetch.PRODUCT_EXCHANGE.get(product, ""), ""),
        ),
        "provider_fields": json.dumps(values, ensure_ascii=False, sort_keys=True),
    }


def _fetch_contract_spec_job(symbol: str) -> dict:
    import akshare as ak

    request_started = datetime.now(SHANGHAI).isoformat()
    errors = []
    started = time.perf_counter()
    frame = pd.DataFrame()
    attempts = 0
    for attempts in (1, 2):
        try:
            frame = ak.futures_contract_detail(symbol=symbol).copy()
            errors = []
            break
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            if attempts == 1:
                time.sleep(0.5)
    return {
        "symbol": symbol,
        "request_started_shanghai": request_started,
        "response_received_shanghai": datetime.now(SHANGHAI).isoformat(),
        "elapsed_seconds": time.perf_counter() - started,
        "attempts": attempts,
        "frame": frame,
        "error": " | ".join(errors),
    }


def _fetch_selected_contract_specs(
    symbols: list[str],
    *,
    output: Path,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    unique_symbols = sorted(set(str(symbol).upper() for symbol in symbols))
    spec_rows = []
    query_rows = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(_fetch_contract_spec_job, symbol): symbol for symbol in unique_symbols
        }
        for number, future in enumerate(as_completed(futures), 1):
            response = future.result()
            raw = response.pop("frame")
            symbol = response["symbol"]
            raw_path = output / "market_raw" / "contract_specs" / f"{symbol}.csv"
            _csv_frame(raw, raw_path)
            result = "error" if response["error"] else "empty_response" if raw.empty else "ok"
            query_rows.append(
                {
                    **response,
                    "result": result,
                    "rows_returned": int(len(raw)),
                    "columns_returned": ",".join(str(column) for column in raw.columns),
                    "raw_file": raw_path.relative_to(output).as_posix(),
                    "raw_file_sha256": _sha256(raw_path),
                }
            )
            if not raw.empty:
                spec_rows.append(_extract_contract_spec(symbol, raw))
            if number % 25 == 0 or number == len(unique_symbols):
                print(f"contract_spec_fetch={number}/{len(unique_symbols)}", flush=True)
    specs = (
        pd.DataFrame(spec_rows).sort_values("symbol").reset_index(drop=True)
        if spec_rows
        else pd.DataFrame()
    )
    queries = (
        pd.DataFrame(query_rows).sort_values("symbol").reset_index(drop=True)
        if query_rows
        else pd.DataFrame()
    )
    _csv_frame(specs, output / "coverage" / "selected_contract_specs.csv")
    _csv_frame(queries, output / "market_spec_query_log.csv")
    missing = sorted(set(unique_symbols) - set(specs.symbol.astype(str)))
    if missing:
        raise RuntimeError(f"selected contract specification responses are missing: {missing}")
    return specs, queries


def _account_multipliers_from_specs(
    selected_specs: pd.DataFrame,
) -> tuple[dict[str, float], pd.DataFrame, pd.DataFrame]:
    """Use verified effective contract multipliers in account economics only."""
    required = {"product", "symbol", "provider_multiplier"}
    if not required.issubset(selected_specs.columns) or selected_specs.empty:
        raise RuntimeError("selected contract multiplier evidence is missing")

    account_multipliers = PRODUCT_MULTIPLIERS.copy()
    audited_specs = selected_specs.copy()
    audited_specs["provider_multiplier"] = pd.to_numeric(
        audited_specs.provider_multiplier, errors="coerce"
    )
    if (
        audited_specs.provider_multiplier.isna().any()
        or not np.isfinite(audited_specs.provider_multiplier.to_numpy(float)).all()
    ):
        missing = (
            audited_specs.loc[audited_specs.provider_multiplier.isna(), "symbol"]
            .astype(str)
            .tolist()
        )
        raise RuntimeError(f"effective contract multipliers are missing: {missing}")

    rows = []
    for product, group in audited_specs.groupby("product", sort=True):
        values = sorted(set(group.provider_multiplier.astype(float)))
        if len(values) != 1:
            raise RuntimeError(
                "selected contracts for one product have conflicting effective multipliers: "
                f"product={product} symbols={group.symbol.tolist()} multipliers={values}"
            )
        effective = values[0]
        frozen = float(PRODUCT_MULTIPLIERS.get(product, np.nan))
        account_multipliers[str(product)] = effective
        rows.append(
            {
                "product": str(product),
                "selected_contract_count": int(group.symbol.nunique()),
                "selected_contracts": ";".join(sorted(group.symbol.astype(str).unique())),
                "frozen_model_multiplier": frozen,
                "effective_provider_multiplier": effective,
                "account_multiplier_used": effective,
                "differs_from_frozen_model": not bool(
                    np.isclose(effective, frozen, rtol=0.0, atol=1e-12)
                ),
            }
        )

    multiplier_by_product = {row["product"]: row["account_multiplier_used"] for row in rows}
    audited_specs["account_multiplier_used"] = audited_specs["product"].map(multiplier_by_product)
    audited_specs["account_multiplier_matches_provider"] = np.isclose(
        audited_specs.account_multiplier_used,
        audited_specs.provider_multiplier,
        rtol=0.0,
        atol=1e-12,
    )
    if not audited_specs.account_multiplier_matches_provider.all():
        raise RuntimeError("account multiplier map does not cover every selected contract")
    return account_multipliers, audited_specs, pd.DataFrame(rows)


def _fetch_official_reference_sources(output: Path) -> list[dict]:
    source_names = (
        "DCE_2026_HOLIDAY",
        "DCE_A",
        "DCE_C",
        "DCE_EG",
        "DCE_I",
        "DCE_M",
        "DCE_P",
        "DCE_PP",
        "DCE_Y",
        "CZCE_TA",
        "DCE",
        "CZCE_SESSION",
        "SHFE",
        "INE",
    )

    def fetch_one(name: str) -> dict:
        url = OFFICIAL_SOURCE_URLS[name]
        path = output / "market_raw" / "official_sources" / f"{name.lower()}"
        errors = []
        status_code = None
        content_type = ""
        body = b""
        for attempt in (1, 2):
            try:
                request = urllib.request.Request(
                    url, headers={"User-Agent": "afuture-historical-replay/1"}
                )
                with urllib.request.urlopen(request, timeout=25) as response:
                    status_code = int(response.status)
                    content_type = response.headers.get("Content-Type", "")
                    body = response.read()
                errors = []
                break
            except (OSError, urllib.error.URLError, TimeoutError) as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                if attempt == 1:
                    time.sleep(0.5)
        suffix = (
            ".pdf" if "pdf" in content_type.lower() or url.lower().endswith(".pdf") else ".html"
        )
        path = path.with_suffix(suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return {
            "name": name,
            "url": url,
            "status_code": status_code,
            "content_type": content_type,
            "result": "ok" if body and not errors else "error",
            "error": " | ".join(errors),
            "size_bytes": len(body),
            "path": path.relative_to(output).as_posix(),
            "sha256": _sha256(path),
        }

    rows = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_one, name): name for name in source_names}
        for future in as_completed(futures):
            rows.append(future.result())
            print(f"official_reference_fetch={len(rows)}/{len(source_names)}", flush=True)
    rows.sort(key=lambda item: str(item["name"]))
    _write_json(output / "official_source_fetch_log.json", rows)
    return rows


def _raw_path(root: Path, kind: str, symbol: str, period: str | None) -> Path:
    suffix = "_60m" if kind == "minute_oi" else ""
    folder = {
        "continuous": "continuous_daily",
        "specific": "specific_contract_daily",
        "minute_oi": "oi_60m",
    }[kind]
    return root / "market_raw" / folder / f"{symbol}{suffix}.csv"


def _normalize_daily(
    frame: pd.DataFrame,
    *,
    product: str,
    symbol: str,
    exchange: str | None = None,
    delivery: pd.Timestamp | None = None,
) -> pd.DataFrame:
    if "date" not in frame.columns:
        return pd.DataFrame()
    result = frame.copy()
    result["date"] = pd.to_datetime(result["date"], errors="coerce").dt.normalize()
    for field in ("open", "high", "low", "close", "volume", "hold", "settle"):
        if field not in result:
            result[field] = np.nan
        result[field] = pd.to_numeric(result[field], errors="coerce")
    result["product"] = str(product).upper()
    result["symbol"] = str(symbol).upper()
    if exchange is not None:
        result["exchange"] = str(exchange).upper()
    if delivery is not None:
        result["delivery"] = pd.Timestamp(delivery).normalize()
    result = result.dropna(subset=["date"])
    result = result.drop_duplicates(["date", "symbol"], keep="last")
    return result.sort_values(["date", "symbol"]).reset_index(drop=True)


def fetch_market_data(
    *, end: pd.Timestamp, output: Path
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list[dict]]:
    jobs: list[tuple[str, str, str | None]] = []
    jobs.extend(("continuous", f"{product}0", None) for product in broad_fetch.PRODUCTS)
    jobs.extend(
        ("specific", symbol, exchange) for _product, exchange, symbol in _specific_symbols(end)
    )
    jobs.extend(
        ("minute_oi", f"{product}{month}", None)
        for product in oi_gate.SUPPORTED_PRODUCTS
        for month in OI_SYMBOL_MONTHS
    )
    expected = len(jobs)
    continuous_frames: list[pd.DataFrame] = []
    specific_frames: list[pd.DataFrame] = []
    minute_frames: list[pd.DataFrame] = []
    query_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=MAX_FETCH_WORKERS) as executor:
        futures = {
            executor.submit(_fetch_job, kind, symbol): (kind, symbol, extra)
            for kind, symbol, extra in jobs
        }
        for number, future in enumerate(as_completed(futures), 1):
            kind, symbol, extra = futures[future]
            response = future.result()
            frame = response.pop("frame")
            response["kind"] = kind
            response["symbol"] = symbol
            response["period"] = "60" if kind == "minute_oi" else ""
            response["rows_returned"] = int(len(frame))
            response["columns_returned"] = ",".join(str(column) for column in frame.columns)
            if not frame.empty:
                first_col = "datetime" if kind == "minute_oi" else "date"
                values = pd.to_datetime(frame[first_col], errors="coerce").dropna()
                response["provider_min_timestamp"] = values.min().isoformat() if len(values) else ""
                response["provider_max_timestamp"] = values.max().isoformat() if len(values) else ""
            else:
                response["provider_min_timestamp"] = ""
                response["provider_max_timestamp"] = ""
            if response["error"]:
                response["result"] = "error"
            elif frame.empty:
                response["result"] = "empty_response"
            else:
                response["result"] = "ok"
            raw_file = _raw_path(output, kind, symbol, None)
            _csv_frame(frame, raw_file)
            response["raw_file"] = raw_file.relative_to(output).as_posix()
            response["raw_file_sha256"] = _sha256(raw_file)
            query_rows.append(
                {k: v for k, v in response.items() if k != "elapsed_seconds"}
                | {"elapsed_seconds": response["elapsed_seconds"]}
            )

            if kind == "continuous" and not frame.empty:
                product = symbol[:-1]
                normalized = _normalize_daily(frame, product=product, symbol=symbol)
                normalized = normalized[
                    (normalized.date >= OVERLAP_START) & (normalized.date <= end)
                ]
                continuous_frames.append(normalized)
            elif kind == "specific" and not frame.empty:
                product = "".join(character for character in symbol if character.isalpha()).upper()
                normalized = _normalize_daily(
                    frame,
                    product=product,
                    symbol=symbol,
                    exchange=str(extra),
                    delivery=specific_fetch.delivery_date(symbol),
                )
                normalized = normalized[
                    (normalized.date >= OVERLAP_START) & (normalized.date <= end)
                ]
                specific_frames.append(normalized)
            elif kind == "minute_oi" and not frame.empty:
                normalized = frame.copy()
                if "datetime" not in normalized:
                    normalized = normalized.rename(columns={normalized.columns[0]: "datetime"})
                normalized["datetime"] = pd.to_datetime(normalized["datetime"], errors="coerce")
                for field in OI_VALUE_FIELDS:
                    if field in normalized:
                        normalized[field] = pd.to_numeric(normalized[field], errors="coerce")
                normalized["symbol"] = symbol.upper()
                normalized["product"] = "".join(
                    character for character in symbol if character.isalpha()
                ).upper()
                normalized = normalized.dropna(subset=["datetime"])
                normalized = normalized[
                    (normalized.datetime >= OVERLAP_START)
                    & (normalized.datetime <= end + pd.Timedelta(hours=23, minutes=59))
                ]
                minute_frames.append(normalized)
            if number % 50 == 0 or number == expected:
                print(f"market_fetch={number}/{expected}", flush=True)

    queries = pd.DataFrame(query_rows).sort_values(["kind", "symbol"]).reset_index(drop=True)
    _csv_frame(queries, output / "market_query_log.csv")
    if len(queries) != expected:
        raise RuntimeError(f"market query log incomplete: {len(queries)}/{expected}")
    if (queries.loc[queries.kind == "continuous", "result"] != "ok").any():
        bad = queries.loc[(queries.kind == "continuous") & (queries.result != "ok")]
        raise RuntimeError(
            f"continuous daily provider failures: {bad[['symbol', 'result', 'error']].to_dict('records')}"
        )
    continuous = (
        pd.concat(continuous_frames, ignore_index=True) if continuous_frames else pd.DataFrame()
    )
    specific = pd.concat(specific_frames, ignore_index=True) if specific_frames else pd.DataFrame()
    minute = pd.concat(minute_frames, ignore_index=True) if minute_frames else pd.DataFrame()
    _csv_frame(
        continuous, output / "market_normalized" / "continuous_provider_overlap_and_extension.csv"
    )
    _csv_frame(
        specific, output / "market_normalized" / "specific_provider_overlap_and_extension.csv"
    )
    _csv_frame(minute, output / "market_normalized" / "oi_60m_provider_overlap_and_extension.csv")
    return continuous, specific, minute, query_rows


def _complete_observed_calendar(
    continuous_provider: pd.DataFrame,
    *,
    start: pd.Timestamp,
    end_cap: pd.Timestamp,
) -> pd.DatetimeIndex:
    frame = continuous_provider.copy()
    frame = frame[(frame.date >= start) & (frame.date <= end_cap)].copy()
    for field in REQUIRED_CONTINUOUS_FIELDS:
        if field not in frame:
            raise ValueError(f"continuous daily feed missing {field}")
    complete = frame.dropna(subset=list(REQUIRED_CONTINUOUS_FIELDS))
    complete = complete[
        (complete.open > 0.0)
        & (complete.high > 0.0)
        & (complete.low > 0.0)
        & (complete.close > 0.0)
    ]
    expected = pd.bdate_range(start, end_cap)
    counts = complete.groupby("date")["product"].nunique()
    observed = (
        pd.DatetimeIndex(counts[counts == len(broad_fetch.PRODUCTS)].index)
        .normalize()
        .sort_values()
    )
    missing = expected.difference(observed)
    extra = observed.difference(expected)
    if len(missing) or len(extra):
        raise RuntimeError(
            f"continuous common calendar mismatch: missing={[d.strftime('%F') for d in missing]}, "
            f"extra={[d.strftime('%F') for d in extra]}"
        )
    if observed.empty or observed[0] != start:
        raise RuntimeError(f"continuous data do not form a complete interval from {start.date()}")
    return observed


def _complete_daily_calendar(
    continuous_provider: pd.DataFrame, end_cap: pd.Timestamp
) -> pd.DatetimeIndex:
    return _complete_observed_calendar(
        continuous_provider,
        start=START,
        end_cap=end_cap,
    )


def _overlap_differences(
    frozen: pd.DataFrame,
    fetched: pd.DataFrame,
    *,
    time_column: str,
    fields: tuple[str, ...],
) -> pd.DataFrame:
    keys = [time_column, "product", "symbol"]
    left = frozen.copy()
    right = fetched.copy()
    left[time_column] = pd.to_datetime(left[time_column], errors="coerce")
    right[time_column] = pd.to_datetime(right[time_column], errors="coerce")
    left = left[(left[time_column] >= OVERLAP_START) & (left[time_column] <= FROZEN_CUTOFF)]
    right = right[(right[time_column] >= OVERLAP_START) & (right[time_column] <= FROZEN_CUTOFF)]
    for field in fields:
        if field not in left:
            left[field] = np.nan
        if field not in right:
            right[field] = np.nan
    if left.empty or right.empty:
        return pd.DataFrame(
            columns=keys
            + [f"{name}_frozen" for name in fields]
            + [f"{name}_provider" for name in fields]
        )
    common = left.merge(right, on=keys, how="inner", suffixes=("_frozen", "_provider"))
    changed = pd.Series(False, index=common.index)
    for field in fields:
        old = pd.to_numeric(common.get(f"{field}_frozen"), errors="coerce")
        new = pd.to_numeric(common.get(f"{field}_provider"), errors="coerce")
        changed |= ~np.isclose(old, new, rtol=0.0, atol=0.0, equal_nan=True)
    columns = keys + [f"{field}_{side}" for field in fields for side in ("frozen", "provider")]
    return common.loc[changed, columns].sort_values(keys).reset_index(drop=True)


def _build_historical_candidate(
    *,
    base_weights: pd.DataFrame,
    bars_60m: pd.DataFrame,
    continuous: pd.DataFrame,
):
    flow = oi_gate.build_daily_price_oi_flow(bars_60m)
    lagged = oi_gate.lag_flow_to_target_days(
        flow,
        target_days=base_weights.index,
        products=STRESS90_POLICY.oi_products,
    )
    close = stress80.continuous_close_panel(continuous, list(base_weights.columns))
    path = build_stress90_candidate_path(
        base_weights=base_weights,
        completed_close_prices=close,
        confirming_flow=lagged.reindex(columns=STRESS90_POLICY.oi_products),
    )
    digest = candidate_weight_digest(path.survivor_weights)
    if digest != EXPECTED_CANDIDATE_WEIGHT_SHA256:
        raise RuntimeError(
            f"frozen historical Stress-90 candidate mismatch: {digest} != {EXPECTED_CANDIDATE_WEIGHT_SHA256}"
        )
    return (
        path,
        close,
        {
            "candidate_weight_sha256": digest,
            "last_daily_decision_digest": path.decisions[-1].daily_decision_digest,
            "final_state_sha256": candidate_state_digest(path.final_state),
            "last_target_day": pd.Timestamp(path.survivor_weights.index[-1]).date().isoformat(),
        },
    )


def _oi_source_days_for_targets(
    target_days: pd.DatetimeIndex | pd.Series | list,
    observed_days: pd.DatetimeIndex | pd.Series | list,
) -> pd.DatetimeIndex:
    targets = pd.DatetimeIndex(pd.to_datetime(list(target_days), errors="coerce")).normalize()
    observed = pd.DatetimeIndex(pd.to_datetime(list(observed_days), errors="coerce")).normalize()
    observed = observed[~observed.isna()].unique().sort_values()
    if targets.empty or not observed[observed < targets[0]].size:
        raise ValueError(
            "OI source-day mapping requires an observed session before the first target"
        )
    prior = observed[observed < targets[0]][-1]
    return pd.DatetimeIndex([prior, *targets[:-1]]).unique().sort_values()


def _fixed_oi_flow_tail(fixed_bars: pd.DataFrame) -> pd.DataFrame:
    fixed_tail = fixed_bars.copy()
    fixed_tail["datetime"] = pd.to_datetime(fixed_tail["datetime"], errors="coerce")
    return fixed_tail[
        (fixed_tail["datetime"] >= OVERLAP_START)
        & (fixed_tail["datetime"] <= FROZEN_CUTOFF)
        & fixed_tail["product"].isin(oi_gate.SUPPORTED_PRODUCTS)
    ]


def _session_coverage(mapped_bars: pd.DataFrame, required_days: pd.DatetimeIndex) -> pd.DataFrame:
    use = mapped_bars[mapped_bars.trading_day.isin(required_days)].copy()
    use["clock_label"] = use.datetime.dt.strftime("%H:%M")
    rows = []
    for day in required_days:
        for product in oi_gate.SUPPORTED_PRODUCTS:
            subset = use[(use.trading_day == day) & (use["product"] == product)]
            labels = set(subset.clock_label)
            rows.append(
                {
                    "trading_day": day,
                    "product": product,
                    "rows": int(len(subset)),
                    "symbols": ";".join(sorted(subset.symbol.unique())),
                    "clock_labels": ";".join(sorted(labels)),
                    "missing_expected_labels": ";".join(
                        label
                        for label in (*NIGHT_SESSION_LABELS, *DAY_SESSION_LABELS)
                        if label not in labels
                    ),
                    "has_night": all(label in labels for label in NIGHT_SESSION_LABELS),
                    "has_day": all(label in labels for label in DAY_SESSION_LABELS),
                }
            )
    return pd.DataFrame(rows)


def _oi_contract_details(
    mapped_bars: pd.DataFrame, required_days: pd.DatetimeIndex
) -> pd.DataFrame:
    use = mapped_bars[mapped_bars.trading_day.isin(required_days)].copy()
    use = use.sort_values(["trading_day", "product", "symbol", "datetime"])
    rows = []
    for (day, product, symbol), group in use.groupby(
        ["trading_day", "product", "symbol"], sort=True
    ):
        rows.append(
            {
                "trading_day": day,
                "product": product,
                "symbol": symbol,
                "bars": int(len(group)),
                "first_datetime": group.datetime.iloc[0],
                "last_datetime": group.datetime.iloc[-1],
                "first_open": float(group.open.iloc[0]),
                "last_close": float(group.close.iloc[-1]),
                "first_hold": float(group.hold.iloc[0]),
                "last_hold": float(group.hold.iloc[-1]),
                "total_volume": float(group.volume.sum()),
                "flow_if_selected": float(np.sign(group.close.iloc[-1] / group.open.iloc[0] - 1.0))
                if float(group.hold.iloc[-1]) > float(group.hold.iloc[0])
                else 0.0,
                "clock_labels": ";".join(sorted(set(group.datetime.dt.strftime("%H:%M")))),
            }
        )
    return pd.DataFrame(rows)


def _candidate_layer_table(path, output: Path) -> None:
    for name, frame in (
        ("base_weights", path.base_weights),
        ("oi_confirmed_weights", path.oi_confirmed_weights),
        ("cost_approved_weights", path.cost_approved_weights),
        ("survivor_weights", path.survivor_weights),
    ):
        _csv_frame(frame.rename_axis("date").reset_index(), output / "strategy" / f"{name}.csv")
    decisions = output / "strategy" / "daily_decisions.jsonl"
    decisions.parent.mkdir(parents=True, exist_ok=True)
    with decisions.open("w", encoding="utf-8", newline="\n") as stream:
        for decision in path.decisions:
            stream.write(
                json.dumps(decision, ensure_ascii=False, sort_keys=True, default=_json_default)
                + "\n"
            )
    details = pd.DataFrame(
        {
            "date": path.survivor_weights.index,
            "current_hhi": path.current_hhi.to_numpy(),
            "prior_hhi_median": path.prior_hhi_median.to_numpy(),
            "concentration_freeze": path.concentration_freeze.to_numpy(bool),
            "decision_digest": [item.daily_decision_digest for item in path.decisions],
            "post_state_digest": [
                candidate_state_digest(item.post_state) for item in path.decisions
            ],
        }
    )
    _csv_frame(details, output / "strategy" / "daily_decision_index.csv")


def _preperiod_terminal_state(path):
    """Return the state consumed by the first replay decision, with digest proof."""
    if not path.decisions:
        raise RuntimeError("new-period candidate path has no first target-day decision")
    first = path.decisions[0]
    state = first.prior_state
    if first.input_digests.get("prior_state") != candidate_state_digest(state):
        raise RuntimeError("first target-day prior-state digest does not match its state")
    return state


def _verify_strategy_restart(
    *,
    base_weights: pd.DataFrame,
    close: pd.DataFrame,
    lagged_flow: pd.DataFrame,
    initial_state,
) -> dict:
    split = max(1, len(base_weights) // 2)
    prefix_base = base_weights.iloc[:split]
    suffix_base = base_weights.iloc[split:]
    prefix = build_stress90_candidate_path(
        base_weights=prefix_base,
        completed_close_prices=close,
        confirming_flow=lagged_flow.reindex(index=prefix_base.index),
        initial_state=initial_state,
    )
    resumed = build_stress90_candidate_path(
        base_weights=suffix_base,
        completed_close_prices=close,
        confirming_flow=lagged_flow.reindex(index=suffix_base.index),
        initial_state=prefix.final_state,
    )
    continuous = build_stress90_candidate_path(
        base_weights=base_weights,
        completed_close_prices=close,
        confirming_flow=lagged_flow.reindex(index=base_weights.index),
        initial_state=initial_state,
    )
    for name in (
        "base_weights",
        "oi_confirmed_weights",
        "cost_approved_weights",
        "survivor_weights",
    ):
        expected = getattr(continuous, name).iloc[split:]
        actual = getattr(resumed, name)
        pd.testing.assert_frame_equal(actual, expected)
    expected_decisions = [item.daily_decision_digest for item in continuous.decisions[split:]]
    actual_decisions = [item.daily_decision_digest for item in resumed.decisions]
    if expected_decisions != actual_decisions:
        raise AssertionError("Stress-90 resumed strategy decisions differ from continuous path")
    if candidate_state_digest(resumed.final_state) != candidate_state_digest(
        continuous.final_state
    ):
        raise AssertionError("Stress-90 resumed final state differs from continuous path")
    return {
        "passed": True,
        "checkpoint_day": pd.Timestamp(prefix_base.index[-1]).date().isoformat(),
        "prefix_sessions": len(prefix_base),
        "suffix_sessions": len(suffix_base),
        "suffix_decision_digest_match": True,
        "final_state_digest": candidate_state_digest(resumed.final_state),
    }


def _account_config(margin_proxy: float) -> ProductionMechanicsConfig:
    return ProductionMechanicsConfig(
        initial_capital=mechanics.INITIAL_CAPITAL,
        margin_rate_proxy=margin_proxy,
    )


def _write_account_ledger(result, scenario: str, output: Path) -> tuple[Path, Path]:
    daily = result.daily.copy()
    events = result.events.copy()
    if "kind" not in events:
        events = pd.DataFrame(
            columns=(
                "date",
                "kind",
                "action",
                "product",
                "symbol",
                "delta_lots",
                "price",
                "transaction_cost",
                "gross_pnl",
            )
        )
    gross_by_day = (
        events.loc[events.kind == "pnl"].groupby("date").gross_pnl.sum()
        if not events.empty
        else pd.Series(dtype=float)
    )
    cost_by_day = (
        events.loc[events.kind == "trade"].groupby("date").transaction_cost.sum()
        if not events.empty
        else pd.Series(dtype=float)
    )
    ledger = daily.copy()
    ledger["gross_pnl_events"] = gross_by_day.reindex(ledger.index, fill_value=0.0)
    ledger["transaction_cost_events"] = cost_by_day.reindex(ledger.index, fill_value=0.0)
    ledger["equity_reconstructed"] = (
        mechanics.INITIAL_CAPITAL
        + (ledger["gross_pnl_events"] - ledger["transaction_cost_events"]).cumsum()
    )
    ledger["equity_reconciliation_delta"] = ledger["equity"] - ledger["equity_reconstructed"]
    if not np.allclose(
        ledger.equity.to_numpy(float),
        ledger.equity_reconstructed.to_numpy(float),
        rtol=0.0,
        atol=1e-6,
    ):
        raise AssertionError(f"{scenario} daily equity does not reconcile to PnL and costs")
    _csv_frame(
        ledger.rename_axis("date").reset_index(),
        output / "account" / scenario.lower() / "cash_equity_reconciliation.csv",
    )

    risk_columns = [
        column
        for column in (
            "risk_reason",
            "margin_reject",
            "daily_circuit",
            "gross_guard",
            "halted",
            "unavailable_contract_notional",
        )
        if column in ledger
    ]
    risk_mask = pd.Series(False, index=ledger.index)
    if "risk_reason" in ledger:
        risk_mask |= ledger.risk_reason.astype(str).ne("")
    if "margin_reject" in ledger:
        risk_mask |= ledger.margin_reject.astype(str).ne("")
    for flag in ("daily_circuit", "gross_guard", "halted"):
        if flag in ledger:
            risk_mask |= ledger[flag].astype(bool)
    if "unavailable_contract_notional" in ledger:
        risk_mask |= ledger.unavailable_contract_notional.astype(float).gt(0.0)
    _csv_frame(
        ledger.loc[risk_mask, risk_columns].rename_axis("date").reset_index(),
        output / "account" / scenario.lower() / "risk_events.csv",
    )

    state: dict[str, int] = {}
    marks: dict[str, float] = {}
    position_rows = []
    no_trade_rows = []
    for day in ledger.index:
        day_events = events[events.date == day] if not events.empty else events
        for event in day_events.itertuples(index=False):
            if event.kind == "trade":
                after = int(event.lots_after)
                if after:
                    state[str(event.symbol)] = after
                else:
                    state.pop(str(event.symbol), None)
            elif event.kind == "pnl":
                marks[str(event.symbol)] = float(event.price)
        for symbol, lots in sorted(state.items()):
            product = "".join(character for character in symbol if character.isalpha()).upper()
            position_rows.append(
                {
                    "date": day,
                    "product": product,
                    "symbol": symbol,
                    "lots": lots,
                    "mark_price": marks.get(symbol, np.nan),
                    "mark_source": "latest simulated daily bar event"
                    if symbol in marks
                    else "missing_mark",
                }
            )
        row = ledger.loc[day]
        if float(row.get("turnover_notional", 0.0)) == 0.0:
            reasons = []
            if bool(row.get("halted", False)):
                reasons.append("account_hard_halt")
            if bool(row.get("daily_circuit", False)):
                reasons.append("daily_loss_circuit")
            if str(row.get("margin_reject", "")):
                reasons.append("margin_rejected_open")
            if float(row.get("unavailable_contract_notional", 0.0)) > 0.0:
                reasons.append("specific_contract_unavailable")
            if float(row.get("raw_target_gross_ratio", 0.0)) <= 1e-15:
                reasons.append("flat_strategy_target")
            if not reasons:
                reasons.append("unchanged_lots_or_integer_lot_floor")
            no_trade_rows.append({"date": day, "reason_codes": ";".join(reasons)})

    positions_path = output / "account" / scenario.lower() / "position_ledger.csv"
    no_trade_path = output / "account" / scenario.lower() / "no_trade_reasons.csv"
    _csv_frame(pd.DataFrame(position_rows), positions_path)
    _csv_frame(pd.DataFrame(no_trade_rows), no_trade_path)
    return positions_path, no_trade_path


def _simulate_account(
    *,
    scenario: str,
    specific: pd.DataFrame,
    weights: pd.DataFrame,
    preperiod_weights: pd.DataFrame,
    output: Path,
) -> tuple[dict, object]:
    if scenario == "Base":
        cost = mechanics.BASE_COST_BPS
        margin = mechanics.BASE_MARGIN_PROXY
    elif scenario == "Stress":
        cost = mechanics.STRESS_COST_BPS
        margin = mechanics.STRESS_MARGIN_PROXY
    else:
        raise ValueError(f"unsupported scenario {scenario}")
    initial_concentrations = stress90_eval.completed_concentrations_before(
        pd.concat([preperiod_weights, weights]).sort_index(),
        start=pd.Timestamp(weights.index[0]),
    )
    config = _account_config(margin)
    sim = ExpandingMedianConcentrationFreezeDirectionalProductionAcceptance(
        config,
        completed_concentrations=initial_concentrations,
    )
    prepared = sim.prepare_contracts(specific)
    result = sim.simulate(specific, weights, cost_bps=cost, prepared=prepared)
    if result.final_checkpoint is None:
        raise RuntimeError(f"{scenario} account simulation produced no checkpoint")
    daily = result.daily.copy()
    events = result.events.copy()
    daily_path = output / "account" / scenario.lower() / "daily_ledger.csv"
    events_path = output / "account" / scenario.lower() / "events.csv"
    _csv_frame(daily.rename_axis("date").reset_index(), daily_path)
    _write_account_ledger(result, scenario.lower(), output)
    events.insert(0, "event_id", [f"{scenario.upper()}-{i:07d}" for i in range(len(events))])
    if events.event_id.duplicated().any():
        raise AssertionError("simulated event identifiers are not unique")
    _csv_frame(events, events_path)
    _write_json(
        output / "account" / scenario.lower() / "final_checkpoint.json",
        result.final_checkpoint.to_dict(),
    )
    econ = stress80.economics(result.daily, result.events, cost_bps=cost)
    net_profit = float(result.final_equity) - float(mechanics.INITIAL_CAPITAL)
    if abs(net_profit - float(econ["net_alpha"])) > max(1e-6, abs(net_profit) * 1e-10):
        raise AssertionError(f"{scenario} cash-flow ledger does not reconcile")
    margin_ratio = daily["margin"].div(daily["equity"].replace(0.0, np.nan))
    gross_ratio = daily["gross_notional"].div(daily["equity"].replace(0.0, np.nan))
    summary = {
        "scenario": scenario,
        "cost_bps": cost,
        "margin_rate_proxy": margin,
        "starting_capital": mechanics.INITIAL_CAPITAL,
        "ending_equity": float(result.final_equity),
        "net_profit": net_profit,
        "net_return": net_profit / mechanics.INITIAL_CAPITAL,
        "gross_signal_pnl": float(econ["gross_signal_pnl"]),
        "turnover_notional": float(econ["turnover_notional"]),
        "transaction_cost": float(econ["transaction_cost"]),
        "net_alpha_reconciled": float(econ["net_alpha"]),
        "max_drawdown": float(mechanics._result_stats(result)["max_drawdown"]),
        "max_margin_ratio_proxy": float(margin_ratio.max()) if margin_ratio.notna().any() else 0.0,
        "max_gross_notional_ratio": float(gross_ratio.max()) if gross_ratio.notna().any() else 0.0,
        "max_margin_notional_proxy": float(daily["margin"].max()) if not daily.empty else 0.0,
        "max_gross_notional": float(daily["gross_notional"].max()) if not daily.empty else 0.0,
        "trading_days": int(len(daily)),
        "trade_events": int((result.events.kind.astype(str) == "trade").sum())
        if "kind" in result.events
        else 0,
        "pnl_events": int((result.events.kind.astype(str) == "pnl").sum())
        if "kind" in result.events
        else 0,
        "risk_circuit_days": int(daily["daily_circuit"].astype(bool).sum())
        if "daily_circuit" in daily
        else 0,
        "margin_reject_days": int((daily.margin_reject.astype(str) != "").sum())
        if "margin_reject" in daily
        else 0,
        "halted": bool(daily.halted.astype(bool).any()) if "halted" in daily else False,
        "first_divergence": result.first_divergence,
        "statistics": mechanics._result_stats(result),
        "economics": econ,
        "events_sha256": _sha256(events_path),
        "daily_ledger_sha256": _sha256(daily_path),
    }

    split = max(1, len(weights) // 2)
    prefix_sim = ExpandingMedianConcentrationFreezeDirectionalProductionAcceptance(
        config,
        completed_concentrations=initial_concentrations,
    )
    prefix = prefix_sim.simulate(
        specific,
        weights.iloc[:split],
        cost_bps=cost,
        prepared=prepared,
    )
    if prefix.final_checkpoint is None:
        raise RuntimeError(f"{scenario} checkpoint prefix is empty")
    restored_checkpoint = DirectionalSimulationCheckpoint.from_dict(
        json.loads(json.dumps(prefix.final_checkpoint.to_dict()))
    )
    resumed_sim = ExpandingMedianConcentrationFreezeDirectionalProductionAcceptance(
        config,
        completed_concentrations=initial_concentrations,
    )
    resumed = resumed_sim.simulate(
        specific,
        weights.iloc[split:],
        cost_bps=cost,
        prepared=prepared,
        checkpoint=restored_checkpoint,
    )
    daily_joined = pd.concat([prefix.daily, resumed.daily])
    events_joined = pd.concat([prefix.events, resumed.events], ignore_index=True)
    pd.testing.assert_frame_equal(daily_joined, result.daily)
    pd.testing.assert_frame_equal(events_joined, result.events)
    if (
        resumed.final_equity != result.final_equity
        or resumed.final_checkpoint != result.final_checkpoint
    ):
        raise AssertionError(f"{scenario} account checkpoint resume diverged")
    _write_json(
        output / "account" / scenario.lower() / "recovery_checkpoint.json",
        prefix.final_checkpoint.to_dict(),
    )
    summary["checkpoint_recovery"] = {
        "passed": True,
        "checkpoint_day": pd.Timestamp(weights.index[split - 1]).date().isoformat(),
        "prefix_days": split,
        "resumed_days": len(weights) - split,
        "daily_equity_and_risk_ledger_match": True,
        "simulated_event_rows_match": True,
        "event_ids_unique": True,
        "final_state_match": True,
        "checkpoint_sha256": _sha256(
            output / "account" / scenario.lower() / "recovery_checkpoint.json"
        ),
    }
    return summary, result


def _contract_selection_audit(
    specific: pd.DataFrame, target_days: pd.DatetimeIndex
) -> pd.DataFrame:
    sim = ExpandingMedianConcentrationFreezeDirectionalProductionAcceptance(
        _account_config(mechanics.BASE_MARGIN_PROXY)
    )
    normalized = sim._normalize_contracts(specific)
    rows = []
    for day in target_days:
        selected = sim.select_contracts_for_day(normalized, pd.Timestamp(day))
        prior = normalized.loc[normalized.date < pd.Timestamp(day), "date"]
        activity_day = pd.Timestamp(prior.max()).normalize() if not prior.empty else pd.NaT
        available = set(normalized.loc[normalized.date == pd.Timestamp(day), "symbol"].astype(str))
        for product, symbol in sorted(selected.items()):
            rows.append(
                {
                    "target_trading_day": day,
                    "activity_source_day": activity_day,
                    "product": product,
                    "symbol": symbol,
                    "selected_from_prior_activity": True,
                    "target_day_contract_bar_available": symbol in available,
                }
            )
    return pd.DataFrame(rows)


def _specific_field_coverage(specific: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for product, group in specific.groupby("product", sort=True):
        for field in ("open", "high", "low", "close", "volume", "hold", "settle"):
            present = (
                pd.to_numeric(group[field], errors="coerce").notna()
                if field in group
                else pd.Series(False, index=group.index)
            )
            rows.append(
                {
                    "product": product,
                    "field": field,
                    "rows": int(len(group)),
                    "rows_present": int(present.sum()),
                    "rows_missing": int((~present).sum()),
                    "coverage_ratio": float(present.mean()) if len(group) else np.nan,
                    "first_date": group.date.min(),
                    "last_date": group.date.max(),
                    "symbols": int(group.symbol.nunique()),
                }
            )
    return pd.DataFrame(rows)


def _market_manifest(query_rows: list[dict]) -> dict:
    continuous = [item for item in query_rows if item["kind"] == "continuous"]
    specific = [item for item in query_rows if item["kind"] == "specific"]
    minute = [item for item in query_rows if item["kind"] == "minute_oi"]
    return {
        "provider": "AKShare/Sina futures endpoints",
        "akshare_version": __import__("akshare").__version__,
        "daily_endpoint": "ak.futures_zh_daily_sina(symbol=...) (unbounded history return; suffix filtered locally)",
        "minute_endpoint": "ak.futures_zh_minute_sina(symbol=..., period='60') (unbounded vendor response; interval retained locally)",
        "contract_spec_endpoint": "ak.futures_contract_detail(symbol=...) for every account-selected contract",
        "provider_response_archive": "AKShare-returned parsed DataFrame serialized before normalization; this is not a raw HTTP wire capture",
        "continuous_queries": len(continuous),
        "specific_contract_queries": len(specific),
        "oi_60m_contract_queries": len(minute),
        "empty_response_count": sum(item["result"] == "empty_response" for item in query_rows),
        "error_count": sum(item["result"] == "error" for item in query_rows),
        "query_log": "market_query_log.csv",
        "raw_directories": [
            "market_raw/continuous_daily/",
            "market_raw/specific_contract_daily/",
            "market_raw/oi_60m/",
            "market_raw/contract_specs/",
            "market_raw/official_sources/",
        ],
    }


def _chinese_report(report: dict) -> str:
    base = report["scenarios"]["Base"]
    stress = report["scenarios"]["Stress"]
    coverage = report["coverage"]
    multiplier_evidence = report["contract_spec_evidence"]
    multiplier_overrides = multiplier_evidence["model_multiplier_discrepancies"]
    multiplier_note = ""
    if multiplier_overrides:
        details = "；".join(
            f"{item['product']}：冻结研究映射 {item['frozen_model_multiplier']:g}，"
            f"本区间有效具体合约 {item['selected_contracts']} 为 "
            f"{item['effective_provider_multiplier']:g} 吨/手"
            for item in multiplier_overrides
        )
        multiplier_note = f"""
## 具体合约规格差异

- 所选的 {multiplier_evidence["selected_contracts"]} 个具体合约均取得非空规格，账户计算使用当前规格。{details}。账户手数折算、名义敞口、盈亏、成本及保证金估算均按该具体合约有效乘数计算。
- 该差异来自合约规格证据，不改变模板、策略权重、候选池或风险阈值；模型原始乘数与采用乘数并列保存在 `coverage/selected_contract_specs.csv` 和 `coverage/account_multiplier_audit.csv`。
- TA 规格以郑商所《精对苯二甲酸（PTA）期货业务细则》（2024-02-06 起施行）第 3 条为官方依据，全文原件见 `market_raw/official_sources/czce_ta.pdf`，该规则载明交易单位为 5 吨/手。其官方入口：{OFFICIAL_SOURCE_URLS["CZCE_TA"]}。
"""
    return f"""# Stress-90 新增区间连续账户历史回放

## 范围和证据等级

- run_id：`{report["run_id"]}`
- 区间：`{coverage["actual_start"]}` 至 `{coverage["actual_end"]}`，共 `{coverage["target_trading_days"]}` 个完整交易日；共同截止 `{coverage["common_complete_cutoff"]}`。
- 证据：离线历史账户代理，`prospective_evidence=false`、`live_authorized=false`、`risk_increase_authorized=false`。这不是同期真实账户交易，也不是一个月实盘或 Shadow 证明。
- 使用代码提交 `{report["source"]["code_commit_sha"]}`，固定策略基线 `main={report["source"]["base_main_sha"]}`。旧五份输入使用前后 SHA/尺寸校验通过且未修改。
- 行情源：AKShare `{report["market"]["akshare_version"]}` / Sina；未限日期的接口原始解析表逐合约保存，运行只使用其 `{coverage["actual_start"]}` 之后新增部分，交接处另存冲突对比。

## 结果

| 情景 | 成本 | 保证金代理 | 期末权益 | 区间净收益 | 净收益率 | 最大回撤 | 成交事件 | 换手名义额 | 交易成本 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Base | {base["cost_bps"]} bp | {base["margin_rate_proxy"]:.0%} | ¥{base["ending_equity"]:,.2f} | ¥{base["net_profit"]:,.2f} | {base["net_return"]:.3%} | {base["max_drawdown"]:.3%} | {base["trade_events"]} | ¥{base["turnover_notional"]:,.2f} | ¥{base["transaction_cost"]:,.2f} |
| Stress | {stress["cost_bps"]} bp | {stress["margin_rate_proxy"]:.0%} | ¥{stress["ending_equity"]:,.2f} | ¥{stress["net_profit"]:,.2f} | {stress["net_return"]:.3%} | {stress["max_drawdown"]:.3%} | {stress["trade_events"]} | ¥{stress["turnover_notional"]:,.2f} | ¥{stress["transaction_cost"]:,.2f} |

初始权益均为 ¥500,000、期初空仓、无入金。成本分别为 5/15 bp；保证金率分别为 12%/15%代理。策略目标、整数手数、合约选择、减仓优先、风险门和保证金代理缓冲沿用冻结代码，没有搜索或修改。
{multiplier_note}

## 数据时序与边界

- OI 的 9 个支持品种保留原集合。21:00–23:00 夜盘记录映射到下一实际开市交易日，日盘保留自然日期；D 日完整的 Price×OI flow 最早用于下一个交易日。8 月 20 日夜盘属于 8 月 21 日交易日，因此不用于 8 月 21 日目标；该日目标只使用 8 月 20 日已完成交易日信号。
- 9 月 23 日在本次数据截止锁定时尚未收盘，未计入；9 月 22 日是全部 50 个连续根品种共同取得完整 OHLC/成交量/持仓量的最后日期。大商所 2026 年中秋休市自 9 月 25 日开始，晚于本轮截止（https://www.dce.com.cn/dce/content/2025/ywggytz/18625615.html）。
- 账户以具体合约日线选择并成交，按固定代码的“合约月份 15 日 + 20 日交割缓冲”代理选约；结算价与最小变动价位仅作为原始市场证据，模拟盈亏使用日线开收盘。不能据此宣称历史柜台保证金、真实盘口滑点、排队、部分成交或盘中回撤。
- 提供商未返回的值保持缺失并写入覆盖/查询日志，不填零。详细逐品种/合约日线、60m、会话映射、权重层、策略逐日决策、订单/成交/风险/资金账本及检查点均随私库原件保存。

## 验证与限制

- 新旧 fixed candidate 交接摘要与预存 SHA 一致；新增 base 权重在 8 月 20 日前逐项与冻结权重比较。
- Stress-90 策略中段 checkpoint 的恢复后缀逐日决策/状态与不中断路径一致；Base 和 Stress 账户从同一中段 checkpoint 恢复后，逐日权益、风险行、全部模拟事件和最终持仓状态逐项一致。
- 短区间 CAGR/Sharpe 不适用旧两年收益门槛。没有真实 CTP 订单、成交、结算或一个自然月现场运行记录。
- PR #55 仍是独立的 Draft/未合并工程路径；结算桥接、每日技术许可和自动化现场编排仍未完成，本报告不把它们标为通过。

完整文件清单及哈希见 `run_manifest.json`、`SHA256SUMS`；私库远端回读收据随后记录分支、commit、tree、blob 和 SHA-256 对照。
"""


def run_replay(args: argparse.Namespace) -> dict:
    output = Path(args.output_dir).resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"run output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    execution_time = datetime.now(SHANGHAI).isoformat()
    end_cap = pd.Timestamp(args.end_cap).normalize()
    if end_cap < START:
        raise ValueError("end cap precedes requested start")

    fixed_specific, fixed_continuous, base_weights_frozen, fixed_bars, fixed_input_manifest = (
        stress80._load_inputs(Path(args.input_dir).resolve())
    )
    fixed_manifest = stress80.validate_fixed_input_manifest(fixed_input_manifest)
    continuous_vendor, specific_vendor, oi_vendor, query_rows = fetch_market_data(
        end=end_cap, output=output
    )
    official_reference_rows = _fetch_official_reference_sources(output)
    observed_calendar = _complete_observed_calendar(
        continuous_vendor,
        start=OVERLAP_START,
        end_cap=end_cap,
    )
    calendar = _complete_daily_calendar(continuous_vendor, end_cap)
    actual_end = pd.Timestamp(calendar[-1]).normalize()
    if actual_end != end_cap:
        raise RuntimeError(
            f"common complete cutoff {actual_end.date()} does not match locked cap {end_cap.date()}"
        )

    overlap_continuous = _overlap_differences(
        fixed_continuous,
        continuous_vendor,
        time_column="date",
        fields=REQUIRED_CONTINUOUS_FIELDS,
    )
    overlap_specific = _overlap_differences(
        fixed_specific,
        specific_vendor,
        time_column="date",
        fields=("open", "high", "low", "close", "volume", "hold", "settle"),
    )
    overlap_oi = _overlap_differences(
        fixed_bars,
        oi_vendor,
        time_column="datetime",
        fields=OI_VALUE_FIELDS,
    )
    _csv_frame(overlap_continuous, output / "overlap" / "continuous_daily_conflicts.csv")
    _csv_frame(overlap_specific, output / "overlap" / "specific_contract_daily_conflicts.csv")
    _csv_frame(overlap_oi, output / "overlap" / "oi_60m_conflicts.csv")

    calendar_frame = pd.DataFrame(
        {
            "trading_day": observed_calendar,
            "observed_continuous_products": len(broad_fetch.PRODUCTS),
            "calendar_basis": "intersection of 50 observed complete continuous daily roots",
            "session_role": [
                "oi_warmup_only" if day < START else "replay_target" for day in observed_calendar
            ],
        }
    )
    _csv_frame(calendar_frame, output / "exchange_calendar.csv")
    if len(calendar) != len(pd.bdate_range(START, end_cap)):
        raise AssertionError("calendar verification did not cover every expected weekday session")

    continuous_extension = continuous_vendor[
        (continuous_vendor.date >= START) & (continuous_vendor.date <= actual_end)
    ].copy()
    specific_extension_all = specific_vendor[
        (specific_vendor.date >= START) & (specific_vendor.date <= actual_end)
    ].copy()
    missing_specific = specific_extension_all[list(REQUIRED_SPECIFIC_FIELDS)].isna().any(axis=1)
    invalid_values = (
        (specific_extension_all.open <= 0.0)
        | (specific_extension_all.close <= 0.0)
        | (specific_extension_all.volume < 0.0)
        | (specific_extension_all.hold < 0.0)
    )
    invalid_specific = specific_extension_all[missing_specific | invalid_values].copy()
    invalid_specific["validation_reason"] = np.where(
        missing_specific.loc[invalid_specific.index],
        "missing_required_value",
        "invalid_required_value",
    )
    _csv_frame(
        invalid_specific, output / "market_normalized" / "specific_invalid_required_rows.csv"
    )
    specific_field_coverage = _specific_field_coverage(specific_extension_all)
    _csv_frame(
        specific_field_coverage, output / "coverage" / "specific_contract_field_coverage.csv"
    )
    specific_extension = specific_extension_all.dropna(subset=list(REQUIRED_SPECIFIC_FIELDS))
    specific_extension = specific_extension[
        (specific_extension.open > 0.0)
        & (specific_extension.close > 0.0)
        & (specific_extension.volume >= 0.0)
        & (specific_extension.hold >= 0.0)
    ].copy()
    _csv_frame(continuous_extension, output / "market_normalized" / "continuous_extension.csv")
    _csv_frame(specific_extension, output / "market_normalized" / "specific_contract_extension.csv")

    continuous_old = fixed_continuous.copy()
    continuous_old["date"] = pd.to_datetime(continuous_old.date, errors="coerce").dt.normalize()
    continuous_all = pd.concat(
        [continuous_old[continuous_old.date <= FROZEN_CUTOFF.normalize()], continuous_extension],
        ignore_index=True,
    )
    continuous_all = continuous_all.drop_duplicates(["date", "product"], keep="last").sort_values(
        ["date", "product"]
    )
    generated_weights = execution_target.generate_execution_signal_weights(continuous_all)
    policy_start = pd.Timestamp(mechanics.WINDOWS["train"][0])
    base_weight_prefix_audit, policy_weight_mismatches, pre_policy_weight_mismatches = (
        _base_weight_prefix_audit(
            generated_weights,
            base_weights_frozen,
            policy_start=policy_start,
            cutoff=FROZEN_CUTOFF,
        )
    )
    _csv_frame(
        policy_weight_mismatches,
        output / "strategy" / "base_weight_prefix_mismatches.csv",
    )
    _csv_frame(
        pre_policy_weight_mismatches,
        output / "strategy" / "pre_policy_weight_history_differences.csv",
    )
    _write_json(
        output / "strategy" / "base_weight_prefix_audit.json",
        {
            **base_weight_prefix_audit,
            "policy_start_source": "tools/evaluate_directional_production_mechanics.py:WINDOWS['train'][0]",
            "policy_mismatch_file": "strategy/base_weight_prefix_mismatches.csv",
            "pre_policy_mismatch_file": "strategy/pre_policy_weight_history_differences.csv",
            "pre_policy_mismatch_samples": pre_policy_weight_mismatches.head(20).to_dict(
                orient="records"
            ),
        },
    )
    if not base_weight_prefix_audit["passed"]:
        details = policy_weight_mismatches.head(20).to_dict(orient="records")
        raise RuntimeError(
            f"current frozen-policy base weights differ from the archived prefix: {details[:5]}"
        )
    base_weights_new = generated_weights.reindex(
        index=calendar, columns=base_weights_frozen.columns
    )
    if (
        base_weights_new.isna().any(axis=None)
        or not np.isfinite(base_weights_new.to_numpy(float)).all()
    ):
        raise RuntimeError("new target base weights contain missing or non-finite values")
    if bool((base_weights_new.abs().sum(axis=1) > stress80.MAX_GROSS + 1e-10).any()):
        raise RuntimeError("new target base weights exceed frozen gross limit")

    fixed_candidate_path, fixed_close, fixed_candidate_metadata = _build_historical_candidate(
        base_weights=base_weights_frozen,
        bars_60m=fixed_bars,
        continuous=fixed_continuous,
    )
    close_provider = pd.concat([fixed_continuous, continuous_extension], ignore_index=True)
    close_panel = stress80.continuous_close_panel(close_provider, list(base_weights_frozen.columns))

    # New rows start strictly after the immutable source cutoff. Overlap rows remain in
    # the provider-return and conflict files only; the frozen versions own that period.
    fixed_tail = _fixed_oi_flow_tail(fixed_bars)
    fetched_tail = oi_vendor[
        (oi_vendor.datetime > FROZEN_CUTOFF)
        & (oi_vendor.datetime <= actual_end + pd.Timedelta(hours=23, minutes=59))
    ]
    flow_bars = pd.concat([fixed_tail, fetched_tail], ignore_index=True)
    flow_bars = flow_bars.drop_duplicates(["datetime", "product", "symbol"], keep="last")
    flow_bars = flow_bars.sort_values(["datetime", "product", "symbol"]).reset_index(drop=True)
    mapped_bars = map_oi_bars_to_trading_day(flow_bars, trading_days=observed_calendar)
    mapped_use = mapped_bars[mapped_bars.trading_day.notna()].copy()
    _csv_frame(mapped_bars, output / "market_normalized" / "oi_60m_trading_day_mapping.csv")
    _csv_frame(
        mapped_bars.groupby("mapping_status", dropna=False).size().rename("rows").reset_index(),
        output / "market_normalized" / "oi_60m_mapping_status.csv",
    )

    source_days = _oi_source_days_for_targets(calendar, observed_calendar)
    needed_flow_days = pd.DatetimeIndex(
        source_days[(source_days >= START - pd.Timedelta(days=1)) & (source_days <= actual_end)]
    ).unique()
    # Include the source session on 2026-08-20 even though the account starts on Aug 21.
    needed_flow_days = pd.DatetimeIndex(
        sorted(set(needed_flow_days) | {pd.Timestamp("2026-08-20")})
    )
    session_coverage = _session_coverage(mapped_use, needed_flow_days)
    _csv_frame(session_coverage, output / "coverage" / "oi_session_coverage.csv")
    contract_details = _oi_contract_details(mapped_use, needed_flow_days)
    _csv_frame(contract_details, output / "coverage" / "oi_contract_session_coverage.csv")
    flow = oi_gate.build_daily_price_oi_flow(mapped_use, trading_day_column="trading_day")
    lagged_flow = oi_gate.lag_flow_to_target_days(
        flow,
        target_days=base_weights_new.index,
        products=STRESS90_POLICY.oi_products,
    )
    _csv_frame(
        flow.rename_axis("trading_day").reset_index(),
        output / "strategy" / "completed_oi_flow_by_trading_day.csv",
    )
    _csv_frame(
        lagged_flow.rename_axis("target_trading_day").reset_index(),
        output / "strategy" / "oi_flow_available_by_target_day.csv",
    )
    oi_source_days = _oi_source_days_for_targets(calendar, observed_calendar)
    _csv_frame(
        pd.DataFrame(
            {
                "target_trading_day": calendar,
                "source_trading_day": oi_source_days,
                "base_price_source_day": oi_source_days,
                "decision_time_shanghai": [
                    f"{day.date()} 15:00:00+08:00" for day in oi_source_days
                ],
                "source_max_time_shanghai": [
                    f"{day.date()} 15:00:00+08:00" for day in oi_source_days
                ],
                "oi_flow_available": [bool(row.notna().all()) for _, row in lagged_flow.iterrows()],
            }
        ),
        output / "coverage" / "signal_availability.csv",
    )

    close_new = close_panel.reindex(index=close_panel.index.union(calendar)).sort_index()
    path = build_stress90_candidate_path(
        base_weights=base_weights_new,
        completed_close_prices=close_new,
        confirming_flow=lagged_flow.reindex(columns=STRESS90_POLICY.oi_products),
        initial_state=fixed_candidate_path.final_state,
    )
    _candidate_layer_table(path, output)
    _write_json(
        output / "strategy" / "preperiod_terminal_state.json",
        {
            "candidate_state": _preperiod_terminal_state(path),
            "preperiod_candidate_state_digest": candidate_state_digest(
                _preperiod_terminal_state(path)
            ),
            "first_new_decision_digest": path.decisions[0].daily_decision_digest,
            "first_new_target_day": pd.Timestamp(base_weights_new.index[0]).date().isoformat(),
            "historical_candidate": fixed_candidate_metadata,
        },
    )
    restart_check = _verify_strategy_restart(
        base_weights=base_weights_new,
        close=close_new,
        lagged_flow=lagged_flow,
        initial_state=fixed_candidate_path.final_state,
    )
    _write_json(output / "strategy" / "restart_verification.json", restart_check)

    candidate_new = path.survivor_weights.reindex(
        index=calendar, columns=base_weights_frozen.columns
    )
    candidate_old = fixed_candidate_path.survivor_weights.reindex(
        columns=base_weights_frozen.columns
    )
    specific_old = fixed_specific.copy()
    specific_old["date"] = pd.to_datetime(specific_old.date, errors="coerce").dt.normalize()
    specific_all = pd.concat(
        [specific_old[specific_old.date <= FROZEN_CUTOFF.normalize()], specific_extension],
        ignore_index=True,
    )
    duplicate_contract_days = int(specific_all.duplicated(["date", "symbol"]).sum())
    if duplicate_contract_days:
        raise RuntimeError(
            f"specific daily duplicate product/date/symbol rows: {duplicate_contract_days}"
        )
    specific_all = specific_all.sort_values(["date", "product", "symbol"]).reset_index(drop=True)
    selection_audit = _contract_selection_audit(specific_all, calendar)
    _csv_frame(selection_audit, output / "coverage" / "concrete_contract_selection.csv")
    selected_symbols = (
        sorted(selection_audit.symbol.astype(str).unique()) if not selection_audit.empty else []
    )
    selected_contract_specs, spec_query_rows = _fetch_selected_contract_specs(
        selected_symbols,
        output=output,
    )
    account_multipliers, selected_contract_specs, multiplier_audit = (
        _account_multipliers_from_specs(selected_contract_specs)
    )
    _csv_frame(
        selected_contract_specs,
        output / "coverage" / "selected_contract_specs.csv",
    )
    _csv_frame(
        multiplier_audit,
        output / "coverage" / "account_multiplier_audit.csv",
    )
    _write_json(
        output / "coverage" / "official_source_urls.json",
        {
            "market_sessions_and_product_rules": OFFICIAL_SOURCE_URLS,
            "contract_spec_provider": "AKShare futures_contract_detail(symbol=...), raw parsed response saved per selected contract",
            "calendar_mapping_note": "Observed DCE/CZCE open dates are cross-checked against official exchange session/product/holiday sources listed here.",
            "source_fetch_log": "official_source_fetch_log.json",
            "source_fetch_results": official_reference_rows,
        },
    )

    # Compare one checkpoint restart of the account in both cost/margin cases.
    scenarios = {}
    frozen_multipliers = PRODUCT_MULTIPLIERS.copy()
    try:
        PRODUCT_MULTIPLIERS.clear()
        PRODUCT_MULTIPLIERS.update(account_multipliers)
        for scenario in ("Base", "Stress"):
            summary, _result = _simulate_account(
                scenario=scenario,
                specific=specific_all,
                weights=candidate_new,
                preperiod_weights=candidate_old,
                output=output,
            )
            scenarios[scenario] = summary
    finally:
        PRODUCT_MULTIPLIERS.clear()
        PRODUCT_MULTIPLIERS.update(frozen_multipliers)

    target_missing_specific = []
    for day in calendar:
        day_products = set(specific_all.loc[specific_all.date == day, "product"].astype(str))
        missing_products = sorted(set(broad_fetch.PRODUCTS) - day_products)
        if missing_products:
            target_missing_specific.append(
                {"date": day.date().isoformat(), "missing_products": missing_products}
            )
    _write_json(output / "coverage" / "specific_missing_product_days.json", target_missing_specific)

    fixed_inputs_after = []
    for item in fixed_manifest:
        path_in = Path(args.input_dir).resolve() / str(item["basename"])
        fixed_inputs_after.append(
            {
                "basename": item["basename"],
                "sha256_before": item["sha256"],
                "sha256_after": _sha256(path_in),
                "size_bytes_before": item["size_bytes"],
                "size_bytes_after": path_in.stat().st_size,
                "unchanged": _sha256(path_in) == item["sha256"]
                and path_in.stat().st_size == item["size_bytes"],
            }
        )
    if not all(item["unchanged"] for item in fixed_inputs_after):
        raise RuntimeError("a frozen source input changed during the replay")
    _write_json(output / "fixed_inputs_unchanged.json", fixed_inputs_after)

    missing_oi_flow = [
        {
            "target_trading_day": day.date().isoformat(),
            "missing_products": list(lagged_flow.columns[lagged_flow.loc[day].isna()]),
        }
        for day in calendar
        if bool(lagged_flow.loc[day].isna().any())
    ]
    _write_json(output / "coverage" / "missing_oi_flow_by_target_day.json", missing_oi_flow)

    report = {
        "run_id": args.run_id,
        "evidence_scope": "historical_account_proxy",
        "prospective_evidence": False,
        "live_authorized": False,
        "risk_increase_authorized": False,
        "source": {
            "base_main_sha": args.base_main_sha,
            "code_commit_sha": args.code_commit_sha,
            "vault_source_sha": args.vault_source_sha,
            "pr55": "open_draft_unmerged; independent replay is not blocked by production-orchestration gaps",
            "strategy_policy_digest": STRESS90_POLICY.policy_definition_digest,
            "fixed_historical_candidate_digest": fixed_candidate_metadata[
                "candidate_weight_sha256"
            ],
            "historical_candidate_expected_digest": EXPECTED_CANDIDATE_WEIGHT_SHA256,
            "base_weight_prefix_parity": {
                **base_weight_prefix_audit,
                "policy_start_source": "tools/evaluate_directional_production_mechanics.py:WINDOWS['train'][0]",
                "policy_mismatch_file": "strategy/base_weight_prefix_mismatches.csv",
                "pre_policy_mismatch_file": "strategy/pre_policy_weight_history_differences.csv",
            },
        },
        "scope": {
            "requested_start": START.date().isoformat(),
            "requested_end_cap": end_cap.date().isoformat(),
            "actual_start": pd.Timestamp(calendar[0]).date().isoformat(),
            "actual_end": actual_end.date().isoformat(),
            "execution_time_shanghai": execution_time,
            "task_scope_check_time_shanghai": TASK_START_SHANGHAI,
            "timezone": "Asia/Shanghai",
            "cutoff_reason": "the request began before the 2026-09-23 close; the latest verified common complete daily data were 2026-09-22",
            "requested_2026_09_23_excluded": True,
            "no_live_month_claim": True,
        },
        "coverage": {
            "actual_start": pd.Timestamp(calendar[0]).date().isoformat(),
            "actual_end": actual_end.date().isoformat(),
            "common_complete_cutoff": actual_end.date().isoformat(),
            "target_trading_days": int(len(calendar)),
            "continuous_products": len(broad_fetch.PRODUCTS),
            "specific_contract_products": specific_all["product"].nunique(),
            "supported_oi_products": list(oi_gate.SUPPORTED_PRODUCTS),
            "target_missing_specific_product_days": target_missing_specific,
            "missing_oi_flow_target_days": missing_oi_flow,
            "provider_daily_overlap_conflicts": {
                "continuous": len(overlap_continuous),
                "specific_contracts": len(overlap_specific),
                "oi_60m": len(overlap_oi),
            },
            "selected_contract_spec_count": len(selected_contract_specs),
            "selected_contract_spec_missing_count": max(
                0, len(selected_symbols) - len(selected_contract_specs)
            ),
            "selected_contracts_with_provider_tick": int(
                selected_contract_specs.price_tick.notna().sum()
            )
            if not selected_contract_specs.empty
            else 0,
            "new_specific_settle_field_rows": int(
                specific_field_coverage.loc[
                    specific_field_coverage.field == "settle", "rows_present"
                ].sum()
            ),
            "new_specific_daily_rows_with_missing_settle": int(
                specific_field_coverage.loc[
                    specific_field_coverage.field == "settle", "rows_missing"
                ].sum()
            ),
            "raw_response_directories_complete": True,
            "fixed_five_source_files_unchanged": True,
        },
        "market": _market_manifest(query_rows),
        "contract_spec_evidence": {
            "provider": "AKShare/Sina futures_contract_detail(symbol=...) per account-selected specific contract",
            "selected_contracts": len(selected_symbols),
            "responses_ok": sum(item["result"] == "ok" for item in spec_query_rows),
            "responses_empty": sum(item["result"] == "empty_response" for item in spec_query_rows),
            "responses_error": sum(item["result"] == "error" for item in spec_query_rows),
            "multiplier_matches_frozen_model": int(
                selected_contract_specs.model_multiplier_matches_provider.sum()
            )
            if not selected_contract_specs.empty
            else 0,
            "account_multiplier_matches_selected_contracts": int(
                selected_contract_specs.account_multiplier_matches_provider.sum()
            )
            if not selected_contract_specs.empty
            else 0,
            "model_multiplier_discrepancies": multiplier_audit.loc[
                multiplier_audit.differs_from_frozen_model
            ].to_dict("records"),
            "account_multiplier_audit": "coverage/account_multiplier_audit.csv",
            "account_economics_use_effective_selected_contract_multiplier": True,
            "strategy_weights_and_risk_thresholds_changed": False,
            "price_tick_used_in_simulation": False,
            "query_log": "market_spec_query_log.csv",
            "normalized_table": "coverage/selected_contract_specs.csv",
            "raw_directory": "market_raw/contract_specs/",
        },
        "official_reference_evidence": {
            "requested_sources": len(official_reference_rows),
            "retrieved_sources": sum(item["result"] == "ok" for item in official_reference_rows),
            "failed_sources": [item for item in official_reference_rows if item["result"] != "ok"],
            "fetch_log": "official_source_fetch_log.json",
            "raw_directory": "market_raw/official_sources/",
        },
        "session_mapping": {
            "product_set": list(oi_gate.SUPPORTED_PRODUCTS),
            "exchanges": {
                product: "CZCE" if product == "TA" else "DCE"
                for product in oi_gate.SUPPORTED_PRODUCTS
            },
            "day_sessions": ["09:00-10:15", "10:30-11:30", "13:30-15:00"],
            "night_session": "21:00-23:00",
            "night_mapping": "calendar date evening -> next date in the 50-root observed open-day intersection",
            "bar_chronology": "vendor timestamps retained and sorted; mapped session includes prior-calendar-evening bars before target-day daytime bars",
            "ct_calendar_source": "AKShare/Sina 50-root complete daily date intersection, cross-checked against official exchange sessions/holiday notices",
            "session_coverage_table": "coverage/oi_session_coverage.csv",
        },
        "strategy": {
            "parameter_search": False,
            "template_pool_changed": False,
            "risk_thresholds_changed": False,
            "account_initial_capital": mechanics.INITIAL_CAPITAL,
            "account_initial_positions": {},
            "deposits": 0.0,
            "risk_limits": asdict(_account_config(mechanics.BASE_MARGIN_PROXY)),
            "margin_buffer": _account_config(mechanics.BASE_MARGIN_PROXY).margin_estimate_buffer,
            "drawdown_reserve_ratio": stress80.DRAWDOWN_RESERVE,
            "oi_session_flow_digest_source": "completed trading-day D flow, exposed at target D+1 only",
            "specific_contract_lifecycle_proxy": "frozen delivery_date(symbol)=contract-month day 15; min_days_to_delivery=20 (not exchange-specific final trading date)",
            "old_candidate_state": fixed_candidate_metadata,
        },
        "scenarios": scenarios,
        "restart_verification": restart_check,
        "fixed_input_manifest": fixed_manifest,
        "fixed_input_postrun_verification": fixed_inputs_after,
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "akshare": __import__("akshare").__version__,
            "command": sys.argv,
            "script_sha256": _sha256(Path(__file__).resolve()),
            "exit_code": 0,
        },
        "limitations": [
            "这是历史市场数据上的离线账户代理，不是真实账户成交、真实 CTP 或一个自然月模拟。",
            "AKShare/Sina 60m 接口没有交易日字段；本轮按正式 DCE/CZCE 会话时段和观察到的完整开市日映射，仍不等于历史 CTP trading_day 原始字段。",
            "具体合约日线使用冻结代码定义的每月 15 日交割日代理及 20 日缓冲，不等于逐品种最终交易日。",
            "保证金使用固定代理；日线成交规则无法核验真实滑点、盘口容量、排队、部分成交或盘中最大回撤。",
            "期末如有持仓，按最后交易日收盘价计入浮动盈亏，不自动平仓、不计虚构退出费用。",
            "短区间年化/Sharpe 不用于旧两年验收门槛或未来盈利证明。",
        ],
    }
    _write_json(output / "replay_report.json", report)
    (output / "report_zh.md").write_text(_chinese_report(report), encoding="utf-8")
    _csv_frame(pd.DataFrame(query_rows), output / "market_query_log.csv")

    # One authoritative run manifest includes hashes/byte counts for every payload file
    # except itself and SHA256SUMS; SHA256SUMS then includes this manifest.
    artifacts = []
    for path in sorted(
        item
        for item in output.rglob("*")
        if item.is_file() and item.name not in {"run_manifest.json", "SHA256SUMS"}
    ):
        artifacts.append(
            {
                "path": path.relative_to(output).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    manifest = {
        "run_id": args.run_id,
        "created_at_shanghai": datetime.now(SHANGHAI).isoformat(),
        "private_only_payload": True,
        "evidence_scope": report["evidence_scope"],
        "base_main_sha": args.base_main_sha,
        "code_commit_sha": args.code_commit_sha,
        "vault_source_sha": args.vault_source_sha,
        "output_file_count_before_manifest": len(artifacts),
        "total_payload_bytes_before_manifest": sum(item["size_bytes"] for item in artifacts),
        "files": artifacts,
        "report_sha256": _sha256(output / "replay_report.json"),
        "fixed_input_manifest": fixed_manifest,
    }
    _write_json(output / "run_manifest.json", manifest)
    checksums = []
    for path in sorted(
        item for item in output.rglob("*") if item.is_file() and item.name != "SHA256SUMS"
    ):
        checksums.append(f"{_sha256(path)}  {path.relative_to(output).as_posix()}")
    (output / "SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "run_id": args.run_id,
                "actual_end": actual_end.date().isoformat(),
                "scenarios": scenarios,
                "output": str(output),
            },
            ensure_ascii=False,
            default=_json_default,
        ),
        flush=True,
    )
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--end-cap", default="2026-09-22")
    parser.add_argument("--base-main-sha", default=FIXED_MAIN_SHA)
    parser.add_argument("--code-commit-sha", required=True)
    parser.add_argument("--vault-source-sha", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    run_replay(_parse_args())
