"""Deterministic, fixed-input bootstrap for the production Stress-90 state machine.

This module is deliberately independent of ``tools/`` and evaluator entrypoints.  It
validates immutable source bytes before parsing, rebuilds the frozen base policy, then
replays the same incremental primitives used by live runtime.  Only the documented
historical profile may create a live seed/state; alternate expectations are test-only
dry runs and can never be promoted accidentally.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .directional_60m_oi_confirmation import (
    build_daily_price_oi_flow,
    lag_flow_to_target_days,
)
from .directional_60m_oi_reversal_confirmation import (
    apply_oi_confirmation_to_direction_changes,
)
from .directional_activity import (
    ContractActivity,
    DirectionalActivityIntegrityError,
    DirectionalActivitySnapshot,
    DirectionalActivityStore,
)
from .directional_cost_aware_no_trade import apply_cost_aware_no_trade
from .directional_ohlc_cache import (
    DirectionalOHLCCacheIntegrityError,
    DirectionalOHLCCacheStore,
)
from .directional_sessions import PRODUCT_SESSION_MANIFEST
from .directional_stress90_oi_runtime import (
    OiEvidenceIntegrityError,
    Stress90OiEvidenceState,
    Stress90OiEvidenceStore,
    build_fixed_historical_60m_evidence,
)
from .directional_stress90_policy import (
    STRESS90_POLICY,
    Stress90CandidatePath,
    build_stress90_candidate_path,
    candidate_weight_digest,
)
from .directional_stress90_state import (
    FIXED_BOOTSTRAP_INPUT_NAMES,
    Stress90BootstrapSeed,
    Stress90DecisionInputs,
    Stress90PolicyState,
    Stress90PolicyStateStore,
    Stress90PreparedDecision,
    Stress90SeedStore,
    Stress90StateIntegrityError,
    stress90_decision_inputs_digest,
)
from .directional_turnover_aware_survivor_reallocation import (
    reallocate_survivors_lexicographically,
)
from .durable_file_creation import (
    DurableFileCreationToken,
    DurableFileError,
    canonical_file_path,
    creation_token_matches,
    durable_file_lock,
    unlink_created_file,
)
from .execution_aligned_policy import ExecutionAlignedAggressivePolicy

FIXED_STRESS90_INPUT_SHA256 = MappingProxyType(
    {
        "broad_daily_universe.csv": (
            "c1d46bc113a79bd2e000bf5d66ee53c59337eda750b2d3b1409e40f3f4833d0f"
        ),
        "return_target_specific_contracts.csv": (
            "f8b3f4232cb1bc9eaed65a4401dff6bed121faee064252727202874ad7d53c64"
        ),
        "execution_aligned_weights.csv": (
            "250a55c1c18ecd3754f489136515275eec3a48d505fd41026d68ab43924e08c1"
        ),
        "prior_two_year_broad_60m.csv": (
            "3351aa3ae8dec0cf0e9b9a64026181ac8ff2cc22858c442821fe3c94767036b1"
        ),
        "two_year_broad_60m.csv": (
            "5faf112bb69dd5ddf48ed34419e2046b1c6bdd651b595ca46a46804d8317a27b"
        ),
    }
)

_BASE_PARITY_ATOL = 5e-15
_LAYER_PARITY_ATOL = 1e-14
_HEX_DIGEST_LENGTH = 64
_CHINA = ZoneInfo("Asia/Shanghai")


class Stress90BootstrapError(RuntimeError):
    """The historical bootstrap evidence cannot be trusted or replayed."""


@dataclass(frozen=True)
class _OwnedPathSnapshot:
    """Pre-bootstrap identity and bytes for one owner-declared durable path."""

    kind: str
    payload: bytes | str | None

    @property
    def absent(self) -> bool:
        return self.kind == "missing"


def _snapshot_owned_path(path: Path) -> _OwnedPathSnapshot:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return _OwnedPathSnapshot("missing", None)
    if stat.S_ISLNK(metadata.st_mode):
        return _OwnedPathSnapshot("symlink", os.readlink(path))
    if not stat.S_ISREG(metadata.st_mode):
        return _OwnedPathSnapshot("non-regular", None)
    return _OwnedPathSnapshot("regular", path.read_bytes())


def _require_supported_owned_paths(
    *,
    owner: str,
    paths: Mapping[str, Path],
    snapshots: Mapping[Path, _OwnedPathSnapshot],
) -> None:
    for role, path in paths.items():
        snapshot = snapshots[path]
        if snapshot.kind in {"symlink", "non-regular"}:
            raise Stress90BootstrapError(
                f"existing Stress-90 {owner} incident: {snapshot.kind} {role} path is not supported"
            )


@dataclass(frozen=True)
class Stress90BootstrapExpectations:
    """Explicit evidence profile; only the module constant is live-promotable."""

    input_sha256: Mapping[str, str]
    candidate_weight_sha256: str
    official_historical_profile: bool

    def __post_init__(self) -> None:
        expected_names = set(FIXED_BOOTSTRAP_INPUT_NAMES)
        if set(self.input_sha256) != expected_names:
            raise ValueError("Stress-90 bootstrap expectations require exactly five inputs")
        for name, digest in self.input_sha256.items():
            if (
                not isinstance(name, str)
                or not isinstance(digest, str)
                or len(digest) != _HEX_DIGEST_LENGTH
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise ValueError("Stress-90 bootstrap input digests must be lowercase SHA-256")
        digest = self.candidate_weight_sha256
        if (
            not isinstance(digest, str)
            or len(digest) != _HEX_DIGEST_LENGTH
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise ValueError("Stress-90 candidate expectation must be lowercase SHA-256")
        if not isinstance(self.official_historical_profile, bool):
            raise ValueError("official_historical_profile must be bool")


OFFICIAL_STRESS90_BOOTSTRAP_EXPECTATIONS = Stress90BootstrapExpectations(
    input_sha256=FIXED_STRESS90_INPUT_SHA256,
    candidate_weight_sha256=STRESS90_POLICY.historical_candidate_weight_sha256,
    official_historical_profile=True,
)


@dataclass(frozen=True)
class Stress90BootstrapResult:
    source_manifest: Mapping[str, str]
    candidate_weight_sha256: str
    base_max_abs_error: float
    batch_incremental_max_abs_error: float
    target_day_count: int
    first_target_day: str
    last_target_day: str
    last_input_day: str
    policy_definition_digest: str
    historical_candidate_parity: bool
    seed_path: Path | None
    state_path: Path | None
    oi_evidence_path: Path | None
    candidate_path: Stress90CandidatePath

    def to_dict(self) -> dict[str, object]:
        return {
            "source_manifest": dict(self.source_manifest),
            "candidate_weight_sha256": self.candidate_weight_sha256,
            "base_max_abs_error": self.base_max_abs_error,
            "batch_incremental_max_abs_error": self.batch_incremental_max_abs_error,
            "target_day_count": self.target_day_count,
            "first_target_day": self.first_target_day,
            "last_target_day": self.last_target_day,
            "last_input_day": self.last_input_day,
            "policy_definition_digest": self.policy_definition_digest,
            "historical_candidate_parity": self.historical_candidate_parity,
            "seed_path": None if self.seed_path is None else str(self.seed_path),
            "state_path": None if self.state_path is None else str(self.state_path),
            "oi_evidence_path": (
                None if self.oi_evidence_path is None else str(self.oi_evidence_path)
            ),
        }


def _is_official_profile(expectations: Stress90BootstrapExpectations) -> bool:
    return bool(
        expectations.official_historical_profile
        and dict(expectations.input_sha256) == dict(FIXED_STRESS90_INPUT_SHA256)
        and expectations.candidate_weight_sha256
        == STRESS90_POLICY.historical_candidate_weight_sha256
    )


def _canonical_through_day(raw: str) -> tuple[str, pd.Timestamp]:
    if not isinstance(raw, str):
        raise Stress90BootstrapError("bootstrap through day must be YYYYMMDD")
    try:
        parsed = pd.Timestamp(datetime.strptime(raw, "%Y%m%d")).normalize()
    except ValueError as exc:
        raise Stress90BootstrapError("bootstrap through day must be YYYYMMDD") from exc
    if parsed.strftime("%Y%m%d") != raw:
        raise Stress90BootstrapError("bootstrap through day must be YYYYMMDD")
    return raw, parsed


def _verify_source_bytes(
    runtime_dir: Path,
    expectations: Stress90BootstrapExpectations,
) -> Mapping[str, str]:
    manifest: dict[str, str] = {}
    missing: list[str] = []
    for name in FIXED_BOOTSTRAP_INPUT_NAMES:
        path = runtime_dir / name
        if not path.is_file():
            missing.append(name)
            continue
        digest = sha256(path.read_bytes()).hexdigest()
        manifest[name] = digest
        if digest != expectations.input_sha256[name]:
            raise Stress90BootstrapError(
                f"fixed Stress-90 input SHA-256 mismatch: {name}; "
                f"expected={expectations.input_sha256[name]}, actual={digest}"
            )
    if missing:
        raise Stress90BootstrapError(
            "fixed Stress-90 bootstrap inputs are missing: " + ", ".join(sorted(missing))
        )
    return MappingProxyType(manifest)


def _read_csv(path: Path, *, name: str) -> pd.DataFrame:
    try:
        frame = pd.read_csv(path)
    except Exception as exc:
        raise Stress90BootstrapError(f"cannot parse {name}") from exc
    if frame.empty:
        raise Stress90BootstrapError(f"{name} is empty")
    return frame


def _load_continuous_panels(path: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    frame = _read_csv(path, name="broad daily universe")
    required = {"date", "product", "open", "close"}
    if not required.issubset(frame.columns):
        raise Stress90BootstrapError(
            "broad daily universe missing columns: "
            + ", ".join(sorted(required - set(frame.columns)))
        )
    frame = frame.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    frame["product"] = frame["product"].astype(str).str.upper()
    frame["open"] = pd.to_numeric(frame["open"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    invalid = (
        frame["date"].isna()
        | ~frame["product"].isin(STRESS90_POLICY.products)
        | ~np.isfinite(frame["open"].to_numpy(float))
        | ~np.isfinite(frame["close"].to_numpy(float))
        | (frame["open"] <= 0.0)
        | (frame["close"] <= 0.0)
    )
    if bool(invalid.any()):
        raise Stress90BootstrapError("broad daily universe contains invalid rows")
    if bool(frame.duplicated(["date", "product"]).any()):
        raise Stress90BootstrapError("broad daily universe contains duplicate date/product rows")
    open_prices = frame.pivot(index="date", columns="product", values="open").sort_index()
    close_prices = frame.pivot(index="date", columns="product", values="close").sort_index()
    open_prices = open_prices.reindex(columns=STRESS90_POLICY.products)
    close_prices = close_prices.reindex(index=open_prices.index, columns=STRESS90_POLICY.products)
    if open_prices.isna().any().any() or close_prices.isna().any().any():
        raise Stress90BootstrapError("broad daily universe lacks complete 50-product coverage")
    if not open_prices.index.is_monotonic_increasing or open_prices.index.has_duplicates:
        raise Stress90BootstrapError("broad daily universe sessions are not canonical")
    return frame, open_prices.astype(float), close_prices.astype(float)


def _load_archived_weights(path: Path, through: pd.Timestamp) -> pd.DataFrame:
    rows = _read_csv(path, name="execution-aligned weights")
    required = {"level_0", "level_1", "weight"}
    if set(rows.columns) != required:
        raise Stress90BootstrapError(
            "execution-aligned weights must contain exactly level_0, level_1, weight"
        )
    rows = rows.copy()
    rows["level_0"] = pd.to_datetime(rows["level_0"], errors="coerce").dt.normalize()
    rows["level_1"] = rows["level_1"].astype(str).str.upper()
    rows["weight"] = pd.to_numeric(rows["weight"], errors="coerce")
    invalid = (
        rows["level_0"].isna()
        | ~rows["level_1"].isin(STRESS90_POLICY.products)
        | ~np.isfinite(rows["weight"].to_numpy(float))
    )
    if bool(invalid.any()):
        raise Stress90BootstrapError("execution-aligned weights contain invalid rows")
    if bool(rows.duplicated(["level_0", "level_1"]).any()):
        raise Stress90BootstrapError("execution-aligned weights contain duplicate rows")
    rows = rows[rows["level_0"] <= through]
    if rows.empty or through not in set(rows["level_0"]):
        raise Stress90BootstrapError("bootstrap through day is absent from frozen weights")
    weights = (
        rows.pivot(index="level_0", columns="level_1", values="weight")
        .sort_index()
        .reindex(columns=STRESS90_POLICY.products)
        .fillna(0.0)
        .astype(float)
    )
    if bool((weights.abs().sum(axis=1) > STRESS90_POLICY.max_gross_leverage + 1e-10).any()):
        raise Stress90BootstrapError("execution-aligned weights exceed frozen gross cap")
    return weights


def _validate_target_continuity(targets: pd.DatetimeIndex, sessions: pd.DatetimeIndex) -> None:
    positions = sessions.get_indexer(targets)
    if bool((positions < 0).any()):
        raise Stress90BootstrapError("frozen target day is absent from broad daily sessions")
    if len(positions) > 1 and not np.array_equal(np.diff(positions), np.ones(len(positions) - 1)):
        raise Stress90BootstrapError("frozen target-day gap cannot be skipped")
    if positions[0] <= 0:
        raise Stress90BootstrapError("bootstrap needs a completed session before first target")


def _validate_specific_contracts(path: Path, targets: pd.DatetimeIndex) -> pd.DataFrame:
    frame = _read_csv(path, name="specific-contract daily evidence")
    required = {"date", "delivery", "product", "symbol", "open", "close", "volume", "hold"}
    missing = required - set(frame.columns)
    if missing:
        raise Stress90BootstrapError(
            "specific-contract evidence missing columns: " + ", ".join(sorted(missing))
        )
    frame = frame.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    frame["delivery"] = pd.to_datetime(frame["delivery"], errors="coerce").dt.normalize()
    frame["product"] = frame["product"].astype(str).str.upper()
    frame["symbol"] = frame["symbol"].astype(str).str.upper()
    for column in ("open", "close", "volume", "hold"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    numeric = frame[["open", "close", "volume", "hold"]].to_numpy(float)
    invalid = (
        frame["date"].isna()
        | frame["delivery"].isna()
        | ~frame["product"].isin(STRESS90_POLICY.products)
        | frame["symbol"].isin({"", "NAN", "NONE"})
        | ~np.isfinite(numeric).all(axis=1)
        | (frame["open"] <= 0.0)
        | (frame["close"] <= 0.0)
        | (frame["volume"] < 0.0)
        | (frame["hold"] < 0.0)
    )
    if bool(invalid.any()):
        raise Stress90BootstrapError("specific-contract evidence contains invalid rows")
    if bool(frame.duplicated(["date", "symbol"]).any()):
        raise Stress90BootstrapError("specific-contract evidence contains duplicate date/symbol")
    covered = frame[frame["date"].isin(targets)].groupby("date")["product"].agg(set)
    required_products = set(STRESS90_POLICY.products)
    for target in targets:
        if target not in covered.index or not required_products.issubset(covered.loc[target]):
            raise Stress90BootstrapError(
                f"specific-contract evidence lacks 50-product coverage: {target.date()}"
            )
    return frame


def _through_day_activity_snapshot(
    specific_contracts: pd.DataFrame,
    through: pd.Timestamp,
) -> DirectionalActivitySnapshot:
    """Build the immutable completed activity input for the first live target."""

    rows = specific_contracts.loc[specific_contracts["date"] == through]
    if rows.empty:
        raise Stress90BootstrapError("specific-contract through-day activity is missing")
    timestamp = through.to_pydatetime().replace(hour=15, tzinfo=_CHINA)
    contracts: dict[str, ContractActivity] = {}
    for row in rows.itertuples(index=False):
        symbol = str(row.symbol).upper()
        product = str(row.product).upper()
        if symbol in contracts:
            raise Stress90BootstrapError("specific-contract through-day symbols are duplicated")
        contracts[symbol] = ContractActivity(
            symbol=symbol,
            exchange=PRODUCT_SESSION_MANIFEST[product].exchange,
            product=product,
            trading_day=through.strftime("%Y%m%d"),
            volume=float(row.volume),
            open_interest=float(row.hold),
            timestamp=timestamp,
        )
    return DirectionalActivitySnapshot(through.strftime("%Y%m%d"), contracts)


def _load_60m(paths: tuple[Path, Path]) -> pd.DataFrame:
    frames = [_read_csv(path, name=path.name) for path in paths]
    frame = pd.concat(frames, ignore_index=True)
    required = {"datetime", "product", "symbol", "open", "close", "volume", "hold"}
    missing = required - set(frame.columns)
    if missing:
        raise Stress90BootstrapError("60m evidence missing columns: " + ", ".join(sorted(missing)))
    frame = frame.copy()
    frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce")
    frame["product"] = frame["product"].astype(str).str.upper()
    frame["symbol"] = frame["symbol"].astype(str).str.upper()
    for column in ("open", "close", "volume", "hold"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    numeric = frame[["open", "close", "volume", "hold"]].to_numpy(float)
    invalid = (
        frame["datetime"].isna()
        | ~frame["product"].isin(STRESS90_POLICY.products)
        | frame["symbol"].isin({"", "NAN", "NONE"})
        | ~np.isfinite(numeric).all(axis=1)
        | (frame["open"] <= 0.0)
        | (frame["close"] <= 0.0)
        | (frame["volume"] < 0.0)
        | (frame["hold"] < 0.0)
    )
    if bool(invalid.any()):
        raise Stress90BootstrapError("60m evidence contains invalid rows")
    keys = ["datetime", "product", "symbol"]
    duplicates = frame[frame.duplicated(keys, keep=False)]
    if not duplicates.empty:
        values = ["open", "close", "volume", "hold"]
        conflicting = duplicates.groupby(keys, sort=False)[values].nunique(dropna=False).max(axis=1)
        if bool((conflicting > 1).any()):
            raise Stress90BootstrapError("60m evidence contains conflicting duplicate bars")
        frame = frame.drop_duplicates(keys, keep="first")
    return frame.sort_values(keys).reset_index(drop=True)


def _build_lagged_flow(
    bars: pd.DataFrame,
    targets: pd.DatetimeIndex,
    sessions: pd.DatetimeIndex,
) -> tuple[pd.DataFrame, pd.DatetimeIndex]:
    first_position = int(sessions.get_loc(targets[0]))
    source_days = pd.DatetimeIndex([sessions[first_position - 1], *targets[:-1]])
    raw_days = pd.DatetimeIndex(bars["datetime"].dt.normalize())
    supported = set(STRESS90_POLICY.oi_products)
    for source_day in source_days:
        received = set(bars.loc[raw_days == source_day, "product"])
        missing = sorted(supported - received)
        if missing:
            raise Stress90BootstrapError(
                f"60m OI coverage incomplete for {source_day.date()}: missing={missing}"
            )
    flow = build_daily_price_oi_flow(bars)
    for source_day in source_days:
        if source_day not in flow.index:
            raise Stress90BootstrapError(
                f"60m OI coverage did not produce completed flow: {source_day.date()}"
            )
        missing = sorted(supported - set(flow.columns[flow.loc[source_day].notna()]))
        if missing:
            raise Stress90BootstrapError(
                f"60m OI coverage incomplete for {source_day.date()}: missing={missing}"
            )
    lagged = lag_flow_to_target_days(
        flow,
        target_days=targets,
        products=STRESS90_POLICY.oi_products,
    )
    if lagged.isna().any().any():
        raise Stress90BootstrapError("60m OI coverage contains missing/incomplete target flow")
    return lagged.astype(float), source_days


def _max_abs_error(left: pd.DataFrame, right: pd.DataFrame) -> float:
    if (
        left.shape != right.shape
        or not left.index.equals(right.index)
        or not left.columns.equals(right.columns)
    ):
        return float("inf")
    if left.empty:
        return 0.0
    return float(np.max(np.abs(left.to_numpy(float) - right.to_numpy(float))))


def _verify_batch_adapters(
    path: Stress90CandidatePath,
    *,
    base: pd.DataFrame,
    close: pd.DataFrame,
    flow: pd.DataFrame,
) -> float:
    oi = apply_oi_confirmation_to_direction_changes(
        raw_weights=base,
        confirming_flow=flow,
        supported_products=STRESS90_POLICY.oi_products,
    )
    cost = apply_cost_aware_no_trade(weights=oi, close_prices=close)
    survivor = reallocate_survivors_lexicographically(
        original_weights=oi,
        approved_weights=cost,
    )
    errors = (
        _max_abs_error(path.oi_confirmed_weights, oi),
        _max_abs_error(path.cost_approved_weights, cost),
        _max_abs_error(path.survivor_weights, survivor),
    )
    maximum = max(errors)
    if not isfinite(maximum) or maximum > _LAYER_PARITY_ATOL:
        raise Stress90BootstrapError(
            f"batch/incremental candidate parity failed; max_abs_error={maximum:.17g}"
        )
    return maximum


def bootstrap_stress90(
    *,
    runtime_dir: str | Path,
    through_day: str,
    expectations: Stress90BootstrapExpectations = OFFICIAL_STRESS90_BOOTSTRAP_EXPECTATIONS,
    write_artifacts: bool = True,
) -> Stress90BootstrapResult:
    """Validate and replay fixed history, optionally creating immutable live artifacts."""

    runtime = canonical_file_path(Path(runtime_dir) / ".stress90-bootstrap-transition").parent
    if not write_artifacts:
        return _bootstrap_stress90(
            runtime=runtime,
            through_day=through_day,
            expectations=expectations,
            write_artifacts=False,
        )
    try:
        with durable_file_lock(runtime / ".stress90-bootstrap-transition"):
            return _bootstrap_stress90(
                runtime=runtime,
                through_day=through_day,
                expectations=expectations,
                write_artifacts=True,
            )
    except DurableFileError as exc:
        raise Stress90BootstrapError("Stress-90 bootstrap transition lock failed") from exc


def _bootstrap_stress90(
    *,
    runtime: Path,
    through_day: str,
    expectations: Stress90BootstrapExpectations,
    write_artifacts: bool,
) -> Stress90BootstrapResult:
    """Run with the canonical runtime transition lock already held for writes."""

    through_text, through = _canonical_through_day(through_day)
    official_profile = _is_official_profile(expectations)
    if write_artifacts and not official_profile:
        raise Stress90BootstrapError(
            "live seed/state creation requires the official immutable historical profile"
        )
    source_manifest = _verify_source_bytes(runtime, expectations)

    _continuous, open_prices, close_prices = _load_continuous_panels(
        runtime / "broad_daily_universe.csv"
    )
    archived = _load_archived_weights(runtime / "execution_aligned_weights.csv", through)
    targets = pd.DatetimeIndex(archived.index)
    _validate_target_continuity(targets, close_prices.index)
    specific_contracts = _validate_specific_contracts(
        runtime / "return_target_specific_contracts.csv", targets
    )

    rebuilt_full = ExecutionAlignedAggressivePolicy(STRESS90_POLICY.products).weight_history(
        open_prices, close_prices
    )
    rebuilt = rebuilt_full.reindex(index=targets, columns=STRESS90_POLICY.products)
    base_error = _max_abs_error(rebuilt, archived)
    if not isfinite(base_error) or base_error > _BASE_PARITY_ATOL:
        raise Stress90BootstrapError(f"base policy parity failed; max_abs_error={base_error:.17g}")

    bars = _load_60m(
        (
            runtime / "prior_two_year_broad_60m.csv",
            runtime / "two_year_broad_60m.csv",
        )
    )
    lagged, source_days = _build_lagged_flow(bars, targets, close_prices.index)
    path = build_stress90_candidate_path(
        base_weights=archived,
        completed_close_prices=close_prices,
        confirming_flow=lagged,
    )
    adapter_error = _verify_batch_adapters(
        path,
        base=archived,
        close=close_prices,
        flow=lagged,
    )
    digest = candidate_weight_digest(path.survivor_weights)
    if digest != expectations.candidate_weight_sha256:
        raise Stress90BootstrapError(
            "Stress-90 candidate SHA-256 mismatch; "
            f"expected={expectations.candidate_weight_sha256}, actual={digest}"
        )

    historical_parity = bool(
        official_profile and digest == STRESS90_POLICY.historical_candidate_weight_sha256
    )
    seed_path: Path | None = None
    state_path: Path | None = None
    oi_evidence_path: Path | None = None
    if write_artifacts:
        if not historical_parity:
            raise Stress90BootstrapError("historical candidate parity is required for live seed")
        seed_path = runtime / "stress90_bootstrap_seed.json"
        state_path = runtime / "stress90_policy_state.json"
        oi_evidence_path = runtime / "stress90_oi_evidence.json"
        ohlc_cache_path = runtime / "directional_ohlc_cache.json"
        activity_path = runtime / "directional_activity.json"
        ohlc_store = DirectionalOHLCCacheStore(ohlc_cache_path)
        activity_store = DirectionalActivityStore(activity_path)
        seed_store = Stress90SeedStore(seed_path)
        policy_store = Stress90PolicyStateStore(state_path)
        oi_store = Stress90OiEvidenceStore(oi_evidence_path)

        owned_paths = {
            "OHLC cache": {"current": ohlc_store.path},
            "activity": {"current": activity_store.path},
            "bootstrap seed": {"current": seed_store.path},
            "policy state": {
                "current": policy_store.path,
                "previous": policy_store.previous_path,
                "lock": policy_store.lock_path,
            },
            "OI evidence": {
                "current": oi_store.path,
                "previous": oi_store.previous_path,
                "lineage": oi_store.lineage_path,
                "lock": oi_store.lock_path,
            },
        }
        try:
            owned_snapshots = {
                path: _snapshot_owned_path(path)
                for paths in owned_paths.values()
                for path in paths.values()
            }
        except OSError as exc:
            raise Stress90BootstrapError(
                "failed to snapshot existing Stress-90 bootstrap artifacts"
            ) from exc

        for owner, paths in owned_paths.items():
            _require_supported_owned_paths(
                owner=owner,
                paths=paths,
                snapshots=owned_snapshots,
            )

        # OI is the bootstrap commit authority.  Inspect it first so an
        # ambiguous final write is reported as the durable OI incident rather
        # than as an ordinary prerequisite overwrite.
        try:
            existing_oi = oi_store.load_record()
        except OiEvidenceIntegrityError as exc:
            raise Stress90BootstrapError(f"existing Stress-90 OI evidence incident: {exc}") from exc
        if existing_oi is not None:
            raise Stress90BootstrapError(
                "Stress-90 bootstrap refuses to overwrite existing live OI evidence"
            )

        try:
            existing_ohlc = ohlc_store.load(STRESS90_POLICY.products)
        except DirectionalOHLCCacheIntegrityError as exc:
            raise Stress90BootstrapError(f"existing Stress-90 OHLC cache incident: {exc}") from exc
        if existing_ohlc is not None:
            raise Stress90BootstrapError(
                "Stress-90 bootstrap refuses to overwrite existing live OHLC cache"
            )

        try:
            activity_store.load_state()
        except DirectionalActivityIntegrityError as exc:
            raise Stress90BootstrapError(f"existing Stress-90 activity incident: {exc}") from exc
        if not owned_snapshots[activity_store.path].absent:
            raise Stress90BootstrapError(
                "Stress-90 bootstrap refuses to overwrite existing live activity"
            )

        if not owned_snapshots[seed_store.path].absent:
            try:
                seed_store.load_required(expected_source_manifest=source_manifest)
            except Stress90StateIntegrityError as exc:
                raise Stress90BootstrapError(
                    f"existing Stress-90 bootstrap seed incident: {exc}"
                ) from exc
            raise Stress90BootstrapError(
                "Stress-90 bootstrap refuses to overwrite existing live bootstrap seed"
            )

        try:
            existing_policy = policy_store.load_record()
        except Stress90StateIntegrityError as exc:
            raise Stress90BootstrapError(
                f"existing Stress-90 policy state incident: {exc}"
            ) from exc
        if existing_policy is not None:
            raise Stress90BootstrapError(
                "Stress-90 bootstrap refuses to overwrite existing live policy state"
            )
        through_rows = bars.loc[
            bars["datetime"].dt.normalize() == through,
            ["datetime", "product", "symbol", "open", "close", "volume", "hold"],
        ]
        try:
            bridge = build_fixed_historical_60m_evidence(
                through_text,
                through_rows.to_dict(orient="records"),
            )
        except OiEvidenceIntegrityError as exc:
            raise Stress90BootstrapError(str(exc)) from exc
        seed = Stress90BootstrapSeed.from_candidate_state(
            path.final_state,
            bootstrap_source_manifest=source_manifest,
            bootstrap_through_day=through_text,
            last_completed_input_day=source_days[-1].strftime("%Y%m%d"),
        )
        final_decision = path.decisions[-1]
        final_input_day = str(final_decision.input_days["completed_close"])
        final_target = pd.Timestamp(targets[-1])
        final_history = close_prices.loc[close_prices.index < final_target]
        final_inputs = Stress90DecisionInputs(
            previous_target_trading_day=final_input_day,
            target_trading_day=through_text,
            base_weights=archived.loc[final_target].to_dict(),
            completed_close_history={
                product: tuple(float(value) for value in final_history[product])
                for product in STRESS90_POLICY.products
            },
            completed_oi_flow={
                product: float(lagged.at[final_target, product])
                for product in STRESS90_POLICY.oi_products
            },
            completed_close_day=final_input_day,
            completed_oi_day=final_input_day,
        )
        prepared = Stress90PreparedDecision.from_decision(
            final_decision,
            previous_target_trading_day=final_input_day,
            source_input_digest=stress90_decision_inputs_digest(final_inputs),
        )
        completed_open = open_prices.loc[open_prices.index <= through]
        completed_close = close_prices.loc[close_prices.index <= through]
        activity = _through_day_activity_snapshot(specific_contracts, through)
        creation_tokens: list[DurableFileCreationToken] = []
        oi_phase_started = False
        try:
            created_ohlc, ohlc_token = ohlc_store.save_new(
                STRESS90_POLICY.products,
                completed_open,
                completed_close,
            )
            creation_tokens.append(ohlc_token)
            created_activity, activity_token = activity_store.save_new(activity)
            creation_tokens.append(activity_token)
            seed_token = seed_store.save_new(seed)
            creation_tokens.append(seed_token)
            created_policy, policy_token = policy_store.save_new(
                Stress90PolicyState.from_seed(seed, prepared_decision=prepared)
            )
            creation_tokens.append(policy_token)

            if not all(creation_token_matches(token) for token in creation_tokens):
                raise OSError("bootstrap prerequisite changed before OI commit")
            reloaded_ohlc = ohlc_store.load(STRESS90_POLICY.products)
            if (
                reloaded_ohlc is None
                or reloaded_ohlc.products != created_ohlc.products
                or reloaded_ohlc.content_digest != created_ohlc.content_digest
            ):
                raise OSError("bootstrap OHLC prerequisite changed before OI commit")
            if activity_store.load_state() != created_activity:
                raise OSError("bootstrap activity prerequisite changed before OI commit")
            if seed_store.load_required(expected_source_manifest=source_manifest) != seed:
                raise OSError("bootstrap seed prerequisite changed before OI commit")
            if policy_store.load_record() != created_policy:
                raise OSError("bootstrap policy prerequisite changed before OI commit")

            # OI lineage is the bootstrap commit point.  All prior sequence-1
            # artifacts remain independently rollback-safe until this final write.
            oi_phase_started = True
            oi_store.save_state(Stress90OiEvidenceState(completed=(bridge,)))
        except (
            OSError,
            DirectionalActivityIntegrityError,
            DirectionalOHLCCacheIntegrityError,
            OiEvidenceIntegrityError,
            Stress90StateIntegrityError,
        ) as exc:
            cleanup_errors: list[str] = []
            if not oi_phase_started:
                owner_tokens = (
                    ohlc_store.last_creation_token,
                    activity_store.last_creation_token,
                    seed_store.last_creation_token,
                    policy_store.last_creation_token,
                )
                known_tokens = {token.path for token in creation_tokens}
                creation_tokens.extend(
                    token
                    for token in owner_tokens
                    if token is not None and token.path not in known_tokens
                )
                for token in reversed(creation_tokens):
                    try:
                        if not unlink_created_file(token):
                            cleanup_errors.append(
                                f"{token.path}: creation token no longer matches current"
                            )
                    except OSError as cleanup_exc:
                        cleanup_errors.append(f"{token.path}: {cleanup_exc}")
            message = f"failed to persist Stress-90 bootstrap artifacts: {exc}"
            if cleanup_errors:
                message += "; rollback cleanup errors: " + "; ".join(cleanup_errors)
            raise Stress90BootstrapError(message) from exc

    return Stress90BootstrapResult(
        source_manifest=source_manifest,
        candidate_weight_sha256=digest,
        base_max_abs_error=base_error,
        batch_incremental_max_abs_error=adapter_error,
        target_day_count=len(targets),
        first_target_day=targets[0].strftime("%Y%m%d"),
        last_target_day=targets[-1].strftime("%Y%m%d"),
        last_input_day=source_days[-1].strftime("%Y%m%d"),
        policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
        historical_candidate_parity=historical_parity,
        seed_path=seed_path,
        state_path=state_path,
        oi_evidence_path=oi_evidence_path,
        candidate_path=path,
    )
