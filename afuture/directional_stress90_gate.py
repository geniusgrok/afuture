"""Frozen promotion gate for the validated Stress90 candidate."""
from __future__ import annotations

from collections.abc import Mapping

BASE_MIN_ANNUALIZED = 1.0
STRESS_MIN_ANNUALIZED = 0.85
HARD_DRAWDOWN = 0.30
MAX_REALIZED_GROSS = 2.0
MAX_CONTRACT_LOTS = 35
BASELINE_NET_ALPHA_PER_TURNOVER_BPS = 30.990722

REQUIRED_WINDOWS = (
    ("base", "full_recent"),
    ("stress", "train"),
    ("stress", "validation"),
    ("stress", "oos"),
    ("stress", "full_recent"),
)


def evaluate_stress90_gate(
    results: Mapping[tuple[str, str], Mapping],
    *,
    max_contract_lots: int,
) -> dict[str, object]:
    """Apply the declared gate without result-dependent discretion."""
    missing = [key for key in REQUIRED_WINDOWS if key not in results]
    if missing:
        raise ValueError(f"promotion results missing: {missing}")

    reasons: list[str] = []
    if int(max_contract_lots) != MAX_CONTRACT_LOTS:
        reasons.append("max_contract_lots_not_35")

    base = results[("base", "full_recent")]["stats"]
    stress_full = results[("stress", "full_recent")]["stats"]
    if float(base["annualized_return"]) < BASE_MIN_ANNUALIZED:
        reasons.append("base_full_recent_below_100pct")
    if float(stress_full["annualized_return"]) < STRESS_MIN_ANNUALIZED:
        reasons.append("stress_full_recent_below_85pct")

    for scenario, window in REQUIRED_WINDOWS:
        stats = results[(scenario, window)]["stats"]
        label = f"{scenario}_{window}"
        if float(stats["max_drawdown"]) < -HARD_DRAWDOWN - 1e-12:
            reasons.append(f"{label}_dd_exceeds_30pct")
        if bool(stats.get("halted", False)):
            reasons.append(f"{label}_halted")
        if (
            float(stats.get("max_realized_gross_notional_ratio", 0.0))
            > MAX_REALIZED_GROSS + 1e-10
        ):
            reasons.append(f"{label}_gross_exceeds_2x")
        if int(stats.get("margin_reject_days", 0)) != 0:
            reasons.append(f"{label}_margin_rejects_nonzero")

    for window in ("train", "validation", "oos"):
        if float(results[("stress", window)]["stats"]["annualized_return"]) <= 0.0:
            reasons.append(f"stress_{window}_not_positive")

    efficiency = float(
        results[("stress", "full_recent")]["economics"][
            "net_alpha_per_turnover_bps"
        ]
    )
    if efficiency <= BASELINE_NET_ALPHA_PER_TURNOVER_BPS:
        reasons.append("stress_net_alpha_per_turnover_not_above_30_990722bps")

    return {"passed": not reasons, "reasons": reasons}
