"""真实 CTP 行情 + 模拟成交的 Shadow Broker。

所有行情、合约目录和柜台元数据来自真实 broker；所有订单只进入本地 SimBroker，
因此 Shadow 模式从类型层面就不能把订单发到真实柜台。
"""

from __future__ import annotations

from dataclasses import replace

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
    ) -> None:
        self.live = live_broker
        self.sim = SimBroker(
            initial_capital,
            {},
            slippage_ticks=slippage_ticks,
            conservative=True,
            latency_ticks=latency_ticks,
            market_impact_ticks=market_impact_ticks,
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
        self.sim.specs.update(specs)

    def subscribe(self, symbol: str, exchange: str) -> None:
        self.live.subscribe(symbol, exchange)
        if symbol in self.sim.specs:
            self.sim.subscribe(symbol, exchange)

    def send_order(self, request: OrderRequest) -> str:
        """关键安全属性：订单只发送到本地模拟柜台。"""
        return self.sim.send_order(request)

    def cancel_order(self, order_id: str) -> None:
        self.sim.cancel_order(order_id)

    def get_order(self, order_id: str):
        return self.sim.get_order(order_id)

    def get_active_orders(self):
        return self.sim.get_active_orders()

    def get_positions(self):
        return self.sim.get_positions()

    def get_account(self):
        """资金和仓位来自 Shadow 虚拟账户，交易日锚定真实 CTP。"""
        return replace(
            self.sim.get_account(),
            trading_day=self.live.get_trading_day(),
        )

    def owns_order(self, order_id: str) -> bool:
        return self.sim.owns_order(order_id)

    def get_trading_day(self) -> str:
        return self.live.get_trading_day()

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

    def snapshot_marker(self):
        getter = getattr(self.live, "snapshot_marker", None)
        return getter() if callable(getter) else (0, 0)

    def snapshot_ready(self, marker) -> bool:
        getter = getattr(self.live, "snapshot_ready", None)
        return bool(getter(marker)) if callable(getter) else True

    def poll_events(self) -> list[BrokerEvent]:
        """真实 Tick 驱动模拟撮合；真实账户/持仓事件不覆盖 Shadow 虚拟账户。"""
        result: list[BrokerEvent] = []
        for event in self.live.poll_events():
            if event.event_type == "tick" and isinstance(event.payload, Tick):
                self.publish_tick(event.payload)
                result.append(event)
            elif event.event_type == "broker_error":
                result.append(event)

        for event in self.sim.poll_events():
            if event.event_type != "tick":
                result.append(event)
        return result

    def publish_tick(self, tick: Tick) -> None:
        """测试和真实 poll 都通过这一入口驱动本地撮合。"""
        if tick.symbol not in self.sim.specs:
            return
        self.sim.publish_tick(tick)
