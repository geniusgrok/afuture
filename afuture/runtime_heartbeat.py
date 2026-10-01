"""Process-local main-loop heartbeat observer installed by the canonical CLI router."""

from __future__ import annotations

import signal
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from time import monotonic
from typing import Any

from .heartbeat import HeartbeatWriter


@dataclass(frozen=True)
class RuntimeHeartbeatContext:
    path: str
    interval_seconds: float
    mode: str
    policy: str
    canonical_runtime_digest: str
    account_identity_digest: str
    deployment_identity_digest: str
    risk_overlay_digest: str
    process_uuid: str | None = None


_CONTEXT: RuntimeHeartbeatContext | None = None
_INSTALLED = False
_OBSERVERS: dict[int, RuntimeHeartbeatObserver] = {}
_STOP_HEARTBEAT_OK: bool | None = None
_SIGTERM_PREVIOUS: Any = None
_SIGTERM_INSTALLED = False


def _raise_keyboard_interrupt(_signum: int, _frame: object) -> None:
    """Route systemd SIGTERM through the existing KeyboardInterrupt shutdown path."""

    raise KeyboardInterrupt


@contextmanager
def sigterm_as_keyboard_interrupt() -> Iterator[None]:
    """Temporarily translate SIGTERM into the CLI's established clean-stop signal."""

    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _install_sigterm_shutdown_handler() -> None:
    global _SIGTERM_INSTALLED, _SIGTERM_PREVIOUS
    if _SIGTERM_INSTALLED:
        return
    _SIGTERM_PREVIOUS = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    _SIGTERM_INSTALLED = True


def _restore_sigterm_shutdown_handler() -> None:
    global _SIGTERM_INSTALLED, _SIGTERM_PREVIOUS
    if not _SIGTERM_INSTALLED:
        return
    signal.signal(signal.SIGTERM, _SIGTERM_PREVIOUS)
    _SIGTERM_PREVIOUS = None
    _SIGTERM_INSTALLED = False


def configure_runtime_heartbeat(context: RuntimeHeartbeatContext | None) -> None:
    global _CONTEXT, _STOP_HEARTBEAT_OK
    _CONTEXT = context
    _STOP_HEARTBEAT_OK = None
    if context is None:
        _restore_sigterm_shutdown_handler()
    elif context.mode in {"live", "shadow"}:
        _install_sigterm_shutdown_handler()


def stopped_heartbeat_succeeded() -> bool | None:
    return _STOP_HEARTBEAT_OK


def runtime_identity_digest(*, runtime_dir: str, deployment_digest: str, role: str) -> str:
    material = "\0".join(("afuture.runtime-identity.v1", role, runtime_dir, deployment_digest))
    return sha256(material.encode()).hexdigest()


