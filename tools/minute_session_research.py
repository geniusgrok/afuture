"""Fixed opening-range single-lot episodes on real saved five-minute bar quotes."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from adaptive_alpha_research import write_json
from pair_episode_research import readiness


def daytime_ends(day):
    return pd.DatetimeIndex(
        [
            value
            for start, end in (("09:05", "10:15"), ("10:35", "11:30"), ("13:35", "15:00"))
            for value in pd.date_range(
                str(day.date()) + " " + start, str(day.date()) + " " + end, freq="5min"
            )
        ]
    )


def session_episodes(bars: pd.DataFrame, unit: float):
    if not np.isfinite(unit) or unit <= 0:
        raise ValueError("invalid actual contract unit")
    frame = bars.copy().sort_values("datetime")
    if frame.datetime.duplicated().any():
        raise ValueError("duplicate specific bar timestamp")
    if not np.isfinite(frame[["open", "high", "low", "close", "volume"]].to_numpy()).all():
        raise ValueError("non-finite quote evidence")
    if (
        (frame[["open", "high", "low", "close"]] <= 0).any().any()
        or (frame.high < frame[["open", "close"]].max(axis=1)).any()
        or (frame.low > frame[["open", "close"]].min(axis=1)).any()
    ):
        raise ValueError("invalid bar OHLC")
    episodes, audit = [], []
    for day, source in frame.groupby(frame.datetime.dt.normalize()):
        expected = daytime_ends(day)
        if not expected.isin(source.datetime).all():
            left_boundary = (
                day == frame.datetime.min().normalize()
                and not expected[:3].isin(source.datetime).any()
            )
            if not left_boundary:
                raise ValueError(
                    f"incomplete subsequent session: {day}; no future-coverage day selection"
                )
            audit.append(
                {
                    "date": day,
                    "reason": "incomplete45_bar_day",
                    "observed": int(source.datetime.isin(expected).sum()),
                }
            )
            continue
        quote = source.set_index("datetime").loc[expected].reset_index(names="datetime")
        upper, lower = float(quote.high.iloc[:3].max()), float(quote.low.iloc[:3].min())
        found = False
        for signal in range(3, len(quote) - 3):
            close = float(quote.close.iloc[signal])
            sign = 1 if close > upper else -1 if close < lower else 0
            if not sign:
                continue
            entry = signal + 2
            entry_price = float(quote.open.iloc[entry])
            if entry_price * unit > 100000:
                audit.append(
                    {
                        "date": day,
                        "reason": "first_signal_unfunded",
                        "entry_gross": entry_price * unit,
                    }
                )
                found = True
                break
            if float(quote.volume.iloc[entry]) < 1:
                raise ValueError(
                    f"entry lacks executable-volume quote: {day}; "
                    "completed entry-bar volume cannot select a skipped trade"
                )
            exit_position = len(quote) - 1
            reason = "scheduled_day_exit"
            for completed in range(entry, len(quote) - 2):
                mark = float(quote.close.iloc[completed])
                if lower <= mark <= upper:
                    exit_position = completed + 2
                    reason = "range_reentry"
                    break
            if float(quote.volume.iloc[exit_position]) < 1:
                raise ValueError(
                    f"held episode lacks executable-volume exit quote: {day} "
                    f"{quote.symbol.iloc[exit_position]} {quote.datetime.iloc[exit_position]}"
                )
            exit_price = float(quote.open.iloc[exit_position])
            entry_time = quote.datetime.iloc[entry] - pd.Timedelta(minutes=5)
            exit_time = quote.datetime.iloc[exit_position] - pd.Timedelta(minutes=5)
            signal_time = quote.datetime.iloc[signal]
            if entry_time <= signal_time or exit_time <= entry_time:
                raise ValueError("noncausal bar execution convention")
            gross = sign * (exit_price - entry_price) * unit
            turnover = (entry_price + exit_price) * unit
            episodes.append(
                {
                    "date": day,
                    "symbol": quote.symbol.iloc[entry],
                    "signal_bar_end": signal_time,
                    "entry_bar_end": quote.datetime.iloc[entry],
                    "exit_bar_end": quote.datetime.iloc[exit_position],
                    "modeled_entry_time": entry_time,
                    "modeled_exit_time": exit_time,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "direction": sign,
                    "lots": 1,
                    "unit": unit,
                    "opening_upper": upper,
                    "opening_lower": lower,
                    "gross_pnl": gross,
                    "turnover": turnover,
                    "base_fee": turnover * 0.0005,
                    "stress_fee": turnover * 0.0015,
                    "base_net": gross - turnover * 0.0005,
                    "stress_net": gross - turnover * 0.0015,
                    "complete": True,
                    "exit_reason": reason,
                }
            )
            audit.append({"date": day, "reason": "completed_one_lot_episode"})
            found = True
            break
        if not found:
            audit.append({"date": day, "reason": "no_completed_range_break"})
    return pd.DataFrame(episodes), pd.DataFrame(audit)


def run(inputs: Path, units_path: Path, calendar_path: Path, output: Path):
    output.mkdir(parents=True, exist_ok=False)
    units = pd.read_csv(units_path).set_index("product").account_multiplier_used.to_dict()
    summary = {}
    calendar = pd.DatetimeIndex(pd.read_csv(calendar_path, parse_dates=["date"]).date)
    for path in sorted(inputs.glob("*.csv")):
        bars = pd.read_csv(path, parse_dates=["datetime"])
        symbol = path.stem
        product = bars["product"].iloc[0]
        folder = output / symbol
        folder.mkdir()
        try:
            first, last = bars.datetime.min().normalize(), bars.datetime.max().normalize()
            expected_days = calendar[(calendar > first) & (calendar <= last)]
            daytime = bars.loc[(bars.datetime.dt.hour >= 9) & (bars.datetime.dt.hour <= 15)]
            if not expected_days.isin(daytime.datetime.dt.normalize()).all():
                raise ValueError("missing complete source-backed trading day")
            episodes, audit = session_episodes(bars, float(units[product]))
        except ValueError as exc:
            summary[symbol] = {
                "blocked": True,
                "error": str(exc),
                "economic_result": "not_available",
            }
            write_json(output / "summary.json", summary)
            print(symbol, summary[symbol], flush=True)
            continue
        episodes.to_csv(folder / "episodes.csv", index=False)
        audit.to_csv(folder / "day_audit.csv", index=False)
        nets = episodes.stress_net.to_numpy() if len(episodes) else np.array([])
        summary[symbol] = {
            "completed": len(episodes),
            "gross": float(episodes.gross_pnl.sum()) if len(episodes) else 0,
            "base_net": float(episodes.base_net.sum()) if len(episodes) else 0,
            "stress_net": float(nets.sum()),
            "readiness": readiness(nets),
            "all_quote_identities_passed": True,
            "day_reasons": audit.reason.value_counts().to_dict(),
            "scope": "isolated one-lot bar-quote episodes; no portfolio/counter or prospective certification",
        }
        if len(episodes):
            prices = bars.set_index("datetime")
            for episode in episodes.to_dict("records"):
                start = float(prices.loc[episode["entry_bar_end"], "open"])
                end = float(prices.loc[episode["exit_bar_end"], "open"])
                if (
                    abs(
                        episode["gross_pnl"] - episode["direction"] * (end - start) * units[product]
                    )
                    > 1e-6
                    or abs(episode["stress_fee"] - (start + end) * units[product] * 0.0015) > 1e-6
                ):
                    raise ValueError("minute episode quote/fee identity mismatch")
        write_json(output / "summary.json", summary)
        print(symbol, summary[symbol], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--units", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.inputs, args.units, args.calendar, args.output)
