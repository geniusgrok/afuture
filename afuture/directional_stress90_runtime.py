"""Production runtime adapter for the explicitly selected Stress-90 policy."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from math import isfinite
from pathlib import Path

import pandas as pd

from .auto_runtime import MetadataPrefetcher
from .directional_activity import (
    select_contracts_from_activity,
    validate_directional_activity_snapshot,
)
from .directional_ohlc_cache import DirectionalOHLCCacheStore
from .directional_ohlc_refresh import load_stress90_completed_ohlc
from .directional_risk import DirectionalRiskResponseMode
from .directional_runtime import DirectionalActionResult
from .directional_sessions import (
    PRODUCT_SESSION_MANIFEST,
    OpeningWindowStatus,
    opening_window_status,
)
from .directional_stress90_execution import (
    Stress90ExecutionIntentStore,
    prepare_stress90_execution_intent,
)
from .directional_stress90_oi_runtime import (
    Stress90OiEvidenceAggregator,
    Stress90OiEvidenceStore,
)
from .directional_stress90_planner import (
    Stress90LotStages,
    build_stress90_rebalance_stages,
)
from .directional_stress90_policy import STRESS90_POLICY
from .directional_stress90_state import (
    Stress90DecisionInputs,
    Stress90PolicyStateStore,
    Stress90SeedStore,
    drawdown_reserve_triggered_from_state,
    prepare_stress90_decision,
    record_completed_account_day,
)
from .execution_aligned_policy import FROZEN_PRODUCTS, ExecutionAlignedAggressivePolicy
from .execution_aligned_runtime import ExecutionAlignedDirectionalPortfolioManager
from .metadata import validate_contract_metadata
from .models import Offset, Order, OrderRequest, OrderSide
from .position import PositionBook


class Stress90DataUnavailableError(RuntimeError):
    """Verified market evidence cannot cover a required completed trading day."""


@dataclass(frozen=True)
class Stress90AccountDayContinuityEvidence:
    """Exact immutable market-session identity for one account settlement rollover."""

    completed_account_day: str
    current_ctp_trading_day: str
    ohlc_content_digest: str
    oi_store_checksum: str
    completed_oi_evidence_digest: str
    observed_transition_digest: str
    continuity_digest: str


def load_stress90_account_day_continuity_evidence(
    ohlc_store: DirectionalOHLCCacheStore,
    oi_store: Stress90OiEvidenceStore,
    *,
    completed_account_day: str,
    current_ctp_trading_day: str,
) -> Stress90AccountDayContinuityEvidence:
    """Load and identify the exact verified market-session chain for one rollover."""

    previous = _trading_day(
        completed_account_day,
        name="completed account trading day",
    )
    current = _trading_day(
        current_ctp_trading_day,
        name="current account trading day",
    )
    if current <= previous:
        raise RuntimeError("completed account session continuity is unavailable")
    entry = load_stress90_completed_ohlc(
        ohlc_store,
        products=STRESS90_POLICY.products,
        current_ctp_trading_day=current,
        required_completed_day=previous,
    )
    if stress90_target_transitions(
        last_completed_target_day=previous,
        current_ctp_trading_day=current,
        completed_close_index=entry.close.index,
    ) != ((previous, current),):
        raise RuntimeError("completed account OHLC session chain is not contiguous")
    oi_record = oi_store.load_required_record()
    evidence = tuple(
        item for item in oi_record.state.completed if previous <= item.trading_day < current
    )
    if len(evidence) != 1 or evidence[0].trading_day != previous:
        raise RuntimeError("completed account OI session chain is not contiguous")
    completed = evidence[0]
    if (
        not completed.complete
        or set(completed.flows) != set(STRESS90_POLICY.oi_products)
        or any(value not in (-1, 0, 1) for value in completed.flows.values())
    ):
        raise RuntimeError("completed account OI evidence is incomplete")
    transitions = tuple(
        item
        for item in oi_record.state.observed_transitions
        if item.source_trading_day == previous and item.target_trading_day == current
    )
    if len(transitions) != 1:
        raise RuntimeError("completed account CTP rollover was not continuously observed")
    transition = transitions[0]
    if transition.completed_oi_evidence_digest != completed.evidence_digest:
        raise RuntimeError("completed account CTP rollover evidence digest mismatch")
    transition_payload = {
        "source_trading_day": transition.source_trading_day,
        "target_trading_day": transition.target_trading_day,
        "completed_oi_evidence_digest": transition.completed_oi_evidence_digest,
    }
    observed_transition_digest = sha256(
        json.dumps(
            transition_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    identity = {
        "kind": "afuture.stress90.account-day-continuity",
        "version": 1,
        "completed_account_day": previous,
        "current_ctp_trading_day": current,
        "ohlc_content_digest": entry.content_digest,
        "oi_store_checksum": oi_record.checksum,
        "completed_oi_evidence_digest": completed.evidence_digest,
        "observed_transition_digest": observed_transition_digest,
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return Stress90AccountDayContinuityEvidence(
        completed_account_day=previous,
        current_ctp_trading_day=current,
        ohlc_content_digest=entry.content_digest,
        oi_store_checksum=oi_record.checksum,
        completed_oi_evidence_digest=completed.evidence_digest,
        observed_transition_digest=observed_transition_digest,
        continuity_digest=sha256(encoded).hexdigest(),
    )


def _validate_hard_risk_envelope(config, risk_config) -> None:
    limits = STRESS90_POLICY
    violations: list[str] = []
    upper_bounds = (
        ("directional.max_gross_leverage", config.max_gross_leverage, limits.max_gross_leverage),
        ("directional.max_contract_volume", config.max_contract_volume, limits.max_contract_lots),
        ("risk.max_contract_volume", risk_config.max_contract_volume, limits.max_contract_lots),
        ("risk.max_margin_ratio", risk_config.max_margin_ratio, limits.max_margin_ratio),
        (
            "risk.max_daily_loss_ratio",
            risk_config.max_daily_loss_ratio,
            limits.daily_loss_ratio,
        ),
        (
            "risk.max_total_drawdown_ratio",
            risk_config.max_total_drawdown_ratio,
            limits.hard_drawdown_ratio,
        ),
    )
    for name, configured, maximum in upper_bounds:
        if float(configured) > float(maximum) + 1e-15:
            violations.append(name)
    if risk_config.min_available_ratio + 1e-15 < limits.min_available_ratio:
        violations.append("risk.min_available_ratio")
    if risk_config.margin_estimate_buffer + 1e-15 < limits.margin_estimate_buffer:
        violations.append("risk.margin_estimate_buffer")
    if violations:
        raise ValueError("Stress-90 hard risk envelope cannot be loosened: " + ",".join(violations))


def _trading_day(raw: str, *, name: str) -> str:
    if not isinstance(raw, str):
        raise RuntimeError(f"{name} must be YYYYMMDD")
    try:
        parsed = pd.Timestamp(raw).strftime("%Y%m%d")
    except Exception as exc:
        raise RuntimeError(f"{name} must be YYYYMMDD") from exc
    if len(raw) != 8 or parsed != raw:
        raise RuntimeError(f"{name} must be YYYYMMDD")
    return raw


def stress90_target_transitions(
    *,
    last_completed_target_day: str,
    current_ctp_trading_day: str,
    completed_close_index: pd.DatetimeIndex,
) -> tuple[tuple[str, str], ...]:
    """Derive target gaps only from verified observed sessions plus current CTP day."""

    previous = _trading_day(last_completed_target_day, name="last Stress-90 target day")
    current = _trading_day(current_ctp_trading_day, name="current CTP trading day")
    if current < previous:
        raise RuntimeError("current CTP trading day moved backward")
    index = pd.DatetimeIndex(completed_close_index)
    if index.tz is not None or not index.equals(index.normalize()):
        raise RuntimeError("completed OHLC sessions are invalid")
    if index.has_duplicates or not index.is_monotonic_increasing:
        raise RuntimeError("completed OHLC sessions are not unique/increasing")
    previous_timestamp = pd.Timestamp(f"{previous[:4]}-{previous[4:6]}-{previous[6:]}")
    current_timestamp = pd.Timestamp(f"{current[:4]}-{current[4:6]}-{current[6:]}")
    if previous_timestamp not in index:
        raise RuntimeError("completed OHLC does not cover prior target session")
    if current == previous:
        return ()
    if bool((index >= current_timestamp).any()):
        raise RuntimeError("OHLC cache contains a non-completed current/future session")
    intermediate = [
        item.strftime("%Y%m%d") for item in index if previous_timestamp < item < current_timestamp
    ]
    targets = (*intermediate, current)
    result: list[tuple[str, str]] = []
    source = previous
    for target in targets:
        result.append((source, target))
        source = target
    return tuple(result)


class _CacheOnlySignalProvider:
    def load(self, products):
        del products
        raise RuntimeError("Stress-90 order path is cache-only")


class Stress90DirectionalPortfolioManager(ExecutionAlignedDirectionalPortfolioManager):
    """Independent adapter; candidate targets remain 1x and soft defenses freeze only risk."""

    policy_risk_response_mode = DirectionalRiskResponseMode.FREEZE_NEW_RISK
    runtime_policy_id = STRESS90_POLICY.policy_id
    runtime_policy_definition_digest = STRESS90_POLICY.policy_definition_digest
    runtime_products_manifest_digest = STRESS90_POLICY.products_manifest_digest

    def runtime_bootstrap_seed_digest(self) -> str:
        return self.seed_store.load_required().seed_digest

    def __init__(
        self,
        config,
        broker,
        risk_manager,
        *,
        policy_state_path: str | Path,
        seed_path: str | Path,
        oi_evidence_path: str | Path,
        execution_intent_path: str | Path | None = None,
        **kwargs,
    ) -> None:
        configured = tuple(sorted({str(item).upper() for item in config.products}))
        if configured != FROZEN_PRODUCTS:
            raise ValueError("Stress-90 production requires the frozen 50-product universe")
        if (config.policy or "stress90") != "stress90":
            raise ValueError("Stress-90 manager policy identity mismatch")
        _validate_hard_risk_envelope(config, risk_manager.config)
        self.policy_state_store = Stress90PolicyStateStore(policy_state_path)
        self.seed_store = Stress90SeedStore(seed_path)
        self.execution_intent_store = Stress90ExecutionIntentStore(
            execution_intent_path
            or Path(policy_state_path).with_name("stress90_execution_intent.json")
        )
        self.oi_evidence_store = Stress90OiEvidenceStore(oi_evidence_path)
        self.oi_evidence = Stress90OiEvidenceAggregator(store=self.oi_evidence_store)
        super().__init__(
            config,
            broker,
            risk_manager,
            signal_provider=_CacheOnlySignalProvider(),
            policy=ExecutionAlignedAggressivePolicy(products=configured),
            raw_tick_observer=self.oi_evidence,
            **kwargs,
        )
        self._configured_specs = dict(self._specs)
        self._metadata_prefetcher = MetadataPrefetcher(self.metadata_timeout_seconds)
        self._stress90_metadata_trading_day: str | None = None

    def bootstrap(self, now) -> None:
        record = self.policy_state_store.load_required_record()
        seed = self.seed_store.load_required()
        state = record.state
        if state.bootstrap_seed_digest != seed.seed_digest:
            raise RuntimeError("Stress-90 policy state/bootstrap seed identity mismatch")
        if state.bootstrap_source_manifest != seed.bootstrap_source_manifest:
            raise RuntimeError("Stress-90 bootstrap source manifest mismatch")
        raw_account_identity = (
            self.broker.get_account_identity_digest()
            if callable(getattr(self.broker, "get_account_identity_digest", None))
            else ""
        )
        account_identity = raw_account_identity if isinstance(raw_account_identity, str) else ""
        if account_identity and (
            not state.live_account_identity_digest
            or state.live_account_identity_digest != account_identity
        ):
            raise RuntimeError("Stress-90 policy/Broker account identity mismatch")
        current = _trading_day(self.broker.get_trading_day(), name="current CTP trading day")
        if current < state.last_completed_target_day:
            raise RuntimeError("current CTP trading day moved backward")
        super().bootstrap(now)

    def close(self) -> None:
        try:
            self._metadata_prefetcher.close()
        finally:
            super().close()

    def completed_account_day_is_contiguous(self, old_day: str, new_day: str) -> bool:
        """Prove one account rollover from the same complete OHLC/OI session chain."""

        try:
            self.completed_account_day_continuity_evidence(old_day, new_day)
            return True
        except (RuntimeError, TypeError, ValueError):
            return False

    def completed_account_day_continuity_evidence(
        self,
        old_day: str,
        new_day: str,
    ) -> Stress90AccountDayContinuityEvidence:
        """Return the exact OHLC/OI evidence digest for one authoritative transition."""

        if self._ohlc_cache is None:
            raise RuntimeError("completed account session continuity is unavailable")
        return load_stress90_account_day_continuity_evidence(
            self._ohlc_cache,
            self.oi_evidence_store,
            completed_account_day=old_day,
            current_ctp_trading_day=new_day,
        )

    def _ensure_specs(self, symbols: set[str]):
        """Read live CTP specs only from a background-refreshed, day-scoped cache."""

        blocks = bool(getattr(self.broker, "metadata_query_blocks", True))
        if not blocks:
            return super()._ensure_specs(symbols)
        day = _trading_day(self.broker.get_trading_day(), name="metadata CTP trading day")
        if day != self._stress90_metadata_trading_day:
            self._metadata_prefetcher.invalidate()
            self._specs.clear()
            self._stress90_metadata_trading_day = day
        requested = tuple(sorted(set(symbols)))
        self._metadata_prefetcher.request(self.broker, requested)
        rows = self._metadata_prefetcher.get(requested)
        if rows is None:
            error = self._metadata_prefetcher.error(requested)
            detail = f": {error}" if error else ""
            raise RuntimeError("background live metadata is not ready" + detail)
        self_check = validate_contract_metadata(dict(rows), dict(rows))
        if not self_check.allowed:
            raise RuntimeError(f"background live metadata is invalid: {self_check.reason}")
        configured = {
            symbol: self._configured_specs[symbol]
            for symbol in requested
            if symbol in self._configured_specs
        }
        if configured:
            configured_check = validate_contract_metadata(configured, dict(rows))
            if not configured_check.allowed:
                raise RuntimeError("configured/live metadata mismatch: " + configured_check.reason)
        self._specs.update(rows)
        return {symbol: self._specs[symbol] for symbol in requested}

    def _base_weights_for_transition(
        self,
        entry,
        *,
        completed_input_day: str,
        target_trading_day: str,
    ) -> dict[str, float]:
        input_day = _trading_day(completed_input_day, name="completed Stress-90 input day")
        target_day = _trading_day(target_trading_day, name="authoritative Stress-90 target day")
        input_timestamp = pd.Timestamp(f"{input_day[:4]}-{input_day[4:6]}-{input_day[6:]}")
        target_timestamp = pd.Timestamp(f"{target_day[:4]}-{target_day[4:6]}-{target_day[6:]}")
        if target_timestamp <= input_timestamp:
            raise RuntimeError("Stress-90 target must follow its completed input day")
        open_history = entry.open.loc[entry.open.index <= input_timestamp].copy()
        close_history = entry.close.loc[entry.close.index <= input_timestamp].copy()
        if input_timestamp not in close_history.index:
            raise RuntimeError("OHLC cache does not cover Stress-90 completed input day")
        if len(close_history) < 140 or not open_history.index.equals(close_history.index):
            raise RuntimeError("Stress-90 completed OHLC history is incomplete")
        synthetic_open = close_history.iloc[[-1]].copy()
        synthetic_close = close_history.iloc[[-1]].copy()
        synthetic_open.index = pd.DatetimeIndex([target_timestamp])
        synthetic_close.index = pd.DatetimeIndex([target_timestamp])
        weights = self.policy.target_weights(
            pd.concat([open_history, synthetic_open]),
            pd.concat([close_history, synthetic_close]),
        )
        normalized = {str(product).upper(): float(value) for product, value in weights.items()}
        if set(normalized) != set(STRESS90_POLICY.products):
            raise RuntimeError("Stress-90 Base target product manifest mismatch")
        gross = sum(abs(value) for value in normalized.values())
        if gross > STRESS90_POLICY.max_gross_leverage + 1e-10:
            raise RuntimeError("Stress-90 Base target exceeds 2x gross")
        return {product: normalized[product] for product in STRESS90_POLICY.products}

    def _prepare_decision_for_current_day(
        self,
        current_ctp_trading_day: str,
        *,
        required_activity_day: str | None = None,
    ):
        current = _trading_day(current_ctp_trading_day, name="current CTP trading day")
        record = self.policy_state_store.load_required_record()
        state = record.state
        if current < state.last_completed_target_day:
            raise RuntimeError("current CTP trading day moved backward")
        if current == state.last_completed_target_day:
            prepared = state.prepared_decision
            if prepared is None or prepared.target_trading_day != current:
                raise RuntimeError("Stress-90 current target has no prepared decision")
            return prepared

        if self._ohlc_cache is None:
            raise Stress90DataUnavailableError("verified Stress-90 OHLC cache is not configured")
        try:
            entry = load_stress90_completed_ohlc(
                self._ohlc_cache,
                products=STRESS90_POLICY.products,
                current_ctp_trading_day=current,
                required_completed_day=state.last_completed_target_day,
            )
            transitions = stress90_target_transitions(
                last_completed_target_day=state.last_completed_target_day,
                current_ctp_trading_day=current,
                completed_close_index=entry.close.index,
            )
            if (
                required_activity_day is not None
                and transitions
                and transitions[-1][0] != required_activity_day
            ):
                raise RuntimeError("completed directional activity is stale for Stress-90 target")
        except RuntimeError as exc:
            if "moved backward" in str(exc):
                raise
            raise Stress90DataUnavailableError(str(exc)) from exc
        try:
            oi_record = self.oi_evidence_store.load_required_record()
        except RuntimeError as exc:
            raise Stress90DataUnavailableError(
                f"verified Stress-90 OI evidence is unavailable: {exc}"
            ) from exc
        oi_by_day = {item.trading_day: item for item in oi_record.state.completed}
        transition_input_days = {input_day for input_day, _target_day in transitions}
        completed_oi_days = {
            day for day in oi_by_day if state.last_completed_target_day <= day < current
        }
        if completed_oi_days != transition_input_days:
            missing_ohlc_sessions = sorted(completed_oi_days - transition_input_days)
            missing_oi_sessions = sorted(transition_input_days - completed_oi_days)
            raise Stress90DataUnavailableError(
                "verified OHLC/OI session calendars do not align; "
                f"OHLC_missing={missing_ohlc_sessions}, OI_missing={missing_oi_sessions}"
            )
        prepared = None
        for input_day, target_day in transitions:
            evidence = oi_by_day.get(input_day)
            if evidence is None or not evidence.complete:
                raise Stress90DataUnavailableError(
                    f"completed Stress-90 OI evidence is missing/incomplete: {input_day}"
                )
            observed = tuple(
                item
                for item in oi_record.state.observed_transitions
                if item.source_trading_day == input_day
                and item.target_trading_day == target_day
            )
            if (
                len(observed) != 1
                or observed[0].completed_oi_evidence_digest != evidence.evidence_digest
            ):
                raise Stress90DataUnavailableError(
                    "authoritative CTP trading-day rollover evidence is missing or mismatched: "
                    f"{input_day}->{target_day}"
                )
            flows = {product: evidence.flows[product] for product in STRESS90_POLICY.oi_products}
            if any(value not in (-1, 0, 1) for value in flows.values()):
                raise Stress90DataUnavailableError(
                    f"completed Stress-90 OI flow is missing/incomplete: {input_day}"
                )
            input_timestamp = pd.Timestamp(f"{input_day[:4]}-{input_day[4:6]}-{input_day[6:]}")
            completed_close = entry.close.loc[entry.close.index <= input_timestamp]
            if input_timestamp not in completed_close.index:
                raise Stress90DataUnavailableError(
                    f"verified OHLC is missing Stress-90 input day: {input_day}"
                )
            base_weights = self._base_weights_for_transition(
                entry,
                completed_input_day=input_day,
                target_trading_day=target_day,
            )
            prepared = prepare_stress90_decision(
                self.policy_state_store,
                Stress90DecisionInputs(
                    previous_target_trading_day=input_day,
                    target_trading_day=target_day,
                    base_weights=base_weights,
                    completed_close_history={
                        product: tuple(float(value) for value in completed_close[product])
                        for product in STRESS90_POLICY.products
                    },
                    completed_oi_flow=flows,
                    completed_close_day=input_day,
                    completed_oi_day=input_day,
                ),
            )
        if prepared is None:
            raise RuntimeError("Stress-90 target transition was not prepared")
        return prepared

    def _entry_session_windows(self, product: str) -> tuple[str, ...]:
        normalized = str(product).upper()
        try:
            return (PRODUCT_SESSION_MANIFEST[normalized].first_entry_window,)
        except KeyError as exc:  # pragma: no cover - frozen manifest invariant
            raise RuntimeError(f"Stress-90 product session is missing: {product}") from exc

    def _opening_policy_rejection(
        self,
        account,
        positions,
        requests,
        specs,
        now,
    ) -> str:
        """Recheck marked post-open gross from the same fresh account used by RiskManager."""

        del now
        try:
            account.validate()
            target = self._current_lots(positions)
        except (TypeError, ValueError, RuntimeError):
            return "Stress-90 opening gross evidence is invalid"
        for request in requests:
            if request.offset.value != "OPEN":
                continue
            delta = int(request.volume) if request.side.value == "BUY" else -int(request.volume)
            current = int(target.get(request.symbol, 0))
            if current and (current > 0) != (delta > 0):
                return "Stress-90 opening batch bypassed reduction-first"
            target[request.symbol] = current + delta
        if account.equity <= 0.0:
            return "Stress-90 opening gross requires positive equity"
        gross_notional = 0.0
        for symbol, volume in target.items():
            if not volume:
                continue
            tick = self._ticks.get(symbol)
            spec = specs.get(symbol)
            if tick is None or spec is None:
                return f"Stress-90 opening gross evidence is missing: {symbol}"
            lot_notional = float(tick.mid_price) * float(spec.multiplier)
            if lot_notional <= 0.0:
                return f"Stress-90 opening gross evidence is invalid: {symbol}"
            gross_notional += abs(int(volume)) * lot_notional
        if gross_notional > (float(account.equity) * float(self.config.max_gross_leverage) + 1e-10):
            return "Stress-90 opening batch would exceed configured gross limit"
        return ""

    def record_completed_account_return(
        self,
        trading_day: str,
        daily_return: float,
        *,
        completed_equity: float | None = None,
    ) -> bool:
        """Persist one completed Broker day before generic runtime state advances."""

        record = self.policy_state_store.load_required_record()
        state = record.state
        inception_segment = bool(
            state.live_inception_day == trading_day
            and (
                state.last_completed_account_day is None
                or (
                    state.last_completed_account_day == trading_day
                    and not state.recent_daily_returns_for_adaptive_margin
                )
            )
        )
        effective_return = float(daily_return)
        if inception_segment:
            inception_equity = state.live_inception_equity
            if (
                inception_equity is None
                or completed_equity is None
                or not isfinite(float(completed_equity))
                or float(completed_equity) <= 0.0
            ):
                raise RuntimeError(
                    "completed live inception equity evidence is unavailable"
                )
            effective_return = float(completed_equity) / float(inception_equity) - 1.0
        if state.last_completed_account_day == trading_day:
            persisted_return = (
                state.completed_account_wealth - 1.0
                if inception_segment
                else state.recent_daily_returns_for_adaptive_margin[-1]
            )
            if abs(float(persisted_return) - effective_return) > 1e-15:
                raise RuntimeError("same completed account day conflicts with persisted return")
            return not inception_segment
        updated = record_completed_account_day(
            state,
            trading_day,
            effective_return,
            include_adaptive_margin=not inception_segment,
        )
        self.policy_state_store.save(updated, expected_sequence=record.sequence)
        return not inception_segment

    @staticmethod
    def _current_lots(positions) -> dict[str, int]:
        result: dict[str, int] = {}
        for position in positions:
            if position.empty:
                continue
            if position.long_total > 0 and position.short_total > 0:
                raise RuntimeError(
                    f"opposing Broker position cannot enter Stress-90 planner: {position.symbol}"
                )
            result[position.symbol] = int(position.net_volume)
        return result

    def _data_unavailable_result(self, reason: str) -> DirectionalActionResult:
        exposed = any(not position.empty for position in self.broker.get_positions())
        return DirectionalActionResult(
            "risk_off" if exposed else "reject",
            f"Stress-90 data unavailable: {reason}",
        )

    def _plan_lot_stages(
        self,
        *,
        account,
        prepared,
        product_ticks,
        specs,
        current_lots,
        symbol_products,
        unavailable_products,
        authorized_transition_products=(),
        authorized_transition_kinds=None,
        authorized_transition_max_replacement_notionals=None,
        persisted_margin_fitted_lots=None,
        persisted_freeze_authorized_lots=None,
        entry_blocked_products=(),
    ) -> Stress90LotStages:
        state = self.policy_state_store.load_required()
        return build_stress90_rebalance_stages(
            account=account,
            product_weights=prepared.survivor_weights,
            product_ticks=product_ticks,
            specs=specs,
            incumbent_ticks={
                symbol: self._ticks[symbol] for symbol in current_lots if symbol in self._ticks
            },
            current_lots=current_lots,
            symbol_products=symbol_products,
            max_contract_volume=min(
                self.config.max_contract_volume,
                self.risk_manager.config.max_contract_volume,
            ),
            max_gross_leverage=self.config.max_gross_leverage,
            max_margin_ratio=self.risk_manager.config.max_margin_ratio,
            min_available_ratio=self.risk_manager.config.min_available_ratio,
            max_daily_loss_ratio=self.risk_manager.config.max_daily_loss_ratio,
            margin_estimate_buffer=self.risk_manager.config.margin_estimate_buffer,
            completed_returns=state.recent_daily_returns_for_adaptive_margin,
            drawdown_reserve_freeze=drawdown_reserve_triggered_from_state(state),
            concentration_freeze=bool(prepared.concentration_freeze),
            unavailable_products=unavailable_products,
            entry_blocked_products=entry_blocked_products,
            authorized_transition_products=authorized_transition_products,
            authorized_transition_kinds=authorized_transition_kinds,
            authorized_transition_max_replacement_notionals=(
                authorized_transition_max_replacement_notionals
            ),
            persisted_margin_fitted_lots=persisted_margin_fitted_lots,
            persisted_freeze_authorized_lots=persisted_freeze_authorized_lots,
        )

    def _record_stress90_quality(self, prepared, stages: Stress90LotStages) -> None:
        if self.quality is None:
            return
        has_decision = getattr(self.quality, "has_stress90_decision", None)
        record_decision = getattr(self.quality, "record_stress90_decision", None)
        if not callable(has_decision) or not callable(record_decision):
            raise RuntimeError("Stress-90 quality recorder lacks decision audit capability")
        if has_decision(prepared.daily_decision_digest):
            return
        state = self.policy_state_store.load_required()
        completed_drawdown = max(
            0.0,
            1.0
            - float(state.completed_account_wealth) / float(state.completed_account_high_watermark),
        )
        record_decision(
            target_trading_day=prepared.target_trading_day,
            stress90_base_target=dict(prepared.base_weights),
            stress90_oi_target=dict(prepared.oi_confirmed_weights),
            stress90_cost_approved_target=dict(prepared.cost_approved_weights),
            stress90_survivor_target=dict(prepared.survivor_weights),
            stress90_hhi=prepared.current_hhi,
            stress90_prior_hhi_median=prepared.prior_hhi_median,
            stress90_concentration_freeze=prepared.concentration_freeze,
            stress90_completed_drawdown=completed_drawdown,
            stress90_drawdown_reserve_freeze=drawdown_reserve_triggered_from_state(state),
            stress90_raw_integer_target=stages.raw_integer_lots,
            stress90_margin_fitted_target=stages.margin_fitted_lots,
            stress90_drawdown_frozen_target=stages.drawdown_frozen_lots,
            stress90_hhi_frozen_target=stages.hhi_frozen_lots,
            stress90_integer_target=stages.margin_fitted_lots,
            stress90_final_frozen_target=stages.final_frozen_lots,
            stress90_reduction_plan=stages.reductions,
            stress90_opening_plan=stages.openings,
            stress90_decision_digest=prepared.daily_decision_digest,
        )

    def maybe_rebalance(self, now: datetime) -> DirectionalActionResult:
        """Converge Broker truth to one already-durable Stress-90 daily decision."""

        if not self._initialized:
            return DirectionalActionResult("reject", "directional manager is not initialized")
        if not self.broker.is_ready():
            return DirectionalActionResult("reject", "broker is not ready")
        current = _trading_day(self.broker.get_trading_day(), name="current CTP trading day")
        state_record = self.policy_state_store.load_required_record()
        raw_account_identity = (
            self.broker.get_account_identity_digest()
            if callable(getattr(self.broker, "get_account_identity_digest", None))
            else ""
        )
        account_identity = raw_account_identity if isinstance(raw_account_identity, str) else ""
        if (
            not account_identity
            or state_record.state.live_account_identity_digest != account_identity
            or state_record.state.live_account_epoch is None
        ):
            raise RuntimeError("Stress-90 policy/Broker account lifecycle identity mismatch")
        account_epoch = state_record.state.live_account_epoch

        def intent_matches_account(intent) -> bool:
            return bool(
                intent.account_identity_digest == account_identity
                and intent.account_epoch == account_epoch
            )

        active_orders = self.broker.get_active_orders()
        if active_orders:
            owns_order = getattr(self.broker, "owns_order", None)
            identity_getter = getattr(self.broker, "get_order_submission_identity", None)
            intent_record = self.execution_intent_store.load_record()
            if intent_record is not None and intent_record.retired:
                intent_record = None
            for order in active_orders:
                if (
                    not isinstance(order, Order)
                    or not order.active
                    or not callable(owns_order)
                    or not owns_order(order.order_id)
                ):
                    raise RuntimeError("unknown active order detected during Stress-90 replay")
                if callable(identity_getter):
                    identity = identity_getter(order.order_id)
                    authorization_kind = getattr(identity, "authorization_kind", "")
                    common_identity_matches = bool(
                        identity is not None
                        and getattr(identity, "status", None) in {"prepared", "submitted"}
                        and getattr(identity, "target_trading_day", None) == current
                        and getattr(identity, "policy_id", None) == self.runtime_policy_id
                        and getattr(identity, "policy_definition_digest", None)
                        == self.runtime_policy_definition_digest
                        and getattr(identity, "products_manifest_digest", None)
                        == self.runtime_products_manifest_digest
                        and isinstance(getattr(identity, "request", None), OrderRequest)
                        and identity.request == order.request
                    )
                    candidate_matches = bool(
                        authorization_kind == "candidate"
                        and intent_record is not None
                        and intent_matches_account(intent_record.intent)
                        and intent_record.intent.target_trading_day == current
                        and getattr(identity, "daily_decision_digest", None)
                        == intent_record.intent.daily_decision_digest
                        and getattr(identity, "execution_intent_digest", None)
                        == intent_record.intent.source_digest
                    )
                    reduction_matches = bool(
                        authorization_kind == "risk_reduction"
                        and order.request.offset is not Offset.OPEN
                    )
                    if not common_identity_matches or not (candidate_matches or reduction_matches):
                        raise RuntimeError("unknown active order detected during Stress-90 replay")
                elif (
                    intent_record is None
                    or not intent_matches_account(intent_record.intent)
                    or intent_record.intent.target_trading_day != current
                    or not order.request.reference.startswith(
                        "directional:stress90:" + intent_record.intent.daily_decision_digest[:12]
                    )
                ):
                    raise RuntimeError("unknown active order detected during Stress-90 replay")
            return DirectionalActionResult(
                "wait", "active orders must settle before Stress-90 rebalance"
            )

        unresolved_getter = getattr(self.broker, "get_unresolved_order_submission_identities", None)
        if callable(unresolved_getter):
            unresolved = tuple(unresolved_getter())
            if unresolved:
                intent_record = self.execution_intent_store.load_record()
                if intent_record is not None and intent_record.retired:
                    intent_record = None
                if any(
                    entry.target_trading_day != current
                    or entry.policy_id != self.runtime_policy_id
                    or entry.policy_definition_digest != self.runtime_policy_definition_digest
                    or entry.products_manifest_digest != self.runtime_products_manifest_digest
                    or (
                        entry.authorization_kind == "candidate"
                        and (
                            intent_record is None
                            or not intent_matches_account(intent_record.intent)
                            or entry.daily_decision_digest
                            != intent_record.intent.daily_decision_digest
                            or entry.execution_intent_digest != intent_record.intent.source_digest
                        )
                    )
                    or (
                        entry.authorization_kind == "risk_reduction"
                        and entry.request.offset is Offset.OPEN
                    )
                    or entry.authorization_kind not in {"candidate", "risk_reduction"}
                    for entry in unresolved
                ):
                    raise RuntimeError(
                        "unresolved CTP order submission does not match current Stress-90 intent"
                    )
                return DirectionalActionResult(
                    "wait",
                    "unresolved exact CTP order submission must replay before resubmission",
                )

        if current < state_record.state.last_completed_target_day:
            raise RuntimeError("current CTP trading day moved backward")
        if (
            current == state_record.state.bootstrap_through_day
            and current == state_record.state.last_completed_target_day
            and state_record.state.prepared_decision is None
        ):
            return DirectionalActionResult(
                "hold",
                "Stress-90 bootstrap target is already completed; awaiting next target day",
            )

        snapshot = self.activity_tracker.completed_snapshot if self.activity_tracker else None
        if snapshot is None:
            return self._data_unavailable_result("completed directional activity is unavailable")
        try:
            validate_directional_activity_snapshot(snapshot)
        except (TypeError, ValueError) as exc:
            return self._data_unavailable_result(str(exc))
        if snapshot.trading_day >= current:
            return self._data_unavailable_result(
                "completed directional activity is not strictly prior to target day"
            )
        try:
            prepared = self._prepare_decision_for_current_day(
                current,
                required_activity_day=snapshot.trading_day,
            )
        except Stress90DataUnavailableError as exc:
            return self._data_unavailable_result(str(exc))
        if prepared.previous_target_trading_day != snapshot.trading_day:
            return self._data_unavailable_result(
                "completed directional activity does not match decision input day"
            )

        # Broker account, position, quote and contract truth is deliberately read only
        # after the immutable candidate decision has reached durable storage.
        positions = self.broker.get_positions()
        current_lots = self._current_lots(positions)
        catalog_by_symbol = {item.symbol: item for item in self._catalog}
        unknown_positions = sorted(set(current_lots) - set(catalog_by_symbol))
        if unknown_positions:
            raise RuntimeError(
                "Broker position is absent from reconciled contract catalog: "
                + ",".join(unknown_positions)
            )
        preferred_symbols = {
            catalog_by_symbol[symbol].product.upper(): symbol for symbol in current_lots
        }
        planned_date = datetime.strptime(current, "%Y%m%d").date()
        selected = select_contracts_from_activity(
            self.config,
            self._catalog,
            snapshot,
            planned_date,
            preferred_symbols=preferred_symbols,
        )
        required_products = {
            product
            for product, weight in prepared.survivor_weights.items()
            if abs(float(weight)) > 1e-15
        }
        available_products = {
            product
            for product in required_products
            if product in selected and selected[product].symbol in self._ticks
        }
        unavailable_products = required_products - available_products
        symbols = set(current_lots) | {selected[product].symbol for product in available_products}
        missing_position_quotes = sorted(set(current_lots) - set(self._ticks))
        if missing_position_quotes:
            return self._data_unavailable_result(
                "Broker positions have no current quote: " + ",".join(missing_position_quotes)
            )
        try:
            specs = self._ensure_specs(symbols)
            account = self.broker.get_account()
            account.validate()
        except (TypeError, ValueError, RuntimeError) as exc:
            return self._data_unavailable_result(f"live metadata/account is invalid: {exc}")

        symbol_products = {
            symbol: catalog_by_symbol[symbol].product.upper() for symbol in current_lots
        }
        symbol_products.update(
            {selected[product].symbol: product for product in available_products}
        )
        product_ticks = {
            product: self._ticks[selected[product].symbol] for product in available_products
        }
        initial = self._plan_lot_stages(
            account=account,
            prepared=prepared,
            product_ticks=product_ticks,
            specs=specs,
            current_lots=current_lots,
            symbol_products=symbol_products,
            unavailable_products=unavailable_products,
        )
        intent = prepare_stress90_execution_intent(
            self.execution_intent_store,
            target_trading_day=prepared.target_trading_day,
            daily_decision_digest=prepared.daily_decision_digest,
            account_identity_digest=account_identity,
            account_epoch=account_epoch,
            current_lots=current_lots,
            margin_fitted_lots=initial.margin_fitted_lots,
            freeze_authorized_lots=initial.final_frozen_lots,
            symbol_products=symbol_products,
            lot_notionals={
                symbol: float(self._ticks[symbol].mid_price) * float(specs[symbol].multiplier)
                for symbol in set(current_lots) | set(initial.final_frozen_lots)
                if symbol in self._ticks and symbol in specs
            },
        )
        set_submission_context = getattr(self.broker, "set_order_submission_context", None)
        if callable(set_submission_context):
            set_submission_context(
                target_trading_day=intent.target_trading_day,
                daily_decision_digest=intent.daily_decision_digest,
                execution_intent_digest=intent.source_digest,
                transition={
                    "freeze_authorized_lots": dict(intent.freeze_authorized_lots),
                    "transitions": [
                        {
                            "product": transition.product,
                            "kind": transition.kind,
                            "source_symbols": list(transition.source_symbols),
                            "target_symbol": transition.target_symbol,
                            "target_sign": transition.target_sign,
                            "max_replacement_notional": transition.max_replacement_notional,
                        }
                        for transition in intent.transitions
                    ],
                },
            )
        order_reference_prefix = f"directional:stress90:{intent.daily_decision_digest[:12]}"
        seed_references = getattr(self.broker, "seed_order_reference_prefixes", None)
        if callable(seed_references):
            seed_references((order_reference_prefix,))
        preview = self._plan_lot_stages(
            account=account,
            prepared=prepared,
            product_ticks=product_ticks,
            specs=specs,
            current_lots=current_lots,
            symbol_products=symbol_products,
            unavailable_products=unavailable_products,
            authorized_transition_products=intent.authorized_transition_products,
            authorized_transition_kinds=intent.authorized_transition_kinds,
            authorized_transition_max_replacement_notionals=(
                intent.authorized_transition_max_replacement_notionals
            ),
            persisted_margin_fitted_lots=intent.initial_margin_fitted_lots,
            persisted_freeze_authorized_lots=intent.freeze_authorized_lots,
        )
        blocked_products: set[str] = set()
        for symbol, category in preview.action_categories.items():
            if category not in {"entry", "same_sign_add", "reversal_open"}:
                continue
            product = symbol_products[symbol]
            if (
                opening_window_status(product, now, action_category=category)
                is not OpeningWindowStatus.OPEN
            ):
                blocked_products.add(product)
        if unavailable_products:
            blocked_products.update(STRESS90_POLICY.products)
        stages = self._plan_lot_stages(
            account=account,
            prepared=prepared,
            product_ticks=product_ticks,
            specs=specs,
            current_lots=current_lots,
            symbol_products=symbol_products,
            unavailable_products=unavailable_products,
            authorized_transition_products=intent.authorized_transition_products,
            authorized_transition_kinds=intent.authorized_transition_kinds,
            authorized_transition_max_replacement_notionals=(
                intent.authorized_transition_max_replacement_notionals
            ),
            persisted_margin_fitted_lots=intent.initial_margin_fitted_lots,
            persisted_freeze_authorized_lots=intent.freeze_authorized_lots,
            entry_blocked_products=blocked_products,
        )
        self._last_lot_stages = stages
        self._record_stress90_quality(prepared, stages)
        target_gross = sum(abs(float(value)) for value in prepared.survivor_weights.values())
        signal_day = str(prepared.input_days["completed_close"])
        if stages.reductions:
            phase_target = self._post_reduction_target(positions, stages.reductions)
            self._start_quality_cycle(
                now,
                signal_day=signal_day,
                activity_day=snapshot.trading_day,
                target_gross=target_gross,
                target_lots=phase_target,
                reductions=stages.reductions,
                openings={},
                planned_turnover_notional=self._planned_turnover_notional(stages.reductions),
                reason="stress90-reduce-before-open",
            )
            return self._submit_reductions(
                positions,
                stages.reductions,
                now,
                reference=f"directional:stress90:{prepared.daily_decision_digest[:12]}",
            )
        if stages.openings:
            self._start_quality_cycle(
                now,
                signal_day=signal_day,
                activity_day=snapshot.trading_day,
                target_gross=target_gross,
                target_lots=stages.final_frozen_lots,
                reductions={},
                openings=stages.openings,
                planned_turnover_notional=self._planned_turnover_notional(stages.openings),
                reason="stress90-open-to-target",
            )
            return self._submit_openings(
                positions,
                stages.openings,
                {product: selected[product] for product in available_products},
                specs,
                now,
                reference_prefix=order_reference_prefix,
            )
        if unavailable_products:
            return self._data_unavailable_result(
                "target products are unavailable: " + ",".join(sorted(unavailable_products))
            )
        if blocked_products:
            return DirectionalActionResult(
                "hold",
                "Stress-90 first entry window is unavailable/missed for: "
                + ",".join(sorted(blocked_products)),
            )
        return DirectionalActionResult("hold", "Stress-90 portfolio is at target")

    def runtime_order_reference_prefixes(self) -> tuple[str, ...]:
        record = self.execution_intent_store.load_record()
        if record is None:
            return ()
        policy = self.policy_state_store.load_required()
        account_identity = (
            self.broker.get_account_identity_digest()
            if callable(getattr(self.broker, "get_account_identity_digest", None))
            else ""
        )
        if record.retired:
            if (
                not account_identity
                or record.effective_account_identity_digest != account_identity
                or record.effective_account_identity_digest != policy.live_account_identity_digest
                or record.effective_account_epoch != policy.live_account_epoch
            ):
                raise RuntimeError("Stress-90 retired execution intent lifecycle fence mismatch")
            return ()
        if (
            not account_identity
            or record.intent.account_identity_digest != account_identity
            or record.intent.account_identity_digest != policy.live_account_identity_digest
            or record.intent.account_epoch != policy.live_account_epoch
        ):
            raise RuntimeError(
                "Stress-90 execution intent belongs to a stale account lifecycle epoch"
            )
        return (f"directional:stress90:{record.intent.daily_decision_digest[:12]}",)

    def reconcile_authorized_crash_fills(
        self,
        local_positions,
        remote_positions,
        recent_trade_ids,
    ) -> tuple[bool, tuple[str, ...], str]:
        """Prove Broker drift solely from persisted-intent CTP trades after a crash."""

        from .reconcile import compare_positions

        record = self.execution_intent_store.load_record()
        intent = None if record is None or record.retired else record.intent
        current = _trading_day(self.broker.get_trading_day(), name="current CTP trading day")
        prefix = (
            "" if intent is None else f"directional:stress90:{intent.daily_decision_digest[:12]}"
        )
        seed_references = getattr(self.broker, "seed_order_reference_prefixes", None)
        if callable(seed_references) and prefix:
            seed_references((prefix,))

        candidate_identity_error: str | None = None
        candidate_identity_checked = False

        def validate_candidate_identity() -> str:
            nonlocal candidate_identity_checked, candidate_identity_error
            if candidate_identity_checked:
                return candidate_identity_error or ""
            candidate_identity_checked = True
            if intent is None:
                candidate_identity_error = "Stress-90 execution intent is missing"
                return candidate_identity_error
            if intent.target_trading_day != current:
                candidate_identity_error = "Stress-90 execution intent is not for current CTP day"
                return candidate_identity_error
            try:
                policy = self.policy_state_store.load_required()
            except Exception as exc:
                candidate_identity_error = f"Stress-90 policy state is invalid: {exc}"
                return candidate_identity_error
            if (
                policy.prepared_decision is None
                or policy.prepared_decision.daily_decision_digest != intent.daily_decision_digest
                or policy.live_account_identity_digest != intent.account_identity_digest
                or policy.live_account_epoch != intent.account_epoch
                or (
                    callable(getattr(self.broker, "get_account_identity_digest", None))
                    and self.broker.get_account_identity_digest() != intent.account_identity_digest
                )
            ):
                candidate_identity_error = "Stress-90 execution intent/prepared decision mismatch"
            return candidate_identity_error or ""

        known = set(str(item) for item in recent_trade_ids)
        exact_identity = getattr(self.broker, "get_order_submission_identity", None)
        book = PositionBook(local_positions)
        adopted: list[str] = []
        adopted_volume_by_order: dict[str, int] = {}
        trades = sorted(
            self.broker.get_session_trades(),
            key=lambda item: (item.timestamp, item.exchange, item.trade_id),
        )
        for trade in trades:
            try:
                trade.validate()
            except (AttributeError, TypeError, ValueError) as exc:
                return False, (), f"invalid crash-window trade: {exc}"
            identity = f"{current}:{trade.exchange}:{trade.trade_id}"
            legacy = f"{current}:{trade.trade_id}"
            already_known = identity in known
            if already_known and not callable(exact_identity):
                continue
            if legacy in known:
                return False, (), "ambiguous legacy trade identity"
            order = self.broker.get_order(trade.order_id)
            owned = order is not None and self.broker.owns_order(trade.order_id)
            if owned and callable(exact_identity):
                submission = exact_identity(trade.order_id)
                authorization_kind = getattr(submission, "authorization_kind", "")
                current_candidate_valid = bool(
                    authorization_kind == "candidate"
                    and not validate_candidate_identity()
                    and intent is not None
                    and submission.daily_decision_digest == intent.daily_decision_digest
                    and submission.execution_intent_digest == intent.source_digest
                )
                known_candidate_valid = bool(
                    already_known
                    and authorization_kind == "candidate"
                    and getattr(submission, "account_identity_digest", None)
                    == self.broker.get_account_identity_digest()
                )
                reduction_valid = bool(
                    authorization_kind == "risk_reduction"
                    and submission is not None
                    and submission.request.offset is not Offset.OPEN
                )
                owned = bool(
                    submission is not None
                    and getattr(submission, "status", "submitted") != "aborted_before_send"
                    and submission.target_trading_day == current
                    and submission.policy_id == self.runtime_policy_id
                    and submission.policy_definition_digest == self.runtime_policy_definition_digest
                    and submission.products_manifest_digest == self.runtime_products_manifest_digest
                    and (
                        current_candidate_valid
                        or known_candidate_valid
                        or reduction_valid
                    )
                )
                if owned:
                    durable_request = getattr(submission, "request", None)
                    if not isinstance(durable_request, OrderRequest):
                        return False, (), "durable order request evidence is invalid"
                    if order.request != durable_request:
                        return False, (), "Broker order differs from durable order request"
                    if (
                        trade.symbol,
                        trade.exchange,
                        trade.side,
                        trade.offset,
                    ) != (
                        durable_request.symbol,
                        durable_request.exchange,
                        durable_request.side,
                        durable_request.offset,
                    ):
                        return False, (), "crash-window trade differs from durable order"
                    price_tolerance = max(1e-10, abs(float(durable_request.price)) * 1e-12)
                    worse_than_limit = (
                        trade.side is OrderSide.BUY
                        and trade.price > float(durable_request.price) + price_tolerance
                    ) or (
                        trade.side is OrderSide.SELL
                        and trade.price < float(durable_request.price) - price_tolerance
                    )
                    if worse_than_limit:
                        return False, (), "crash-window trade violates durable limit price"
                    from .broker.ctp_order_journal import (
                        CtpOrderFillEvidence,
                        coerce_ctp_order_fill_evidence,
                        ctp_order_fill_evidence,
                    )

                    persisted_fill_keys = set(getattr(submission, "fill_keys", ()))
                    persisted_filled_volume = getattr(submission, "filled_volume", None)
                    raw_fill_evidence = getattr(submission, "fill_evidence", None)
                    if (
                        isinstance(persisted_filled_volume, bool)
                        or not isinstance(persisted_filled_volume, int)
                        or persisted_filled_volume < 0
                        or persisted_filled_volume > durable_request.volume
                    ):
                        return False, (), "durable filled-volume evidence is invalid"
                    persisted_evidence: tuple[CtpOrderFillEvidence, ...]
                    try:
                        observed_evidence = ctp_order_fill_evidence(current, trade)
                        if raw_fill_evidence is None:
                            if persisted_fill_keys or persisted_filled_volume:
                                return (
                                    False,
                                    (),
                                    "durable fill fingerprint evidence is missing",
                                )
                            persisted_evidence = ()
                        else:
                            persisted_evidence = tuple(
                                coerce_ctp_order_fill_evidence(item) for item in raw_fill_evidence
                            )
                    except (TypeError, ValueError, RuntimeError) as exc:
                        return False, (), f"durable fill fingerprint is invalid: {exc}"
                    persisted_by_key = {item.key: item for item in persisted_evidence}
                    if (
                        set(persisted_by_key) != persisted_fill_keys
                        or sum(item.volume for item in persisted_evidence)
                        != persisted_filled_volume
                    ):
                        return False, (), "durable fill fingerprint evidence is inconsistent"
                    if (
                        identity in persisted_by_key
                        and persisted_by_key[identity] != observed_evidence
                    ):
                        return False, (), "persisted fill fingerprint mismatch"
                    if already_known and identity not in persisted_by_key:
                        return False, (), "known fill fingerprint is not durable"
                    additional = 0 if identity in persisted_fill_keys else trade.volume
                    adopted_volume = (
                        adopted_volume_by_order.get(
                            trade.order_id,
                            persisted_filled_volume,
                        )
                        + additional
                    )
                    if adopted_volume > durable_request.volume:
                        return False, (), "crash-window trade exceeds durable order volume"
                    adopted_volume_by_order[trade.order_id] = adopted_volume
            elif owned:
                # Durable Sim/Shadow ownership has no CTP identity; its persisted request is exact.
                owned = bool(prefix and order.request.reference.startswith(prefix))
            if not owned:
                return False, (), f"unowned crash-window trade: {trade.trade_id}"
            if already_known:
                continue
            try:
                book.apply_trade(trade)
            except ValueError as exc:
                return False, (), f"crash-window trade cannot apply: {exc}"
            adopted.append(identity)
        result = compare_positions(book.all(), remote_positions)
        if not result.matched:
            return False, (), "crash-window trades do not reconcile Broker truth: " + result.details
        return True, tuple(adopted), "persisted Stress-90 intent/trades reconcile Broker truth"


class _CandidateOnlyBroker:
    """Capability fence for HALTED candidate preparation; it can never place an order."""

    metadata_query_blocks = False

    def send_order(self, _request):
        raise RuntimeError("HALTED Stress-90 candidate preparer cannot send orders")

    def set_raw_tick_observer(self, _observer) -> None:
        return None


def prepare_halted_stress90_candidate(
    *,
    directional_config,
    risk_config,
    runtime_dir: str | Path,
    current_ctp_trading_day: str,
    required_activity_day: str,
    static_specs=None,
):
    """Advance complete candidate days under a broker façade with no order authority."""

    from .risk import RiskManager

    runtime = Path(runtime_dir)
    manager = Stress90DirectionalPortfolioManager(
        directional_config,
        _CandidateOnlyBroker(),
        RiskManager(risk_config),
        policy_state_path=runtime / "stress90_policy_state.json",
        seed_path=runtime / "stress90_bootstrap_seed.json",
        oi_evidence_path=runtime / "stress90_oi_evidence.json",
        execution_intent_path=runtime / "stress90_execution_intent.json",
        activity_store_path=runtime / "directional_activity.json",
        ohlc_cache_path=runtime / "directional_ohlc_cache.json",
        static_specs=static_specs,
    )
    try:
        return manager._prepare_decision_for_current_day(
            current_ctp_trading_day,
            required_activity_day=required_activity_day,
        )
    finally:
        # No raw observer was installed and no Broker work occurred.  Close only
        # the unused metadata executor so this preparation cannot checkpoint or
        # otherwise mutate market evidence as a side effect.
        manager._metadata_prefetcher.close()