class RuntimeHeartbeatObserver:
    def __init__(self, engine: Any, context: RuntimeHeartbeatContext) -> None:
        self.engine = engine
        self.context = context
        alerts = getattr(engine, "alerts", None)
        self.writer = HeartbeatWriter(
            context.path,
            interval_seconds=context.interval_seconds,
            process_uuid=context.process_uuid,
            alert_manager=alerts,
        )
        self._critical_backlog_streak = 0
        self._last_successful_cycle_utc = ""

    def _source_broker(self) -> object:
        broker = self.engine.broker
        return getattr(broker, "live", broker)

    @staticmethod
    def _age(source: object, attribute: str) -> float | None:
        raw = getattr(source, attribute, None)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or float(raw) <= 0:
            return None
        return max(0.0, monotonic() - float(raw))

    def _quote_age(self) -> float:
        quotes = getattr(self.engine, "quotes", {})
        if not isinstance(quotes, dict):
            return 0.0
        clock = getattr(self.engine, "health_clock", None)
        now = clock() if callable(clock) else datetime.now(timezone.utc)
        risk = getattr(self.engine, "risk_manager", None)
        active = set(quotes)
        if risk is not None:
            active = risk.active_runtime_symbols(active, now)
        ages: list[float] = []
        for symbol, tick in quotes.items():
            if symbol not in active:
                continue
            timestamp = getattr(tick, "timestamp", None)
            if isinstance(timestamp, datetime) and timestamp.tzinfo is not None:
                ages.append(
                    max(
                        0.0,
                        (now - timestamp.astimezone(timezone.utc)).total_seconds(),
                    )
                )
        return max(ages, default=0.0)

    def _state_records(self):
        state_store = self.engine.state_store
        generic = state_store.load_required_record()
        runtime_dir = state_store.path.resolve(strict=False).parent
        policy_sequence = 0
        policy_checksum = ""
        try:
            from .directional_stress90_state import Stress90PolicyStateStore

            policy = Stress90PolicyStateStore(
                runtime_dir / "stress90_policy_state.json"
            ).load_required_record()
            policy_sequence = int(policy.sequence)
            policy_checksum = str(policy.checksum)
        except (OSError, RuntimeError, ValueError):
            pass
        return generic, policy_sequence, policy_checksum

    def facts(self) -> dict[str, object]:
        source = self._source_broker()
        state_record, policy_sequence, policy_checksum = self._state_records()
        state = state_record.state
        counters: dict[str, object] = {}
        delivery = getattr(source, "delivery_counters", None)
        if callable(delivery):
            raw = delivery()
            if isinstance(raw, dict):
                counters = {
                    str(key): value
                    for key, value in raw.items()
                    if isinstance(value, (int, float, bool, str))
                }
        backlog = counters.get("critical_backlog")
        if type(backlog) is not int:
            enqueued = counters.get("critical_enqueued", 0)
            delivered = counters.get("critical_delivered", 0)
            backlog = (
                max(0, int(enqueued) - int(delivered))
                if type(enqueued) is int and type(delivered) is int
                else 0
            )
        if int(backlog) > 0:
            self._critical_backlog_streak += 1
        else:
            self._critical_backlog_streak = 0
        counters["critical_backlog"] = int(backlog)
        counters["critical_backlog_streak"] = self._critical_backlog_streak

        ready = False
        health = ""
        is_ready = getattr(source, "is_ready", None)
        if callable(is_ready):
            try:
                ready = bool(is_ready())
            except Exception:
                ready = False
        health_error = getattr(source, "health_error", None)
        if callable(health_error):
            try:
                raw_health = health_error()
                health = "" if raw_health is None else str(raw_health)
            except Exception:
                health = "health check failed"
        broker_state = "ready" if ready and not health else "unhealthy"
        broker = self.engine.broker
        active_count = 0
        get_active = getattr(broker, "get_active_orders", None)
        if callable(get_active):
            try:
                active_count = len(get_active())
            except Exception:
                active_count = -1
        return {
            "mode": self.context.mode,
            "policy": self.context.policy,
            "canonical_runtime_digest": self.context.canonical_runtime_digest,
            "account_identity_digest": self.context.account_identity_digest,
            "ctp_trading_day": str(getattr(state, "trading_day", "")),
            "broker_connection_state": broker_state,
            "last_account_snapshot_age_seconds": self._age(
                source,
                "_last_account_monotonic",
            ),
            "last_complete_position_snapshot_age_seconds": self._age(
                source,
                "_last_position_snapshot_monotonic",
            ),
            "max_required_quote_age_seconds": self._quote_age(),
            "critical_queue": counters,
            "alert_delivery": self.engine.alerts.diagnostics(),
            "active_order_count": active_count,
            "runtime_mode": str(getattr(state, "runtime_mode", "")),
            "kill_switch": bool(getattr(state, "kill_switch", False)),
            "reconciled": bool(getattr(state, "reconciled", False)),
            "generic_state_sequence": int(state_record.sequence),
            "generic_state_checksum": str(state_record.checksum),
            "policy_state_sequence": policy_sequence,
            "policy_state_checksum": policy_checksum,
            "risk_overlay_digest": self.context.risk_overlay_digest,
            "deployment_identity_digest": self.context.deployment_identity_digest,
            "last_successful_cycle_utc": self._last_successful_cycle_utc,
            "last_error_category": (
                "runtime_halted"
                if bool(getattr(state, "kill_switch", False))
                or str(getattr(state, "runtime_mode", "")) == "HALTED"
                else ("broker_health" if health else "")
            ),
        }

    def _alert_failure(self, exc: Exception) -> None:
        alerts = getattr(self.engine, "alerts", None)
        if alerts is not None:
            alerts.critical(
                "heartbeat collection or write failed",
                {"error_category": type(exc).__name__},
            )

    def after_cycle(self) -> None:
        self._last_successful_cycle_utc = datetime.now(timezone.utc).isoformat()
        if not self.writer.is_due():
            return
        try:
            self.writer.write(self.facts())
        except Exception as exc:
            # Observability must never become a trading-control failure. Risk actions,
            # cancellation, and reduction decisions have already completed this cycle.
            self._alert_failure(exc)

    def after_stop(self, *, clean: bool) -> bool:
        try:
            generic = self.engine.state_store.load_required_record()
            return self.writer.write_stopped(
                self.facts(),
                final_state_checksum=str(generic.checksum),
                clean=clean,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            self._alert_failure(exc)
            return False
        finally:
            _restore_sigterm_shutdown_handler()


def _observer(engine: Any) -> RuntimeHeartbeatObserver | None:
    if _CONTEXT is None:
        return None
    key = id(engine)
    observer = _OBSERVERS.get(key)
    if observer is None:
        observer = RuntimeHeartbeatObserver(engine, _CONTEXT)
        _OBSERVERS[key] = observer
    return observer


def install_engine_heartbeat_hooks() -> None:
    """Install process-local wrappers; callback methods are deliberately untouched."""

    global _INSTALLED
    if _INSTALLED:
        return
    from .directional_engine import DirectionalTradingEngine
    from .engine import TradingEngine

    original_run_once = TradingEngine.run_once
    original_directional_run_once = DirectionalTradingEngine.run_once
    original_stop = TradingEngine.stop

    def run_once(self: Any, *args: object, **kwargs: object):
        result = original_run_once(self, *args, **kwargs)
        if not isinstance(self, DirectionalTradingEngine):
            observer = _observer(self)
            if observer is not None:
                observer.after_cycle()
        return result

    def directional_run_once(self: Any, *args: object, **kwargs: object):
        result = original_directional_run_once(self, *args, **kwargs)
        observer = _observer(self)
        if observer is not None:
            observer.after_cycle()
        return result

    directional_run_once._afuture_heartbeat_full_cycle = True  # type: ignore[attr-defined]

    def stop(self: Any, *args: object, **kwargs: object):
        global _STOP_HEARTBEAT_OK
        result = original_stop(self, *args, **kwargs)
        observer = _observer(self)
        if observer is not None:
            _STOP_HEARTBEAT_OK = observer.after_stop(clean=True)
            _OBSERVERS.pop(id(self), None)
        else:
            _restore_sigterm_shutdown_handler()
        return result

    TradingEngine.run_once = run_once  # type: ignore[method-assign]
    DirectionalTradingEngine.run_once = directional_run_once  # type: ignore[method-assign]
    TradingEngine.stop = stop  # type: ignore[method-assign]
    _INSTALLED = True
