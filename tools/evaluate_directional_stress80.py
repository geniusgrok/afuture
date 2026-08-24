"""Fixed-input cheap L3A screen for causal Product x Alpha Stress-80 research."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from afuture.directional_opportunity_ledger import (
    ALLOWED_HORIZONS,
    build_opportunity_ledger,
)
from afuture.directional_stress80_research import (
    ALLOWED_FAMILIES,
    combine_family_edge_signals,
    family_signal_history,
)

BASE_COST_BPS = 5.0
STRESS_COST_BPS = 15.0
MAX_GROSS_LEVERAGE = 2.0
BASELINE_STRESS_ANNUALIZED = 0.289559
BASELINE_STRESS_NET_ALPHA_PER_TURNOVER_BPS = 12.2556
WINDOWS = {
    "prior1": ("2022-08-22", "2023-08-20"),
    "prior2": ("2023-08-21", "2024-08-20"),
    "train": ("2024-08-21", "2025-08-20"),
    "validation": ("2025-08-21", "2026-02-20"),
    "selection_full": ("2024-08-21", "2026-02-20"),
    "oos": ("2026-02-21", "2026-08-20"),
    "full_recent": ("2024-08-21", "2026-08-20"),
}


def _drawdown_magnitude(value: float) -> float:
    value = float(value)
    return abs(min(value, 0.0))


def l3a_gate(report: Mapping) -> dict:
    """Apply exactly the predeclared cheap-screen advancement gate."""
    stress = report["stress"]
    economics = report["economics"]["stress"]["full_recent"]
    risk = report["risk"]
    checks = {
        "full_recent_stress_beats_production_baseline": (
            float(stress["full_recent"]["annualized_return"])
            > BASELINE_STRESS_ANNUALIZED
        ),
        "validation_stress_positive": (
            float(stress["validation"]["annualized_return"]) > 0.0
        ),
        "oos_stress_positive": float(stress["oos"]["annualized_return"]) > 0.0,
        "validation_dd_within_30pct": (
            _drawdown_magnitude(stress["validation"]["max_drawdown"]) <= 0.30
        ),
        "oos_dd_within_30pct": (
            _drawdown_magnitude(stress["oos"]["max_drawdown"]) <= 0.30
        ),
        "full_recent_net_alpha_per_turnover_improves": (
            float(economics["net_alpha_per_turnover_bps"])
            > BASELINE_STRESS_NET_ALPHA_PER_TURNOVER_BPS
        ),
        "validation_no_halt_or_hard_violation": (
            not bool(risk["validation"].get("permanent_halt", False))
            and not list(risk["validation"].get("violations", []))
        ),
        "oos_no_halt_or_hard_violation": (
            not bool(risk["oos"].get("permanent_halt", False))
            and not list(risk["oos"].get("violations", []))
        ),
    }
    reasons = [name for name, passed in checks.items() if not passed]
    return {"passed": not reasons, "checks": checks, "reasons": reasons}


def build_forward_label_panels(
    close_returns: pd.DataFrame,
    *,
    horizons: Sequence[int] = ALLOWED_HORIZONS,
) -> tuple[dict[int, pd.DataFrame], dict[int, pd.DataFrame]]:
    """Build D -> D+1..D+h roll-safe compounded labels and completion dates."""
    requested = tuple(int(item) for item in horizons)
    if not requested or any(item not in ALLOWED_HORIZONS for item in requested):
        raise ValueError("forward label horizon must be one of 5, 10, 20")
    frame = close_returns.copy().sort_index()
    frame.index = pd.to_datetime(frame.index, errors="coerce").normalize()
    if frame.index.hasnans or frame.index.has_duplicates:
        raise ValueError("close return index must be unique valid trading dates")
    frame = frame.apply(pd.to_numeric, errors="coerce")
    values = frame.to_numpy(float)
    n_days, n_products = values.shape
    returns: dict[int, pd.DataFrame] = {}
    availability: dict[int, pd.DataFrame] = {}
    for horizon in requested:
        out = np.full((n_days, n_products), np.nan, dtype=float)
        available = np.full((n_days, n_products), np.datetime64("NaT"), dtype="datetime64[ns]")
        for position in range(max(0, n_days - horizon)):
            future = values[position + 1 : position + horizon + 1]
            valid = np.isfinite(future).all(axis=0)
            if not bool(valid.any()):
                continue
            compounded = np.prod(1.0 + future[:, valid], axis=0) - 1.0
            out[position, valid] = compounded
            available[position, valid] = frame.index[position + horizon].to_datetime64()
        returns[horizon] = pd.DataFrame(out, index=frame.index, columns=frame.columns)
        availability[horizon] = pd.DataFrame(
            available, index=frame.index, columns=frame.columns
        )
    return returns, availability


class _StreamingEdgeBook:
    """O(1)-state causal estimator matching directional_causal_edge shrinkage."""

    def __init__(self, ledger: pd.DataFrame) -> None:
        frame = ledger.copy()
        frame["label_available_date"] = pd.to_datetime(
            frame["label_available_date"], errors="coerce"
        ).dt.normalize()
        frame["forward_horizon_sessions"] = pd.to_numeric(
            frame["forward_horizon_sessions"], errors="coerce"
        )
        frame["future_specific_contract_gross_return"] = pd.to_numeric(
            frame["future_specific_contract_gross_return"], errors="coerce"
        )
        frame["family"] = frame["family"].astype(str).str.lower()
        frame["product"] = frame["product"].astype(str).str.upper()
        frame = frame[
            frame["label_available_date"].notna()
            & frame["forward_horizon_sessions"].isin(ALLOWED_HORIZONS)
            & np.isfinite(frame["future_specific_contract_gross_return"].to_numpy(float))
        ].sort_values("label_available_date", kind="stable")
        self._availability = frame["label_available_date"].to_numpy(dtype="datetime64[ns]")
        self._horizons = frame["forward_horizon_sessions"].to_numpy(dtype=int)
        self._families = frame["family"].to_numpy(dtype=object)
        self._products = frame["product"].to_numpy(dtype=object)
        self._values = frame["future_specific_contract_gross_return"].to_numpy(float)
        self._position = 0
        self._global_count: dict[int, int] = {}
        self._global_sum: dict[int, float] = {}
        self._family_count: dict[tuple[int, str], int] = {}
        self._family_sum: dict[tuple[int, str], float] = {}
        self._cell_count: dict[tuple[int, str, str], int] = {}
        self._cell_sum: dict[tuple[int, str, str], float] = {}
        self._prior_support: dict[int, float] = {}

    def advance(self, decision_date) -> None:
        cutoff = np.datetime64(pd.Timestamp(decision_date).normalize(), "ns")
        changed: set[int] = set()
        while (
            self._position < len(self._values)
            and self._availability[self._position] < cutoff
        ):
            horizon = int(self._horizons[self._position])
            family = str(self._families[self._position])
            product = str(self._products[self._position])
            value = float(self._values[self._position])
            self._global_count[horizon] = self._global_count.get(horizon, 0) + 1
            self._global_sum[horizon] = self._global_sum.get(horizon, 0.0) + value
            family_key = (horizon, family)
            self._family_count[family_key] = self._family_count.get(family_key, 0) + 1
            self._family_sum[family_key] = self._family_sum.get(family_key, 0.0) + value
            cell_key = (horizon, family, product)
            self._cell_count[cell_key] = self._cell_count.get(cell_key, 0) + 1
            self._cell_sum[cell_key] = self._cell_sum.get(cell_key, 0.0) + value
            changed.add(horizon)
            self._position += 1
        for horizon in changed:
            supports = [
                count
                for (item_horizon, _family, _product), count in self._cell_count.items()
                if item_horizon == horizon and count > 0
            ]
            self._prior_support[horizon] = (
                float(np.median(supports)) if supports else 0.0
            )

    def estimate(self, *, product: str, family: str, horizon: int) -> float:
        horizon = int(horizon)
        product = str(product).upper()
        family = str(family).lower()
        global_support = self._global_count.get(horizon, 0)
        if global_support <= 0:
            return float("nan")
        global_mean = self._global_sum[horizon] / float(global_support)
        prior_support = self._prior_support.get(horizon, 0.0)

        family_key = (horizon, family)
        family_support = self._family_count.get(family_key, 0)
        family_mean = (
            self._family_sum[family_key] / float(family_support)
            if family_support
            else global_mean
        )
        family_weight = (
            float(family_support) / (float(family_support) + prior_support)
            if family_support > 0 and prior_support > 0.0
            else (1.0 if family_support > 0 else 0.0)
        )
        family_prior = (
            family_weight * family_mean + (1.0 - family_weight) * global_mean
        )

        cell_key = (horizon, family, product)
        support = self._cell_count.get(cell_key, 0)
        cell_mean = (
            self._cell_sum[cell_key] / float(support) if support else family_prior
        )
        product_weight = (
            float(support) / (float(support) + prior_support)
            if support > 0 and prior_support > 0.0
            else (1.0 if support > 0 else 0.0)
        )
        return float(
            product_weight * cell_mean + (1.0 - product_weight) * family_prior
        )


def _continuous_open_close(
    raw: pd.DataFrame,
    *,
    products: Sequence[str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    frame = raw.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    frame["product"] = frame["product"].astype(str).str.upper()
    for column in ("open", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["date", "product", "open", "close"])
    frame = frame[(frame["open"] > 0.0) & (frame["close"] > 0.0)]
    frame = frame.drop_duplicates(["date", "product"], keep="last")
    ordered = sorted({str(item).upper() for item in products})
    available = set(frame["product"].unique())
    missing = sorted(set(ordered) - available)
    if missing:
        raise ValueError(f"continuous Stress80 feed missing products: {missing}")
    opens = frame.pivot(index="date", columns="product", values="open").sort_index()
    closes = frame.pivot(index="date", columns="product", values="close").sort_index()
    return opens.reindex(columns=ordered), closes.reindex(columns=ordered)


def build_candidate_weight_history(
    *,
    family_paths: Mapping[str, pd.DataFrame],
    ledger: pd.DataFrame,
    index: pd.DatetimeIndex,
    products: Sequence[str],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Build causal target weights and per-session Product edge for each execution day."""
    ordered = sorted({str(item).upper() for item in products})
    days = pd.DatetimeIndex(pd.to_datetime(index)).normalize()
    paths = {
        family: family_paths[family].reindex(index=days, columns=ordered)
        for family in ALLOWED_FAMILIES
    }
    weights = pd.DataFrame(0.0, index=days, columns=ordered)
    expected = pd.DataFrame(np.nan, index=days, columns=ordered)
    family_score_mass = pd.DataFrame(0.0, index=days, columns=ALLOWED_FAMILIES)
    book = _StreamingEdgeBook(ledger)

    for day in days:
        book.advance(day)
        signals: dict[str, dict[str, float]] = {}
        edges: dict[tuple[str, str], float] = {}
        for family in ALLOWED_FAMILIES:
            row = paths[family].loc[day]
            family_signals: dict[str, float] = {}
            for product, raw_signal in row.items():
                signal = float(raw_signal) if pd.notna(raw_signal) else float("nan")
                if not np.isfinite(signal) or abs(signal) <= 1e-15:
                    continue
                family_signals[str(product)] = signal
                horizon_edges: list[float] = []
                for horizon in ALLOWED_HORIZONS:
                    value = book.estimate(
                        product=str(product), family=family, horizon=horizon
                    )
                    if np.isfinite(value):
                        horizon_edges.append(value / float(horizon))
                composite = (
                    float(np.mean(horizon_edges)) if horizon_edges else float("nan")
                )
                edges[(str(product), family)] = composite
                if np.isfinite(composite) and composite > 0.0:
                    family_score_mass.at[day, family] += abs(signal * composite)
            signals[family] = family_signals
        day_weights, day_edges = combine_family_edge_signals(
            family_signals=signals,
            family_edges=edges,
        )
        for product, value in day_weights.items():
            weights.at[day, product] = float(value)
        for product, value in day_edges.items():
            expected.at[day, product] = float(value)

    gross = weights.abs().sum(axis=1)
    if bool((gross > MAX_GROSS_LEVERAGE + 1e-10).any()):
        raise AssertionError("Stress80 L3A candidate exceeded 2x gross")
    return weights, expected, family_score_mass


