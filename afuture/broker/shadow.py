"""真实 CTP 行情 + 模拟成交的 Shadow Broker。

所有行情、合约目录和柜台元数据来自真实 broker；所有订单只进入本地 SimBroker，
因此 Shadow 模式从类型层面就不能把订单发到真实柜台。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from ..models import BrokerEvent, ContractSpec, OrderRequest, Tick
from .base import Broker
from .sim import SimBroker


class ShadowBroker(Broker):
    """把真实行情源与本地保守撮合组合为一个 Broker 接口。"""

    def __init__(
        self,
        live_broker,
        initial_capital: float,
        *,
        slippage_ticks: int = 1,
        latency_ticks: int = 1,
        market_impact_ticks: int = 1,
        state_path: str | Path | None = None,
    ) -> None:
        self.live = live_broker
        state_identity = self.get_account_identity_digest() if state_path is not None else ""
        if state_path is not None and not state_identity:
            raise RuntimeError("persistent ShadowBroker requires a non-empty account identity")
        self.sim = SimBroker(
            initial_capital,
            {},
            slippage_ticks=slippage_ticks,
            conservative=True,
            latency_ticks=latency_ticks,
            market_impact_ticks=market_impact_ticks,
            state_path=state_path,
            state_identity=state_identity,
            dynamic_specs=True,
            dynamic_catalog=True,
        )
        self._started = False

    def start(self) -> None:
        self.live.start()
        self.sim.start()
        self._started = True

    def stop(self) -> None:
        try:
            self.sim.stop()
        finally:
            self.live.stop()
            self._started = False

    def is_ready(self) -> bool:
        return self._started and self.live.is_ready() and self.sim.is_ready()

    def update_specs(self, specs: dict[str, ContractSpec]) -> None:
        """动态候选拿到真实 CTP 参数后同步给本地撮合和保证金模型。"""
        self.sim.update_specs(specs)

    def subscribe(self, symbol: str, exchange: str) -> None:
        self.live.subscribe(symbol, exchange)
        if symbol in self.sim.specs:
            self.sim.subscribe(symbol, exchange)

    def set_raw_tick_observer(self, observer) -> None:
        """Observe the real raw market callback once, never the coalesced Shadow copy."""
        self.live.set_raw_tick_observer(observer)

    def _synchronize_trading_day(self) -> str:
        trading_day = self.live.get_trading_day()
        self.sim.synchronize_trading_day(trading_day)
        return trading_day

    def send_order(self, request: OrderRequest) -> str:
        """关键安全属性：订单只发送到本地模拟柜台。"""
        require_session = getattr(
            self.live,
            "require_stress90_session_startup_capability_current",
            None,
        )
        if callable(require_session):
            # Shadow orders do not consume the live-account baseline. Any later real
            # CTP order/trade ingress continues to invalidate simulated submissions.
            require_session()
        self._synchronize_trading_day()
        return self.sim.send_order(request)

    def cancel_order(self, order_id: str) -> None:
        self._synchronize_trading_day()
        self.sim.cancel_order(order_id)

    def get_order(self, order_id: str):
        self._synchronize_trading_day()
        return self.sim.get_order(order_id)

    def get_active_orders(self):
        self._synchronize_trading_day()
        return self.sim.get_active_orders()

    def get_positions(self):
        self._synchronize_trading_day()
        return self.sim.get_positions()

    def get_account(self):
        """资金和仓位来自 Shadow 虚拟账户，交易日锚定真实 CTP。"""
        trading_day = self._synchronize_trading_day()
        return replace(
            self.sim.get_account(),
            trading_day=trading_day,
        )

    def owns_order(self, order_id: str) -> bool:
        self._synchronize_trading_day()
        return self.sim.owns_order(order_id)

    def get_session_trades(self):
        self._synchronize_trading_day()
        return self.sim.get_session_trades()

    def refresh_session_activity(self, *, timeout_seconds: float = 10.0):
        """Prove the live CTP source session while local trades stay Sim-owned."""
        return self.live.refresh_session_activity(timeout_seconds=timeout_seconds)

    def get_session_activity_account_identity_digest(self) -> str:
        return self.live.get_account_identity_digest()

    def configure_order_submission_journal(self, path, **identity) -> None:
        self.live.configure_order_submission_journal(path, **identity)

    def require_stress90_session_startup_capability(self) -> None:
        self.live.require_stress90_session_startup_capability()

    def install_stress90_session_startup_capability(self, **proof) -> None:
        self.live.install_stress90_session_startup_capability(**proof)

    def require_stress90_session_startup_capability_current(self) -> None:
        self.live.require_stress90_session_startup_capability_current()

    def require_session_activity_evidence_current(self, evidence) -> None:
        self.live.require_session_activity_evidence_current(evidence)

    def recover_stress90_session_activity(self, evidence):
        return self.live.recover_stress90_session_activity(evidence)

    def checkpoint_order_submission_journal(self) -> None:
        checkpoint = getattr(self.live, "checkpoint_order_submission_journal", None)
        if callable(checkpoint):
            checkpoint()

    def seed_order_reference_prefixes(self, prefixes) -> None:
        setter = getattr(self.sim, "seed_order_reference_prefixes", None)
        if callable(setter):
            setter(prefixes)

    def get_trading_day(self) -> str:
        return self.live.get_trading_day()

    def get_account_identity_digest(self) -> str:
        live_identity = self.live.get_account_identity_digest()
        if not live_identity:
            return ""
        return sha256(f"afuture.shadow-account.v1\0{live_identity}".encode()).hexdigest()

    def get_contract_catalog(self):
        return self.live.get_contract_catalog()

    def get_live_contract_specs(
        self, symbols: list[str], timeout_seconds: float = 10.0
    ) -> dict[str, ContractSpec]:
        rows = self.live.get_live_contract_specs(symbols, timeout_seconds)
        self.update_specs(rows)
        return rows

    def health_error(self) -> str | None:
        return self.live.health_error()

    def has_pending_critical_events(self) -> bool:
        return bool(self.live.has_pending_critical_events())

    @contextmanager
    def lifecycle_state_commit_fence(self):
        """Linearize both live callback ingress and the local Shadow account."""

        live_fence = getattr(self.live, "lifecycle_state_commit_fence", None)
        sim_fence = getattr(self.sim, "lifecycle_state_commit_fence", None)
        if not callable(live_fence) or not callable(sim_fence):
            raise RuntimeError("Shadow lifecycle commit fence is unavailable")
        with live_fence():
            with sim_fence():
                yield

    def snapshot_marker(self):
        getter = getattr(self.live, "snapshot_marker", None)
        return getter() if callable(getter) else (0, 0)

    def snapshot_ready(self, marker) -> bool:
        getter = getattr(self.live, "snapshot_ready", None)
        return bool(getter(marker)) if callable(getter) else True

    def poll_events(self) -> list[BrokerEvent]:
        """真实 Tick 驱动模拟撮合；真实账户/持仓事件不覆盖 Shadow 虚拟账户。"""
        result: list[BrokerEvent] = []

        def append_simulated_critical_events() -> None:
            result.extend(event for event in self.sim.poll_events() if event.event_type != "tick")

        with self.sim.market_batch(trading_day=self.live.get_trading_day()):
            append_simulated_critical_events()
            for event in self.live.poll_events():
                if event.event_type == "tick" and isinstance(event.payload, Tick):
                    self.publish_tick(event.payload)
                    append_simulated_critical_events()
                    result.append(event)
                elif event.event_type == "broker_error":
                    result.append(event)
            append_simulated_critical_events()
        return result

    def publish_tick(self, tick: Tick) -> None:
        """测试和真实 poll 都通过这一入口驱动本地撮合。"""
        if tick.symbol not in self.sim.specs:
            return
        self.sim.publish_tick(tick)
