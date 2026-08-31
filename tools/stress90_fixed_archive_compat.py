"""Frozen-input-only Stress-90 historical replay compatibility.

This module is an offline evidence adapter.  It preserves the archived research
semantics at commit ``9c51195042393304eb05d783d1895a165f99b0a7`` without
weakening the current incremental production primitives.  Callers must first
present the complete five-file frozen manifest; any other input is rejected.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from types import MappingProxyType

import numpy as np
import pandas as pd

from afuture.directional_stress90_policy import _apply_cost_gate_from_trends_row

HISTORICAL_REPLAY_COMMIT = "9c51195042393304eb05d783d1895a165f99b0a7"
FIXED_INPUT_SHA256 = MappingProxyType(
    {
        "broad_daily_universe.csv": (
            "c1d46bc113a79bd2e000bf5d66ee53c59337eda750b2d3b1409e40f3f4833d0f"
        ),
        "execution_aligned_weights.csv": (
            "250a55c1c18ecd3754f489136515275eec3a48d505fd41026d68ab43924e08c1"
        ),
        "prior_two_year_broad_60m.csv": (
            "3351aa3ae8dec0cf0e9b9a64026181ac8ff2cc22858c442821fe3c94767036b1"
        ),
        "return_target_specific_contracts.csv": (
            "f8b3f4232cb1bc9eaed65a4401dff6bed121faee064252727202874ad7d53c64"
        ),
        "two_year_broad_60m.csv": (
            "5faf112bb69dd5ddf48ed34419e2046b1c6bdd651b595ca46a46804d8317a27b"
        ),
    }
)
FIXED_INPUT_SIZE_BYTES = MappingProxyType(
    {
        "broad_daily_universe.csv": 3_278_200,
        "execution_aligned_weights.csv": 109_977,
        "prior_two_year_broad_60m.csv": 3_698_425,
        "return_target_specific_contracts.csv": 40_842_073,
        "two_year_broad_60m.csv": 4_080_136,
    }
)
AUTHORIZATION_METADATA = MappingProxyType(
    {
        "historical_replay_commit": HISTORICAL_REPLAY_COMMIT,
        "evidence_scope": "historical_research_only",
        "live_authorized": False,
        "risk_increase_authorized": False,
        "prospective_evidence": False,
    }
)

TREND_LOOKBACK_SESSIONS = 20
BENEFIT_HORIZON_SESSIONS = 3
COST_HURDLE_BPS = 15.0
_EPS = 1e-12


class FixedArchiveCompatibilityError(ValueError):
    """The caller did not present the exact frozen archive contract."""


@dataclass(frozen=True)
class FixedArchiveReplay:
    specific_raw: pd.DataFrame
    candidate_weights: pd.DataFrame
    audit: Mapping[str, object]
    input_manifest: tuple[dict[str, str | int], ...]


def validate_fixed_archive_manifest(
    manifest: Iterable[Mapping[str, object]],
) -> tuple[dict[str, str | int], ...]:
    """Return the canonical manifest only when all five frozen identities match."""

    entries: dict[str, dict[str, str | int]] = {}
    for raw in manifest:
        if set(raw) != {"basename", "sha256", "size_bytes"}:
            raise FixedArchiveCompatibilityError("fixed archive manifest entry is invalid")
        basename = raw["basename"]
        digest = raw["sha256"]
        size_bytes = raw["size_bytes"]
        if not isinstance(basename, str) or basename in entries:
            raise FixedArchiveCompatibilityError("fixed archive basenames are invalid")
        expected_digest = FIXED_INPUT_SHA256.get(basename)
        expected_size = FIXED_INPUT_SIZE_BYTES.get(basename)
        if expected_digest is None or expected_size is None:
            raise FixedArchiveCompatibilityError(f"unexpected fixed archive file: {basename}")
        if digest != expected_digest:
            raise FixedArchiveCompatibilityError(f"fixed archive SHA-256 mismatch: {basename}")
        if isinstance(size_bytes, bool) or size_bytes != expected_size:
            raise FixedArchiveCompatibilityError(f"fixed archive size mismatch: {basename}")
        entries[basename] = {
            "basename": basename,
            "sha256": expected_digest,
            "size_bytes": expected_size,
        }
    if set(entries) != set(FIXED_INPUT_SHA256):
        raise FixedArchiveCompatibilityError("fixed archive manifest is incomplete")
    return tuple(entries[name] for name in sorted(entries))


def _normalized_weights(frame: pd.DataFrame, *, name: str) -> pd.DataFrame:
    result = frame.copy()
    result.index = pd.DatetimeIndex(pd.to_datetime(result.index, errors="coerce")).normalize()
    result.columns = [str(column).upper() for column in result.columns]
    result = result.astype(float)
    if (
        result.index.hasnans
        or result.index.has_duplicates
        or not result.index.is_monotonic_increasing
    ):
        raise FixedArchiveCompatibilityError(f"{name} index is invalid")
    if len(set(result.columns)) != len(result.columns):
        raise FixedArchiveCompatibilityError(f"{name} columns are invalid")
    if not np.isfinite(result.to_numpy(float)).all():
        raise FixedArchiveCompatibilityError(f"{name} must be finite")
    return result


def _apply_historical_oi_confirmation(
    *,
    raw_weights: pd.DataFrame,
    confirming_flow: pd.DataFrame,
    supported_products: Iterable[str],
) -> pd.DataFrame:
    """Reproduce archived OI gating while preserving missing evidence as missing."""

    raw = _normalized_weights(raw_weights, name="historical raw weights")
    flow = confirming_flow.copy()
    flow.index = pd.DatetimeIndex(pd.to_datetime(flow.index, errors="coerce")).normalize()
    flow.columns = [str(column).upper() for column in flow.columns]
    flow = flow.reindex(index=raw.index, columns=raw.columns).astype(float)
    supported = {str(product).upper() for product in supported_products}
    result = pd.DataFrame(0.0, index=raw.index, columns=raw.columns)
    previous = {product: 0.0 for product in raw.columns}

    for day in raw.index:
        for product in raw.columns:
            target = float(raw.at[day, product])
            current = float(previous[product])
            raw_evidence = flow.at[day, product]
            evidence = float(raw_evidence) if isfinite(float(raw_evidence)) else None
            if product not in supported:
                applied = target
            elif abs(target) <= _EPS:
                applied = 0.0
            elif abs(current) <= _EPS:
                applied = target if evidence == float(np.sign(target)) else 0.0
            elif np.sign(target) != np.sign(current):
                applied = target if evidence == float(np.sign(target)) else 0.0
            elif abs(target) > abs(current) + _EPS:
                applied = target if evidence == float(np.sign(target)) else current
            else:
                applied = target
            if abs(applied) > abs(target) + _EPS:
                raise AssertionError("historical OI confirmation increased product exposure")
            result.at[day, product] = applied
            previous[product] = applied

    if bool((result.abs().sum(axis=1) > raw.abs().sum(axis=1) + 1e-10).any()):
        raise AssertionError("historical OI confirmation increased gross exposure")
    return result.astype(float)


def _historical_completed_return_sums(close_prices: pd.DataFrame) -> pd.DataFrame:
    close = close_prices.copy()
    close.index = pd.DatetimeIndex(pd.to_datetime(close.index, errors="coerce")).normalize()
    close.columns = [str(column).upper() for column in close.columns]
    close = close.sort_index().astype(float)
    daily = close.where(close > 0.0).pct_change(fill_method=None)
    return (
        daily.rolling(
            TREND_LOOKBACK_SESSIONS,
            min_periods=TREND_LOOKBACK_SESSIONS,
        )
        .sum()
        .shift(1)
    )


def _apply_historical_cost_gate(
    *,
    weights: pd.DataFrame,
    close_prices: pd.DataFrame,
    initial_weights: Mapping[str, float] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Apply the archived 20-session arithmetic-return gate without imputing gaps."""

    raw = _normalized_weights(weights, name="historical OI-confirmed weights")
    completed = _historical_completed_return_sums(close_prices).reindex(
        index=raw.index,
        columns=raw.columns,
    )
    state = {
        str(product).upper(): float(value)
        for product, value in (initial_weights or {}).items()
        if isfinite(float(value)) and abs(float(value)) > _EPS
    }
    output = pd.DataFrame(0.0, index=raw.index, columns=raw.columns)
    for timestamp in raw.index:
        trends = {
            product: (
                float(completed.at[timestamp, product])
                if isfinite(float(completed.at[timestamp, product]))
                else None
            )
            for product in raw.columns
        }
        applied = _apply_cost_gate_from_trends_row(
            oi_weights=raw.loc[timestamp].to_dict(),
            prior_approved=state,
            completed_return_sums=trends,
            completed_lookback_sessions=TREND_LOOKBACK_SESSIONS,
            benefit_horizon_sessions=BENEFIT_HORIZON_SESSIONS,
            cost_hurdle_bps=COST_HURDLE_BPS,
        )
        output.loc[timestamp] = pd.Series(applied).reindex(raw.columns)
        state = {product: value for product, value in applied.items() if abs(value) > _EPS}
    return output.astype(float), completed