def _window_slice(frame: pd.Series | pd.DataFrame, name: str):
    start, end = WINDOWS[name]
    return frame.loc[pd.Timestamp(start) : pd.Timestamp(end)]


def _metrics(series: pd.Series) -> dict:
    values = np.nan_to_num(series.to_numpy(float), nan=0.0, posinf=0.0, neginf=0.0)
    if values.size == 0:
        return {
            "days": 0,
            "active_days": 0,
            "total_return": 0.0,
            "annualized_return": 0.0,
            "annualized_volatility": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
        }
    equity = np.cumprod(1.0 + values)
    total = float(equity[-1] - 1.0)
    annualized = (
        (1.0 + total) ** (252.0 / values.size) - 1.0 if total > -1.0 else -1.0
    )
    std = float(values.std(ddof=1)) if values.size > 1 else 0.0
    sharpe = float(values.mean() / std * np.sqrt(252.0)) if std > 1e-12 else 0.0
    peak = np.maximum.accumulate(equity)
    drawdown = equity / peak - 1.0
    return {
        "days": int(values.size),
        "active_days": int((np.abs(values) > 1e-15).sum()),
        "total_return": total,
        "annualized_return": float(annualized),
        "annualized_volatility": float(std * np.sqrt(252.0)),
        "sharpe": sharpe,
        "max_drawdown": float(drawdown.min()),
    }


