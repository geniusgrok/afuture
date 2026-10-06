"""Causal residual experts, lifecycle reductions and one-account legacy mixtures.

This offline runner consumes a frozen protocol and archived causal predictions.
Every registered recipe is attempted in order, including after economic failure.
The existing account owns concrete fills, integer lots, fees and hard risk gates.
"""

from __future__ import annotations

import argparse
import contextlib
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
from adaptive_alpha_research import PRODUCT_MULTIPLIERS, read_weights, write_json
from continuous_rebuild_research import run
from core_rebuild_research import ProjectedCovarianceAccount
from execute_research_continuation import reference_inputs, validate
from holding_exit_research import HoldingExitAccount, trailing_step
from profit_rebuild_research import refitted_targets
from research_continuation import _digest, execute_chain
from strategy_rebuild_research import GROUPS, diagnose_accounts, verified_seed_receipt

from afuture.curve_experts import choose_mature_expert, forecast_curve_expert
from afuture.execution_aligned_policy import FROZEN_PRODUCTS

PREDICTION_COLUMNS = [
    "entry_day",
    "product",
    "symbol",
    "expected_return",
    "volatility",
    "decision_day",
    "training_through",
]


def next_registered_recipe(experiments, history):
    """Economic failures never suppress a different registered mechanism."""
    identities = [spec["id"] for spec in experiments]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate registered recipe identity")
    seen = {row["spec"]["id"] for row in history}
    for proposed in experiments:
        if proposed["id"] not in seen:
            return {
                **proposed,
                "continuation_after": [
                    {"id": row["spec"]["id"], "status": row["status"]} for row in history
                ],
            }
    return None


def causal_archive(predictions, audit):
    """Bind each prediction to its actual archived training maturity evidence."""
    result = predictions.copy()
    for name in ("entry_day", "decision_day"):
        result[name] = pd.to_datetime(result[name])
    if result.duplicated(["entry_day", "product"]).any():
        raise ValueError("duplicate causal forecast identity")
    evidence = pd.DataFrame(audit)
    if evidence.empty:
        if len(result):
            raise ValueError("forecast archive lacks model audits")
        result["training_through"] = pd.Series(dtype="datetime64[ns]")
        return result
    evidence["decision_day"] = pd.to_datetime(evidence.decision_day)
    if evidence.decision_day.duplicated().any():
        raise ValueError("duplicate model decision audit")
    evidence["training_through"] = pd.to_datetime(evidence.last_maturity_day)
    if "training_through" in result:
        result = result.drop(columns="training_through")
    result = result.merge(
        evidence[["decision_day", "training_through"]],
        on="decision_day",
        how="left",
        validate="many_to_one",
    )
    if (
        not result.entry_day.eq(result.decision_day).all()
        or result.training_through.isna().any()
        or not result.training_through.lt(result.entry_day).all()
        or not np.isfinite(result.expected_return.to_numpy(dtype=float)).all()
    ):
        raise ValueError("forecast lacks strict prior archived training evidence")
    return result


def attach_parent_observations(observations, predictions):
    """Missing OOF forecasts stay missing; no in-sample parent labels are filled."""
    archive = predictions.rename(
        columns={
            "expected_return": "parent_expected_return",
            "training_through": "parent_training_through",
            "decision_day": "parent_decision_day",
        }
    ).copy()
    archive["parent_product"] = archive["product"]
    archive["parent_symbol"] = archive["symbol"]
    columns = [
        "entry_day",
        "product",
        "symbol",
        "parent_expected_return",
        "parent_training_through",
        "parent_decision_day",
        "parent_product",
        "parent_symbol",
    ]
    return observations.merge(
        archive[columns], on=["entry_day", "product", "symbol"], how="left", validate="one_to_one"
    )


def score_forecasts(rows):
    """Preserve T2's five-session forecast threshold and capped score allocation."""
    current = pd.Series(0.0, index=FROZEN_PRODUCTS)
    if rows.empty:
        return current
    if rows["product"].duplicated().any():
        raise ValueError("more than one forecast per product")
    values = rows[["expected_return", "volatility"]].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values[:, 1] <= 0).any():
        raise ValueError("invalid score forecast")
    if not rows["product"].isin(FROZEN_PRODUCTS).all():
        raise ValueError("unknown score product")
    scores = (rows.expected_return.abs() - 0.003).clip(lower=0) / (5 * rows.volatility.pow(2))
    total = float(scores.sum())
    if total > 0:
        amounts = (2 * scores / total).clip(upper=0.25)
        for product, weight in zip(
            rows["product"], np.sign(rows.expected_return) * amounts, strict=True
        ):
            current[product] = float(weight)
    return current


