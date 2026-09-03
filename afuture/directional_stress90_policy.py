"""Deterministic production primitives for the frozen Stress-90 candidate."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from math import isfinite
from statistics import median
from types import MappingProxyType
from typing import TypeAlias

import numpy as np
import pandas as pd

from .directional_sessions import SESSION_MANIFEST_DIGEST
from .execution_aligned_policy import (
    BASE_COST_BPS,
    EXECUTION_TEMPLATE_IDS,
    FROZEN_PRODUCTS,
    MAX_GROSS_LEVERAGE,
    META_COUNT,
    META_LOOKBACK,
    META_REBALANCE,
    STRESS_COST_BPS,
)

EXPECTED_CANDIDATE_WEIGHT_SHA256 = (
    "8e38dbf6441b561dd1728df08665b94b15cc3358823257505c2fcb9d63f09f28"
)
SUPPORTED_OI_PRODUCTS = ("A", "C", "EG", "I", "M", "P", "PP", "TA", "Y")

_OI_EPS = 1e-15
_NUMERIC_EPS = 1e-12
ProductWeights: TypeAlias = Mapping[str, float]


def _canonical_digest(payload: object) -> str:
    """Hash a JSON-compatible policy payload without decimal rendering ambiguity."""

    def normalize(value: object) -> object:
        if isinstance(value, float):
            return {"float_hex": value.hex()}
        if isinstance(value, Mapping):
            return {str(key): normalize(item) for key, item in sorted(value.items())}
        if isinstance(value, (list, tuple)):
            return [normalize(item) for item in value]
        return value

    encoded = json.dumps(
        normalize(payload),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_stress90_digest(payload: object) -> str:
    """Public canonical digest used by strict Stress-90 state/evidence envelopes."""

    return _canonical_digest(payload)


@dataclass(frozen=True)
class Stress90PolicyDefinition:
    """Immutable economic and hard-risk identity of the Stress-90 policy."""

    policy_id: str = "directional.stress90"
    definition_version: int = 1
    products: tuple[str, ...] = FROZEN_PRODUCTS
    oi_products: tuple[str, ...] = SUPPORTED_OI_PRODUCTS
    template_ids: tuple[str, ...] = EXECUTION_TEMPLATE_IDS
    meta_lookback: int = META_LOOKBACK
    meta_rebalance: int = META_REBALANCE
    meta_count: int = META_COUNT
    base_cost_bps: float = BASE_COST_BPS
    stress_cost_bps: float = STRESS_COST_BPS
    completed_lookback_sessions: int = 20
    benefit_horizon_sessions: int = 3
    cost_hurdle_bps: float = 15.0
    hhi_rule: str = "abs_weight_hhi_le_strictly_prior_expanding_median"
    session_manifest_digest: str = SESSION_MANIFEST_DIGEST
    hard_drawdown_ratio: float = 0.30
    daily_loss_ratio: float = 0.05
    max_gross_leverage: float = MAX_GROSS_LEVERAGE
    max_margin_ratio: float = 0.35
    min_available_ratio: float = 0.25
    max_contract_lots: int = 35
    margin_estimate_buffer: float = 1.25
    historical_candidate_weight_sha256: str = EXPECTED_CANDIDATE_WEIGHT_SHA256

    def __post_init__(self) -> None:
        expected = {
            "policy_id": "directional.stress90",
            "definition_version": 1,
            "products": FROZEN_PRODUCTS,
            "oi_products": SUPPORTED_OI_PRODUCTS,
            "template_ids": EXECUTION_TEMPLATE_IDS,
            "meta_lookback": META_LOOKBACK,
            "meta_rebalance": META_REBALANCE,
            "meta_count": META_COUNT,
            "base_cost_bps": BASE_COST_BPS,
            "stress_cost_bps": STRESS_COST_BPS,
            "completed_lookback_sessions": 20,
            "benefit_horizon_sessions": 3,
            "cost_hurdle_bps": 15.0,
            "hhi_rule": "abs_weight_hhi_le_strictly_prior_expanding_median",
            "session_manifest_digest": SESSION_MANIFEST_DIGEST,
            "hard_drawdown_ratio": 0.30,
            "daily_loss_ratio": 0.05,
            "max_gross_leverage": MAX_GROSS_LEVERAGE,
            "max_margin_ratio": 0.35,
            "min_available_ratio": 0.25,
            "max_contract_lots": 35,
            "margin_estimate_buffer": 1.25,
            "historical_candidate_weight_sha256": EXPECTED_CANDIDATE_WEIGHT_SHA256,
        }
        if any(getattr(self, name) != value for name, value in expected.items()):
            raise ValueError("Stress-90 policy definition is frozen")

    @property
    def drawdown_reserve_ratio(self) -> float:
        return self.hard_drawdown_ratio - self.daily_loss_ratio

    @property
    def hard_risk_envelope(self) -> Mapping[str, float | int]:
        return MappingProxyType(
            {
                "max_target_gross": self.max_gross_leverage,
                "max_realized_gross": self.max_gross_leverage,
                "max_margin_ratio": self.max_margin_ratio,
                "min_available_ratio": self.min_available_ratio,
                "max_daily_loss_ratio": self.daily_loss_ratio,
                "max_total_drawdown_ratio": self.hard_drawdown_ratio,
                "max_contract_lots": self.max_contract_lots,
                "margin_estimate_buffer": self.margin_estimate_buffer,
            }
        )

    @property
    def products_manifest_digest(self) -> str:
        return _canonical_digest(self.products)

    @property
    def policy_manifest_digest(self) -> str:
        return _canonical_digest(
            {
                "products": self.products,
                "oi_products": self.oi_products,
                "template_ids": self.template_ids,
            }
        )

    @property
    def policy_definition_digest(self) -> str:
        return _canonical_digest(asdict(self))


STRESS90_POLICY = Stress90PolicyDefinition()


class Stress90PolicyError(ValueError):
    """Base class for deterministic Stress-90 policy failures."""


class Stress90InputIncomplete(Stress90PolicyError):
    """Required completed market evidence is absent or incomplete."""


class Stress90InvariantError(Stress90PolicyError):
    """A policy identity, support, sign, finite-value, or gross invariant failed."""


def _normalized_values(
    raw: Mapping[str, float],
    *,
    name: str,
    expected_products: Sequence[str] | None = None,
) -> dict[str, float]:
    result: dict[str, float] = {}
    for raw_product, raw_value in raw.items():
        product = str(raw_product).upper()
        if product in result:
            raise Stress90InvariantError(f"{name} contains duplicate product: {product}")
        value = float(raw_value)
        if not isfinite(value):
            raise Stress90InvariantError(f"{name} weights must be finite")
        result[product] = value
    if expected_products is not None:
        expected = tuple(str(product).upper() for product in expected_products)
        if set(result) != set(expected):
            missing = sorted(set(expected) - set(result))
            extra = sorted(set(result) - set(expected))
            raise Stress90InvariantError(
                f"{name} product manifest mismatch; missing={missing}, extra={extra}"
            )
        result = {product: result[product] for product in expected}
    return result


def _frozen_weights(values: Mapping[str, float]) -> ProductWeights:
    return MappingProxyType({str(product): float(value) for product, value in values.items()})


def _gross(weights: Mapping[str, float]) -> float:
    return float(sum(abs(float(value)) for value in weights.values()))


def _require_gross_cap(
    weights: Mapping[str, float],
    *,
    name: str,
    cap: float = MAX_GROSS_LEVERAGE,
) -> None:
    if _gross(weights) > float(cap) + _NUMERIC_EPS:
        raise Stress90InvariantError(f"{name} exceeds 2x gross")


def apply_oi_confirmation_row(
    *,
    raw_weights: Mapping[str, float],
    prior_applied: Mapping[str, float],
    completed_flow: Mapping[str, int | float | None],
    supported_products: Collection[str],
) -> dict[str, float]:
    """Apply the fixed OI entry/add/reversal confirmation to one target row."""

    raw = _normalized_values(raw_weights, name="raw OI")
    prior = _normalized_values(prior_applied, name="prior OI")
    unknown_prior = sorted(set(prior) - set(raw))
    if unknown_prior:
        raise Stress90InvariantError(f"prior OI contains new product support: {unknown_prior}")
    supported = {str(product).upper() for product in supported_products}

    output: dict[str, float] = {}
    for product, target in raw.items():
        current = float(prior.get(product, 0.0))
        same_side = (
            abs(current) > _OI_EPS and abs(target) > _OI_EPS and (target > 0.0) == (current > 0.0)
        )
        needs_confirmation = product in supported and (
            (abs(current) <= _OI_EPS and abs(target) > _OI_EPS)
            or ((target > 0.0) != (current > 0.0) and abs(target) > _OI_EPS)
            or (same_side and abs(target) > abs(current) + _OI_EPS)
        )
        raw_flow = completed_flow.get(product)
        if needs_confirmation and (
            raw_flow is None
            or (isinstance(raw_flow, (float, np.floating)) and not isfinite(float(raw_flow)))
        ):
            raise Stress90InputIncomplete(f"missing completed OI flow: {product}")
        if raw_flow is not None and not (
            isinstance(raw_flow, (float, np.floating)) and not isfinite(float(raw_flow))
        ):
            if isinstance(raw_flow, bool) or raw_flow not in (-1, 0, 1):
                raise Stress90InvariantError(f"invalid completed OI flow for {product}: {raw_flow}")
            flow = int(raw_flow)
        else:
            flow = 0

        if product not in supported:
            applied = target
        elif abs(target) <= _OI_EPS:
            applied = 0.0
        elif abs(current) <= _OI_EPS:
            applied = target if flow == (1 if target > 0.0 else -1) else 0.0
        elif (target > 0.0) != (current > 0.0):
            applied = target if flow == (1 if target > 0.0 else -1) else 0.0
        elif abs(target) > abs(current) + _OI_EPS:
            applied = target if flow == (1 if target > 0.0 else -1) else current
        else:
            applied = target
        if abs(applied) > abs(target) + _OI_EPS:
            raise Stress90InvariantError("OI confirmation increased product exposure")
        output[product] = float(applied)

    if _gross(output) > _gross(raw) + _NUMERIC_EPS:
        raise Stress90InvariantError("OI confirmation increased raw gross")
    return output


def _completed_return_sum(
    close_values: Sequence[float],
    *,
    lookback: int,
) -> float | None:
    values = tuple(float(value) for value in close_values)
    if len(values) < lookback + 1:
        return None
    selected = values[-(lookback + 1) :]
    if any(not isfinite(value) or value <= 0.0 for value in selected):
        return None
    return float(
        sum(selected[index] / selected[index - 1] - 1.0 for index in range(1, len(selected)))
    )


def apply_cost_gate_row(
    *,
    oi_weights: Mapping[str, float],
    prior_approved: Mapping[str, float],
    completed_close_history: Mapping[str, Sequence[float]],
    completed_lookback_sessions: int = 20,
    benefit_horizon_sessions: int = 3,
    cost_hurdle_bps: float = 15.0,
) -> dict[str, float]:
    """Use completed close returns to block only entries and same-side adds."""

    weights = _normalized_values(oi_weights, name="OI-confirmed")
    prior = _normalized_values(prior_approved, name="prior cost-approved")
    unknown_prior = sorted(set(prior) - set(weights))
    if unknown_prior:
        raise Stress90InvariantError(
            f"prior cost-approved contains new product support: {unknown_prior}"
        )
    missing = sorted(set(weights) - {str(product).upper() for product in completed_close_history})
    if missing:
        raise Stress90InputIncomplete(f"completed close history missing products: {missing}")
    if completed_lookback_sessions <= 0 or benefit_horizon_sessions <= 0:
        raise Stress90InvariantError("cost gate session counts must be positive")
    hurdle = float(cost_hurdle_bps) / 10_000.0
    if not isfinite(hurdle) or hurdle <= 0.0:
        raise Stress90InvariantError("cost hurdle must be finite and positive")

    normalized_history = {
        str(product).upper(): values for product, values in completed_close_history.items()
    }
    trends = {
        product: _completed_return_sum(
            normalized_history[product],
            lookback=completed_lookback_sessions,
        )
        for product in weights
    }
    return _apply_cost_gate_from_trends_row(
        oi_weights=weights,
        prior_approved=prior,
        completed_return_sums=trends,
        benefit_horizon_sessions=benefit_horizon_sessions,
        completed_lookback_sessions=completed_lookback_sessions,
        cost_hurdle_bps=hurdle * 10_000.0,
    )


def _apply_cost_gate_from_trends_row(
    *,
    oi_weights: Mapping[str, float],
    prior_approved: Mapping[str, float],
    completed_return_sums: Mapping[str, float | None],
    completed_lookback_sessions: int = 20,
    benefit_horizon_sessions: int = 3,
    cost_hurdle_bps: float = 15.0,
) -> dict[str, float]:
    """Shared gate once completed arithmetic return sums have been calculated."""

    weights = _normalized_values(oi_weights, name="OI-confirmed")
    prior = _normalized_values(prior_approved, name="prior cost-approved")
    hurdle = float(cost_hurdle_bps) / 10_000.0
    horizon_scale = float(benefit_horizon_sessions) / float(completed_lookback_sessions)
    output: dict[str, float] = {}
    for product, target in weights.items():
        current = float(prior.get(product, 0.0))
        same_side = (
            abs(current) > _NUMERIC_EPS
            and abs(target) > _NUMERIC_EPS
            and (current > 0.0) == (target > 0.0)
        )
        increase = (abs(current) <= _NUMERIC_EPS and abs(target) > _NUMERIC_EPS) or (
            same_side and abs(target) > abs(current) + _NUMERIC_EPS
        )
        applied = target
        if increase:
            raw_trend = completed_return_sums.get(product)
            trend = float(raw_trend) if raw_trend is not None else None
            benefit = (
                (1.0 if target > 0.0 else -1.0) * trend * horizon_scale
                if trend is not None and isfinite(trend)
                else None
            )
            if benefit is None or benefit <= hurdle + _NUMERIC_EPS:
                applied = current
        output[product] = float(applied)

    _require_gross_cap(output, name="cost-approved")
    if _gross(output) > _gross(weights) + _NUMERIC_EPS:
        raise Stress90InvariantError("cost gate increased OI-confirmed gross")
    return output


def reallocate_survivor_row(
    *,
    oi_weights: Mapping[str, float],
    approved_weights: Mapping[str, float],
    prior_survivor: Mapping[str, float],
) -> dict[str, float]:
    """Restore OI gross on eligible support using the frozen lexicographic tie-break."""

    original = _normalized_values(oi_weights, name="OI-confirmed")
    approved = _normalized_values(approved_weights, name="cost-approved")
    previous = _normalized_values(prior_survivor, name="prior survivor")
    if set(approved) != set(original):
        raise Stress90InvariantError("cost-approved product manifest mismatch")
    if set(previous) - set(original):
        raise Stress90InvariantError("prior survivor product manifest mismatch")
    _require_gross_cap(original, name="OI-confirmed")
    if _gross(approved) > _gross(original) + _NUMERIC_EPS:
        raise Stress90InvariantError("cost-approved gross exceeds OI-confirmed gross")

    products = tuple(original)
    original_row = np.asarray([original[product] for product in products], dtype=float)
    approved_row = np.asarray([approved[product] for product in products], dtype=float)
    previous_row = np.asarray([previous.get(product, 0.0) for product in products], dtype=float)
    support = np.abs(approved_row) > _NUMERIC_EPS
    for index, _product in enumerate(products):
        if not support[index]:
            continue
        if abs(original_row[index]) <= _NUMERIC_EPS:
            raise Stress90InvariantError("cost-approved weights created new support")
        if np.sign(approved_row[index]) != np.sign(original_row[index]):
            raise Stress90InvariantError("cost-approved weights changed target sign")

    original_gross = float(np.abs(original_row).sum())
    if original_gross <= _NUMERIC_EPS or not bool(support.any()):
        return {product: 0.0 for product in products}

    signs = np.sign(original_row)
    base = np.where(support, np.abs(original_row), 0.0)
    residual = max(original_gross - float(base.sum()), 0.0)
    magnitudes = base.copy()
    same_sign_previous = support & (np.sign(previous_row) == signs)
    capacity = np.where(
        same_sign_previous,
        np.maximum(np.abs(previous_row) - base, 0.0),
        0.0,
    )
    capacity_total = float(capacity.sum())
    if residual > _NUMERIC_EPS and capacity_total > _NUMERIC_EPS:
        used = min(residual, capacity_total)
        magnitudes += capacity * (used / capacity_total)
        residual -= used

    if residual > _NUMERIC_EPS:
        denominator = float(base.sum())
        if denominator <= _NUMERIC_EPS:
            raise Stress90InvariantError("survivor support has no original magnitude")
        magnitudes += base * (residual / denominator)

    applied = np.where(support, signs * magnitudes, 0.0)
    output = {product: float(applied[index]) for index, product in enumerate(products)}
    if abs(_gross(output) - original_gross) > 1e-10:
        raise Stress90InvariantError("survivor reallocation failed to restore OI gross")
    _require_gross_cap(output, name="survivor")
    return output


def target_weight_concentration(weights: Mapping[str, float]) -> float | None:
    """Return standard HHI over normalized absolute product weights."""

    values = _normalized_values(weights, name="target concentration")
    magnitudes = [abs(value) for value in values.values() if value != 0.0]
    gross = float(sum(magnitudes))
    if gross <= 0.0:
        return None
    return float(sum((value / gross) ** 2 for value in magnitudes))


def advance_concentration_history(
    completed_concentrations: Sequence[float],
    weights: Mapping[str, float],
) -> tuple[float | None, float | None, bool, tuple[float, ...]]:
    """Compare with strictly-prior HHI median, then append the current HHI."""

    history = tuple(float(value) for value in completed_concentrations)
    if any(not isfinite(value) or not 0.0 < value <= 1.0 for value in history):
        raise Stress90InvariantError("completed concentrations must be finite in (0, 1]")
    current = target_weight_concentration(weights)
    prior_median = float(median(history)) if history else None
    if current is None:
        return None, prior_median, False, history
    freeze = bool(prior_median is not None and current <= prior_median)
    return current, prior_median, freeze, (*history, current)


@dataclass(frozen=True)
class Stress90CandidateState:
    """Immutable incremental signal state, independent of Broker and account state."""

    policy_definition_digest: str
    products_manifest_digest: str
    last_completed_target_day: str | None
    last_decision_digest: str | None
    last_oi_confirmed_weights: ProductWeights
    last_cost_approved_weights: ProductWeights
    last_survivor_weights: ProductWeights
    completed_concentrations: tuple[float, ...]

    @classmethod
    def initial(
        cls,
        definition: Stress90PolicyDefinition = STRESS90_POLICY,
    ) -> Stress90CandidateState:
        zeros = {product: 0.0 for product in definition.products}
        return cls(
            policy_definition_digest=definition.policy_definition_digest,
            products_manifest_digest=definition.products_manifest_digest,
            last_completed_target_day=None,
            last_decision_digest=None,
            last_oi_confirmed_weights=_frozen_weights(zeros),
            last_cost_approved_weights=_frozen_weights(zeros),
            last_survivor_weights=_frozen_weights(zeros),
            completed_concentrations=(),
        )


@dataclass(frozen=True)
class Stress90Decision:
    """Auditable output of one pure Stress-90 target-day transition."""

    target_trading_day: str
    base_weights: ProductWeights
    oi_confirmed_weights: ProductWeights
    cost_approved_weights: ProductWeights
    survivor_weights: ProductWeights
    current_hhi: float | None
    prior_hhi_median: float | None
    concentration_freeze: bool
    input_days: Mapping[str, str]
    input_digests: Mapping[str, str]
    layer_digests: Mapping[str, str]
    prior_state: Stress90CandidateState
    post_state: Stress90CandidateState
    daily_decision_digest: str


@dataclass(frozen=True)
class Stress90CandidatePath:
    """Batch view produced solely by incremental transitions."""

    base_weights: pd.DataFrame
    oi_confirmed_weights: pd.DataFrame
    cost_approved_weights: pd.DataFrame
    survivor_weights: pd.DataFrame
    current_hhi: pd.Series
    prior_hhi_median: pd.Series
    concentration_freeze: pd.Series
    decisions: tuple[Stress90Decision, ...]
    final_state: Stress90CandidateState


def _canonical_day(raw: str | date | datetime, *, name: str) -> str:
    if isinstance(raw, datetime):
        value = raw.date().strftime("%Y%m%d")
    elif isinstance(raw, date):
        value = raw.strftime("%Y%m%d")
    else:
        value = str(raw).replace("-", "")
    if len(value) != 8 or not value.isdigit():
        raise Stress90InvariantError(f"invalid {name}: {raw}")
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise Stress90InvariantError(f"invalid {name}: {raw}") from exc
    return value


def _validated_prior_state(
    state: Stress90CandidateState,
    definition: Stress90PolicyDefinition,
) -> None:
    if state.policy_definition_digest != definition.policy_definition_digest:
        raise Stress90InvariantError("policy definition digest mismatch")
    if state.products_manifest_digest != definition.products_manifest_digest:
        raise Stress90InvariantError("products manifest digest mismatch")
    for name, weights in (
        ("prior OI-confirmed", state.last_oi_confirmed_weights),
        ("prior cost-approved", state.last_cost_approved_weights),
        ("prior survivor", state.last_survivor_weights),
    ):
        normalized = _normalized_values(
            weights,
            name=name,
            expected_products=definition.products,
        )
        _require_gross_cap(normalized, name=name, cap=definition.max_gross_leverage)
    advance_concentration_history(state.completed_concentrations, {})


def candidate_state_digest(state: Stress90CandidateState) -> str:
    """Commit to every economically relevant field in an incremental candidate state."""

    return _canonical_digest(
        {
            "policy_definition_digest": state.policy_definition_digest,
            "products_manifest_digest": state.products_manifest_digest,
            "last_completed_target_day": state.last_completed_target_day,
            "last_decision_digest": state.last_decision_digest,
            "last_oi_confirmed_weights": state.last_oi_confirmed_weights,
            "last_cost_approved_weights": state.last_cost_approved_weights,
            "last_survivor_weights": state.last_survivor_weights,
            "completed_concentrations": state.completed_concentrations,
        }
    )


def step_stress90_candidate(
    prior_state: Stress90CandidateState,
    target_trading_day: str | date | datetime,
    base_weights: Mapping[str, float],
    completed_close_history: Mapping[str, Sequence[float]],
    completed_oi_flow: Mapping[str, int | float | None],
    *,
    completed_close_day: str | date | datetime,
    completed_oi_day: str | date | datetime,
    definition: Stress90PolicyDefinition = STRESS90_POLICY,
) -> Stress90Decision:
    """Advance the frozen Base→OI→cost→survivor→HHI candidate exactly once in memory."""

    _validated_prior_state(prior_state, definition)
    target_day = _canonical_day(target_trading_day, name="target trading day")
    close_day = _canonical_day(completed_close_day, name="completed close day")
    oi_day = _canonical_day(completed_oi_day, name="completed OI day")
    if close_day >= target_day or oi_day >= target_day:
        raise Stress90InputIncomplete("completed input day must be strictly before target day")
    if close_day != oi_day:
        raise Stress90InputIncomplete("completed close and OI days must align")
    if prior_state.last_completed_target_day is not None:
        prior_day = _canonical_day(
            prior_state.last_completed_target_day,
            name="prior target trading day",
        )
        if target_day <= prior_day:
            raise Stress90InvariantError("target trading day did not advance")

    base = _normalized_values(
        base_weights,
        name="base",
        expected_products=definition.products,
    )
    _require_gross_cap(base, name="base", cap=definition.max_gross_leverage)
    close_keys = tuple(str(product).upper() for product in completed_close_history)
    if set(close_keys) != set(definition.products) or len(close_keys) != len(definition.products):
        raise Stress90InputIncomplete("completed close history product manifest mismatch")
    flow_keys = tuple(str(product).upper() for product in completed_oi_flow)
    if set(flow_keys) != set(definition.oi_products) or len(flow_keys) != len(
        definition.oi_products
    ):
        raise Stress90InputIncomplete("completed OI flow product manifest mismatch")

    raw_oi_flow = {str(key).upper(): value for key, value in completed_oi_flow.items()}
    oi = apply_oi_confirmation_row(
        raw_weights=base,
        prior_applied=prior_state.last_oi_confirmed_weights,
        completed_flow=raw_oi_flow,
        supported_products=definition.oi_products,
    )
    normalized_oi_flow: dict[str, int | None] = {}
    for product in definition.oi_products:
        value = raw_oi_flow[product]
        normalized_oi_flow[product] = (
            None
            if value is None
            or (isinstance(value, (float, np.floating)) and not isfinite(float(value)))
            else int(value)
        )
    approved = apply_cost_gate_row(
        oi_weights=oi,
        prior_approved=prior_state.last_cost_approved_weights,
        completed_close_history={
            str(product).upper(): values for product, values in completed_close_history.items()
        },
        completed_lookback_sessions=definition.completed_lookback_sessions,
        benefit_horizon_sessions=definition.benefit_horizon_sessions,
        cost_hurdle_bps=definition.cost_hurdle_bps,
    )
    survivor = reallocate_survivor_row(
        oi_weights=oi,
        approved_weights=approved,
        prior_survivor=prior_state.last_survivor_weights,
    )
    current_hhi, prior_hhi_median, freeze, concentrations = advance_concentration_history(
        prior_state.completed_concentrations,
        survivor,
    )

    normalized_history = {
        str(product).upper(): tuple(float(value) for value in values)
        for product, values in completed_close_history.items()
    }
    input_days = {"completed_close": close_day, "completed_oi": oi_day}
    input_digests = {
        "prior_state": candidate_state_digest(prior_state),
        "base_weights": _canonical_digest(base),
        "completed_close": _canonical_digest(normalized_history),
        "completed_oi": _canonical_digest(normalized_oi_flow),
    }
    layer_digests = {
        "base": _canonical_digest(base),
        "oi": _canonical_digest(oi),
        "cost": _canonical_digest(approved),
        "survivor": _canonical_digest(survivor),
    }
    decision_digest = _canonical_digest(
        {
            "policy_definition_digest": definition.policy_definition_digest,
            "target_trading_day": target_day,
            "input_days": input_days,
            "input_digests": input_digests,
            "layer_digests": layer_digests,
            "current_hhi": current_hhi,
            "prior_hhi_median": prior_hhi_median,
            "concentration_freeze": freeze,
        }
    )
    post_state = Stress90CandidateState(
        policy_definition_digest=definition.policy_definition_digest,
        products_manifest_digest=definition.products_manifest_digest,
        last_completed_target_day=target_day,
        last_decision_digest=decision_digest,
        last_oi_confirmed_weights=_frozen_weights(oi),
        last_cost_approved_weights=_frozen_weights(approved),
        last_survivor_weights=_frozen_weights(survivor),
        completed_concentrations=concentrations,
    )
    return Stress90Decision(
        target_trading_day=target_day,
        base_weights=_frozen_weights(base),
        oi_confirmed_weights=_frozen_weights(oi),
        cost_approved_weights=_frozen_weights(approved),
        survivor_weights=_frozen_weights(survivor),
        current_hhi=current_hhi,
        prior_hhi_median=prior_hhi_median,
        concentration_freeze=freeze,
        input_days=MappingProxyType(input_days),
        input_digests=MappingProxyType(input_digests),
        layer_digests=MappingProxyType(layer_digests),
        prior_state=prior_state,
        post_state=post_state,
        daily_decision_digest=decision_digest,
    )


def _normalized_frame(
    frame: pd.DataFrame,
    *,
    name: str,
    products: Sequence[str],
    require_finite: bool,
) -> pd.DataFrame:
    result = frame.copy()
    result.index = pd.DatetimeIndex(pd.to_datetime(result.index, errors="coerce")).normalize()
    if (
        result.index.hasnans
        or result.index.has_duplicates
        or not result.index.is_monotonic_increasing
    ):
        raise Stress90InvariantError(f"{name} index must be unique, finite, and increasing")
    normalized_columns = [str(column).upper() for column in result.columns]
    if len(set(normalized_columns)) != len(normalized_columns):
        raise Stress90InvariantError(f"{name} contains duplicate product columns")
    result.columns = normalized_columns
    expected = tuple(str(product).upper() for product in products)
    if set(result.columns) != set(expected):
        raise Stress90InvariantError(f"{name} product manifest mismatch")
    result = result.reindex(columns=expected).astype(float)
    if require_finite and not np.isfinite(result.to_numpy(float)).all():
        raise Stress90InvariantError(f"{name} must be finite")
    return result


def build_stress90_candidate_path(
    *,
    base_weights: pd.DataFrame,
    completed_close_prices: pd.DataFrame,
    confirming_flow: pd.DataFrame,
    initial_state: Stress90CandidateState | None = None,
    definition: Stress90PolicyDefinition = STRESS90_POLICY,
) -> Stress90CandidatePath:
    """Replay batch inputs through the exact incremental production transition."""

    base = _normalized_frame(
        base_weights,
        name="base weights",
        products=definition.products,
        require_finite=True,
    )
    close = _normalized_frame(
        completed_close_prices,
        name="completed close prices",
        products=definition.products,
        require_finite=False,
    )
    finite_close = close.where(np.isfinite(close))
    if bool((finite_close <= 0.0).any().any()):
        raise Stress90InputIncomplete("completed close prices must be positive")
    flow = _normalized_frame(
        confirming_flow,
        name="confirming OI flow",
        products=definition.oi_products,
        require_finite=False,
    ).reindex(index=base.index)

    state = initial_state or Stress90CandidateState.initial(definition)
    decisions: list[Stress90Decision] = []
    for target_day in base.index:
        history = close.loc[close.index < target_day]
        if history.empty:
            raise Stress90InputIncomplete(
                f"completed close history missing before target day {target_day.date()}"
            )
        input_day = pd.Timestamp(history.index[-1]).strftime("%Y%m%d")
        flow_row = flow.loc[target_day]
        decision = step_stress90_candidate(
            prior_state=state,
            target_trading_day=pd.Timestamp(target_day).strftime("%Y%m%d"),
            base_weights=base.loc[target_day].to_dict(),
            completed_close_history={
                product: tuple(history[product].to_numpy(float)) for product in definition.products
            },
            completed_oi_flow={
                product: (None if pd.isna(flow_row[product]) else float(flow_row[product]))
                for product in definition.oi_products
            },
            completed_close_day=input_day,
            completed_oi_day=input_day,
            definition=definition,
        )
        decisions.append(decision)
        state = decision.post_state

    index = base.index

    def layer_frame(attribute: str) -> pd.DataFrame:
        return pd.DataFrame(
            [dict(getattr(decision, attribute)) for decision in decisions],
            index=index,
            columns=definition.products,
            dtype=float,
        )

    return Stress90CandidatePath(
        base_weights=layer_frame("base_weights"),
        oi_confirmed_weights=layer_frame("oi_confirmed_weights"),
        cost_approved_weights=layer_frame("cost_approved_weights"),
        survivor_weights=layer_frame("survivor_weights"),
        current_hhi=pd.Series(
            [decision.current_hhi for decision in decisions],
            index=index,
            dtype=object,
        ),
        prior_hhi_median=pd.Series(
            [decision.prior_hhi_median for decision in decisions],
            index=index,
            dtype=object,
        ),
        concentration_freeze=pd.Series(
            [decision.concentration_freeze for decision in decisions],
            index=index,
            dtype=bool,
        ),
        decisions=tuple(decisions),
        final_state=state,
    )


def candidate_weight_digest(weights: pd.DataFrame) -> str:
    """Return the archived stable text SHA-256 for a complete candidate path."""

    rows: list[str] = []
    ordered = weights.sort_index().reindex(sorted(weights.columns), axis=1)
    values = ordered.to_numpy(float)
    if not np.isfinite(values).all():
        raise Stress90InvariantError("candidate weights must be finite")
    for timestamp, row in ordered.iterrows():
        day = pd.Timestamp(timestamp).date().isoformat()
        for product, raw_value in row.items():
            rows.append(f"{day}|{str(product).upper()}|{float(raw_value):.17g}\n")
    return hashlib.sha256("".join(rows).encode("utf-8")).hexdigest()
