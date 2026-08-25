"""Production runtime adapter for the explicitly selected Stress-90 policy."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from .directional_activity import (
    select_contracts_from_activity,
    validate_directional_activity_snapshot,
)
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


class Stress90DataUnavailableError(RuntimeError):
    """Verified market evidence cannot cover a required completed trading day."""


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

    def bootstrap(self, now) -> None:
        record = self.policy_state_store.load_required_record()
        seed = self.seed_store.load_required()
        state = record.state
        if state.bootstrap_seed_digest != seed.seed_digest:
            raise RuntimeError("Stress-90 policy state/bootstrap seed identity mismatch")
        if state.bootstrap_source_manifest != seed.bootstrap_source_manifest:
            raise RuntimeError("Stress-90 bootstrap source manifest mismatch")
        current = _trading_day(self.broker.get_trading_day(), name="current CTP trading day")
        if current < state.last_completed_target_day:
            raise RuntimeError("current CTP trading day moved backward")
        super().bootstrap(now)

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
        prepared = None
        for input_day, target_day in transitions:
            evidence = oi_by_day.get(input_day)
            if evidence is None or not evidence.complete:
                raise Stress90DataUnavailableError(
                    f"completed Stress-90 OI evidence is missing/incomplete: {input_day}"
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

    def record_completed_account_return(
        self,
        trading_day: str,
        daily_return: float,
    ) -> None:
        """Persist one completed Broker day before generic runtime state advances."""

        record = self.policy_state_store.load_required_record()
        state = record.state
        if state.last_completed_account_day == trading_day:
            if (
                not state.recent_daily_returns_for_adaptive_margin
                or abs(state.recent_daily_returns_for_adaptive_margin[-1] - float(daily_return))
                > 1e-15
            ):
                raise RuntimeError("same completed account day conflicts with persisted return")
            return
        updated = record_completed_account_day(state, trading_day, daily_return)
        self.policy_state_store.save(updated, expected_sequence=record.sequence)

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
        persisted_margin_fitted_lots=None,
        entry_blocked_products=(),
    ) -> Stress90LotStages:
        state = self.policy_state_store.load_required()
        return build_stress90_rebalance_stages(
            account=account,
            product_weights=prepared.survivor_weights,
            product_ticks=product_ticks,
            specs=specs,
            current_lots=current_lots,
            symbol_products=symbol_products,
            max_contract_volume=min(
                self.config.max_contract_volume,
                self.risk_manager.config.max_contract_volume,
            ),
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
            persisted_margin_fitted_lots=persisted_margin_fitted_lots,
        )

    def maybe_rebalance(self, now: datetime) -> DirectionalActionResult:
        """Converge Broker truth to one already-durable Stress-90 daily decision."""

        if not self._initialized:
            return DirectionalActionResult("reject", "directional manager is not initialized")
        if not self.broker.is_ready():
            return DirectionalActionResult("reject", "broker is not ready")
        if self.broker.get_active_orders():
            return DirectionalActionResult(
                "wait", "active orders must settle before Stress-90 rebalance"
            )

        current = _trading_day(self.broker.get_trading_day(), name="current CTP trading day")
        state_record = self.policy_state_store.load_required_record()
        if current < state_record.state.last_completed_target_day:
            raise RuntimeError("current CTP trading day moved backward")

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
            current_lots=current_lots,
            margin_fitted_lots=initial.margin_fitted_lots,
            symbol_products=symbol_products,
        )
        preview = self._plan_lot_stages(
            account=account,
            prepared=prepared,
            product_ticks=product_ticks,
            specs=specs,
            current_lots=current_lots,
            symbol_products=symbol_products,
            unavailable_products=unavailable_products,
            authorized_transition_products=intent.authorized_transition_products,
            persisted_margin_fitted_lots=intent.initial_margin_fitted_lots,
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
            persisted_margin_fitted_lots=intent.initial_margin_fitted_lots,
            entry_blocked_products=blocked_products,
        )
        self._last_lot_stages = stages
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