def replay_fixed_archive(runtime: Path) -> FixedArchiveReplay:
    """Verify, parse, and replay the one frozen five-file archive."""

    import tools.evaluate_directional_stress80_final as stress80
    from afuture.directional_turnover_aware_survivor_reallocation import (
        reallocate_survivors_lexicographically,
    )

    specific, continuous, base_weights, bars, raw_manifest = stress80._load_inputs(runtime)
    input_manifest = validate_fixed_archive_manifest(raw_manifest)
    flow = stress80.oi_gate.build_daily_price_oi_flow(bars)
    lagged = stress80.oi_gate.lag_flow_to_target_days(
        flow,
        target_days=base_weights.index,
        products=base_weights.columns,
    )
    confirmed = _apply_historical_oi_confirmation(
        raw_weights=base_weights,
        confirming_flow=lagged,
        supported_products=stress80.oi_gate.SUPPORTED_PRODUCTS,
    )
    close = stress80.continuous_close_panel(continuous, list(base_weights.columns))
    approved, _completed_return_sums = _apply_historical_cost_gate(
        weights=confirmed,
        close_prices=close,
    )
    candidate = reallocate_survivors_lexicographically(
        original_weights=confirmed,
        approved_weights=approved,
    )
    candidate = candidate.reindex(
        index=base_weights.index,
        columns=base_weights.columns,
    ).astype(float)

    if not np.isfinite(candidate.to_numpy()).all():
        raise AssertionError("fixed archive candidate contains non-finite weights")
    if bool((candidate.abs().sum(axis=1) > stress80.MAX_GROSS + 1e-10).any()):
        raise AssertionError("fixed archive candidate exceeds 2x gross")
    if bool((candidate.abs().sum(axis=1) > confirmed.abs().sum(axis=1) + 1e-10).any()):
        raise AssertionError("fixed archive survivor allocation exceeds confirmed OI gross")

    digest = stress80.candidate_weight_digest(candidate)
    full = candidate.loc[pd.Timestamp("2024-08-21") : pd.Timestamp("2026-08-20")]
    audit: dict[str, object] = {
        **dict(AUTHORIZATION_METADATA),
        "candidate_weight_sha256": digest,
        "policy_definition_digest": stress80.STRESS90_POLICY.policy_definition_digest,
        "supported_products": list(stress80.oi_gate.SUPPORTED_PRODUCTS),
        "confirmed_turnover_full_path": stress80.weight_turnover(confirmed),
        "approved_turnover_full_path": stress80.weight_turnover(approved),
        "candidate_turnover_full_path": stress80.weight_turnover(candidate),
        "candidate_average_gross_full_recent": float(full.abs().sum(axis=1).mean()),
        "candidate_max_gross": float(candidate.abs().sum(axis=1).max()),
    }
    return FixedArchiveReplay(
        specific_raw=specific,
        candidate_weights=candidate,
        audit=MappingProxyType(audit),
        input_manifest=input_manifest,
    )
