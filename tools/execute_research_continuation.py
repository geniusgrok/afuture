"""Execute evidence-led structural changes after a failed directional candidate."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from adaptive_alpha_research import (
    PRODUCT_MULTIPLIERS,
    account_metrics,
    read_weights,
    verify_events,
    write_json,
)
from continuous_rebuild_research import qualify_oi_targets, run
from holding_exit_research import HoldingExitAccount
from lot_projection_research import LotProjectionAccount
from relative_sector_research import SECTOR_GROUPS, relative_sector_weights
from research_continuation import execute_chain

from afuture.directional_stress90_policy import build_stress90_candidate_path

FAMILIES = ("breakout", "tsmom", "moving_average", "reversal", "acceleration", "momentum")


def verify_risk_reasons(daily):
    """Market-data interruptions cannot supply economic routing evidence."""
    valid = {
        "",
        "equity is not positive",
        "drawdown limit reached",
        "margin ratio limit reached",
        "available cash reserve too low",
        "daily loss limit reached",
        "combined margin ratio would exceed limit",
        "combined cash reserve would fall below limit",
        "contract volume limit reached",
    }
    reasons = set(daily.risk_reason.fillna(""))
    if reasons - valid:
        raise ValueError(f"non-economic account interruption: {sorted(reasons - valid)}")


def reference_inputs(previous):
    """Bind comparison paths to their already preserved original identities."""
    manifest_path = previous / "FILES.json"
    manifest = json.loads(manifest_path.read_text())["files"]
    result = {str(manifest_path): hashlib.sha256(manifest_path.read_bytes()).hexdigest()}
    for window in ("historical", "recent"):
        for pool in ("full", "exAG"):
            for cost in ("base", "stress"):
                relative = f"E1_{window}/E1_{pool}_{cost}/daily.csv"
                path = previous / relative
                identity = manifest[relative]
                raw = path.read_bytes()
                if (
                    len(raw) != identity["bytes"]
                    or hashlib.sha256(raw).hexdigest() != identity["sha256"]
                ):
                    raise ValueError("comparison differs from preserved original")
                result[str(path)] = identity["sha256"]
    return result


def confidence_budget(cost, survivor, families):
    """Only restore approved extra budget supported by causal family agreement."""
    if len(families) != 6:
        raise ValueError("exactly six frozen families required")
    if not cost.index.equals(survivor.index) or not cost.columns.equals(survivor.columns):
        raise ValueError("parent target identities differ")
    if ((cost * survivor < -1e-12) | (cost.abs() > survivor.abs() + 1e-12)).any().any():
        raise ValueError("parents do not share nested approved magnitude and direction")
    for frame in families:
        missing = cost.columns.difference(frame.columns)
        if (
            frame.index.has_duplicates
            or frame.columns.has_duplicates
            or not cost.index.isin(frame.index).all()
            or survivor[missing].abs().to_numpy().any()
            or not np.isfinite(frame.to_numpy()).all()
        ):
            raise ValueError("incomplete family evidence")
    votes = (
        sum(
            np.sign(frame.reindex(index=cost.index, columns=cost.columns, fill_value=0.0))
            for frame in families
        )
        / 6.0
    )
    agreement = (np.sign(survivor) * votes).clip(lower=0.0, upper=1.0)
    result = np.sign(survivor) * (cost.abs() + (survivor.abs() - cost.abs()) * agreement)
    if not np.isfinite(result.to_numpy()).all() or (result.abs().sum(axis=1) > 2 + 1e-10).any():
        raise ValueError("nonfinite or excessive restored budget")
    return result, agreement


def core_gates(summary, original):
    cells = {}
    for pool in ("full", "exAG"):
        for cost in ("base", "stress"):
            label = pool + "_" + cost
            candidate = summary[label]
            baseline = original["B0_" + label]
            floor = {"base": 3980823.91, "stress": 2302279.715}[cost]
            cap = {"base": 0.21513667943624126, "stress": 0.20702004457968537}[cost]
            cells[label] = {
                "complete_without_halt": candidate.get("days", 980) == 980
                and not candidate.get("halted", False),
                "return_floor": candidate["final_equity"] >= floor
                if pool == "full"
                else candidate["net_profit"] > 0,
                "drawdown_cap": candidate["mdd"] <= (cap if pool == "full" else baseline["mdd"]),
                "paired_B0_equity_better": candidate["final_equity"] > baseline["final_equity"],
                "paired_B0_drawdown_not_higher": candidate["mdd"] <= baseline["mdd"],
            }
    return all(all(cell.values()) for cell in cells.values()), cells


def next_experiment(history):
    """Failures select subsequent structures; no retry-count economic acceptance."""
    seen = {item["spec"]["id"]: item for item in history}
    if history:
        latest = history[-1]
        if (
            latest["status"] == "invalid_evidence"
            and latest["result"].get("stage") == "interrupted"
        ):
            return latest["spec"]
    if any(item["status"] == "candidate_passed" for item in history):
        # The owner must run the original extended acceptance, never auto-promote.
        return None
    if "P1" not in seen:
        return {
            "id": "P1",
            "rule": "M1Q joint floor/ceil projection before inherited risks and E1 exit",
            "parent_evidence": "M1Q intended gross1.486 vs realized0.320; no new signal",
        }
    if "C1" not in seen:
        parent = seen["P1"]
        if parent["status"] == "invalid_evidence":
            reason = "P1 input/implementation invalid; continue independent original-account budget mechanism"
        else:
            reason = "P1 did not satisfy original core; inspect verified gross/cost/realization before changing budget mechanism"
        return {
            "id": "C1",
            "rule": "six-family signed agreement controls only extra survivor budget; same E1 exit",
            "parent_evidence": reason,
            "parent_attempt": parent["attempt"],
            "parent_diagnostics": parent.get("result"),
        }
    if "MIX" not in seen:
        for candidate in ("P1", "C1"):
            parent = seen[candidate]
            if parent["status"] == "invalid_evidence":
                continue
            diagnostic = parent["result"]
            if diagnostic["component_net_positive"] or diagnostic["component_protection_observed"]:
                return {
                    "id": "MIX",
                    "component": candidate,
                    "component_attempt": parent["attempt"],
                    "rule": "fixed equal continuous-target mix with E1 parent; one projected account",
                    "parent_evidence": "verified net or conditional protection supports testing shared-account increment",
                    "parent_diagnostics": diagnostic,
                }
    if "R1" not in seen:
        failures = [item for item in history if item["status"] == "economic_failed"]
        if {item["spec"]["id"] for item in failures} >= {"P1", "C1"}:
            return {
                "id": "R1",
                "rule": "fixed within-sector relative leaders and laggards, weekly; no survivor refill",
                "parent_evidence": "verified budget or shared-account revisions did not meet core; test removing cross-sector directional allocation",
                "parent_attempts": [item["attempt"] for item in failures],
                "parent_diagnostics": [item["result"]["diagnostics"] for item in failures],
                "prior_failure": "broad cross-sectional momentum failed; sector pairing is the structural revision",
            }
    return None


def build_targets(spec, previous, continuous, baseline, attempt):
    strategy = attempt / "strategy"
    strategy.mkdir()
    for pool in ("full", "exAG"):
        folder = strategy / pool
        folder.mkdir()
        survivor = read_weights(previous / f"recent/B0_{pool}_base/all_weights.csv")
        fixed = read_weights(continuous / f"strategy/{pool}/M1Q_weights.csv")
        if spec["id"] == "P1":
            weights = fixed
        elif spec["id"] == "C1":
            cost = read_weights(previous / f"recent/C4B_{pool}_base/all_weights.csv")
            families = [
                read_weights(continuous / f"strategy/{pool}/family_{name}.csv") for name in FAMILIES
            ]
            weights, agreement = confidence_budget(cost, survivor, families)
            agreement.to_csv(folder / "agreement.csv", index_label="date")
        elif spec["id"] == "MIX":
            component = read_weights(
                attempt.parent
                / spec["component_attempt"]
                / f"strategy/{pool}/{spec['component']}_weights.csv"
            )
            weights = (survivor + component) / 2.0
        elif spec["id"] == "R1":
            data = pd.read_csv(baseline / "market_normalized/continuous.csv", parse_dates=["date"])
            prices = data.pivot(index="date", columns="product", values="close").reindex(
                columns=survivor.columns
            )
            raw, rankings = relative_sector_weights(prices, pool=pool)
            raw.to_csv(folder / "R1_raw.csv", index_label="date")
            rankings.to_csv(folder / "rankings.csv", index=False)
            flow = read_weights(continuous / "strategy/lagged_flow.csv")
            qualified = qualify_oi_targets(raw, flow)
            qualified.to_csv(folder / "R1_qualified.csv", index_label="date")
            path = build_stress90_candidate_path(
                base_weights=qualified.loc[survivor.index],
                completed_close_prices=prices,
                confirming_flow=flow,
            )
            path.oi_confirmed_weights.to_csv(folder / "R1_oi.csv", index_label="date")
            weights = path.cost_approved_weights
        else:
            raise ValueError("unknown frozen candidate")
        if pool == "exAG" and weights.AG.abs().max() != 0:
            raise ValueError("independent excluded pool contains AG")
        weights.to_csv(folder / f"{spec['id']}_weights.csv", index_label="date")
    return strategy


def execute(spec, attempt, *, previous, continuous, baseline, units):
    strategy = build_targets(spec, previous, continuous, baseline, attempt)
    with (attempt / "execution.log").open("w") as log, contextlib.redirect_stdout(log):
        for window in ("historical", "recent"):
            run(
                previous,
                strategy,
                previous / "pair_inputs/specific.csv",
                units,
                attempt / window,
                spec["id"],
                window,
                account_type=HoldingExitAccount if spec["id"] == "C1" else LotProjectionAccount,
            )


def account_exposures(daily, events, market):
    """Rebuild end-of-day signed sector exposure from actual integer fills."""
    sectors = {product: sector for sector, group in SECTOR_GROUPS.items() for product in group}
    prices = market.set_index(["date", "symbol"]).close
    positions, products, rows = {}, {}, []
    for day in daily.index:
        for event in events.loc[(events.date == day) & (events.kind == "trade")].itertuples():
            positions[event.symbol] = int(event.lots_after)
            products[event.symbol] = event.product
        row = {key + "_" + kind: 0.0 for key in SECTOR_GROUPS for kind in ("net", "gross")}
        for symbol, lots in positions.items():
            if not lots:
                continue
            product = products[symbol]
            amount = lots * float(prices.loc[(day, symbol)]) * PRODUCT_MULTIPLIERS[product]
            row[sectors[product] + "_net"] += amount
            row[sectors[product] + "_gross"] += abs(amount)
        row["gross"] = sum(row[key + "_gross"] for key in SECTOR_GROUPS)
        row["net"] = sum(row[key + "_net"] for key in SECTOR_GROUPS)
        if abs(row["gross"] - daily.loc[day, "gross_notional"]) > 1e-6:
            raise ValueError("actual fill-owned exposure does not reconcile")
        rows.append(row)
    return pd.DataFrame(rows, index=daily.index)


def validate(spec, attempt, *, previous, units):
    """Re-read every saved cell and real quote; exit status is not evidence."""
    market = pd.read_csv(previous / "pair_inputs/specific.csv", parse_dates=["date", "delivery"])
    PRODUCT_MULTIPLIERS.update(
        pd.read_csv(units).set_index("product").account_multiplier_used.to_dict()
    )
    metrics, checks, diagnostics = {}, {}, {}
    protection = []
    for window, days in (("historical", 980), ("recent", 42)):
        summary = json.loads((attempt / window / "summary.json").read_text())
        recorded_checks = json.loads((attempt / window / "checks.json").read_text())
        expected = {
            f"{spec['id']}_{pool}_{cost}"
            for pool in ("full", "exAG")
            for cost in ("base", "stress")
        }
        if set(summary) != expected or set(recorded_checks) != expected:
            raise ValueError("incomplete account matrix")
        metrics[window] = {}
        for label in sorted(expected):
            folder = attempt / window / label
            daily = read_weights(folder / "daily.csv")
            events = pd.read_csv(
                folder / "events.csv", parse_dates=["date"], float_precision="round_trip"
            )
            if not recorded_checks[label].get("passed") or len(daily) != days:
                raise ValueError("saved account verifier failed or sample incomplete")
            if not np.isfinite(daily.equity.to_numpy()).all():
                raise ValueError("nonfinite account path")
            verify_risk_reasons(daily)
            short_label = label.removeprefix(spec["id"] + "_")
            control = read_weights(previous / f"E1_{window}/E1_{short_label}/daily.csv")
            if not daily.index.equals(control.index):
                raise ValueError("account calendar differs from frozen comparison")
            if short_label.startswith("exAG") and events["product"].eq("AG").any():
                raise ValueError("excluded product appears in account events")
            checks[window + "/" + label] = verify_events(
                daily, events, market, 15 if label.endswith("stress") else 5
            )
            exposure = account_exposures(daily, events, market)
            exposure.to_csv(folder / "sector_exposure.csv", index_label="date")
            actual = account_metrics(daily, events)
            for key, value in actual.items():
                if isinstance(value, bool):
                    equal = value == summary[label][key]
                else:
                    equal = np.isfinite(value) and abs(value - summary[label][key]) < 1e-6
                if not equal:
                    raise ValueError("saved account metrics do not reproduce")
            episodes = pd.read_csv(folder / "holding_episodes.csv", float_precision="round_trip")
            if abs(episodes.net_pnl.sum() - actual["net_profit"]) > 1e-6:
                raise ValueError("holding attribution differs from cash")
            metrics[window][short_label] = actual
            if window == "historical":
                diagnostics[short_label] = {
                    "mean_actual_gross_ratio": float((daily.gross_notional / daily.equity).mean()),
                    "mean_integer_loss": float(daily.integer_rounding_loss_notional.mean()),
                    "gross_profit": actual["gross_pnl"],
                    "fees": actual["fees"],
                    "mean_absolute_net_ratio": float((exposure.net.abs() / daily.equity).mean()),
                    "entry_exit_turnover": float(
                        (daily.turnover_entry + daily.turnover_exit).sum()
                    ),
                    "resize_turnover": float(daily.turnover_resize.sum()),
                }
                if short_label.endswith("stress"):
                    losing_days = control.index[control.daily_return < 0]
                    conditional_mean = float(daily.loc[losing_days, "daily_return"].mean())
                    diagnostics[short_label]["mean_return_on_E1_losing_days"] = conditional_mean
                    protection.append(conditional_mean > 0)
    original = json.loads((previous / "E1_historical/summary.json").read_text())
    passed, gates = core_gates(metrics["historical"], original)
    any_halted = any(cell["halted"] for group in metrics.values() for cell in group.values())
    result = {
        "economic_passed": passed and not any_halted,
        "any_risk_halt": any_halted,
        "scope": "core historical development only; extended original acceptance still required",
        "core_gates": gates,
        "metrics": metrics,
        "diagnostics": diagnostics,
        "component_net_positive": not any_halted
        and all(metrics["historical"][p + "_stress"]["net_profit"] > 0 for p in ("full", "exAG")),
        "component_protection_observed": bool(not any_halted and protection and all(protection)),
        "all_eight_account_ledgers_passed": True,
        "independent_sample": False,
    }
    write_json(attempt / "independent_checks.json", checks)
    write_json(attempt / "analysis.json", result)
    return result


def main(args):
    protocol = json.loads((args.output / "FROZEN_PROTOCOL.json").read_text())
    inputs = {entry["path"]: entry["sha256"] for entry in protocol["inputs"]}
    inputs.update(reference_inputs(args.previous))
    additional = json.loads((args.output / "R1_PROTOCOL.json").read_text())
    inputs.update({entry["path"]: entry["sha256"] for entry in additional["inputs"]})
    source_dir = Path(__file__).parent
    sources = {
        str(source_dir / name): hashlib.sha256((source_dir / name).read_bytes()).hexdigest()
        for name in (
            "execute_research_continuation.py",
            "research_continuation.py",
            "lot_projection_research.py",
            "continuous_rebuild_research.py",
            "holding_exit_research.py",
            "adaptive_alpha_research.py",
            "relative_sector_research.py",
        )
    }
    for path in sorted((source_dir.parent / "afuture").rglob("*.py")):
        sources[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    units = args.baseline / "coverage/account_multiplier_audit.csv"
    common = {"previous": args.previous, "units": units}
    state = execute_chain(
        args.output / args.campaign,
        next_experiment,
        lambda spec, attempt: execute(
            spec, attempt, continuous=args.continuous, baseline=args.baseline, **common
        ),
        lambda spec, attempt: validate(spec, attempt, **common),
        context={
            "sources": sources,
            "inputs": inputs,
            "frozen_protocol": protocol,
            "additional_protocol": additional,
        },
    )
    print(json.dumps(state, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", required=True, type=Path)
    parser.add_argument("--continuous", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--campaign", default="chain", help="new immutable campaign directory name")
    main(parser.parse_args())
