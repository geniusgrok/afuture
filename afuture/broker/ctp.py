"""基于 VeighNa ``vnpy_ctp`` 的 CTP 柜台适配器。

个人期货账户通常通过期货公司的 CTP 交易/行情前置接入交易所。
模块在运行实盘命令时才导入二进制依赖，研究和测试环境不需要安装 CTP。
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from math import isfinite
from threading import Event, Lock
from time import monotonic, sleep
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

from ..models import (
    AccountSnapshot,
    BrokerEvent,
    ContractInfo,
    ContractPosition,
    ContractSpec,
    FeeSpec,
    Offset,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Tick,
    Trade,
)
from ..position import PositionBook
from .base import Broker


class _RateWaiter(TypedDict):
    kind: str
    event: Event
    rows: list[dict[str, Any]]
    error: str


@dataclass(frozen=True)
class CtpCredentials:
    """CTP 连接参数。敏感字段从环境变量注入。"""

    user_id: str
    password: str
    broker_id: str
    td_address: str
    md_address: str
    app_id: str
    auth_code: str
    environment: str = "test"


def build_ctp_setting(credentials: CtpCredentials) -> dict[str, str]:
    """构造当前 ``CtpGateway`` 使用的中文配置键。"""
    return {
        "用户名": credentials.user_id,
        "密码": credentials.password,
        "经纪商代码": credentials.broker_id,
        "交易服务器": credentials.td_address,
        "行情服务器": credentials.md_address,
        "产品名称": credentials.app_id,
        "授权编码": credentials.auth_code,
        "柜台环境": "测试" if credentials.environment.lower() == "test" else "实盘",
    }


class CtpBroker(Broker):
    """把 VeighNa CTP 对象转换为 afuture 内部模型。"""

    gateway_name = "CTP"
    _MAX_SEEN_TRADE_KEYS = 10_000

    def __init__(
        self,
        credentials: CtpCredentials,
        *,
        snapshot_stale_seconds: float = 20.0,
        max_events_per_poll: int = 100,
    ) -> None:
        if snapshot_stale_seconds <= 0:
            raise ValueError("snapshot_stale_seconds must be positive")
        if (
            isinstance(max_events_per_poll, bool)
            or not isinstance(max_events_per_poll, int)
            or max_events_per_poll <= 0
        ):
            raise ValueError("max_events_per_poll must be a positive integer")
        self.credentials = credentials
        self.snapshot_stale_seconds = snapshot_stale_seconds
        self.max_events_per_poll = max_events_per_poll
        self._runtime: dict[str, Any] | None = None
        # VeighNa is an optional runtime dependency.  Keep its dynamic objects at
        # the adapter boundary instead of leaking ``Any`` into domain models.
        self._event_engine: Any | None = None
        self._main_engine: Any | None = None
        self._critical_events: deque[BrokerEvent] = deque()
        self._latest_ticks: dict[tuple[str, str], BrokerEvent] = {}
        self._event_lock = Lock()
        # Lock order is position -> event whenever both are needed. Event polling never
        # acquires the position lock, so snapshot/trade state and their events stay ordered.
        self._position_lock = Lock()
        self._critical_enqueued = 0
        self._ticks_received = 0
        self._ticks_coalesced = 0
        self._critical_delivered = 0
        self._ticks_delivered = 0
        self._order_references: dict[str, str] = {}
        self._last_account: AccountSnapshot | None = None
        self._positions: dict[tuple[str, str], ContractPosition] = {}
        self._trading_day = ""
        self._account_event_generation = 0
        self._position_snapshot_generation = 0
        self._last_account_monotonic = 0.0
        self._last_position_snapshot_monotonic = 0.0
        self._contract_catalog: dict[str, ContractInfo] = {}
        self._seen_trade_keys: set[str] = set()
        self._seen_trade_order: deque[str] = deque()

    def seed_trade_identities(self, identities: list[str]) -> None:
        """Restore durable, exchange-qualified fill identities before callbacks start."""
        if self._main_engine is not None or self._event_engine is not None:
            raise RuntimeError("CTP trade identities must be seeded before broker start")

        qualified: list[str] = []
        for identity in identities:
            if not isinstance(identity, str):
                continue
            parts = identity.split(":", 2)
            if len(parts) != 3:
                continue
            trading_day, exchange, trade_id = parts
            try:
                parsed_day = datetime.strptime(trading_day, "%Y%m%d")
            except ValueError:
                continue
            if (
                parsed_day.strftime("%Y%m%d") != trading_day
                or not re.fullmatch(r"[A-Z][A-Z0-9]*", exchange)
                or not trade_id
            ):
                continue
            qualified.append(identity)

        bounded = qualified[-self._MAX_SEEN_TRADE_KEYS :]
        self._seen_trade_keys = set(bounded)
        self._seen_trade_order = deque(dict.fromkeys(bounded))

    def _load_runtime(self) -> dict[str, Any]:
        """延迟加载实盘依赖，并扩展完整持仓快照和费率查询回调。"""
        if self._runtime is not None:
            return self._runtime
        try:
            from vnpy.event import EventEngine
            from vnpy.trader.constant import Direction, Exchange, Status
            from vnpy.trader.constant import Offset as VnOffset
            from vnpy.trader.constant import OrderType as VnOrderType
            from vnpy.trader.engine import MainEngine
            from vnpy.trader.event import EVENT_ACCOUNT, EVENT_ORDER, EVENT_TICK, EVENT_TRADE
            from vnpy.trader.object import OrderRequest as VnOrderRequest
            from vnpy.trader.object import SubscribeRequest
            from vnpy_ctp.gateway.ctp_gateway import CtpGateway, CtpTdApi
        except ImportError as exc:
            raise RuntimeError(
                "CTP live dependencies are missing; install with: pip install -e '.[live]'"
            ) from exc

        class TrackedCtpTdApi(CtpTdApi):
            """在官方交易 API 上增加查询完成边界，不改动官方下单逻辑。"""

            def __init__(self, gateway):
                super().__init__(gateway)
                self._afuture_rate_waiters: dict[int, _RateWaiter] = {}

            def onRspQryInvestorPosition(self, data, error, reqid, last):
                error_id = int((error or {}).get("ErrorID", 0))
                if error_id:
                    super().onRspQryInvestorPosition(data, error, reqid, last)
                    return
                if not last:
                    super().onRspQryInvestorPosition(data, error, reqid, False)
                    return
                if data:
                    super().onRspQryInvestorPosition(data, error, reqid, False)
                snapshot = list(self.positions.values())
                for position in snapshot:
                    self.gateway.on_position(position)
                self.positions.clear()
                callback = getattr(self.gateway, "_afuture_position_snapshot_callback", None)
                if callable(callback):
                    callback(snapshot)

            def onRspQryInstrument(self, data, error, reqid, last):
                # 先交给官方实现维护 ContractData/contract_inited，再把原始到期日暴露给 afuture。
                super().onRspQryInstrument(data, error, reqid, last)
                if int((error or {}).get("ErrorID", 0)) or not data:
                    return
                callback = getattr(self.gateway, "_afuture_contract_metadata_callback", None)
                if callable(callback):
                    callback(dict(data))

            def _capture_rate(self, kind: str, data, error, reqid: int, last: bool) -> None:
                waiter = self._afuture_rate_waiters.get(reqid)
                if waiter is None or waiter.get("kind") != kind:
                    return
                if int((error or {}).get("ErrorID", 0)):
                    waiter["error"] = str(
                        (error or {}).get("ErrorMsg", "CTP metadata query failed")
                    )
                elif data:
                    waiter["rows"].append(dict(data))
                if last:
                    waiter["event"].set()

            def onRspQryInstrumentMarginRate(self, data, error, reqid, last):
                self._capture_rate("margin", data, error, reqid, last)

            def onRspQryInstrumentCommissionRate(self, data, error, reqid, last):
                self._capture_rate("commission", data, error, reqid, last)

        class TrackedCtpGateway(CtpGateway):
            default_name = "CTP"

            def __init__(self, event_engine, gateway_name):
                super().__init__(event_engine, gateway_name)
                self._afuture_position_snapshot_callback = None
                self._afuture_contract_metadata_callback = None
                # super 创建的交易 API 还未连接，直接替换不会遗留会话。
                self.td_api = TrackedCtpTdApi(self)

        self._runtime = {
            "EventEngine": EventEngine,
            "MainEngine": MainEngine,
            "CtpGateway": TrackedCtpGateway,
            "Direction": Direction,
            "Exchange": Exchange,
            "Offset": VnOffset,
            "OrderType": VnOrderType,
            "Status": Status,
            "OrderRequest": VnOrderRequest,
            "SubscribeRequest": SubscribeRequest,
            "EVENT_ACCOUNT": EVENT_ACCOUNT,
            "EVENT_ORDER": EVENT_ORDER,
            "EVENT_TICK": EVENT_TICK,
            "EVENT_TRADE": EVENT_TRADE,
        }
        return self._runtime

    def start(self) -> None:
        runtime = self._load_runtime()
        self._event_engine = runtime["EventEngine"]()
        self._main_engine = runtime["MainEngine"](self._event_engine)
        self._main_engine.add_gateway(runtime["CtpGateway"])
        gateway = self._main_engine.get_gateway(self.gateway_name)
        if gateway is None:
            raise RuntimeError("CTP gateway could not be created")
        gateway._afuture_position_snapshot_callback = self._handle_position_snapshot
        gateway._afuture_contract_metadata_callback = self._handle_contract_metadata
        self._event_engine.register(runtime["EVENT_TICK"], self._on_tick)
        self._event_engine.register(runtime["EVENT_ORDER"], self._on_order)
        self._event_engine.register(runtime["EVENT_TRADE"], self._on_trade)
        self._event_engine.register(runtime["EVENT_ACCOUNT"], self._on_account)
        self._main_engine.connect(build_ctp_setting(self.credentials), self.gateway_name)

    def stop(self) -> None:
        if self._main_engine is not None:
            self._main_engine.close()
        self._main_engine = None
        self._event_engine = None

    def is_ready(self) -> bool:
        if self._main_engine is None:
            return False
        gateway = self._main_engine.get_gateway(self.gateway_name)
        if gateway is None:
            return False
        td_api = getattr(gateway, "td_api", None)
        md_api = getattr(gateway, "md_api", None)
        return bool(
            td_api
            and md_api
            and getattr(td_api, "login_status", False)
            and getattr(td_api, "contract_inited", False)
            and getattr(md_api, "login_status", False)
        )

    def health_error(self, *, now_monotonic: float | None = None) -> str | None:
        if not self.is_ready():
            return None
        now = monotonic() if now_monotonic is None else now_monotonic
        if self._last_account_monotonic <= 0 or self._last_position_snapshot_monotonic <= 0:
            return "CTP account/position snapshot is not initialized"
        if now - self._last_account_monotonic > self.snapshot_stale_seconds:
            return "CTP account snapshot is stale"
        if now - self._last_position_snapshot_monotonic > self.snapshot_stale_seconds:
            return "CTP position snapshot is stale"
        return None

    def snapshot_marker(self) -> tuple[int, int]:
        return self._account_event_generation, self._position_snapshot_generation

    def snapshot_ready(self, marker: tuple[int, int]) -> bool:
        account_generation, position_generation = marker
        return bool(
            self._last_account is not None
            and self._account_event_generation > account_generation
            and self._position_snapshot_generation > position_generation
        )

    def subscribe(self, symbol: str, exchange: str) -> None:
        if self._main_engine is None:
            raise RuntimeError("CTP broker is not started")
        runtime = self._load_runtime()
        request = runtime["SubscribeRequest"](
            symbol=symbol,
            exchange=self._exchange(exchange),
        )
        self._main_engine.subscribe(request, self.gateway_name)

    def send_order(self, request: OrderRequest) -> str:
        if not self.is_ready() or self._main_engine is None:
            raise RuntimeError("CTP market/trading session is not ready")
        order_id = self._main_engine.send_order(self._to_vnpy_order(request), self.gateway_name)
        if not order_id:
            raise RuntimeError("CTP order request was not accepted by gateway")
        self._order_references[order_id] = request.reference
        return order_id

    def owns_order(self, order_id: str) -> bool:
        return order_id in self._order_references

    def cancel_order(self, order_id: str) -> None:
        if self._main_engine is None:
            return
        order = self._main_engine.get_order(order_id)
        if order is not None:
            self._main_engine.cancel_order(order.create_cancel_request(), self.gateway_name)

    def get_order(self, order_id: str) -> Order | None:
        if self._main_engine is None:
            return None
        raw = self._main_engine.get_order(order_id)
        return self._convert_order(raw) if raw else None

    def get_active_orders(self) -> list[Order]:
        if self._main_engine is None:
            return []
        return [self._convert_order(order) for order in self._main_engine.get_all_active_orders()]

    def get_account(self) -> AccountSnapshot:
        if self._main_engine is not None:
            accounts = self._main_engine.get_all_accounts()
            if accounts:
                self._last_account = self._convert_account(accounts[0])
        if self._last_account is None:
            raise RuntimeError("CTP account snapshot is not available")
        return self._last_account

    def get_positions(self) -> list[ContractPosition]:
        with self._position_lock:
            return self._copy_positions_unlocked()

    def _copy_positions_unlocked(self) -> list[ContractPosition]:
        return [replace(position) for position in self._positions.values() if not position.empty]

    def poll_events(self) -> list[BrokerEvent]:
        result: list[BrokerEvent] = []
        with self._event_lock:
            while self._critical_events and len(result) < self.max_events_per_poll:
                result.append(self._critical_events.popleft())
                self._critical_delivered += 1
            while self._latest_ticks and len(result) < self.max_events_per_poll:
                key = next(iter(self._latest_ticks))
                result.append(self._latest_ticks.pop(key))
                self._ticks_delivered += 1
        return result

    def delivery_counters(self) -> dict[str, int]:
        """Return a lock-consistent delivery snapshot for lightweight observability."""
        with self._event_lock:
            return {
                "critical_enqueued": self._critical_enqueued,
                "ticks_received": self._ticks_received,
                "ticks_coalesced": self._ticks_coalesced,
                "critical_delivered": self._critical_delivered,
                "ticks_delivered": self._ticks_delivered,
                "critical_backlog": len(self._critical_events),
                "tick_backlog": len(self._latest_ticks),
            }

    def _enqueue_critical(self, event: BrokerEvent) -> None:
        with self._event_lock:
            self._critical_events.append(event)
            self._critical_enqueued += 1

    def _enqueue_tick(self, tick: Tick) -> None:
        key = (tick.symbol, tick.exchange)
        with self._event_lock:
            self._ticks_received += 1
            if key in self._latest_ticks:
                self._ticks_coalesced += 1
            self._latest_ticks[key] = BrokerEvent("tick", tick)

    def get_trading_day(self) -> str:
        if self._main_engine is not None:
            gateway = self._main_engine.get_gateway(self.gateway_name)
            if gateway is None:
                raise RuntimeError("CTP trading day gateway is unavailable")
            td_api = getattr(gateway, "td_api", None)
            if td_api is None:
                raise RuntimeError("CTP trading day API is unavailable")
            getter = getattr(td_api, "getTradingDay", None)
            if not callable(getter):
                raise RuntimeError("CTP trading day getter is unavailable")
            try:
                value = getter()
            except Exception as exc:
                raise RuntimeError("CTP trading day query failed") from exc
            if isinstance(value, bytes):
                try:
                    value = value.decode("ascii")
                except UnicodeDecodeError as exc:
                    raise RuntimeError("CTP trading day is invalid") from exc
            self._trading_day = self._validate_trading_day(value)
        return self._validate_trading_day(self._trading_day)

    @staticmethod
    def _validate_trading_day(value: object) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"\d{8}", value):
            raise RuntimeError("CTP trading day is missing or invalid")
        try:
            parsed = datetime.strptime(value, "%Y%m%d")
        except ValueError as exc:
            raise RuntimeError("CTP trading day is missing or invalid") from exc
        if parsed.strftime("%Y%m%d") != value:
            raise RuntimeError("CTP trading day is missing or invalid")
        return value

    def get_contract_catalog(self) -> list[ContractInfo]:
        """返回 CTP 合约查询得到的期货目录，供自动构建相邻月份。"""
        return sorted(
            self._contract_catalog.values(),
            key=lambda item: (item.product.lower(), item.expiry, item.symbol),
        )

    def _handle_contract_metadata(self, data: dict) -> None:
        """提取自动发现真正需要的少量字段，并过滤期权/组合合约。"""
        symbol = str(data.get("InstrumentID", "")).strip()
        exchange = str(data.get("ExchangeID", "")).strip().upper()
        product = str(data.get("ProductID", "")).strip()
        expiry_raw = str(data.get("ExpireDate", "")).strip()
        if not symbol or not exchange or not re.fullmatch(r"[A-Za-z]{1,4}\d{3,4}", symbol):
            return
        if not product:
            match = re.match(r"[A-Za-z]+", symbol)
            product = match.group(0) if match else ""
        try:
            expiry = datetime.strptime(expiry_raw, "%Y%m%d").date().isoformat()
        except ValueError:
            return
        self._contract_catalog[symbol] = ContractInfo(
            symbol=symbol,
            exchange=exchange,
            product=product,
            expiry=expiry,
        )

    def get_live_contract_specs(
        self, symbols: list[str], timeout_seconds: float = 10.0
    ) -> dict[str, ContractSpec]:
        """从 CTP/VeighNa 获取乘数、tick、保证金和手续费用于启动安全门。

        查询失败、固定金额保证金等无法可靠映射的情况一律报错，由上层 fail-closed。
        """
        if not self.is_ready() or self._main_engine is None:
            raise RuntimeError("CTP is not ready for metadata query")
        gateway = self._main_engine.get_gateway(self.gateway_name)
        td_api = getattr(gateway, "td_api", None) if gateway else None
        if td_api is None:
            raise RuntimeError("CTP trading API is unavailable")
        contract_map = {c.symbol: c for c in self._main_engine.get_all_contracts()}
        deadline = monotonic() + max(timeout_seconds, 0.1)
        result: dict[str, ContractSpec] = {}
        for symbol in symbols:
            contract = contract_map.get(symbol)
            if contract is None:
                raise RuntimeError(f"CTP contract metadata missing: {symbol}")
            margin = self._query_rate(td_api, "margin", symbol, deadline)
            commission = self._query_rate(td_api, "commission", symbol, deadline)
            long_by_volume = float(margin.get("LongMarginRatioByVolume", 0.0) or 0.0)
            short_by_volume = float(margin.get("ShortMarginRatioByVolume", 0.0) or 0.0)
            if long_by_volume > 0 or short_by_volume > 0:
                raise RuntimeError(f"fixed-per-lot margin is unsupported for validation: {symbol}")
            fee = FeeSpec(
                open_fixed=float(commission.get("OpenRatioByVolume", 0.0) or 0.0),
                open_rate=float(commission.get("OpenRatioByMoney", 0.0) or 0.0),
                close_fixed=float(commission.get("CloseRatioByVolume", 0.0) or 0.0),
                close_rate=float(commission.get("CloseRatioByMoney", 0.0) or 0.0),
                close_today_fixed=float(commission.get("CloseTodayRatioByVolume", 0.0) or 0.0),
                close_today_rate=float(commission.get("CloseTodayRatioByMoney", 0.0) or 0.0),
            )
            result[symbol] = ContractSpec(
                symbol=symbol,
                exchange=contract.exchange.value,
                multiplier=float(contract.size),
                price_tick=float(contract.pricetick),
                margin_rate_long=float(margin.get("LongMarginRatioByMoney", 0.0) or 0.0),
                margin_rate_short=float(margin.get("ShortMarginRatioByMoney", 0.0) or 0.0),
                fee=fee,
            )
        return result

    def _query_rate(self, td_api, kind: str, symbol: str, deadline: float) -> dict:
        reqid = int(getattr(td_api, "reqid", 0)) + 1
        td_api.reqid = reqid
        waiter: _RateWaiter = {
            "kind": kind,
            "event": Event(),
            "rows": [],
            "error": "",
        }
        td_api._afuture_rate_waiters[reqid] = waiter
        try:
            while True:
                if monotonic() >= deadline:
                    raise RuntimeError(f"CTP {kind} query timeout: {symbol}")
                if kind == "margin":
                    status = td_api.reqQryInstrumentMarginRate(
                        {"InstrumentID": symbol, "HedgeFlag": "1"}, reqid
                    )
                else:
                    status = td_api.reqQryInstrumentCommissionRate({"InstrumentID": symbol}, reqid)
                if not status:
                    break
                sleep(0.2)
            remaining = max(0.0, deadline - monotonic())
            if not waiter["event"].wait(remaining):
                raise RuntimeError(f"CTP {kind} query timeout: {symbol}")
            if waiter["error"]:
                raise RuntimeError(f"CTP {kind} query failed for {symbol}: {waiter['error']}")
            if not waiter["rows"]:
                raise RuntimeError(f"CTP {kind} query returned no data: {symbol}")
            return waiter["rows"][-1]
        finally:
            td_api._afuture_rate_waiters.pop(reqid, None)

    def _to_vnpy_order(self, request: OrderRequest):
        runtime = self._load_runtime()
        direction = (
            runtime["Direction"].LONG
            if request.side is OrderSide.BUY
            else runtime["Direction"].SHORT
        )
        offset_map = {
            Offset.OPEN: runtime["Offset"].OPEN,
            Offset.CLOSE: runtime["Offset"].CLOSE,
            Offset.CLOSE_TODAY: runtime["Offset"].CLOSETODAY,
            Offset.CLOSE_YESTERDAY: runtime["Offset"].CLOSEYESTERDAY,
        }
        type_map = {
            OrderType.LIMIT: runtime["OrderType"].LIMIT,
            OrderType.FAK: runtime["OrderType"].FAK,
            OrderType.FOK: runtime["OrderType"].FOK,
        }
        return runtime["OrderRequest"](
            symbol=request.symbol,
            exchange=self._exchange(request.exchange),
            direction=direction,
            type=type_map[request.order_type],
            volume=request.volume,
            price=request.price,
            offset=offset_map[request.offset],
            reference=request.reference,
        )

    def _exchange(self, exchange: str):
        runtime = self._load_runtime()
        try:
            return getattr(runtime["Exchange"], exchange)
        except AttributeError as exc:
            raise ValueError(f"unsupported exchange: {exchange}") from exc

    def _on_tick(self, event) -> None:
        try:
            raw = event.data
            tick = Tick(
                symbol=raw.symbol,
                exchange=raw.exchange.value,
                timestamp=raw.datetime,
                bid_price=float(raw.bid_price_1),
                ask_price=float(raw.ask_price_1),
                last_price=float(raw.last_price),
                bid_volume=float(raw.bid_volume_1),
                ask_volume=float(raw.ask_volume_1),
                trading_day=self.get_trading_day(),
                limit_up=float(getattr(raw, "limit_up", 0.0) or 0.0),
                limit_down=float(getattr(raw, "limit_down", 0.0) or 0.0),
                volume=float(getattr(raw, "volume", 0.0) or 0.0),
                open_interest=float(getattr(raw, "open_interest", 0.0) or 0.0),
            )
            tick.validate()
            self._notify_raw_tick_observer(
                tick,
                self._contract_catalog.get(tick.symbol),
            )
        except Exception as exc:
            self._enqueue_critical(
                BrokerEvent("broker_error", f"CTP tick conversion failed: {exc}")
            )
            return
        self._enqueue_tick(tick)

    def _on_order(self, event) -> None:
        try:
            order = self._convert_order(event.data)
        except Exception as exc:
            self._enqueue_critical(
                BrokerEvent(
                    "broker_error",
                    f"CTP order conversion failed: {exc}",
                )
            )
            return
        self._enqueue_critical(BrokerEvent("order", order))

    def _on_trade(self, event) -> None:
        try:
            raw = event.data
            numeric_volume = float(raw.volume)
            if (
                isinstance(raw.volume, bool)
                or not isfinite(numeric_volume)
                or numeric_volume <= 0
                or not numeric_volume.is_integer()
            ):
                raise ValueError(f"invalid CTP trade volume: {raw.volume!r}")
            price = float(raw.price)
            if not isfinite(price) or price <= 0:
                raise ValueError(f"invalid CTP trade price: {raw.price!r}")
            trade = Trade(
                raw.vt_tradeid,
                raw.vt_orderid,
                raw.symbol,
                raw.exchange.value,
                self._direction_to_side(raw.direction),
                self._offset_to_model(raw.offset),
                int(numeric_volume),
                price,
                raw.datetime or datetime.now(ZoneInfo("Asia/Shanghai")),
            )
            trade.validate()
            trading_day = self.get_trading_day()
        except Exception as exc:
            self._enqueue_critical(
                BrokerEvent(
                    "broker_error",
                    f"CTP trade conversion failed: {exc}",
                )
            )
            return
        trade_key = f"{trading_day}:{trade.exchange}:{trade.trade_id}"
        if trade_key in self._seen_trade_keys:
            return
        self._seen_trade_keys.add(trade_key)
        self._seen_trade_order.append(trade_key)
        if len(self._seen_trade_order) > self._MAX_SEEN_TRADE_KEYS:
            expired = self._seen_trade_order.popleft()
            self._seen_trade_keys.discard(expired)
        with self._position_lock:
            try:
                book = PositionBook(self._copy_positions_unlocked())
                book.apply_trade(trade)
                self._positions = {
                    (position.symbol, position.exchange): position for position in book.all()
                }
            except Exception as exc:
                self._enqueue_critical(
                    BrokerEvent("broker_error", f"CTP trade mirror update failed: {exc}")
                )
            self._enqueue_critical(BrokerEvent("trade", trade))

    def _on_account(self, event) -> None:
        try:
            account = self._convert_account(event.data)
        except Exception as exc:
            self._enqueue_critical(
                BrokerEvent("account_error", f"CTP account conversion failed: {exc}")
            )
            return
        self._last_account = account
        self._account_event_generation += 1
        self._last_account_monotonic = monotonic()
        self._enqueue_critical(BrokerEvent("account", self._last_account))

    def _handle_position_snapshot(self, raw_positions: list[Any]) -> None:
        with self._position_lock:
            combined: dict[tuple[str, str], ContractPosition] = {}
            try:
                for raw in raw_positions:
                    volume = self._exact_integer(raw.volume, "position volume", positive=True)
                    yesterday = self._exact_integer(
                        raw.yd_volume,
                        "yesterday position volume",
                        positive=False,
                    )
                    if yesterday > volume:
                        raise ValueError(f"invalid CTP position volume for {raw.symbol}")
                    today = volume - yesterday
                    symbol = str(raw.symbol).strip()
                    exchange = str(raw.exchange.value).strip()
                    if not symbol or not exchange:
                        raise ValueError("CTP position identity is empty")
                    price = float(raw.price)
                    if not isfinite(price) or price <= 0:
                        raise ValueError(f"invalid CTP position price for {symbol}")
                    key = (symbol, exchange)
                    position = combined.setdefault(key, ContractPosition(symbol, exchange))
                    side = self._direction_to_side(raw.direction)
                    if side is OrderSide.BUY:
                        position.long_today += today
                        position.long_yesterday += yesterday
                        position.long_price = price
                    else:
                        position.short_today += today
                        position.short_yesterday += yesterday
                        position.short_price = price
                for position in combined.values():
                    position.validate()
            except Exception as exc:
                self._enqueue_critical(BrokerEvent("broker_error", str(exc)))
                return
            self._positions = {
                key: position for key, position in combined.items() if not position.empty
            }
            self._position_snapshot_generation += 1
            self._last_position_snapshot_monotonic = monotonic()
            self._enqueue_critical(
                BrokerEvent("position_snapshot", self._copy_positions_unlocked())
            )

    def _convert_account(self, raw) -> AccountSnapshot:
        balance = float(raw.balance)
        available = float(raw.available)
        # VeighNa AccountData 不暴露 CurrMargin，用权益与可用资金差额作为保守代理。
        margin = max(0.0, balance - available)
        account = AccountSnapshot(
            balance, balance, available, margin, 0.0, 0.0, self.get_trading_day()
        )
        account.validate()
        return account

    def _convert_order(self, raw) -> Order:
        volume = self._exact_integer(raw.volume, "order volume", positive=True)
        traded = self._exact_integer(raw.traded, "order traded", positive=False)
        if traded > volume:
            raise ValueError("CTP order traded volume exceeds order volume")
        price = float(raw.price)
        if not isfinite(price) or price <= 0:
            raise ValueError(f"invalid CTP order price: {raw.price!r}")
        reference = getattr(raw, "reference", "") or self._order_references.get(raw.vt_orderid, "")
        request = OrderRequest(
            raw.symbol,
            raw.exchange.value,
            self._direction_to_side(raw.direction),
            self._offset_to_model(raw.offset),
            volume,
            price,
            self._type_to_model(raw.type),
            reference,
        )
        return Order(
            raw.vt_orderid,
            request,
            self._status_to_model(raw.status),
            traded,
            price,
        )

    @staticmethod
    def _exact_integer(value: Any, field: str, *, positive: bool) -> int:
        if isinstance(value, bool):
            raise ValueError(f"invalid CTP {field}: {value!r}")
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid CTP {field}: {value!r}") from exc
        if not isfinite(numeric) or not numeric.is_integer():
            raise ValueError(f"invalid CTP {field}: {value!r}")
        integer = int(numeric)
        if integer < 0 or (positive and integer <= 0):
            raise ValueError(f"invalid CTP {field}: {value!r}")
        return integer

    @staticmethod
    def _enum_name(value: object, field: str) -> str:
        """Return an explicit protocol enum name; never guess an economic default."""
        name = getattr(value, "name", None)
        if not isinstance(name, str) or not name:
            raise ValueError(f"unsupported CTP {field}: {value!r}")
        return name.upper()

    @classmethod
    def _direction_to_side(cls, value: object) -> OrderSide:
        name = cls._enum_name(value, "direction")
        mapping = {
            "LONG": OrderSide.BUY,
            "SHORT": OrderSide.SELL,
        }
        try:
            return mapping[name]
        except KeyError as exc:
            raise ValueError(f"unsupported CTP direction: {name}") from exc

    @classmethod
    def _offset_to_model(cls, value: object) -> Offset:
        name = cls._enum_name(value, "offset")
        mapping = {
            "OPEN": Offset.OPEN,
            "CLOSE": Offset.CLOSE,
            "CLOSETODAY": Offset.CLOSE_TODAY,
            "CLOSEYESTERDAY": Offset.CLOSE_YESTERDAY,
        }
        try:
            return mapping[name]
        except KeyError as exc:
            raise ValueError(f"unsupported CTP offset: {name}") from exc

    @classmethod
    def _type_to_model(cls, value: object) -> OrderType:
        name = cls._enum_name(value, "order type")
        mapping = {
            "LIMIT": OrderType.LIMIT,
            "FAK": OrderType.FAK,
            "FOK": OrderType.FOK,
        }
        try:
            return mapping[name]
        except KeyError as exc:
            raise ValueError(f"unsupported CTP order type: {name}") from exc

    def _status_to_model(self, value: object) -> OrderStatus:
        runtime = self._load_runtime()
        status_map = {
            runtime["Status"].SUBMITTING: OrderStatus.SUBMITTING,
            runtime["Status"].NOTTRADED: OrderStatus.NOT_TRADED,
            runtime["Status"].PARTTRADED: OrderStatus.PART_TRADED,
            runtime["Status"].ALLTRADED: OrderStatus.FILLED,
            runtime["Status"].CANCELLED: OrderStatus.CANCELLED,
            runtime["Status"].REJECTED: OrderStatus.REJECTED,
        }
        try:
            return status_map[value]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"unsupported CTP status: {value!r}") from exc
