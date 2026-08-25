"""用与生产一致的时间轴和 Universe 复验两年日频相对价值证据。

真实字段来自 AKShare/Sina specific-contract 60 分钟 OHLC、成交量和持仓量；
历史 L1 bid/ask/depth 不可得，因此本脚本只证明信号层经济性，不替代 Shadow/CTP
成交质量验证。参数和品种资格只使用 OOS 之前的窗口；final OOS 不参与选择。
"""

from __future__ import annotations

import json
import math
import re
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

PRODUCTS = ("A", "C", "EG", "FG", "I", "M", "MA", "OI", "P", "PP", "RB", "RM", "SA", "TA", "Y")
SPECS = {
    "A": (10.0, 1.0),
    "C": (10.0, 1.0),
    "EG": (10.0, 1.0),
    "FG": (20.0, 1.0),
    "I": (100.0, 0.5),
    "M": (10.0, 1.0),
    "MA": (10.0, 1.0),
    "OI": (10.0, 1.0),
    "P": (10.0, 2.0),
    "PP": (5.0, 1.0),
    "RB": (10.0, 1.0),
    "RM": (10.0, 1.0),
    "SA": (20.0, 1.0),
    "TA": (5.0, 2.0),
    "Y": (10.0, 2.0),
}
WINDOWS = {
    "prior1": ("2022-08-22", "2023-08-20"),
    "prior2": ("2023-08-21", "2024-08-20"),
    "train": ("2024-08-21", "2025-08-20"),
    "validation": ("2025-08-21", "2026-02-20"),
    "oos": ("2026-02-21", "2026-08-20"),
    "full_recent": ("2024-08-21", "2026-08-20"),
}
PROFILE = {
    "lookback": 25,
    "mode": "logratio",
    "confirm": True,
    "arm": 2.5,
    "confirm_delta": 0.3,
    "min_entry": 1.75,
    "exit_z": 0.75,
    "stop": 4.0,
    "maxhold": 20,
    "trend_window": 6,
    "max_z_slope": 0.75,
    "min_stationarity": 0.01,
    "max_half_life": 60.0,
    "cost_mult": 2.0,
    "cost_ticks": 5.0,
    "min_oi": 5000.0,
    "min_bar_volume": 1000.0,
}
SAMPLE_START_MINUTE = 22 * 60 + 55
SAMPLE_END_MINUTE = 23 * 60
DAY_SESSION_START_MINUTE = 8 * 60
DAY_SESSION_END_MINUTE = 20 * 60
NIGHT_SESSION_START_MINUTE = 20 * 60
MIN_DAYS_TO_EXPIRY = 20
MAX_CONTRACTS_PER_PRODUCT = 3
DELIVERY_DAY_PROXY = 15


def _contract_key(symbol: str) -> int:
    match = re.search(r"(\d+)$", str(symbol))
    return int(match.group(1)) if match else 0


def _delivery_date(symbol: str) -> pd.Timestamp | None:
    """公开历史源无官方 ExpireDate，研究使用交割月 15 日的保守代理。"""
    match = re.search(r"(\d{4})$", str(symbol))
    if not match:
        return None
    digits = match.group(1)
    try:
        return pd.Timestamp(date(2000 + int(digits[:2]), int(digits[2:]), DELIVERY_DAY_PROXY))
    except ValueError:
        return None