def predictions_to_targets(predictions, calendar):
    groups = dict(tuple(predictions.groupby("decision_day")))
    result = pd.DataFrame(0.0, index=calendar, columns=FROZEN_PRODUCTS)
    current = pd.Series(0.0, index=result.columns)
    for position, day in enumerate(calendar):
        if position % 5 == 0:
            current = score_forecasts(groups.get(day, predictions.iloc[:0]))
        result.loc[day] = current
    return result


def expert_targets(observations, parent_predictions, calendar, *, pool, mechanism):
    exclusions = ("AG",) if pool == "exAG" else ()
    available = observations.loc[
        ~observations["product"].isin(exclusions) & observations.curve_status.eq("eligible")
    ].copy()
    available = attach_parent_observations(available, parent_predictions)
    training = available.loc[available.label_status.eq("observed")].copy()
    by_day = dict(tuple(available.groupby("entry_day")))
    parent_by_day = dict(tuple(parent_predictions.groupby("decision_day")))
    audits, forecasts = [], []
    for position, day in enumerate(calendar):
        if position % 5:
            continue
        targets = by_day.get(day, available.iloc[:0])
        if mechanism == "seasonal_residual":
            targets = targets.loc[targets.parent_expected_return.notna()].copy()
        prediction = forecast_curve_expert(
            training,
            targets,
            decision_day=day,
            mechanism=mechanism,
            excluded_products=exclusions,
            groups=GROUPS,
            grouped=True,
        )
        audit = dict(prediction.audit)
        audit["correction_ready"] = (
            bool(prediction.ready) if mechanism in ("seasonal_residual", "residual_only") else None
        )
        audit["fallback_to_archived_T2"] = False
        if prediction.ready:
            rows = prediction.forecasts.merge(
                targets[["product", "symbol", "volatility"]],
                on=["product", "symbol"],
                validate="one_to_one",
            )
            rows["decision_day"] = day
            rows["training_through"] = pd.Timestamp(audit["last_maturity_day"])
        elif mechanism == "seasonal_residual":
            rows = parent_by_day.get(day, parent_predictions.iloc[:0]).copy()
            audit["fallback_to_archived_T2"] = bool(len(rows))
            audit["correction_ready"] = False
            audit.setdefault("residual_last_maturity_day", audit["last_maturity_day"])
            # Retain the real training maturity of the unchanged parent forecast.
            if len(rows):
                audit["last_maturity_day"] = rows.training_through.max().date().isoformat()
        else:
            rows = pd.DataFrame(columns=PREDICTION_COLUMNS)
        audits.append(audit)
        if len(rows):
            forecasts.append(rows)
    predictions = (
        pd.concat(forecasts, ignore_index=True)
        if forecasts
        else pd.DataFrame(columns=PREDICTION_COLUMNS)
    )
    return predictions_to_targets(predictions, calendar), audits, predictions


def equal_forecasts(current_experts, fallback):
    """Average available current paired identities; no realized outcome is read."""
    if any(frame.empty for frame in current_experts.values()):
        return fallback.copy(), True
    names = list(current_experts)
    joined = None
    for name in names:
        frame = current_experts[name]
        piece = frame[["entry_day", "product", "symbol", "expected_return"]].rename(
            columns={"expected_return": name}
        )
        joined = (
            piece
            if joined is None
            else joined.merge(piece, on=["entry_day", "product", "symbol"], validate="one_to_one")
        )
    if joined is None or joined.empty:
        return fallback.copy(), True
    rows = joined[["entry_day", "product", "symbol"]].copy()
    rows["expected_return"] = joined[names].mean(axis=1)
    reference = current_experts[names[0]]
    rows = rows.merge(
        reference[["entry_day", "product", "symbol", "volatility", "decision_day"]],
        on=["entry_day", "product", "symbol"],
        validate="one_to_one",
    )
    rows["training_through"] = max(
        frame.training_through.max() for frame in current_experts.values()
    )
    return rows, False


