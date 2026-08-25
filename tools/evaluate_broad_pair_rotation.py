"""Capital-efficient rotation across prequalified economic pairs.

The underlying pair family is defined in evaluate_broad_pair_regime.py. This module
adds the production-like selector behavior afuture actually needs: capital is assigned
only to the strongest currently active pair instead of being permanently divided among
all prequalified pairs. Selection is causal, turnover is charged whenever the selected
pair or residual direction changes, and Final OOS remains evaluation-only.
"""

from __future__ import annotations

import json
from pathlib import Path

import evaluate_broad_pair_regime as base
import numpy as np
import pandas as pd

MAX_ACTIVE_PAIRS = 1


def _desired_path(
    close: pd.DataFrame,
    returns: pd.DataFrame,
    pair: base.EconomicPair,
    profile: base.PairProfile,
    statistics: dict[str, pd.Series],
) -> pd.DataFrame:
    index = close.index
    left_returns = returns[pair.left].reindex(index).to_numpy(float)
    right_returns = returns[pair.right].reindex(index).to_numpy(float)
    beta = statistics["beta"].reindex(index).to_numpy(float)
    zscore = statistics["zscore"].reindex(index).to_numpy(float)
    correlation = statistics["correlation"].reindex(index).to_numpy(float)
    half_life = statistics["half_life"].reindex(index).to_numpy(float)
    volatility_ratio = statistics["volatility_ratio"].reindex(index).to_numpy(float)

    direction = np.zeros(len(index), dtype=int)
    score = np.zeros(len(index), dtype=float)
    next_return = np.zeros(len(index), dtype=float)
    state = 0
    entry_index = -1
    left_weight = 0.0
    right_weight = 0.0

    for position_index in range(len(index) - 1):
        current_z = zscore[position_index]
        if state == 0:
            eligible = (
                np.isfinite(current_z)
                and np.isfinite(beta[position_index])
                and beta[position_index] > 0.0
                and np.isfinite(correlation[position_index])
                and correlation[position_index] >= profile.min_correlation
                and np.isfinite(half_life[position_index])
                and 2.0 <= half_life[position_index] <= 60.0
                and np.isfinite(volatility_ratio[position_index])
                and volatility_ratio[position_index] >= profile.min_volatility_ratio
            )
            if eligible and current_z >= profile.entry_z:
                state = -1
            elif eligible and current_z <= -profile.entry_z:
                state = 1
            if state:
                entry_index = position_index
                current_beta = float(beta[position_index])
                normalization = 1.0 + abs(current_beta)
                left_weight = state / normalization
                right_weight = state * (-current_beta / normalization)
        else:
            holding_days = position_index - entry_index
            exit_now = (
                not np.isfinite(current_z)
                or abs(current_z) <= profile.exit_z
                or abs(current_z) >= profile.stop_z
                or holding_days >= profile.max_holding_days
            )
            if exit_now:
                state = 0
                entry_index = -1
                left_weight = 0.0
                right_weight = 0.0

        direction[position_index] = state
        score[position_index] = abs(current_z) if state and np.isfinite(current_z) else 0.0
        if state:
            next_index = position_index + 1
            left_value = left_returns[next_index]
            right_value = right_returns[next_index]
            if np.isfinite(left_value) and np.isfinite(right_value):
                next_return[next_index] = left_weight * left_value + right_weight * right_value

    return pd.DataFrame(
        {"direction": direction, "score": score, "next_return": next_return},
        index=index,
    )


def _select_pair_ids(
    candidates: list[tuple[str, float]],
    pair_lookup: dict[str, base.EconomicPair],
    *,
    max_active_pairs: int = MAX_ACTIVE_PAIRS,
) -> list[str]:
    """Select strongest candidates without sharing a contract root."""
    selected: list[str] = []
    used_products: set[str] = set()
    for pair_id, _ in sorted(candidates, key=lambda item: (-item[1], item[0])):
        pair = pair_lookup[pair_id]
        if pair.left in used_products or pair.right in used_products:
            continue
        selected.append(pair_id)
        used_products.add(pair.left)
        used_products.add(pair.right)
        if len(selected) >= max_active_pairs:
            break
    return selected


