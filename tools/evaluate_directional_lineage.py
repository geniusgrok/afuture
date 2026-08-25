"""Exact audit lineage for the frozen execution-aligned directional Production path.

Inputs are the same continuous daily evidence used by the frozen policy plus the product
trade ledgers emitted by ``evaluate_directional_production_mechanics.py``. Template
contributions are exact at target-weight level; realized position, turnover, and cost stay
product-level truth and are never fictionally allocated back to individual templates.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from afuture.directional_lineage import (
    build_exact_lineage_attribution,
    build_weight_lineage,
)
from afuture.execution_aligned_policy import ExecutionAlignedAggressivePolicy
from afuture.execution_aligned_runtime import FROZEN_PRODUCTS


def _load_continuous(path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    raw = pd.read_csv(path)
    raw["date"] = pd.to_datetime(raw["date"], errors="coerce")
    raw["product"] = raw["product"].astype(str).str.upper()
    for column in ("open", "close"):
        raw[column] = pd.to_numeric(raw[column], errors="coerce")
    raw = raw.dropna(subset=["date", "product", "open", "close"])
    raw = raw[(raw["open"] > 0) & (raw["close"] > 0)]
    raw = raw.drop_duplicates(["date", "product"], keep="last")
    products = tuple(sorted(str(item).upper() for item in FROZEN_PRODUCTS))
    open_prices = (
        raw.pivot(index="date", columns="product", values="open")
        .sort_index()
        .reindex(columns=products)
    )
    close = (
        raw.pivot(index="date", columns="product", values="close")
        .sort_index()
        .reindex(index=open_prices.index, columns=products)
    )
    missing = [product for product in products if close[product].notna().sum() < 140]
    if missing:
        raise RuntimeError(f"directional lineage history missing products: {missing}")
    return open_prices, close


def _enrich_with_governor(
    product_execution: pd.DataFrame,
    daily_path: Path,
) -> pd.DataFrame:
    daily = pd.read_csv(daily_path)
    date_column = "date" if "date" in daily.columns else str(daily.columns[0])
    daily[date_column] = pd.to_datetime(daily[date_column], errors="coerce").dt.normalize()
    if "risk_scale" not in daily.columns:
        raise RuntimeError(f"risk_scale is missing from {daily_path}")
    scales = daily[[date_column, "risk_scale"]].rename(columns={date_column: "date"})
    result = product_execution.merge(scales, on="date", how="left")
    result["governor_target_weight"] = result["target_weight"].astype(float) * result[
        "risk_scale"
    ].astype(float)
    return result


def _endpoint(
    *,
    name: str,
    meta: pd.DataFrame,
    template_product: pd.DataFrame,
    events_path: Path,
    daily_path: Path,
    runtime: Path,
) -> dict:
    events = pd.read_csv(events_path)
    attribution = build_exact_lineage_attribution(
        meta=meta,
        template_product=template_product,
        events=events,
    )
    product_execution = _enrich_with_governor(attribution["product_execution"], daily_path)
    output = runtime / f"directional_lineage_{name}_product_execution.csv"
    product_execution.to_csv(output, index=False)

    trades = events[events["kind"] == "trade"].copy()
    expected_turnover = float(trades["turnover_notional"].sum())
    expected_cost = float(trades["transaction_cost"].sum())
    actual_turnover = float(product_execution["turnover_notional"].sum())
    actual_cost = float(product_execution["transaction_cost"].sum())
    if abs(actual_turnover - expected_turnover) > 1e-6:
        raise AssertionError(f"{name} lineage turnover does not reconcile")
    if abs(actual_cost - expected_cost) > 1e-6:
        raise AssertionError(f"{name} lineage transaction cost does not reconcile")
    return {
        "product_execution_rows": int(len(product_execution)),
        "turnover_notional": actual_turnover,
        "transaction_cost": actual_cost,
        "cost_reconciles_to_trade_ledger": True,
        "turnover_reconciles_to_trade_ledger": True,
    }


def main() -> None:
    runtime = Path("runtime")
    continuous_path = runtime / "broad_daily_universe.csv"
    required = [
        continuous_path,
        runtime / "directional_production_base_daily.csv",
        runtime / "directional_production_stress_daily.csv",
        runtime / "directional_production_base_events.csv",
        runtime / "directional_production_stress_events.csv",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise SystemExit(f"directional lineage inputs missing: {missing}")

    open_prices, close = _load_continuous(continuous_path)
    policy = ExecutionAlignedAggressivePolicy(products=tuple(close.columns))
    weights, meta, template_product = build_weight_lineage(policy, open_prices, close)
    weights.to_csv(runtime / "directional_lineage_target_weights.csv")
    meta.to_csv(runtime / "directional_lineage_meta.csv", index=False)
    template_product.to_csv(runtime / "directional_lineage_template_product.csv", index=False)

    summary = {
        "role": "behavior-neutral exact template-to-product-to-execution attribution",
        "selection_frozen": True,
        "production_policy_modified": False,
        "template_cost_allocation": False,
        "template_product_rows": int(len(template_product)),
        "meta_rows": int(len(meta)),
        "base": _endpoint(
            name="base",
            meta=meta,
            template_product=template_product,
            events_path=runtime / "directional_production_base_events.csv",
            daily_path=runtime / "directional_production_base_daily.csv",
            runtime=runtime,
        ),
        "stress": _endpoint(
            name="stress",
            meta=meta,
            template_product=template_product,
            events_path=runtime / "directional_production_stress_events.csv",
            daily_path=runtime / "directional_production_stress_daily.csv",
            runtime=runtime,
        ),
        "lineage": [
            "selected template",
            "template product contribution",
            "raw product target weight",
            "completed-return governor target weight",
            "realized product position",
            "exact product turnover",
            "exact product transaction cost",
        ],
    }
    output = runtime / "directional_lineage_report.json"
    output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
