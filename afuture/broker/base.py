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


class RawMarketEvidenceError(RuntimeError):
    """Typed data-quality failure already recorded by a callback-safe observer."""


class RawMarketEvidenceFatalError(RuntimeError):
    """Evidence integrity failure that must stop normal Tick delivery."""


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

    def has_pending_critical_events(self) -> bool:
        """Whether bounded FIFO delivery still hides order/account-critical events."""
        return False

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

    def get_account_identity_digest(self) -> str:
        """Return a non-secret stable account identity, or empty when unsupported."""
        return ""

    def get_live_contract_specs(
        self, symbols: list[str], timeout_seconds: float = 10.0
    ) -> dict[str, ContractSpec]:
        """实盘适配器应覆盖；模拟柜台直接返回本地参数。"""
        return {}

    def get_contract_catalog(self) -> list[ContractInfo]:
        """返回可用于自动发现的期货合约目录。"""
        return []

    def get_session_trades(self) -> list:
        """Return Broker-known trades for crash reconciliation when supported."""
        return []

    def refresh_session_activity(self, *, timeout_seconds: float = 10.0):
        """Return complete request-bound session activity, or fail if unsupported."""
        del timeout_seconds
        raise RuntimeError("Broker cannot prove complete session activity")

    def get_session_activity_account_identity_digest(self) -> str:
        """Identity represented by refresh_session_activity evidence."""
        return self.get_account_identity_digest()

    def seed_order_reference_prefixes(self, prefixes) -> None:
        """Authorize persisted strategy references before reconnect callbacks arrive."""
        del prefixes
