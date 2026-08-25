"""Explicit runtime policy identity activation and switch guard."""

from __future__ import annotations

import re
from dataclasses import replace

from .directional_stress90_policy import STRESS90_POLICY
from .models import RuntimeMode
from .state import RuntimeState

STRESS90_ACTIVATION_CONFIRMATION = "I_CONFIRM_STRESS90_POLICY_ACTIVATION"
POLICY_IDENTITY_STATE_KEY = "directional_policy_identity"
_SHA256 = re.compile(r"[0-9a-f]{64}")


def activate_stress90_policy(
    state: RuntimeState,
    *,
    broker_flat: bool,
    local_flat: bool,
    no_active_orders: bool,
    reconciled: bool,
    bootstrap_seed_digest: str,
    operator_reason: str,
    strong_confirmation: str,
) -> RuntimeState:
    """Return a still-HALTED state carrying an explicit Stress-90 identity marker."""

    if not isinstance(state, RuntimeState):
        raise RuntimeError("Stress-90 activation state is invalid")
    if (
        state.runtime_mode != RuntimeMode.HALTED.value
        or not state.kill_switch
        or any(
            value is not True for value in (broker_flat, local_flat, no_active_orders, reconciled)
        )
        or not state.reconciled
    ):
        raise RuntimeError("Stress-90 activation lifecycle gates failed")
    if strong_confirmation != STRESS90_ACTIVATION_CONFIRMATION:
        raise RuntimeError("Stress-90 activation confirmation is invalid")
    if not isinstance(operator_reason, str) or not operator_reason.strip():
        raise RuntimeError("Stress-90 activation operator reason is required")
    if (
        not isinstance(bootstrap_seed_digest, str)
        or _SHA256.fullmatch(bootstrap_seed_digest) is None
    ):
        raise RuntimeError("Stress-90 activation bootstrap identity is invalid")

    marker = {
        "policy_id": STRESS90_POLICY.policy_id,
        "policy_definition_digest": STRESS90_POLICY.policy_definition_digest,
        "products_manifest_digest": STRESS90_POLICY.products_manifest_digest,
        "bootstrap_seed_digest": bootstrap_seed_digest,
        "operator_reason": operator_reason.strip(),
    }
    strategy_states = {key: dict(value) for key, value in state.strategy_states.items()}
    strategy_states[POLICY_IDENTITY_STATE_KEY] = marker
    return replace(state, strategy_states=strategy_states)


def require_directional_policy_identity(
    state: RuntimeState,
    *,
    policy_id: str,
    policy_definition_digest: str,
    products_manifest_digest: str = "",
    bootstrap_seed_digest: str | None = None,
) -> None:
    """Fail closed on switches; only legacy execution-aligned state may lack a marker."""

    marker = state.strategy_states.get(POLICY_IDENTITY_STATE_KEY)
    if marker is None:
        if policy_id == "execution_aligned":
            return
        raise RuntimeError("Stress-90 requires explicit activation from HALTED state")
    if not isinstance(marker, dict):
        raise RuntimeError("directional policy identity marker is invalid")
    marked_policy = marker.get("policy_id")
    marked_digest = marker.get("policy_definition_digest")
    if marked_policy != policy_id or (
        policy_definition_digest and marked_digest != policy_definition_digest
    ):
        raise RuntimeError(
            "directional policy identity mismatch; explicit HALTED migration is required"
        )
    if (
        products_manifest_digest
        and marker.get("products_manifest_digest") != products_manifest_digest
    ):
        raise RuntimeError(
            "directional product manifest identity mismatch; explicit activation is required"
        )
    if (
        bootstrap_seed_digest is not None
        and marker.get("bootstrap_seed_digest") != bootstrap_seed_digest
    ):
        raise RuntimeError(
            "directional policy bootstrap identity mismatch; explicit activation is required"
        )
