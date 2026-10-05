"""Paired causal curve refits and explicit per-asset research risk eligibility."""

from __future__ import annotations

import argparse
import contextlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from adaptive_alpha_research import read_weights, write_json
from continuous_rebuild_research import run
from core_rebuild_research import ProjectedCovarianceAccount
from execute_research_continuation import reference_inputs, validate
from research_continuation import _digest, execute_chain
from strategy_rebuild_research import GROUPS, diagnose_accounts, verified_seed_receipt

from afuture.curve_rebuild import forecast_curve_rebuild
from afuture.execution_aligned_policy import FROZEN_PRODUCTS
from afuture.forecast_portfolio import forecast_covariance_portfolio


def optimized_targets(predictions, calendar, tape, *, eligibility):
    predictions = predictions.copy()
    predictions["decision_day"] = pd.to_datetime(predictions.decision_day)
    if predictions.duplicated(["decision_day", "product"]).any():
        raise ValueError("duplicate forecast identity")
    groups = dict(tuple(predictions.groupby("decision_day")))
    result = pd.DataFrame(0.0, index=calendar, columns=FROZEN_PRODUCTS)
    current = pd.Series(0.0, index=result.columns)
    audits = []
    for i, day in enumerate(calendar):
        if i % 5 == 0:
            rows = groups.get(day)
            forecasts = {} if rows is None else rows.set_index("product").expected_return.to_dict()
            allocation = forecast_covariance_portfolio(
                forecasts, tape, decision_day=day, risk_eligibility=eligibility
            )
            current[:] = 0.0
            for product, weight in allocation.weights.items():
                current[product] = weight
            audits.append(allocation.audit)
        result.loc[day] = current
    return result, audits


def refitted_targets(observations, calendar, *, pool, spec):
    exclusions = sorted(set(spec["excluded"]) | ({"AG"} if pool == "exAG" else set()))
    available = observations.loc[
        ~observations["product"].isin(exclusions) & observations.curve_status.eq("eligible")
    ].copy()
    training = available.loc[available.label_status.eq("observed")].copy()
    by_day = dict(tuple(available.groupby("entry_day")))
    result = pd.DataFrame(0.0, index=calendar, columns=FROZEN_PRODUCTS)
    current = pd.Series(0.0, index=result.columns)
    audits, forecasts = [], []
    for i, day in enumerate(calendar):
        if i % 5 == 0:
            targets = by_day.get(day, available.iloc[:0])
            prediction = forecast_curve_rebuild(
                training,
                targets,
                decision_day=day,
                excluded_products=exclusions,
                groups=GROUPS,
                grouped=spec["grouped"],
                seasonal=spec["seasonal"],
            )
            audits.append(prediction.audit)
            current[:] = 0.0
            if prediction.ready and len(prediction.forecasts):
                rows = prediction.forecasts.merge(
                    targets[["product", "symbol", "volatility"]],
                    on=["product", "symbol"],
                    validate="one_to_one",
                )
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


def profit_assessment(result, parent, *, control_only=False):
    """A registered intermediate cash improvement is separate from final acceptance."""
    cells = {}
    for pool in ("full", "exAG"):
        historical = result["metrics"]["historical"][pool + "_stress"]
        recent = result["metrics"]["recent"][pool + "_stress"]
        old = parent["metrics"]["historical"][pool + "_stress"]
        old_recent = parent["metrics"]["recent"][pool + "_stress"]
        cells[pool] = {
            "historical_net_profit_at_least_5000": historical["net_profit"] >= 5000,
            "historical_profit_increment": historical["net_profit"] - old["net_profit"],
            "historical_drawdown_change": historical["mdd"] - old["mdd"],
            "recent_net_profit": recent["net_profit"],
            "recent_profit_increment": recent["net_profit"] - old_recent["net_profit"],
        }
    improved = (
        not control_only
        and not result["any_risk_halt"]
        and all(
            cell["historical_net_profit_at_least_5000"]
            and cell["historical_profit_increment"] >= 5000
            and cell["historical_drawdown_change"] <= 1e-12
            and cell["recent_net_profit"] > 0
            and cell["recent_profit_increment"] >= -1e-6
            for cell in cells.values()
        )
    )
    return {
        "paired_profit_component_achieved": improved,
        "eligible_profit_claim": not control_only,
        "cells": cells,
        "economic_goal_achieved": False,
        "independent_sample": False,
    }


