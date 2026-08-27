"""Read-only projection of the shared Stress-90 Doctor mechanics into capacity diagnostics."""

from __future__ import annotations

from collections.abc import Mapping


def build_stress90_capacity_payload(
    report,
    *,
    account_identity_digest: str,
    ctp_trading_day: str,
) -> dict[str, object]:
    facts = getattr(report, "facts", None)
    stress = facts.get("stress90") if isinstance(facts, Mapping) else None
    if not isinstance(stress, Mapping):
        raise RuntimeError("Stress-90 capacity report requires Doctor Stress-90 facts")
    stages = stress.get("integer_target_stages")
    if not isinstance(stages, Mapping):
        raise RuntimeError("Stress-90 capacity report requires shared integer planner facts")
    costs = stress.get("live_costs")
    if not isinstance(costs, Mapping):
        raise RuntimeError("Stress-90 capacity report requires live cost evidence")
    risk_preview = stress.get("risk_manager_preview")
    if not isinstance(risk_preview, Mapping):
        raise RuntimeError("Stress-90 capacity report requires RiskManager preview")
    failures = [
        str(item.name)
        for item in getattr(report, "checks", ())
        if not bool(getattr(item, "passed", False))
        and str(getattr(item, "name", "")).startswith("stress90_")
        and str(getattr(item, "name", "")) != "stress90_runtime_permission"
    ]
    return {
        "orders_sent": 0,
        "cancels_sent": 0,
        "account_identity_digest": str(account_identity_digest),
        "ctp_trading_day": str(ctp_trading_day),
        "risk_overlay_digest": stress.get("risk_overlay_digest"),
        "configured_live_risk_scale": stress.get("configured_live_risk_scale"),
        "raw_product_weights": stress.get("raw_product_weights", {}),
        "scaled_product_weights": stress.get("scaled_product_weights", {}),
        "raw_target_gross": stress.get("raw_target_gross", 0.0),
        "scaled_target_gross": stress.get("scaled_target_gross", 0.0),
        "selected_contracts": stress.get("selected_contracts", {}),
        "contract_capacity": stress.get("contract_capacity", {}),
        "live_costs": dict(costs),
        "raw_lots": stages.get("raw_integer_lots", {}),
        "scaled_lots": stages.get("scaled_integer_lots", {}),
        "margin_fitted_lots": stages.get("margin_fitted_lots", {}),
        "drawdown_frozen_lots": stages.get("drawdown_frozen_lots", {}),
        "hhi_frozen_lots": stages.get("hhi_frozen_lots", {}),
        "final_lots": stages.get("final_frozen_lots", {}),
        "reductions": stages.get("reductions", {}),
        "openings": stages.get("openings", {}),
        "nonzero_product_count": stress.get("nonzero_product_count", 0),
        "largest_product_absolute_gross_share": stress.get(
            "largest_product_absolute_gross_share", 0.0
        ),
        "product_hhi": stress.get("product_hhi", 0.0),
        "integer_tracking_error": stress.get("integer_tracking_error", 0.0),
        "estimated_margin_ratio": stress.get("estimated_margin_ratio", 0.0),
        "estimated_available_ratio": stress.get("estimated_available_ratio", 0.0),
        "contract_cap_hits": stress.get("contract_cap_hits", []),
        "clipped_products": stress.get("clipped_products", {}),
        "risk_manager_preview": dict(risk_preview),
        "execution_chain_commissioning_only": bool(
            stress.get("execution_chain_commissioning_only", False)
        ),
        "portfolio_representation_warning": stress.get("portfolio_representation_warning", ""),
        "hard_safety_failures": failures,
        "hard_safety_passed": not failures and bool(risk_preview.get("allowed", False)),
    }