def ensemble_targets(experts, observations, calendar, *, pool, mode):
    exclusions = ("AG",) if pool == "exAG" else ()
    by_expert = {
        name: dict(tuple(frame.groupby("decision_day"))) for name, frame in experts.items()
    }
    base = experts["C_T2"]
    forecasts, audits = [], []
    for position, day in enumerate(calendar):
        if position % 5:
            continue
        current = {name: rows.get(day, experts[name].iloc[:0]) for name, rows in by_expert.items()}
        fallback = current["C_T2"]
        if mode == "static":
            rows, fallback_used = equal_forecasts(current, fallback)
            audit = {
                "decision_day": day.date().isoformat(),
                "mechanism": "fixed_equal_forecast_control",
                "expert_order": list(experts),
                "fallback_to_archived_T2": fallback_used and bool(len(rows)),
                "last_maturity_day": rows.training_through.max().date().isoformat()
                if len(rows)
                else None,
            }
        elif mode == "causal":
            choice = choose_mature_expert(
                experts,
                observations,
                decision_day=day,
                groups=GROUPS,
                excluded_products=exclusions,
            )
            audit = dict(choice.audit)
            audit["fallback_to_archived_T2"] = not choice.ready and bool(len(fallback))
            if choice.ready:
                rows = choice.forecasts.merge(
                    fallback[["product", "symbol", "volatility"]],
                    on=["product", "symbol"],
                    validate="one_to_one",
                )
                rows["decision_day"] = day
                # The model binds both selector labels and actually used current forecasts.
                rows["training_through"] = pd.Timestamp(audit["last_maturity_day"])
            else:
                rows = fallback.copy()
                if len(rows):
                    audit["last_maturity_day"] = rows.training_through.max().date().isoformat()
        else:
            raise ValueError("unknown frozen ensemble mechanism")
        audits.append(audit)
        if len(rows):
            forecasts.append(rows[PREDICTION_COLUMNS])
    predictions = pd.concat(forecasts, ignore_index=True) if forecasts else base.iloc[:0].copy()
    return predictions_to_targets(predictions, calendar), audits, predictions


def one_account_targets(components):
    """Net continuous targets before one common account sizes or trades lots."""
    if not components:
        raise ValueError("single-account mixture requires components")
    frames = list(components.values())
    first = frames[0]
    if any(
        not first.index.equals(frame.index) or not first.columns.equals(frame.columns)
        for frame in frames
    ):
        raise ValueError("single-account component identities differ")
    if any(not np.isfinite(frame.to_numpy()).all() for frame in frames):
        raise ValueError("nonfinite component target")
    result = sum(frames[1:], first.copy()) / len(frames)
    if (result.abs().sum(axis=1) > 2 + 1e-10).any():
        raise ValueError("single-account target exceeds common gross budget")
    return result


def incremental_account_targets(legacy, curve):
    """Reserve legacy requests first; admit compatible curve only in spare capacity."""
    if not legacy.index.equals(curve.index) or not legacy.columns.equals(curve.columns):
        raise ValueError("incremental component identities differ")
    if not np.isfinite(legacy.to_numpy()).all() or not np.isfinite(curve.to_numpy()).all():
        raise ValueError("nonfinite incremental component target")
    if (legacy.abs().sum(axis=1) > 2 + 1e-10).any():
        raise ValueError("legacy target exceeds common gross budget")
    compatible = legacy * curve >= 0
    spare_product = (0.25 - legacy.abs()).clip(lower=0)
    requested = curve.abs().clip(upper=spare_product).where(compatible, 0.0)
    spare_gross = (2 - legacy.abs().sum(axis=1)).clip(lower=0)
    total = requested.sum(axis=1)
    scale = (spare_gross / total.replace(0, np.nan)).clip(upper=1).fillna(0)
    additions = np.sign(curve) * requested.mul(scale, axis=0)
    result = legacy + additions
    if (
        (result.abs().sum(axis=1) > 2 + 1e-10).any()
        or (result.abs() + 1e-12 < legacy.abs()).any().any()
        or (result * legacy < -1e-12).any().any()
    ):
        raise ValueError("incremental target changed reserved legacy requests")
    return result


