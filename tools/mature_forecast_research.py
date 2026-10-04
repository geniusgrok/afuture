"""Replace the alpha core with ownership-independent, strictly matured market labels."""

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
from core_rebuild_research import ProjectedCovarianceAccount, stage_assessment
from execute_research_continuation import reference_inputs, validate
from research_continuation import execute_chain

from afuture.execution_aligned_policy import FROZEN_PRODUCTS
from afuture.mature_market_forecast import completed_pressure_features, forecast_from_mature_market


def build_observations(market, selections, calendar):
    """Keep selected entries even when their later label is missing; no outcome screening."""
    frame = market.copy().sort_values(["symbol", "date"])
    frame["date"] = pd.to_datetime(frame.date)
    if frame.duplicated(["date", "symbol"]).any():
        raise ValueError("duplicate specific-contract observations")
    prior_session = dict(zip(calendar[1:], calendar[:-1], strict=True))
    grouped = frame.groupby("symbol", sort=False)
    contiguous = grouped.date.shift(1).eq(frame.date.map(prior_session))
    frame["previous_close"] = grouped.close.shift(1).where(contiguous)
    frame["previous_oi"] = grouped.hold.shift(1).where(contiguous)
    frame["same_contract_return"] = frame.close / frame.previous_close - 1.0
    frame["volatility"] = frame.groupby("symbol", sort=False).same_contract_return.transform(
        lambda values: values.rolling(20, min_periods=20).std(ddof=1)
    )
    indexed = frame.set_index(["date", "symbol"])
    endpoints = {day: calendar[i + 5] for i, day in enumerate(calendar[:-5])}
    records = []
    for selected in selections.itertuples(index=False):
        day, prior, symbol = selected.date, selected.selection_through, selected.symbol
        entry = indexed.loc[(prior, symbol)]
        maturity = endpoints.get(day, pd.NaT)
        status, features = "feature_unavailable", dict.fromkeys(("x1", "x2", "x3"), np.nan)
        sigma = float(entry.volatility)
        raw = [
            entry.open,
            entry.high,
            entry.low,
            entry.close,
            entry.previous_close,
            entry.hold,
            entry.previous_oi,
            entry.volume,
        ]
        if np.isfinite(raw).all() and np.isfinite(sigma) and sigma > 0 and entry.volume > 0:
            try:
                features = completed_pressure_features(
                    **dict(
                        zip(
                            (
                                "open_price",
                                "high",
                                "low",
                                "close",
                                "previous_close",
                                "open_interest",
                                "previous_open_interest",
                                "volume",
                            ),
                            map(float, raw),
                            strict=True,
                        )
                    )
                )
                status = "eligible"
            except ValueError:
                status = "invalid_completed_feature"
        start = indexed.open.get((day, symbol), np.nan)
        end = indexed.open.get((maturity, symbol), np.nan)
        label_available = bool(np.isfinite([start, end]).all() and min(start, end) > 0)
        records.append(
            {
                "entry_day": day,
                "feature_through": prior,
                "maturity_day": maturity,
                "product": selected.product,
                "symbol": symbol,
                **features,
                "volatility": sigma,
                "gross_return": float(end / start - 1) if label_available else np.nan,
                "feature_status": status,
                "label_status": "observed" if label_available else "unavailable_endpoint",
            }
        )
    return pd.DataFrame(records)


