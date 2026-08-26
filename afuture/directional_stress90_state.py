"""Strict exactly-once state and immutable seed identity for Stress-90."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from math import isfinite
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import MappingProxyType

from .directional_stress90_policy import (
    STRESS90_POLICY,
    ProductWeights,
    Stress90CandidateState,
    Stress90Decision,
    Stress90PolicyDefinition,
    candidate_state_digest,
    canonical_stress90_digest,
    step_stress90_candidate,
)
from .durable_file_creation import (
    DurableFileCreationToken,
    canonical_file_path,
    create_durable_file_exclusive,
    durable_file_lock,
)

STRESS90_STATE_SCHEMA_VERSION = 4
STRESS90_STATE_KIND = "afuture.directional.stress90.policy-state"
STRESS90_SEED_SCHEMA_VERSION = 1
STRESS90_SEED_KIND = "afuture.directional.stress90.bootstrap-seed"
MAX_COMPLETED_CONCENTRATIONS = 100_000
MAX_BOOTSTRAP_SOURCES = 32
FIXED_BOOTSTRAP_INPUT_NAMES = (
    "broad_daily_universe.csv",
    "return_target_specific_contracts.csv",
    "execution_aligned_weights.csv",
    "prior_two_year_broad_60m.csv",
    "two_year_broad_60m.csv",
)
REBASE_CONFIRMATION = "RESET_STRESS90_ACCOUNT_PATH"

_HEX_SHA256 = re.compile(r"[0-9a-f]{64}")
_WEIGHT_EPS = 1e-12


class Stress90StateIntegrityError(RuntimeError):
    """Persisted Stress-90 state, identity, or transition cannot be trusted."""


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Stress90StateIntegrityError(f"duplicate Stress-90 JSON key: {key}")
        result[key] = value
    return result


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise Stress90StateIntegrityError("Stress-90 state is not canonical JSON") from exc


def _checksum(unsigned: Mapping[str, object]) -> str:
    return sha256(_canonical_json(dict(unsigned))).hexdigest()


def _valid_sha256(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _HEX_SHA256.fullmatch(value) is None:
        raise Stress90StateIntegrityError(f"{name} must be a lowercase SHA-256")
    return value


def _valid_day(value: object, *, name: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise Stress90StateIntegrityError(f"{name} must be YYYYMMDD")
    try:
        parsed = datetime.strptime(value, "%Y%m%d").strftime("%Y%m%d")
    except ValueError as exc:
        raise Stress90StateIntegrityError(f"{name} must be YYYYMMDD") from exc
    if parsed != value:
        raise Stress90StateIntegrityError(f"{name} must be YYYYMMDD")
    return value


def _valid_source_manifest(raw: object) -> Mapping[str, str]:
    if not isinstance(raw, Mapping) or not raw or len(raw) > MAX_BOOTSTRAP_SOURCES:
        raise Stress90StateIntegrityError("bootstrap source manifest is invalid")
    result: dict[str, str] = {}
    for raw_name, raw_digest in raw.items():
        if not isinstance(raw_name, str) or not raw_name or raw_name in result:
            raise Stress90StateIntegrityError("bootstrap source manifest names are invalid")
        result[raw_name] = _valid_sha256(
            raw_digest,
            name=f"bootstrap source digest for {raw_name}",
        )
    return MappingProxyType(dict(sorted(result.items())))


def _valid_weights(
    raw: object,
    *,
    name: str,
    definition: Stress90PolicyDefinition = STRESS90_POLICY,
) -> ProductWeights:
    if not isinstance(raw, Mapping):
        raise Stress90StateIntegrityError(f"{name} must be a product-weight object")
    normalized: dict[str, float] = {}
    for raw_product, raw_value in raw.items():
        if not isinstance(raw_product, str) or raw_product != raw_product.upper():
            raise Stress90StateIntegrityError(f"{name} product identities must be uppercase")
        if raw_product in normalized:
            raise Stress90StateIntegrityError(f"{name} contains duplicate products")
        if (
            isinstance(raw_value, bool)
            or not isinstance(raw_value, (int, float))
            or not isfinite(raw_value)
        ):
            raise Stress90StateIntegrityError(f"{name} weights must be finite")
        normalized[raw_product] = float(raw_value)
    if set(normalized) != set(definition.products):
        raise Stress90StateIntegrityError(f"{name} product manifest mismatch")
    ordered = {product: normalized[product] for product in definition.products}
    if sum(abs(value) for value in ordered.values()) > definition.max_gross_leverage + _WEIGHT_EPS:
        raise Stress90StateIntegrityError(f"{name} exceeds 2x gross")
    return MappingProxyType(ordered)


def _valid_concentrations(raw: object) -> tuple[float, ...]:
    if not isinstance(raw, (list, tuple)) or len(raw) > MAX_COMPLETED_CONCENTRATIONS:
        raise Stress90StateIntegrityError("completed concentrations are invalid")
    result: list[float] = []
    for raw_value in raw:
        if (
            isinstance(raw_value, bool)
            or not isinstance(raw_value, (int, float))
            or not isfinite(raw_value)
            or not 0.0 < float(raw_value) <= 1.0
        ):
            raise Stress90StateIntegrityError("completed concentrations must be finite in (0, 1]")
        result.append(float(raw_value))
    return tuple(result)


def _valid_recent_returns(raw: object) -> tuple[float, ...]:
    if not isinstance(raw, (list, tuple)) or len(raw) > 2:
        raise Stress90StateIntegrityError("adaptive-margin completed returns exceed two days")
    result: list[float] = []
    for raw_value in raw:
        if (
            isinstance(raw_value, bool)
            or not isinstance(raw_value, (int, float))
            or not isfinite(raw_value)
            or float(raw_value) <= -1.0
        ):
            raise Stress90StateIntegrityError("adaptive-margin completed returns are invalid")
        result.append(float(raw_value))
    return tuple(result)


def _valid_digest_map(
    raw: object,
    *,
    name: str,
    expected_keys: set[str],
) -> Mapping[str, str]:
    if not isinstance(raw, Mapping) or set(raw) != expected_keys:
        raise Stress90StateIntegrityError(f"{name} fields are invalid")
    return MappingProxyType(
        {key: _valid_sha256(raw[key], name=f"{name} {key}") for key in sorted(expected_keys)}
    )


def _bootstrap_identity_digest(
    *,
    policy_id: str,
    policy_definition_digest: str,
    products_manifest_digest: str,
    supported_oi_products: Sequence[str],
    bootstrap_source_manifest: Mapping[str, str],
    bootstrap_through_day: str,
    bootstrap_candidate_state_digest: str,
    historical_candidate_weight_sha256: str,
) -> str:
    return canonical_stress90_digest(
        {
            "policy_id": policy_id,
            "policy_definition_digest": policy_definition_digest,
            "products_manifest_digest": products_manifest_digest,
            "supported_oi_products": tuple(supported_oi_products),
            "bootstrap_source_manifest": bootstrap_source_manifest,
            "bootstrap_through_day": bootstrap_through_day,
            "bootstrap_candidate_state_digest": bootstrap_candidate_state_digest,
            "historical_candidate_weight_sha256": historical_candidate_weight_sha256,
        }
    )


@dataclass(frozen=True)
class Stress90BootstrapSeed:
    """Immutable candidate seed; it deliberately contains no live account path."""

    policy_id: str
    policy_definition_digest: str
    products_manifest_digest: str
    supported_oi_products: tuple[str, ...]
    bootstrap_source_manifest: Mapping[str, str]
    bootstrap_through_day: str
    last_completed_input_day: str
    last_completed_target_day: str
    last_decision_digest: str
    last_oi_confirmed_weights: ProductWeights
    last_cost_approved_weights: ProductWeights
    last_survivor_weights: ProductWeights
    completed_concentrations: tuple[float, ...]
    bootstrap_candidate_state_digest: str
    historical_candidate_weight_sha256: str
    seed_digest: str

    @classmethod
    def from_candidate_state(
        cls,
        candidate_state: Stress90CandidateState,
        *,
        bootstrap_source_manifest: Mapping[str, str],
        bootstrap_through_day: str,
        last_completed_input_day: str,
        definition: Stress90PolicyDefinition = STRESS90_POLICY,
    ) -> Stress90BootstrapSeed:
        through = _valid_day(bootstrap_through_day, name="bootstrap through day")
        input_day = _valid_day(last_completed_input_day, name="last completed input day")
        if candidate_state.policy_definition_digest != definition.policy_definition_digest:
            raise Stress90StateIntegrityError("policy definition digest mismatch in seed")
        if candidate_state.products_manifest_digest != definition.products_manifest_digest:
            raise Stress90StateIntegrityError("products manifest digest mismatch in seed")
        if candidate_state.last_completed_target_day != through:
            raise Stress90StateIntegrityError("bootstrap through day must equal final target day")
        if input_day is None or through is None or input_day >= through:
            raise Stress90StateIntegrityError("last completed input day must precede final target")
        last_decision = _valid_sha256(
            candidate_state.last_decision_digest,
            name="last bootstrap decision digest",
        )
        sources = _valid_source_manifest(bootstrap_source_manifest)
        oi = _valid_weights(candidate_state.last_oi_confirmed_weights, name="seed OI-confirmed")
        cost = _valid_weights(candidate_state.last_cost_approved_weights, name="seed cost-approved")
        survivor = _valid_weights(candidate_state.last_survivor_weights, name="seed survivor")
        concentrations = _valid_concentrations(candidate_state.completed_concentrations)
        state_digest = candidate_state_digest(candidate_state)
        seed_digest = _bootstrap_identity_digest(
            policy_id=definition.policy_id,
            policy_definition_digest=definition.policy_definition_digest,
            products_manifest_digest=definition.products_manifest_digest,
            supported_oi_products=definition.oi_products,
            bootstrap_source_manifest=sources,
            bootstrap_through_day=through,
            bootstrap_candidate_state_digest=state_digest,
            historical_candidate_weight_sha256=definition.historical_candidate_weight_sha256,
        )
        return cls(
            policy_id=definition.policy_id,
            policy_definition_digest=definition.policy_definition_digest,
            products_manifest_digest=definition.products_manifest_digest,
            supported_oi_products=definition.oi_products,
            bootstrap_source_manifest=sources,
            bootstrap_through_day=through,
            last_completed_input_day=input_day,
            last_completed_target_day=through,
            last_decision_digest=last_decision,
            last_oi_confirmed_weights=oi,
            last_cost_approved_weights=cost,
            last_survivor_weights=survivor,
            completed_concentrations=concentrations,
            bootstrap_candidate_state_digest=state_digest,
            historical_candidate_weight_sha256=definition.historical_candidate_weight_sha256,
            seed_digest=seed_digest,
        )


@dataclass(frozen=True)
class Stress90PreparedDecision:
    previous_target_trading_day: str
    target_trading_day: str
    daily_decision_digest: str
    source_input_digest: str
    input_days: Mapping[str, str]
    input_digests: Mapping[str, str]
    layer_digests: Mapping[str, str]
    base_weights: ProductWeights
    oi_confirmed_weights: ProductWeights
    cost_approved_weights: ProductWeights
    survivor_weights: ProductWeights
    current_hhi: float | None
    prior_hhi_median: float | None
    concentration_freeze: bool

    @classmethod
    def from_decision(
        cls,
        decision: Stress90Decision,
        *,
        previous_target_trading_day: str,
        source_input_digest: str,
    ) -> Stress90PreparedDecision:
        return cls(
            previous_target_trading_day=previous_target_trading_day,
            target_trading_day=decision.target_trading_day,
            daily_decision_digest=decision.daily_decision_digest,
            source_input_digest=source_input_digest,
            input_days=MappingProxyType(dict(decision.input_days)),
            input_digests=MappingProxyType(dict(decision.input_digests)),
            layer_digests=MappingProxyType(dict(decision.layer_digests)),
            base_weights=MappingProxyType(dict(decision.base_weights)),
            oi_confirmed_weights=MappingProxyType(dict(decision.oi_confirmed_weights)),
            cost_approved_weights=MappingProxyType(dict(decision.cost_approved_weights)),
            survivor_weights=MappingProxyType(dict(decision.survivor_weights)),
            current_hhi=decision.current_hhi,
            prior_hhi_median=decision.prior_hhi_median,
            concentration_freeze=decision.concentration_freeze,
        )


@dataclass(frozen=True)
class Stress90PolicyState:
    policy_id: str
    policy_definition_digest: str
    products_manifest_digest: str
    supported_oi_products: tuple[str, ...]
    bootstrap_source_manifest: Mapping[str, str]
    bootstrap_through_day: str
    bootstrap_candidate_state_digest: str
    bootstrap_seed_digest: str
    last_completed_input_day: str
    last_completed_target_day: str
    last_decision_digest: str
    last_oi_confirmed_weights: ProductWeights
    last_cost_approved_weights: ProductWeights
    last_survivor_weights: ProductWeights
    completed_concentrations: tuple[float, ...]
    prepared_decision: Stress90PreparedDecision | None
    completed_account_wealth: float
    completed_account_high_watermark: float
    last_completed_account_day: str | None
    recent_daily_returns_for_adaptive_margin: tuple[float, ...]
    live_inception_day: str | None
    live_inception_equity: float | None
    live_account_identity_digest: str | None
    live_account_epoch: str | None

    @classmethod
    def from_seed(
        cls,
        seed: Stress90BootstrapSeed,
        *,
        prepared_decision: Stress90PreparedDecision | None = None,
        definition: Stress90PolicyDefinition = STRESS90_POLICY,
    ) -> Stress90PolicyState:
        if seed.policy_definition_digest != definition.policy_definition_digest:
            raise Stress90StateIntegrityError("policy definition digest mismatch in seed")
        if seed.seed_digest != _bootstrap_identity_digest(
            policy_id=seed.policy_id,
            policy_definition_digest=seed.policy_definition_digest,
            products_manifest_digest=seed.products_manifest_digest,
            supported_oi_products=seed.supported_oi_products,
            bootstrap_source_manifest=seed.bootstrap_source_manifest,
            bootstrap_through_day=seed.bootstrap_through_day,
            bootstrap_candidate_state_digest=seed.bootstrap_candidate_state_digest,
            historical_candidate_weight_sha256=seed.historical_candidate_weight_sha256,
        ):
            raise Stress90StateIntegrityError("bootstrap seed digest mismatch")
        state = cls(
            policy_id=seed.policy_id,
            policy_definition_digest=seed.policy_definition_digest,
            products_manifest_digest=seed.products_manifest_digest,
            supported_oi_products=seed.supported_oi_products,
            bootstrap_source_manifest=seed.bootstrap_source_manifest,
            bootstrap_through_day=seed.bootstrap_through_day,
            bootstrap_candidate_state_digest=seed.bootstrap_candidate_state_digest,
            bootstrap_seed_digest=seed.seed_digest,
            last_completed_input_day=seed.last_completed_input_day,
            last_completed_target_day=seed.last_completed_target_day,
            last_decision_digest=seed.last_decision_digest,
            last_oi_confirmed_weights=seed.last_oi_confirmed_weights,
            last_cost_approved_weights=seed.last_cost_approved_weights,
            last_survivor_weights=seed.last_survivor_weights,
            completed_concentrations=seed.completed_concentrations,
            prepared_decision=prepared_decision,
            completed_account_wealth=1.0,
            completed_account_high_watermark=1.0,
            last_completed_account_day=None,
            recent_daily_returns_for_adaptive_margin=(),
            live_inception_day=None,
            live_inception_equity=None,
            live_account_identity_digest=None,
            live_account_epoch=None,
        )
        _validate_state(state, definition)
        return state

    def candidate_state(self) -> Stress90CandidateState:
        return Stress90CandidateState(
            policy_definition_digest=self.policy_definition_digest,
            products_manifest_digest=self.products_manifest_digest,
            last_completed_target_day=self.last_completed_target_day,
            last_decision_digest=self.last_decision_digest,
            last_oi_confirmed_weights=self.last_oi_confirmed_weights,
            last_cost_approved_weights=self.last_cost_approved_weights,
            last_survivor_weights=self.last_survivor_weights,
            completed_concentrations=self.completed_concentrations,
        )


@dataclass(frozen=True)
class Stress90DecisionInputs:
    previous_target_trading_day: str
    target_trading_day: str
    base_weights: Mapping[str, float]
    completed_close_history: Mapping[str, Sequence[float]]
    completed_oi_flow: Mapping[str, int | float | None]
    completed_close_day: str
    completed_oi_day: str


@dataclass(frozen=True)
class Stress90StateRecord:
    state: Stress90PolicyState
    sequence: int
    checksum: str


def _decision_inputs_digest(
    inputs: Stress90DecisionInputs,
    definition: Stress90PolicyDefinition,
) -> str:
    previous = _valid_day(inputs.previous_target_trading_day, name="previous target trading day")
    target = _valid_day(inputs.target_trading_day, name="target trading day")
    close_day = _valid_day(inputs.completed_close_day, name="completed close day")
    oi_day = _valid_day(inputs.completed_oi_day, name="completed OI day")
    base = _valid_weights(inputs.base_weights, name="decision base weights", definition=definition)
    close_keys = {str(product).upper() for product in inputs.completed_close_history}
    if close_keys != set(definition.products) or len(inputs.completed_close_history) != len(
        definition.products
    ):
        raise Stress90StateIntegrityError("decision close history product manifest mismatch")
    close: dict[str, tuple[float, ...]] = {}
    for product in definition.products:
        values = tuple(float(value) for value in inputs.completed_close_history[product])
        if any(not isfinite(value) or value <= 0.0 for value in values):
            raise Stress90StateIntegrityError("decision close history is incomplete")
        close[product] = values
    flow_keys = {str(product).upper() for product in inputs.completed_oi_flow}
    if flow_keys != set(definition.oi_products) or len(inputs.completed_oi_flow) != len(
        definition.oi_products
    ):
        raise Stress90StateIntegrityError("decision OI flow product manifest mismatch")
    flow: dict[str, int] = {}
    for product in definition.oi_products:
        value = inputs.completed_oi_flow[product]
        if isinstance(value, bool) or value not in (-1, 0, 1):
            raise Stress90StateIntegrityError(f"invalid completed OI flow: {product}")
        flow[product] = int(value)
    return canonical_stress90_digest(
        {
            "previous_target_trading_day": previous,
            "target_trading_day": target,
            "base_weights": base,
            "completed_close_history": close,
            "completed_oi_flow": flow,
            "completed_close_day": close_day,
            "completed_oi_day": oi_day,
        }
    )


def stress90_decision_inputs_digest(
    inputs: Stress90DecisionInputs,
    definition: Stress90PolicyDefinition = STRESS90_POLICY,
) -> str:
    """Return the canonical identity of one complete production decision input."""

    return _decision_inputs_digest(inputs, definition)


def _validate_prepared(
    prepared: Stress90PreparedDecision,
    state: Stress90PolicyState,
    definition: Stress90PolicyDefinition,
) -> None:
    previous = _valid_day(
        prepared.previous_target_trading_day,
        name="prepared previous target day",
    )
    target = _valid_day(prepared.target_trading_day, name="prepared target day")
    if previous is None or target is None or previous >= target:
        raise Stress90StateIntegrityError("prepared target-day ordering is invalid")
    _valid_sha256(prepared.daily_decision_digest, name="prepared daily decision digest")
    _valid_sha256(prepared.source_input_digest, name="prepared source input digest")
    if set(prepared.input_days) != {"completed_close", "completed_oi"}:
        raise Stress90StateIntegrityError("prepared input day fields are invalid")
    for input_name, input_value in prepared.input_days.items():
        _valid_day(input_value, name=f"prepared {input_name} day")
    _valid_digest_map(
        prepared.input_digests,
        name="prepared input digests",
        expected_keys={"prior_state", "base_weights", "completed_close", "completed_oi"},
    )
    _valid_digest_map(
        prepared.layer_digests,
        name="prepared layer digests",
        expected_keys={"base", "oi", "cost", "survivor"},
    )
    base = _valid_weights(prepared.base_weights, name="prepared base", definition=definition)
    oi = _valid_weights(
        prepared.oi_confirmed_weights,
        name="prepared OI-confirmed",
        definition=definition,
    )
    cost = _valid_weights(
        prepared.cost_approved_weights,
        name="prepared cost-approved",
        definition=definition,
    )
    survivor = _valid_weights(
        prepared.survivor_weights,
        name="prepared survivor",
        definition=definition,
    )
    del base
    if oi != state.last_oi_confirmed_weights or cost != state.last_cost_approved_weights:
        raise Stress90StateIntegrityError("prepared layer state does not match policy state")
    if survivor != state.last_survivor_weights:
        raise Stress90StateIntegrityError("prepared survivor state does not match policy state")
    for hhi_name, hhi_value in (
        ("current HHI", prepared.current_hhi),
        ("prior HHI median", prepared.prior_hhi_median),
    ):
        if hhi_value is not None and (not isfinite(hhi_value) or not 0.0 < hhi_value <= 1.0):
            raise Stress90StateIntegrityError(f"prepared {hhi_name} is invalid")
    if not isinstance(prepared.concentration_freeze, bool):
        raise Stress90StateIntegrityError("prepared concentration freeze must be bool")
    if state.last_completed_target_day != target:
        raise Stress90StateIntegrityError("prepared target does not match completed target")
    if state.last_decision_digest != prepared.daily_decision_digest:
        raise Stress90StateIntegrityError("prepared decision digest does not match policy state")
    if prepared.current_hhi is not None and (
        not state.completed_concentrations
        or state.completed_concentrations[-1] != prepared.current_hhi
    ):
        raise Stress90StateIntegrityError("prepared HHI was not appended exactly once")


def _validate_state(
    state: Stress90PolicyState,
    definition: Stress90PolicyDefinition = STRESS90_POLICY,
) -> None:
    if not isinstance(state, Stress90PolicyState):
        raise Stress90StateIntegrityError("Stress-90 policy state has invalid type")
    if state.policy_id != definition.policy_id:
        raise Stress90StateIntegrityError("policy id mismatch")
    if state.policy_definition_digest != definition.policy_definition_digest:
        raise Stress90StateIntegrityError("policy definition digest mismatch")
    if state.products_manifest_digest != definition.products_manifest_digest:
        raise Stress90StateIntegrityError("products manifest digest mismatch")
    if tuple(state.supported_oi_products) != definition.oi_products:
        raise Stress90StateIntegrityError("supported OI products mismatch")
    sources = _valid_source_manifest(state.bootstrap_source_manifest)
    through = _valid_day(state.bootstrap_through_day, name="bootstrap through day")
    input_day = _valid_day(state.last_completed_input_day, name="last completed input day")
    target_day = _valid_day(state.last_completed_target_day, name="last completed target day")
    if through is None or input_day is None or target_day is None:
        raise Stress90StateIntegrityError("required Stress-90 target days are missing")
    if input_day >= target_day or target_day < through:
        raise Stress90StateIntegrityError("Stress-90 completed day ordering is invalid")
    _valid_sha256(state.bootstrap_candidate_state_digest, name="bootstrap candidate state digest")
    expected_seed = _bootstrap_identity_digest(
        policy_id=state.policy_id,
        policy_definition_digest=state.policy_definition_digest,
        products_manifest_digest=state.products_manifest_digest,
        supported_oi_products=state.supported_oi_products,
        bootstrap_source_manifest=sources,
        bootstrap_through_day=through,
        bootstrap_candidate_state_digest=state.bootstrap_candidate_state_digest,
        historical_candidate_weight_sha256=definition.historical_candidate_weight_sha256,
    )
    if state.bootstrap_seed_digest != expected_seed:
        raise Stress90StateIntegrityError("bootstrap seed digest mismatch")
    _valid_sha256(state.last_decision_digest, name="last decision digest")
    oi = _valid_weights(
        state.last_oi_confirmed_weights,
        name="last OI-confirmed",
        definition=definition,
    )
    cost = _valid_weights(
        state.last_cost_approved_weights,
        name="last cost-approved",
        definition=definition,
    )
    survivor = _valid_weights(
        state.last_survivor_weights,
        name="last survivor",
        definition=definition,
    )
    del oi, cost, survivor
    concentrations = _valid_concentrations(state.completed_concentrations)
    if concentrations != state.completed_concentrations:
        raise Stress90StateIntegrityError("completed concentrations are not canonical")
    for name, value in (
        ("completed account wealth", state.completed_account_wealth),
        ("completed account high watermark", state.completed_account_high_watermark),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            or float(value) <= 0.0
        ):
            raise Stress90StateIntegrityError(f"{name} must be finite and positive")
    if state.completed_account_wealth > state.completed_account_high_watermark + _WEIGHT_EPS:
        raise Stress90StateIntegrityError("completed account wealth exceeds high watermark")
    account_day = _valid_day(
        state.last_completed_account_day,
        name="last completed account day",
        optional=True,
    )
    inception_day = _valid_day(
        state.live_inception_day,
        name="live inception day",
        optional=True,
    )
    if account_day is not None and inception_day is None:
        raise Stress90StateIntegrityError("completed account day requires live inception day")
    if account_day is not None and inception_day is not None and account_day < inception_day:
        raise Stress90StateIntegrityError("completed account day precedes live inception")
    inception_equity = state.live_inception_equity
    if (inception_day is None) != (inception_equity is None):
        raise Stress90StateIntegrityError(
            "live inception day and equity must be initialized together"
        )
    if inception_equity is not None and (
        isinstance(inception_equity, bool)
        or not isinstance(inception_equity, (int, float))
        or not isfinite(inception_equity)
        or float(inception_equity) <= 0.0
    ):
        raise Stress90StateIntegrityError("live inception equity must be finite and positive")
    _valid_recent_returns(state.recent_daily_returns_for_adaptive_margin)
    if state.live_account_identity_digest is not None:
        _valid_sha256(
            state.live_account_identity_digest,
            name="live account identity digest",
        )
    if state.live_account_epoch is not None:
        _valid_sha256(
            state.live_account_epoch,
            name="live account epoch",
        )
    if (state.live_account_identity_digest is None) != (state.live_account_epoch is None):
        raise Stress90StateIntegrityError(
            "live account identity and lifecycle epoch must be bound together"
        )
    if state.prepared_decision is not None:
        _validate_prepared(state.prepared_decision, state, definition)


def _prepared_payload(prepared: Stress90PreparedDecision | None) -> dict[str, object] | None:
    if prepared is None:
        return None
    return {
        "previous_target_trading_day": prepared.previous_target_trading_day,
        "target_trading_day": prepared.target_trading_day,
        "daily_decision_digest": prepared.daily_decision_digest,
        "source_input_digest": prepared.source_input_digest,
        "input_days": dict(prepared.input_days),
        "input_digests": dict(prepared.input_digests),
        "layer_digests": dict(prepared.layer_digests),
        "base_weights": dict(prepared.base_weights),
        "oi_confirmed_weights": dict(prepared.oi_confirmed_weights),
        "cost_approved_weights": dict(prepared.cost_approved_weights),
        "survivor_weights": dict(prepared.survivor_weights),
        "current_hhi": prepared.current_hhi,
        "prior_hhi_median": prepared.prior_hhi_median,
        "concentration_freeze": prepared.concentration_freeze,
    }


def _state_payload(state: Stress90PolicyState) -> dict[str, object]:
    return {
        "policy_id": state.policy_id,
        "policy_definition_digest": state.policy_definition_digest,
        "products_manifest_digest": state.products_manifest_digest,
        "supported_oi_products": list(state.supported_oi_products),
        "bootstrap_source_manifest": dict(state.bootstrap_source_manifest),
        "bootstrap_through_day": state.bootstrap_through_day,
        "bootstrap_candidate_state_digest": state.bootstrap_candidate_state_digest,
        "bootstrap_seed_digest": state.bootstrap_seed_digest,
        "last_completed_input_day": state.last_completed_input_day,
        "last_completed_target_day": state.last_completed_target_day,
        "last_decision_digest": state.last_decision_digest,
        "last_oi_confirmed_weights": dict(state.last_oi_confirmed_weights),
        "last_cost_approved_weights": dict(state.last_cost_approved_weights),
        "last_survivor_weights": dict(state.last_survivor_weights),
        "completed_concentrations": list(state.completed_concentrations),
        "prepared_decision": _prepared_payload(state.prepared_decision),
        "completed_account_wealth": state.completed_account_wealth,
        "completed_account_high_watermark": state.completed_account_high_watermark,
        "last_completed_account_day": state.last_completed_account_day,
        "recent_daily_returns_for_adaptive_margin": list(
            state.recent_daily_returns_for_adaptive_margin
        ),
        "live_inception_day": state.live_inception_day,
        "live_inception_equity": state.live_inception_equity,
        "live_account_identity_digest": state.live_account_identity_digest,
        "live_account_epoch": state.live_account_epoch,
    }


_STATE_FIELDS = {
    "policy_id",
    "policy_definition_digest",
    "products_manifest_digest",
    "supported_oi_products",
    "bootstrap_source_manifest",
    "bootstrap_through_day",
    "bootstrap_candidate_state_digest",
    "bootstrap_seed_digest",
    "last_completed_input_day",
    "last_completed_target_day",
    "last_decision_digest",
    "last_oi_confirmed_weights",
    "last_cost_approved_weights",
    "last_survivor_weights",
    "completed_concentrations",
    "prepared_decision",
    "completed_account_wealth",
    "completed_account_high_watermark",
    "last_completed_account_day",
    "recent_daily_returns_for_adaptive_margin",
    "live_inception_day",
    "live_inception_equity",
    "live_account_identity_digest",
    "live_account_epoch",
}

_PREPARED_FIELDS = {
    "previous_target_trading_day",
    "target_trading_day",
    "daily_decision_digest",
    "source_input_digest",
    "input_days",
    "input_digests",
    "layer_digests",
    "base_weights",
    "oi_confirmed_weights",
    "cost_approved_weights",
    "survivor_weights",
    "current_hhi",
    "prior_hhi_median",
    "concentration_freeze",
}


def _prepared_from_payload(
    raw: object,
    definition: Stress90PolicyDefinition,
) -> Stress90PreparedDecision | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or set(raw) != _PREPARED_FIELDS:
        raise Stress90StateIntegrityError("prepared decision fields are invalid")
    input_days = raw["input_days"]
    if not isinstance(input_days, Mapping) or set(input_days) != {
        "completed_close",
        "completed_oi",
    }:
        raise Stress90StateIntegrityError("prepared input day fields are invalid")
    return Stress90PreparedDecision(
        previous_target_trading_day=str(raw["previous_target_trading_day"]),
        target_trading_day=str(raw["target_trading_day"]),
        daily_decision_digest=str(raw["daily_decision_digest"]),
        source_input_digest=str(raw["source_input_digest"]),
        input_days=MappingProxyType({str(key): str(value) for key, value in input_days.items()}),
        input_digests=_valid_digest_map(
            raw["input_digests"],
            name="prepared input digests",
            expected_keys={"prior_state", "base_weights", "completed_close", "completed_oi"},
        ),
        layer_digests=_valid_digest_map(
            raw["layer_digests"],
            name="prepared layer digests",
            expected_keys={"base", "oi", "cost", "survivor"},
        ),
        base_weights=_valid_weights(
            raw["base_weights"], name="prepared base", definition=definition
        ),
        oi_confirmed_weights=_valid_weights(
            raw["oi_confirmed_weights"],
            name="prepared OI-confirmed",
            definition=definition,
        ),
        cost_approved_weights=_valid_weights(
            raw["cost_approved_weights"],
            name="prepared cost-approved",
            definition=definition,
        ),
        survivor_weights=_valid_weights(
            raw["survivor_weights"],
            name="prepared survivor",
            definition=definition,
        ),
        current_hhi=(None if raw["current_hhi"] is None else float(raw["current_hhi"])),
        prior_hhi_median=(
            None if raw["prior_hhi_median"] is None else float(raw["prior_hhi_median"])
        ),
        concentration_freeze=raw["concentration_freeze"],
    )


def _state_from_payload(
    raw: object,
    definition: Stress90PolicyDefinition,
) -> Stress90PolicyState:
    if not isinstance(raw, Mapping) or set(raw) != _STATE_FIELDS:
        raise Stress90StateIntegrityError("Stress-90 policy state fields are invalid")
    supported = raw["supported_oi_products"]
    if not isinstance(supported, list) or any(not isinstance(value, str) for value in supported):
        raise Stress90StateIntegrityError("supported OI products are invalid")
    state = Stress90PolicyState(
        policy_id=str(raw["policy_id"]),
        policy_definition_digest=str(raw["policy_definition_digest"]),
        products_manifest_digest=str(raw["products_manifest_digest"]),
        supported_oi_products=tuple(supported),
        bootstrap_source_manifest=_valid_source_manifest(raw["bootstrap_source_manifest"]),
        bootstrap_through_day=str(raw["bootstrap_through_day"]),
        bootstrap_candidate_state_digest=str(raw["bootstrap_candidate_state_digest"]),
        bootstrap_seed_digest=str(raw["bootstrap_seed_digest"]),
        last_completed_input_day=str(raw["last_completed_input_day"]),
        last_completed_target_day=str(raw["last_completed_target_day"]),
        last_decision_digest=str(raw["last_decision_digest"]),
        last_oi_confirmed_weights=_valid_weights(
            raw["last_oi_confirmed_weights"],
            name="last OI-confirmed",
            definition=definition,
        ),
        last_cost_approved_weights=_valid_weights(
            raw["last_cost_approved_weights"],
            name="last cost-approved",
            definition=definition,
        ),
        last_survivor_weights=_valid_weights(
            raw["last_survivor_weights"],
            name="last survivor",
            definition=definition,
        ),
        completed_concentrations=_valid_concentrations(raw["completed_concentrations"]),
        prepared_decision=_prepared_from_payload(raw["prepared_decision"], definition),
        completed_account_wealth=float(raw["completed_account_wealth"]),
        completed_account_high_watermark=float(raw["completed_account_high_watermark"]),
        last_completed_account_day=(
            None
            if raw["last_completed_account_day"] is None
            else str(raw["last_completed_account_day"])
        ),
        recent_daily_returns_for_adaptive_margin=_valid_recent_returns(
            raw["recent_daily_returns_for_adaptive_margin"]
        ),
        live_inception_day=(
            None if raw["live_inception_day"] is None else str(raw["live_inception_day"])
        ),
        live_inception_equity=(
            None if raw["live_inception_equity"] is None else float(raw["live_inception_equity"])
        ),
        live_account_identity_digest=(
            None
            if raw["live_account_identity_digest"] is None
            else str(raw["live_account_identity_digest"])
        ),
        live_account_epoch=(
            None if raw["live_account_epoch"] is None else str(raw["live_account_epoch"])
        ),
    )
    _validate_state(state, definition)
    return state


class Stress90PolicyStateStore:
    """Atomic checksummed state; `.prev` is explicit evidence, never fallback input."""

    def __init__(
        self,
        path: str | Path,
        *,
        definition: Stress90PolicyDefinition = STRESS90_POLICY,
    ) -> None:
        self.path = canonical_file_path(path)
        self.definition = definition
        self._last_creation_token: DurableFileCreationToken | None = None

    @property
    def last_creation_token(self) -> DurableFileCreationToken | None:
        return self._last_creation_token

    @property
    def previous_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.prev")

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.lock")

    def _require_fresh_or_current(self) -> None:
        if self.path.exists():
            return
        evidence = [path.name for path in (self.previous_path, self.lock_path) if path.exists()]
        if evidence:
            raise Stress90StateIntegrityError(
                "current Stress-90 policy state is missing while incident evidence exists: "
                + ", ".join(evidence)
            )

    def load_record(self) -> Stress90StateRecord | None:
        with durable_file_lock(self.path):
            return self.load_record_unlocked()

    def load_record_unlocked(self) -> Stress90StateRecord | None:
        """Decode current while the caller already holds this artifact lock."""

        self._require_fresh_or_current()
        if not self.path.exists():
            return None
        return self._read_record(self.path)

    def load_required_record(self) -> Stress90StateRecord:
        record = self.load_record()
        if record is None:
            raise Stress90StateIntegrityError("required Stress-90 policy state is missing")
        return record

    def load_required(self) -> Stress90PolicyState:
        return self.load_required_record().state

    def load_previous_record(self) -> Stress90StateRecord:
        with durable_file_lock(self.path):
            return self.load_previous_record_unlocked()

    def load_previous_record_unlocked(self) -> Stress90StateRecord:
        """Decode `.prev` while the caller already holds the current-path lock."""

        if not self.previous_path.exists():
            raise Stress90StateIntegrityError("previous Stress-90 policy state is missing")
        return self._read_record(self.previous_path)

    def _read_record(self, path: Path) -> Stress90StateRecord:
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Stress90StateIntegrityError("invalid Stress-90 state UTF-8") from exc
        try:
            raw = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
        except json.JSONDecodeError as exc:
            raise Stress90StateIntegrityError("invalid Stress-90 state JSON") from exc
        if not isinstance(raw, Mapping):
            raise Stress90StateIntegrityError("Stress-90 state envelope must be an object")
        fields = {"kind", "schema_version", "sequence", "state", "checksum"}
        if set(raw) != fields:
            raise Stress90StateIntegrityError("Stress-90 state envelope fields are invalid")
        if raw["kind"] != STRESS90_STATE_KIND:
            raise Stress90StateIntegrityError("Stress-90 state kind is invalid")
        schema = raw["schema_version"]
        if (
            isinstance(schema, bool)
            or not isinstance(schema, int)
            or schema != STRESS90_STATE_SCHEMA_VERSION
        ):
            raise Stress90StateIntegrityError("Stress-90 state schema is unsupported")
        sequence = raw["sequence"]
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise Stress90StateIntegrityError("Stress-90 state sequence must be positive")
        checksum = raw["checksum"]
        _valid_sha256(checksum, name="Stress-90 state checksum")
        unsigned = {key: value for key, value in raw.items() if key != "checksum"}
        if checksum != _checksum(unsigned):
            raise Stress90StateIntegrityError("Stress-90 state checksum mismatch")
        state = _state_from_payload(raw["state"], self.definition)
        return Stress90StateRecord(state=state, sequence=sequence, checksum=checksum)

    def save(
        self,
        state: Stress90PolicyState,
        *,
        expected_sequence: int | None = None,
    ) -> Stress90StateRecord:
        with durable_file_lock(self.path):
            return self._save_locked(state, expected_sequence=expected_sequence)

    def _save_locked(
        self,
        state: Stress90PolicyState,
        *,
        expected_sequence: int | None,
    ) -> Stress90StateRecord:
        _validate_state(state, self.definition)
        self._require_fresh_or_current()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        previous_bytes: bytes | None = None
        current = self.load_record_unlocked()
        current_sequence = 0 if current is None else current.sequence
        if expected_sequence is not None and expected_sequence != current_sequence:
            raise Stress90StateIntegrityError("Stress-90 state sequence changed concurrently")
        if current is not None:
            previous_bytes = self.path.read_bytes()
        sequence = current_sequence + 1
        unsigned: dict[str, object] = {
            "kind": STRESS90_STATE_KIND,
            "schema_version": STRESS90_STATE_SCHEMA_VERSION,
            "sequence": sequence,
            "state": _state_payload(state),
        }
        checksum = _checksum(unsigned)
        encoded = json.dumps(
            {**unsigned, "checksum": checksum},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        if previous_bytes is not None:
            self._atomic_replace(self.previous_path, previous_bytes)
        self._atomic_replace(self.path, encoded)
        return Stress90StateRecord(state=state, sequence=sequence, checksum=checksum)

    def save_new(
        self,
        state: Stress90PolicyState,
    ) -> tuple[Stress90StateRecord, DurableFileCreationToken]:
        """Create sequence 1 without replacing a concurrent policy writer."""

        _validate_state(state, self.definition)
        self._require_fresh_or_current()
        unsigned: dict[str, object] = {
            "kind": STRESS90_STATE_KIND,
            "schema_version": STRESS90_STATE_SCHEMA_VERSION,
            "sequence": 1,
            "state": _state_payload(state),
        }
        checksum = _checksum(unsigned)
        encoded = json.dumps(
            {**unsigned, "checksum": checksum},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        try:
            token = create_durable_file_exclusive(self.path, encoded)
        except FileExistsError as exc:
            raise Stress90StateIntegrityError(
                "Stress-90 policy state appeared concurrently"
            ) from exc
        self._last_creation_token = token
        return Stress90StateRecord(state=state, sequence=1, checksum=checksum), token

    @staticmethod
    def _atomic_replace(target: Path, payload: bytes) -> None:
        temporary: Path | None = None
        try:
            with NamedTemporaryFile("wb", dir=target.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(target)
            directory_descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()


_SEED_FIELDS = {
    "policy_id",
    "policy_definition_digest",
    "products_manifest_digest",
    "supported_oi_products",
    "bootstrap_source_manifest",
    "bootstrap_through_day",
    "last_completed_input_day",
    "last_completed_target_day",
    "last_decision_digest",
    "last_oi_confirmed_weights",
    "last_cost_approved_weights",
    "last_survivor_weights",
    "completed_concentrations",
    "bootstrap_candidate_state_digest",
    "historical_candidate_weight_sha256",
    "seed_digest",
}


def _seed_payload(seed: Stress90BootstrapSeed) -> dict[str, object]:
    return {
        "policy_id": seed.policy_id,
        "policy_definition_digest": seed.policy_definition_digest,
        "products_manifest_digest": seed.products_manifest_digest,
        "supported_oi_products": list(seed.supported_oi_products),
        "bootstrap_source_manifest": dict(seed.bootstrap_source_manifest),
        "bootstrap_through_day": seed.bootstrap_through_day,
        "last_completed_input_day": seed.last_completed_input_day,
        "last_completed_target_day": seed.last_completed_target_day,
        "last_decision_digest": seed.last_decision_digest,
        "last_oi_confirmed_weights": dict(seed.last_oi_confirmed_weights),
        "last_cost_approved_weights": dict(seed.last_cost_approved_weights),
        "last_survivor_weights": dict(seed.last_survivor_weights),
        "completed_concentrations": list(seed.completed_concentrations),
        "bootstrap_candidate_state_digest": seed.bootstrap_candidate_state_digest,
        "historical_candidate_weight_sha256": seed.historical_candidate_weight_sha256,
        "seed_digest": seed.seed_digest,
    }


def _validate_seed(
    seed: Stress90BootstrapSeed,
    definition: Stress90PolicyDefinition = STRESS90_POLICY,
) -> None:
    if not isinstance(seed, Stress90BootstrapSeed):
        raise Stress90StateIntegrityError("Stress-90 bootstrap seed has invalid type")
    if seed.policy_id != definition.policy_id:
        raise Stress90StateIntegrityError("bootstrap seed policy id mismatch")
    if seed.policy_definition_digest != definition.policy_definition_digest:
        raise Stress90StateIntegrityError("bootstrap seed policy definition digest mismatch")
    if seed.products_manifest_digest != definition.products_manifest_digest:
        raise Stress90StateIntegrityError("bootstrap seed products manifest digest mismatch")
    if seed.supported_oi_products != definition.oi_products:
        raise Stress90StateIntegrityError("bootstrap seed supported OI products mismatch")
    sources = _valid_source_manifest(seed.bootstrap_source_manifest)
    through = _valid_day(seed.bootstrap_through_day, name="bootstrap seed through day")
    input_day = _valid_day(seed.last_completed_input_day, name="bootstrap seed input day")
    target_day = _valid_day(seed.last_completed_target_day, name="bootstrap seed target day")
    if (
        through is None
        or input_day is None
        or target_day is None
        or through != target_day
        or input_day >= target_day
    ):
        raise Stress90StateIntegrityError("bootstrap seed day ordering is invalid")
    decision_digest = _valid_sha256(
        seed.last_decision_digest,
        name="bootstrap seed last decision digest",
    )
    oi = _valid_weights(
        seed.last_oi_confirmed_weights,
        name="bootstrap seed OI-confirmed",
        definition=definition,
    )
    cost = _valid_weights(
        seed.last_cost_approved_weights,
        name="bootstrap seed cost-approved",
        definition=definition,
    )
    survivor = _valid_weights(
        seed.last_survivor_weights,
        name="bootstrap seed survivor",
        definition=definition,
    )
    concentrations = _valid_concentrations(seed.completed_concentrations)
    candidate = Stress90CandidateState(
        policy_definition_digest=seed.policy_definition_digest,
        products_manifest_digest=seed.products_manifest_digest,
        last_completed_target_day=target_day,
        last_decision_digest=decision_digest,
        last_oi_confirmed_weights=oi,
        last_cost_approved_weights=cost,
        last_survivor_weights=survivor,
        completed_concentrations=concentrations,
    )
    expected_candidate_digest = candidate_state_digest(candidate)
    if seed.bootstrap_candidate_state_digest != expected_candidate_digest:
        raise Stress90StateIntegrityError("bootstrap seed candidate state digest mismatch")
    if seed.historical_candidate_weight_sha256 != definition.historical_candidate_weight_sha256:
        raise Stress90StateIntegrityError("bootstrap seed historical candidate digest mismatch")
    expected_seed_digest = _bootstrap_identity_digest(
        policy_id=seed.policy_id,
        policy_definition_digest=seed.policy_definition_digest,
        products_manifest_digest=seed.products_manifest_digest,
        supported_oi_products=seed.supported_oi_products,
        bootstrap_source_manifest=sources,
        bootstrap_through_day=through,
        bootstrap_candidate_state_digest=expected_candidate_digest,
        historical_candidate_weight_sha256=seed.historical_candidate_weight_sha256,
    )
    if seed.seed_digest != expected_seed_digest:
        raise Stress90StateIntegrityError("bootstrap seed digest mismatch")


def _seed_from_payload(
    raw: object,
    definition: Stress90PolicyDefinition,
) -> Stress90BootstrapSeed:
    if not isinstance(raw, Mapping) or set(raw) != _SEED_FIELDS:
        raise Stress90StateIntegrityError("Stress-90 bootstrap seed fields are invalid")
    supported = raw["supported_oi_products"]
    if not isinstance(supported, list) or any(not isinstance(value, str) for value in supported):
        raise Stress90StateIntegrityError("bootstrap seed supported OI products are invalid")
    seed = Stress90BootstrapSeed(
        policy_id=str(raw["policy_id"]),
        policy_definition_digest=str(raw["policy_definition_digest"]),
        products_manifest_digest=str(raw["products_manifest_digest"]),
        supported_oi_products=tuple(supported),
        bootstrap_source_manifest=_valid_source_manifest(raw["bootstrap_source_manifest"]),
        bootstrap_through_day=str(raw["bootstrap_through_day"]),
        last_completed_input_day=str(raw["last_completed_input_day"]),
        last_completed_target_day=str(raw["last_completed_target_day"]),
        last_decision_digest=str(raw["last_decision_digest"]),
        last_oi_confirmed_weights=_valid_weights(
            raw["last_oi_confirmed_weights"],
            name="bootstrap seed OI-confirmed",
            definition=definition,
        ),
        last_cost_approved_weights=_valid_weights(
            raw["last_cost_approved_weights"],
            name="bootstrap seed cost-approved",
            definition=definition,
        ),
        last_survivor_weights=_valid_weights(
            raw["last_survivor_weights"],
            name="bootstrap seed survivor",
            definition=definition,
        ),
        completed_concentrations=_valid_concentrations(raw["completed_concentrations"]),
        bootstrap_candidate_state_digest=str(raw["bootstrap_candidate_state_digest"]),
        historical_candidate_weight_sha256=str(raw["historical_candidate_weight_sha256"]),
        seed_digest=str(raw["seed_digest"]),
    )
    _validate_seed(seed, definition)
    return seed


class Stress90SeedStore:
    """Write-once immutable bootstrap seed envelope."""

    def __init__(
        self,
        path: str | Path,
        *,
        definition: Stress90PolicyDefinition = STRESS90_POLICY,
    ) -> None:
        self.path = canonical_file_path(path)
        self.definition = definition
        self._last_creation_token: DurableFileCreationToken | None = None

    @property
    def last_creation_token(self) -> DurableFileCreationToken | None:
        return self._last_creation_token

    def save_new(self, seed: Stress90BootstrapSeed) -> DurableFileCreationToken:
        _validate_seed(seed, self.definition)
        unsigned: dict[str, object] = {
            "kind": STRESS90_SEED_KIND,
            "schema_version": STRESS90_SEED_SCHEMA_VERSION,
            "seed": _seed_payload(seed),
        }
        encoded = json.dumps(
            {**unsigned, "checksum": _checksum(unsigned)},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            token = create_durable_file_exclusive(self.path, encoded)
        except FileExistsError as exc:
            raise Stress90StateIntegrityError("Stress-90 bootstrap seed is immutable") from exc
        self._last_creation_token = token
        return token

    def load_required(
        self,
        *,
        expected_source_manifest: Mapping[str, str] | None = None,
    ) -> Stress90BootstrapSeed:
        with durable_file_lock(self.path):
            return self.load_required_unlocked(expected_source_manifest=expected_source_manifest)

    def load_required_unlocked(
        self,
        *,
        expected_source_manifest: Mapping[str, str] | None = None,
    ) -> Stress90BootstrapSeed:
        """Decode seed while the caller already holds this artifact lock."""

        if not self.path.exists():
            raise Stress90StateIntegrityError("required Stress-90 bootstrap seed is missing")
        try:
            text = self.path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Stress90StateIntegrityError("invalid Stress-90 bootstrap seed UTF-8") from exc
        try:
            raw = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
        except json.JSONDecodeError as exc:
            raise Stress90StateIntegrityError("invalid Stress-90 bootstrap seed JSON") from exc
        if not isinstance(raw, Mapping) or set(raw) != {
            "kind",
            "schema_version",
            "seed",
            "checksum",
        }:
            raise Stress90StateIntegrityError("Stress-90 bootstrap seed envelope is invalid")
        if raw["kind"] != STRESS90_SEED_KIND:
            raise Stress90StateIntegrityError("Stress-90 bootstrap seed kind is invalid")
        if raw["schema_version"] != STRESS90_SEED_SCHEMA_VERSION:
            raise Stress90StateIntegrityError("Stress-90 bootstrap seed schema is unsupported")
        checksum = raw["checksum"]
        _valid_sha256(checksum, name="Stress-90 bootstrap seed checksum")
        unsigned = {key: value for key, value in raw.items() if key != "checksum"}
        if checksum != _checksum(unsigned):
            raise Stress90StateIntegrityError("Stress-90 bootstrap seed checksum mismatch")
        seed = _seed_from_payload(raw["seed"], self.definition)
        if expected_source_manifest is not None:
            expected = _valid_source_manifest(expected_source_manifest)
            if seed.bootstrap_source_manifest != expected:
                raise Stress90StateIntegrityError("bootstrap seed source manifest mismatch")
        return seed


def prepare_stress90_decision(
    store: Stress90PolicyStateStore,
    inputs: Stress90DecisionInputs,
) -> Stress90PreparedDecision:
    """Persist one target transition before returning it to any order-capable caller."""

    record = store.load_required_record()
    state = record.state
    source_digest = _decision_inputs_digest(inputs, store.definition)
    target = _valid_day(inputs.target_trading_day, name="target trading day")
    previous = _valid_day(
        inputs.previous_target_trading_day,
        name="previous target trading day",
    )
    prepared = state.prepared_decision
    if prepared is not None and prepared.target_trading_day == target:
        if prepared.source_input_digest != source_digest:
            raise Stress90StateIntegrityError("same-day input digest changed")
        return prepared
    if previous != state.last_completed_target_day:
        raise Stress90StateIntegrityError("target-day gap before Stress-90 decision")
    if target is None or target <= state.last_completed_target_day:
        raise Stress90StateIntegrityError("Stress-90 target day did not advance")

    decision = step_stress90_candidate(
        prior_state=state.candidate_state(),
        target_trading_day=inputs.target_trading_day,
        base_weights=inputs.base_weights,
        completed_close_history=inputs.completed_close_history,
        completed_oi_flow=inputs.completed_oi_flow,
        completed_close_day=inputs.completed_close_day,
        completed_oi_day=inputs.completed_oi_day,
        definition=store.definition,
    )
    prepared = Stress90PreparedDecision.from_decision(
        decision,
        previous_target_trading_day=inputs.previous_target_trading_day,
        source_input_digest=source_digest,
    )
    post = decision.post_state
    next_state = replace(
        state,
        last_completed_input_day=str(decision.input_days["completed_close"]),
        last_completed_target_day=post.last_completed_target_day or "",
        last_decision_digest=post.last_decision_digest or "",
        last_oi_confirmed_weights=post.last_oi_confirmed_weights,
        last_cost_approved_weights=post.last_cost_approved_weights,
        last_survivor_weights=post.last_survivor_weights,
        completed_concentrations=post.completed_concentrations,
        prepared_decision=prepared,
    )
    store.save(next_state, expected_sequence=record.sequence)
    return prepared


def completed_account_drawdown(state: Stress90PolicyState) -> float:
    """Return positive completed-path drawdown without consulting current-day PnL."""

    _validate_state(state)
    return float(1.0 - state.completed_account_wealth / state.completed_account_high_watermark)


def drawdown_reserve_triggered_from_state(
    state: Stress90PolicyState,
    *,
    definition: Stress90PolicyDefinition = STRESS90_POLICY,
) -> bool:
    """Trigger at the fixed 30%-5%=25% completed-account reserve boundary."""

    _validate_state(state, definition)
    reserve = definition.hard_drawdown_ratio - definition.daily_loss_ratio
    return completed_account_drawdown(state) >= reserve - _WEIGHT_EPS


def record_completed_account_day(
    state: Stress90PolicyState,
    trading_day: str,
    daily_return: float,
    *,
    include_adaptive_margin: bool = True,
    definition: Stress90PolicyDefinition = STRESS90_POLICY,
) -> Stress90PolicyState:
    """Advance normalized completed wealth/HWM exactly once for one Broker day."""

    _validate_state(state, definition)
    day = _valid_day(trading_day, name="completed account trading day")
    if day is None:  # pragma: no cover - required by _valid_day
        raise Stress90StateIntegrityError("completed account trading day is missing")
    if state.last_completed_account_day is not None and day <= state.last_completed_account_day:
        raise Stress90StateIntegrityError("completed account days must strictly advance")
    if (
        isinstance(daily_return, bool)
        or not isinstance(daily_return, (int, float))
        or not isfinite(daily_return)
        or float(daily_return) <= -1.0
    ):
        raise Stress90StateIntegrityError(
            "completed account return must be finite and greater than -100%"
        )
    value = float(daily_return)
    wealth = state.completed_account_wealth * (1.0 + value)
    if not isfinite(wealth) or wealth <= 0.0:
        raise Stress90StateIntegrityError("completed account wealth became invalid")
    high_watermark = max(state.completed_account_high_watermark, wealth)
    if not isinstance(include_adaptive_margin, bool):
        raise Stress90StateIntegrityError("adaptive-margin inclusion flag must be boolean")
    recent = state.recent_daily_returns_for_adaptive_margin
    if include_adaptive_margin:
        recent = (*recent, value)[-2:]
    updated = replace(
        state,
        completed_account_wealth=float(wealth),
        completed_account_high_watermark=float(high_watermark),
        last_completed_account_day=day,
        recent_daily_returns_for_adaptive_margin=tuple(recent),
        live_inception_day=state.live_inception_day or day,
        live_inception_equity=state.live_inception_equity or 1.0,
    )
    _validate_state(updated, definition)
    return updated


def bind_stress90_account_identity(
    state: Stress90PolicyState,
    account_identity_digest: str,
    *,
    account_epoch: str | None = None,
    allow_rebind: bool = False,
    definition: Stress90PolicyDefinition = STRESS90_POLICY,
) -> Stress90PolicyState:
    """Bind account soft-path state to a non-secret Broker identity."""

    _validate_state(state, definition)
    digest = _valid_sha256(account_identity_digest, name="live account identity digest")
    existing = state.live_account_identity_digest
    if existing is not None and existing != digest and not allow_rebind:
        raise Stress90StateIntegrityError(
            "Stress-90 account identity mismatch; explicit rebase is required"
        )
    epoch = state.live_account_epoch
    if account_epoch is not None:
        epoch = _valid_sha256(account_epoch, name="live account epoch")
    if epoch is None:
        raise Stress90StateIntegrityError(
            "Stress-90 account binding requires an explicit lifecycle epoch"
        )
    updated = replace(
        state,
        live_account_identity_digest=digest,
        live_account_epoch=epoch,
    )
    _validate_state(updated, definition)
    return updated


@dataclass(frozen=True)
class Stress90AccountRebaseAudit:
    account_trading_day: str
    account_equity: float
    operator_reason: str
    old_completed_account_wealth: float
    old_completed_account_high_watermark: float
    old_last_completed_account_day: str | None


def rebase_stress90_account(
    state: Stress90PolicyState,
    *,
    account_trading_day: str,
    account_equity: float,
    operator_reason: str,
    halted: bool,
    broker_flat: bool,
    local_flat: bool,
    no_active_orders: bool,
    reconciled: bool,
    strong_confirmation: str,
    account_identity_digest: str | None = None,
    account_epoch: str | None = None,
    definition: Stress90PolicyDefinition = STRESS90_POLICY,
) -> tuple[Stress90PolicyState, Stress90AccountRebaseAudit]:
    """Reset only account soft-path statistics after explicit lifecycle gates pass."""

    _validate_state(state, definition)
    gates = (halted, broker_flat, local_flat, no_active_orders, reconciled)
    if any(value is not True for value in gates):
        raise Stress90StateIntegrityError("Stress-90 account rebase lifecycle gate failed")
    if strong_confirmation != REBASE_CONFIRMATION:
        raise Stress90StateIntegrityError("Stress-90 account rebase confirmation is invalid")
    if not isinstance(operator_reason, str) or not operator_reason.strip():
        raise Stress90StateIntegrityError("Stress-90 account rebase reason is required")
    day = _valid_day(account_trading_day, name="Stress-90 account rebase trading day")
    if day is None:  # pragma: no cover - required by _valid_day
        raise Stress90StateIntegrityError("Stress-90 account rebase trading day is missing")
    prior_account_days = tuple(
        prior
        for prior in (
            state.last_completed_target_day,
            state.last_completed_account_day,
            state.live_inception_day,
        )
        if prior is not None
    )
    if any(day < prior for prior in prior_account_days):
        raise Stress90StateIntegrityError("Stress-90 account rebase trading day moved backward")
    if (
        isinstance(account_equity, bool)
        or not isinstance(account_equity, (int, float))
        or not isfinite(account_equity)
        or float(account_equity) <= 0.0
    ):
        raise Stress90StateIntegrityError("Stress-90 account rebase equity is invalid")
    audit = Stress90AccountRebaseAudit(
        account_trading_day=day,
        account_equity=float(account_equity),
        operator_reason=operator_reason.strip(),
        old_completed_account_wealth=state.completed_account_wealth,
        old_completed_account_high_watermark=state.completed_account_high_watermark,
        old_last_completed_account_day=state.last_completed_account_day,
    )
    updated = replace(
        state,
        completed_account_wealth=1.0,
        completed_account_high_watermark=1.0,
        last_completed_account_day=None,
        recent_daily_returns_for_adaptive_margin=(),
        live_inception_day=day,
        live_inception_equity=float(account_equity),
    )
    if account_identity_digest is not None:
        updated = bind_stress90_account_identity(
            updated,
            account_identity_digest,
            account_epoch=account_epoch,
            allow_rebind=True,
            definition=definition,
        )
    _validate_state(updated, definition)
    return updated, audit
