"""Offline three-leg fallback accounts when fundamental/event inputs are blocked.

Price-only butterflies are explicitly not fundamentally anchored strategies.
Daily open fills, proportional costs and margins remain research proxies. This
tool does not import or change the live account, order or risk execution graph.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from adaptive_alpha_research import account_metrics, write_json

from afuture.new_mechanisms import butterfly_lots

LEGS = (1, -2, 1)


def observations(market: pd.DataFrame, *, minimum_delivery_days: int = 20) -> pd.DataFrame:
    if market.duplicated(["date", "symbol"]).any():
        raise ValueError("ambiguous concrete-contract rows")
    calendar = pd.DatetimeIndex(sorted(market.date.unique()))
    closes = market.pivot(index="date", columns="symbol", values="close")
    opens = market.pivot(index="date", columns="symbol", values="open")
    rows = []
    for index, day in enumerate(calendar[:-1]):
        prior = market.loc[market.date == day]
        prior = prior.loc[
            (prior.volume >= 1000)
            & (prior.hold >= 5000)
            & ((prior.delivery - day).dt.days >= minimum_delivery_days)
        ]
        for product, group in prior.groupby("product"):
            chosen = group.sort_values(["volume", "symbol"], ascending=[False, True]).head(3)
            if len(chosen) != 3:
                continue
            chosen = chosen.sort_values("delivery")
            months = tuple(int(value.year * 12 + value.month) for value in chosen.delivery)
            try:
                butterfly_lots(months)
            except ValueError:
                continue
            symbols = tuple(chosen.symbol)
            prices = chosen.close.to_numpy(dtype=float)
            gross = float(np.dot(np.abs(LEGS), prices))
            curve = float(np.dot(LEGS, prices))
            old = (
                closes.reindex(index=[calendar[index - 20]], columns=list(symbols)).to_numpy()[0]
                if index >= 20
                else np.full(3, np.nan)
            )
            change = (
                (curve - float(np.dot(LEGS, old))) / gross if np.isfinite(old).all() else np.nan
            )
            entry_day = calendar[index + 1]
            maturity = calendar[index + 5] if index + 5 < len(calendar) else pd.NaT
            target = np.nan
            if pd.notna(maturity):
                start = opens.reindex(index=[entry_day], columns=list(symbols)).to_numpy()[0]
                end = closes.reindex(index=[maturity], columns=list(symbols)).to_numpy()[0]
                if np.isfinite(start).all() and np.isfinite(end).all() and min(start) > 0:
                    target = float(np.dot(LEGS, end - start) / np.dot(np.abs(LEGS), start))
            rows.append(
                {
                    "date": entry_day,
                    "source_day": day,
                    "product": product,
                    "near": symbols[0],
                    "middle": symbols[1],
                    "far": symbols[2],
                    "curve": curve,
                    "gross_points": gross,
                    "x1": curve / gross,
                    "x2": change,
                    "maturity_day": maturity,
                    "target": target,
                }
            )
    return pd.DataFrame(rows)


def forecasts(rows: pd.DataFrame, calendar: pd.DatetimeIndex, mechanism: str, pool: str):
    excluded = {"AG"} if pool == "exAG" else set()
    if mechanism == "BFL2_EXFGJM":
        excluded.update(("FG", "JM"))
    data = rows.loc[~rows["product"].isin(excluded)].copy()
    outputs, audit = [], []
    for day in calendar:
        current = data.loc[data.date == day].copy()
        if current.empty:
            continue
        if mechanism == "BFL1":
            current["forecast"] = -current.x1
            audit.append({"date": day, "fit": "structural_curvature", "rows": 0})
        else:
            previous = pd.DatetimeIndex(sorted(data.loc[data.date < day, "date"].unique()))
            start = (
                previous[-252] if len(previous) >= 252 else previous[0] if len(previous) else day
            )
            train = data.loc[
                (data.date >= start)
                & (data.maturity_day < day)
                & np.isfinite(data[["x1", "x2", "target"]]).all(axis=1)
            ]
            if train.date.nunique() < 126 or len(train) < 200:
                audit.append(
                    {"date": day, "fit": "insufficient_mature_history", "rows": len(train)}
                )
                continue
            x = train[["x1", "x2"]].to_numpy()
            y = train.target.to_numpy()
            # Equal training-date mass, independent of a day's available product count.
            weights = (1 / train.groupby("date").date.transform("size")).to_numpy()
            weights /= weights.sum()
            mean_x, mean_y = weights @ x, float(weights @ y)
            centered = x - mean_x
            # Dimensionless feature standardization uses only mature training rows.
            scale = np.sqrt(weights @ (centered * centered))
            scale = np.maximum(scale, 1e-6)
            z = centered / scale
            coefficient = np.linalg.solve(
                (z.T * weights) @ z + np.eye(2), (z.T * weights) @ (y - mean_y)
            )
            current = current.loc[np.isfinite(current[["x1", "x2"]]).all(axis=1)]
            current["forecast"] = (
                mean_y + ((current[["x1", "x2"]].to_numpy() - mean_x) / scale) @ coefficient
            )
            audit.append(
                {
                    "date": day,
                    "fit": "mature_ridge",
                    "rows": len(train),
                    "dates": train.date.nunique(),
                    "max_maturity": train.maturity_day.max(),
                    "excluded": ",".join(sorted(excluded)),
                    "intercept": mean_y,
                    "b1": coefficient[0],
                    "b2": coefficient[1],
                }
            )
        outputs.append(current)
    return pd.concat(outputs, ignore_index=True) if outputs else data.iloc[:0].assign(
        forecast=0.0
    ), pd.DataFrame(audit)


def replay(market, predictions, days, units, *, cost_bps, margin, mechanism, failure_output=None):
    quotes = market.set_index(["date", "symbol"]).to_dict("index")
    calendar = pd.DatetimeIndex(sorted(market.date.unique()))
    previous = {day: calendar[index - 1] for index, day in enumerate(calendar) if index}
    grouped = {day: group for day, group in predictions.groupby("date")}
    active, events, ledger, decisions = {}, [], [], []
    cash, peak, halted, count = 500000.0, 500000.0, False, 0

    def preserve_failure():
        if failure_output is not None:
            pd.DataFrame(events).to_csv(failure_output / "partial_events.csv", index=False)
            pd.DataFrame(ledger).to_csv(failure_output / "partial_ledger.csv", index=False)
            pd.DataFrame(decisions).to_csv(failure_output / "partial_decisions.csv", index=False)
            write_json(
                failure_output / "partial_state.json",
                {
                    "cash": cash,
                    "peak": peak,
                    "halted": halted,
                    "active": {
                        product: {**basket, "last_day": str(basket["last_day"])}
                        for product, basket in active.items()
                    },
                    "qualification": "interrupted account, not an economic result",
                },
            )

    def quote(day, symbol):
        value = quotes.get((day, symbol))
        if (
            value is None
            or not np.isfinite([value["open"], value["close"]]).all()
            or min(value["open"], value["close"]) <= 0
        ):
            preserve_failure()
            raise ValueError(f"missing/invalid held concrete quote {day} {symbol}")
        return value

    def mark(basket, day, action):
        nonlocal cash
        for symbol, signed in zip(basket["symbols"], basket["lots"], strict=True):
            value = quote(day, symbol)
            unit = units[basket["product"]]
            price = value["close"] if action == "intraday" else value["open"]
            if action in ("entry", "exit"):
                if value["volume"] < abs(signed):
                    preserve_failure()
                    raise ValueError(
                        "chosen fill lacks even total-day volume; evidence invalid, not skipped"
                    )
                turnover = abs(signed) * unit * price
                pnl = 0.0
            else:
                prior = (
                    quote(basket["last_day"], symbol)["close"] if action == "gap" else value["open"]
                )
                pnl, turnover = signed * unit * (price - prior), 0.0
            fee = turnover * cost_bps / 10000
            cash += pnl - fee
            events.append(
                {
                    "date": day,
                    "basket": basket["id"],
                    "product": basket["product"],
                    "symbol": symbol,
                    "action": action,
                    "unit": unit,
                    "price": price,
                    "lots": signed,
                    "pnl": pnl,
                    "turnover": turnover,
                    "fee": fee,
                }
            )

    def gross(day, phase):
        return sum(
            abs(lot) * units[b["product"]] * quote(day, symbol)[phase]
            for b in active.values()
            for symbol, lot in zip(b["symbols"], b["lots"], strict=True)
        )

    for day in days:
        start_event = len(events)
        before = cash
        source = previous.get(day)
        for basket in active.values():
            mark(basket, day, "gap")
        forced = halted or cash <= 0 or 1 - cash / peak >= 0.20 or day == days[-1]
        if forced and day != days[-1]:
            halted = True
        for product, basket in list(active.items()):
            prices = [quote(source, symbol)["close"] for symbol in basket["symbols"]]
            curve = float(np.dot(LEGS, prices))
            cross = mechanism == "BFL1" and basket["direction"] * curve >= 0
            loss_stop = basket["direction"] * (curve - basket["entry_curve"]) < -2 * abs(
                basket["entry_curve"]
            )
            if forced or basket["age"] >= 5 or cross or loss_stop:
                mark(basket, day, "exit")
                del active[product]
        # Reduction precedes any new allocation; no spread margin offsets assumed.
        for product in sorted(list(active), reverse=True):
            if gross(day, "open") <= min(1000000, max(cash, 0) * 0.35 / margin):
                break
            mark(active[product], day, "exit")
            del active[product]
        candidates = grouped.get(day, predictions.iloc[:0]).sort_values(
            ["forecast", "product"],
            key=lambda s: s.abs() if s.name == "forecast" else s,
            ascending=[False, True],
        )
        for row in candidates.itertuples():
            if forced or halted or row.product in active or abs(row.forecast) <= 0.003:
                continue
            if row.source_day != source:
                raise ValueError("prediction is not from the completed previous trading day")
            direction = 1 if row.forecast > 0 else -1
            symbols = (row.near, row.middle, row.far)
            # New-order opening quotes are observable at execution; this is a risk
            # check on actual price, never a filter on the day's future outcomes.
            notional = sum(
                abs(n) * quote(day, s)["open"] * units[row.product]
                for n, s in zip(LEGS, symbols, strict=True)
            )
            reason = "accepted"
            if row.gross_points * units[row.product] > 100000 or notional > 100000:
                reason = "one_basket_capacity"
            elif gross(day, "open") + notional > min(
                1000000, max(cash - notional * cost_bps / 10000, 0) * 0.35 / margin
            ):
                reason = "shared_account_capacity"
            decisions.append(
                {
                    "date": day,
                    "product": row.product,
                    "forecast": row.forecast,
                    "entry_notional": notional,
                    "reason": reason,
                }
            )
            if reason != "accepted":
                continue
            count += 1
            basket = {
                "id": count,
                "product": row.product,
                "symbols": symbols,
                "lots": tuple(direction * n for n in LEGS),
                "direction": direction,
                "entry_curve": row.curve,
                "age": 0,
                "last_day": day,
            }
            mark(basket, day, "entry")
            active[row.product] = basket
        for basket in active.values():
            mark(basket, day, "intraday")
            basket["last_day"] = day
            basket["age"] += 1
        peak = max(peak, cash)
        if 1 - cash / peak >= 0.20 or cash <= 0:
            halted = True
        today = events[start_event:]
        ledger.append(
            {
                "date": day,
                "equity": cash,
                "gross_pnl": sum(e["pnl"] for e in today),
                "fees": sum(e["fee"] for e in today),
                "turnover": sum(e["turnover"] for e in today),
                "daily_return": cash / before - 1,
                "gross_notional": gross(day, "close"),
                "margin": gross(day, "close") * margin,
                "halted": halted,
                "active_baskets": len(active),
            }
        )
    return (
        pd.DataFrame(ledger),
        pd.DataFrame(
            events,
            columns=[
                "date",
                "basket",
                "product",
                "symbol",
                "action",
                "unit",
                "price",
                "lots",
                "pnl",
                "turnover",
                "fee",
            ],
        ),
        pd.DataFrame(
            decisions, columns=["date", "product", "forecast", "entry_notional", "reason"]
        ),
    )


def verify_account(market, ledger, events, units, cost_bps):
    """Independent ownership/cash reconstruction directly from original quotes."""
    quote = market.set_index(["date", "symbol"])
    owned, last_close = {}, {}
    daily, cash, max_error = {}, 500000.0, 0.0
    for event in events.itertuples():
        key = (event.basket, event.symbol)
        value = quote.loc[(event.date, event.symbol)]
        assert event.unit == units[event.product]
        assert isinstance(event.lots, int) and event.lots != 0
        assert event.price == value["close" if event.action == "intraday" else "open"]
        expected_pnl, turnover = 0.0, 0.0
        if event.action == "entry":
            assert key not in owned
            owned[key] = event.lots
            turnover = abs(event.lots) * event.unit * event.price
        else:
            assert owned[key] == event.lots
            if event.action == "exit":
                turnover = abs(event.lots) * event.unit * event.price
                del owned[key]
            else:
                prior = last_close[key] if event.action == "gap" else value["open"]
                expected_pnl = event.lots * event.unit * (event.price - prior)
                if event.action == "intraday":
                    last_close[key] = event.price
        assert abs(expected_pnl - event.pnl) < 1e-8
        assert abs(turnover - event.turnover) < 1e-8
        assert abs(turnover * cost_bps / 10000 - event.fee) < 1e-8
        daily[event.date] = daily.get(event.date, 0.0) + expected_pnl - event.fee
    assert not owned, "final liquidation must close all concrete legs"
    for row in ledger.itertuples():
        before = cash
        cash += daily.get(row.date, 0.0)
        max_error = max(max_error, abs(cash - row.equity))
        assert abs(row.daily_return - (cash / before - 1)) < 1e-10
    assert max_error < 1e-6
    return {
        "cash_error": max_error,
        "owned_fills_and_actual_quotes_verified": True,
        "all_final_legs_flat": True,
        "events": len(events),
    }


def run(
    inputs: Path,
    units_path: Path,
    output: Path,
    *,
    observations_path=None,
    minimum_delivery_days=20,
):
    output.mkdir(exist_ok=False, parents=True)
    market = pd.read_csv(inputs, parse_dates=["date", "delivery"], float_precision="round_trip")
    units_frame = pd.read_csv(units_path)
    units = dict(zip(units_frame["product"], units_frame.account_multiplier_used, strict=True))
    calendar = pd.DatetimeIndex(sorted(market.date.unique()))
    rows = (
        pd.read_csv(
            observations_path,
            parse_dates=["date", "source_day", "maturity_day"],
            float_precision="round_trip",
        )
        if observations_path is not None
        else observations(market, minimum_delivery_days=minimum_delivery_days)
    )
    rows.to_csv(output / "observations.csv", index=False)
    results = []
    for mechanism in ("BFL1", "BFL2", "BFL2_EXFGJM"):
        for pool in ("full", "exAG"):
            predicted, audit = forecasts(rows, calendar, mechanism, pool)
            folder = output / mechanism / pool
            folder.mkdir(parents=True)
            predicted.to_csv(folder / "predictions.csv", index=False)
            audit.to_csv(folder / "fit_audit.csv", index=False)
            for period, start, end in (
                ("historical", "2022-09-06", "2026-09-22"),
                ("recent", "2026-08-03", "2026-09-30"),
            ):
                days = calendar[(calendar >= start) & (calendar <= end)]
                for name, cost, margin in (("base", 5, 0.12), ("stress", 15, 0.15)):
                    target = folder / (period + "_" + name)
                    target.mkdir()
                    try:
                        ledger, events, decisions = replay(
                            market,
                            predicted,
                            days,
                            units,
                            cost_bps=cost,
                            margin=margin,
                            mechanism=mechanism,
                            failure_output=target,
                        )
                    except ValueError as error:
                        record = {
                            "mechanism": mechanism,
                            "pool": pool,
                            "period": period,
                            "cost": name,
                            "status": "invalid_evidence",
                            "error": str(error),
                            "economic_result": None,
                        }
                        write_json(target / "failure.json", record)
                        results.append(record)
                        write_json(output / "RESULTS.json", results)
                        print(json.dumps(record), flush=True)
                        continue
                    ledger.to_csv(target / "ledger.csv", index=False)
                    events.to_csv(target / "events.csv", index=False)
                    decisions.to_csv(target / "decisions.csv", index=False)
                    checked = verify_account(market, ledger, events, units, cost)
                    metric_events = events.assign(
                        kind=np.where(events.action.isin(("entry", "exit")), "trade", "pnl"),
                        gross_pnl=events.pnl,
                        transaction_cost=events.fee,
                    )
                    metrics = account_metrics(ledger, metric_events)
                    record = {
                        "status": "complete",
                        "mechanism": mechanism,
                        "pool": pool,
                        "period": period,
                        "cost": name,
                        **metrics,
                        "baskets_entered": int((events.action == "entry").sum() / 3)
                        if len(events)
                        else 0,
                        "verification": checked,
                    }
                    files = {
                        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in target.iterdir()
                    }
                    write_json(target / "complete.json", {"result": record, "outputs": files})
                    results.append(record)
                    write_json(output / "RESULTS.json", results)
                    print(json.dumps(record), flush=True)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--units", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--observations", type=Path)
    parser.add_argument("--minimum-delivery-days", type=int, default=20)
    args = parser.parse_args()
    run(
        args.inputs,
        args.units,
        args.output,
        observations_path=args.observations,
        minimum_delivery_days=args.minimum_delivery_days,
    )
