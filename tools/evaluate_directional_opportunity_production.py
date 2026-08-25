"""Fixed Production-mechanics evaluation for opportunity-driven directional V2."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def evaluate(specific_raw: pd.DataFrame, continuous_raw: pd.DataFrame) -> dict:
    import evaluate_directional_production_mechanics as mechanics
    import evaluate_opportunity_aligned_target as target

    weights = target.generate_execution_signal_weights(continuous_raw)
    historical = target.evaluate(specific_raw, continuous_raw)
    report = mechanics.evaluate_with_weights(
        specific_raw,
        weights,
        float_report=historical,
    )
    report["role"] = "production-mechanics proxy acceptance for opportunity-driven directional V2"
    report["selection_frozen"] = True
    report["parameter_search"] = False
    report["strategy_version"] = "opportunity_directional_v2"
    return report


def _jsonable(report: dict) -> dict:
    return {key: value for key, value in report.items() if not key.startswith("_")}


def main() -> None:
    runtime = Path("runtime")
    specific_path = runtime / "return_target_specific_contracts.csv"
    continuous_path = runtime / "broad_daily_universe.csv"
    missing = [str(path) for path in (specific_path, continuous_path) if not path.exists()]
    if missing:
        raise SystemExit(f"opportunity production inputs missing: {missing}")
    report = evaluate(pd.read_csv(specific_path), pd.read_csv(continuous_path))
    report["_base_daily"].to_csv(runtime / "opportunity_production_base_daily.csv")
    report["_stress_daily"].to_csv(runtime / "opportunity_production_stress_daily.csv")
    report["_base_events"].to_csv(runtime / "opportunity_production_base_events.csv", index=False)
    report["_stress_events"].to_csv(
        runtime / "opportunity_production_stress_events.csv", index=False
    )
    output = runtime / "opportunity_production_mechanics_report.json"
    output.write_text(
        json.dumps(_jsonable(report), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(_jsonable(report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