def conflict_reduced_account_targets(legacy, curve):
    """Use opposing causal requests only to reduce reserved legacy exposure.

    Compatible additions retain the original incremental allocation. Released
    capacity stays unused; opposing requests cannot reverse legacy positions.
    Account HHI, reserve, integer projection and hard limits remain downstream.
    """
    baseline = incremental_account_targets(legacy, curve)
    reduction = curve.abs().clip(upper=legacy.abs()).where(legacy * curve < 0, 0.0)
    return baseline - np.sign(legacy) * reduction


def corroborated_account_targets(legacy, curve):
    """Reserve legacy capacity only where a mature curve request agrees.

    An absent or opposing curve request grants no legacy reservation. Curve
    requests then use the unchanged incremental caps in the single account.
    This is a causal admission rule, not selection by realized component PnL.
    """
    incremental_account_targets(legacy, curve)  # Validate both component identities.
    confirmed = legacy.where(legacy * curve > 0, 0.0)
    return incremental_account_targets(confirmed, curve)


def reduce_incumbent_resizing(final_lots, current_lots):
    """Suppress same-sign incumbent additions while retaining every risk reduction."""
    result = dict(final_lots)
    for symbol, target in final_lots.items():
        current = int(current_lots.get(symbol, 0))
        if target * current > 0 and abs(target) > abs(current):
            result[symbol] = current
    return result


class InertiaAccount(ProjectedCovarianceAccount):
    def __init__(self, *args, decision_days, **kwargs):
        super().__init__(*args, **kwargs)
        self.decision_days = set(decision_days)

    def target_lot_stages(self, **kwargs):
        baseline = super().target_lot_stages(**kwargs)
        if self.day in self.decision_days:
            return baseline
        final = reduce_incumbent_resizing(baseline.final_lots, kwargs.get("current_lots") or {})
        notional = sum(
            abs(quantity)
            * kwargs["product_open_prices"].get(self._product(symbol), 0.0)
            * PRODUCT_MULTIPLIERS[self._product(symbol)]
            for symbol, quantity in final.items()
        )
        return replace(baseline, final_lots=final, final_notional=float(notional))


class ProjectedHoldingCovarianceAccount(ProjectedCovarianceAccount):
    """Apply the existing E1 reduction rule to the same fill-owned shared account."""

    def __init__(self, market, config=None, *, market_returns, completed_concentrations=()):
        ProjectedCovarianceAccount.__init__(
            self,
            market,
            config,
            market_returns=market_returns,
            completed_concentrations=completed_concentrations,
        )
        self.market = {
            symbol: frame.set_index("date").sort_index()
            for symbol, frame in market.groupby("symbol", sort=False)
        }
        self.tracks = {}
        self.blocked = {}

    def observe_target_state(self, *, day, product_weights):
        super().observe_target_state(day=day, product_weights=product_weights)
        for product, sign in list(self.blocked.items()):
            target = float(product_weights.get(product, 0.0))
            if target == 0.0 or target * sign < 0.0:
                del self.blocked[product]

    def target_lot_stages(self, **kwargs):
        baseline = super().target_lot_stages(**kwargs)
        current = {
            symbol: lots for symbol, lots in (kwargs.get("current_lots") or {}).items() if lots
        }
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
        final = {
            symbol: quantity
            for symbol, quantity in baseline.final_lots.items()
            if quantity * self.blocked.get(self._product(symbol), 0) <= 0
        }
        notional = sum(
            abs(quantity)
            * kwargs["product_open_prices"].get(self._product(symbol), 0.0)
            * PRODUCT_MULTIPLIERS[self._product(symbol)]
            for symbol, quantity in final.items()
        )
        return replace(baseline, final_lots=final, final_notional=float(notional))


def paired_metrics(result, parent):
    return {
        window: {
            label: {
                "net_profit_change": metrics["net_profit"]
                - parent["metrics"][window][label]["net_profit"],
                "gross_pnl_change": metrics["gross_pnl"]
                - parent["metrics"][window][label]["gross_pnl"],
                "fees_change": metrics["fees"] - parent["metrics"][window][label]["fees"],
                "drawdown_change": metrics["mdd"] - parent["metrics"][window][label]["mdd"],
            }
            for label, metrics in cells.items()
        }
        for window, cells in result["metrics"].items()
    }