def main(args):
    protocol_path = args.output / "FROZEN_PROTOCOL.json"
    protocol = json.loads(protocol_path.read_text())
    inputs = {entry["path"]: entry["sha256"] for entry in protocol["inputs"]}
    inputs.update(reference_inputs(args.previous))
    inputs[str(protocol_path)] = _digest(protocol_path)
    tape = read_weights(Path(protocol["market_returns"]))
    observations = pd.read_csv(
        protocol["curve_observations"],
        parse_dates=["entry_day", "feature_through", "maturity_day"],
    )
    seeds = {}
    expected = {
        str(args.previous / "pair_inputs/specific.csv"): inputs[
            str(args.previous / "pair_inputs/specific.csv")
        ],
        str(args.units): inputs[str(args.units)],
    }
    for name, path in protocol["seed_receipts"].items():
        receipt = Path(path)
        seeds[name] = verified_seed_receipt(receipt, expected)
    dates = read_weights(
        Path(protocol["seed_receipts"]["T1"]).parent / "strategy/full/T1_weights.csv"
    ).index
    root = Path(__file__).resolve().parents[1]
    sources = {str(path): _digest(path) for path in sorted(root.rglob("*.py"))}

    def select(history):
        seen = {row["spec"]["id"]: row for row in history}
        for proposed in protocol["experiments"]:
            if proposed["id"] in seen:
                continue
            parent = seen.get(proposed["parent"])
            if parent is not None and parent["status"] == "invalid_evidence":
                continue
            spec = dict(proposed)
            spec["continuation_after"] = [
                {"id": row["spec"]["id"], "status": row["status"]} for row in history
            ]
            return spec
        return None

    def parent_record(spec, attempt):
        if spec["parent"] in seeds:
            return seeds[spec["parent"]]
        for path in sorted(attempt.parent.glob("*/complete.json")):
            record = json.loads(path.read_text())
            if record["spec"]["id"] == spec["parent"]:
                return record
        raise ValueError("registered parent receipt is missing")

    def execute(spec, attempt):
        strategy = attempt / "strategy"
        for pool in ("full", "exAG"):
            folder = strategy / pool
            folder.mkdir(parents=True)
            if spec["kind"] == "risk":
                seed = parent_record(spec, attempt)
                if spec.get("forecast_parent"):
                    seed = seeds[spec["forecast_parent"]]
                seed_path = next(
                    Path(path).parent
                    for name, path in protocol["seed_receipts"].items()
                    if name == seed["spec"]["id"]
                )
                predictions = pd.read_csv(seed_path / f"strategy/{pool}/predictions.csv")
                targets, audit = optimized_targets(
                    predictions, tape.index, tape, eligibility=spec["eligibility"]
                )
            else:
                targets, audit, predictions = refitted_targets(
                    observations, tape.index, pool=pool, spec=spec
                )
            excluded = set(spec.get("excluded", [])) | ({"AG"} if pool == "exAG" else set())
            if any(targets[product].ne(0).any() for product in excluded):
                raise ValueError("excluded product in fitted target")
            targets.loc[dates].to_csv(folder / f"{spec['id']}_weights.csv", index_label="date")
            predictions.to_csv(folder / "predictions.csv", index=False)
            write_json(folder / "model_audit.json", audit)

        def account_type(market, config, *, completed_concentrations):
            return ProjectedCovarianceAccount(
                market,
                config,
                market_returns=tape,
                completed_concentrations=completed_concentrations,
            )

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
        result["profit_assessment"] = profit_assessment(
            result,
            parent_record(spec, attempt)["result"],
            control_only=spec.get("control_only", False),
        )
        write_json(attempt / "analysis.json", result)
        return result

    state = execute_chain(
        args.output / "chain",
        select,
        execute,
        checked,
        context={"inputs": inputs, "sources": sources, "protocol": protocol},
    )
    write_json(args.output / "COMPLETION.json", state)
    print(json.dumps({"status": state["status"], "experiments": len(state["history"])}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("output", "previous", "units"):
        parser.add_argument("--" + name, required=True, type=Path)
    main(parser.parse_args())
