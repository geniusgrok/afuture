"""Five-window quick Production gate for confirmed direction changes plus new-product freeze.

Fixed composition:
1. completed D->D+1 60m Price x OI confirmation on the frozen 9-product set;
2. entries, same-sign adds and reversals into a new side require confirmation, while an
   unconfirmed reversal exits to flat;
3. archived 20-session / 3-session / 15bp cost eligibility;
4. tracking-first / turnover-second survivor reallocation at the OI path's original gross;
5. unchanged risk trigger with freeze-new-product-only soft response.

No parameter is fit in this gate and all hard Production constraints are unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pandas as pd

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import evaluate_directional_60m_oi_confirmation as oi_gate
import evaluate_directional_60m_oi_freeze as checkpoint
import evaluate_directional_oi_freeze_cost_combo as suppress
import evaluate_directional_production_mechanics as mechanics
from afuture.directional_acceptance import ProductionMechanicsConfig
from afuture.directional_60m_oi_reversal_confirmation import (
    apply_oi_confirmation_to_direction_changes,
)
from afuture.directional_cost_aware_no_trade import apply_cost_aware_no_trade
from afuture.directional_freeze_new_product_only import (
    FreezeNewProductOnlyDirectionalProductionAcceptance,
)
from afuture.directional_turnover_aware_survivor_reallocation import (
    reallocate_survivors_lexicographically,
)

FREEZE_PRODUCT_STRESS_REFERENCE = 0.76864283
BASELINE_NET_ALPHA_PER_TURNOVER_BPS = 12.2556


def build_candidate_weights(
    *,
    base_weights: pd.DataFrame,
    bars_60m: pd.DataFrame,
    close_prices: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    flow = oi_gate.build_daily_price_oi_flow(bars_60m)
    lagged = oi_gate.lag_flow_to_target_days(
        flow,
        target_days=base_weights.index,
        products=base_weights.columns,
    )
    confirmed = apply_oi_confirmation_to_direction_changes(
        raw_weights=base_weights,
        confirming_flow=lagged,
        supported_products=oi_gate.SUPPORTED_PRODUCTS,
    )
    approved = apply_cost_aware_no_trade(
        weights=confirmed,
        close_prices=close_prices,
    )
    candidate = reallocate_survivors_lexicographically(
        original_weights=confirmed,
        approved_weights=approved,
    )
    return confirmed, approved, candidate


def _run_window(simulator, *, specific_raw, prepared, weights, window, cost_bps):
    start, end = mechanics.WINDOWS[window]
    result = simulator.simulate(
        specific_raw,
        weights.loc[pd.Timestamp(start):pd.Timestamp(end)].copy(),
        cost_bps=cost_bps,
        prepared=prepared,
    )
    return mechanics._result_stats(result), result.daily.copy(), result.events.copy()


def _gate(base_full: dict, stress: dict, economics: dict) -> dict:
    reasons: list[str] = []
    if float(base_full["annualized_return"]) < 0.80:
        reasons.append("base_full_recent_below_80pct")
    for key, item in (("base_full_recent", base_full), *stress.items()):
        if float(item["max_drawdown"]) < -0.30 - 1e-12:
            reasons.append(f"{key}_dd_exceeds_30pct")
        if bool(item.get("halted", False)):
            reasons.append(f"{key}_halted")
        if float(item.get("max_realized_gross_notional_ratio", 0.0)) > 2.0 + 1e-10:
            reasons.append(f"{key}_gross_exceeds_2x")
        if int(item.get("margin_reject_days", 0)) != 0:
            reasons.append(f"{key}_margin_rejects_nonzero")
    if float(stress["full_recent"]["annualized_return"]) < 0.80:
        reasons.append("stress_full_recent_below_80pct")
    for window in ("train", "validation", "oos"):
        if float(stress[window]["annualized_return"]) <= 0.0:
            reasons.append(f"stress_{window}_not_positive")
    if float(economics["net_alpha_per_turnover_bps"]) <= BASELINE_NET_ALPHA_PER_TURNOVER_BPS:
        reasons.append("stress_net_alpha_per_turnover_not_above_baseline")
    return {"passed": not reasons, "reasons": reasons}


def _turnover(weights: pd.DataFrame) -> float:
    values = weights.diff().abs().sum(axis=1)
    if len(values):
        values.iloc[0] = float(weights.iloc[0].abs().sum())
    return float(values.sum())


def evaluate(*, specific_raw, continuous_raw, base_weights, bars_60m) -> dict:
    close = suppress.continuous_close_panel(continuous_raw, list(base_weights.columns))
    confirmed, approved, candidate_weights = build_candidate_weights(
        base_weights=base_weights,
        bars_60m=bars_60m,
        close_prices=close,
    )
    base_sim = FreezeNewProductOnlyDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=mechanics.INITIAL_CAPITAL,
            margin_rate_proxy=mechanics.BASE_MARGIN_PROXY,
        )
    )
    stress_sim = FreezeNewProductOnlyDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=mechanics.INITIAL_CAPITAL,
            margin_rate_proxy=mechanics.STRESS_MARGIN_PROXY,
        )
    )
    prepared = base_sim.prepare_contracts(specific_raw)
    base_full, _bd, _be = _run_window(
        base_sim,
        specific_raw=specific_raw,
        prepared=prepared,
        weights=candidate_weights,
        window="full_recent",
        cost_bps=mechanics.BASE_COST_BPS,
    )
    stress: dict[str, dict] = {}
    full_daily = pd.DataFrame(); full_events = pd.DataFrame()
    for window in ("train", "validation", "oos", "full_recent"):
        stats, daily, events = _run_window(
            stress_sim,
            specific_raw=specific_raw,
            prepared=prepared,
            weights=candidate_weights,
            window=window,
            cost_bps=mechanics.STRESS_COST_BPS,
        )
        stress[window] = stats
        if window == "full_recent":
            full_daily, full_events = daily, events
    economics = checkpoint._economics(
        full_daily, full_events, cost_bps=mechanics.STRESS_COST_BPS
    )
    gate = _gate(base_full, stress, economics)
    return {
        "role": "five-window quick Production gate for OI-confirmed direction changes plus freeze-new-product",
        "parameter_search": False,
        "production_wiring": False,
        "previous_champion_stress_full_recent": FREEZE_PRODUCT_STRESS_REFERENCE,
        "base_full_recent": base_full,
        "stress": stress,
        "economics": economics,
        "weight_path": {
            "confirmed_turnover_full_path": _turnover(confirmed),
            "approved_turnover_full_path": _turnover(approved),
            "candidate_turnover_full_path": _turnover(candidate_weights),
            "candidate_average_gross_full_recent": float(
                candidate_weights.loc["2024-08-21":"2026-08-20"]
                .abs().sum(axis=1).mean()
            ),
        },
        "delta_vs_76_freeze_product_stress_annualized": (
            float(stress["full_recent"]["annualized_return"])
            - FREEZE_PRODUCT_STRESS_REFERENCE
        ),
        "promotion_gate": gate,
        "constraints": {
            "target_and_realized_gross_cap": 2.0,
            "hard_margin_ratio": 0.35,
            "min_available_ratio": 0.25,
            "daily_loss_ratio": 0.05,
            "total_drawdown_ratio": 0.30,
            "max_contract_volume": 35,
            "risk_trigger_unchanged": True,
            "reduction_first_unchanged": True,
        },
    }


def main() -> None:
    runtime = Path("runtime")
    paths = {
        "specific": runtime / "return_target_specific_contracts.csv",
        "continuous": runtime / "broad_daily_universe.csv",
        "weights": runtime / "execution_aligned_weights.csv",
        "prior": runtime / "prior_two_year_broad_60m.csv",
        "recent": runtime / "two_year_broad_60m.csv",
    }
    missing = [str(path) for path in paths.values() if not path.exists()]
    if missing:
        raise SystemExit(f"OI reversal quick inputs missing: {missing}")
    report = evaluate(
        specific_raw=pd.read_csv(paths["specific"]),
        continuous_raw=pd.read_csv(paths["continuous"]),
        base_weights=oi_gate.load_frozen_weights(paths["weights"]),
        bars_60m=oi_gate.load_60m([paths["prior"], paths["recent"]]),
    )
    output = runtime / "stress80_oi_reversal_freeze_product_quick_report.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
