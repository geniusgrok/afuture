"""Offline failure-exit and fixed-family research on preserved market originals."""

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
    target_weight_concentration,
    verify_events,
    write_json,
)
from holding_exit_research import HoldingExitAccount

from afuture.directional_60m_oi_confirmation import (
    build_daily_price_oi_flow,
    lag_flow_to_target_days,
)
from afuture.directional_stress90_policy import STRESS90_POLICY, build_stress90_candidate_path
from afuture.execution_aligned_policy import (
    FROZEN_PRODUCTS,
    ExecutionAlignedAggressivePolicy,
    _intraday_proxy_stream,
    _parse_template_id,
    _template_weight_path,
)


def failure_exit(close: float, sign: int, entry: float, atr: float, armed: bool) -> bool:
    if not np.isfinite([close, entry, atr]).all() or atr <= 0 or sign not in (-1, 1):
        raise ValueError("invalid failure-exit inputs")
    return bool(not armed and sign * (close - entry) <= -atr)


class FailureExitAccount(HoldingExitAccount):
    """Add one completed-close exit before the existing profitable trail is armed."""

    def target_lot_stages(self, **kwargs):
        baseline = super().target_lot_stages(**kwargs)
        current = {s: q for s, q in (kwargs.get("current_lots") or {}).items() if q}
        for symbol, quantity in current.items():
            track = self.tracks[symbol]
            history = self.market[symbol].loc[self.market[symbol].index < self.day]
            close = float(history.close.iloc[-1])
            product = self._product(symbol)
            if product not in self.blocked and failure_exit(
                close, track["sign"], track["entry"], track["atr"], track["armed"]
            ):
                self.blocked[product] = track["sign"]
                self.exit_audit.append(
                    {
                        "target_day": self.day,
                        "source_day": history.index[-1],
                        "symbol": symbol,
                        "filled_lots": quantity,
                        "close": close,
                        "entry": track["entry"],
                        "atr": track["atr"],
                        "reason": "unarmed_adverse_one_atr",
                    }
                )
        final = {
            s: q
            for s, q in baseline.final_lots.items()
            if q * self.blocked.get(self._product(s), 0) <= 0
        }
        notional = sum(
            abs(q)
            * kwargs["product_open_prices"].get(self._product(s), 0.0)
            * PRODUCT_MULTIPLIERS[self._product(s)]
            for s, q in final.items()
        )
        return replace(baseline, final_lots=final, final_notional=float(notional))


def family_paths(close: pd.DataFrame) -> tuple[dict[str, pd.DataFrame], dict[str, int]]:
    returns = close.where(close > 0).pct_change(fill_method=None)
    returns = returns.mask(returns.abs() > 0.20)
    grouped: dict[str, list[pd.DataFrame]] = {}
    for name in ExecutionAlignedAggressivePolicy(products=tuple(close.columns)).template_ids:
        template = _parse_template_id(name)
        grouped.setdefault(template.family, []).append(_template_weight_path(returns, template))
    families = {
        name: sum(frames[1:], frames[0].copy()) / len(frames) for name, frames in grouped.items()
    }
    return families, {name: len(frames) for name, frames in grouped.items()}


def fixed_family_weights(families: dict[str, pd.DataFrame]) -> pd.DataFrame:
    if not families:
        raise ValueError("no signal families")
    frames = list(families.values())
    result = sum(frames[1:], frames[0].copy()) / len(frames)
    if not np.isfinite(result.to_numpy()).all() or (result.abs().sum(axis=1) > 2 + 1e-10).any():
        raise ValueError("invalid fixed family budget")
    return result


def market_states(close: pd.DataFrame) -> pd.Series:
    """No current close or future fitted boundary enters today's state."""
    volatility = close.pct_change(fill_method=None).rolling(20, min_periods=20).std()
    completed = volatility.median(axis=1).shift(1)
    prior_median = completed.expanding(min_periods=20).median().shift(1)
    state = pd.Series("unavailable", index=close.index)
    valid = completed.notna() & prior_median.notna()
    state.loc[valid] = np.where(completed.loc[valid] > prior_median.loc[valid], "high", "low")
    return state


def conditional_weights(families: dict[str, pd.DataFrame], states, base, stress):
    result = pd.DataFrame(0.0, index=states.index, columns=next(iter(families.values())).columns)
    audit = []
    for day, state in states.items():
        history = states.index[(states.index < day) & (states == state)]
        eligible = []
        if state != "unavailable" and len(history) >= 60:
            eligible = [
                name
                for name in families
                if base.loc[history, name].mean() > 0 and stress.loc[history, name].mean() > 0
            ]
        if eligible:
            result.loc[day] = sum(families[name].loc[day] for name in eligible) / len(eligible)
        audit.append(
            {
                "date": day,
                "state": state,
                "prior_observations": len(history),
                "families": ",".join(eligible),
            }
        )
    return result, pd.DataFrame(audit)


