"""Offline causal-feature / future-label diagnostics for directional entry and exit events."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from afuture.directional_entry_diagnostics import (
    label_directional_entry_exit_events,
    summarize_entry_exit_quality,
)


DEFAULT_SLICES = {
    "train": ("2024-08-21", "2025-08-20"),
    "validation": ("2025-08-21", "2026-02-20"),
    "oos": ("2026-02-21", "2026-08-20"),
    "full_recent": ("2024-08-21", "2026-08-20"),
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Label production entry/exit events with completed-history features and "
            "explicitly future-only 1/3/5/10-session diagnostic outcomes."
        )
    )
    parser.add_argument("--events", required=True, type=Path)
    parser.add_argument("--specific-contracts", required=True, type=Path)
    parser.add_argument("--product-history", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--labeled-output", type=Path)
    parser.add_argument("--stress-cost-bps", type=float, default=15.0)
    return parser.parse_args()


def _product_breakdown(labeled: pd.DataFrame) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for product, frame in labeled.groupby("product", sort=True):
        result[str(product)] = summarize_entry_exit_quality(frame)
    return result


def main() -> None:
    args = _parse_args()
    events = pd.read_csv(args.events)
    specific = pd.read_csv(args.specific_contracts)
    product_history = pd.read_csv(args.product_history)
    labeled = label_directional_entry_exit_events(
        events=events,
        specific_contracts=specific,
        product_history=product_history,
        horizons=(1, 3, 5, 10),
        stress_cost_bps=float(args.stress_cost_bps),
    )
    slices: dict[str, dict] = {}
    dates = pd.to_datetime(labeled["date"], errors="coerce") if not labeled.empty else pd.Series(dtype="datetime64[ns]")
    for name, (start, end) in DEFAULT_SLICES.items():
        mask = (dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))
        slices[name] = summarize_entry_exit_quality(labeled.loc[mask].copy())
    report = {
        "role": "offline entry/exit quality diagnostics; future labels are not production inputs",
        "stress_cost_bps_one_way": float(args.stress_cost_bps),
        "round_trip_cost": 2.0 * float(args.stress_cost_bps) / 10000.0,
        "causality": {
            "feature_columns_use_completed_history_only": True,
            "label_columns_may_use_future_sessions": True,
            "future_labels_are_production_inputs": False,
            "meta_cause_reliably_reconstructable_from_event_artifact": False,
        },
        "full": summarize_entry_exit_quality(labeled),
        "slices": slices,
        "products": _product_breakdown(labeled),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    labeled_path = args.labeled_output or args.output.with_name(
        f"{args.output.stem}_labeled.csv"
    )
    labeled_path.parent.mkdir(parents=True, exist_ok=True)
    labeled.to_csv(labeled_path, index=False)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
