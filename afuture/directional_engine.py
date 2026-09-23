"""TradingEngine extension for the account-exclusive directional portfolio."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from math import isclose, isfinite

from .directional_risk import (
    DirectionalRiskResponseMode,
    DirectionalRiskScaledPolicy,
)
from .engine import TradingEngine
from .models import Order, RuntimeMode, Tick, Trade
from .state import MAX_RECENT_TRADE_IDS

_DAILY_CIRCUIT_REASON = "daily loss limit reached"
_ACCOUNT_RISK_REASONS = {
    "equity is not positive",
    _DAILY_CIRCUIT_REASON,
    "drawdown limit reached",
    "margin ratio limit reached",
    "available cash reserve too low",
}


class DirectionalTradingEngine(TradingEngine):
    """Reuse the production engine while delegating directional portfolio lifecycle."""

    def __init__(self, *args, directional_manager, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.directional_manager = directional_manager
        self._directional_initialized = False
        self.directional_manager.completed_returns_provider = lambda: tuple(
            self.state.recent_daily_returns
        )
        policy = getattr(self.directional_manager, "policy", None)
        response_mode = DirectionalRiskResponseMode(
            getattr(
                self.directional_manager,
                "policy_risk_response_mode",
                DirectionalRiskResponseMode.TARGET_SCALE,
            )
        )
        if response_mode is DirectionalRiskResponseMode.TARGET_SCALE:
            if policy is not None and not isinstance(policy, DirectionalRiskScaledPolicy):
                self.directional_manager.policy = DirectionalRiskScaledPolicy(
                    policy,
                    completed_returns_provider=(lambda: tuple(self.state.recent_daily_returns)),
                )
        elif isinstance(policy, DirectionalRiskScaledPolicy):
            raise ValueError("freeze-new-risk policy cannot use target scaling wrapper")

    def start(self) -> None:
        prefixes = getattr(self.directional_manager, "runtime_order_reference_prefixes", None)
        seed = getattr(self.broker, "seed_order_reference_prefixes", None)
        if callable(prefixes) and callable(seed):
            seed(prefixes())
        super().start()

    def reconcile_startup(self) -> bool:
        remote = self.broker.get_positions()
        local = self.state_store.positions_from_state(self.state)
        recover = getattr(
            self.directional_manager,
            "reconcile_authorized_crash_fills",
            None,
        )
        if not callable(recover):
            return super().reconcile_startup()
        try:
            allowed, adopted_ids, detail = recover(
                local,
                remote,
                tuple(self.state.recent_trade_ids),
            )
        except Exception as exc:
            self.emergency_stop(f"Stress-90 crash reconciliation failed: {exc}")
            return False
        if not allowed:
            self.emergency_stop(f"position reconciliation failed: {detail}")
            return False
        for identity in adopted_ids:
            if identity not in self._recent_trade_id_set:
                self._recent_trade_id_set.add(identity)
                self.state.recent_trade_ids.append(identity)
        while len(self.state.recent_trade_ids) > MAX_RECENT_TRADE_IDS:
            expired = self.state.recent_trade_ids.pop(0)
            self._recent_trade_id_set.discard(expired)
        self.state.positions = [asdict(position) for position in remote]
        self._sync_strategy_positions(remote)
        self.state.reconciled = True
        self._record(
            "stress90_crash_fill_reconciliation",
            {"adopted_trade_ids": list(adopted_ids), "detail": detail},
        )
        self._persist()
        return True

    def initialize_after_ready(self) -> None:
        super().initialize_after_ready()
        if not self._initialized:
            return
        if self.halted:
            self._try_daily_circuit_recovery(self.broker.get_account())
        self._initialize_directional_manager()

    def _initialize_directional_manager(self) -> None:
        if self.halted or not self._initialized or self._directional_initialized:
            return
        try:
            policy_id = getattr(self.directional_manager, "runtime_policy_id", "")
            if policy_id:
                from .directional_policy_activation import (
                    require_directional_policy_identity,
                )

                require_directional_policy_identity(
                    self.state,
                    policy_id=policy_id,
                    policy_definition_digest=getattr(
                        self.directional_manager,
                        "runtime_policy_definition_digest",
                        "",
                    ),
                    products_manifest_digest=getattr(
                        self.directional_manager,
                        "runtime_products_manifest_digest",
                        "",
                    ),
                    bootstrap_seed_digest=(
                        self.directional_manager.runtime_bootstrap_seed_digest()
                        if callable(
                            getattr(
                                self.directional_manager,
                                "runtime_bootstrap_seed_digest",
                                None,
                            )
                        )
                        else None
                    ),
                    account_identity_digest=(
                        self.broker.get_account_identity_digest()
                        if callable(getattr(self.broker, "get_account_identity_digest", None))
                        else ""
                    ),
                    risk_overlay_digest=(
                        getattr(self.directional_manager, "risk_overlay_digest", None)
                        if policy_id == "directional.stress90"
                        else None
                    ),
                )
            self.directional_manager.bootstrap(self._reference_now())
            self._directional_initialized = True
        except Exception as exc:
            self.emergency_stop(f"directional initialization failed: {exc}")

    def on_tick(self, tick: Tick) -> None:
        if (
            not self.halted
            and self.state.trading_day
            and getattr(
                self.directional_manager, "requires_explicit_settlement_roll_forward", False
            )
        ):
            try:
                account_day = str(self.broker.get_account().trading_day or "")
            except Exception as exc:
                self.emergency_stop(f"Stress-90 account trading day unavailable: {exc}")
                return
            if account_day != self.state.trading_day:
                self.emergency_stop(
                    "Stress-90 requires explicit settlement roll-forward before ticks"
                )
                return
        try:
            self.directional_manager.observe(tick)
        except Exception as exc:
            self.emergency_stop(f"directional tick handling failed: {exc}")
            return
        super().on_tick(tick)

    def _enforce_realized_gross_after_critical_boundary(self) -> bool:
        if (
            self.halted
            or not self._initialized
            or not self._directional_initialized
            or self.state.runtime_mode != RuntimeMode.RUNNING.value
        ):
            return False
        try:
            result = self.directional_manager.enforce_realized_gross_limit(self._reference_now())
        except Exception as exc:
            self.emergency_stop(f"directional gross guard failed: {exc}")
            return False
        if result.action not in {"hold", "wait"}:
            self._record(
                "directional_gross_guard",
                {
                    "action": result.action,
                    "reason": result.reason,
                    "order_ids": list(result.order_ids),
                },
            )
        if result.action == "reject" and self.directional_manager.has_risk():
            self.enter_reduce_only(result.reason or "directional realized gross guard rejected")
            return False
        return result.action in {"hold", "wait"}

    def run_once(self) -> None:
        super().run_once()
        checkpoint_orders = getattr(self.broker, "checkpoint_order_submission_journal", None)
        if callable(checkpoint_orders):
            try:
                checkpoint_orders()
            except Exception as exc:
                self.emergency_stop(f"CTP order journal checkpoint failed: {exc}")
                return
        acknowledge_critical = getattr(self.broker, "acknowledge_critical_events", None)
        if callable(acknowledge_critical) and not acknowledge_critical():
            return
        pending_critical = getattr(self.broker, "has_pending_critical_events", None)
        if callable(pending_critical) and pending_critical():
            # A bounded Broker poll may not yet have delivered a fatal/unknown event.
            # Never perform evidence advancement or strategy/order work until FIFO drains.
            return
        checkpoint = getattr(self.directional_manager, "checkpoint_activity", None)
        if callable(checkpoint):
            try:
                checkpoint()
            except Exception as exc:
                self.emergency_stop(f"directional activity checkpoint failed: {exc}")
                return
        oi_checkpoint = getattr(self.directional_manager, "checkpoint_oi_evidence", None)
        if callable(oi_checkpoint):
            try:
                oi_checkpoint()
            except Exception as exc:
                self.emergency_stop(f"directional OI evidence checkpoint failed: {exc}")
                return
        self._initialize_directional_manager()
        if (
            self.halted
            or not self._initialized
            or not self._directional_initialized
            or self.state.runtime_mode != RuntimeMode.RUNNING.value
        ):
            return
        if not self._enforce_realized_gross_after_critical_boundary():
            return
        try:
            result = self.directional_manager.maybe_rebalance(self._reference_now())
            if result.action == "risk_off":
                self._record(
                    "directional_rebalance",
                    {
                        "action": result.action,
                        "reason": result.reason,
                        "order_ids": list(result.order_ids),
                    },
                )
                if self.directional_manager.has_risk():
                    self.enter_reduce_only(result.reason or "directional risk-off")
                return
            if result.action not in {"hold", "wait"}:
                self._record(
                    "directional_rebalance",
                    {
                        "action": result.action,
                        "reason": result.reason,
                        "order_ids": list(result.order_ids),
                    },
                )
        except Exception as exc:
            self.emergency_stop(f"directional rebalance failed: {exc}")

    def _hard_account_risk_reason(self) -> str:
        """Return non-recoverable account risk without letting daily loss mask it."""
        try:
            account = self.broker.get_account()
        except Exception as exc:
            return f"daily circuit hard-risk classification failed: {exc}"
        try:
            account.validate()
        except (TypeError, ValueError) as exc:
            return f"invalid account snapshot: {exc}"

        high_watermark = max(
            float(self.risk_manager.high_watermark or 0.0),
            float(self.state.equity_high_watermark or 0.0),
            float(account.equity),
        )
        drawdown = max(0.0, high_watermark - account.equity) / high_watermark
        margin_ratio = max(0.0, float(account.margin)) / account.equity
        available_ratio = float(account.available) / account.equity
        config = self.risk_manager.config

        if drawdown >= config.max_total_drawdown_ratio:
            return "drawdown limit reached"
        if margin_ratio > config.max_margin_ratio:
            return "margin ratio limit reached"
        if available_ratio < config.min_available_ratio:
            return "available cash reserve too low"
        return ""

    def emergency_stop(self, reason: str) -> None:
        if reason == _DAILY_CIRCUIT_REASON:
            hard_reason = self._hard_account_risk_reason()
            if hard_reason:
                reason = hard_reason

        if reason == _DAILY_CIRCUIT_REASON:
            circuit_day = str(self.state.trading_day or "")
            if not circuit_day:
                try:
                    circuit_day = str(self.broker.get_account().trading_day or "")
                except Exception:
                    circuit_day = ""
            self.state.directional_daily_circuit_day = circuit_day
        else:
            self.state.directional_daily_circuit_day = ""

        if (
            (reason in _ACCOUNT_RISK_REASONS or reason.startswith("invalid account snapshot:"))
            and hasattr(self, "directional_manager")
            and self.directional_manager.has_risk()
        ):
            self.enter_reduce_only(reason)
            return
        super().emergency_stop(reason)

    def _try_daily_circuit_recovery(self, account) -> bool:
        marker = str(self.state.directional_daily_circuit_day or "")
        current_day = str(getattr(account, "trading_day", "") or "")
        if not marker or not current_day or current_day == marker:
            return False
        if not self.halted or self.state.runtime_mode != RuntimeMode.HALTED.value:
            return False
        if not self.broker.is_ready() or self.broker.get_active_orders():
            return False
        if self.directional_manager.has_risk():
            return False
        if not self._metadata_verified_session or not self.state.metadata_verified:
            return False

        decision = self.risk_manager.check_account(account)
        self.state.equity_high_watermark = self.risk_manager.high_watermark
        if not decision.allowed:
            self.emergency_stop(decision.reason)
            return False
        if not self.reconcile_startup():
            return False
        if not self.state_store.can_clear_kill_switch(self.state):
            return False

        self.state.kill_switch = False
        self.state.kill_reason = ""
        self.state.runtime_mode = RuntimeMode.RUNNING.value
        self.state.reduce_reason = ""
        self.state.directional_daily_circuit_day = ""
        self.halted = False
        self._persist()
        return True

    def _handle_account_event(self, account) -> None:
        super()._handle_account_event(account)
        if self.halted and self.state.directional_daily_circuit_day:
            self._try_daily_circuit_recovery(account)

    def _advance_trading_day(self, account) -> None:
        old_day = str(self.state.trading_day or "")
        new_day = str(getattr(account, "trading_day", "") or "")
        old_day_start = float(self.state.day_start_equity or 0.0)
        old_last_day = str(self.state.last_account_trading_day or "")
        old_last_equity = float(self.state.last_account_equity or 0.0)
        old_last_deposit = float(self.state.last_account_deposit or 0.0)
        old_last_withdrawal = float(self.state.last_account_withdrawal or 0.0)
        old_cash_flow_verified = bool(self.state.last_account_cash_flow_verified)
        old_settlement_id = int(self.state.last_account_settlement_id)

        stress90_account = (
            getattr(self.directional_manager, "runtime_policy_id", "") == "directional.stress90"
        )
        stress90_new_day_start: float | None = None
        if stress90_account and new_day and old_day and new_day != old_day:
            if bool(
                getattr(
                    self.directional_manager,
                    "requires_explicit_settlement_roll_forward",
                    False,
                )
            ):
                raise RuntimeError(
                    "Stress-90 requires explicit settlement roll-forward before runtime day advance"
                )
            try:
                natural_days = (
                    datetime.strptime(new_day, "%Y%m%d") - datetime.strptime(old_day, "%Y%m%d")
                ).days
            except ValueError as exc:
                raise RuntimeError("Stress-90 trading-day gap identity is invalid") from exc
            if natural_days != 1:
                raise RuntimeError(
                    "Stress-90 trading-day gap is non-adjacent and requires an official "
                    "immutable session ledger"
                )
            continuity = getattr(
                self.directional_manager,
                "completed_account_day_is_contiguous",
                None,
            )
            try:
                verified_continuity = (
                    continuity(old_day, new_day) if callable(continuity) else False
                )
            except Exception as exc:
                raise RuntimeError(
                    "Stress-90 trading-day gap continuity evidence is invalid"
                ) from exc
            if verified_continuity is not True:
                raise RuntimeError(
                    "Stress-90 trading-day gap lacks complete authoritative continuity evidence"
                )
        account_deposit = float(getattr(account, "deposit", 0.0))
        account_withdrawal = float(getattr(account, "withdrawal", 0.0))
        account_cash_flow_verified = bool(getattr(account, "cash_flow_verified", False))
        if stress90_account and new_day:
            if not account_cash_flow_verified:
                raise RuntimeError(
                    "Stress-90 account cash flow is unverified; explicit rebase required"
                )
            if new_day == old_last_day:
                if (
                    abs(account_deposit - old_last_deposit) > 1e-12
                    or abs(account_withdrawal - old_last_withdrawal) > 1e-12
                ):
                    raise RuntimeError(
                        "Stress-90 account cash flow changed; explicit rebase required"
                    )
                settlement = getattr(account, "previous_settlement_equity", None)
                settlement_id = getattr(account, "settlement_id", None)
                source_prebalance = old_day_start - old_last_deposit + old_last_withdrawal
                if (
                    old_day != old_last_day
                    or not bool(getattr(account, "settlement_verified", False))
                    or isinstance(settlement, bool)
                    or not isinstance(settlement, (int, float))
                    or not isfinite(float(settlement))
                    or float(settlement) <= 0.0
                    or isinstance(settlement_id, bool)
                    or not isinstance(settlement_id, int)
                    or settlement_id != old_settlement_id
                    or not isfinite(source_prebalance)
                    or source_prebalance <= 0.0
                    or not isclose(
                        source_prebalance,
                        float(settlement),
                        rel_tol=1e-12,
                        abs_tol=1e-8,
                    )
                ):
                    raise RuntimeError(
                        "Stress-90 same-day CTP settlement lineage changed; explicit rebase required"
                    )
            elif old_last_day and (abs(account_deposit) > 1e-12 or abs(account_withdrawal) > 1e-12):
                raise RuntimeError(
                    "Stress-90 new-day account cash flow is nonzero; explicit rebase required"
                )
            if old_day and new_day != old_day:
                settlement = getattr(account, "previous_settlement_equity", None)
                settlement_id = getattr(account, "settlement_id", None)
                if (
                    not bool(getattr(account, "settlement_verified", False))
                    or isinstance(settlement, bool)
                    or not isinstance(settlement, (int, float))
                    or not isfinite(float(settlement))
                    or float(settlement) <= 0.0
                    or isinstance(settlement_id, bool)
                    or not isinstance(settlement_id, int)
                    or old_settlement_id < 0
                    or old_last_day != old_day
                    or old_day_start <= 0.0
                    or old_last_equity <= 0.0
                ):
                    raise RuntimeError(
                        "Stress-90 CTP settlement evidence is invalid; explicit rebase required"
                    )
                stress90_new_day_start = float(settlement) + account_deposit - account_withdrawal
                if not isfinite(stress90_new_day_start) or stress90_new_day_start <= 0.0:
                    raise RuntimeError(
                        "Stress-90 current-day hard baseline is invalid; explicit rebase required"
                    )

        if (
            new_day
            and old_day
            and new_day != old_day
            and old_last_day == old_day
            and old_day_start > 0
            and old_last_equity > 0
        ):
            if stress90_account and not old_cash_flow_verified:
                raise RuntimeError(
                    "Stress-90 completed account cash flow is unverified; explicit rebase required"
                )
            if stress90_account:
                if stress90_new_day_start is None:
                    raise RuntimeError(
                        "Stress-90 current-day hard baseline is unavailable; explicit rebase required"
                    )
                completed_equity = stress90_new_day_start
            else:
                completed_equity = old_last_equity
            completed_return = completed_equity / old_day_start - 1.0
            recorder = getattr(
                self.directional_manager,
                "record_completed_account_return",
                None,
            )
            include_completed_return = True
            if callable(recorder):
                include_completed_return = (
                    recorder(
                        old_day,
                        completed_return,
                        completed_equity=completed_equity,
                    )
                    is not False
                )
            if include_completed_return:
                values = [float(value) for value in self.state.recent_daily_returns[-1:]]
                values.append(float(completed_return))
                self.state.recent_daily_returns = values[-2:]

        super()._advance_trading_day(account)
        if stress90_new_day_start is not None:
            # The generic engine uses current equity for ordinary strategies. Stress-90
            # must exclude current-day PnL from the 5% hard daily-loss baseline.
            self.state.day_start_equity = stress90_new_day_start
            self.risk_manager.set_day_start_equity(stress90_new_day_start, new_day)
        if new_day:
            self.state.last_account_equity = float(account.equity)
            self.state.last_account_trading_day = new_day
            self.state.last_account_deposit = account_deposit
            self.state.last_account_withdrawal = account_withdrawal
            self.state.last_account_cash_flow_verified = account_cash_flow_verified
            self.state.last_account_settlement_id = int(
                getattr(account, "settlement_id", -1)
                if getattr(account, "settlement_id", None) is not None
                else -1
            )

    def _capture_quality_trade(self, trade: Trade) -> None:
        expected = self.directional_manager.directional_order_expectation(trade.order_id)
        if expected is not None:
            commission, source = self._quality_commission(trade)
            self.directional_manager.note_directional_quality_fill(
                trade,
                commission=commission,
                commission_source=source,
            )
            return
        super()._capture_quality_trade(trade)

    def _handle_trade_event(self, trade) -> bool:
        expected = (
            self.directional_manager.directional_order_expectation(trade.order_id)
            if isinstance(trade, Trade)
            else None
        )
        processed = super()._handle_trade_event(trade)
        if expected is not None and processed and not self.halted:
            self.directional_manager._finalize_quality_cycle_if_settled(self._reference_now())
        return processed

    def _handle_order_event(self, order) -> None:
        super()._handle_order_event(order)
        if self.halted:
            return
        if (
            isinstance(order, Order)
            and self.directional_manager.directional_order_expectation(order.order_id) is not None
        ):
            self.directional_manager.note_directional_quality_order(order)

    def stop(self) -> None:
        try:
            self.directional_manager.close()
        finally:
            super().stop()

    def _market_health_reason(self) -> str:
        if self.pairs:
            return super()._market_health_reason()
        required = set(self.directional_manager.required_symbols())
        if not required:
            return ""
        reference = self._health_reference_time()
        if reference is None:
            return ""
        quotes_ready = required.issubset(self.quotes)
        if not quotes_ready and self._quote_initialization_grace_active():
            return ""
        max_quote_age: float
        if self.historical_mode:
            max_quote_age = 0.0
        else:
            observed_quote_age = self._max_quote_age(required, reference, quotes_ready)
            if observed_quote_age is None:
                return "market quote timestamp is in the future"
            max_quote_age = observed_quote_age
        try:
            self.broker.get_account()
            account_ready = True
        except Exception:
            account_ready = False
        try:
            self.broker.get_positions()
            position_ready = True
        except Exception:
            position_ready = False
        return self.health_monitor.evaluate(
            connected=self.broker.is_ready(),
            account_ready=account_ready,
            position_ready=position_ready,
            quotes_ready=quotes_ready,
            max_quote_age=max_quote_age,
        )

    def _reduce_only_cycle(self) -> None:
        if self.directional_manager.has_risk():
            try:
                result = self.directional_manager.flatten(self._reference_now())
                if result.action == "reject" and result.reason:
                    suffix = f"directional flatten failed: {result.reason}"
                    self.state.reduce_reason = (
                        f"{self.state.reduce_reason}; {suffix}"
                        if self.state.reduce_reason
                        else suffix
                    )
                    self._persist()
            except Exception as exc:
                suffix = f"directional flatten exception: {exc}"
                self.state.reduce_reason = (
                    f"{self.state.reduce_reason}; {suffix}" if self.state.reduce_reason else suffix
                )
                self._persist()
            return
        super()._reduce_only_cycle()

    def _reference_now(self) -> datetime:
        reference = self._health_reference_time()
        if reference is None:
            reference = self.health_clock()
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        return reference
