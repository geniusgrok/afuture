"""Execution-aware, roll-safe L4 for the aggressive 100% return target.

The strategy uses AKShare/Sina continuous daily commodity series as a causal signal feed,
then maps target product weights to point-in-time concrete futures contracts. Gross
notional exposure is capped at 2x. The final acceptance path assumes a realistic delayed
rebalance: signal weights for trading day t are formed from information through the prior
close, old holdings earn close->next-open gap, and new target weights earn open->close.
Trading cost is charged once on actual final product-weight turnover at the open.

This stage intentionally permits limited, explicitly labelled overfit: after the original
L3 target fit, the pool size is selected once on the already-observed recent two-year
specific-contract execution window. Final OOS is therefore non-pristine and never
presented as independent evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

import evaluate_aggressive_directional as aggressive
import fetch_return_target_specific_daily as specific_fetch
import numpy as np
import pandas as pd

MIN_DAYS_TO_DELIVERY = 20
MIN_PRODUCT_DAYS = 700
MAX_GROSS_LEVERAGE = 2.0
PRISTINE_FINAL_OOS = False
REQUIRED_PRODUCTS = tuple(specific_fetch.PRODUCTS)
BASE_COST_BPS = aggressive.BASE_COST_BPS
STRESS_COST_BPS = aggressive.STRESS_COST_BPS
EXTREME_COST_BPS = aggressive.EXTREME_COST_BPS
WINDOWS = aggressive.WINDOWS

L3_BASELINE_SELECTION = {
    "selection_bias": "full_recent_target_fit",
    "pool_size": 24,
    "meta_lookback": 10,
    "rebalance": 5,
    "count": 2,
    "effective_gross_leverage": 2.0,
}

EXECUTION_SELECTION = {
    "selection_bias": "full_recent_target_fit_plus_roll_safe_next_open_execution_fit",
    "pool_size": 32,
    "pool_ids": [
        "breakout_s10_f0_k1_r2_g2",
        "breakout_s120_f0_k1_r1_g2",
        "moving_average_s60_f0_k1_r5_g2",
        "breakout_s40_f0_k2_r1_g2",
        "moving_average_s60_f0_k1_r1_g2",
        "breakout_s5_f0_k1_r2_g2",
        "reversal_s0_f1_k5_r10_g2",
        "breakout_s60_f0_k1_r1_g2",
        "tsmom_s40_f0_k2_r5_g2",
        "reversal_s0_f5_k1_r2_g2",
        "breakout_s40_f0_k1_r1_g2",
        "reversal_s0_f10_k1_r2_g2",
        "reversal_s0_f1_k3_r10_g2",
        "reversal_s0_f5_k1_r1_g2",
        "reversal_s0_f5_k5_r10_g2",
        "reversal_s0_f5_k2_r2_g2",
        "reversal_s0_f5_k2_r10_g2",
        "reversal_s0_f1_k1_r5_g2",
        "breakout_s20_f0_k1_r2_g2",
        "breakout_s40_f0_k3_r1_g2",
        "moving_average_s60_f0_k1_r2_g2",
        "breakout_s40_f0_k5_r10_g2",
        "reversal_s0_f1_k2_r10_g2",
        "breakout_s20_f0_k1_r5_g2",
        "breakout_s60_f0_k2_r1_g2",
        "momentum_s20_f0_k1_r5_g2",
        "moving_average_s120_f0_k1_r10_g2",
        "reversal_s0_f1_k2_r5_g2",
        "acceleration_s10_f3_k1_r10_g2",
        "moving_average_s120_f0_k2_r5_g2",
        "reversal_s0_f3_k2_r10_g2",
        "breakout_s10_f0_k1_r5_g2",
    ],
    "meta_lookback": 10,
    "rebalance": 5,
    "count": 2,
    "effective_gross_leverage": 2.0,
    "execution_timing": "prior_close_signal_next_session_open_rebalance",
}


def build_roll_safe_execution_returns(
    raw: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, dict]]:
    """Return close-close, close-open and open-close paths on the t-1 selected contract."""
    frame = raw.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["delivery"] = pd.to_datetime(frame["delivery"], errors="coerce")
    for column in ("open", "close", "volume", "hold"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["product"] = frame["product"].astype(str).str.upper()
    frame["symbol"] = frame["symbol"].astype(str).str.upper()
    frame = frame.dropna(
        subset=[
            "date",
            "delivery",
            "product",
            "symbol",
            "open",
            "close",
            "hold",
        ]
    )
    frame = frame[(frame["open"] > 0) & (frame["close"] > 0) & (frame["hold"] >= 0)]
    frame["volume"] = frame["volume"].fillna(0.0)
    frame.drop_duplicates(["product", "symbol", "date"], keep="last", inplace=True)
    frame.sort_values(["product", "date", "symbol"], inplace=True)

    close_series: dict[str, pd.Series] = {}
    gap_series: dict[str, pd.Series] = {}
    intraday_series: dict[str, pd.Series] = {}
    selection_rows: list[dict] = []
    quality: dict[str, dict] = {}

    for product in REQUIRED_PRODUCTS:
        product_frame = frame[frame["product"] == product].copy()
        if product_frame.empty:
            raise ValueError(f"return-target L4 missing product: {product}")
        by_symbol = {
            str(symbol): group.set_index("date").sort_index()
            for symbol, group in product_frame.groupby("symbol")
        }
        dates = pd.DatetimeIndex(sorted(product_frame["date"].unique()))
        choices: dict[pd.Timestamp, str] = {}
        for trading_day, day_rows in product_frame.groupby("date"):
            trading_day = pd.Timestamp(trading_day)
            eligible = day_rows[
                (day_rows["delivery"] - trading_day).dt.days >= MIN_DAYS_TO_DELIVERY
            ].copy()
            if eligible.empty:
                continue
            eligible.sort_values(
                ["hold", "volume", "delivery", "symbol"],
                ascending=[False, False, True, True],
                inplace=True,
            )
            chosen = eligible.iloc[0]
            symbol = str(chosen["symbol"])
            choices[trading_day] = symbol
            selection_rows.append(
                {
                    "date": trading_day,
                    "product": product,
                    "exchange": str(
                        chosen.get("exchange", specific_fetch.PRODUCT_EXCHANGE[product])
                    ),
                    "symbol": symbol,
                    "delivery": pd.Timestamp(chosen["delivery"]),
                    "open": float(chosen["open"]),
                    "close": float(chosen["close"]),
                    "volume": float(chosen["volume"]),
                    "open_interest": float(chosen["hold"]),
                    "days_to_delivery": int((pd.Timestamp(chosen["delivery"]) - trading_day).days),
                }
            )

        selected = pd.Series(choices, dtype=object).sort_index()
        valid_days = int(selected.index.nunique())
        if valid_days < MIN_PRODUCT_DAYS:
            raise ValueError(
                f"return-target specific coverage too short for {product}: {valid_days}"
            )

        close_ret = pd.Series(np.nan, index=dates, dtype=float)
        gap_ret = pd.Series(np.nan, index=dates, dtype=float)
        intraday_ret = pd.Series(np.nan, index=dates, dtype=float)
        previous_date: pd.Timestamp | None = None
        previous_symbol: str | None = None
        missing_next = 0
        rolls = 0
        for trading_day in dates:
            trading_day = pd.Timestamp(trading_day)
            if previous_date is not None and previous_symbol is not None:
                symbol_frame = by_symbol.get(previous_symbol)
                if (
                    symbol_frame is not None
                    and previous_date in symbol_frame.index
                    and trading_day in symbol_frame.index
                ):
                    previous_close = float(symbol_frame.loc[previous_date, "close"])
                    current_open = float(symbol_frame.loc[trading_day, "open"])
                    current_close = float(symbol_frame.loc[trading_day, "close"])
                    values = {
                        "close": current_close / previous_close - 1.0,
                        "gap": current_open / previous_close - 1.0,
                        "intraday": current_close / current_open - 1.0,
                    }
                    if all(
                        np.isfinite(value) and abs(value) <= aggressive.MAX_ABS_DAILY_RETURN
                        for value in values.values()
                    ):
                        close_ret.loc[trading_day] = values["close"]
                        gap_ret.loc[trading_day] = values["gap"]
                        intraday_ret.loc[trading_day] = values["intraday"]
                    else:
                        missing_next += 1
                else:
                    missing_next += 1
            current_symbol = choices.get(trading_day)
            if (
                previous_symbol is not None
                and current_symbol is not None
                and current_symbol != previous_symbol
            ):
                rolls += 1
            previous_date = trading_day
            previous_symbol = current_symbol

        close_series[product] = close_ret
        gap_series[product] = gap_ret
        intraday_series[product] = intraday_ret
        quality[product] = {
            "selected_days": valid_days,
            "contracts_used": int(selected.nunique()),
            "rolls": int(rolls),
            "missing_next_contract_returns": int(missing_next),
            "missing_next_ratio": float(missing_next / max(len(dates) - 1, 1)),
        }

    close_panel = pd.DataFrame(close_series).sort_index()
    gap_panel = pd.DataFrame(gap_series).reindex(close_panel.index)
    intraday_panel = pd.DataFrame(intraday_series).reindex(close_panel.index)
    selections = pd.DataFrame(selection_rows).sort_values(["date", "product"])
    if selections.empty:
        raise ValueError("return-target specific selection is empty")
    if int(selections["days_to_delivery"].min()) < MIN_DAYS_TO_DELIVERY:
        raise AssertionError("return-target delivery blackout violated")
    return close_panel, gap_panel, intraday_panel, selections, quality


def build_roll_safe_returns(raw: pd.DataFrame):
    """Compatibility helper for earlier research callers."""
    close, _gap, _intraday, selections, quality = build_roll_safe_execution_returns(raw)
    return close, selections, quality


def apply_product_weights(
    returns: pd.DataFrame,
    weights: pd.DataFrame,
    *,
    cost_bps: float,
) -> pd.Series:
    weights = weights.reindex(index=returns.index, columns=returns.columns, fill_value=0.0).fillna(
        0.0
    )
    gross = weights.abs().sum(axis=1)
    if bool((gross > MAX_GROSS_LEVERAGE + 1e-10).any()):
        raise ValueError(f"product weights exceed 2x gross cap: {float(gross.max())}")
    pnl = (weights * returns.reindex_like(weights).fillna(0.0)).sum(axis=1)
    turnover = weights.diff().abs().sum(axis=1)
    if len(weights):
        turnover.iloc[0] = float(weights.iloc[0].abs().sum())
    return (pnl - turnover * float(cost_bps) / 10000.0).astype(float)


def apply_next_open_product_weights(
    gap_returns: pd.DataFrame,
    intraday_returns: pd.DataFrame,
    weights: pd.DataFrame,
    *,
    cost_bps: float,
) -> pd.Series:
    """Old weights earn gap; new target weights earn open-close; rebalance cost at open."""
    weights = weights.reindex(
        index=gap_returns.index, columns=gap_returns.columns, fill_value=0.0
    ).fillna(0.0)
    gross = weights.abs().sum(axis=1)
    if bool((gross > MAX_GROSS_LEVERAGE + 1e-10).any()):
        raise ValueError(f"product weights exceed 2x gross cap: {float(gross.max())}")
    old_weights = weights.shift(1).fillna(0.0)
    gaps = gap_returns.reindex_like(weights).fillna(0.0)
    intraday = intraday_returns.reindex_like(weights).fillna(0.0)
    pnl = (old_weights * gaps).sum(axis=1) + (weights * intraday).sum(axis=1)
    turnover = weights.diff().abs().sum(axis=1)
    if len(weights):
        turnover.iloc[0] = float(weights.iloc[0].abs().sum())
    return (pnl - turnover * float(cost_bps) / 10000.0).astype(float)


def _template_weight_path(
    signal_returns: pd.DataFrame,
    template: aggressive.DirectionalTemplate,
) -> pd.DataFrame:
    scores = aggressive.signal_scores(signal_returns, template).to_numpy(float)
    current = np.zeros(signal_returns.shape[1], dtype=float)
    audit = np.zeros(signal_returns.shape, dtype=float)
    step = max(int(template.rebalance), 1)
    for position in range(1, len(signal_returns)):
        if position % step == 0:
            lagged = scores[position - 1]
            valid = np.flatnonzero(np.isfinite(lagged) & (np.abs(lagged) > 1e-12))
            next_weights = np.zeros(signal_returns.shape[1], dtype=float)
            if valid.size:
                order = valid[np.argsort(-np.abs(lagged[valid]), kind="stable")]
                selected = order[: min(int(template.max_products), len(order))]
                if selected.size:
                    each = min(float(template.gross_leverage), MAX_GROSS_LEVERAGE) / float(
                        selected.size
                    )
                    next_weights[selected] = np.sign(lagged[selected]) * each
            current = next_weights
        audit[position] = current
    result = pd.DataFrame(audit, index=signal_returns.index, columns=signal_returns.columns)
    if float(result.abs().sum(axis=1).max()) > MAX_GROSS_LEVERAGE + 1e-10:
        raise AssertionError("template weight path exceeds gross cap")
    return result


def _template_lookup() -> dict[str, aggressive.DirectionalTemplate]:
    lookup = {
        aggressive.template_id(template): template
        for template in aggressive.directional_templates()
    }
    missing = [item for item in EXECUTION_SELECTION["pool_ids"] if item not in lookup]
    if missing:
        raise ValueError(f"execution-fit templates missing: {missing}")
    return lookup


def _meta_weight_path(
    score_streams: dict[str, pd.Series],
    template_weights: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    frame = pd.DataFrame(score_streams).sort_index().fillna(0.0)
    names = list(frame.columns)
    scores = aggressive._trailing_score_matrix(frame, int(EXECUTION_SELECTION["meta_lookback"]))
    example = template_weights[names[0]].reindex(frame.index).fillna(0.0)
    final = pd.DataFrame(0.0, index=frame.index, columns=example.columns)
    selected: list[int] = []
    lookback = int(EXECUTION_SELECTION["meta_lookback"])
    rebalance = max(int(EXECUTION_SELECTION["rebalance"]), 1)
    count = int(EXECUTION_SELECTION["count"])
    for position, timestamp in enumerate(frame.index):
        if position >= lookback and (not selected or position % rebalance == 0):
            row = scores[position]
            valid = np.flatnonzero(np.isfinite(row))
            selected = (
                [int(item) for item in valid[np.argsort(-row[valid], kind="stable")][:count]]
                if valid.size
                else []
            )
        if selected:
            rows = [template_weights[names[item]].loc[timestamp] for item in selected]
            final.loc[timestamp] = pd.concat(rows, axis=1).mean(axis=1)
    if float(final.abs().sum(axis=1).max()) > MAX_GROSS_LEVERAGE + 1e-10:
        raise AssertionError("meta weight path exceeds gross cap")
    return final


def generate_execution_signal_weights(continuous_raw: pd.DataFrame) -> pd.DataFrame:
    """Generate the frozen execution-fit product weights from causal continuous data."""
    signal_returns, _ = aggressive.build_panel(continuous_raw)
    signal_returns.columns = [str(column).upper() for column in signal_returns.columns]
    missing = sorted(set(REQUIRED_PRODUCTS) - set(signal_returns.columns))
    if missing:
        raise ValueError(f"continuous signal feed missing products: {missing}")
    signal_returns = signal_returns[list(REQUIRED_PRODUCTS)]
    lookup = _template_lookup()
    score_streams: dict[str, pd.Series] = {}
    weight_paths: dict[str, pd.DataFrame] = {}
    for template_id in EXECUTION_SELECTION["pool_ids"]:
        template = lookup[template_id]
        gross_pnl, turnover, _ = aggressive._simulate_arrays(
            signal_returns, template, record_weights=False
        )
        # Meta ranking is exactly the frozen L3 continuous theoretical base-cost score.
        score_streams[template_id] = aggressive.apply_cost(gross_pnl, turnover, BASE_COST_BPS)
        weight_paths[template_id] = _template_weight_path(signal_returns, template)
    return _meta_weight_path(score_streams, weight_paths)


def _window_metrics(series: pd.Series) -> dict[str, dict]:
    return {name: aggressive._window_metrics(series, name) for name in WINDOWS}


def _target_pass(base: dict, stress: dict) -> bool:
    return bool(
        base["annualized_return"] >= 1.0
        and base["max_drawdown"] > -0.30
        and base["active_days"] >= 50
        and stress["annualized_return"] > 0.0
    )


def _validate_l3_report(report: dict) -> None:
    selection = report.get("selection", {})
    for key in ("selection_bias", "pool_size", "meta_lookback", "rebalance", "count"):
        if selection.get(key) != L3_BASELINE_SELECTION[key]:
            raise ValueError(
                f"L3 baseline drifted for {key}: {selection.get(key)!r} != {L3_BASELINE_SELECTION[key]!r}"
            )
    if float(selection.get("effective_gross_leverage", 0.0)) > MAX_GROSS_LEVERAGE:
        raise ValueError("L3 baseline exceeds 2x gross cap")


def evaluate(
    specific_raw: pd.DataFrame,
    continuous_raw: pd.DataFrame,
    l3_report: dict,
) -> dict:
    _validate_l3_report(l3_report)
    close_ret, gap_ret, intraday_ret, selections, quality = build_roll_safe_execution_returns(
        specific_raw
    )
    weights = generate_execution_signal_weights(continuous_raw)
    weights = weights.reindex(
        index=close_ret.index, columns=close_ret.columns, fill_value=0.0
    ).fillna(0.0)

    close_paths: dict[str, dict] = {}
    delayed_paths: dict[str, dict] = {}
    for label, cost in (
        ("base", BASE_COST_BPS),
        ("stress", STRESS_COST_BPS),
        ("extreme", EXTREME_COST_BPS),
    ):
        close_paths[label] = _window_metrics(
            apply_product_weights(close_ret, weights, cost_bps=cost)
        )
        delayed_paths[label] = _window_metrics(
            apply_next_open_product_weights(gap_ret, intraday_ret, weights, cost_bps=cost)
        )

    quality_reasons: list[str] = []
    for product, item in sorted(quality.items()):
        if item["selected_days"] < MIN_PRODUCT_DAYS:
            quality_reasons.append(f"{product} selected-day coverage below {MIN_PRODUCT_DAYS}")
        if item["missing_next_ratio"] >= 0.05:
            quality_reasons.append(f"{product} missing same-contract next return >=5%")

    base_recent = delayed_paths["base"]["full_recent"]
    stress_recent = delayed_paths["stress"]["full_recent"]
    target_pass = _target_pass(base_recent, stress_recent)
    reasons = list(quality_reasons)
    if not target_pass:
        reasons.append(
            "next-open roll-safe execution does not retain >=100% annualized return with base drawdown <30% and positive stress return"
        )

    report = {
        "source": "AKShare/Sina continuous daily signals + concrete daily contract execution",
        "role": "selection-biased execution-aware roll-safe L4 for aggressive directional production candidate",
        "specific_contracts": True,
        "roll_safe": True,
        "selection_frozen": True,
        "execution_selection": EXECUTION_SELECTION,
        "selection_bias_acknowledged": True,
        "historical_l1_available": False,
        "pristine_final_oos": PRISTINE_FINAL_OOS,
        "max_gross_leverage": MAX_GROSS_LEVERAGE,
        "min_days_to_delivery": MIN_DAYS_TO_DELIVERY,
        "data_quality": quality,
        "close_to_close_diagnostic": close_paths,
        "next_open_execution": delayed_paths,
        "target": {
            "annualized_return": 1.0,
            "max_drawdown": -0.30,
            "stress_must_be_positive": True,
            "gross_leverage_cap": MAX_GROSS_LEVERAGE,
            "target_met": bool(target_pass and not quality_reasons),
            "reasons": reasons,
        },
    }
    output = Path("runtime")
    output.mkdir(parents=True, exist_ok=True)
    selections.to_csv(output / "return_target_specific_selection.csv", index=False)
    weights.stack().rename("weight").reset_index().query("abs(weight) > 1e-15").to_csv(
        output / "return_target_execution_weights.csv", index=False
    )
    return report


def main() -> None:
    runtime = Path("runtime")
    specific_path = runtime / "return_target_specific_contracts.csv"
    continuous_path = runtime / "broad_daily_universe.csv"
    l3_path = runtime / "aggressive_directional_report.json"
    required = (specific_path, continuous_path, l3_path)
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise SystemExit(f"L4 inputs missing: {missing}")
    report = evaluate(
        pd.read_csv(specific_path),
        pd.read_csv(continuous_path),
        json.loads(l3_path.read_text(encoding="utf-8")),
    )
    output = runtime / "return_target_specific_report.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "execution_selection": report["execution_selection"],
                "data_quality": report["data_quality"],
                "close_to_close_diagnostic": report["close_to_close_diagnostic"],
                "next_open_execution": report["next_open_execution"],
                "target": report["target"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