def _prepare_intraday(raw: pd.DataFrame) -> pd.DataFrame:
    """把自然时间映射到中国期货交易日，并计算当时可见的累计成交量。"""
    frame = raw.copy()
    frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce")
    for column in ("close", "volume", "hold"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = (
        frame.dropna(subset=["datetime", "close", "volume", "hold", "symbol", "product"])
        .drop_duplicates(["symbol", "datetime"], keep="last")
        .sort_values(["datetime", "symbol"])
        .reset_index(drop=True)
    )
    if frame.empty:
        frame["calendar_date"] = pd.Series(dtype="datetime64[ns]")
        frame["trading_day"] = pd.Series(dtype="datetime64[ns]")
        frame["visible_volume"] = pd.Series(dtype=float)
        return frame

    frame["calendar_date"] = frame["datetime"].dt.normalize()
    minute = frame["datetime"].dt.hour * 60 + frame["datetime"].dt.minute
    day_mask = (minute >= DAY_SESSION_START_MINUTE) & (minute < DAY_SESSION_END_MINUTE)
    day_dates = pd.DatetimeIndex(
        sorted(pd.to_datetime(frame.loc[day_mask, "calendar_date"].dropna().unique()))
    )
    trading_days = []
    for timestamp, natural_day in zip(frame["datetime"], frame["calendar_date"], strict=True):
        current_minute = timestamp.hour * 60 + timestamp.minute
        if current_minute >= NIGHT_SESSION_START_MINUTE:
            index = day_dates.searchsorted(natural_day, side="right")
            trading_days.append(day_dates[index] if index < len(day_dates) else pd.NaT)
        else:
            trading_days.append(natural_day)
    frame["trading_day"] = pd.to_datetime(trading_days)
    frame = (
        frame.dropna(subset=["trading_day"])
        .sort_values(["datetime", "symbol"])
        .reset_index(drop=True)
    )
    frame["visible_volume"] = frame.groupby(["product", "symbol", "trading_day"], sort=False)[
        "volume"
    ].cumsum()
    return frame


def build_pair_frames(
    prior: pd.DataFrame, current: pd.DataFrame
) -> dict[tuple[str, str, str], pd.DataFrame]:
    """按生产规则构建 point-in-time front-3 相邻月价差序列。"""
    combined = _prepare_intraday(pd.concat([prior, current], ignore_index=True))
    if combined.empty:
        return {}
    minute = combined["datetime"].dt.hour * 60 + combined["datetime"].dt.minute
    sample = combined[(minute >= SAMPLE_START_MINUTE) & (minute <= SAMPLE_END_MINUTE)].copy()
    if sample.empty:
        return {}
    sample = (
        sample.sort_values("datetime")
        .groupby(["product", "symbol", "trading_day"], as_index=False)
        .tail(1)
    )

    rows_by_pair: dict[tuple[str, str, str], list[dict]] = {}
    for (product, trading_day), day_rows in sample.groupby(["product", "trading_day"]):
        day_ts = pd.Timestamp(trading_day)
        eligible = []
        for _, row in day_rows.iterrows():
            delivery = _delivery_date(str(row["symbol"]))
            if delivery is None or (delivery - day_ts).days < MIN_DAYS_TO_EXPIRY:
                continue
            eligible.append((delivery, str(row["symbol"]), row))
        eligible.sort(key=lambda item: (item[0], _contract_key(item[1]), item[1]))
        eligible = eligible[:MAX_CONTRACTS_PER_PRODUCT]
        for (_, near_symbol, near_row), (_, far_symbol, far_row) in zip(
            eligible, eligible[1:], strict=False
        ):
            near_time = pd.Timestamp(near_row["datetime"])
            far_time = pd.Timestamp(far_row["datetime"])
            if near_time != far_time:
                continue
            key = (str(product), near_symbol, far_symbol)
            rows_by_pair.setdefault(key, []).append(
                {
                    "trading_day": day_ts,
                    "sample_timestamp": near_time,
                    "near": float(near_row["close"]),
                    "near_vol": float(near_row["visible_volume"]),
                    "near_hold": float(near_row["hold"]),
                    "far": float(far_row["close"]),
                    "far_vol": float(far_row["visible_volume"]),
                    "far_hold": float(far_row["hold"]),
                }
            )

    result = {}
    for key, rows in rows_by_pair.items():
        pair = pd.DataFrame(rows).sort_values("sample_timestamp").reset_index(drop=True)
        pair["datetime"] = pair["trading_day"]
        pair["raw"] = pair["near"] - pair["far"]
        pair["logratio"] = np.log(pair["near"] / pair["far"])
        result[key] = pair
    return result


def rolling_z(frame: pd.DataFrame, lookback: int):
    values = frame["logratio"].to_numpy(float)
    raw = frame["raw"].to_numpy(float)
    series = pd.Series(values)
    reference_mean = series.rolling(lookback, min_periods=lookback).mean().shift(1).to_numpy()
    reference_std = series.rolling(lookback, min_periods=lookback).std(ddof=0).shift(1).to_numpy()
    zscore = (values - reference_mean) / reference_std
    raw_std = pd.Series(raw).rolling(lookback, min_periods=lookback).std(ddof=0).shift(1).to_numpy()
    return zscore, raw_std


def rolling_mr_stats(values: np.ndarray, lookback: int):
    count = len(values)
    half_life = np.full(count, np.nan)
    stationarity = np.full(count, np.nan)
    for index in range(lookback, count):
        window = np.asarray(values[index - lookback : index], float)
        levels = window[:-1]
        changes = np.diff(window)
        level_mean = levels.mean()
        change_mean = changes.mean()
        denominator = ((levels - level_mean) ** 2).sum()
        if denominator <= 1e-12:
            half_life[index] = 999.0
            stationarity[index] = 0.0
            continue
        beta = ((levels - level_mean) * (changes - change_mean)).sum() / denominator
        if beta >= 0:
            half_life[index] = 999.0
            stationarity[index] = 0.0
        else:
            half_life[index] = max(0.1, -math.log(2.0) / beta)
            stationarity[index] = min(1.0, max(0.0, -beta))
    return half_life, stationarity


def generate_trades(frame: pd.DataFrame, product: str, params: dict) -> pd.DataFrame:
    """用冻结的 M/OI 信号规则生成成本后价差交易。"""
    lookback = int(params["lookback"])
    zscore, raw_std = rolling_z(frame, lookback)
    half_life, stationarity = rolling_mr_stats(frame["logratio"].to_numpy(float), lookback)
    raw = frame["raw"].to_numpy(float)
    datetimes = frame["datetime"].to_numpy()
    near_hold = frame["near_hold"].to_numpy(float)
    far_hold = frame["far_hold"].to_numpy(float)
    near_vol = frame["near_vol"].to_numpy(float)
    far_vol = frame["far_vol"].to_numpy(float)
    multiplier, tick = SPECS[product]
    cost_points = float(params["cost_ticks"]) * tick * float(params["cost_mult"])

    rows = []
    position = 0
    entry_index = None
    armed = 0
    armed_extreme = None
    for index in range(lookback, len(frame)):
        current_z = zscore[index]
        if not np.isfinite(current_z):
            continue
        liquid = (
            min(near_hold[index], far_hold[index]) >= params["min_oi"]
            and min(near_vol[index], far_vol[index]) >= params["min_bar_volume"]
        )
        if position == 0:
            if (
                not liquid
                or stationarity[index] < params["min_stationarity"]
                or half_life[index] > params["max_half_life"]
            ):
                armed = 0
                armed_extreme = None
                continue
            trend_window = int(params["trend_window"])
            slope = 0.0
            if (
                trend_window > 1
                and index >= trend_window - 1
                and np.all(np.isfinite(zscore[index - trend_window + 1 : index + 1]))
            ):
                slope = float(
                    np.polyfit(
                        np.arange(trend_window),
                        zscore[index - trend_window + 1 : index + 1],
                        1,
                    )[0]
                )
            if armed == 0:
                if current_z >= params["arm"]:
                    armed = -1
                    armed_extreme = current_z
                elif current_z <= -params["arm"]:
                    armed = 1
                    armed_extreme = current_z
                continue
            if armed == -1:
                armed_extreme = max(float(armed_extreme), current_z)
                confirmed = (
                    current_z <= armed_extreme - params["confirm_delta"]
                    and current_z >= params["min_entry"]
                )
            else:
                armed_extreme = min(float(armed_extreme), current_z)
                confirmed = (
                    current_z >= armed_extreme + params["confirm_delta"]
                    and current_z <= -params["min_entry"]
                )
            if confirmed and abs(slope) <= params["max_z_slope"]:
                position = armed
                entry_index = index
                entry_raw = raw[index]
                entry_z = current_z
                entry_std = raw_std[index]
                entry_half_life = half_life[index]
                entry_stationarity = stationarity[index]
                armed = 0
                armed_extreme = None
            elif (armed == -1 and current_z < params["min_entry"]) or (
                armed == 1 and current_z > -params["min_entry"]
            ):
                armed = 0
                armed_extreme = None
            continue

        holding = index - int(entry_index)
        normal_exit = (position == 1 and current_z >= -params["exit_z"]) or (
            position == -1 and current_z <= params["exit_z"]
        )
        stop = (position == 1 and current_z <= -params["stop"]) or (
            position == -1 and current_z >= params["stop"]
        )
        timeout = params["maxhold"] > 0 and holding >= params["maxhold"]
        terminal = index == len(frame) - 1
        if normal_exit or stop or timeout or terminal:
            pnl_points = position * (raw[index] - entry_raw) - cost_points
            pnl_cash = pnl_points * multiplier
            risk_cash = (
                max(entry_std if np.isfinite(entry_std) and entry_std > 0 else tick, tick)
                * multiplier
                * 2.5
            )
            rows.append(
                {
                    "entry_time": pd.Timestamp(datetimes[entry_index]),
                    "exit_time": pd.Timestamp(datetimes[index]),
                    "pnl_cash": float(pnl_cash),
                    "pnl_points": float(pnl_points),
                    "R": float(pnl_cash / risk_cash),
                    "entry_z": float(entry_z),
                    "exit_z": float(current_z),
                    "hold": int(holding),
                    "entry_std": float(entry_std),
                    "half_life": float(entry_half_life),
                    "stationarity": float(entry_stationarity),
                }
            )
            position = 0
            entry_index = None
    return pd.DataFrame(rows)


def product_trades(frames, product: str, start: str, end: str, params: dict):
    all_rows = []
    for (root, near, far), frame in frames.items():
        if root != product:
            continue
        subset = frame[
            (frame.datetime >= pd.Timestamp(start)) & (frame.datetime <= pd.Timestamp(end))
        ].reset_index(drop=True)
        if len(subset) < params["lookback"] + 10:
            continue
        trades = generate_trades(subset, product, params)
        if not trades.empty:
            trades = trades.copy()
            trades["near"] = near
            trades["far"] = far
            all_rows.append(trades)
    if not all_rows:
        return pd.DataFrame()
    candidates = pd.concat(all_rows, ignore_index=True).sort_values("entry_time")
    accepted = []
    last_exit = pd.Timestamp.min
    for _, row in candidates.iterrows():
        if row.entry_time > last_exit:
            accepted.append(row)
            last_exit = row.exit_time
    return pd.DataFrame(accepted)


def portfolio(frames, roots, window_name: str, params: dict):
    start, end = WINDOWS[window_name]
    rows = []
    for product in roots:
        trades = product_trades(frames, product, start, end, params)
        if not trades.empty:
            trades = trades.copy()
            trades["product"] = product
            rows.append(trades)
    if not rows:
        return pd.DataFrame(columns=["R", "pnl_cash"])
    return pd.concat(rows, ignore_index=True).sort_values(["entry_time", "product"])


def metrics(trades):
    if trades is None or trades.empty:
        return {
            "trades": 0,
            "pnl": 0.0,
            "R": 0.0,
            "win": None,
            "mdd_R": 0.0,
            "pf": None,
            "avgR": 0.0,
        }
    values = trades["R"].to_numpy(float)
    curve = np.cumsum(values)
    peak = np.maximum.accumulate(np.r_[0.0, curve])
    drawdown = np.r_[0.0, curve] - peak
    gains = values[values > 0].sum()
    losses = -values[values < 0].sum()
    return {
        "trades": int(len(trades)),
        "pnl": float(trades.pnl_cash.sum()),
        "R": float(values.sum()),
        "win": float((values > 0).mean()),
        "mdd_R": float(drawdown.min()),
        "pf": float(gains / losses) if losses > 0 else None,
        "avgR": float(values.mean()),
    }


def qualify(frames, params, left: str, right: str):
    """品种身份是冻结规则的输出，不作为硬编码验收输入。"""
    result = []
    for product in PRODUCTS:
        left_metrics = metrics(portfolio(frames, [product], left, params))
        right_metrics = metrics(portfolio(frames, [product], right, params))
        if (
            left_metrics["trades"] >= 1
            and right_metrics["trades"] >= 1
            and left_metrics["R"] > 0
            and right_metrics["R"] > 0
        ):
            result.append(product)
    return result


def capital_proxy(trades, risk_fraction: float):
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in trades["R"].to_numpy(float) if not trades.empty else []:
        equity *= max(0.0, 1.0 + risk_fraction * value)
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1.0)
    return {"return": float(equity - 1.0), "max_drawdown": float(max_drawdown)}


