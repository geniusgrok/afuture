"""S1 offline input/target replay through the existing continuous account.

Untrusted source diagnostics require an explicit switch and cannot be promoted.
No default/live configuration, broker, credentials or deployed service is changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from afuture.directional_60m_oi_confirmation import SUPPORTED_PRODUCTS
from afuture.directional_acceptance import ProductionMechanicsConfig
from afuture.directional_concentration_freeze import (
    ExpandingMedianConcentrationFreezeDirectionalProductionAcceptance as Account,
)
from afuture.directional_stress90_policy import STRESS90_POLICY, apply_cost_gate_row
from afuture.supply_demand import SupplyObservation, specific_carry_pairs, warrant_signal

GROUPS = (
    "A AP B C CF CJ CS LH M OI P PK RM SR Y".split(),
    "AG AL AU BC CU NI PB SN ZN".split(),
    "BU EB EG FU L LU MA NR PG PP RU TA UR V".split(),
    "FG HC I J JM PF RB SA SF SM SP SS".split(),
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--original-root", type=Path, required=True)
    parser.add_argument("--specific-market", type=Path, required=True)
    parser.add_argument("--continuous-market", type=Path, required=True)
    parser.add_argument("--days", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--diagnostic-untrusted-source", action="store_true")
    parser.add_argument("--ablation", action="store_true")
    args = parser.parse_args()
    protocol = json.loads(args.protocol.read_text())
    if protocol["id"] != "S1_WAREHOUSE_WARRANT_STATE_CHANGE_CURVE_20261002":
        raise ValueError("S1 protocol identity mismatch")
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "role": "paired_ablation_diagnostic"
        if args.ablation
        else (
            "untrusted_input_diagnostic"
            if args.diagnostic_untrusted_source
            else "qualified_input_research"
        ),
        "ablation": args.ablation,
        "live_authorized": False,
        "default_changed": False,
        "inputs": {
            p.name: digest(p)
            for p in (
                args.observations,
                args.specific_market,
                args.continuous_market,
                args.days,
                args.protocol,
            )
        },
        "source_files": {
            p: digest(ROOT / p) for p in ("afuture/supply_demand.py", "tools/run_supply_demand.py")
        },
    }
    (args.output / "input_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    rows = json.loads(args.observations.read_text())
    verified: set[tuple[str, str]] = set()
    records = []
    for row in rows:
        identity = row["original_path"], row["source_sha256"]
        if identity not in verified:
            path = (args.original_root / identity[0]).resolve()
            if not path.is_relative_to(args.original_root.resolve()) or digest(path) != identity[1]:
                raise ValueError("supply original identity mismatch")
            verified.add(identity)
        if args.diagnostic_untrusted_source and not row.get("units_explicit_in_snapshot", False):
            continue
        records.append(
            SupplyObservation(
                product=row["product"],
                statistical_day=date.fromisoformat(row["statistical_day"]),
                value=row["value"],
                available_at=datetime.fromisoformat(row["available_at"]),
                source_sha256=row["source_sha256"],
                source_id=row["source_id"],
                scope=row["scope"],
                qualified=True if args.diagnostic_untrusted_source else row["qualified"],
            )
        )
    by_product = {p: tuple(r for r in records if r.product == p) for p in STRESS90_POLICY.products}
    raw = pd.read_csv(args.specific_market, parse_dates=["date", "delivery"])
    continuous = pd.read_csv(args.continuous_market, parse_dates=["date"])
    close = continuous.pivot(index="date", columns="product", values="close").sort_index()
    days = pd.DatetimeIndex(pd.read_csv(args.days)["date"])
    if (
        len(days) != 980
        or days[0] != pd.Timestamp("2022-09-06")
        or days[-1] != pd.Timestamp("2026-09-22")
    ):
        raise ValueError("original account window must stay intact")
    if days.has_duplicates or not days.is_monotonic_increasing:
        raise ValueError("target days must be sorted and unique")
    pairs = specific_carry_pairs(raw)
    pairs.to_csv(args.output / "specific_pairs.csv", index=False)
    carry = pairs.pivot(index="date", columns="product", values="annualized_carry")
    all_days = pd.DatetimeIndex(sorted(raw.date.unique()))
    targets = pd.DataFrame(0.0, index=days, columns=STRESS90_POLICY.products)
    reasons = []
    previous = dict.fromkeys(STRESS90_POLICY.products, 0.0)
    for day in days:
        prior = all_days[all_days < day]
        if not len(prior):
            raise ValueError("missing previous completed market session")
        completed = prior[-1]
        # Earlier than any subsequent night/day opening in the shared daily model.
        decision = (completed.tz_localize("Asia/Shanghai") + pd.Timedelta(hours=15)).to_pydatetime()
        signals = {}
        for product, obs in by_product.items():
            value = (
                float(carry.at[completed, product])
                if completed in carry.index and product in carry
                else float("nan")
            )
            strength, reason = warrant_signal(
                obs,
                product=product,
                completed_day=completed.date(),
                decision_at=decision,
                carry=value,
            )
            if args.ablation and reason in {
                "scarcity_confirmed",
                "abundance_confirmed",
                "state_change_curve_conflict",
            }:
                strength = float(np.sign(value))
            signals[product] = strength * 0.2
            if obs:
                reasons.append(
                    dict(
                        date=str(day.date()),
                        product=product,
                        signal=strength,
                        reason=reason,
                        decision_at=decision.isoformat(),
                    )
                )
        for group in GROUPS:
            gross = sum(abs(signals[p]) for p in group)
            if gross > 0.5:
                for p in group:
                    signals[p] *= 0.5 / gross
        # The currently timed subset is RB; it is outside the existing nine OI
        # products. Future coverage of those products needs the shared OI input.
        if any(signals[p] for p in SUPPORTED_PRODUCTS):
            raise ValueError("new supply coverage requires the existing completed OI gate")
        history = close.loc[close.index < day]
        if any(
            signals[p] and (history[p].dropna().empty or history[p].dropna().index[-1] != completed)
            for p in signals
        ):
            raise ValueError("active supply target has stale completed close input")
        previous = apply_cost_gate_row(
            oi_weights=signals,
            prior_approved=previous,
            completed_close_history={p: history[p].dropna().tail(21).tolist() for p in signals},
        )
        targets.loc[day] = pd.Series(previous)
    pd.DataFrame(reasons).to_csv(args.output / "signal_audit.csv", index=False)
    targets.to_csv(args.output / "cost_approved_weights.csv")
    if not targets.abs().sum(axis=1).gt(0).any():
        (args.output / "readiness.json").write_text(
            json.dumps(
                {
                    "active_target_days": 0,
                    "accounts_run": 0,
                    "reason": "no executable qualified target; do not run an empty account matrix",
                },
                indent=2,
            )
            + "\n"
        )
        print("no active targets; account matrix not run", flush=True)
        return
    results = {}
    for pool in ("full", "exAG"):
        sample = raw.loc[raw["product"].ne("AG")].copy() if pool == "exAG" else raw
        prepared = Account().prepare_contracts(sample)
        weights = targets.copy()
        if pool == "exAG":
            weights["AG"] = 0.0
        for scenario, cost, margin in (("base", 5.0, 0.12), ("stress", 15.0, 0.15)):
            label = f"{pool}_{scenario}"
            account = Account(
                ProductionMechanicsConfig(initial_capital=500000.0, margin_rate_proxy=margin),
                completed_concentrations=(),
            )
            result = account.simulate(sample, weights, cost_bps=cost, prepared=prepared)
            result.daily.rename_axis("date").reset_index().to_csv(
                args.output / f"{label}_daily.csv", index=False
            )
            result.events.to_csv(args.output / f"{label}_events.csv", index=False)
            equity = result.daily.equity.astype(float)
            high = (
                pd.concat([pd.Series([500000.0]), equity], ignore_index=True)
                .cummax()
                .iloc[1:]
                .to_numpy()
            )
            net = float(equity.iloc[-1] - 500000.0)
            fees = float(result.events.transaction_cost.sum()) if not result.events.empty else 0.0
            gross = float(result.events.gross_pnl.sum()) if not result.events.empty else 0.0
            if abs(gross - fees - net) > 1e-6:
                raise ValueError("account PnL/cost reconciliation failed")
            results[label] = dict(
                final_equity=float(equity.iloc[-1]),
                mdd=float((1 - equity / high).max()),
                gross_pnl=gross,
                fees=fees,
                net_pnl=net,
                daily_count=len(equity),
                event_count=len(result.events),
            )
            (args.output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
            print(label, json.dumps(results[label]), flush=True)


if __name__ == "__main__":
    main()
