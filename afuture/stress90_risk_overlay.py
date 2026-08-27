"""Canonical Stress-90 production risk overlay identity and monotone scaling."""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from math import isfinite

_KIND = "afuture.stress90.risk-overlay"
_SCHEMA_VERSION = 1


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _finite_scale(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("directional.live_risk_scale must be finite and in (0, 1]")
    scale = float(value)
    if not isfinite(scale) or not 0.0 < scale <= 1.0:
        raise ValueError("directional.live_risk_scale must be finite and in (0, 1]")
    return scale


def scale_stress90_product_weights(
    weights: Mapping[str, float], live_risk_scale: float
) -> dict[str, float]:
    """Scale production targets before integer sizing without creating or flipping risk."""

    scale = _finite_scale(live_risk_scale)
    result: dict[str, float] = {}
    for raw_product, raw_value in weights.items():
        product = str(raw_product).upper()
        value = float(raw_value)
        if not product or not isfinite(value):
            raise ValueError("Stress-90 product weights must be finite")
        scaled = value * scale
        if abs(scaled) > abs(value) + 1e-15:
            raise RuntimeError("Stress-90 live scale increased absolute target risk")
        if value and scaled and (scaled > 0.0) != (value > 0.0):
            raise RuntimeError("Stress-90 live scale changed target direction")
        result[product] = scaled
    return result


def stress90_risk_overlay_payload(directional, risk) -> dict[str, object]:
    """Return the complete canonical configuration envelope that can change live risk."""

    scale = _finite_scale(getattr(directional, "live_risk_scale", 1.0))
    directional_fields = (
        "max_gross_leverage",
        "max_contract_volume",
        "min_days_to_expiry",
        "min_volume",
        "min_open_interest",
        "rebalance_window",
        "signal_max_age_hours",
        "account_exclusive",
        "account_continuity_mode",
    )
    risk_fields = tuple(getattr(risk, "__dataclass_fields__", {}).keys())
    if not risk_fields:
        risk_fields = (
            "max_margin_ratio",
            "min_available_ratio",
            "max_daily_loss_ratio",
            "max_total_drawdown_ratio",
            "max_contract_volume",
            "margin_estimate_buffer",
            "max_orders_per_minute",
            "min_depth_multiple",
            "max_bid_ask_ticks",
            "limit_distance_ticks",
            "max_quote_age_seconds",
            "max_leg_skew_seconds",
            "expiry_blackout_days",
            "open_cooldown_minutes",
            "close_blackout_minutes",
        )
    directional_payload = {name: getattr(directional, name) for name in directional_fields}
    directional_payload["live_risk_scale"] = scale
    risk_payload = {name: getattr(risk, name) for name in risk_fields}
    payload = {
        "kind": _KIND,
        "schema_version": _SCHEMA_VERSION,
        "directional": directional_payload,
        "risk": risk_payload,
    }
    # Canonical encoding is also the final type/finite-value guard.
    try:
        _canonical(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("Stress-90 risk overlay is not canonical finite JSON") from exc
    return payload


def stress90_risk_overlay_digest(directional, risk) -> str:
    return sha256(_canonical(stress90_risk_overlay_payload(directional, risk))).hexdigest()