def original_result(protocol, name):
    metrics = {}
    for window, path in protocol["original_comparisons"][name].items():
        summary = json.loads(Path(path).read_text())
        metrics[window] = {
            label: summary[f"{name}_{label}"]
            for label in ("full_base", "full_stress", "exAG_base", "exAG_stress")
        }
    return {"metrics": metrics}


def main(args):
    protocol_path = args.output / "FROZEN_PROTOCOL.json"
    protocol = json.loads(protocol_path.read_text())
    inputs = {entry["path"]: entry["sha256"] for entry in protocol["inputs"]}
    for appendix_name in ("SUPPLEMENTAL_PROTOCOL.json", "SUPPLEMENTAL_LEGACY_PROTOCOL.json"):
        appendix_path = args.output / appendix_name
        if not appendix_path.exists():
            continue
        appendix = json.loads(appendix_path.read_text())
        if appendix["base_protocol_sha256"] != _digest(protocol_path):
            raise ValueError("supplemental protocol base identity differs")
        protocol = {**protocol, "experiments": [*protocol["experiments"], *appendix["experiments"]]}
        inputs.update({entry["path"]: entry["sha256"] for entry in appendix.get("inputs", [])})
        inputs[str(appendix_path)] = _digest(appendix_path)
    inputs.update(reference_inputs(args.previous))
    inputs[str(protocol_path)] = _digest(protocol_path)
    tape = read_weights(Path(protocol["market_returns"]))
    observations = pd.read_csv(
        protocol["curve_observations"],
        parse_dates=["entry_day", "feature_through", "maturity_day"],
    )
    expected = {
        str(args.previous / "pair_inputs/specific.csv"): inputs[
            str(args.previous / "pair_inputs/specific.csv")
        ],
        str(args.units): inputs[str(args.units)],
    }
    seeds = {
        name: verified_seed_receipt(Path(path), expected)
        for name, path in protocol["seed_receipts"].items()
    }
    t2_folder = Path(protocol["seed_receipts"]["T2"]).parent
    dates = read_weights(t2_folder / "strategy/full/T2_weights.csv").index
    sources = {
        str(path): _digest(path)
        for folder in (Path(__file__).resolve().parents[1] / "afuture", Path(__file__).parent)
        for path in sorted(folder.rglob("*.py"))
    }
    originals = {name: original_result(protocol, name) for name in ("B0", "E1")}

    def completed(name, attempt):
        if name in seeds:
            return seeds[name]
        for path in sorted(attempt.parent.glob("*/complete.json")):
            record = json.loads(path.read_text())
            if record["spec"]["id"] == name:
                if record["status"] == "invalid_evidence":
                    raise ValueError("dependent recipe lacks valid causal parent evidence")
                return record
        raise ValueError("registered parent receipt missing")

    def folder_for(name, attempt):
        if name in seeds:
            return Path(protocol["seed_receipts"][name]).parent
        return attempt.parent / completed(name, attempt)["attempt"]

    def load_predictions(name, pool, attempt):
        folder = folder_for(name, attempt) / "strategy" / pool
        predictions = pd.read_csv(folder / "predictions.csv")
        audit = json.loads((folder / "model_audit.json").read_text())
        return causal_archive(predictions, audit)

    def execute(spec, attempt):
        strategy = attempt / "strategy"
        for pool in ("full", "exAG"):
            folder = strategy / pool
            folder.mkdir(parents=True)
            kind = spec["kind"]
            audit = []
            predictions = pd.DataFrame(columns=PREDICTION_COLUMNS)
            if kind == "seed":
                source = t2_folder / "strategy" / pool
                targets = read_weights(source / "T2_weights.csv")
                predictions = load_predictions("T2", pool, attempt)
                audit = json.loads((source / "model_audit.json").read_text())
                reconstructed = predictions_to_targets(predictions, tape.index).loc[dates]
                if not np.allclose(
                    targets.to_numpy(), reconstructed.to_numpy(), rtol=0, atol=1e-12
                ):
                    raise ValueError("archived T2 scores no longer reproduce exact parent targets")
            elif kind == "curve":
                targets, audit, predictions = refitted_targets(
                    observations,
                    tape.index,
                    pool=pool,
                    spec={"excluded": [], "grouped": True, "seasonal": True},
                )
                predictions = causal_archive(predictions, audit)
            elif kind == "expert":
                targets, audit, predictions = expert_targets(
                    observations,
                    load_predictions("T2", pool, attempt),
                    tape.index,
                    pool=pool,
                    mechanism=spec["mechanism"],
                )
            elif kind == "ensemble":
                experts = {
                    name: load_predictions(name, pool, attempt) for name in spec["components"]
                }
                targets, audit, predictions = ensemble_targets(
                    experts, observations, tape.index, pool=pool, mode=spec["mechanism"]
                )
            elif kind in ("inertia", "curve_exit"):
                source = folder_for(spec["parent"], attempt) / "strategy" / pool
                targets = read_weights(source / f"{spec['parent']}_weights.csv")
                predictions = load_predictions(spec["parent"], pool, attempt)
                audit = json.loads((source / "model_audit.json").read_text())
            elif kind == "legacy":
                targets = read_weights(Path(protocol["legacy_weights"][pool]))
            elif kind in ("mix", "mix_incremental"):
                components = {
                    name: read_weights(
                        folder_for(name, attempt) / f"strategy/{pool}/{name}_weights.csv"
                    ).loc[dates]
                    for name in spec["components"]
                }
                if kind == "mix_incremental":
                    if list(components) != ["C_LEGACY", "X_CAUSAL"]:
                        raise ValueError("incremental mix requires frozen legacy/causal components")
                    targets = incremental_account_targets(
                        components["C_LEGACY"], components["X_CAUSAL"]
                    )
                else:
                    targets = one_account_targets(components)
                for name, frame in components.items():
                    frame.to_csv(folder / f"component_{name}.csv", index_label="date")
                audit = [
                    {
                        "mechanism": "legacy_reserved_curve_increment"
                        if kind == "mix_incremental"
                        else "fixed_equal_netted_targets",
                        "components": list(components),
                        "old_new_budget_selector": False,
                    }
                ]
            else:
                raise ValueError("unknown frozen recipe kind")
            targets = targets.loc[dates]
            if pool == "exAG" and targets.AG.ne(0).any():
                raise ValueError("excluded product in independent account targets")
            targets.to_csv(folder / f"{spec['id']}_weights.csv", index_label="date")
            predictions.to_csv(folder / "predictions.csv", index=False)
            write_json(folder / "model_audit.json", audit)

        def account_type(market, config, *, completed_concentrations):
            if spec.get("account_package") == "legacy":
                return HoldingExitAccount(
                    market, config, completed_concentrations=completed_concentrations
                )
            kwargs = {
                "market_returns": tape,
                "completed_concentrations": completed_concentrations,
            }
            if spec["kind"] in ("curve_exit", "legacy", "mix", "mix_incremental"):
                return ProjectedHoldingCovarianceAccount(market, config, **kwargs)
            if spec["kind"] == "inertia":
                return InertiaAccount(market, config, decision_days=tape.index[::5], **kwargs)
            return ProjectedCovarianceAccount(market, config, **kwargs)

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
        result["research_diagnosis"] = diagnose_accounts(result, attempt, spec["id"], excluded=())
        if spec["parent"] in originals:
            parent = originals[spec["parent"]]
        else:
            parent = completed(spec["parent"], attempt)["result"]
        result["paired_comparison"] = paired_metrics(result, parent)
        result["original_comparisons"] = {
            **{name: paired_metrics(result, original) for name, original in originals.items()},
            "T2": paired_metrics(result, seeds["T2"]["result"]),
        }
        curve_parent = spec.get("curveparent", spec.get("curve_parent"))
        if curve_parent:
            result["curve_component_comparison"] = paired_metrics(
                result, completed(curve_parent, attempt)["result"]
            )
        result["eligible_profit_claim"] = not spec.get("control_only", False)
        result["original_economic_gate_passed"] = result["economic_passed"]
        # Historical financial gates are not independent validation or a live goal pass.
        result["economic_passed"] = bool(
            result["economic_passed"]
            and result["eligible_profit_claim"]
            and all(
                result["metrics"]["recent"][pool + "_stress"]["net_profit"] > 0
                for pool in ("full", "exAG")
            )
        )
        result["scope"] = (
            "paired historical/recent development; new independent validation unavailable"
        )
        result["economic_goal_achieved"] = False
        write_json(attempt / "analysis.json", result)
        return result

    state = execute_chain(
        args.output / "chain",
        lambda history: next_registered_recipe(protocol["experiments"], history),
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
