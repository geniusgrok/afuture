"""Offline fixed-budget comparison and fill-owned holding attribution.

This tool consumes already preserved research inputs. It does not select parameters,
change the production policy, or connect to a broker.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from afuture.directional_acceptance import PRODUCT_MULTIPLIERS, ProductionMechanicsConfig
from afuture.directional_concentration_freeze import (
    ExpandingMedianConcentrationFreezeDirectionalProductionAcceptance as Account,
)
from afuture.directional_stress90_policy import (
    apply_cost_gate_row,
    apply_oi_confirmation_row,
    reallocate_survivor_row,
    target_weight_concentration,
)
from afuture.execution_aligned_policy import FROZEN_PRODUCTS, ExecutionAlignedAggressivePolicy


def read_weights(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, index_col=0, parse_dates=True, float_precision="round_trip")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def account_metrics(daily: pd.DataFrame, events: pd.DataFrame) -> dict:
    equity = daily.equity.to_numpy(float)
    path = np.r_[500000.0, equity]
    gross = float(events.loc[events.kind == "pnl", "gross_pnl"].sum())
    fees = float(events.loc[events.kind == "trade", "transaction_cost"].sum())
    final = float(equity[-1])
    if abs(final - 500000.0 - gross + fees) > 1e-6:
        raise ValueError("event cash does not reconcile")
    return {
        "final_equity": final,
        "net_profit": final - 500000.0,
        "return": final / 500000.0 - 1.0,
        "mdd": float((1.0 - path / np.maximum.accumulate(path)).max()),
        "gross_pnl": gross,
        "fees": fees,
        "days": len(daily),
        "halted": bool(daily.halted.any()),
    }


def verify_events(
    daily: pd.DataFrame, events: pd.DataFrame, market: pd.DataFrame, cost_bps: float
) -> dict:
    """Reconstruct each same-contract mark and filled position independently."""
    prices = market.set_index(["date", "symbol"])
    cash = 500000.0
    lots: dict[str, int] = {}
    prior_day = None
    error = 0.0
    for day in daily.index:
        marked_intraday = set()
        for event in events.loc[events.date == day].to_dict("records"):
            symbol = event["symbol"]
            before, after = int(event["lots_before"]), int(event["lots_after"])
            unit = PRODUCT_MULTIPLIERS[event["product"]]
            if lots.get(symbol, 0) != before:
                raise ValueError("event changes an unowned position")
            if event["kind"] == "trade":
                delta = int(event["delta_lots"])
                if delta != after - before or abs(after) > 35:
                    raise ValueError("invalid integer fill")
                row = prices.loc[(day, symbol)]
                if symbol in marked_intraday:
                    if abs(after) >= abs(before) or before * after < 0:
                        raise ValueError("post-mark fill must reduce risk")
                    fill_price = float(row.close)
                else:
                    fill_price = float(row.open)
                if abs(float(event["price"]) - fill_price) > 1e-9:
                    raise ValueError("fill is not the true specific-contract phase price")
                fee = abs(delta) * fill_price * unit * cost_bps / 10000.0
                if abs(fee - event["transaction_cost"]) > 1e-6:
                    raise ValueError("fill fee mismatch")
                cash -= fee
                lots[symbol] = after
            elif event["kind"] == "pnl":
                if before != after:
                    raise ValueError("mark changes position")
                row = prices.loc[(day, symbol)]
                if event["action"] == "gap":
                    if prior_day is None:
                        raise ValueError("flat account has inherited gap")
                    change = row.open - prices.loc[(prior_day, symbol), "close"]
                elif event["action"] == "intraday":
                    change = row.close - row.open
                    marked_intraday.add(symbol)
                else:
                    raise ValueError("unknown mark action")
                pnl = before * change * unit
                if abs(pnl - event["gross_pnl"]) > 1e-6:
                    raise ValueError("same-contract pnl mismatch")
                cash += pnl
            else:
                raise ValueError("unknown event kind")
        error = max(error, abs(cash - daily.loc[day, "equity"]))
        if error > 1e-6:
            raise ValueError("daily equity mismatch")
        prior_day = day
    return {"passed": True, "cash_max_error": error, "ending_lots": lots}


def holding_episodes(events: pd.DataFrame) -> pd.DataFrame:
    """Attribute contiguous symbol/side exposure; distinguish unfinished episodes."""
    active: dict[str, dict] = {}
    result = []
    for event in events.to_dict("records"):
        symbol = event["symbol"]
        if symbol not in active:
            if event["kind"] != "trade" or int(event["lots_before"]) != 0:
                raise ValueError("episode has no entry fill")
            active[symbol] = {
                "product": event["product"],
                "symbol": symbol,
                "entry_day": str(event["date"]),
                "side": event["side"],
                "gap_pnl": 0.0,
                "intraday_pnl": 0.0,
                "fees": 0.0,
                "max_close_net": 0.0,
                "min_close_net": 0.0,
            }
        episode = active[symbol]
        if event["kind"] == "trade":
            episode["fees"] += float(event["transaction_cost"])
            if int(event["lots_after"]) == 0:
                episode["exit_day"] = str(event["date"])
                episode["complete"] = True
                episode["net_pnl"] = episode["gap_pnl"] + episode["intraday_pnl"] - episode["fees"]
                result.append(episode)
                del active[symbol]
        else:
            episode[event["action"] + "_pnl"] += float(event["gross_pnl"])
            if event["action"] == "intraday":
                value = episode["gap_pnl"] + episode["intraday_pnl"] - episode["fees"]
                episode["max_close_net"] = max(episode["max_close_net"], value)
                episode["min_close_net"] = min(episode["min_close_net"], value)
    for episode in active.values():
        episode["complete"] = False
        episode["exit_day"] = ""
        episode["net_pnl"] = episode["gap_pnl"] + episode["intraday_pnl"] - episode["fees"]
        result.append(episode)
    output = pd.DataFrame(result)
    if (
        abs(
            output.net_pnl.sum()
            - (
                events.loc[events.kind == "pnl", "gross_pnl"].sum()
                - events.loc[events.kind == "trade", "transaction_cost"].sum()
            )
        )
        > 1e-6
    ):
        raise ValueError("episode attribution does not reconcile")
    return output


def extend_layers(base: pd.DataFrame, close: pd.DataFrame, flow: pd.DataFrame, layers: dict):
    """Continue archived signal-only state, with no account wealth inherited."""
    cutoff = layers["survivor"].index[-1]
    previous = {key: value.iloc[-1].to_dict() for key, value in layers.items()}
    rows: dict[str, list] = {key: [] for key in layers}
    days = base.index[base.index > cutoff]
    for day in days:
        prior = close.loc[close.index < day]
        if prior.empty or prior.index[-1] not in flow.index:
            raise ValueError("no immediately prior completed flow")
        source_flow = flow.loc[prior.index[-1]]
        if source_flow.isna().any():
            raise ValueError("incomplete OI flow")
        oi = apply_oi_confirmation_row(
            raw_weights=base.loc[day].to_dict(),
            prior_applied=previous["oi_confirmed"],
            completed_flow=source_flow.to_dict(),
            supported_products=source_flow.index,
        )
        approved = apply_cost_gate_row(
            oi_weights=oi,
            prior_approved=previous["cost_approved"],
            completed_close_history={p: prior[p].to_numpy(float) for p in base.columns},
        )
        survivor = reallocate_survivor_row(
            oi_weights=oi, approved_weights=approved, prior_survivor=previous["survivor"]
        )
        previous = {"oi_confirmed": oi, "cost_approved": approved, "survivor": survivor}
        for key in rows:
            rows[key].append(previous[key])
    return {
        key: pd.concat([value, pd.DataFrame(rows[key], index=days, columns=base.columns)])
        for key, value in layers.items()
    }


def recent_comparison(baseline: Path, prior: Path, output: Path) -> dict:
    output.mkdir(exist_ok=False, parents=True)
    continuous = pd.read_csv(baseline / "market_normalized/continuous.csv", parse_dates=["date"])
    market = pd.read_csv(
        baseline / "market_normalized/specific.csv", parse_dates=["date", "delivery"]
    )
    units = pd.read_csv(baseline / "coverage/account_multiplier_audit.csv")
    PRODUCT_MULTIPLIERS.update(units.set_index("product").account_multiplier_used.to_dict())
    close = continuous.pivot(index="date", columns="product", values="close")
    opens = continuous.pivot(index="date", columns="product", values="open")
    flow = read_weights(baseline / "strategy/completed_oi_flow.csv")
    days = read_weights(baseline / "strategy/target_weights.csv").index
    summaries, checks = {}, {}
    for pool in ("full", "exAG"):
        prefix = "B0" + ("_exAG" if pool == "exAG" else "")
        base = read_weights(baseline / "strategy/base_weights.csv")
        if pool == "exAG":
            products = tuple(p for p in FROZEN_PRODUCTS if p != "AG")
            base = ExecutionAlignedAggressivePolicy(products=products).weight_history(opens, close)
            base = base.reindex(columns=FROZEN_PRODUCTS, fill_value=0.0)
            archived = read_weights(prior / (prefix + "_base_weights.csv"))
            difference = np.abs(base.loc[archived.index].to_numpy() - archived.to_numpy()).max()
            if difference > 1e-12:
                raise ValueError("independent exAG upstream does not reproduce archived prefix")
            checks["exAG_base_prefix"] = float(difference)
        layers = {
            key: read_weights(prior / (prefix + "_" + key + "_weights.csv"))
            for key in ("oi_confirmed", "cost_approved", "survivor")
        }
        extended = extend_layers(base, close, flow, layers)
        if pool == "full":
            archived = read_weights(baseline / "strategy/target_weights.csv")
            difference = np.abs(
                extended["survivor"].loc[days].to_numpy() - archived.to_numpy()
            ).max()
            if difference > 1e-12:
                raise ValueError("B0 new suffix differs from verified account targets")
            checks["B0_full_tail"] = float(difference)
        sample = market.loc[market["product"] != "AG"] if pool == "exAG" else market
        for variant, layer in (("B0", "survivor"), ("C4B", "cost_approved")):
            weights = extended[layer]
            if pool == "exAG" and weights.AG.abs().max() != 0:
                raise ValueError("exAG contains excluded risk")
            warmup = [
                h
                for h in (
                    target_weight_concentration(row.to_dict())
                    for _, row in weights.loc[weights.index < days[0]].iterrows()
                )
                if h is not None
            ]
            for cost_name, cost, margin in (("base", 5, 0.12), ("stress", 15, 0.15)):
                label = f"{variant}_{pool}_{cost_name}"
                folder = output / label
                folder.mkdir()
                if variant == "B0" and pool == "full":
                    daily = read_weights(baseline / f"account/{cost_name}/daily.csv")
                    events = pd.read_csv(
                        baseline / f"account/{cost_name}/events.csv",
                        parse_dates=["date"],
                        float_precision="round_trip",
                    )
                    reuse = True
                else:
                    account = Account(
                        ProductionMechanicsConfig(margin_rate_proxy=margin),
                        completed_concentrations=warmup,
                    )
                    result = account.simulate(sample, weights.loc[days], cost_bps=cost)
                    daily, events = result.daily, result.events
                    events["date"] = pd.to_datetime(events.date)
                    reuse = False
                daily.to_csv(folder / "daily.csv", index_label="date")
                events.to_csv(folder / "events.csv", index=False)
                weights.to_csv(folder / "all_weights.csv", index_label="date")
                holding_episodes(events).to_csv(folder / "holding_episodes.csv", index=False)
                checks[label] = verify_events(daily, events, sample, cost)
                summaries[label] = {**account_metrics(daily, events), "reused": reuse}
                print(label, summaries[label], flush=True)
    write_json(output / "summary.json", summaries)
    write_json(output / "checks.json", checks)
    return summaries


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--prior", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    recent_comparison(args.baseline, args.prior, args.output)