def _window_metrics(series: pd.Series) -> dict[str, dict]:
    return {name: _metrics(_window_slice(series, name)) for name in WINDOWS}


def _window_economics(
    gross: pd.Series,
    turnover: pd.Series,
    *,
    cost_bps: float,
) -> dict[str, dict]:
    result: dict[str, dict] = {}
    cost_rate = float(cost_bps) / 10000.0
    for name in WINDOWS:
        gross_value = float(_window_slice(gross, name).sum())
        turnover_value = float(_window_slice(turnover, name).sum())
        cost = turnover_value * cost_rate
        net = gross_value - cost
        result[name] = {
            "gross_alpha_return_sum": gross_value,
            "turnover_ratio_sum": turnover_value,
            "transaction_cost_return_sum": cost,
            "net_alpha_return_sum": net,
            "net_alpha_per_turnover_bps": (
                net / turnover_value * 10000.0 if turnover_value > 0.0 else 0.0
            ),
        }
    return result


def _concentration(values: pd.Series) -> dict:
    series = values.astype(float).replace([np.inf, -np.inf], np.nan).dropna()
    absolute = series.abs()
    denominator = float(absolute.sum())
    if denominator <= 0.0:
        return {"largest_abs_share": 0.0, "top3_abs_share": 0.0, "leaders": []}
    ordered = absolute.sort_values(ascending=False)
    leaders = [
        {"name": str(name), "value": float(series[name]), "abs_share": float(value / denominator)}
        for name, value in ordered.head(5).items()
    ]
    return {
        "largest_abs_share": float(ordered.iloc[0] / denominator),
        "top3_abs_share": float(ordered.head(3).sum() / denominator),
        "leaders": leaders,
    }