def forecast_weights(observations, calendar, *, pool):
    """Use a fixed stress round-trip reserve; never reuse the legacy trend gate."""
    if pool not in ("full", "exAG"):
        raise ValueError("unknown forecast pool")
    available = observations.loc[observations.feature_status.eq("eligible")].copy()
    training = available.loc[available.label_status.eq("observed")].copy()
    result = pd.DataFrame(0.0, index=calendar, columns=FROZEN_PRODUCTS)
    current = pd.Series(0.0, index=result.columns)
    forecasts, audits = [], []
    targets_by_day = {day: rows for day, rows in available.groupby("entry_day")}
    empty = available.iloc[:0]
    for i, day in enumerate(calendar):
        if i % 5 == 0:
            targets = targets_by_day.get(day, empty)
            forecast = forecast_from_mature_market(
                training, targets, decision_day=day, exclude_ag=pool == "exAG"
            )
            current[:] = 0.0
            audits.append({"decision_day": day.isoformat(), **forecast.audit})
            if forecast.ready and len(forecast.forecasts):
                rows = forecast.forecasts.merge(
                    targets[["product", "symbol", "volatility"]],
                    on=["product", "symbol"],
                    how="left",
                    validate="one_to_one",
                )
                net = (rows.expected_return.abs() - 0.003).clip(lower=0.0)
                scores = net / (5 * rows.volatility.pow(2))
                total = float(scores.sum())
                if total > 0:
                    amount = (2 * scores / total).clip(upper=0.25)
                    for product, value in zip(
                        rows["product"], np.sign(rows.expected_return) * amount, strict=True
                    ):
                        current[product] = float(value)
                rows["decision_day"] = day
                forecasts.append(rows)
        result.loc[day] = current
    if not np.isfinite(result.to_numpy()).all() or (result.abs().sum(axis=1) > 2 + 1e-10).any():
        raise ValueError("invalid forecast target budget")
    if pool == "exAG" and result.AG.ne(0).any():
        raise ValueError("excluded product in independent forecast targets")
    prediction_frame = pd.concat(forecasts, ignore_index=True) if forecasts else pd.DataFrame()
    return result, audits, prediction_frame


def main(args):
    protocol_path = args.output / "MATURE_FORECAST_PROTOCOL.json"
    protocol = json.loads(protocol_path.read_text())
    core_protocol = json.loads((args.output / "CORE_PROTOCOL.json").read_text())
    prior = Path(core_protocol["prior_control"])
    control = json.loads((prior / "analysis.json").read_text())
    observations_path = args.output / "forecast_inputs/observations.csv"
    if not observations_path.exists():
        market = pd.read_csv(args.previous / "pair_inputs/specific.csv", parse_dates=["date"])
        calendar = pd.DatetimeIndex(sorted(market.date.unique()))
        selections = pd.read_csv(
            args.output / "inputs/market_return_audit.csv",
            parse_dates=["date", "selection_through"],
        )
        observations = build_observations(market, selections, calendar)
        observations_path.parent.mkdir()
        observations.to_csv(observations_path, index=False)
        write_json(
            observations_path.with_name("coverage.json"),
            {
                "selected_entries": len(observations),
                "feature_status": observations.feature_status.value_counts().to_dict(),
                "label_status": observations.label_status.value_counts().to_dict(),
                "labels_are_independent_of_account_ownership": True,
            },
        )
    observations = pd.read_csv(
        observations_path, parse_dates=["entry_day", "feature_through", "maturity_day"]
    )
    tape = read_weights(args.output / "inputs/market_returns.csv")
    inputs = {row["path"]: row["sha256"] for row in core_protocol["inputs"]}
    for path in (
        protocol_path,
        observations_path,
        prior / "analysis.json",
        args.output / "inputs/market_return_audit.csv",
        args.output / "inputs/market_returns.csv",
    ):
        inputs[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    inputs.update(reference_inputs(args.previous))
    root = Path(__file__).resolve().parents[1]
    sources = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(
            list((root / "afuture").rglob("*.py")) + list((root / "tools").glob("*.py"))
        )
    }

    def select(history):
        return None if history else {"id": "MATURE1", "rule": protocol}

    def execute(spec, attempt):
        strategy = attempt / "strategy"
        for pool in ("full", "exAG"):
            targets, audit, predictions = forecast_weights(observations, tape.index, pool=pool)
            dates = read_weights(prior / f"strategy/{pool}/R1_weights.csv").index
            folder = strategy / pool
            folder.mkdir(parents=True)
            targets.loc[dates].to_csv(folder / f"{spec['id']}_weights.csv", index_label="date")
            write_json(folder / "model_audit.json", audit)
            predictions.to_csv(folder / "predictions.csv", index=False)

        def account_type(market, config, *, completed_concentrations):
            del completed_concentrations
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
        args.output / "mature_chain",
        select,
        execute,
        checked,
        context={"inputs": inputs, "sources": sources, "protocol": protocol},
    )
    print(json.dumps({"status": state["status"], "experiments": len(state["history"])}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("output", "previous", "units"):
        parser.add_argument("--" + name, required=True, type=Path)
    main(parser.parse_args())
