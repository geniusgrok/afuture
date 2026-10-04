"""Evidence-directed core research: curve information, relative labels and risk.

The outer planner synthesizes specifications from a bounded, declared mechanism
grammar. The inner receipt runner executes and verifies accounts. It does not
invent unimplemented algorithms or install a background research service.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from adaptive_alpha_research import read_weights, write_json
from continuous_rebuild_research import run
from core_rebuild_research import ProjectedCovarianceAccount
from execute_research_continuation import reference_inputs, validate
from mature_forecast_research import forecast_weights as pressure_weights
from relative_sector_research import SECTOR_GROUPS
from research_continuation import _digest, _manifest, _read, _verify_context, execute_chain

from afuture.curve_market_forecast import forecast_from_mature_curve
from afuture.execution_aligned_policy import FROZEN_PRODUCTS
from afuture.forecast_portfolio import forecast_covariance_portfolio
from afuture.recovery_risk import DrawdownRecoveryGovernor

GROUPS = {product: group for group, products in SECTOR_GROUPS.items() for product in products}


def verified_seed_receipt(path, expected_inputs):
    campaign = path.parent.parent.parent
    record = _read(path)
    state = _read(campaign / "state.json")
    context_path = campaign / "context.json"
    context = _read(context_path)
    if state["receipts"].get(path.parent.name) != _digest(path) or record[
        "context_sha256"
    ] != _digest(context_path):
        raise ValueError("seed receipt identity differs from its recorded context/state")
    _verify_context(context)
    if record["status"] not in ("economic_failed", "candidate_passed") or record[
        "manifest"
    ] != _manifest(path.parent):
        raise ValueError("seed account receipt is invalid or changed")
    if any(context["inputs"].get(name) != identity for name, identity in expected_inputs.items()):
        raise ValueError("seed account market or unit identity differs")
    return record


def build_curve_observations(market, observations, calendar):
    """Add curves chosen from completed activity, retaining every future-missing label."""
    frame = market.copy()
    frame.date, frame.delivery = pd.to_datetime(frame.date), pd.to_datetime(frame.delivery)
    if frame.duplicated(["date", "symbol"]).any():
        raise ValueError("duplicate concrete curve observations")
    selected_days = observations[["feature_through", "entry_day"]].drop_duplicates()
    if selected_days.feature_through.duplicated().any():
        raise ValueError("feature session maps to multiple entry sessions")
    entry_days = selected_days.set_index("feature_through").entry_day
    eligible = frame.loc[
        frame.volume.ge(1000)
        & frame.hold.ge(5000)
        & (frame.delivery - frame.date.map(entry_days)).dt.days.ge(20)
    ].copy()
    eligible = eligible.sort_values(
        ["date", "product", "hold", "volume", "delivery", "symbol"],
        ascending=[True, True, False, False, True, True],
    ).drop_duplicates(["date", "product", "delivery"])
    eligible = eligible.loc[eligible.groupby(["date", "product"]).cumcount() < 2]
    choices = {}
    for key, rows in eligible.groupby(["date", "product"], sort=False):
        if len(rows) == 2:
            ordered = rows.sort_values(["delivery", "symbol"])
            near, far = ordered.iloc[0], ordered.iloc[1]
            choices[key] = (near, far)
    closes = frame.set_index(["date", "symbol"]).close
    previous = {day: calendar[i - 20] for i, day in enumerate(calendar) if i >= 20}
    records = []
    for row in observations.itertuples(index=False):
        values = row._asdict()
        for key in ("x1", "x2", "x3", "feature_status"):
            values.pop(key)
        values.update(
            curve_level=np.nan,
            curve_change=np.nan,
            pressure=np.nan,
            curve_status="missing_prior_eligible_pair",
            symbol_near="",
            symbol_far="",
            pair_source_day=row.feature_through,
            pair_history_day=previous.get(row.feature_through, pd.NaT),
        )
        pair = choices.get((row.feature_through, row.product))
        if pair is not None:
            near, far = pair
            values.update(symbol_near=near.symbol, symbol_far=far.symbol)
            years = (far.delivery - near.delivery).days / 365.25
            history_day = previous.get(row.feature_through, pd.NaT)
            old_near = closes.get((history_day, near.symbol), np.nan)
            old_far = closes.get((history_day, far.symbol), np.nan)
            prices = np.array([near.close, far.close, old_near, old_far], dtype=float)
            if (
                row.feature_status == "eligible"
                and np.isfinite(prices).all()
                and prices.min() > 0
                and years > 0
            ):
                spread = np.log(near.close / far.close)
                old_spread = np.log(old_near / old_far)
                values.update(
                    curve_level=float(np.tanh(spread / years / (row.volatility * np.sqrt(252)))),
                    curve_change=float(
                        np.tanh((spread - old_spread) / (row.volatility * np.sqrt(20)))
                    ),
                    pressure=row.x1,
                    curve_status="eligible",
                )
            else:
                values["curve_status"] = "incomplete_same_pair_or_pressure_history"
        records.append(values)
    return pd.DataFrame(records)


def curve_weights(observations, calendar, *, pool, grouped=False, relative=False):
    available = observations.loc[observations.curve_status.eq("eligible")].copy()
    training = available.loc[available.label_status.eq("observed")].copy()
    exclusions = ("AG",) if pool == "exAG" else ()
    result = pd.DataFrame(0.0, index=calendar, columns=FROZEN_PRODUCTS)
    current = pd.Series(0.0, index=result.columns)
    audits, forecasts = [], []
    by_day = dict(tuple(available.groupby("entry_day")))
    for i, day in enumerate(calendar):
        if i % 5 == 0:
            targets = by_day.get(day, available.iloc[:0])
            prediction = forecast_from_mature_curve(
                training,
                targets,
                decision_day=day,
                excluded_products=exclusions,
                groups=GROUPS,
                grouped=grouped,
                relative=relative,
            )
            audits.append(prediction.audit)
            current[:] = 0.0
            if prediction.ready and len(prediction.forecasts):
                rows = prediction.forecasts.merge(
                    targets[["product", "symbol", "volatility"]],
                    on=["product", "symbol"],
                    validate="one_to_one",
                )
                if relative:
                    rows["group"] = rows["product"].map(GROUPS)
                    for _, group in rows.groupby("group"):
                        ordered = group.sort_values(["expected_return", "product"])
                        short, long = ordered.iloc[0], ordered.iloc[-1]
                        # A gross-normalized equal-notional pair costs30bps roundtrip:
                        # its return is (long-short)/2, so the difference must exceed60bps.
                        if long.expected_return - short.expected_return > 0.006:
                            current[long["product"]] = 0.25
                            current[short["product"]] = -0.25
                else:
                    scores = (rows.expected_return.abs() - 0.003).clip(lower=0) / (
                        5 * rows.volatility.pow(2)
                    )
                    total = float(scores.sum())
                    if total > 0:
                        amounts = (2 * scores / total).clip(upper=0.25)
                        for product, weight in zip(
                            rows["product"], np.sign(rows.expected_return) * amounts, strict=True
                        ):
                            current[product] = float(weight)
                rows["decision_day"] = day
                forecasts.append(rows)
        result.loc[day] = current
    predictions = (
        pd.concat(forecasts, ignore_index=True)
        if forecasts
        else pd.DataFrame(
            columns=[
                "entry_day",
                "product",
                "symbol",
                "expected_return",
                "volatility",
                "decision_day",
            ]
        )
    )
    return result, audits, predictions


def optimized_weights(predictions, calendar, tape):
    predictions = predictions.copy()
    predictions["decision_day"] = pd.to_datetime(predictions.decision_day)
    groups = dict(tuple(predictions.groupby("decision_day")))
    result = pd.DataFrame(0.0, index=calendar, columns=FROZEN_PRODUCTS)
    current = pd.Series(0.0, index=result.columns)
    audits = []
    for i, day in enumerate(calendar):
        if i % 5 == 0:
            rows = groups.get(day)
            forecasts = {} if rows is None else rows.set_index("product").expected_return.to_dict()
            allocation = forecast_covariance_portfolio(forecasts, tape, decision_day=day)
            current[:] = 0.0
            for product, weight in allocation.weights.items():
                current[product] = weight
            audits.append(allocation.audit)
        result.loc[day] = current
    return result, audits


class RecoveringProjectedAccount(ProjectedCovarianceAccount):
    def observe_target_state(self, *, day, product_weights):
        super().observe_target_state(day=day, product_weights=product_weights)
        self.risk_governor = DrawdownRecoveryGovernor(self.risk_governor.value)


def diagnose_accounts(result, attempt, candidate, *, excluded=()):
    pools = {}
    for pool in ("full", "exAG"):
        folder = attempt / f"historical/{candidate}_{pool}_stress"
        daily = read_weights(folder / "daily.csv")
        events = pd.read_csv(folder / "events.csv", parse_dates=["date"])
        if events["product"].isin(excluded).any():
            raise ValueError("excluded product in actual account events")
        net = (
            events.groupby("product")
            .gross_pnl.sum()
            .subtract(events.groupby("product").transaction_cost.sum(), fill_value=0)
        )
        daily["net_pnl"] = daily.equity.diff().fillna(daily.equity.iloc[0] - 500000.0)
        annual = daily.groupby(daily.index.year).agg(
            active_days=("gross_notional", lambda x: int(x.gt(0).sum())),
            net_profit=("net_pnl", "sum"),
        )
        positive = net[net > 0].sort_values(ascending=False)
        total = result["metrics"]["historical"][pool + "_stress"]["net_profit"]
        pools[pool] = {
            "active_days": int(daily.gross_notional.gt(0).sum()),
            "profitable_products": len(positive),
            "top_two_share_of_positive_profit": float(positive.iloc[:2].sum() / positive.sum())
            if positive.sum()
            else None,
            "top_two_share_of_total_net_profit": float(positive.iloc[:2].sum() / total)
            if total > 0
            else None,
            "annual": {
                str(year): {
                    "active_days": int(row.active_days),
                    "net_profit": float(row.net_profit),
                }
                for year, row in annual.iterrows()
            },
            "actual_product_net": net.to_dict(),
        }
        if abs(net.sum() - total) > 1e-6:
            raise ValueError("product attribution differs from cash")
    gross_edge = all(result["metrics"]["historical"][p + "_stress"]["gross_pnl"] > 0 for p in pools)
    supported = result["component_net_positive"] and all(
        row["profitable_products"] >= 5
        and row["top_two_share_of_positive_profit"] <= 0.8
        and sum(y["net_profit"] > 0 and y["active_days"] >= 20 for y in row["annual"].values()) >= 2
        for row in pools.values()
    )
    status = (
        "candidate_for_validation"
        if result["economic_passed"]
        else "development_component_supported"
        if supported
        else "retain_information_or_risk_hypothesis"
        if gross_edge
        else "replace_mechanism"
    )
    return {
        "status": status,
        "gross_edge_in_both_pools": gross_edge,
        "pools": pools,
        "final_goal_passed": result["economic_passed"],
        "independent_validation": False,
    }


def next_hypothesis(history, *, event_qualified=False):
    """Synthesize the next structure using diagnoses, without threshold sweeps.

    The declared grammar has six executable mechanisms plus a data-qualified
    event route. Unknown information sources cannot be manufactured by a planner.
    Intermediate weakness does not terminate independent mechanisms.
    """
    seen = {row["spec"]["id"]: row for row in history}
    if any(row["status"] == "candidate_passed" for row in history):
        return None
    if "X1" not in seen:
        return {
            "id": "X1",
            "information": "pressure",
            "excluded": ["FG", "JM"],
            "reason": "actual prior profit concentrated FG/JM; refit exclusions before any calculation",
        }
    if "T1" not in seen:
        return {
            "id": "T1",
            "information": "curve",
            "model": "shared",
            "reason": "replace pressure-only information by observed term structure and same-pair innovation",
        }
    curve_invalid = seen["T1"]["status"] == "invalid_evidence"
    if "T2" not in seen and not curve_invalid:
        return {
            "id": "T2",
            "information": "curve",
            "model": "grouped",
            "parent": "T1",
            "reason": "test fixed economic-group slope deviations after shared model did not meet goal",
            "parent_diagnosis": seen["T1"]["result"].get("research_diagnosis"),
        }
    if "RV1" not in seen and not curve_invalid and seen["T2"]["status"] != "invalid_evidence":
        return {
            "id": "RV1",
            "information": "curve",
            "model": "grouped_relative",
            "parent": "T2",
            "reason": "replace outright prediction by learned within-group relative labels and actual two-product targets",
            "parent_diagnosis": seen["T2"]["result"].get("research_diagnosis"),
        }
    if "OPT1" not in seen:
        parent = next(
            (
                name
                for name in ("T1", "T2")
                if name in seen
                and seen[name]["result"]
                .get("research_diagnosis", {})
                .get("gross_edge_in_both_pools")
            ),
            "MATURE1",
        )
        return {
            "id": "OPT1",
            "information": "inherited_forecasts",
            "parent": parent,
            "reason": "joint signed-forecast covariance/cost construction on the first predeclared gross-positive parent; no best-return selection",
        }
    if "REC1" not in seen:
        return {
            "id": "REC1",
            "information": "prior_R1_REBUILD",
            "risk": "pause_recovery",
            "reason": "independent same-signal comparison of risk pause/probation against the previously hard-stopped account",
        }
    if "PG1" not in seen:
        return {
            "id": "PG1",
            "round": 2,
            "information": "pressure",
            "model": "grouped",
            "parent": "X1",
            "reason": "initial direction changes did not reach goal; synthesize fixed group sharing with ownership-independent pressure observations",
            "parent_diagnosis": seen["X1"]["result"].get("research_diagnosis"),
        }
    if "RP1" not in seen:
        return {
            "id": "RP1",
            "round": 2,
            "information": "pressure",
            "model": "grouped_relative",
            "parent": "PG1",
            "reason": "replace pooled outright pressure labels by within-group relative outcomes; second structural revision, no parameter sweep",
            "parent_diagnosis": seen["PG1"]["result"].get("research_diagnosis"),
        }
    supported = [
        row
        for row in history
        if row["result"].get("research_diagnosis", {}).get("status")
        == "development_component_supported"
        and row["spec"].get("model") not in ("grouped_relative",)
    ]
    pair = next(
        (
            (left, right)
            for i, left in enumerate(supported)
            for right in supported[i + 1 :]
            if {left["spec"].get("information"), right["spec"].get("information")}
            == {"pressure", "curve"}
        ),
        None,
    )
    if pair and "MIX1" not in seen:
        return {
            "id": "MIX1",
            "round": 2,
            "information": "shared_component_forecasts",
            "parents": [row["spec"]["id"] for row in pair],
            "reason": "two distinct information families have development contribution; allocate shared forecasts with covariance/cost utility in one actual account",
        }
    if event_qualified and "EVENT1" not in seen:
        return {
            "id": "EVENT1",
            "information": "certified_supply_vintages",
            "reason": "use only public byte versions whose accepted availability precedes the actual entry clock",
        }
    return None


def main(args):
    protocol_path = args.output / "FROZEN_PROTOCOL.json"
    protocol = json.loads(protocol_path.read_text())
    prior = args.core
    pressure = pd.read_csv(
        prior / "forecast_inputs/observations.csv",
        parse_dates=["entry_day", "feature_through", "maturity_day"],
    )
    tape = read_weights(prior / "inputs/market_returns.csv")
    curves_path = args.output / "inputs/curve_observations.csv"
    if not curves_path.exists():
        market = pd.read_csv(
            args.previous / "pair_inputs/specific.csv", parse_dates=["date", "delivery"]
        )
        curves = build_curve_observations(market, pressure, tape.index)
        curves_path.parent.mkdir(exist_ok=True)
        curves.to_csv(curves_path, index=False)
        write_json(
            curves_path.with_name("coverage.json"),
            {
                "observations": len(curves),
                "curve_status": curves.curve_status.value_counts().to_dict(),
                "label_status": curves.label_status.value_counts().to_dict(),
                "future_label_coverage_used_for_target_selection": False,
            },
        )
    curves = pd.read_csv(curves_path, parse_dates=["entry_day", "feature_through", "maturity_day"])
    old_mature = Path(protocol["mature_attempt"])
    old_risk = Path(protocol["risk_attempt"])
    dates = read_weights(old_risk / "strategy/full/R1_REBUILD_weights.csv").index
    inputs = {row["path"]: row["sha256"] for row in protocol["inputs"]}
    inputs.update(reference_inputs(args.previous))
    seed_history = []
    for receipt_path in args.seed_receipt:
        record = verified_seed_receipt(
            receipt_path,
            {
                str(path): inputs[str(path)]
                for path in (args.previous / "pair_inputs/specific.csv", args.units)
            },
        )
        if any(row["spec"]["id"] == record["spec"]["id"] for row in seed_history):
            raise ValueError("duplicate seed mechanism")
        seed_history.append(record)
        for path in [receipt_path] + [receipt_path.parent / name for name in record["manifest"]]:
            inputs[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    for path in (protocol_path, curves_path, args.output / "EVENT_QUALIFICATION.json"):
        inputs[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    source_root = Path(__file__).resolve().parents[1]
    sources = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(
            list((source_root / "afuture").rglob("*.py"))
            + list((source_root / "tools").glob("*.py"))
        )
    }
    event = json.loads((args.output / "EVENT_QUALIFICATION.json").read_text())

    def select(history):
        history = seed_history + history
        spec = next_hypothesis(history, event_qualified=event["historical_account_ready"])
        cycle = {
            "completed_attempts": len(history),
            "research_round": 1 if len(history) < 6 else 2,
            "next_specification": spec,
            "event_route": event["status"],
            "economic_goal_achieved": any(r["status"] == "candidate_passed" for r in history),
            "remaining_work": []
            if spec
            else [
                "new independent information/mechanism or prospective qualified event observations",
                "original extended validation if original core passes",
            ],
        }
        folder = args.output / "planner_cycles"
        folder.mkdir(exist_ok=True)
        path = folder / f"cycle{len(history):06d}.json"
        if path.exists():
            if json.loads(path.read_text()) != cycle:
                raise ValueError("registered planner cycle changed")
        else:
            write_json(path, cycle)
        return spec

    def execute(spec, attempt):
        strategy = attempt / "strategy"
        for pool in ("full", "exAG"):
            folder = strategy / pool
            folder.mkdir(parents=True)
            name = spec["id"]
            if name == "X1":
                sample = pressure.loc[~pressure["product"].isin(spec["excluded"])].copy()
                targets, audit, predictions = pressure_weights(sample, tape.index, pool=pool)
            elif name in ("T1", "T2", "RV1", "PG1", "RP1"):
                model_input = curves
                if name in ("PG1", "RP1"):
                    model_input = pressure.rename(
                        columns={
                            "x1": "curve_level",
                            "x2": "curve_change",
                            "x3": "pressure",
                            "feature_status": "curve_status",
                        }
                    )
                targets, audit, predictions = curve_weights(
                    model_input,
                    tape.index,
                    pool=pool,
                    grouped=name != "T1",
                    relative=name in ("RV1", "RP1"),
                )
            elif name == "OPT1":
                parent = old_mature
                if spec["parent"] != "MATURE1":
                    parent = next(
                        path
                        for path in attempt.parent.iterdir()
                        if path.is_dir()
                        and (path / "complete.json").exists()
                        and json.loads((path / "complete.json").read_text())["spec"]["id"]
                        == spec["parent"]
                    )
                predictions = pd.read_csv(
                    parent / f"strategy/{pool}/predictions.csv", parse_dates=["decision_day"]
                )
                targets, audit = optimized_weights(predictions, tape.index, tape)
            elif name == "REC1":
                targets = read_weights(old_risk / f"strategy/{pool}/R1_REBUILD_weights.csv")
                audit, predictions = [], pd.DataFrame()
            elif name == "MIX1":
                frames = []
                for parent_id in spec["parents"]:
                    parent = next(
                        path
                        for path in attempt.parent.iterdir()
                        if path.is_dir()
                        and (path / "complete.json").exists()
                        and json.loads((path / "complete.json").read_text())["spec"]["id"]
                        == parent_id
                    )
                    frames.append(
                        pd.read_csv(
                            parent / f"strategy/{pool}/predictions.csv",
                            parse_dates=["decision_day"],
                        )
                    )
                joint = frames[0].merge(
                    frames[1],
                    on=["decision_day", "product", "symbol"],
                    suffixes=("_left", "_right"),
                    validate="one_to_one",
                )
                predictions = joint[["decision_day", "product", "symbol"]].copy()
                predictions["expected_return"] = (
                    joint.expected_return_left + joint.expected_return_right
                ) / 2
                targets, audit = optimized_weights(predictions, tape.index, tape)
            else:
                raise ValueError(
                    "qualified event route requires its own registered executable recipe"
                )
            if pool == "exAG" and targets.AG.ne(0).any():
                raise ValueError("AG in independent target pool")
            targets.loc[dates].to_csv(folder / f"{name}_weights.csv", index_label="date")
            write_json(folder / "model_audit.json", audit)
            predictions.to_csv(folder / "predictions.csv", index=False)

        def account_type(market, config, *, completed_concentrations):
            del completed_concentrations
            cls = RecoveringProjectedAccount if spec["id"] == "REC1" else ProjectedCovarianceAccount
            return cls(market, config, market_returns=tape)

        with (attempt / "execution.log").open("w") as log, contextlib.redirect_stdout(log):
            for window in ("historical", "recent"):
                run(
                    args.previous,
                    strategy,
                    args.previous / "pair_inputs/specific.csv",
                    args.units,
                    attempt / window,
                    spec["id"],
                    window,
                    account_type=account_type,
                )

    def checked(spec, attempt):
        result = validate(spec, attempt, previous=args.previous, units=args.units)
        result["research_diagnosis"] = diagnose_accounts(
            result, attempt, spec["id"], excluded=spec.get("excluded", ())
        )
        write_json(attempt / "analysis.json", result)
        return result

    state = execute_chain(
        args.output / args.campaign,
        select,
        execute,
        checked,
        context={
            "inputs": inputs,
            "sources": sources,
            "protocol": protocol,
            "seed_receipts": [str(path) for path in args.seed_receipt],
        },
    )
    write_json(
        args.output / "COMPLETION.json",
        {
            "status": state["status"],
            "experiments": len(seed_history) + len(state["history"]),
            "history": seed_history + state["history"],
        },
    )
    print(
        json.dumps(
            {"status": state["status"], "experiments": len(seed_history) + len(state["history"])}
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("output", "previous", "core", "units"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--campaign", default="chain")
    parser.add_argument("--seed-receipt", action="append", type=Path, default=[])
    main(parser.parse_args())
