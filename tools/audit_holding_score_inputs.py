"""Audit every frozen template's specific-contract holding-price requirements.

Usage: python tools/audit_holding_score_inputs.py FROZEN_INPUTS REPLAY_MARKET_DIR OUTPUT.json
This only reports missing evidence; it never synthesizes prices or changes frozen inputs.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from afuture.directional_acceptance import DirectionalProductionAcceptance
from afuture.execution_aligned_policy import (
    _EXECUTION_TEMPLATE_IDS,
    _EXECUTION_TEMPLATES,
    _template_weight_path,
)


def audit(inputs: Path, market: Path) -> dict:
    continuous = pd.read_csv(inputs / "broad_daily_universe.csv")
    extension = pd.read_csv(market / "continuous_provider_overlap_and_extension.csv")
    continuous = pd.concat([continuous, extension.loc[extension.date > "2026-08-20"]])
    continuous["date"] = pd.to_datetime(continuous.date)
    if continuous.duplicated(["date", "product"]).any():
        raise ValueError("duplicate continuous date/product")
    continuous_by_day_product = continuous.set_index(["date", "product"])
    close = continuous.pivot(index="date", columns="product", values="close").sort_index()
    returns = close.pct_change(fill_method=None).mask(lambda values: values.abs() > 0.20)
    need: dict[tuple[pd.Timestamp, str], dict[str, list[str]]] = defaultdict(
        lambda: {"new": [], "carry": []}
    )
    for template_id, template in zip(_EXECUTION_TEMPLATE_IDS, _EXECUTION_TEMPLATES, strict=True):
        weights = _template_weight_path(returns, template).abs().gt(1e-15)
        prior = weights.shift(1, fill_value=False)
        for (day, product), active in weights.stack().items():
            if active:
                need[day, product]["new"].append(template_id)
            if prior.at[day, product]:
                need[day, product]["carry"].append(template_id)
    root_gaps = set(close.isna().stack().loc[lambda values: values].index)
    for day, product in root_gaps:
        need[day, product]["new"].append("scenario:all_LOO_input")

    specific = pd.read_csv(inputs / "return_target_specific_contracts.csv")
    added = pd.read_csv(market / "specific_contract_extension.csv")
    specific = pd.concat([specific, added.loc[added.date > "2026-08-20"]])
    selector = DirectionalProductionAcceptance()
    context = selector.prepare_contracts(specific)
    missing = []
    ineligible: list[dict] = []
    fields = ("open", "close", "settle", "volume", "hold")
    for (day, product), roles in sorted(need.items()):
        if not roles["new"] and not roles["carry"]:
            continue
        c = (
            continuous_by_day_product.loc[(day, product)]
            if (day, product) in continuous_by_day_product.index else None
        )
        if c is None or not np.isfinite(c[["open", "close"]].to_numpy(float)).all():
            missing.append(dict(date=str(day.date()), product=product, symbol=None,
                                fields=["continuous.open", "continuous.close"], roles=roles,
                                source_status="unavailable"))
        prior_index = int(context.available_activity_days.searchsorted(day, side="left")) - 1
        if prior_index < 0:
            continue
        prev = context.available_activity_days[prior_index]
        snapshot = context.activity_by_day[prev]
        eligible = snapshot.loc[
            (snapshot["product"] == product)
            & ((snapshot["delivery"] - day).dt.days >= selector.config.min_days_to_delivery)
            & (snapshot["volume"] >= selector.config.min_volume)
            & (snapshot["hold"] >= selector.config.min_open_interest)
        ]
        selected = selector._select_contracts_from_snapshot(eligible, day).get(product)
        # An eligible incumbent can remain selected until a different month beats it
        # in both OI and volume. Include the carry candidates, even when target is zero.
        symbols = set(eligible.symbol) if roles["carry"] else set()
        if roles["new"] and selected:
            symbols.add(selected)
        if not symbols:
            record = dict(date=str(day.date()), product=product, symbol=None,
                          roles=roles, previous_activity_day=str(prev.date()))
            if snapshot["product"].eq(product).any():
                ineligible.append({**record, "reason": "observed_below_eligibility_threshold"})
            else:
                missing.append({**record, "fields": ["previous_activity"],
                                "source_status": "unavailable"})
        for symbol in sorted(symbols):
            found = context.by_day_symbol.get((day, symbol))
            absent = list(fields) if found is None else [
                field for field in fields
                if field not in found or not np.isfinite(float(found[field]))
            ]
            if absent:
                missing.append(dict(date=str(day.date()), product=product, symbol=symbol,
                                    fields=absent, roles=roles,
                                    previous_activity_day=str(prev.date()),
                                    source_status="unavailable"))
    return {"templates": len(_EXECUTION_TEMPLATE_IDS), "days": len(close),
            "required_date_product_cells": len(need), "missing": missing,
            "ineligible": ineligible}


if __name__ == "__main__":
    result = audit(Path(sys.argv[1]), Path(sys.argv[2]))
    Path(sys.argv[3]).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"templates": result["templates"], "days": result["days"],
                      "missing": len(result["missing"]),
                      "ineligible": len(result["ineligible"])}, ensure_ascii=False))