def _annualized(total_return: float, trading_days: int) -> float:
    if trading_days <= 0:
        return 0.0
    if total_return <= -1.0:
        return -1.0
    return (1.0 + total_return) ** (252.0 / trading_days) - 1.0


def neighbor_profiles():
    variants = []
    for name, values in (
        ("lookback", (20, 30)),
        ("arm", (2.25, 2.75)),
        ("confirm_delta", (0.2, 0.4)),
        ("min_entry", (1.5, 2.0)),
        ("exit_z", (0.5, 1.0)),
        ("stop", (3.5, 4.5)),
        ("maxhold", (15, 25)),
        ("max_z_slope", (0.5, 1.0)),
    ):
        for value in values:
            row = dict(PROFILE)
            row[name] = value
            variants.append((f"{name}={value}", row))
    return variants


def promotion_reasons(report: dict) -> list[str]:
    """固定经济晋级门；不得根据最终 OOS 结果移动阈值。"""
    reasons: list[str] = []
    if not report.get("qualified_prior"):
        reasons.append("prior qualification is empty")
    if not report.get("qualified_current"):
        reasons.append("current qualification is empty")
    prior_forward = report.get("prior_forward", {})
    if int(prior_forward.get("trades", 0)) < 2:
        reasons.append("prior forward trade sample is below 2")
    if float(prior_forward.get("R", 0.0)) <= 0:
        reasons.append("prior forward return is not positive")
    final_oos = report.get("final_oos", {})
    if int(final_oos.get("trades", 0)) < 3:
        reasons.append("final OOS trade sample is below 3")
    if float(final_oos.get("R", 0.0)) <= 0:
        reasons.append("final OOS return is not positive")
    if float(final_oos.get("mdd_R", -999.0)) <= -0.5:
        reasons.append("final OOS drawdown exceeds 0.5R")
    full_recent = report.get("full_recent", {})
    if int(full_recent.get("trades", 0)) < 10:
        reasons.append("two-year trade sample is below 10")
    if float(full_recent.get("R", 0.0)) <= 2.0:
        reasons.append("two-year return is not above 2R")
    if float(full_recent.get("mdd_R", -999.0)) <= -0.5:
        reasons.append("two-year drawdown exceeds 0.5R")
    if float(report.get("neighbor_pass_ratio", 0.0)) < 0.5:
        reasons.append("neighbor stability is below 50%")
    return reasons


