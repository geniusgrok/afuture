"""Causal low-gross-leverage return-target portfolio research.

Research only. Signals observed through close t may earn only t+1 returns. Continuous
contracts are L3 discovery evidence; production promotion requires later roll-safe
specific-contract validation and never bypasses afuture's RiskManager/PairExecutor.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

WINDOWS = {
    "prior1": ("2022-08-22", "2023-08-20"),
    "prior2": ("2023-08-21", "2024-08-20"),
    "train": ("2024-08-21", "2025-08-20"),
    "validation": ("2025-08-21", "2026-02-20"),
    "selection_full": ("2024-08-21", "2026-02-20"),
    "oos": ("2026-02-21", "2026-08-20"),
    "full_recent": ("2024-08-21", "2026-08-20"),
}
BASE_COST_BPS = 5.0
STRESS_COST_BPS = 15.0
EXTREME_COST_BPS = 30.0
MAX_GROSS_LEVERAGE = 2.0
MAX_ABS_DAILY_RETURN = 0.20
META_POOL_SIZE = 16

EXCHANGE_PRODUCTS = {
    "DCE": {
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
    },
    "CZCE": {
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
    },
    "SHFE": {
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
    },
    "INE": {"BC", "LU", "NR"},
}
PRODUCT_EXCHANGE = {
    product: exchange for exchange, products in EXCHANGE_PRODUCTS.items() for product in products
}


@dataclass(frozen=True)
class AlphaTemplate:
    family: str
    slow: int
    fast: int
    vol_window: int
    rebalance: int
    max_pairs: int
    gross_leverage: float


def primary_templates() -> tuple[AlphaTemplate, ...]:
    rows: list[AlphaTemplate] = []
    for slow in (10, 20, 40, 60, 120):
        for rebalance in (1, 5):
            for gross in (1.0, 1.5, 2.0):
                rows.append(AlphaTemplate("momentum", slow, 0, 20, rebalance, 1, gross))
    for fast in (1, 3, 5, 10):
        for rebalance in (1, 2, 5):
            for gross in (1.0, 1.5):
                rows.append(AlphaTemplate("reversal", 0, fast, 20, rebalance, 1, gross))
    for slow, fast in ((20, 3), (40, 5), (60, 5), (60, 10), (120, 10)):
        for rebalance in (1, 5):
            for gross in (1.0, 1.5, 2.0):
                rows.append(AlphaTemplate("slow_fast", slow, fast, 20, rebalance, 1, gross))
    for slow in (20, 40, 60):
        for rebalance in (1, 5):
            for gross in (1.0, 1.5, 2.0):
                rows.append(AlphaTemplate("breakout", slow, 5, 20, rebalance, 1, gross))
    return tuple(rows)


def template_id(template: AlphaTemplate) -> str:
    return (
        f"{template.family}_s{template.slow}_f{template.fast}_v{template.vol_window}"
        f"_r{template.rebalance}_p{template.max_pairs}_g{template.gross_leverage:g}"
    )


def _cross_sectional_z(frame: pd.DataFrame) -> pd.DataFrame:
    mean = frame.mean(axis=1)
    std = frame.std(axis=1).replace(0.0, np.nan)
    return frame.sub(mean, axis=0).div(std, axis=0)


def _rolling_log_return(values: pd.DataFrame, window: int) -> pd.DataFrame:
    return np.log1p(values.clip(lower=-0.99)).rolling(window, min_periods=window).sum()


def signal_scores(returns: pd.DataFrame, template: AlphaTemplate) -> pd.DataFrame:
    if template.family == "momentum":
        return _rolling_log_return(returns, template.slow)
    if template.family == "reversal":
        return -_rolling_log_return(returns, template.fast)
    if template.family == "slow_fast":
        slow = _cross_sectional_z(_rolling_log_return(returns, template.slow))
        fast = _cross_sectional_z(_rolling_log_return(returns, template.fast))
        return slow - 0.5 * fast
    if template.family == "breakout":
        synthetic = (1.0 + returns.fillna(0.0)).cumprod()
        low = synthetic.rolling(template.slow, min_periods=template.slow).min()
        high = synthetic.rolling(template.slow, min_periods=template.slow).max()
        location = (synthetic - low) / (high - low).replace(0.0, np.nan)
        acceleration = _rolling_log_return(returns, template.fast)
        return _cross_sectional_z(location) + 0.5 * _cross_sectional_z(acceleration)
    raise ValueError(f"unknown alpha family: {template.family}")


def _exchange_groups(columns: list[str], exchange_map: dict[str, str]) -> list[np.ndarray]:
    groups: list[np.ndarray] = []
    for exchange in sorted(set(exchange_map.values())):
        indices = [
            index for index, name in enumerate(columns) if exchange_map.get(name) == exchange
        ]
        if len(indices) >= 2:
            groups.append(np.asarray(indices, dtype=int))
    return groups


def _weights_from_arrays(
    score: np.ndarray,
    volatility: np.ndarray,
    groups: list[np.ndarray],
    *,
    max_pairs: int,
    gross_leverage: float,
) -> tuple[np.ndarray, list[int]]:
    weights = np.zeros(len(score), dtype=float)
    if max_pairs <= 0 or gross_leverage <= 0.0:
        return weights, []
    candidates: list[tuple[float, int, int]] = []
    for group in groups:
        valid = group[
            np.isfinite(score[group]) & np.isfinite(volatility[group]) & (volatility[group] > 1e-12)
        ]
        if valid.size < 2:
            continue
        ordered = valid[np.argsort(score[valid], kind="stable")]
        lows = ordered
        highs = ordered[::-1]
        for long_index, short_index in zip(highs, lows, strict=True):
            if long_index == short_index:
                continue
            spread = float(score[long_index] - score[short_index])
            if np.isfinite(spread) and spread > 0.0:
                candidates.append((spread, int(long_index), int(short_index)))
    selected: list[tuple[float, int, int]] = []
    used: set[int] = set()
    for candidate in sorted(candidates, key=lambda item: (-item[0], item[1], item[2])):
        _, long_index, short_index = candidate
        if long_index in used or short_index in used:
            continue
        selected.append(candidate)
        used.update((long_index, short_index))
        if len(selected) >= max_pairs:
            break
    if not selected:
        return weights, []
    pair_gross = min(float(gross_leverage), MAX_GROSS_LEVERAGE) / len(selected)
    legs: list[int] = []
    for _, long_index, short_index in selected:
        long_inv = 1.0 / float(volatility[long_index])
        short_inv = 1.0 / float(volatility[short_index])
        scale = pair_gross / (long_inv + short_inv)
        weights[long_index] = long_inv * scale
        weights[short_index] = -short_inv * scale
        legs.extend((long_index, short_index))
    gross = float(np.abs(weights).sum())
    if gross > MAX_GROSS_LEVERAGE + 1e-12:
        weights *= MAX_GROSS_LEVERAGE / gross
    return weights, legs


def _simulate_path(
    returns: pd.DataFrame,
    exchange_map: dict[str, str],
    template: AlphaTemplate,
) -> tuple[pd.Series, pd.Series, list[dict]]:
    data = returns.astype(float).replace([np.inf, -np.inf], np.nan)
    data.columns = [str(column) for column in data.columns]
    columns = list(data.columns)
    values = np.nan_to_num(data.to_numpy(float), nan=0.0, posinf=0.0, neginf=0.0)
    score_values = signal_scores(data, template).to_numpy(float)
    volatility_values = (
        data.rolling(template.vol_window, min_periods=template.vol_window).std().to_numpy(float)
    )
    groups = _exchange_groups(columns, exchange_map)
    weights = np.zeros(len(columns), dtype=float)
    gross_pnl = np.zeros(len(data), dtype=float)
    turnover = np.zeros(len(data), dtype=float)
    audit: list[dict] = []
    step = max(int(template.rebalance), 1)
    legs: list[int] = []
    for index in range(1, len(data)):
        if index % step == 0:
            next_weights, legs = _weights_from_arrays(
                score_values[index - 1],
                volatility_values[index - 1],
                groups,
                max_pairs=template.max_pairs,
                gross_leverage=template.gross_leverage,
            )
            turnover[index] = float(np.abs(next_weights - weights).sum())
            weights = next_weights
        gross_pnl[index] = float(weights @ values[index])
        audit.append(
            {
                "date": data.index[index],
                "gross": float(np.abs(weights).sum()),
                "turnover": float(turnover[index]),
                "legs": [columns[leg] for leg in legs],
            }
        )
    return (
        pd.Series(gross_pnl, index=data.index),
        pd.Series(turnover, index=data.index),
        audit,
    )


def _apply_cost(gross_pnl: pd.Series, turnover: pd.Series, cost_bps: float) -> pd.Series:
    return gross_pnl - turnover * float(cost_bps) / 10000.0


def simulate_template(
    returns: pd.DataFrame,
    exchange_map: dict[str, str],
    template: AlphaTemplate,
    *,
    cost_bps: float,
) -> tuple[pd.Series, list[dict]]:
    gross_pnl, turnover, audit = _simulate_path(returns, exchange_map, template)
    return _apply_cost(gross_pnl, turnover, cost_bps), audit


def _metrics_array(values: np.ndarray) -> dict:
    raw = np.nan_to_num(np.asarray(values, dtype=float), nan=0.0, posinf=0.0, neginf=0.0)
    if raw.size == 0:
        return {
            "days": 0,
            "active_days": 0,
            "total_return": 0.0,
            "annualized_return": 0.0,
            "annualized_volatility": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
        }
    equity = np.cumprod(1.0 + raw)
    total = float(equity[-1] - 1.0)
    annualized = (1.0 + total) ** (252.0 / raw.size) - 1.0 if total > -1.0 else -1.0
    std = float(raw.std(ddof=1)) if raw.size > 1 else 0.0
    sharpe = float(raw.mean() / std * np.sqrt(252.0)) if std > 1e-12 else 0.0
    peak = np.maximum.accumulate(equity)
    drawdown = equity / peak - 1.0
    return {
        "days": int(raw.size),
        "active_days": int((np.abs(raw) > 1e-15).sum()),
        "total_return": total,
        "annualized_return": float(annualized),
        "annualized_volatility": float(std * np.sqrt(252.0)),
        "sharpe": sharpe,
        "max_drawdown": float(drawdown.min()),
    }


def _metrics(series: pd.Series) -> dict:
    return _metrics_array(series.to_numpy(float))


def _window_metrics(series: pd.Series, name: str) -> dict:
    start, end = WINDOWS[name]
    return _metrics(series.loc[pd.Timestamp(start) : pd.Timestamp(end)])


def choose_templates(streams: dict[str, pd.Series], *, start, end, count: int) -> list[str]:
    if count <= 0:
        return []
    ranked: list[tuple[float, str]] = []
    for name, series in streams.items():
        item = _metrics(series.loc[pd.Timestamp(start) : pd.Timestamp(end)])
        if item["annualized_return"] <= 0.0:
            continue
        score = (
            4.0 * item["annualized_return"] + 0.5 * item["sharpe"] - 2.0 * abs(item["max_drawdown"])
        )
        ranked.append((score, name))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [name for _, name in ranked[:count]]


def dynamic_rotate(
    streams: dict[str, pd.Series],
    *,
    meta_lookback: int,
    rebalance: int,
    count: int,
    switch_cost_bps: float,
) -> tuple[pd.Series, list[dict]]:
    if not streams:
        return pd.Series(dtype=float), []
    frame = pd.DataFrame(streams).sort_index().fillna(0.0)
    names = [str(name) for name in frame.columns]
    values = frame.to_numpy(float)
    output = np.zeros(len(frame), dtype=float)
    selected: list[int] = []
    audit: list[dict] = []
    step = max(int(rebalance), 1)
    for index in range(len(frame)):
        if index >= meta_lookback and (not selected or index % step == 0):
            history = values[index - meta_lookback : index]
            ranked: list[tuple[float, int, str]] = []
            for column_index, name in enumerate(names):
                item = _metrics_array(history[:, column_index])
                if item["annualized_return"] <= 0.0 or item["max_drawdown"] <= -0.20:
                    continue
                score = (
                    4.0 * item["annualized_return"]
                    + 0.5 * item["sharpe"]
                    - 2.0 * abs(item["max_drawdown"])
                )
                ranked.append((score, column_index, name))
            ranked.sort(key=lambda item: (-item[0], item[2]))
            next_selected = [column_index for _, column_index, _ in ranked[:count]]
            if next_selected != selected:
                churn = len(set(selected).symmetric_difference(next_selected)) / max(count, 1)
                output[index] -= churn * switch_cost_bps / 10000.0
            selected = next_selected
        if selected:
            output[index] += float(values[index, selected].mean())
        audit.append(
            {
                "date": frame.index[index],
                "selected": [names[column_index] for column_index in selected],
            }
        )
    return pd.Series(output, index=frame.index), audit


def build_panel(raw: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, str], dict]:
    frame = raw.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame["product"] = frame["product"].astype(str).str.upper()
    frame = frame.dropna(subset=["date", "product", "close"])
    frame = frame[frame["close"] > 0]
    frame.drop_duplicates(["date", "product"], keep="last", inplace=True)
    close = frame.pivot(index="date", columns="product", values="close").sort_index().astype(float)
    returns = close.pct_change(fill_method=None)
    outlier = returns.abs() > MAX_ABS_DAILY_RETURN
    removed = int(outlier.sum().sum())
    returns = returns.mask(outlier)
    exchanges = {
        str(product): PRODUCT_EXCHANGE[str(product)]
        for product in returns.columns
        if str(product) in PRODUCT_EXCHANGE
    }
    usable = [str(column) for column in returns.columns if str(column) in exchanges]
    returns = returns[usable]
    coverage = {
        "products": int(len(usable)),
        "trading_days": int(len(returns.index)),
        "first": returns.index.min().date().isoformat() if len(returns) else None,
        "last": returns.index.max().date().isoformat() if len(returns) else None,
        "return_outliers_removed": removed,
    }
    return returns, exchanges, coverage


def _selection_score(row: dict) -> float:
    train = row["base"]["train"]
    validation = row["base"]["validation"]
    stress = row["stress"]["selection_full"]
    worst_drawdown = max(abs(train["max_drawdown"]), abs(validation["max_drawdown"]))
    return (
        2.0 * train["annualized_return"]
        + 3.0 * validation["annualized_return"]
        + 0.5 * min(train["sharpe"], validation["sharpe"])
        + 0.5 * stress["annualized_return"]
        - 2.0 * worst_drawdown
    )


def evaluate(raw: pd.DataFrame) -> dict:
    returns, exchanges, coverage = build_panel(raw)
    if coverage["products"] < 2:
        raise ValueError("return-target research needs at least two known products")

    templates = primary_templates()
    template_lookup: dict[str, AlphaTemplate] = {}
    path_cache: dict[str, tuple[pd.Series, pd.Series, list[dict]]] = {}
    base_streams: dict[str, pd.Series] = {}
    stress_streams: dict[str, pd.Series] = {}
    rows: list[dict] = []
    for template in templates:
        name = template_id(template)
        template_lookup[name] = template
        gross_pnl, turnover, audit = _simulate_path(returns, exchanges, template)
        path_cache[name] = (gross_pnl, turnover, audit)
        base = _apply_cost(gross_pnl, turnover, BASE_COST_BPS)
        stress = _apply_cost(gross_pnl, turnover, STRESS_COST_BPS)
        base_streams[name] = base
        stress_streams[name] = stress
        total_turnover = float(turnover.sum())
        row = {
            "template_id": name,
            "template": asdict(template),
            "annual_turnover": float(total_turnover * 252.0 / max(len(base), 1)),
            "base": {key: _window_metrics(base, key) for key in WINDOWS},
            "stress": {key: _window_metrics(stress, key) for key in WINDOWS},
        }
        row["selection_score"] = _selection_score(row)
        rows.append(row)

    rows.sort(key=lambda row: (-row["selection_score"], row["template_id"]))
    pool_ids = [row["template_id"] for row in rows[:META_POOL_SIZE]]
    pool_base = {name: base_streams[name] for name in pool_ids}
    pool_stress = {name: stress_streams[name] for name in pool_ids}

    selection_start, selection_end = WINDOWS["selection_full"]
    meta_candidates: list[dict] = []
    for lookback in (20, 60, 120):
        for rebalance in (1, 5):
            for count in (1, 2, 3):
                series, _ = dynamic_rotate(
                    pool_base,
                    meta_lookback=lookback,
                    rebalance=rebalance,
                    count=count,
                    switch_cost_bps=BASE_COST_BPS,
                )
                item = _metrics(
                    series.loc[pd.Timestamp(selection_start) : pd.Timestamp(selection_end)]
                )
                score = (
                    4.0 * item["annualized_return"]
                    + 0.5 * item["sharpe"]
                    - 2.0 * abs(item["max_drawdown"])
                )
                meta_candidates.append(
                    {
                        "meta_lookback": lookback,
                        "rebalance": rebalance,
                        "count": count,
                        "score": score,
                        "selection_metrics": item,
                    }
                )
    meta_candidates.sort(
        key=lambda row: (-row["score"], row["meta_lookback"], row["rebalance"], row["count"])
    )
    chosen = meta_candidates[0]

    chosen_base, base_audit = dynamic_rotate(
        pool_base,
        meta_lookback=chosen["meta_lookback"],
        rebalance=chosen["rebalance"],
        count=chosen["count"],
        switch_cost_bps=BASE_COST_BPS,
    )
    chosen_stress, _ = dynamic_rotate(
        pool_stress,
        meta_lookback=chosen["meta_lookback"],
        rebalance=chosen["rebalance"],
        count=chosen["count"],
        switch_cost_bps=STRESS_COST_BPS,
    )
    extreme_streams = {
        name: _apply_cost(path_cache[name][0], path_cache[name][1], EXTREME_COST_BPS)
        for name in pool_ids
    }
    chosen_extreme, _ = dynamic_rotate(
        extreme_streams,
        meta_lookback=chosen["meta_lookback"],
        rebalance=chosen["rebalance"],
        count=chosen["count"],
        switch_cost_bps=EXTREME_COST_BPS,
    )

    base_windows = {name: _window_metrics(chosen_base, name) for name in WINDOWS}
    stress_windows = {name: _window_metrics(chosen_stress, name) for name in WINDOWS}
    extreme_windows = {name: _window_metrics(chosen_extreme, name) for name in WINDOWS}
    effective_gross = max((template_lookup[name].gross_leverage for name in pool_ids), default=0.0)
    full_recent = base_windows["full_recent"]
    stress_recent = stress_windows["full_recent"]
    target_met = bool(
        full_recent["annualized_return"] >= 1.0
        and full_recent["max_drawdown"] > -0.30
        and stress_recent["annualized_return"] > 0.0
        and full_recent["active_days"] >= 50
        and effective_gross <= MAX_GROSS_LEVERAGE
    )

    family_contribution: dict[str, dict] = {}
    for name in pool_ids:
        family = template_lookup[name].family
        item = family_contribution.setdefault(
            family, {"templates": 0, "best_full_recent_annualized_return": -999.0}
        )
        item["templates"] += 1
        item["best_full_recent_annualized_return"] = max(
            item["best_full_recent_annualized_return"],
            _window_metrics(base_streams[name], "full_recent")["annualized_return"],
        )
    selected_counts: dict[str, int] = {}
    for item in base_audit:
        for name in item["selected"]:
            selected_counts[name] = selected_counts.get(name, 0) + 1

    return {
        "source": "AKShare/Sina continuous daily Chinese commodity futures",
        "role": "L3 return-target discovery only; production requires specific contracts",
        "historical_l1_available": False,
        "continuous_roll_series": True,
        "pristine_final_oos": False,
        "windows": WINDOWS,
        "coverage": coverage,
        "base_cost_bps_one_way": BASE_COST_BPS,
        "stress_cost_bps_one_way": STRESS_COST_BPS,
        "extreme_cost_bps_one_way": EXTREME_COST_BPS,
        "template_count": len(templates),
        "template_results": rows,
        "selection": {
            "pool_ids": pool_ids,
            "meta_lookback": chosen["meta_lookback"],
            "rebalance": chosen["rebalance"],
            "count": chosen["count"],
            "portfolio_gross_multiplier": 1.0,
            "effective_gross_leverage": float(effective_gross),
            "selection_score": float(chosen["score"]),
            "selected_day_counts": selected_counts,
        },
        "base": base_windows,
        "stress": stress_windows,
        "extreme": extreme_windows,
        "family_contribution": family_contribution,
        "target": {
            "annualized_return": 1.0,
            "max_drawdown": -0.30,
            "gross_leverage_cap": MAX_GROSS_LEVERAGE,
            "minimum_active_days": 50,
            "stress_must_be_positive": True,
            "target_met": target_met,
        },
    }


def _write_template_csv(report: dict, path: Path) -> None:
    records: list[dict] = []
    for row in report["template_results"]:
        record = {
            **row["template"],
            "template_id": row["template_id"],
            "selection_score": row["selection_score"],
            "annual_turnover": row["annual_turnover"],
        }
        for scenario in ("base", "stress"):
            for window in ("train", "validation", "oos", "full_recent"):
                item = row[scenario][window]
                record[f"{scenario}_{window}_annualized_return"] = item["annualized_return"]
                record[f"{scenario}_{window}_max_drawdown"] = item["max_drawdown"]
        records.append(record)
    pd.DataFrame(records).to_csv(path, index=False)


def main() -> None:
    source = Path("runtime/broad_daily_universe.csv")
    if not source.exists():
        raise SystemExit("broad daily universe missing; run fetch_broad_daily_universe.py")
    report = evaluate(pd.read_csv(source))
    output = Path("runtime")
    output.mkdir(parents=True, exist_ok=True)
    (output / "return_target_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    _write_template_csv(report, output / "return_target_template_results.csv")
    print(
        json.dumps(
            {
                "coverage": report["coverage"],
                "selection": report["selection"],
                "base_full_recent": report["base"]["full_recent"],
                "stress_full_recent": report["stress"]["full_recent"],
                "extreme_full_recent": report["extreme"]["full_recent"],
                "base_oos": report["base"]["oos"],
                "target": report["target"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