def _rotating_portfolio(
    close: pd.DataFrame,
    returns: pd.DataFrame,
    pair_ids: list[str],
    profile: base.PairProfile,
    statistics_cache: dict[tuple[str, int], dict[str, pd.Series]],
    pair_lookup: dict[str, base.EconomicPair],
    *,
    cost_bps: float,
    max_active_pairs: int = MAX_ACTIVE_PAIRS,
) -> pd.Series:
    if not pair_ids:
        return pd.Series(0.0, index=close.index)

    paths = {
        pair_id: _desired_path(
            close,
            returns,
            pair_lookup[pair_id],
            profile,
            statistics_cache[(pair_id, profile.formation)],
        )
        for pair_id in pair_ids
    }
    previous_signed_allocation = {pair_id: 0.0 for pair_id in pair_ids}
    pnl = pd.Series(0.0, index=close.index)

    for position_index in range(len(close.index) - 1):
        candidates = [
            (pair_id, float(paths[pair_id]["score"].iat[position_index]))
            for pair_id in pair_ids
            if int(paths[pair_id]["direction"].iat[position_index]) != 0
        ]
        selected = _select_pair_ids(
            candidates,
            pair_lookup,
            max_active_pairs=max_active_pairs,
        )
        allocation = {pair_id: 0.0 for pair_id in pair_ids}
        if selected:
            gross_each = 1.0 / len(selected)
            for pair_id in selected:
                direction = int(paths[pair_id]["direction"].iat[position_index])
                allocation[pair_id] = direction * gross_each

        turnover = sum(
            abs(allocation[pair_id] - previous_signed_allocation[pair_id]) for pair_id in pair_ids
        )
        pnl.iat[position_index] -= turnover * cost_bps / 10000.0

        next_index = position_index + 1
        for pair_id in selected:
            gross_weight = abs(allocation[pair_id])
            pnl.iat[next_index] += gross_weight * paths[pair_id]["next_return"].iat[next_index]
        previous_signed_allocation = allocation

    pnl.iat[-1] -= (
        sum(abs(value) for value in previous_signed_allocation.values()) * cost_bps / 10000.0
    )
    return pnl


