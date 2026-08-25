"""Roll-safe specific-contract L4 validation for economic-pair rotation.

The broad continuous-contract screen may contain roll artefacts. This validator rebuilds
one causal tradable index per product from concrete futures contracts. Contract choice at
close t uses only t open interest and a 20-day delivery blackout. Return t->t+1 is then
measured on that same chosen contract, so no synthetic roll jump can become alpha.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import evaluate_broad_pair_regime as base
import evaluate_broad_pair_rotation as rotation
import numpy as np
import pandas as pd

MIN_DAYS_TO_DELIVERY = 20
MIN_PRODUCT_DAYS = 700
MAX_ACTIVE_PAIRS = 1

PAIRS = (
    base.EconomicPair("P", "Y", "DCE", "edible_oil"),
    base.EconomicPair("PP", "V", "DCE", "polymer"),
    base.EconomicPair("AL", "ZN", "SHFE", "base_metal"),
    base.EconomicPair("BU", "FU", "SHFE", "fuel"),
    base.EconomicPair("CU", "AL", "SHFE", "base_metal"),
    base.EconomicPair("J", "JM", "DCE", "coal"),
)


def build_roll_safe_panel(
    raw: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    frame = raw.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["delivery"] = pd.to_datetime(frame["delivery"], errors="coerce")
    for column in ("close", "volume", "hold"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["date", "delivery", "product", "symbol", "close", "hold"])
    frame = frame[(frame["close"] > 0) & (frame["hold"] >= 0)]
    frame.drop_duplicates(["product", "symbol", "date"], keep="last", inplace=True)
    frame.sort_values(["product", "date", "symbol"], inplace=True)

    selected_rows: list[dict] = []
    index_series: dict[str, pd.Series] = {}
    return_series: dict[str, pd.Series] = {}
    quality: dict[str, dict] = {}

    required_products = sorted({item for pair in PAIRS for item in (pair.left, pair.right)})
    for product in required_products:
        product_frame = frame[frame["product"] == product].copy()
        if product_frame.empty:
            raise ValueError(f"specific-contract data missing product: {product}")
        dates = pd.DatetimeIndex(sorted(product_frame["date"].unique()))
        by_symbol = {
            symbol: group.set_index("date").sort_index()
            for symbol, group in product_frame.groupby("symbol")
        }

        choices: dict[pd.Timestamp, str] = {}
        for trading_day, day_rows in product_frame.groupby("date"):
            trading_day = pd.Timestamp(trading_day)
            eligible = day_rows[
                (day_rows["delivery"] - trading_day).dt.days >= MIN_DAYS_TO_DELIVERY
            ].copy()
            if eligible.empty:
                continue
            eligible["volume"] = eligible["volume"].fillna(0.0)
            eligible.sort_values(
                ["hold", "volume", "delivery", "symbol"],
                ascending=[False, False, True, True],
                inplace=True,
            )
            chosen = eligible.iloc[0]
            choices[trading_day] = str(chosen["symbol"])
            selected_rows.append(
                {
                    "date": trading_day,
                    "product": product,
                    "symbol": str(chosen["symbol"]),
                    "delivery": pd.Timestamp(chosen["delivery"]),
                    "close": float(chosen["close"]),
                    "volume": float(chosen["volume"]),
                    "open_interest": float(chosen["hold"]),
                    "days_to_delivery": int((pd.Timestamp(chosen["delivery"]) - trading_day).days),
                }
            )

        selected = pd.Series(choices, dtype=object).sort_index()
        all_dates = dates.union(selected.index).sort_values()
        tradable_returns = pd.Series(np.nan, index=all_dates, dtype=float)
        synthetic_index = pd.Series(np.nan, index=all_dates, dtype=float)
        current_index = 100.0
        previous_date: pd.Timestamp | None = None
        previous_symbol: str | None = None
        missing_next = 0
        rolls = 0

        for trading_day in all_dates:
            trading_day = pd.Timestamp(trading_day)
            if previous_date is None:
                synthetic_index.loc[trading_day] = current_index
            else:
                realized = np.nan
                if previous_symbol is not None:
                    symbol_frame = by_symbol.get(previous_symbol)
                    if (
                        symbol_frame is not None
                        and previous_date in symbol_frame.index
                        and trading_day in symbol_frame.index
                    ):
                        previous_close = float(symbol_frame.loc[previous_date, "close"])
                        current_close = float(symbol_frame.loc[trading_day, "close"])
                        if previous_close > 0 and current_close > 0:
                            realized = current_close / previous_close - 1.0
                if np.isfinite(realized) and abs(realized) <= 0.20:
                    tradable_returns.loc[trading_day] = float(realized)
                    current_index *= 1.0 + float(realized)
                else:
                    missing_next += 1
                synthetic_index.loc[trading_day] = current_index

            current_symbol = choices.get(trading_day)
            if (
                previous_symbol is not None
                and current_symbol is not None
                and current_symbol != previous_symbol
            ):
                rolls += 1
            previous_symbol = current_symbol
            previous_date = trading_day

        valid_days = int(selected.index.nunique())
        if valid_days < MIN_PRODUCT_DAYS:
            raise ValueError(
                f"specific-contract coverage too short for {product}: {valid_days} days"
            )
        index_series[product] = synthetic_index
        return_series[product] = tradable_returns
        quality[product] = {
            "selected_days": valid_days,
            "contracts_used": int(selected.nunique()),
            "rolls": int(rolls),
            "missing_next_contract_returns": int(missing_next),
            "missing_next_ratio": float(missing_next / max(len(all_dates) - 1, 1)),
        }

    close = pd.DataFrame(index_series).sort_index()
    returns = pd.DataFrame(return_series).reindex(close.index)
    selections = pd.DataFrame(selected_rows).sort_values(["date", "product"])
    if not selections.empty and int(selections["days_to_delivery"].min()) < MIN_DAYS_TO_DELIVERY:
        raise AssertionError("delivery blackout violated in roll-safe selection")
    return close, returns, selections, quality


def _evaluate_profile(
    close: pd.DataFrame,
    returns: pd.DataFrame,
    profile: base.PairProfile,
    pair_lookup: dict[str, base.EconomicPair],
    statistics_cache: dict[tuple[str, int], dict[str, pd.Series]],
) -> dict:
    stressed_series: dict[str, pd.Series] = {}
    pair_metrics: dict[str, dict] = {}
    for pair_id, pair in pair_lookup.items():
        cache_key = (pair_id, profile.formation)
        if cache_key not in statistics_cache:
            statistics_cache[cache_key] = base._pair_statistics(
                close, returns, pair, profile.formation
            )
        stressed, entries = base._simulate_pair(
            close,
            returns,
            pair,
            profile,
            statistics_cache[cache_key],
            cost_bps=base.STRESS_COST_BPS,
        )
        stressed_series[pair_id] = stressed
        pair_metrics[pair_id] = {
            "entries": len(entries),
            **{
                window: base._window_metrics(stressed, window)
                for window in ("prior1", "prior2", "train", "validation", "oos", "full_recent")
            },
        }

    prior_pairs = sorted(
        pair_id
        for pair_id, item in pair_metrics.items()
        if base._qualifies(item["prior1"]) and base._qualifies(item["prior2"])
    )
    current_pairs = sorted(
        pair_id
        for pair_id, item in pair_metrics.items()
        if base._qualifies(item["train"]) and base._qualifies(item["validation"])
    )
    prior = rotation._rotating_portfolio(
        close,
        returns,
        prior_pairs,
        profile,
        statistics_cache,
        pair_lookup,
        cost_bps=base.STRESS_COST_BPS,
        max_active_pairs=MAX_ACTIVE_PAIRS,
    )
    current = rotation._rotating_portfolio(
        close,
        returns,
        current_pairs,
        profile,
        statistics_cache,
        pair_lookup,
        cost_bps=base.STRESS_COST_BPS,
        max_active_pairs=MAX_ACTIVE_PAIRS,
    )
    result = {
        "profile_id": base._profile_id(profile),
        "profile": asdict(profile),
        "prior_pairs": prior_pairs,
        "current_pairs": current_pairs,
        "prior1": base._window_metrics(prior, "prior1"),
        "prior2": base._window_metrics(prior, "prior2"),
        "prior_forward_train": base._window_metrics(prior, "train"),
        "train": base._window_metrics(current, "train"),
        "validation": base._window_metrics(current, "validation"),
        "oos": base._window_metrics(current, "oos"),
        "full_recent": base._window_metrics(current, "full_recent"),
    }
    pre_oos = [
        result["prior1"],
        result["prior2"],
        result["prior_forward_train"],
        result["train"],
        result["validation"],
    ]
    result["pre_oos_pass"] = bool(
        prior_pairs and current_pairs and all(base._qualifies(item) for item in pre_oos)
    )
    result["pre_oos_score"] = (
        min(item["sharpe"] for item in pre_oos) if result["pre_oos_pass"] else -999.0
    )
    return result


def evaluate(raw: pd.DataFrame) -> dict:
    close, returns, selections, data_quality = build_roll_safe_panel(raw)
    pair_lookup = {f"{pair.left}/{pair.right}": pair for pair in PAIRS}
    statistics_cache: dict[tuple[str, int], dict[str, pd.Series]] = {}
    profiles = [
        _evaluate_profile(close, returns, profile, pair_lookup, statistics_cache)
        for profile in base.PROFILES
    ]
    eligible = [item for item in profiles if item["pre_oos_pass"]]
    selected = max(eligible, key=lambda item: item["pre_oos_score"]) if eligible else None

    report = {
        "source": "AKShare/Sina concrete futures daily bars",
        "role": "L4 roll-safe specific-contract signal evidence; no historical L1/depth",
        "historical_l1_available": False,
        "specific_contracts": True,
        "roll_safe": True,
        "pristine_final_oos": False,
        "stress_cost_bps_one_way": base.STRESS_COST_BPS,
        "min_days_to_delivery": MIN_DAYS_TO_DELIVERY,
        "max_active_pairs": MAX_ACTIVE_PAIRS,
        "max_gross_leverage": base.MAX_GROSS_LEVERAGE,
        "pair_count": len(PAIRS),
        "profile_count": len(base.PROFILES),
        "data_quality": data_quality,
        "profiles": profiles,
        "support": {
            "eligible_profiles": len(eligible),
            "formation_60": sum(
                item["pre_oos_pass"] and item["profile"]["formation"] == 60 for item in profiles
            ),
            "formation_120": sum(
                item["pre_oos_pass"] and item["profile"]["formation"] == 120 for item in profiles
            ),
        },
        "selected_profile": selected["profile"] if selected else None,
        "selected_profile_id": selected["profile_id"] if selected else None,
        "selected_prior_pairs": selected["prior_pairs"] if selected else [],
        "selected_current_pairs": selected["current_pairs"] if selected else [],
        "selected_prior_forward_train": selected["prior_forward_train"] if selected else None,
        "selected_oos_unlevered": selected["oos"] if selected else None,
        "selected_full_recent_unlevered": selected["full_recent"] if selected else None,
    }

    reasons: list[str] = []
    alpha_reasons: list[str] = []
    if selected is None:
        alpha_reasons.append("no roll-safe profile survives all pre-OOS gates")
        report["selected_leverage"] = 0.0
        report["selected_oos"] = None
        report["selected_full_recent"] = None
    else:
        profile = base.PairProfile(**selected["profile"])
        current = rotation._rotating_portfolio(
            close,
            returns,
            selected["current_pairs"],
            profile,
            statistics_cache,
            pair_lookup,
            cost_bps=base.STRESS_COST_BPS,
            max_active_pairs=MAX_ACTIVE_PAIRS,
        )
        selection_start, selection_end = base.WINDOWS["selection_full"]
        calibration = current.loc[pd.Timestamp(selection_start) : pd.Timestamp(selection_end)]
        leverage = base._choose_leverage(calibration)
        report["selected_leverage"] = leverage
        scaled = current * leverage if leverage > 0 else current * 0.0
        report["selected_oos"] = base._window_metrics(scaled, "oos")
        report["selected_full_recent"] = base._window_metrics(scaled, "full_recent")
        if len(eligible) < 2:
            alpha_reasons.append("roll-safe profile neighborhood support is below two")
        if selected["prior_forward_train"]["annualized_return"] <= 0:
            alpha_reasons.append("prior-selected roll-safe family fails forward train")
        if report["selected_oos"]["annualized_return"] <= 0:
            alpha_reasons.append("roll-safe final OOS return is not positive")
        if report["selected_full_recent"]["annualized_return"] <= 0:
            alpha_reasons.append("roll-safe recent two-year return is not positive")
        if report["selected_full_recent"]["max_drawdown"] <= base.MAX_TARGET_DRAWDOWN:
            alpha_reasons.append("roll-safe drawdown exceeds 20%")
        if leverage <= 0:
            alpha_reasons.append("no leverage level satisfies calibration drawdown gate")

    report["alpha_survives_specific_contract"] = not alpha_reasons
    report["alpha_reasons"] = alpha_reasons
    reasons.extend(alpha_reasons)
    if (
        report.get("selected_full_recent") is None
        or report["selected_full_recent"]["annualized_return"] < 1.0
    ):
        reasons.append("stressed roll-safe two-year annualized return is below 100%")
    report["target"] = {
        "annualized_return": 1.0,
        "max_drawdown": base.MAX_TARGET_DRAWDOWN,
        "target_met": not reasons,
        "reasons": reasons,
    }

    output = Path("runtime")
    output.mkdir(parents=True, exist_ok=True)
    selections.to_csv(output / "specific_pair_roll_selection.csv", index=False)
    pd.DataFrame(
        {"date": close.index, **{product: close[product].to_numpy() for product in close.columns}}
    ).to_csv(output / "specific_pair_roll_safe_indices.csv", index=False)
    return report


def main() -> None:
    path = Path("runtime/specific_pair_daily_contracts.csv")
    if not path.exists():
        raise SystemExit("specific contract history missing")
    report = evaluate(pd.read_csv(path))
    output = Path("runtime/specific_pair_rotation_report.json")
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "data_quality": report["data_quality"],
                "support": report["support"],
                "selected_profile": report["selected_profile"],
                "selected_prior_pairs": report["selected_prior_pairs"],
                "selected_current_pairs": report["selected_current_pairs"],
                "selected_prior_forward_train": report["selected_prior_forward_train"],
                "selected_leverage": report["selected_leverage"],
                "selected_oos": report["selected_oos"],
                "selected_full_recent": report["selected_full_recent"],
                "alpha_survives_specific_contract": report["alpha_survives_specific_contract"],
                "alpha_reasons": report["alpha_reasons"],
                "target": report["target"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
