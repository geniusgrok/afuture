"""Explicit runtime policy identity activation and switch guard."""

from __future__ import annotations

import re
from dataclasses import replace

from .directional_stress90_policy import STRESS90_POLICY
from .models import RuntimeMode
from .state import RuntimeState

STRESS90_ACTIVATION_CONFIRMATION = "I_CONFIRM_STRESS90_POLICY_ACTIVATION"
DIRECTIONAL_POLICY_MIGRATION_CONFIRMATION = "I_CONFIRM_DIRECTIONAL_POLICY_MIGRATION"
POLICY_IDENTITY_STATE_KEY = "directional_policy_identity"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_STRESS90_MARKER_FIELDS = {
    "policy_id",
    "policy_definition_digest",
    "products_manifest_digest",
    "bootstrap_seed_digest",
    "account_identity_digest",
    "operator_reason",
    "risk_overlay_digest",
}
_EXECUTION_ALIGNED_MARKER_FIELDS = {
    "policy_id",
    "policy_definition_digest",
    "products_manifest_digest",
    "account_identity_digest",
    "operator_reason",
}
_MIGRATED_EXECUTION_ALIGNED_MARKER_FIELDS = {
    *_EXECUTION_ALIGNED_MARKER_FIELDS,
    "migrated_from_policy_id",
    "migrated_from_policy_definition_digest",
}


