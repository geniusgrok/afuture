"""基于 VeighNa ``vnpy_ctp`` 的 CTP 柜台适配器。

个人期货账户通常通过期货公司的 CTP 交易/行情前置接入交易所。
模块在运行实盘命令时才导入二进制依赖，研究和测试环境不需要安装 CTP。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from math import isfinite
from queue import Empty, Queue
from threading import Event
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

    def __init__(
        self, credentials: CtpCredentials, *, snapshot_stale_seconds: float = 20.0
    ) -> None:
        if snapshot_stale_seconds <= 0:
            raise ValueError("snapshot_stale_seconds must be positive")
        self.credentials = credentials
        self.snapshot_stale_seconds = snapshot_stale_seconds
        self._runtime: dict[str, Any] | None = None
        # VeighNa is an optional runtime dependency.  Keep its dynamic objects at
        # the adapter boundary instead of leaking ``Any`` into domain models.
        self._event_engine: Any | None = None
        self._main_engine: Any | None = None
        self._events: Queue[BrokerEvent] = Queue()
        self._order_references: dict[str, str] = {}
        self._last_account: AccountSnapshot | None = None
        self._positions: dict[str, ContractPosition] = {}
        self._trading_day = ""
        self._account_event_generation = 0
        self._position_snapshot_generation = 0
        self._last_account_monotonic = 0.0
        self._last_position_snapshot_monotonic = 0.0
        self._contract_catalog: dict[str, ContractInfo] = {}

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
        return [replace(position) for position in self._positions.values() if not position.empty]

    def poll_events(self) -> list[BrokerEvent]:
        result = []
        while True:
            try:
                result.append(self._events.get_nowait())
            except Empty:
                return result

    def get_trading_day(self) -> str:
        if self._main_engine is not None:
            gateway = self._main_engine.get_gateway(self.gateway_name)
            td_api = getattr(gateway, "td_api", None) if gateway else None
            getter = getattr(td_api, "getTradingDay", None)
            if callable(getter):
                value = getter()
                if isinstance(value, bytes):
                    value = value.decode("ascii", errors="ignore")
                if value:
                    self._trading_day = str(value)
        return self._trading_day or datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")

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
        try:
            tick.validate()
        except ValueError:
            return
        self._events.put(BrokerEvent("tick", tick))

    def _on_order(self, event) -> None:
        try:
            order = self._convert_order(event.data)
        except (AttributeError, TypeError, ValueError) as exc:
            self._events.put(
                BrokerEvent(
                    "broker_error",
                    f"CTP order conversion failed: {exc}",
                )
            )
            return
        self._events.put(BrokerEvent("order", order))

    def _on_trade(self, event) -> None:
        raw = event.data
        try:
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
        except (AttributeError, TypeError, ValueError) as exc:
            self._events.put(
                BrokerEvent(
                    "broker_error",
                    f"CTP trade conversion failed: {exc}",
                )
            )
            return
        try:
            book = PositionBook(self.get_positions())
            book.apply_trade(trade)
            self._positions = {position.symbol: position for position in book.all()}
        except Exception as exc:
            self._events.put(BrokerEvent("broker_error", f"CTP trade mirror update failed: {exc}"))
        self._events.put(BrokerEvent("trade", trade))

    def _on_account(self, event) -> None:
        self._last_account = self._convert_account(event.data)
        self._account_event_generation += 1
        self._last_account_monotonic = monotonic()
        self._events.put(BrokerEvent("account", self._last_account))

    def _handle_position_snapshot(self, raw_positions: list[Any]) -> None:
        combined: dict[str, ContractPosition] = {}
        try:
            for raw in raw_positions:
                volume = int(raw.volume)
                yesterday = int(raw.yd_volume)
                if volume < 0 or yesterday < 0 or yesterday > volume:
                    raise ValueError(f"invalid CTP position volume for {raw.symbol}")
                today = volume - yesterday
                position = combined.setdefault(
                    raw.symbol, ContractPosition(raw.symbol, raw.exchange.value)
                )
                direction_value = getattr(raw.direction, "name", str(raw.direction)).upper()
                if "LONG" in direction_value:
                    position.long_today += today
                    position.long_yesterday += yesterday
                    position.long_price = float(raw.price)
                elif "SHORT" in direction_value:
                    position.short_today += today
                    position.short_yesterday += yesterday
                    position.short_price = float(raw.price)
                else:
                    raise ValueError(f"unsupported CTP position direction: {direction_value}")
        except Exception as exc:
            self._events.put(BrokerEvent("broker_error", str(exc)))
            return
        self._positions = {
            symbol: position for symbol, position in combined.items() if not position.empty
        }
        self._position_snapshot_generation += 1
        self._last_position_snapshot_monotonic = monotonic()
        self._events.put(BrokerEvent("position_snapshot", self.get_positions()))

    def _convert_account(self, raw) -> AccountSnapshot:
        balance = float(raw.balance)
        available = float(raw.available)
        # VeighNa AccountData 不暴露 CurrMargin，用权益与可用资金差额作为保守代理。
        margin = max(0.0, balance - available)
        return AccountSnapshot(
            balance, balance, available, margin, 0.0, 0.0, self.get_trading_day()
        )

    def _convert_order(self, raw) -> Order:
        reference = getattr(raw, "reference", "") or self._order_references.get(raw.vt_orderid, "")
        request = OrderRequest(
            raw.symbol,
            raw.exchange.value,
            self._direction_to_side(raw.direction),
            self._offset_to_model(raw.offset),
            int(raw.volume),
            float(raw.price),
            self._type_to_model(raw.type),
            reference,
        )
        return Order(
            raw.vt_orderid,
            request,
            self._status_to_model(raw.status),
            int(raw.traded),
            float(raw.price),
        )

    @staticmethod
    def _enum_name(value: object, field: str) -> str:
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