def evaluate_l3a(
    specific_raw: pd.DataFrame,
    continuous_raw: pd.DataFrame,
) -> dict:
    import evaluate_return_target_specific as specific

    close_ret, gap_ret, intraday_ret, _selections, quality = (
        specific.build_roll_safe_execution_returns(specific_raw)
    )
    products = tuple(str(item).upper() for item in close_ret.columns)
    opens, closes = _continuous_open_close(continuous_raw, products=products)
    family_paths_raw = family_signal_history(opens, closes, products=products)
    family_paths = {
        family: frame.reindex(index=close_ret.index, columns=close_ret.columns)
        for family, frame in family_paths_raw.items()
    }

    forward_returns, availability = build_forward_label_panels(close_ret)
    # Row D contains the family target that will be executable on the next observed
    # session.  This preserves D-completed information -> D+1 target causality.
    decision_signals = {
        family: frame.shift(-1) for family, frame in family_paths.items()
    }
    ledger = build_opportunity_ledger(
        signals=decision_signals,
        forward_returns=forward_returns,
        label_available_dates=availability,
        horizons=ALLOWED_HORIZONS,
    )
    weights, expected_edges, family_score_mass = build_candidate_weight_history(
        family_paths=family_paths,
        ledger=ledger,
        index=close_ret.index,
        products=products,
    )

    aligned_weights = weights.reindex(
        index=gap_ret.index, columns=gap_ret.columns, fill_value=0.0
    ).fillna(0.0)
    old_weights = aligned_weights.shift(1).fillna(0.0)
    gaps = gap_ret.reindex_like(aligned_weights).fillna(0.0)
    intraday = intraday_ret.reindex_like(aligned_weights).fillna(0.0)
    gross_by_product = old_weights * gaps + aligned_weights * intraday
    gross = gross_by_product.sum(axis=1)
    turnover_by_product = aligned_weights.diff().abs()
    if len(turnover_by_product):
        turnover_by_product.iloc[0] = aligned_weights.iloc[0].abs()
    turnover = turnover_by_product.sum(axis=1)

    base_net = gross - turnover * BASE_COST_BPS / 10000.0
    stress_net = gross - turnover * STRESS_COST_BPS / 10000.0
    base_metrics = _window_metrics(base_net)
    stress_metrics = _window_metrics(stress_net)
    economics = {
        "base": _window_economics(gross, turnover, cost_bps=BASE_COST_BPS),
        "stress": _window_economics(gross, turnover, cost_bps=STRESS_COST_BPS),
    }

    risk: dict[str, dict] = {}
    gross_path = aligned_weights.abs().sum(axis=1)
    for name in WINDOWS:
        window_gross = _window_slice(gross_path, name)
        violations: list[str] = []
        if len(window_gross) and float(window_gross.max()) > 2.0 + 1e-10:
            violations.append("gross cap")
        risk[name] = {
            "permanent_halt": False,
            "violations": violations,
            "production_halt_applicable": False,
            "max_target_gross": float(window_gross.max()) if len(window_gross) else 0.0,
        }

    full_start, full_end = WINDOWS["full_recent"]
    product_net = (
        gross_by_product
        - turnover_by_product * STRESS_COST_BPS / 10000.0
    ).loc[pd.Timestamp(full_start) : pd.Timestamp(full_end)].sum(axis=0)
    family_mass = family_score_mass.loc[
        pd.Timestamp(full_start) : pd.Timestamp(full_end)
    ].sum(axis=0)

    report = {
        "role": "cheap causal Product x Alpha Stress80 L3A screen",
        "production_wiring": False,
        "fixed_horizons": list(ALLOWED_HORIZONS),
        "allowed_families": list(ALLOWED_FAMILIES),
        "parameter_search": False,
        "opportunity_ledger": {
            "rows": int(len(ledger)),
            "off_policy_labels": True,
            "strict_visibility_rule": "label_available_date < decision_date",
        },
        "base": base_metrics,
        "stress": stress_metrics,
        "economics": economics,
        "risk": risk,
        "data_quality": quality,
        "concentration": {
            "stress_full_recent_product_net": _concentration(product_net),
            "full_recent_family_edge_score_mass": _concentration(family_mass),
        },
        "_weights": aligned_weights,
        "_expected_product_returns": expected_edges.reindex_like(aligned_weights),
    }
    report["l3a_gate"] = l3a_gate(report)
    return report