def environment_diagnostic(states, stress):
    halves = (("2022-09-06", "2024-08-20"), ("2024-08-21", "2026-09-22"))
    result = {}
    for family in stress:
        rows = []
        for start, end in halves:
            row = {}
            for state in ("high", "low"):
                sample = stress.loc[
                    (stress.index >= start) & (stress.index <= end) & (states == state), family
                ]
                row[state] = {"count": len(sample), "mean": float(sample.mean())}
            rows.append(row)
        difference = [row["high"]["mean"] - row["low"]["mean"] for row in rows]
        stable = difference[0] * difference[1] > 0
        preferred = "high" if difference[0] > 0 else "low"
        ready = (
            stable
            and all(row[state]["count"] >= 60 for row in rows for state in ("high", "low"))
            and all(row[preferred]["mean"] > 0 for row in rows)
        )
        result[family] = {"halves": rows, "preferred": preferred, "ready": bool(ready)}
    preferences = {row["preferred"] for row in result.values() if row["ready"]}
    return {
        "families": result,
        "trigger_M2": len(preferences) == 2,
        "scope": "completed causal intraday proxies; development diagnostic, not physical holding/account alpha",
    }


def qualify_oi_targets(raw, flow):
    result = raw.copy()
    missing = flow.reindex(index=raw.index).isna()
    for product in STRESS90_POLICY.oi_products:
        result.loc[missing[product], product] = 0.0
    return result


def build_strategy(
    previous: Path, baseline: Path, fixed: Path, extension: Path, output: Path, *, qualified=False
):
    output.mkdir(exist_ok=False, parents=True)
    continuous = pd.read_csv(baseline / "market_normalized/continuous.csv", parse_dates=["date"])
    close = continuous.pivot(index="date", columns="product", values="close").reindex(
        columns=FROZEN_PRODUCTS
    )
    opens = continuous.pivot(index="date", columns="product", values="open").reindex(
        columns=FROZEN_PRODUCTS
    )
    dates = read_weights(previous / "recent/B0_full_base/all_weights.csv").index
    historical_dates = dates[dates <= "2026-08-20"]
    bars = pd.concat(
        [
            pd.read_csv(fixed / name)
            for name in ("prior_two_year_broad_60m.csv", "two_year_broad_60m.csv")
        ],
        ignore_index=True,
    )
    historical = lag_flow_to_target_days(
        build_daily_price_oi_flow(bars),
        target_days=historical_dates,
        products=STRESS90_POLICY.oi_products,
    )
    extended = read_weights(extension / "strategy/oi_flow_available_by_target_day.csv")
    suffix = read_weights(baseline / "strategy/new_lagged_oi_flow.csv")
    flow = pd.concat([historical, extended, suffix]).reindex(columns=historical.columns)
    if flow.index.has_duplicates:
        raise ValueError("overlapping OI source versions")
    flow.to_csv(output / "lagged_flow.csv", index_label="date")
    # Same primitives and inputs must first reproduce the original B0 state.
    base_b0 = read_weights(baseline / "strategy/base_weights.csv").loc[dates]
    original = build_stress90_candidate_path(
        base_weights=base_b0, completed_close_prices=close, confirming_flow=flow
    )
    expected = read_weights(previous / "recent/B0_full_base/all_weights.csv")
    error = float(np.abs(original.survivor_weights.to_numpy() - expected.to_numpy()).max())
    if error > 1e-12:
        raise ValueError(f"OI/gate inputs do not reproduce B0: {error}")
    write_json(output / "input_reproduction.json", {"passed": True, "B0_max_difference": error})
    for pool in ("full", "exAG"):
        folder = output / pool
        folder.mkdir()
        prices = close.drop(columns="AG") if pool == "exAG" else close
        family, counts = family_paths(prices)
        raw = fixed_family_weights(family).reindex(columns=FROZEN_PRODUCTS, fill_value=0.0)
        state = market_states(prices)
        base_stream = pd.DataFrame(
            {
                name: _intraday_proxy_stream(opens, close, weights, cost_bps=5)
                for name, weights in family.items()
            }
        )
        stress_stream = pd.DataFrame(
            {
                name: _intraday_proxy_stream(opens, close, weights, cost_bps=15)
                for name, weights in family.items()
            }
        )
        for name, frame in family.items():
            frame.to_csv(folder / f"family_{name}.csv", index_label="date")
        base_stream.to_csv(folder / "family_base_proxy.csv", index_label="date")
        stress_stream.to_csv(folder / "family_stress_proxy.csv", index_label="date")
        state.to_csv(folder / "states.csv", index_label="date", header=["state"])
        write_json(folder / "family_counts.json", counts)
        diagnostic = environment_diagnostic(state, stress_stream)
        write_json(folder / "environment_diagnostic.json", diagnostic)
        variant_name = "M1Q" if qualified else "M1"
        if qualified:
            raw.to_csv(folder / "M1_unqualified_raw.csv", index_label="date")
            raw = qualify_oi_targets(raw, flow)
        for label, weights in ((variant_name, raw),):
            weights.to_csv(folder / f"{label}_raw.csv", index_label="date")
            path = build_stress90_candidate_path(
                base_weights=weights.loc[dates], completed_close_prices=close, confirming_flow=flow
            )
            path.oi_confirmed_weights.to_csv(folder / f"{label}_oi.csv", index_label="date")
            path.cost_approved_weights.to_csv(folder / f"{label}_cost.csv", index_label="date")
            path.survivor_weights.to_csv(folder / f"{label}_weights.csv", index_label="date")
        conditional, audit = conditional_weights(family, state, base_stream, stress_stream)
        conditional = conditional.reindex(columns=FROZEN_PRODUCTS, fill_value=0.0)
        conditional.to_csv(folder / "M2_raw_diagnostic.csv", index_label="date")
        audit.to_csv(folder / "conditional_audit.csv", index=False)
        print("strategy", pool, counts, "environment trigger", diagnostic["trigger_M2"], flush=True)


