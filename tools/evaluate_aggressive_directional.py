"""Aggressive low-leverage directional commodity portfolio research.

This research path intentionally prioritizes return. It may fit meta-parameters on the
already-observed 2024-08-21..2026-08-20 recent window, so its result is explicitly
selection-biased and never described as pristine OOS evidence. Daily signal construction
is still causal: information through close t determines weights for t+1, gross exposure
is capped at 2x, and turnover costs are charged explicitly.

Continuous contracts are L3 discovery evidence only. Production promotion requires a
frozen, roll-safe specific-contract L4 and a separate execution integration because the
current afuture production Auto path is same-product calendar-spread only.
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
POOL_SIZES = (16, 24, 32, 48, 64)
META_LOOKBACKS = (5, 10, 15, 20, 30)
META_REBALANCES = (2, 5, 10)
META_COUNTS = (1, 2, 3, 4)


@dataclass(frozen=True)
class DirectionalTemplate:
    family: str
    slow: int
    fast: int
    max_products: int
    rebalance: int
    gross_leverage: float


def directional_templates() -> tuple[DirectionalTemplate, ...]:
    rows: list[DirectionalTemplate] = []
    for family in ("tsmom", "momentum"):
        for slow in (3, 5, 10, 20, 40, 60, 120):
            for max_products in (1, 2, 3, 5):
                for rebalance in (1, 2, 5, 10):
                    rows.append(DirectionalTemplate(family, slow, 0, max_products, rebalance, 2.0))
    for family in ("moving_average", "breakout"):
        for slow in (5, 10, 20, 40, 60, 120):
            for max_products in (1, 2, 3, 5):
                for rebalance in (1, 2, 5, 10):
                    rows.append(DirectionalTemplate(family, slow, 0, max_products, rebalance, 2.0))
    for fast in (1, 3, 5, 10):
        for max_products in (1, 2, 3, 5):
            for rebalance in (1, 2, 5, 10):
                rows.append(DirectionalTemplate("reversal", 0, fast, max_products, rebalance, 2.0))
    for slow, fast in (
        (10, 3),
        (20, 3),
        (20, 5),
        (40, 5),
        (60, 5),
        (60, 10),
        (120, 10),
        (120, 20),
    ):
        for max_products in (1, 2, 3, 5):
            for rebalance in (1, 2, 5, 10):
                rows.append(
                    DirectionalTemplate(
                        "acceleration",
                        slow,
                        fast,
                        max_products,
                        rebalance,
                        2.0,
                    )
                )
    return tuple(rows)


def template_id(template: DirectionalTemplate) -> str:
    return (
        f"{template.family}_s{template.slow}_f{template.fast}"
        f"_k{template.max_products}_r{template.rebalance}"
        f"_g{template.gross_leverage:g}"
    )


def build_panel(raw: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
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
    coverage = {
        "products": int(returns.shape[1]),
        "trading_days": int(returns.shape[0]),
        "first": returns.index.min().date().isoformat() if len(returns) else None,
        "last": returns.index.max().date().isoformat() if len(returns) else None,
        "return_outliers_removed": removed,
    }
    return returns, coverage


def _rolling_log_return(returns: pd.DataFrame, window: int) -> pd.DataFrame:
    return np.log1p(returns.clip(lower=-0.99)).rolling(window, min_periods=window).sum()


def _normalized_price(returns: pd.DataFrame) -> pd.DataFrame:
    return (1.0 + returns.fillna(0.0)).cumprod()


def signal_scores(
    returns: pd.DataFrame,
    template: DirectionalTemplate,
) -> pd.DataFrame:
    vol20 = returns.rolling(20, min_periods=20).std().replace(0.0, np.nan)
    if template.family == "tsmom":
        momentum = _rolling_log_return(returns, template.slow)
        return momentum / (vol20 * np.sqrt(max(template.slow, 1)))
    if template.family == "momentum":
        return _rolling_log_return(returns, template.slow)
    if template.family == "reversal":
        momentum = _rolling_log_return(returns, template.fast)
        return -momentum / (vol20 * np.sqrt(max(template.fast, 1)))
    if template.family == "moving_average":
        price = _normalized_price(returns)
        average = price.rolling(template.slow, min_periods=template.slow).mean()
        deviation = price / average - 1.0
        return deviation / (vol20 * np.sqrt(max(template.slow, 1)))
    if template.family == "breakout":
        price = _normalized_price(returns)
        rolling_low = price.rolling(template.slow, min_periods=template.slow).min()
        rolling_high = price.rolling(template.slow, min_periods=template.slow).max()
        return (price - rolling_low) / (rolling_high - rolling_low).replace(0.0, np.nan) - 0.5
    if template.family == "acceleration":
        slow = _rolling_log_return(returns, template.slow)
        fast = _rolling_log_return(returns, template.fast)
        return (slow - fast) / (vol20 * np.sqrt(max(template.slow, 1)))
    raise ValueError(f"unknown directional family: {template.family}")


def _simulate_arrays(
    returns: pd.DataFrame,
    template: DirectionalTemplate,
    *,
    record_weights: bool,
) -> tuple[pd.Series, pd.Series, list[dict]]:
    data = returns.astype(float).replace([np.inf, -np.inf], np.nan)
    data.columns = [str(column) for column in data.columns]
    columns = list(data.columns)
    values = np.nan_to_num(data.to_numpy(float), nan=0.0, posinf=0.0, neginf=0.0)
    scores = signal_scores(data, template).to_numpy(float)
    weights = np.zeros(len(columns), dtype=float)
    gross_pnl = np.zeros(len(data), dtype=float)
    turnover = np.zeros(len(data), dtype=float)
    audit: list[dict] = []
    step = max(int(template.rebalance), 1)

    for index in range(1, len(data)):
        if index % step == 0:
            lagged = scores[index - 1]
            valid = np.flatnonzero(np.isfinite(lagged) & (np.abs(lagged) > 1e-12))
            next_weights = np.zeros(len(columns), dtype=float)
            if valid.size:
                order = valid[np.argsort(-np.abs(lagged[valid]), kind="stable")]
                selected = order[: min(template.max_products, len(order))]
                if selected.size:
                    gross = min(float(template.gross_leverage), MAX_GROSS_LEVERAGE)
                    each = gross / selected.size
                    next_weights[selected] = np.sign(lagged[selected]) * each
            turnover[index] = float(np.abs(next_weights - weights).sum())
            weights = next_weights
        gross_pnl[index] = float(weights @ values[index])
        if record_weights:
            nonzero = np.flatnonzero(np.abs(weights) > 1e-15)
            audit.append(
                {
                    "date": data.index[index],
                    "gross": float(np.abs(weights).sum()),
                    "weights": {
                        columns[column_index]: float(weights[column_index])
                        for column_index in nonzero
                    },
                }
            )
        else:
            audit.append(
                {
                    "date": data.index[index],
                    "gross": float(np.abs(weights).sum()),
                }
            )
    return (
        pd.Series(gross_pnl, index=data.index),
        pd.Series(turnover, index=data.index),
        audit,
    )


def simulate_path(
    returns: pd.DataFrame,
    template: DirectionalTemplate,
) -> tuple[pd.Series, pd.Series, list[dict]]:
    return _simulate_arrays(returns, template, record_weights=True)


def apply_cost(
    gross_pnl: pd.Series,
    turnover: pd.Series,
    cost_bps: float,
) -> pd.Series:
    return gross_pnl - turnover * float(cost_bps) / 10000.0


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


def _trailing_score_matrix(frame: pd.DataFrame, lookback: int) -> np.ndarray:
    """Return causal trailing return/Sharpe score for every date/template."""
    values = frame.fillna(0.0).to_numpy(float)
    score = np.full_like(values, np.nan, dtype=float)
    if lookback <= 1 or len(frame) <= lookback:
        return score
    for index in range(lookback, len(frame)):
        history = values[index - lookback : index]
        log_total = np.log1p(np.clip(history, -0.999999, None)).sum(axis=0)
        annualized = np.exp(log_total * (252.0 / lookback)) - 1.0
        mean = history.mean(axis=0)
        std = history.std(axis=0, ddof=1)
        sharpe = np.zeros(history.shape[1], dtype=float)
        valid_std = std > 1e-12
        sharpe[valid_std] = mean[valid_std] / std[valid_std] * np.sqrt(252.0)
        row = 4.0 * annualized + 0.5 * sharpe
        row[annualized <= 0.0] = np.nan
        score[index] = row
    return score


def _meta_rotate(
    streams: dict[str, pd.Series],
    *,
    meta_lookback: int,
    rebalance: int,
    count: int,
    switch_cost_bps: float,
) -> tuple[pd.Series, list[dict]]:
    frame = pd.DataFrame(streams).sort_index().fillna(0.0)
    names = [str(name) for name in frame.columns]
    values = frame.to_numpy(float)
    scores = _trailing_score_matrix(frame, meta_lookback)
    output = np.zeros(len(frame), dtype=float)
    selected: list[int] = []
    audit: list[dict] = []
    step = max(int(rebalance), 1)
    for index in range(len(frame)):
        if index >= meta_lookback and (not selected or index % step == 0):
            row = scores[index]
            valid = np.flatnonzero(np.isfinite(row))
            if valid.size:
                order = valid[np.argsort(-row[valid], kind="stable")]
                next_selected = [int(item) for item in order[:count]]
            else:
                next_selected = []
            if next_selected != selected:
                churn = len(set(selected).symmetric_difference(next_selected)) / max(count, 1)
                output[index] -= churn * float(switch_cost_bps) / 10000.0
            selected = next_selected
        if selected:
            output[index] += float(values[index, selected].mean())
        audit.append(
            {
                "date": frame.index[index],
                "selected": [names[item] for item in selected],
            }
        )
    return pd.Series(output, index=frame.index), audit


def _candidate_target(base: dict, stress: dict) -> bool:
    return bool(
        base["annualized_return"] >= 1.0
        and base["max_drawdown"] > -0.30
        and base["active_days"] >= 50
        and stress["annualized_return"] > 0.0
    )


def evaluate(raw: pd.DataFrame) -> dict:
    returns, coverage = build_panel(raw)
    if coverage["products"] < 2:
        raise ValueError("aggressive directional research needs at least two products")

    templates = directional_templates()
    template_lookup: dict[str, DirectionalTemplate] = {}
    path_cache: dict[str, tuple[pd.Series, pd.Series]] = {}
    base_streams: dict[str, pd.Series] = {}
    stress_streams: dict[str, pd.Series] = {}
    results: list[dict] = []

    for template in templates:
        name = template_id(template)
        template_lookup[name] = template
        gross_pnl, turnover, _ = _simulate_arrays(returns, template, record_weights=False)
        path_cache[name] = (gross_pnl, turnover)
        base = apply_cost(gross_pnl, turnover, BASE_COST_BPS)
        stress = apply_cost(gross_pnl, turnover, STRESS_COST_BPS)
        base_streams[name] = base
        stress_streams[name] = stress
        results.append(
            {
                "template_id": name,
                "template": asdict(template),
                "base": {window: _window_metrics(base, window) for window in WINDOWS},
                "stress": {window: _window_metrics(stress, window) for window in WINDOWS},
            }
        )

    # User-authorized return-first fit. This is intentionally in-sample on the
    # already-observed recent window and is labeled as such everywhere.
    results.sort(
        key=lambda row: (
            -row["base"]["full_recent"]["annualized_return"],
            row["template_id"],
        )
    )

    candidates: list[dict] = []
    for pool_size in POOL_SIZES:
        pool_ids = [row["template_id"] for row in results[: min(pool_size, len(results))]]
        pool_base = {name: base_streams[name] for name in pool_ids}
        pool_stress = {name: stress_streams[name] for name in pool_ids}
        for lookback in META_LOOKBACKS:
            for rebalance in META_REBALANCES:
                for count in META_COUNTS:
                    base_series, _ = _meta_rotate(
                        pool_base,
                        meta_lookback=lookback,
                        rebalance=rebalance,
                        count=count,
                        switch_cost_bps=BASE_COST_BPS,
                    )
                    stress_series, _ = _meta_rotate(
                        pool_stress,
                        meta_lookback=lookback,
                        rebalance=rebalance,
                        count=count,
                        switch_cost_bps=STRESS_COST_BPS,
                    )
                    base_recent = _window_metrics(base_series, "full_recent")
                    stress_recent = _window_metrics(stress_series, "full_recent")
                    candidates.append(
                        {
                            "pool_size": pool_size,
                            "meta_lookback": lookback,
                            "rebalance": rebalance,
                            "count": count,
                            "pool_ids": pool_ids,
                            "target_met": _candidate_target(base_recent, stress_recent),
                            "base_full_recent": base_recent,
                            "stress_full_recent": stress_recent,
                        }
                    )

    passing = [item for item in candidates if item["target_met"]]
    if passing:
        chosen = max(
            passing,
            key=lambda item: item["base_full_recent"]["annualized_return"],
        )
    else:
        chosen = max(
            candidates,
            key=lambda item: (
                item["base_full_recent"]["annualized_return"]
                - 2.0
                * max(
                    0.0,
                    abs(item["base_full_recent"]["max_drawdown"]) - 0.30,
                )
                + 0.25 * item["stress_full_recent"]["annualized_return"]
            ),
        )

    pool_ids = chosen["pool_ids"]
    pool_base = {name: base_streams[name] for name in pool_ids}
    pool_stress = {name: stress_streams[name] for name in pool_ids}
    pool_extreme = {
        name: apply_cost(path_cache[name][0], path_cache[name][1], EXTREME_COST_BPS)
        for name in pool_ids
    }
    base_series, meta_audit = _meta_rotate(
        pool_base,
        meta_lookback=chosen["meta_lookback"],
        rebalance=chosen["rebalance"],
        count=chosen["count"],
        switch_cost_bps=BASE_COST_BPS,
    )
    stress_series, _ = _meta_rotate(
        pool_stress,
        meta_lookback=chosen["meta_lookback"],
        rebalance=chosen["rebalance"],
        count=chosen["count"],
        switch_cost_bps=STRESS_COST_BPS,
    )
    extreme_series, _ = _meta_rotate(
        pool_extreme,
        meta_lookback=chosen["meta_lookback"],
        rebalance=chosen["rebalance"],
        count=chosen["count"],
        switch_cost_bps=EXTREME_COST_BPS,
    )

    base_windows = {window: _window_metrics(base_series, window) for window in WINDOWS}
    stress_windows = {window: _window_metrics(stress_series, window) for window in WINDOWS}
    extreme_windows = {window: _window_metrics(extreme_series, window) for window in WINDOWS}

    # Re-simulate only the selected pool with weights retained, then combine the
    # underlying weights according to the causal meta-selection audit.
    weight_audits: dict[str, dict[pd.Timestamp, dict[str, float]]] = {}
    for name in pool_ids:
        _, _, audit = _simulate_arrays(returns, template_lookup[name], record_weights=True)
        weight_audits[name] = {
            pd.Timestamp(item["date"]): dict(item.get("weights", {})) for item in audit
        }
    weight_rows: list[dict] = []
    for item in meta_audit:
        trading_day = pd.Timestamp(item["date"])
        selected_names = item["selected"]
        if not selected_names:
            continue
        aggregate: dict[str, float] = {}
        for name in selected_names:
            for product, weight in weight_audits[name].get(trading_day, {}).items():
                aggregate[product] = aggregate.get(product, 0.0) + float(weight) / len(
                    selected_names
                )
        gross = sum(abs(value) for value in aggregate.values())
        if gross > MAX_GROSS_LEVERAGE + 1e-12:
            scale = MAX_GROSS_LEVERAGE / gross
            aggregate = {product: weight * scale for product, weight in aggregate.items()}
        for product, weight in sorted(aggregate.items()):
            if abs(weight) > 1e-15:
                weight_rows.append(
                    {
                        "date": trading_day,
                        "product": product,
                        "weight": weight,
                    }
                )

    output = Path("runtime")
    output.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(weight_rows).to_csv(output / "aggressive_directional_weights.csv", index=False)
    flat_results: list[dict] = []
    for row in results:
        flat_results.append(
            {
                **row["template"],
                "template_id": row["template_id"],
                "base_full_recent_annualized_return": row["base"]["full_recent"][
                    "annualized_return"
                ],
                "base_full_recent_max_drawdown": row["base"]["full_recent"]["max_drawdown"],
                "stress_full_recent_annualized_return": row["stress"]["full_recent"][
                    "annualized_return"
                ],
            }
        )
    pd.DataFrame(flat_results).to_csv(output / "aggressive_directional_results.csv", index=False)

    target_met = _candidate_target(base_windows["full_recent"], stress_windows["full_recent"])
    selected_day_counts: dict[str, int] = {}
    for item in meta_audit:
        for name in item["selected"]:
            selected_day_counts[name] = selected_day_counts.get(name, 0) + 1

    report = {
        "source": "AKShare/Sina continuous daily Chinese commodity futures",
        "role": "selection-biased L3 aggressive directional target fit; not production evidence",
        "historical_l1_available": False,
        "continuous_roll_series": True,
        "pristine_final_oos": False,
        "coverage": coverage,
        "template_count": len(templates),
        "base_cost_bps_one_way": BASE_COST_BPS,
        "stress_cost_bps_one_way": STRESS_COST_BPS,
        "extreme_cost_bps_one_way": EXTREME_COST_BPS,
        "selection": {
            "selection_bias": "full_recent_target_fit",
            "fit_window": WINDOWS["full_recent"],
            "pool_size": chosen["pool_size"],
            "pool_ids": pool_ids,
            "meta_lookback": chosen["meta_lookback"],
            "rebalance": chosen["rebalance"],
            "count": chosen["count"],
            "effective_gross_leverage": MAX_GROSS_LEVERAGE,
            "selected_day_counts": selected_day_counts,
        },
        "base": base_windows,
        "stress": stress_windows,
        "extreme": extreme_windows,
        "prior_stability": bool(
            base_windows["prior1"]["annualized_return"] > 0.0
            and base_windows["prior2"]["annualized_return"] > 0.0
        ),
        "target": {
            "annualized_return": 1.0,
            "max_drawdown": -0.30,
            "gross_leverage_cap": MAX_GROSS_LEVERAGE,
            "stress_must_be_positive": True,
            "target_met": target_met,
        },
    }
    (output / "aggressive_directional_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return report


def main() -> None:
    source = Path("runtime/broad_daily_universe.csv")
    if not source.exists():
        raise SystemExit("broad daily universe missing; run fetch_broad_daily_universe.py")
    report = evaluate(pd.read_csv(source))
    print(
        json.dumps(
            {
                "coverage": report["coverage"],
                "selection": report["selection"],
                "base_prior1": report["base"]["prior1"],
                "base_prior2": report["base"]["prior2"],
                "base_train": report["base"]["train"],
                "base_validation": report["base"]["validation"],
                "base_oos": report["base"]["oos"],
                "base_full_recent": report["base"]["full_recent"],
                "stress_full_recent": report["stress"]["full_recent"],
                "extreme_full_recent": report["extreme"]["full_recent"],
                "prior_stability": report["prior_stability"],
                "target": report["target"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