def _jsonable(report: Mapping) -> dict:
    return {key: value for key, value in report.items() if not str(key).startswith("_")}


def main() -> None:
    runtime = Path("runtime")
    specific_path = runtime / "return_target_specific_contracts.csv"
    continuous_path = runtime / "broad_daily_universe.csv"
    missing = [str(path) for path in (specific_path, continuous_path) if not path.exists()]
    if missing:
        raise SystemExit(f"Stress80 L3A inputs missing: {missing}")
    report = evaluate_l3a(
        pd.read_csv(specific_path),
        pd.read_csv(continuous_path),
    )
    runtime.mkdir(parents=True, exist_ok=True)
    report["_weights"].to_csv(runtime / "stress80_l3a_weights.csv")
    report["_expected_product_returns"].to_csv(
        runtime / "stress80_l3a_expected_product_returns.csv"
    )
    output = runtime / "stress80_l3a_report.json"
    output.write_text(
        json.dumps(_jsonable(report), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "l3a_gate": report["l3a_gate"],
                "base_full_recent": report["base"]["full_recent"],
                "stress_full_recent": report["stress"]["full_recent"],
                "stress_validation": report["stress"]["validation"],
                "stress_oos": report["stress"]["oos"],
                "stress_economics": report["economics"]["stress"]["full_recent"],
                "concentration": report["concentration"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
