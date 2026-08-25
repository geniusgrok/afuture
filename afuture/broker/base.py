"""交易柜台统一接口。"""

from abc import ABC, abstractmethod
from typing import Protocol

from ..models import (
    AccountSnapshot,
    BrokerEvent,
    ContractInfo,
    ContractPosition,
    ContractSpec,
    Order,
    OrderRequest,
    Tick,
)


class RawTickObserver(Protocol):
    """Non-blocking market-evidence observer invoked before normal Tick delivery."""

    def observe_raw_tick(self, tick: Tick, contract: ContractInfo | None) -> None: ...


class Broker(ABC):
    """交易柜台最小接口。真实柜台元数据查询默认可能阻塞。"""

    metadata_query_blocks = True

    @abstractmethod
    def start(self): ...
    @abstractmethod
    def stop(self): ...
    @abstractmethod
    def is_ready(self): ...
    @abstractmethod
    def subscribe(self, symbol: str, exchange: str): ...
    @abstractmethod
    def send_order(self, request: OrderRequest) -> str: ...
    @abstractmethod
    def cancel_order(self, order_id: str): ...
    @abstractmethod
    def get_account(self) -> AccountSnapshot: ...
    @abstractmethod
    def get_positions(self) -> list[ContractPosition]: ...
    @abstractmethod
    def get_active_orders(self) -> list[Order]: ...
    @abstractmethod
    def get_order(self, order_id: str) -> Order | None: ...
    @abstractmethod
    def poll_events(self) -> list[BrokerEvent]: ...

    def owns_order(self, order_id: str) -> bool:
        return self.get_order(order_id) is not None

    def health_error(self) -> str | None:
        return None

    def publish_tick(self, tick: Tick) -> None:
        raise NotImplementedError

    def set_raw_tick_observer(self, observer: RawTickObserver | None) -> None:
        """Install one callback-safe observer; implementations must notify pre-coalescing."""
        self._raw_tick_observer = observer

    def _notify_raw_tick_observer(
        self,
        tick: Tick,
        contract: ContractInfo | None,
    ) -> None:
        observer = getattr(self, "_raw_tick_observer", None)
        if observer is not None:
            observer.observe_raw_tick(tick, contract)

    def get_trading_day(self) -> str:
        return self.get_account().trading_day

    def get_live_contract_specs(
        self, symbols: list[str], timeout_seconds: float = 10.0
    ) -> dict[str, ContractSpec]:
        """实盘适配器应覆盖；模拟柜台直接返回本地参数。"""
        return {}

    def get_contract_catalog(self) -> list[ContractInfo]:
        """返回可用于自动发现的期货合约目录。"""
        return []
