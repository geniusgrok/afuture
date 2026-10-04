"""Causal one-lot two-leg paper episodes from preserved daily contract rows.

Episodes are independent per product. Their sum is not a portfolio return. Daily
opens are simulated fills and do not certify atomic execution or intraday risk.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def spread_signal(history: np.ndarray, latest: float, gross: float) -> tuple[int, float]:
    """A fixed 63-session, one-sigma hypothesis with a four-leg Stress hurdle."""
    if len(history) != 63 or not np.isfinite(history).all():
        raise ValueError("signal requires exactly 63 complete prior observations")
    if not np.isfinite([latest, gross]).all() or gross <= 0:
        raise ValueError("latest mark and gross notional must be finite and positive")
    anchor, std = float(history.mean()), float(history.std(ddof=1))
    deviation = latest - anchor
    if std <= 0 or abs(deviation) <= max(std, gross * 0.003):
        return 0, anchor
    return (-1 if deviation > 0 else 1), anchor


def eligible(row: dict, source_day: pd.Timestamp) -> bool:
    return (
        row["volume"] >= 1000 and row["hold"] >= 5000 and (row["delivery"] - source_day).days >= 20
    )


def readiness(values: list[float]) -> dict:
    if len(values) < 4:
        return {"count": len(values), "ready": False, "reason": "fewer_than_four"}
    array = np.array(values)
    rng = np.random.default_rng(20261004)
    means = rng.choice(array, size=(10000, len(array)), replace=True).mean(axis=1)
    lower = float(np.quantile(means, 0.025))
    return {
        "count": len(values),
        "median": float(np.median(array)),
        "mean": float(array.mean()),
        "lower95_bootstrap": lower,
        "ready": bool(np.median(array) > 0 and lower > 0),
        "scope": "descriptive development gate; no independence or statistical certification",
    }


def paper_episodes(
    market: pd.DataFrame,
    selections: pd.DataFrame,
    days: pd.DatetimeIndex,
    units: dict[str, float],
    *,
    mechanism: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    if market.duplicated(["date", "symbol"]).any():
        raise ValueError("ambiguous contract versions")
    prices = market.set_index(["date", "symbol"])
    close = market.pivot(index="date", columns="symbol", values="close")
    calendar = pd.DatetimeIndex(sorted(market.date.unique()))
    picks = selections.set_index(["date", "product"])
    if picks.index.has_duplicates:
        raise ValueError("ambiguous pair selection")
    active, episodes, marks, audit = {}, [], [], []
    products = sorted(selections["product"].unique())

    def row(day, symbol):
        if (day, symbol) not in prices.index:
            raise ValueError(f"missing held-contract valuation: {day.date()} {symbol}")
        value = prices.loc[(day, symbol)].to_dict()
        if (
            not np.isfinite([value["open"], value["close"]]).all()
            or min(value["open"], value["close"]) <= 0
        ):
            raise ValueError("invalid specific price")
        return value

    def mark(episode, day, action, before, after):
        for symbol, sign in (
            (episode["symbol_near"], episode["direction"]),
            (episode["symbol_far"], -episode["direction"]),
        ):
            value = row(day, symbol)
            unit = units[value["product"]]
            if action in ("entry", "exit"):
                notional = value["open"] * unit
                gross_pnl = 0.0
                episode["turnover"] += notional
            else:
                notional = 0.0
                prior = row(episode["last_day"], symbol) if action == "gap" else None
                change = value["open"] - prior["close"] if prior else value["close"] - value["open"]
                gross_pnl = sign * change * unit
                episode["gross_pnl"] += gross_pnl
            marks.append(
                {
                    "mechanism": mechanism,
                    "episode": episode["id"],
                    "date": day,
                    "symbol": symbol,
                    "action": action,
                    "unit": unit,
                    "lots_before": sign * before,
                    "lots_after": sign * after,
                    "price": value["close"] if action == "intraday" else value["open"],
                    "turnover": notional,
                    "gross_pnl": gross_pnl,
                }
            )

    for day in days:
        previous = calendar[calendar < day]
        if len(previous) == 0:
            continue
        source_day = previous[-1]
        for product in products:
            if product in active:
                episode = active[product]
                mark(episode, day, "gap", 1, 1)
                near, far = episode["symbol_near"], episode["symbol_far"]
                old_near, old_far = row(source_day, near), row(source_day, far)
                delta = old_near["close"] - old_far["close"]
                crossed = episode["direction"] * (delta - episode["anchor"]) >= 0
                lost = not eligible(old_near, source_day) or not eligible(old_far, source_day)
                if crossed or lost or episode["holding_sessions"] >= 20:
                    mark(episode, day, "exit", 1, 0)
                    episode.update(
                        exit_day=day,
                        complete=True,
                        exit_reason="anchor_cross"
                        if crossed
                        else "eligibility"
                        if lost
                        else "max_holding",
                    )
                    episodes.append(episode)
                    del active[product]
                    # No same-day re-entry after exit; prevents overlapping observations.
                    continue
                mark(episode, day, "intraday", 1, 1)
                episode["holding_sessions"] += 1
                episode["last_day"] = day
                continue
            if (source_day, product) not in picks.index:
                audit.append({"day": day, "product": product, "reason": "no_prior_eligible_pair"})
                continue
            pick = picks.loc[(source_day, product)]
            near, far = pick.symbol_near, pick.symbol_far
            prior_near, prior_far = row(source_day, near), row(source_day, far)
            history_days = calendar[calendar <= source_day][-63:]
            if len(history_days) != 63 or near not in close or far not in close:
                audit.append({"day": day, "product": product, "reason": "same_pair_warmup"})
                continue
            history = close.reindex(history_days)[near] - close.reindex(history_days)[far]
            if history.isna().any():
                audit.append({"day": day, "product": product, "reason": "same_pair_warmup"})
                continue
            unit = units[prior_near["product"]]
            if unit != units[prior_far["product"]]:
                raise ValueError("one-lot pair has unequal physical units")
            gross = (prior_near["close"] + prior_far["close"]) * unit
            direction, anchor = spread_signal(
                history.to_numpy() * unit, float(history.iloc[-1]) * unit, gross
            )
            if direction == 0:
                audit.append(
                    {"day": day, "product": product, "reason": "no_cost_supported_deviation"}
                )
                continue
            today_near, today_far = row(day, near), row(day, far)
            entry_gross = (today_near["open"] + today_far["open"]) * unit
            if entry_gross > 100000.0:
                audit.append(
                    {
                        "day": day,
                        "product": product,
                        "reason": "one_pair_exceeds_budget",
                        "gross": entry_gross,
                    }
                )
                continue
            if entry_gross * 0.15 * 1.25 > 0.35 * 500000.0:
                raise ValueError("one pair exceeds conservative margin envelope")
            episode = {
                "id": f"{mechanism}:{product}:{day.date()}",
                "mechanism": mechanism,
                "product": product,
                "symbol_near": near,
                "symbol_far": far,
                "signal_day": source_day,
                "entry_day": day,
                "direction": direction,
                "anchor": anchor / unit,
                "gross_pnl": 0.0,
                "turnover": 0.0,
                "holding_sessions": 1,
                "last_day": day,
                "entry_gross": entry_gross,
                "stress_margin_no_offset": entry_gross * 0.15 * 1.25,
            }
            mark(episode, day, "entry", 0, 1)
            mark(episode, day, "intraday", 1, 1)
            active[product] = episode
    for episode in active.values():
        episode.update(exit_day=pd.NaT, complete=False, exit_reason="open_at_period_end")
        episodes.append(episode)
    for episode in episodes:
        episode["base_cost"] = episode["turnover"] * 0.0005
        episode["stress_cost"] = episode["turnover"] * 0.0015
        episode["base_net"] = episode["gross_pnl"] - episode["base_cost"]
        episode["stress_net"] = episode["gross_pnl"] - episode["stress_cost"]
    frame = pd.DataFrame(episodes)
    mark_frame = pd.DataFrame(marks)
    groups = {}
    if not frame.empty:
        for (product, direction), group in frame.loc[frame.complete].groupby(
            ["product", "direction"]
        ):
            groups[f"{product}:{direction}"] = readiness(group.stress_net.tolist())
        for episode in episodes:
            owned = mark_frame.loc[mark_frame.episode == episode["id"]]
            if (
                abs(owned.gross_pnl.sum() - episode["gross_pnl"]) > 1e-6
                or abs(owned.turnover.sum() - episode["turnover"]) > 1e-6
            ):
                raise ValueError("two-leg event totals do not reconcile")
            for symbol, sign in (
                (episode["symbol_near"], episode["direction"]),
                (episode["symbol_far"], -episode["direction"]),
            ):
                start = row(episode["entry_day"], symbol)["open"]
                end_day = episode["exit_day"] if episode["complete"] else episode["last_day"]
                end_price = row(end_day, symbol)["open" if episode["complete"] else "close"]
                expected = sign * (end_price - start) * units[row(end_day, symbol)["product"]]
                actual = owned.loc[owned.symbol == symbol, "gross_pnl"].sum()
                if abs(expected - actual) > 1e-6:
                    raise ValueError("two-leg marks differ from entry/exit identity")
    summary = {
        "mechanism": mechanism,
        "episodes": len(frame),
        "completed": int(frame.complete.sum()) if not frame.empty else 0,
        "readiness": groups,
        "event_and_price_identity_passed": True,
        "scope": "isolated paper episodes; neither portfolio return nor production replay",
    }
    return frame, mark_frame, pd.DataFrame(audit), summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", type=Path, required=True)
    parser.add_argument("--selections", type=Path, required=True)
    parser.add_argument("--units", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mechanism", required=True)
    args = parser.parse_args()
    market = pd.read_csv(args.market, parse_dates=["date", "delivery"])
    picks = pd.read_csv(args.selections, parse_dates=["date"])
    units = pd.read_csv(args.units).set_index("product").account_multiplier_used.to_dict()
    days = pd.DatetimeIndex(sorted(market.date.unique()))
    days = days[(days >= "2022-09-06") & (days <= "2026-09-30")]
    frame, marks, audit, summary = paper_episodes(
        market, picks, days, units, mechanism=args.mechanism
    )
    args.output.mkdir(exist_ok=False, parents=True)
    frame.to_csv(args.output / "episodes.csv", index=False)
    marks.to_csv(args.output / "events.csv", index=False)
    audit.to_csv(args.output / "decision_audit.csv", index=False)
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
