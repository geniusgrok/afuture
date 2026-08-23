"""TradingEngine extension for the account-exclusive directional portfolio."""
from __future__ import annotations

from datetime import datetime, timezone

from .directional_risk import DirectionalRiskScaledPolicy
from .engine import TradingEngine
from .models import Order, RuntimeMode, Tick, Trade


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
        policy = getattr(self.directional_manager, "policy", None)
        if policy is not None and not isinstance(policy, DirectionalRiskScaledPolicy):
            self.directional_manager.policy = DirectionalRiskScaledPolicy(
                policy,
                completed_returns_provider=(
                    lambda: tuple(self.state.recent_daily_returns)
                ),
            )

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
            self.directional_manager.bootstrap(self._reference_now())
            self._directional_initialized = True
        except Exception as exc:
            self.emergency_stop(f"directional initialization failed: {exc}")

    def on_tick(self, tick: Tick) -> None:
        try:
            self.directional_manager.observe(tick)
        except Exception as exc:
            self.emergency_stop(f"directional tick handling failed: {exc}")
            return
        super().on_tick(tick)

    def run_once(self) -> None:
        super().run_once()
        self._initialize_directional_manager()
        if (
            self.halted
            or not self._initialized
            or not self._directional_initialized
            or self.state.runtime_mode != RuntimeMode.RUNNING.value
        ):
            return
        try:
            result = self.directional_manager.maybe_rebalance(
                self._reference_now()
            )
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
        if account.equity <= 0:
            return "equity is not positive"

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
            # Any reason other than the known recoverable daily circuit invalidates
            # automatic recovery. Total drawdown, margin/cash and infrastructure faults
            # therefore remain manual/fail-closed halts.
            self.state.directional_daily_circuit_day = ""

        if (
            reason in _ACCOUNT_RISK_REASONS
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

        if (
            new_day
            and old_day
            and new_day != old_day
            and old_last_day == old_day
            and old_day_start > 0
            and old_last_equity > 0
        ):
            completed_return = old_last_equity / old_day_start - 1.0
            values = [
                float(value)
                for value in self.state.recent_daily_returns[-1:]
            ]
            values.append(float(completed_return))
            self.state.recent_daily_returns = values[-2:]

        super()._advance_trading_day(account)
        if new_day:
            self.state.last_account_equity = float(account.equity)
            self.state.last_account_trading_day = new_day

    def _capture_quality_trade(self, trade: Trade) -> None:
        # Directional expectations are registered at submission time. They are enough to
        # identify the fill; querying Broker.get_order() here creates an unnecessary
        # adapter dependency and can race order-cache propagation.
        expected = self.directional_manager.directional_order_expectation(
            trade.order_id
        )
        if expected is not None:
            commission, source = self._quality_commission(trade)
            self.directional_manager.note_directional_quality_fill(
                trade,
                commission=commission,
                commission_source=source,
            )
            return
        super()._capture_quality_trade(trade)

    def _handle_trade_event(self, trade) -> None:
        expected = (
            self.directional_manager.directional_order_expectation(trade.order_id)
            if isinstance(trade, Trade)
            else None
        )
        # Base handler owns validation, expected-position mutation, persistence and pair
        # quality. Directional observability is layered around it, never instead of it.
        super()._handle_trade_event(trade)
        if expected is not None and not self.halted:
            self.directional_manager._finalize_quality_cycle_if_settled(
                self._reference_now()
            )

    def _handle_order_event(self, order) -> None:
        super()._handle_order_event(order)
        if (
            isinstance(order, Order)
            and self.directional_manager.directional_order_expectation(order.order_id)
            is not None
        ):
            self.directional_manager.note_directional_quality_order(order)
            # Do not finalize here. Some gateways publish terminal order status before the
            # corresponding trade callback; finalizing would discard the fill expectation.

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
        if self.historical_mode:
            max_quote_age = 0.0
        else:
            max_quote_age = self._max_quote_age(required, reference, quotes_ready)
            if max_quote_age is None:
                return "market quote timestamp is in the future"
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
                    f"{self.state.reduce_reason}; {suffix}"
                    if self.state.reduce_reason
                    else suffix
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
