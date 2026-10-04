"""Factorial core replacement with real, ownership-independent market observations."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
from adaptive_alpha_research import PRODUCT_MULTIPLIERS, read_weights, write_json
from continuous_rebuild_research import run
from execute_research_continuation import reference_inputs, validate
from lot_projection_research import LotProjectionAccount, project_integer_targets
from research_continuation import execute_chain

from afuture.directional_acceptance import DirectionalProductionAcceptance
from afuture.directional_robustness import CovarianceBudgetDirectionalProductionAcceptance


def market_return_tape(market, calendar):
    """Choose contracts before observing each day's outcome; never splice roll prices."""
    account = DirectionalProductionAcceptance()
    normalized = account._normalize_contracts(market)
    by_date = {day: rows for day, rows in normalized.groupby("date", sort=True)}
    closes = normalized.set_index(["date", "symbol"]).close
    products = sorted(normalized["product"].unique())
    result = pd.DataFrame(np.nan, index=calendar, columns=products)
    audit = []
    for position, day in enumerate(calendar):
        if not position:
            continue
        prior = calendar[position - 1]
        snapshot = by_date.get(prior)
        if snapshot is None:
            continue
        selected = account._select_contracts_from_snapshot(snapshot, day)
        for product, symbol in selected.items():
            before = closes.get((prior, symbol), np.nan)
            after = closes.get((day, symbol), np.nan)
            valid = bool(np.isfinite([before, after]).all() and min(before, after) > 0)
            value = float(after / before - 1.0) if valid else np.nan
            result.loc[day, product] = value
            audit.append(
                {
                    "date": day,
                    "selection_through": prior,
                    "product": product,
                    "symbol": symbol,
                    "previous_close": before,
                    "close": after,
                    "return": value,
                    "status": "marked" if valid else "missing_same_contract_mark",
                }
            )
    return result, pd.DataFrame(audit)


class ProjectedCovarianceAccount(CovarianceBudgetDirectionalProductionAcceptance):
    """Keep the control's lot projector while replacing its complete soft-risk package."""

    def __init__(self, market, config=None, *, market_returns, completed_concentrations=()):
        del market, completed_concentrations
        super().__init__(config, market_returns=market_returns)
        self.exit_audit = []
        self.projection_audit = []
        self.day = None

    def observe_target_state(self, *, day, product_weights):
        self.day = day
        super().observe_target_state(day=day, product_weights=product_weights)

    def target_lot_stages(self, **kwargs):
        projection = project_integer_targets(
            equity=float(kwargs["equity"]),
            product_weights=kwargs["product_weights"],
            product_open_prices=kwargs["product_open_prices"],
            selected_symbols=kwargs["selected_symbols"],
            multipliers=PRODUCT_MULTIPLIERS,
            max_lots=self.config.max_contract_volume,
        )
        baseline = super().target_lot_stages(**dict(kwargs, product_weights=projection.weights))
        if baseline.raw_integer_lots != projection.lots:
            raise ValueError("core replacement changed shared integer projection")
        self.projection_audit.append(
            {
                "date": self.day,
                "original_continuous_budget": projection.original_budget,
                "projected_notional": projection.projected_notional,
                "margin_fitted_notional": baseline.margin_fitted_notional,
                "final_notional": baseline.final_notional,
            }
        )
        return replace(
            baseline,
            desired_notional=projection.original_budget,
            integer_rounding_loss_notional=max(
                0.0, projection.available_budget - projection.projected_notional
            ),
            max_volume_clipping_notional=projection.volume_clipping_notional,
            unavailable_contract_notional=projection.unavailable_notional,
        )


def stage_assessment(result, control):
    """Keep falsifiable intermediate improvements even below the final wealth goal."""
    if result["economic_passed"]:
        return {"status": "candidate_for_validation", "reason": "original core passed"}
    reasons = []
    for pool in ("full", "exAG"):
        new = result["metrics"]["historical"][pool + "_stress"]
        old = control["metrics"]["historical"][pool + "_stress"]
        if new["halted"]:
            continue
        if new["net_profit"] > old["net_profit"] and new["mdd"] <= old["mdd"]:
            reasons.append(pool + ": improved stress profit with no worse drawdown")
        if new["gross_pnl"] >= old["gross_pnl"] and new["fees"] < old["fees"]:
            reasons.append(pool + ": preserved gross benefit with lower cost")
    return {
        "status": "retain_for_rebuild" if reasons else "replace_mechanism",
        "reason": reasons or ["no registered matched improvement; diagnose gross/cost/risk"],
        "final_economic_goal_passed": False,
        "independent_validation": False,
    }


def main(args):
    protocol_path = args.output / "CORE_PROTOCOL.json"
    protocol = json.loads(protocol_path.read_text())
    control_path = Path(protocol["prior_control"])
    control = json.loads((control_path / "complete.json").read_text())["result"]
    inputs = {row["path"]: row["sha256"] for row in protocol["inputs"]}
    inputs[str(protocol_path)] = hashlib.sha256(protocol_path.read_bytes()).hexdigest()
    inputs.update(reference_inputs(args.previous))
    tape_path = args.output / "inputs/market_returns.csv"
    if not tape_path.exists():
        market = pd.read_csv(args.previous / "pair_inputs/specific.csv", parse_dates=["date"])
        calendar = pd.DatetimeIndex(sorted(market.date.unique()))
        tape, audit = market_return_tape(market, calendar)
        tape_path.parent.mkdir()
        tape.to_csv(tape_path, index_label="date")
        audit.to_csv(tape_path.with_name("market_return_audit.csv"), index=False)
    for path in tape_path.parent.iterdir():
        inputs[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    tape = read_weights(tape_path)
    sources = {}
    root = Path(__file__).resolve().parents[1]
    for path in sorted(
        list((root / "afuture").rglob("*.py")) + list((root / "tools").glob("*.py"))
    ):
        sources[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()

    def select(history):
        seen = {row["spec"]["id"] for row in history}
        return next((item for item in protocol["experiments"] if item["id"] not in seen), None)

    def execute(spec, attempt):
        strategy = attempt / "strategy"
        for pool in ("full", "exAG"):
            name = "R1_weights" if spec["id"] == "R1_RISK" else "R1_raw"
            weights = read_weights(control_path / f"strategy/{pool}/{name}.csv")
            # Preserve the exact previous account calendar, including its flat warmup.
            dates = read_weights(control_path / f"strategy/{pool}/R1_weights.csv").index
            weights = weights.loc[dates]
            folder = strategy / pool
            folder.mkdir(parents=True)
            weights.to_csv(folder / f"{spec['id']}_weights.csv", index_label="date")

        def account_type(market, config, *, completed_concentrations):
            if spec["id"] == "R1_RAW":
                return LotProjectionAccount(
                    market, config, completed_concentrations=completed_concentrations
                )
            return ProjectedCovarianceAccount(market, config, market_returns=tape)

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
        result["development_assessment"] = stage_assessment(result, control)
        write_json(attempt / "analysis.json", result)
        return result

    state = execute_chain(
        args.output / "factorial",
        select,
        execute,
        checked,
        context={"sources": sources, "inputs": inputs, "protocol": protocol},
    )
    print(json.dumps({"status": state["status"], "experiments": len(state["history"])}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("output", "previous", "units"):
        parser.add_argument("--" + name, required=True, type=Path)
    main(parser.parse_args())