def activate_stress90_policy(
    state: RuntimeState,
    *,
    broker_flat: bool,
    local_flat: bool,
    no_active_orders: bool,
    reconciled: bool,
    bootstrap_seed_digest: str,
    account_identity_digest: str,
    risk_overlay_digest: str,
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
    if _SHA256.fullmatch(account_identity_digest or "") is None:
        raise RuntimeError("Stress-90 activation account identity is invalid")
    if _SHA256.fullmatch(risk_overlay_digest or "") is None:
        raise RuntimeError("Stress-90 activation risk overlay identity is invalid")

    existing_marker = state.strategy_states.get(POLICY_IDENTITY_STATE_KEY)
    if existing_marker is not None:
        if not isinstance(existing_marker, dict):
            raise RuntimeError("directional policy identity marker is invalid")
        # Activation may be the explicit switch from execution-aligned, but it
        # must never relabel an already-identified Stress-90 state whose
        # immutable definition, bootstrap, product universe, or account differs.
        existing_policy_id = existing_marker.get("policy_id")
        if existing_policy_id == STRESS90_POLICY.policy_id:
            if set(existing_marker) != _STRESS90_MARKER_FIELDS:
                raise RuntimeError("Stress-90 policy identity marker is invalid")
            require_directional_policy_identity(
                state,
                policy_id=STRESS90_POLICY.policy_id,
                policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
                products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
                bootstrap_seed_digest=bootstrap_seed_digest,
                account_identity_digest=account_identity_digest,
                risk_overlay_digest=risk_overlay_digest,
            )
        elif existing_policy_id == "execution_aligned":
            if existing_marker.get("policy_definition_digest") != "":
                raise RuntimeError(
                    "execution-aligned policy definition digest mismatch; identity is invalid"
                )
            require_directional_policy_identity(
                state,
                policy_id="execution_aligned",
                policy_definition_digest="",
                products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
                account_identity_digest=account_identity_digest,
            )
            provenance_fields = {
                "migrated_from_policy_id",
                "migrated_from_policy_definition_digest",
            }
            has_provenance = bool(provenance_fields.intersection(existing_marker))
            expected_fields = (
                _MIGRATED_EXECUTION_ALIGNED_MARKER_FIELDS
                if has_provenance
                else _EXECUTION_ALIGNED_MARKER_FIELDS
            )
            if set(existing_marker) != expected_fields:
                raise RuntimeError("execution-aligned policy identity marker is invalid")
            if has_provenance:
                if (
                    existing_marker.get("migrated_from_policy_id") != STRESS90_POLICY.policy_id
                    or existing_marker.get("migrated_from_policy_definition_digest")
                    != STRESS90_POLICY.policy_definition_digest
                ):
                    raise RuntimeError("execution-aligned migration provenance identity is invalid")
        else:
            raise RuntimeError("directional policy identity marker is invalid")

    marker = {
        "policy_id": STRESS90_POLICY.policy_id,
        "policy_definition_digest": STRESS90_POLICY.policy_definition_digest,
        "products_manifest_digest": STRESS90_POLICY.products_manifest_digest,
        "bootstrap_seed_digest": bootstrap_seed_digest,
        "account_identity_digest": account_identity_digest,
        "operator_reason": operator_reason.strip(),
    }
    marker["risk_overlay_digest"] = risk_overlay_digest
    strategy_states = {key: dict(value) for key, value in state.strategy_states.items()}
    strategy_states[POLICY_IDENTITY_STATE_KEY] = marker
    return replace(
        state,
        strategy_states=strategy_states,
        directional_daily_circuit_day="",
    )


def require_directional_policy_identity(
    state: RuntimeState,
    *,
    policy_id: str,
    policy_definition_digest: str,
    products_manifest_digest: str = "",
    bootstrap_seed_digest: str | None = None,
    account_identity_digest: str | None = None,
    risk_overlay_digest: str | None = None,
) -> None:
    """Fail closed on policy switches and non-current Stress-90 identity markers."""

    marker = state.strategy_states.get(POLICY_IDENTITY_STATE_KEY)
    if marker is None:
        if policy_id == "execution_aligned":
            return
        raise RuntimeError("Stress-90 requires explicit activation from HALTED state")
    if not isinstance(marker, dict):
        raise RuntimeError("directional policy identity marker is invalid")
    if policy_id == STRESS90_POLICY.policy_id:
        if set(marker) != _STRESS90_MARKER_FIELDS:
            raise RuntimeError("Stress-90 policy identity marker is not current")
        if _SHA256.fullmatch(str(marker.get("risk_overlay_digest", ""))) is None:
            raise RuntimeError("Stress-90 risk overlay identity is invalid")
    marked_policy = marker.get("policy_id")
    marked_digest = marker.get("policy_definition_digest")
    if marked_policy != policy_id or marked_digest != policy_definition_digest:
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
    if risk_overlay_digest is not None:
        if _SHA256.fullmatch(risk_overlay_digest or "") is None:
            raise RuntimeError("directional risk overlay identity is invalid")
        if marker.get("risk_overlay_digest") != risk_overlay_digest:
            raise RuntimeError(
                "directional risk overlay identity mismatch; explicit HALTED activation/reactivation is required"
            )
    if account_identity_digest is not None:
        if _SHA256.fullmatch(account_identity_digest or "") is None:
            raise RuntimeError("directional Broker account identity is unavailable")
        if marker.get("account_identity_digest") != account_identity_digest:
            raise RuntimeError(
                "directional account identity mismatch; explicit HALTED rebase is required"
            )


def rebind_stress90_risk_overlay_identity(
    state: RuntimeState,
    *,
    risk_overlay_digest: str,
    operator_reason: str,
) -> RuntimeState:
    """Rebind only the production risk overlay after the existing HALTED lifecycle gates."""

    if (
        state.runtime_mode != RuntimeMode.HALTED.value
        or not state.kill_switch
        or not state.reconciled
    ):
        raise RuntimeError("Stress-90 risk overlay reactivation requires HALTED reconciled state")
    if _SHA256.fullmatch(risk_overlay_digest or "") is None:
        raise RuntimeError("Stress-90 risk overlay reactivation digest is invalid")
    if not isinstance(operator_reason, str) or not operator_reason.strip():
        raise RuntimeError("Stress-90 risk overlay reactivation operator reason is required")
    marker = state.strategy_states.get(POLICY_IDENTITY_STATE_KEY)
    if (
        not isinstance(marker, dict)
        or set(marker) != _STRESS90_MARKER_FIELDS
        or marker.get("policy_id") != STRESS90_POLICY.policy_id
        or marker.get("policy_definition_digest") != STRESS90_POLICY.policy_definition_digest
        or marker.get("products_manifest_digest") != STRESS90_POLICY.products_manifest_digest
        or _SHA256.fullmatch(str(marker.get("bootstrap_seed_digest", ""))) is None
        or _SHA256.fullmatch(str(marker.get("account_identity_digest", ""))) is None
        or _SHA256.fullmatch(str(marker.get("risk_overlay_digest", ""))) is None
    ):
        raise RuntimeError("Stress-90 risk overlay source identity is invalid")
    strategy_states = {key: dict(value) for key, value in state.strategy_states.items()}
    strategy_states[POLICY_IDENTITY_STATE_KEY] = {
        **marker,
        "risk_overlay_digest": risk_overlay_digest,
        "operator_reason": operator_reason.strip(),
    }
    return replace(
        state,
        strategy_states=strategy_states,
        kill_switch=True,
        kill_reason="Stress-90 risk overlay rebound; fresh Doctor permit remains required",
        runtime_mode=RuntimeMode.HALTED.value,
        reconciled=True,
        metadata_verified=False,
        directional_daily_circuit_day="",
    )


def rebind_stress90_runtime_account_identity(
    state: RuntimeState,
    *,
    account_identity_digest: str,
) -> RuntimeState:
    """Update only the account identity after the separate rebase gates passed."""

    if state.runtime_mode != RuntimeMode.HALTED.value or not state.kill_switch:
        raise RuntimeError("Stress-90 account identity rebind requires HALTED state")
    if _SHA256.fullmatch(account_identity_digest or "") is None:
        raise RuntimeError("Stress-90 account identity rebind is invalid")
    marker = state.strategy_states.get(POLICY_IDENTITY_STATE_KEY)
    if (
        not isinstance(marker, dict)
        or set(marker) != _STRESS90_MARKER_FIELDS
        or marker.get("policy_id") != STRESS90_POLICY.policy_id
        or marker.get("policy_definition_digest") != STRESS90_POLICY.policy_definition_digest
        or marker.get("products_manifest_digest") != STRESS90_POLICY.products_manifest_digest
        or _SHA256.fullmatch(str(marker.get("bootstrap_seed_digest", ""))) is None
        or _SHA256.fullmatch(str(marker.get("account_identity_digest", ""))) is None
        or _SHA256.fullmatch(str(marker.get("risk_overlay_digest", ""))) is None
    ):
        raise RuntimeError("Stress-90 account identity marker is invalid")
    strategy_states = {key: dict(value) for key, value in state.strategy_states.items()}
    strategy_states[POLICY_IDENTITY_STATE_KEY]["account_identity_digest"] = account_identity_digest
    return replace(state, strategy_states=strategy_states)


def migrate_stress90_to_execution_aligned(
    state: RuntimeState,
    *,
    broker_flat: bool,
    local_flat: bool,
    no_active_orders: bool,
    reconciled: bool,
    account_identity_digest: str,
    operator_reason: str,
    strong_confirmation: str,
) -> RuntimeState:
    """Return a still-HALTED explicit Stress-90 → execution-aligned identity switch."""

    if (
        not isinstance(state, RuntimeState)
        or state.runtime_mode != RuntimeMode.HALTED.value
        or not state.kill_switch
        or not state.reconciled
        or any(
            value is not True for value in (broker_flat, local_flat, no_active_orders, reconciled)
        )
    ):
        raise RuntimeError("directional policy migration lifecycle gates failed")
    if strong_confirmation != DIRECTIONAL_POLICY_MIGRATION_CONFIRMATION:
        raise RuntimeError("directional policy migration confirmation is invalid")
    if not isinstance(operator_reason, str) or not operator_reason.strip():
        raise RuntimeError("directional policy migration operator reason is required")
    if _SHA256.fullmatch(account_identity_digest or "") is None:
        raise RuntimeError("directional policy migration account identity is invalid")
    marker = state.strategy_states.get(POLICY_IDENTITY_STATE_KEY)
    if (
        not isinstance(marker, dict)
        or set(marker) != _STRESS90_MARKER_FIELDS
        or marker.get("policy_id") != STRESS90_POLICY.policy_id
        or marker.get("policy_definition_digest") != STRESS90_POLICY.policy_definition_digest
        or marker.get("products_manifest_digest") != STRESS90_POLICY.products_manifest_digest
        or _SHA256.fullmatch(str(marker.get("bootstrap_seed_digest", ""))) is None
        or marker.get("account_identity_digest") != account_identity_digest
        or _SHA256.fullmatch(str(marker.get("risk_overlay_digest", ""))) is None
    ):
        raise RuntimeError("directional policy migration source identity is invalid")
    strategy_states = {key: dict(value) for key, value in state.strategy_states.items()}
    strategy_states[POLICY_IDENTITY_STATE_KEY] = {
        "policy_id": "execution_aligned",
        "policy_definition_digest": "",
        "products_manifest_digest": STRESS90_POLICY.products_manifest_digest,
        "account_identity_digest": account_identity_digest,
        "operator_reason": operator_reason.strip(),
        "migrated_from_policy_id": STRESS90_POLICY.policy_id,
        "migrated_from_policy_definition_digest": (STRESS90_POLICY.policy_definition_digest),
    }
    return replace(
        state,
        strategy_states=strategy_states,
        kill_switch=True,
        kill_reason=(
            "directional policy migrated to execution_aligned; fresh reconcile remains required"
        ),
        runtime_mode=RuntimeMode.HALTED.value,
        reconciled=True,
        metadata_verified=False,
        directional_daily_circuit_day="",
    )


def reactivate_stress90_policy(
    state: RuntimeState,
    *,
    broker_flat: bool,
    local_flat: bool,
    no_active_orders: bool,
    reconciled: bool,
    bootstrap_seed_digest: str,
    account_identity_digest: str,
    risk_overlay_digest: str,
    operator_reason: str,
    activation_confirmation: str,
    rebase_confirmation: str,
) -> RuntimeState:
    """Explicitly return an exact migrated identity to Stress-90 while still HALTED."""

    from .directional_stress90_state import REBASE_CONFIRMATION

    if (
        activation_confirmation != STRESS90_ACTIVATION_CONFIRMATION
        or rebase_confirmation != REBASE_CONFIRMATION
    ):
        raise RuntimeError("Stress-90 reactivation confirmation is invalid")
    marker = state.strategy_states.get(POLICY_IDENTITY_STATE_KEY)
    if (
        not isinstance(marker, dict)
        or set(marker) != _MIGRATED_EXECUTION_ALIGNED_MARKER_FIELDS
        or marker.get("policy_id") != "execution_aligned"
        or marker.get("policy_definition_digest") != ""
        or marker.get("products_manifest_digest") != STRESS90_POLICY.products_manifest_digest
        or marker.get("account_identity_digest") != account_identity_digest
        or marker.get("migrated_from_policy_id") != STRESS90_POLICY.policy_id
        or marker.get("migrated_from_policy_definition_digest")
        != STRESS90_POLICY.policy_definition_digest
    ):
        raise RuntimeError(
            "Stress-90 reactivation requires exact migrated execution-aligned provenance"
        )
    return activate_stress90_policy(
        state,
        broker_flat=broker_flat,
        local_flat=local_flat,
        no_active_orders=no_active_orders,
        reconciled=reconciled,
        bootstrap_seed_digest=bootstrap_seed_digest,
        account_identity_digest=account_identity_digest,
        risk_overlay_digest=risk_overlay_digest,
        operator_reason=operator_reason,
        strong_confirmation=activation_confirmation,
    )