def run(
    previous: Path,
    strategy: Path,
    market_path: Path,
    units_path: Path,
    output: Path,
    variant: str,
    window: str,
):
    market = pd.read_csv(market_path, parse_dates=["date", "delivery"])
    PRODUCT_MULTIPLIERS.update(
        pd.read_csv(units_path).set_index("product").account_multiplier_used.to_dict()
    )
    output.mkdir(exist_ok=False, parents=True)
    summaries, checks = {}, {}
    for pool in ("full", "exAG"):
        weights_path = (
            previous / f"recent/B0_{pool}_base/all_weights.csv"
            if variant == "E3"
            else strategy / pool / f"{variant}_weights.csv"
        )
        all_weights = read_weights(weights_path)
        start, end = (
            ("2022-09-06", "2026-09-22") if window == "historical" else ("2026-08-03", "2026-09-30")
        )
        weights = all_weights.loc[(all_weights.index >= start) & (all_weights.index <= end)]
        warmup = (
            []
            if window == "historical"
            else [
                h
                for h in (
                    target_weight_concentration(row.to_dict())
                    for _, row in all_weights.loc[all_weights.index < start].iterrows()
                )
                if h is not None
            ]
        )
        sample = market.loc[market["product"] != "AG"] if pool == "exAG" else market
        prepared = Account().prepare_contracts(sample)
        for cost_name, cost, margin in (("base", 5, 0.12), ("stress", 15, 0.15)):
            label = f"{variant}_{pool}_{cost_name}"
            folder = output / label
            folder.mkdir()
            account_class = FailureExitAccount if variant == "E3" else HoldingExitAccount
            account = account_class(
                sample,
                ProductionMechanicsConfig(margin_rate_proxy=margin),
                completed_concentrations=warmup,
            )
            result = account.simulate(sample, weights, cost_bps=cost, prepared=prepared)
            daily, events = result.daily, result.events
            events["date"] = pd.to_datetime(events.date)
            daily.to_csv(folder / "daily.csv", index_label="date")
            events.to_csv(folder / "events.csv", index=False)
            pd.DataFrame(account.exit_audit).to_csv(folder / "exit_audit.csv", index=False)
            if len(events):
                holding_episodes(events).to_csv(folder / "holding_episodes.csv", index=False)
            summaries[label] = {
                **account_metrics(daily, events),
                "first_divergence": result.first_divergence,
            }
            try:
                checks[label] = verify_events(daily, events, sample, cost)
            except (ValueError, KeyError) as exc:
                checks[label] = {"passed": False, "error": str(exc)}
            write_json(output / "summary.json", summaries)
            write_json(output / "checks.json", checks)
            print(label, summaries[label], checks[label]["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--baseline", type=Path, required=True)
    build.add_argument("--fixed", type=Path, required=True)
    build.add_argument("--extension", type=Path, required=True)
    build.add_argument("--qualified", action="store_true")
    replay = sub.add_parser("replay")
    replay.add_argument("--strategy", type=Path, required=True)
    replay.add_argument("--market", type=Path, required=True)
    replay.add_argument("--units", type=Path, required=True)
    replay.add_argument("--variant", choices=("E3", "M1", "M1Q", "M2"), required=True)
    replay.add_argument("--window", choices=("historical", "recent"), required=True)
    args = parser.parse_args()
    if args.command == "build":
        build_strategy(
            args.previous,
            args.baseline,
            args.fixed,
            args.extension,
            args.output,
            qualified=args.qualified,
        )
    else:
        run(
            args.previous,
            args.strategy,
            args.market,
            args.units,
            args.output,
            args.variant,
            args.window,
        )
