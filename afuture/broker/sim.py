"""确定性模拟柜台，支持普通和保守撮合模式。"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from functools import wraps
from math import ceil, isfinite
from pathlib import Path
from threading import RLock

from ..fees import calculate_commission
from ..models import (
    AccountSnapshot,
    BrokerEvent,
    ContractInfo,
    ContractPosition,
    ContractSpec,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Tick,
    Trade,
)
from ..position import PositionBook
from .base import Broker, RawMarketEvidenceError
from .sim_state import (
    EMPTY_ACCOUNTING_HISTORY_DIGEST,
    DecodedSimBrokerState,
    DecodedSimMarketState,
    SimAccountingBaseline,
    SimBrokerStateIntegrityError,
    SimBrokerStateStore,
    advance_accounting_history_digest,
    calculate_market_digest,
    decode_sim_broker_state,
    decode_sim_market_state,
    encode_contract_catalog,
    encode_contract_specs,
    encode_sim_broker_state,
    encode_sim_market_state,
    validate_sim_broker_state_transition,
)


def _lifecycle_locked(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lifecycle_state_lock:
            return method(self, *args, **kwargs)

    return wrapped


class SimBroker(Broker):
    """按一档盘口撮合，并可启用深度消耗、延迟和市场冲击的保守模式。"""

    metadata_query_blocks = False

    def __init__(
        self,
        initial_capital: float,
        specs: dict[str, ContractSpec],
        *,
        slippage_ticks: int = 0,
        conservative: bool = False,
        latency_ticks: int = 0,
        market_impact_ticks: int = 0,
        depth_haircut: float = 1.0,
        size_impact_ticks: int = 0,
        contract_catalog: list[ContractInfo] | None = None,
        state_path: str | Path | None = None,
        state_identity: str = "",
        dynamic_specs: bool = False,
        dynamic_catalog: bool = False,
    ) -> None:
        if initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if not 0.0 < float(depth_haircut) <= 1.0:
            raise ValueError("depth_haircut must be in (0, 1]")
        self.initial_capital = initial_capital
        self.specs = specs if state_path is None else dict(specs)
        self._known_specs = dict(specs)
        self._dynamic_specs = bool(dynamic_specs)
        self._dynamic_catalog = bool(dynamic_catalog)
        if self._dynamic_catalog and contract_catalog:
            raise ValueError("dynamic contract catalog cannot bind startup rows")
        encoded_catalog = encode_contract_catalog(list(contract_catalog or []))
        self._unverified_spec_symbols: set[str] = set()
        self.slippage_ticks = max(0, slippage_ticks)
        self.conservative = conservative
        self.latency_ticks = max(0, latency_ticks)
        self.market_impact_ticks = max(0, market_impact_ticks)
        self.depth_haircut = float(depth_haircut)
        self.size_impact_ticks = max(0, int(size_impact_ticks))
        self._contract_catalog = list(contract_catalog or [])
        self._contract_catalog_by_symbol = {
            contract.symbol: contract for contract in self._contract_catalog
        }
        self.position_book = PositionBook()
        self._orders: dict[str, Order] = {}
        self._trades: list[Trade] = []
        self._trade_trading_days: dict[str, str] = {}
        self._ticks: dict[str, Tick] = {}
        self._events: list[BrokerEvent] = []
        self._next_order_sequence = 1
        self._next_trade_sequence = 1
        self._started = False
        self._lifecycle_state_lock = RLock()
        self._raw_market_connection_generation = 0
        self._balance = initial_capital
        self._realized_pnl = 0.0
        self._commission = 0.0
        self._trading_day = ""
        self._tick_seq = 0
        self._eligible_seq: dict[str, int] = {}
        self._depth: dict[str, list[int]] = {}
        self._order_arrival: dict[str, tuple[float, float, int]] = {}
        self._first_fill_seq: dict[str, int] = {}
        self._accounting_baseline = SimAccountingBaseline(
            balance=float(initial_capital),
            realized_pnl=0.0,
            commission=0.0,
            trading_day="",
            positions=[],
            next_order_sequence=1,
            next_trade_sequence=1,
            compacted_order_count=0,
            compacted_trade_count=0,
            compacted_through_trading_day="",
            history_digest=EMPTY_ACCOUNTING_HISTORY_DIGEST,
        )
        self._deposit = 0.0
        self._withdrawal = 0.0
        self._cash_flow_verified = True
        self._previous_settlement_equity: float | None = float(initial_capital)
        self._settlement_verified = True
        self._settlement_id = 0
        self._market_state_dirty = False
        self._state_store: SimBrokerStateStore | None = None
        self._market_state_store: SimBrokerStateStore | None = None
        self._persistence_fault: BaseException | None = None
        self._market_batch_active = False
        self._market_batch_truth_pending = False
        self._durable_session_compaction = state_path is not None
        self._strict_tick_trading_day = state_path is not None
        if state_path is not None:
            if not isinstance(state_identity, str):
                raise ValueError("state_identity must be a string")
            configuration = {
                "initial_capital": float(self.initial_capital),
                "slippage_ticks": self.slippage_ticks,
                "conservative": self.conservative,
                "latency_ticks": self.latency_ticks,
                "market_impact_ticks": self.market_impact_ticks,
                "depth_haircut": self.depth_haircut,
                "size_impact_ticks": self.size_impact_ticks,
                "state_identity": state_identity,
                "dynamic_specs": self._dynamic_specs,
                "dynamic_catalog": self._dynamic_catalog,
                "contract_specs": (
                    None if self._dynamic_specs else encode_contract_specs(self._known_specs)
                ),
                "contract_catalog": None if self._dynamic_catalog else encoded_catalog,
            }
            self._state_store = SimBrokerStateStore(state_path, configuration)
            record = self._state_store.load()
            if record is None:
                self._state_store.initialize(self._snapshot_state())
            else:
                if record.operation is not None:
                    raise SimBrokerStateIntegrityError(
                        f"incomplete broker operation: {record.operation}"
                    )
                restored = decode_sim_broker_state(
                    record.state,
                    initial_capital=float(self.initial_capital),
                )
                previous_record = self._state_store.previous_record
                previous = (
                    None
                    if record.sequence == 1 or previous_record is None
                    else decode_sim_broker_state(
                        previous_record.state,
                        initial_capital=float(self.initial_capital),
                    )
                )
                validate_sim_broker_state_transition(
                    previous,
                    restored,
                    initial_capital=float(self.initial_capital),
                )
                self._restore_state(restored)
            market_path = Path(state_path).with_name(f"{Path(state_path).name}.market")
            self._market_state_store = SimBrokerStateStore(
                market_path,
                {**configuration, "component": "bounded-market-state-v1"},
            )
            market_record = self._market_state_store.load()
            if market_record is None:
                self._market_state_store.initialize(self._snapshot_market_state())
            else:
                if market_record.operation is not None:
                    raise SimBrokerStateIntegrityError(
                        f"incomplete broker market operation: {market_record.operation}"
                    )
                market_state = decode_sim_market_state(
                    market_record.state,
                    contract_specs=self._known_specs,
                )
                self._restore_market_state(market_state)

    def _decoded_state(self) -> DecodedSimBrokerState:
        return DecodedSimBrokerState(
            accounting_baseline=self._accounting_baseline,
            balance=float(self._balance),
            realized_pnl=float(self._realized_pnl),
            commission=float(self._commission),
            trading_day=self._trading_day,
            positions=self.position_book.all(),
            orders=list(self._orders.values()),
            trades=list(self._trades),
            ticks=dict(self._ticks),
            next_order_sequence=self._next_order_sequence,
            next_trade_sequence=self._next_trade_sequence,
            tick_sequence=self._tick_seq,
            eligible_sequence=dict(self._eligible_seq),
            depth={symbol: list(depth) for symbol, depth in self._depth.items()},
            order_arrival=dict(self._order_arrival),
            first_fill_sequence=dict(self._first_fill_seq),
            contract_specs=dict(self._known_specs),
            trade_trading_days=dict(self._trade_trading_days),
            deposit=self._deposit,
            withdrawal=self._withdrawal,
            cash_flow_verified=self._cash_flow_verified,
            previous_settlement_equity=self._previous_settlement_equity,
            settlement_verified=self._settlement_verified,
            settlement_id=self._settlement_id,
        )

    def _snapshot_state(self) -> dict[str, object]:
        return encode_sim_broker_state(
            self._decoded_state(),
            initial_capital=float(self.initial_capital),
        )

    def _snapshot_market_state(self) -> dict[str, object]:
        store = self._state_store
        if store is None:
            raise SimBrokerStateIntegrityError("bounded market state requires durable broker truth")
        return encode_sim_market_state(
            DecodedSimMarketState(
                broker_sequence=store.sequence,
                broker_checksum=store.current_checksum,
                trading_day=self._trading_day,
                ticks=dict(self._ticks),
                tick_sequence=self._tick_seq,
                depth={symbol: list(depth) for symbol, depth in self._depth.items()},
                market_digest=calculate_market_digest(
                    self._trading_day,
                    self._ticks,
                    self._tick_seq,
                    self._depth,
                ),
            )
        )

    def _restore_state(self, state: DecodedSimBrokerState) -> None:
        self._accounting_baseline = state.accounting_baseline
        self.position_book = PositionBook(state.positions)
        self._orders = {order.order_id: order for order in state.orders}
        self._trades = list(state.trades)
        self._trade_trading_days = dict(state.trade_trading_days)
        self._ticks = dict(state.ticks)
        self._next_order_sequence = state.next_order_sequence
        self._next_trade_sequence = state.next_trade_sequence
        self._balance = state.balance
        self._realized_pnl = state.realized_pnl
        self._commission = state.commission
        self._trading_day = state.trading_day
        self._tick_seq = state.tick_sequence
        self._eligible_seq = dict(state.eligible_sequence)
        self._depth = {symbol: list(depth) for symbol, depth in state.depth.items()}
        self._order_arrival = dict(state.order_arrival)
        self._first_fill_seq = dict(state.first_fill_sequence)
        self._known_specs = dict(state.contract_specs)
        if self._dynamic_specs:
            self.specs = {}
            self._unverified_spec_symbols = {
                *(position.symbol for position in state.positions),
                *(order.request.symbol for order in state.orders if order.active),
            }
        elif encode_contract_specs(self.specs) != encode_contract_specs(self._known_specs):
            raise SimBrokerStateIntegrityError("persisted contract spec mismatch")
        self._deposit = state.deposit
        self._withdrawal = state.withdrawal
        self._cash_flow_verified = state.cash_flow_verified
        self._previous_settlement_equity = state.previous_settlement_equity
        self._settlement_verified = state.settlement_verified
        self._settlement_id = state.settlement_id

    def _restore_market_state(self, state: DecodedSimMarketState) -> None:
        store = self._state_store
        if store is None:
            raise SimBrokerStateIntegrityError("bounded market state lacks durable broker truth")
        if state.broker_sequence > store.sequence:
            raise SimBrokerStateIntegrityError("market state is ahead of durable broker truth")
        if state.broker_sequence < store.sequence:
            previous = store.previous_record
            absorbed_immediate_ancestor = bool(
                previous is not None
                and previous.sequence == state.broker_sequence + 1
                and previous.previous_checksum == state.broker_checksum
                and store.sequence == previous.sequence + 1
            )
            if not absorbed_immediate_ancestor:
                raise SimBrokerStateIntegrityError("market state ancestry cannot be proven")
            if previous is None:  # Kept total for type narrowing.
                raise SimBrokerStateIntegrityError("market state ancestry cannot be proven")
            absorbed_state = decode_sim_broker_state(
                previous.state,
                initial_capital=float(self.initial_capital),
            )
            absorbed_market_digest = calculate_market_digest(
                absorbed_state.trading_day,
                absorbed_state.ticks,
                absorbed_state.tick_sequence,
                absorbed_state.depth,
            )
            if state.market_digest != absorbed_market_digest:
                raise SimBrokerStateIntegrityError(
                    "stale market content was not absorbed by broker truth"
                )
            if state.tick_sequence > self._tick_seq:
                raise SimBrokerStateIntegrityError(
                    "stale market state contains uncommitted newer ticks"
                )
            return
        if state.broker_checksum != store.current_checksum:
            raise SimBrokerStateIntegrityError("market/broker truth checksum mismatch")
        if state.tick_sequence < self._tick_seq:
            raise SimBrokerStateIntegrityError("market state moved tick sequence backward")
        if self._trading_day and state.trading_day != self._trading_day:
            raise SimBrokerStateIntegrityError("market/broker trading day mismatch")
        self._trading_day = state.trading_day
        self._ticks = dict(state.ticks)
        self._tick_seq = state.tick_sequence
        self._depth = {symbol: list(depth) for symbol, depth in state.depth.items()}

    @contextmanager
    def _durable_operation(self, operation: str):
        store = self._state_store
        if store is None:
            yield
            return
        if self._market_batch_active:
            raise SimBrokerStateIntegrityError(
                "broker truth operation is not allowed inside a market batch"
            )
        if self._persistence_fault is not None:
            raise SimBrokerStateIntegrityError(
                "simulated-broker persistence is faulted; restart is required"
            ) from self._persistence_fault
        try:
            store.begin(self._snapshot_state(), operation)
        except BaseException as exc:
            self._persistence_fault = exc
            raise
        try:
            yield
        except BaseException as exc:
            self._persistence_fault = exc
            raise
        try:
            store.complete(self._snapshot_state(), operation)
        except BaseException as exc:
            self._persistence_fault = exc
            raise
        self._rebind_market_store()
        self._market_state_dirty = False

    @_lifecycle_locked
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._raw_market_connection_generation += 1
        self._notify_raw_market_connection(True)

    @_lifecycle_locked
    def stop(self) -> None:
        if self._started:
            self._notify_raw_market_connection(False)
        self.checkpoint_market_state()
        self._started = False

    def set_raw_tick_observer(self, observer) -> None:
        super().set_raw_tick_observer(observer)
        if observer is not None and self._started:
            self._notify_raw_market_connection(True)

    def _notify_raw_market_connection(self, connected: bool) -> None:
        observer = getattr(self, "_raw_tick_observer", None)
        callback = getattr(observer, "note_raw_market_connection", None)
        if callable(callback):
            callback(
                connected=connected,
                generation=self._raw_market_connection_generation,
            )

    @contextmanager
    def lifecycle_state_commit_fence(self):
        """Block all simulated account/position mutations during lifecycle CAS."""

        with self._lifecycle_state_lock:
            if self._market_batch_active:
                raise RuntimeError(
                    "sim lifecycle commit requires a completed market event batch"
                )
            yield

    def checkpoint_market_state(self) -> None:
        """Persist at most one dirty market snapshot at a caller-defined batch boundary."""
        if self._state_store is None:
            return
        if self._persistence_fault is not None:
            raise SimBrokerStateIntegrityError(
                "simulated-broker persistence is faulted; restart is required"
            ) from self._persistence_fault
        if not self._market_state_dirty:
            return
        self._advance_market_store("market_batch_checkpoint")
        self._market_state_dirty = False

    def _advance_market_store(self, operation: str) -> None:
        store = self._market_state_store
        if store is None:
            return
        try:
            store.begin(self._snapshot_market_state(), operation)
            store.complete(self._snapshot_market_state(), operation)
        except BaseException as exc:
            self._persistence_fault = exc
            raise

    def _rebind_market_store(self) -> None:
        if self._market_state_store is None:
            return
        self._advance_market_store("bind_broker_truth")

    def begin_market_batch(self, *, trading_day: str | None = None) -> None:
        """Write a pending marker before a broker event batch can mutate state."""
        if self._market_batch_active:
            raise SimBrokerStateIntegrityError("simulated market batch is already active")
        if trading_day is not None:
            self.synchronize_trading_day(trading_day)
        if self._persistence_fault is not None:
            raise SimBrokerStateIntegrityError(
                "simulated-broker persistence is faulted; restart is required"
            ) from self._persistence_fault
        self._market_batch_active = True
        self._market_batch_truth_pending = bool(
            self._state_store is not None and any(order.active for order in self._orders.values())
        )
        try:
            if self._market_batch_truth_pending:
                if self._state_store is None:  # Kept total for type narrowing.
                    raise SimBrokerStateIntegrityError("market batch lacks broker store")
                self._state_store.begin(self._snapshot_state(), "market_event_batch")
        except BaseException as exc:
            self._persistence_fault = exc
            self._market_batch_active = False
            raise

    def complete_market_batch(self) -> None:
        """Commit one event batch before its events may be returned to strategy code."""
        if not self._market_batch_active:
            raise SimBrokerStateIntegrityError("simulated market batch is not active")
        try:
            if self._market_batch_truth_pending:
                if self._state_store is None:  # Kept total for type narrowing.
                    raise SimBrokerStateIntegrityError("market batch lacks broker store")
                self._state_store.complete(self._snapshot_state(), "market_event_batch")
                self._rebind_market_store()
            elif self._market_state_store is not None and self._market_state_dirty:
                self._advance_market_store("market_event_batch")
        except BaseException as exc:
            self._persistence_fault = exc
            raise
        finally:
            self._market_batch_active = False
            self._market_batch_truth_pending = False
        self._market_state_dirty = False

    @contextmanager
    def market_batch(self, *, trading_day: str | None = None):
        """Explicit batch context for persistent Sim/replay tick injection."""
        self.begin_market_batch(trading_day=trading_day)
        try:
            yield
        except BaseException as exc:
            if self._state_store is not None:
                self._persistence_fault = exc
            self._market_batch_active = False
            self._market_batch_truth_pending = False
            raise
        self.complete_market_batch()

    def is_ready(self) -> bool:
        return self._started and not self._unverified_spec_symbols

    def subscribe(self, symbol: str, exchange: str) -> None:
        if symbol not in self.specs:
            raise KeyError(f"unknown contract: {symbol}")

    def get_live_contract_specs(
        self, symbols: list[str], timeout_seconds: float = 10.0
    ) -> dict[str, ContractSpec]:
        """模拟柜台直接返回本地参数，便于测试元数据安全门。"""
        return {symbol: self.specs[symbol] for symbol in symbols}

    @_lifecycle_locked
    def update_specs(self, specs: dict[str, ContractSpec]) -> None:
        """Install live specs, rejecting any reinterpretation of persisted truth."""
        normalized: dict[str, ContractSpec] = {}
        for symbol, spec in specs.items():
            if not isinstance(spec, ContractSpec) or spec.symbol != symbol:
                raise SimBrokerStateIntegrityError("contract spec manifest identity mismatch")
            previous = self._known_specs.get(symbol)
            if previous is not None and previous != spec:
                raise SimBrokerStateIntegrityError(f"contract spec mismatch for {symbol}")
            normalized[symbol] = spec
        additions = {
            symbol: spec for symbol, spec in normalized.items() if symbol not in self._known_specs
        }
        if additions and self._state_store is not None and not self._dynamic_specs:
            raise SimBrokerStateIntegrityError(
                "fixed persistent contract specs cannot accept additions"
            )
        if additions and self._state_store is not None:
            with self._durable_operation("update_specs"):
                self._known_specs.update(additions)
                self.specs.update(normalized)
                self._unverified_spec_symbols.difference_update(normalized)
            return
        self._known_specs.update(additions)
        self.specs.update(normalized)
        self._unverified_spec_symbols.difference_update(normalized)

    @staticmethod
    def _validated_trading_day(value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("simulated-broker trading day must be YYYYMMDD")
        try:
            parsed = datetime.strptime(value, "%Y%m%d").strftime("%Y%m%d")
        except ValueError as exc:
            raise ValueError("simulated-broker trading day must be YYYYMMDD") from exc
        if parsed != value:
            raise ValueError("simulated-broker trading day must be YYYYMMDD")
        return value

    def _account_values(self) -> tuple[float, float, float]:
        unrealized = 0.0
        margin = 0.0
        for position in self.position_book.all():
            spec = self.specs[position.symbol]
            tick = self._ticks.get(position.symbol)
            mark = (
                tick.last_price
                if tick is not None
                else max(position.long_price, position.short_price, 0.0)
            )
            unrealized += (mark - position.long_price) * position.long_total * spec.multiplier
            unrealized += (position.short_price - mark) * position.short_total * spec.multiplier
            margin += (
                mark
                * spec.multiplier
                * (
                    position.long_total * spec.margin_rate_long
                    + position.short_total * spec.margin_rate_short
                )
            )
        return unrealized, margin, self._balance + unrealized

    def _apply_trading_day(self, trading_day: str, completed_equity: float | None) -> None:
        if not self._trading_day:
            self._trading_day = trading_day
            return
        if trading_day == self._trading_day:
            return
        if completed_equity is None or not isfinite(completed_equity) or completed_equity <= 0:
            raise SimBrokerStateIntegrityError("simulated settlement equity is invalid")
        completed_trading_day = self._trading_day
        if self._durable_session_compaction:
            for order in self._orders.values():
                if order.active:
                    order.status = OrderStatus.CANCELLED
                    self._events.append(BrokerEvent("order", order))
        self.position_book.roll_trading_day()
        self._previous_settlement_equity = float(completed_equity)
        self._settlement_verified = True
        self._settlement_id += 1
        self._deposit = 0.0
        self._withdrawal = 0.0
        self._cash_flow_verified = True
        self._trading_day = trading_day
        if self._durable_session_compaction:
            self._ticks.clear()
            self._depth.clear()
            self._compact_session_history(completed_trading_day)

    def _compact_session_history(self, completed_trading_day: str) -> None:
        """Fold the completed session into a verified cumulative accounting baseline."""
        baseline = self._accounting_baseline
        history_digest = advance_accounting_history_digest(
            baseline.history_digest,
            list(self._orders.values()),
            list(self._trades),
            self._trade_trading_days,
            compacted_through_trading_day=completed_trading_day,
        )
        self._accounting_baseline = SimAccountingBaseline(
            balance=float(self._balance),
            realized_pnl=float(self._realized_pnl),
            commission=float(self._commission),
            trading_day=self._trading_day,
            positions=self.position_book.all(),
            next_order_sequence=self._next_order_sequence,
            next_trade_sequence=self._next_trade_sequence,
            compacted_order_count=(baseline.compacted_order_count + len(self._orders)),
            compacted_trade_count=(baseline.compacted_trade_count + len(self._trades)),
            compacted_through_trading_day=completed_trading_day,
            history_digest=history_digest,
        )
        self._orders.clear()
        self._trades.clear()
        self._trade_trading_days.clear()
        self._eligible_seq.clear()
        self._order_arrival.clear()
        self._first_fill_seq.clear()

    @_lifecycle_locked
    def synchronize_trading_day(self, trading_day: str) -> None:
        """Advance durable simulated settlement before Shadow exposes a live new day."""
        normalized = self._validated_trading_day(trading_day)
        if normalized == self._trading_day:
            return
        if self._trading_day and normalized < self._trading_day:
            raise SimBrokerStateIntegrityError("simulated-broker trading day moved backward")
        completed_equity = self._account_values()[2] if self._trading_day else None
        with self._durable_operation("synchronize_trading_day"):
            self._apply_trading_day(normalized, completed_equity)

    def get_contract_catalog(self) -> list[ContractInfo]:
        """历史回放只暴露当日已经挂牌的合约，实盘空 listing 语义保持不变。"""
        if not self._trading_day:
            return list(self._contract_catalog)
        try:
            today = datetime.strptime(self._trading_day, "%Y%m%d").date()
        except ValueError:
            return list(self._contract_catalog)

        visible: list[ContractInfo] = []
        for item in self._contract_catalog:
            if item.listing:
                try:
                    listing = date.fromisoformat(item.listing)
                except ValueError:
                    # 历史可见性元数据损坏时 fail-closed，避免未来合约泄漏进 Universe。
                    continue
                if listing > today:
                    continue
            visible.append(item)
        return visible

    def _displayed_depth(self, volume: float) -> int:
        raw = max(0, int(volume))
        if not self.conservative:
            return raw
        return max(0, int(raw * self.depth_haircut))

    @_lifecycle_locked
    def publish_tick(self, tick: Tick) -> None:
        tick.validate()
        trading_day = self._validated_trading_day(tick.trading_day)
        if self._strict_tick_trading_day and self._trading_day and trading_day < self._trading_day:
            raise SimBrokerStateIntegrityError("simulated-broker trading day moved backward")
        has_active_orders = any(order.active for order in self._orders.values())
        if self._state_store is not None:
            if self._trading_day and trading_day != self._trading_day:
                raise RuntimeError(
                    "persistent tick day transition requires an explicit market batch "
                    "prepared with trading_day"
                )
            if has_active_orders and (
                not self._market_batch_active or not self._market_batch_truth_pending
            ):
                raise RuntimeError("persistent active-order tick requires an explicit market batch")
        try:
            self._notify_raw_tick_observer(
                tick,
                self._contract_catalog_by_symbol.get(tick.symbol),
            )
        except RawMarketEvidenceError:
            # The observer already made the affected evidence day incomplete.
            # The same valid market Tick must still reach matching and the manager.
            pass
        except Exception as exc:
            self._events.append(
                BrokerEvent("broker_error", f"raw Tick observer failed: {type(exc).__name__}")
            )
            return
        completed_equity = (
            self._account_values()[2]
            if self._trading_day and trading_day != self._trading_day
            else None
        )
        self._apply_tick(tick, completed_equity)
        if self._state_store is not None:
            self._market_state_dirty = True

    def _apply_tick(self, tick: Tick, completed_equity: float | None) -> None:
        self._apply_trading_day(tick.trading_day, completed_equity)
        self._tick_seq += 1
        self._ticks[tick.symbol] = tick
        # 每个新 Tick 只有一份一档深度；同一 Tick 内的多个订单共享并消耗这份深度。
        # 保守模式可对显示深度做固定 haircut，用于模拟排队优先级和不可获得的队列份额。
        self._depth[tick.symbol] = [
            self._displayed_depth(tick.bid_volume),
            self._displayed_depth(tick.ask_volume),
        ]
        # 当前行情可能使上一轮延迟 FAK/FOK 首次具备成交资格。先产生这些成交/撤单
        # 回报，再把同一行情交给策略生成新决策，避免 broker 内部仓位已经变化而
        # TradingEngine 的期望状态尚未推进的时间倒置。
        self._match_symbol(tick.symbol)
        self._events.append(BrokerEvent("tick", tick))

    @_lifecycle_locked
    def send_order(self, request: OrderRequest) -> str:
        if not self._started:
            raise RuntimeError("sim broker is not started")
        if request.symbol not in self.specs:
            raise KeyError(f"unknown contract: {request.symbol}")
        if request.volume <= 0 or request.price <= 0:
            raise ValueError("invalid order request")

        with self._durable_operation("send_order"):
            order_id = f"SIM-{self._next_order_sequence}"
            self._next_order_sequence += 1
            order = Order(
                order_id=order_id,
                request=request,
                status=OrderStatus.NOT_TRADED,
            )
            self._orders[order_id] = order
            arrival = self._ticks.get(request.symbol)
            if arrival is not None:
                self._order_arrival[order_id] = (
                    float(arrival.bid_price),
                    float(arrival.ask_price),
                    self._tick_seq,
                )
            self._events.append(BrokerEvent("order", order))
            self._eligible_seq[order_id] = self._tick_seq + self.latency_ticks

            if not self.conservative or self.latency_ticks == 0:
                self._match_order(order)
                self._cancel_ioc_remainder(order)
        return order_id

    @_lifecycle_locked
    def cancel_order(self, order_id: str) -> None:
        with self._durable_operation("cancel_order"):
            order = self._orders.get(order_id)
            if order and order.active:
                order.status = OrderStatus.CANCELLED
                self._events.append(BrokerEvent("order", order))

    @_lifecycle_locked
    def get_order(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    @_lifecycle_locked
    def owns_order(self, order_id: str) -> bool:
        """Prove current or compacted local order identity without retaining history."""
        if order_id in self._orders:
            return True
        prefix = "SIM-"
        if not isinstance(order_id, str) or not order_id.startswith(prefix):
            return False
        sequence_text = order_id[len(prefix) :]
        if not sequence_text.isdigit() or sequence_text.startswith("0"):
            return False
        sequence = int(sequence_text)
        return 0 < sequence < self._accounting_baseline.next_order_sequence

    @_lifecycle_locked
    def get_active_orders(self) -> list[Order]:
        return [order for order in self._orders.values() if order.active]

    def get_trades(self) -> list[Trade]:
        return list(self._trades)

    @_lifecycle_locked
    def get_session_trades(self) -> list[Trade]:
        return self.get_trades()

    def get_orders(self) -> list[Order]:
        return list(self._orders.values())

    @_lifecycle_locked
    def get_positions(self) -> list[ContractPosition]:
        return self.position_book.all()

    def get_execution_stress_summary(self) -> dict[str, float | int]:
        """Summarize observed matching friction without influencing broker behavior."""
        requested = sum(int(order.request.volume) for order in self._orders.values())
        filled = sum(int(order.traded) for order in self._orders.values())
        unfilled = max(0, requested - filled)
        turnover = 0.0
        spread_cost = 0.0
        slippage_impact_cost = 0.0
        commission_cost = 0.0
        latency_volume = 0.0
        latency_weight = 0
        for trade in self._trades:
            spec = self.specs[trade.symbol]
            notional_scale = float(trade.volume) * float(spec.multiplier)
            turnover += abs(float(trade.price)) * notional_scale
            commission_cost += float(trade.commission)
            order = self._orders.get(trade.order_id)
            arrival = self._order_arrival.get(trade.order_id)
            if order is None or arrival is None:
                continue
            bid, ask, submit_seq = arrival
            mid = (bid + ask) / 2.0
            if order.request.side is OrderSide.BUY:
                best = ask
                spread_cost += max(0.0, best - mid) * notional_scale
                slippage_impact_cost += (float(trade.price) - best) * notional_scale
            else:
                best = bid
                spread_cost += max(0.0, mid - best) * notional_scale
                slippage_impact_cost += (best - float(trade.price)) * notional_scale
            fill_seq = self._first_fill_seq.get(trade.order_id, submit_seq)
            latency_volume += max(0, fill_seq - submit_seq) * int(trade.volume)
            latency_weight += int(trade.volume)
        return {
            "order_count": len(self._orders),
            "trade_count": len(self._trades),
            "requested_volume": requested,
            "filled_volume": filled,
            "unfilled_volume": unfilled,
            "fill_ratio": float(filled / requested) if requested else 0.0,
            "turnover_notional": float(turnover),
            "spread_cost": float(spread_cost),
            "slippage_impact_cost": float(slippage_impact_cost),
            "commission_cost": float(commission_cost),
            "total_execution_cost": float(spread_cost + slippage_impact_cost + commission_cost),
            "volume_weighted_latency_ticks": float(latency_volume / latency_weight)
            if latency_weight
            else 0.0,
        }

    @_lifecycle_locked
    def get_account(self) -> AccountSnapshot:
        unrealized, margin, equity = self._account_values()
        return AccountSnapshot(
            balance=self._balance,
            equity=equity,
            available=equity - margin,
            margin=margin,
            realized_pnl=self._realized_pnl - self._commission,
            unrealized_pnl=unrealized,
            trading_day=(self._trading_day or datetime.now(timezone.utc).strftime("%Y%m%d")),
            deposit=self._deposit,
            withdrawal=self._withdrawal,
            cash_flow_verified=self._cash_flow_verified,
            previous_settlement_equity=self._previous_settlement_equity,
            settlement_verified=self._settlement_verified,
            settlement_id=self._settlement_id,
        )

    @_lifecycle_locked
    def poll_events(self) -> list[BrokerEvent]:
        events = list(self._events)
        self._events.clear()
        return events

    def _match_symbol(self, symbol: str) -> None:
        for order in list(self._orders.values()):
            if not order.active or order.request.symbol != symbol:
                continue
            self._match_order(order)
            if self._tick_seq >= self._eligible_seq.get(order.order_id, 0):
                self._cancel_ioc_remainder(order)

    def _match_order(self, order: Order) -> None:
        tick = self._ticks.get(order.request.symbol)
        if (
            tick is None
            or (self._strict_tick_trading_day and tick.trading_day != self._trading_day)
            or not order.active
        ):
            return
        eligible = self._eligible_seq.get(order.order_id, 0)
        if self.conservative and self._tick_seq < eligible:
            return

        request = order.request
        depth = self._depth.setdefault(
            request.symbol,
            [
                self._displayed_depth(tick.bid_volume),
                self._displayed_depth(tick.ask_volume),
            ],
        )
        if request.side is OrderSide.BUY:
            marketable = request.price >= tick.ask_price
            available = depth[1]
            raw_price = tick.ask_price
            sign = 1
            depth_index = 1
        else:
            marketable = request.price <= tick.bid_price
            available = depth[0]
            raw_price = tick.bid_price
            sign = -1
            depth_index = 0
        if not marketable or available <= 0:
            return

        remaining = request.volume - order.traded
        if request.order_type is OrderType.FOK and available < remaining:
            return
        fill_volume = min(remaining, available)
        spec = self.specs[request.symbol]
        impact_ticks = self.market_impact_ticks if self.conservative else 0
        if self.conservative and self.size_impact_ticks > 0:
            # L1 cannot reveal deeper-book prices. Penalize orders that ask for more than
            # the queue-adjusted opposite depth by one declared tick per additional full
            # depth multiple. This changes price only; FAK fill quantity remains limited
            # by the actually available L1 queue above.
            extra_multiples = max(0, int(ceil(remaining / available)) - 1)
            impact_ticks += extra_multiples * self.size_impact_ticks
        fill_price = raw_price + sign * (self.slippage_ticks + impact_ticks) * spec.price_tick
        if request.side is OrderSide.BUY:
            fill_price = min(fill_price, float(request.price))
            if tick.limit_up > 0:
                fill_price = min(fill_price, tick.limit_up)
        else:
            fill_price = max(fill_price, float(request.price))
            if tick.limit_down > 0:
                fill_price = max(fill_price, tick.limit_down)

        depth[depth_index] -= fill_volume
        self._fill(order, fill_volume, fill_price)

    def _cancel_ioc_remainder(self, order: Order) -> None:
        if order.request.order_type in {OrderType.FAK, OrderType.FOK} and order.active:
            order.status = OrderStatus.CANCELLED
            self._events.append(BrokerEvent("order", order))

    def _fill(self, order: Order, volume: int, price: float) -> None:
        previous_traded = order.traded
        order.traded += volume
        order.average_price = (
            order.average_price * previous_traded + price * volume
        ) / order.traded
        order.status = (
            OrderStatus.FILLED if order.traded == order.request.volume else OrderStatus.PART_TRADED
        )
        self._first_fill_seq.setdefault(order.order_id, self._tick_seq)

        tick = self._ticks[order.request.symbol]
        trade = Trade(
            trade_id=f"SIM-T-{self._next_trade_sequence}",
            order_id=order.order_id,
            symbol=order.request.symbol,
            exchange=order.request.exchange,
            side=order.request.side,
            offset=order.request.offset,
            volume=volume,
            price=price,
            timestamp=tick.timestamp,
        )
        self._next_trade_sequence += 1
        realized_points = self.position_book.apply_trade(trade)
        spec = self.specs[trade.symbol]
        realized = realized_points * spec.multiplier
        commission = calculate_commission(spec, trade.offset, trade.price, trade.volume)
        self._realized_pnl += realized
        self._commission += commission
        self._balance += realized - commission

        trade = Trade(**{**trade.__dict__, "commission": commission})
        self._trades.append(trade)
        self._trade_trading_days[trade.trade_id] = self._trading_day
        self._events.extend([BrokerEvent("trade", trade), BrokerEvent("order", order)])