def evaluate(raw: pd.DataFrame) -> dict:
    close, returns = base._load_panel(raw)
    pair_lookup = {f"{pair.left}/{pair.right}": pair for pair in base.PAIRS}
    statistics_cache: dict[tuple[str, int], dict[str, pd.Series]] = {}

    # Pair eligibility remains the conservative individual-pair gate from the base
    # screen; rotation changes capital use, not which pairs are allowed into research.
    base_report = base.evaluate(raw)
    results: list[dict] = []

    for base_row in base_report["profiles"]:
        profile = base.PairProfile(**base_row["profile"])
        pair_ids = sorted(set(base_row["prior_pairs"]) | set(base_row["current_pairs"]))
        for pair_id in pair_ids:
            cache_key = (pair_id, profile.formation)
            if cache_key not in statistics_cache:
                statistics_cache[cache_key] = base._pair_statistics(
                    close,
                    returns,
                    pair_lookup[pair_id],
                    profile.formation,
                )

        prior = _rotating_portfolio(
            close,
            returns,
            base_row["prior_pairs"],
            profile,
            statistics_cache,
            pair_lookup,
            cost_bps=base.STRESS_COST_BPS,
        )
        current = _rotating_portfolio(
            close,
            returns,
            base_row["current_pairs"],
            profile,
            statistics_cache,
            pair_lookup,
            cost_bps=base.STRESS_COST_BPS,
        )
        metrics = {
            "profile_id": base_row["profile_id"],
            "profile": base_row["profile"],
            "prior_pairs": base_row["prior_pairs"],
            "current_pairs": base_row["current_pairs"],
            "prior1": base._window_metrics(prior, "prior1"),
            "prior2": base._window_metrics(prior, "prior2"),
            "prior_forward_train": base._window_metrics(prior, "train"),
            "train": base._window_metrics(current, "train"),
            "validation": base._window_metrics(current, "validation"),
            "oos": base._window_metrics(current, "oos"),
            "full_recent": base._window_metrics(current, "full_recent"),
        }
        pre_oos_items = [
            metrics["prior1"],
            metrics["prior2"],
            metrics["prior_forward_train"],
            metrics["train"],
            metrics["validation"],
        ]
        metrics["pre_oos_pass"] = bool(
            metrics["prior_pairs"]
            and metrics["current_pairs"]
            and all(base._qualifies(item) for item in pre_oos_items)
        )
        metrics["pre_oos_score"] = (
            min(item["sharpe"] for item in pre_oos_items) if metrics["pre_oos_pass"] else -999.0
        )
        results.append(metrics)

    eligible = [item for item in results if item["pre_oos_pass"]]
    selected = max(eligible, key=lambda item: item["pre_oos_score"]) if eligible else None
    support = {
        "eligible_profiles": len(eligible),
        "formation_60": sum(
            item["pre_oos_pass"] and item["profile"]["formation"] == 60 for item in results
        ),
        "formation_120": sum(
            item["pre_oos_pass"] and item["profile"]["formation"] == 120 for item in results
        ),
    }

    report = {
        "source": base_report["source"],
        "role": "L3 capital-efficient economic-pair rotation screen only",
        "historical_l1_available": False,
        "continuous_roll_series": True,
        "pristine_final_oos": False,
        "stress_cost_bps_one_way": base.STRESS_COST_BPS,
        "max_active_pairs": MAX_ACTIVE_PAIRS,
        "max_gross_leverage": base.MAX_GROSS_LEVERAGE,
        "support": support,
        "profiles": results,
        "selected_profile": selected["profile"] if selected else None,
        "selected_profile_id": selected["profile_id"] if selected else None,
        "selected_prior_pairs": selected["prior_pairs"] if selected else [],
        "selected_current_pairs": selected["current_pairs"] if selected else [],
        "selected_prior_forward_train": (selected["prior_forward_train"] if selected else None),
        "selected_oos_unlevered": selected["oos"] if selected else None,
        "selected_full_recent_unlevered": selected["full_recent"] if selected else None,
    }

    reasons: list[str] = []
    if selected is None:
        report["selected_leverage"] = 0.0
        report["selected_oos"] = None
        report["selected_full_recent"] = None
        reasons.append("no rotation profile survives all pre-OOS gates")
    else:
        profile = base.PairProfile(**selected["profile"])
        selected_current = _rotating_portfolio(
            close,
            returns,
            selected["current_pairs"],
            profile,
            statistics_cache,
            pair_lookup,
            cost_bps=base.STRESS_COST_BPS,
        )
        selection_start, selection_end = base.WINDOWS["selection_full"]
        calibration = selected_current.loc[
            pd.Timestamp(selection_start) : pd.Timestamp(selection_end)
        ]
        leverage = base._choose_leverage(calibration)
        report["selected_leverage"] = leverage
        scaled = selected_current * leverage if leverage > 0.0 else selected_current * 0.0
        report["selected_oos"] = base._window_metrics(scaled, "oos")
        report["selected_full_recent"] = base._window_metrics(scaled, "full_recent")
        if leverage <= 0.0:
            reasons.append("no leverage level satisfies calibration drawdown gate")
        if support["eligible_profiles"] < 2:
            reasons.append("rotation profile neighborhood support is below two")
        if report["selected_oos"]["annualized_return"] <= 0.0:
            reasons.append("selected rotation profile final OOS return is not positive")
        if report["selected_full_recent"]["annualized_return"] < 1.0:
            reasons.append("stressed two-year annualized return is below 100%")
        if report["selected_full_recent"]["max_drawdown"] <= base.MAX_TARGET_DRAWDOWN:
            reasons.append("stressed two-year drawdown exceeds 20%")

    report["target"] = {
        "annualized_return": 1.0,
        "max_drawdown": base.MAX_TARGET_DRAWDOWN,
        "target_met": not reasons,
        "reasons": reasons,
    }
    return report


def main() -> None:
    path = Path("runtime/broad_daily_universe.csv")
    if not path.exists():
        raise SystemExit("broad daily universe missing; run fetch_broad_daily_universe.py")
    report = evaluate(pd.read_csv(path))
    output = Path("runtime/broad_pair_rotation_report.json")
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "support": report["support"],
                "selected_profile": report["selected_profile"],
                "selected_prior_pairs": report["selected_prior_pairs"],
                "selected_current_pairs": report["selected_current_pairs"],
                "selected_prior_forward_train": report["selected_prior_forward_train"],
                "selected_oos_unlevered": report["selected_oos_unlevered"],
                "selected_full_recent_unlevered": report["selected_full_recent_unlevered"],
                "selected_leverage": report["selected_leverage"],
                "selected_oos": report["selected_oos"],
                "selected_full_recent": report["selected_full_recent"],
                "target": report["target"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
