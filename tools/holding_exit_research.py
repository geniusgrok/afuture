"""Research-only completed-close trailing exit on the original account simulator."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
from adaptive_alpha_research import (
    PRODUCT_MULTIPLIERS,
    Account,
    ProductionMechanicsConfig,
    account_metrics,
    holding_episodes,
    read_weights,
    verify_events,
    write_json,
)


def trailing_step(close: float, sign: int, entry: float, peak: float, atr: float, armed: bool):
    if not np.isfinite([close, entry, peak, atr]).all() or atr <= 0 or sign not in (-1, 1):
        raise ValueError("invalid completed-close trailing inputs")
    peak = max(peak, sign * close)
    armed = bool(armed or sign * (close - entry) >= atr)
    return peak, armed, bool(armed and peak - sign * close >= atr)


class HoldingExitAccount(Account):
    """Only remove targets; incumbents come from the original fill-owned simulator."""

    def __init__(self, market, config, *, completed_concentrations=()):
        super().__init__(config, completed_concentrations=completed_concentrations)
        self.market = {
            symbol: frame.set_index("date").sort_index()
            for symbol, frame in market.groupby("symbol", sort=False)
        }
        self.day = None
        self.tracks = {}
        self.blocked = {}
        self.exit_audit = []

    def observe_target_state(self, *, day, product_weights):
        super().observe_target_state(day=day, product_weights=product_weights)
        self.day = day
        for product, sign in list(self.blocked.items()):
            target = float(product_weights.get(product, 0.0))
            if target == 0.0 or target * sign < 0.0:
                del self.blocked[product]

    def target_lot_stages(self, **kwargs):
        baseline = super().target_lot_stages(**kwargs)
        current = {s: q for s, q in (kwargs.get("current_lots") or {}).items() if q}
        for symbol in set(self.tracks) - set(current):
            del self.tracks[symbol]
        for symbol, quantity in current.items():
            sign = 1 if quantity > 0 else -1
            history = self.market[symbol].loc[self.market[symbol].index < self.day]
            if history.empty:
                raise ValueError("incumbent has no prior completed contract mark")
            if symbol not in self.tracks or self.tracks[symbol]["sign"] != sign:
                previous = history.close.shift(1)
                true_range = (
                    pd.concat(
                        [
                            history.high - history.low,
                            (history.high - previous).abs(),
                            (history.low - previous).abs(),
                        ],
                        axis=1,
                    )
                    .max(axis=1)
                    .iloc[-20:]
                )
                if (
                    len(true_range) != 20
                    or not np.isfinite(true_range).all()
                    or true_range.mean() <= 0
                ):
                    raise ValueError(
                        "filled exposure lacks complete 20-session true-range evidence"
                    )
                self.tracks[symbol] = {
                    "sign": sign,
                    "entry": float(history.open.iloc[-1]),
                    "peak": sign * float(history.open.iloc[-1]),
                    "atr": float(true_range.mean()),
                    "armed": False,
                }
            track = self.tracks[symbol]
            close = float(history.close.iloc[-1])
            peak, armed, trigger = trailing_step(
                close, sign, track["entry"], track["peak"], track["atr"], track["armed"]
            )
            track.update(peak=peak, armed=armed)
            if trigger and self._product(symbol) not in self.blocked:
                self.blocked[self._product(symbol)] = sign
                self.exit_audit.append(
                    {
                        "target_day": self.day,
                        "source_day": history.index[-1],
                        "symbol": symbol,
                        "filled_lots": quantity,
                        "close": close,
                        "peak_signed": peak,
                        "atr": track["atr"],
                    }
                )
        final = dict(baseline.final_lots)
        for symbol, quantity in list(final.items()):
            if quantity * self.blocked.get(self._product(symbol), 0) > 0:
                del final[symbol]
        if any(abs(q) > abs(baseline.final_lots.get(s, 0)) for s, q in final.items()):
            raise ValueError("exit adapter increased a target")
        notional = sum(
            abs(q)
            * kwargs["product_open_prices"].get(self._product(s), 0.0)
            * PRODUCT_MULTIPLIERS[self._product(s)]
            for s, q in final.items()
        )
        return replace(baseline, final_lots=final, final_notional=float(notional))


def run(recent: Path, market_path: Path, units_path: Path, output: Path, *, historical=False):
    market = pd.read_csv(market_path, parse_dates=["date", "delivery"])
    units = pd.read_csv(units_path).set_index("product").account_multiplier_used.to_dict()
    PRODUCT_MULTIPLIERS.update(units)
    output.mkdir(exist_ok=False, parents=True)
    summaries, checks = {}, {}
    for pool in ("full", "exAG"):
        weights = read_weights(recent / f"B0_{pool}_base/all_weights.csv")
        if historical:
            weights = weights.loc[(weights.index >= "2022-09-06") & (weights.index <= "2026-09-22")]
        else:
            weights = weights.loc[(weights.index >= "2026-08-03") & (weights.index <= "2026-09-30")]
        sample = market.loc[market["product"] != "AG"] if pool == "exAG" else market
        warmup = []
        if not historical:
            all_weights = read_weights(recent / f"B0_{pool}_base/all_weights.csv")
            from adaptive_alpha_research import target_weight_concentration

            warmup = [
                h
                for h in (
                    target_weight_concentration(row.to_dict())
                    for _, row in all_weights.loc[all_weights.index < weights.index[0]].iterrows()
                )
                if h is not None
            ]
        prepared = Account().prepare_contracts(sample)
        variants = ("B0", "E1") if historical else ("E1",)
        for variant in variants:
            for cost_name, cost, margin in (("base", 5, 0.12), ("stress", 15, 0.15)):
                config = ProductionMechanicsConfig(margin_rate_proxy=margin)
                account = (
                    HoldingExitAccount(sample, config, completed_concentrations=warmup)
                    if variant == "E1"
                    else Account(config, completed_concentrations=warmup)
                )
                label = f"{variant}_{pool}_{cost_name}"
                result = account.simulate(sample, weights, cost_bps=cost, prepared=prepared)
                events, daily = result.events, result.daily
                events["date"] = pd.to_datetime(events.date)
                if daily.halted.any():
                    raise ValueError(f"{label} incomplete account: {result.first_divergence}")
                folder = output / label
                folder.mkdir()
                daily.to_csv(folder / "daily.csv", index_label="date")
                events.to_csv(folder / "events.csv", index=False)
                holding_episodes(events).to_csv(folder / "holding_episodes.csv", index=False)
                if variant == "E1":
                    pd.DataFrame(account.exit_audit).to_csv(folder / "exit_audit.csv", index=False)
                summaries[label] = account_metrics(daily, events)
                checks[label] = verify_events(daily, events, sample, cost)
                write_json(output / "summary.json", summaries)
                write_json(output / "checks.json", checks)
                print(label, summaries[label], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recent", type=Path, required=True)
    parser.add_argument("--market", type=Path, required=True)
    parser.add_argument("--units", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--historical", action="store_true")
    args = parser.parse_args()
    run(args.recent, args.market, args.units, args.output, historical=args.historical)