def evaluate(prior: pd.DataFrame, current: pd.DataFrame) -> dict:
    frames = build_pair_frames(prior, current)
    qualified_prior = qualify(frames, PROFILE, "prior1", "prior2")
    qualified_current = qualify(frames, PROFILE, "train", "validation")
    prior_forward_trades = portfolio(frames, qualified_prior, "train", PROFILE)
    final_oos_trades = portfolio(frames, qualified_current, "oos", PROFILE)
    full_recent_trades = portfolio(frames, qualified_current, "full_recent", PROFILE)

    neighbors = []
    for label, params in neighbor_profiles():
        prior_products = qualify(frames, params, "prior1", "prior2")
        current_products = qualify(frames, params, "train", "validation")
        prior_metrics = metrics(portfolio(frames, prior_products, "train", params))
        oos_metrics = metrics(portfolio(frames, current_products, "oos", params))
        passed = bool(
            prior_products
            and current_products
            and prior_metrics["trades"] >= 2
            and oos_metrics["trades"] >= 2
            and prior_metrics["R"] > 0
            and oos_metrics["R"] > 0
        )
        neighbors.append(
            {
                "variant": label,
                "prior_products": prior_products,
                "current_products": current_products,
                "prior_forward": prior_metrics,
                "oos": oos_metrics,
                "passed": passed,
            }
        )
    pass_count = sum(item["passed"] for item in neighbors)

    prepared_current = _prepare_intraday(current)
    current_days = int(
        prepared_current[
            (prepared_current.trading_day >= pd.Timestamp(WINDOWS["full_recent"][0]))
            & (prepared_current.trading_day <= pd.Timestamp(WINDOWS["full_recent"][1]))
        ].trading_day.nunique()
    )
    proxies = {
        name: capital_proxy(full_recent_trades, risk)
        for name, risk in (
            ("1pct_risk", 0.01),
            ("1_5pct_risk", 0.015),
            ("2pct_risk", 0.02),
        )
    }
    for row in proxies.values():
        row["annualized_return"] = _annualized(row["return"], current_days)

    report = {
        "source": "AKShare/Sina specific-contract 60-minute bars",
        "historical_l1_available": False,
        "sample_reference": (
            "last exact common 60-minute timestamp within 22:55-23:00 China-time; "
            "no day-session fallback"
        ),
        "trading_day_mapping": (
            "night bars >=20:00 map to the next observed day-session date; "
            "unmatched tail nights are dropped fail-closed"
        ),
        "universe_alignment": (
            "per futures trading day: 20-day delivery blackout using delivery-month "
            "15th proxy, then front three contracts and adjacent pairs only"
        ),
        "activity_alignment": "cumulative volume through sample >=1000 and OI >=5000",
        "windows": WINDOWS,
        "profile": PROFILE,
        "cost_stress": "2x conservative round-trip ticks",
        "qualified_prior": qualified_prior,
        "qualified_current": qualified_current,
        "prior_forward": metrics(prior_forward_trades),
        "final_oos": metrics(final_oos_trades),
        "full_recent": metrics(full_recent_trades),
        "capital_proxy": proxies,
        "neighbor_pass_count": pass_count,
        "neighbor_total": len(neighbors),
        "neighbor_pass_ratio": pass_count / len(neighbors) if neighbors else 0.0,
        "neighbors": neighbors,
        "two_year_trading_days": current_days,
    }
    report["promotion_reasons"] = promotion_reasons(report)
    report["accepted"] = not report["promotion_reasons"]
    observed_annualized = float(proxies["2pct_risk"]["annualized_return"])
    report["target"] = {
        "annualized_return": 1.0,
        "observed_2pct_risk_proxy_annualized_return": observed_annualized,
        "target_met": bool(report["accepted"] and observed_annualized >= 1.0),
        "note": "risk proxy is a diagnostic, not permission to increase leverage",
    }
    return report


def main() -> int:
    runtime = Path("runtime")
    current_path = runtime / "two_year_broad_60m.csv"
    prior_path = runtime / "prior_two_year_broad_60m.csv"
    if not current_path.exists() or not prior_path.exists():
        raise SystemExit("real-data files are missing; run the fetch tools first")
    report = evaluate(pd.read_csv(prior_path), pd.read_csv(current_path))
    output = runtime / "two_year_strategy_report.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "accepted": report["accepted"],
                "promotion_reasons": report["promotion_reasons"],
                "qualified_prior": report["qualified_prior"],
                "qualified_current": report["qualified_current"],
                "prior_forward": report["prior_forward"],
                "final_oos": report["final_oos"],
                "full_recent": report["full_recent"],
                "neighbor_pass_ratio": report["neighbor_pass_ratio"],
                "capital_proxy": report["capital_proxy"],
                "target": report["target"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
