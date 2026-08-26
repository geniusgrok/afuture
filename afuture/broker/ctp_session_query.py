"""Request-bound CTP current-session order/trade completeness evidence."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
from math import isfinite
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from zoneinfo import ZoneInfo

from ..models import Offset, OrderSide, OrderType, Trade
from .ctp_order_journal import CtpOrderSubmissionEntry

_KIND = "afuture.ctp.session-activity-evidence"
_SCHEMA_VERSION = 3
_MAX_ROWS = 20_000
_SHA = re.compile(r"[0-9a-f]{64}")
_EXCHANGE = re.compile(r"[A-Z][A-Z0-9]*")
_ACTIVE_ORDER_STATUSES = frozenset({"1", "3", "b", "c"})
_TERMINAL_ORDER_STATUSES = frozenset({"0", "2", "4", "5"})
_CHINA = ZoneInfo("Asia/Shanghai")


class CtpSessionQueryIntegrityError(ValueError):
    """A query generation or its persisted evidence cannot be trusted."""


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CtpSessionQueryIntegrityError("CTP session evidence is not canonical JSON") from exc


def _digest(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CtpSessionQueryIntegrityError(f"duplicate CTP session evidence JSON key: {key}")
        result[key] = value
    return result


def _day(raw: object, *, name: str = "CTP session trading day") -> str:
    if not isinstance(raw, str):
        raise CtpSessionQueryIntegrityError(f"{name} must be YYYYMMDD")
    try:
        parsed = datetime.strptime(raw, "%Y%m%d").strftime("%Y%m%d")
    except ValueError as exc:
        raise CtpSessionQueryIntegrityError(f"{name} must be YYYYMMDD") from exc
    if parsed != raw:
        raise CtpSessionQueryIntegrityError(f"{name} must be YYYYMMDD")
    return raw


def _nonempty(raw: object, *, name: str) -> str:
    if not isinstance(raw, str) or not raw or raw.strip() != raw:
        raise CtpSessionQueryIntegrityError(f"{name} is invalid")
    return raw


def _positive_int(raw: object, *, name: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise CtpSessionQueryIntegrityError(f"{name} is invalid")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise CtpSessionQueryIntegrityError(f"{name} is invalid") from exc
    if value <= 0 or str(value) != str(raw):
        raise CtpSessionQueryIntegrityError(f"{name} is invalid")
    return value


def _nonnegative_int(raw: object, *, name: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise CtpSessionQueryIntegrityError(f"{name} is invalid")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise CtpSessionQueryIntegrityError(f"{name} is invalid") from exc
    if value < 0 or str(value) != str(raw):
        raise CtpSessionQueryIntegrityError(f"{name} is invalid")
    return value


def _positive_float(raw: object, *, name: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise CtpSessionQueryIntegrityError(f"{name} is invalid")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise CtpSessionQueryIntegrityError(f"{name} is invalid") from exc
    if not isfinite(value) or value <= 0.0:
        raise CtpSessionQueryIntegrityError(f"{name} is invalid")
    return value


def _ctp_side(raw: object) -> OrderSide:
    try:
        return {"0": OrderSide.BUY, "1": OrderSide.SELL}[str(raw)]
    except KeyError as exc:
        raise CtpSessionQueryIntegrityError("CTP session direction is invalid") from exc


def _ctp_offset(raw: object) -> Offset:
    value = str(raw)
    if len(value) != 1:
        raise CtpSessionQueryIntegrityError("CTP session offset is invalid")
    try:
        return {
            "0": Offset.OPEN,
            "1": Offset.CLOSE,
            "3": Offset.CLOSE_TODAY,
            "4": Offset.CLOSE_YESTERDAY,
        }[value]
    except KeyError as exc:
        raise CtpSessionQueryIntegrityError("CTP session offset is invalid") from exc


def _ctp_order_type(raw: Mapping[str, object]) -> OrderType:
    price_type = str(raw.get("OrderPriceType", ""))
    time_condition = str(raw.get("TimeCondition", ""))
    volume_condition = str(raw.get("VolumeCondition", ""))
    if price_type != "2":
        raise CtpSessionQueryIntegrityError("CTP session order price type is unsupported")
    if time_condition == "3" and volume_condition == "1":
        return OrderType.LIMIT
    if time_condition == "1" and volume_condition == "1":
        return OrderType.FAK
    if time_condition == "1" and volume_condition == "3":
        return OrderType.FOK
    raise CtpSessionQueryIntegrityError("CTP session order type is unsupported")


def _request_id(raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise CtpSessionQueryIntegrityError("CTP session request id is invalid")
    return raw


def _identity(
    raw: Mapping[str, object],
    *,
    broker_id: str,
    investor_id: str,
    invest_unit_id: str,
    trading_day: str,
) -> tuple[str, str, str, str]:
    expected = (
        _nonempty(broker_id, name="expected BrokerID"),
        _nonempty(investor_id, name="expected InvestorID"),
        str(invest_unit_id),
        _day(trading_day),
    )
    actual = (
        _nonempty(raw.get("BrokerID"), name="CTP session BrokerID"),
        _nonempty(raw.get("InvestorID"), name="CTP session investor identity"),
        str(raw.get("InvestUnitID", "") or ""),
        _day(raw.get("TradingDay")),
    )
    if actual[0] != expected[0]:
        raise CtpSessionQueryIntegrityError("CTP session BrokerID mismatch")
    if actual[1] != expected[1]:
        raise CtpSessionQueryIntegrityError("CTP session investor identity mismatch")
    if actual[2] != expected[2]:
        raise CtpSessionQueryIntegrityError("CTP session invest-unit identity mismatch")
    if actual[3] != expected[3]:
        raise CtpSessionQueryIntegrityError("CTP session trading day mismatch")
    return actual


@dataclass(frozen=True)
class CtpSessionOrder:
    broker_id: str
    investor_id: str
    invest_unit_id: str
    trading_day: str
    instrument_id: str
    exchange_id: str
    order_sys_id: str
    front_id: int
    session_id: int
    order_ref: int
    order_status: str
    side: OrderSide
    offset: Offset
    volume: int
    volume_traded: int
    volume_total: int
    price: float
    order_type: OrderType

    @property
    def order_id(self) -> str:
        return f"CTP.{self.front_id}_{self.session_id}_{self.order_ref}"

    @property
    def identity(self) -> str:
        if self.order_sys_id:
            return f"{self.exchange_id}:sys:{self.order_sys_id}"
        return f"local:{self.front_id}:{self.session_id}:{self.order_ref}"

    @property
    def active(self) -> bool:
        return self.order_status in _ACTIVE_ORDER_STATUSES


@dataclass(frozen=True)
class CtpSessionTrade:
    broker_id: str
    investor_id: str
    invest_unit_id: str
    trading_day: str
    instrument_id: str
    exchange_id: str
    trade_id: str
    order_sys_id: str
    order_ref: int
    side: OrderSide
    offset: Offset
    volume: int
    price: float
    timestamp: datetime

    @property
    def identity(self) -> str:
        return f"{self.exchange_id}:{self.trade_id}"

    @property
    def domain_trade_id(self) -> str:
        return f"CTP.{self.trade_id}"

    @property
    def fill_key(self) -> str:
        return f"{self.trading_day}:{self.exchange_id}:{self.domain_trade_id}"

    def to_domain_trade(self, order_id: str) -> Trade:
        trade = Trade(
            self.domain_trade_id,
            order_id,
            self.instrument_id,
            self.exchange_id,
            self.side,
            self.offset,
            self.volume,
            self.price,
            self.timestamp,
        )
        trade.validate()
        return trade


def normalize_ctp_session_order(
    raw: Mapping[str, object],
    *,
    broker_id: str,
    investor_id: str,
    invest_unit_id: str,
    trading_day: str,
) -> CtpSessionOrder:
    if not isinstance(raw, Mapping):
        raise CtpSessionQueryIntegrityError("CTP session order row is invalid")
    identity = _identity(
        raw,
        broker_id=broker_id,
        investor_id=investor_id,
        invest_unit_id=invest_unit_id,
        trading_day=trading_day,
    )
    exchange = _nonempty(raw.get("ExchangeID"), name="CTP session order exchange").upper()
    if _EXCHANGE.fullmatch(exchange) is None:
        raise CtpSessionQueryIntegrityError("CTP session order exchange is invalid")
    order_sys_id = str(raw.get("OrderSysID", "") or "").strip()
    front_id = _positive_int(raw.get("FrontID"), name="CTP session order FrontID")
    session_id = _positive_int(raw.get("SessionID"), name="CTP session order SessionID")
    order_ref = _positive_int(raw.get("OrderRef"), name="CTP session order OrderRef")
    status = _nonempty(str(raw.get("OrderStatus", "")), name="CTP session order status")
    if status not in _ACTIVE_ORDER_STATUSES | _TERMINAL_ORDER_STATUSES:
        raise CtpSessionQueryIntegrityError("CTP session order status is unknown")
    volume = _positive_int(
        raw.get("VolumeTotalOriginal"),
        name="CTP session order volume",
    )
    volume_traded = _nonnegative_int(
        raw.get("VolumeTraded"),
        name="CTP session order traded volume",
    )
    volume_total = _nonnegative_int(
        raw.get("VolumeTotal"),
        name="CTP session order remaining volume",
    )
    if volume_traded + volume_total != volume:
        raise CtpSessionQueryIntegrityError("CTP session order volume accounting is invalid")
    if status == "0" and (volume_traded != volume or volume_total != 0):
        raise CtpSessionQueryIntegrityError("CTP all-traded order volume is invalid")
    if status in {"1", "2"} and not (0 < volume_traded < volume):
        raise CtpSessionQueryIntegrityError("CTP part-traded order volume is invalid")
    if status in {"3", "4", "b", "c"} and volume_traded != 0:
        raise CtpSessionQueryIntegrityError("CTP no-trade order volume is invalid")
    if status == "5" and volume_traded >= volume:
        raise CtpSessionQueryIntegrityError("CTP cancelled order volume is invalid")
    return CtpSessionOrder(
        broker_id=identity[0],
        investor_id=identity[1],
        invest_unit_id=identity[2],
        trading_day=identity[3],
        instrument_id=_nonempty(raw.get("InstrumentID"), name="CTP session order instrument"),
        exchange_id=exchange,
        order_sys_id=order_sys_id,
        front_id=front_id,
        session_id=session_id,
        order_ref=order_ref,
        order_status=status,
        side=_ctp_side(raw.get("Direction")),
        offset=_ctp_offset(raw.get("CombOffsetFlag")),
        volume=volume,
        volume_traded=volume_traded,
        volume_total=volume_total,
        price=_positive_float(raw.get("LimitPrice"), name="CTP session order price"),
        order_type=_ctp_order_type(raw),
    )


def normalize_ctp_session_trade(
    raw: Mapping[str, object],
    *,
    broker_id: str,
    investor_id: str,
    invest_unit_id: str,
    trading_day: str,
) -> CtpSessionTrade:
    if not isinstance(raw, Mapping):
        raise CtpSessionQueryIntegrityError("CTP session trade row is invalid")
    identity = _identity(
        raw,
        broker_id=broker_id,
        investor_id=investor_id,
        invest_unit_id=invest_unit_id,
        trading_day=trading_day,
    )
    exchange = _nonempty(raw.get("ExchangeID"), name="CTP session trade exchange").upper()
    if _EXCHANGE.fullmatch(exchange) is None:
        raise CtpSessionQueryIntegrityError("CTP session trade exchange is invalid")
    order_sys_id = _nonempty(
        str(raw.get("OrderSysID", "") or "").strip(),
        name="CTP session trade OrderSysID",
    )
    trade_date = _day(raw.get("TradeDate"), name="CTP session trade date")
    trade_time = _nonempty(raw.get("TradeTime"), name="CTP session trade time")
    try:
        timestamp = datetime.strptime(
            f"{trade_date} {trade_time}",
            "%Y%m%d %H:%M:%S",
        ).replace(tzinfo=_CHINA)
    except ValueError as exc:
        raise CtpSessionQueryIntegrityError("CTP session trade time is invalid") from exc
    return CtpSessionTrade(
        broker_id=identity[0],
        investor_id=identity[1],
        invest_unit_id=identity[2],
        trading_day=identity[3],
        instrument_id=_nonempty(raw.get("InstrumentID"), name="CTP session trade instrument"),
        exchange_id=exchange,
        trade_id=_nonempty(raw.get("TradeID"), name="CTP session TradeID"),
        order_sys_id=order_sys_id,
        order_ref=_positive_int(raw.get("OrderRef"), name="CTP session trade OrderRef"),
        side=_ctp_side(raw.get("Direction")),
        offset=_ctp_offset(raw.get("OffsetFlag")),
        volume=_positive_int(raw.get("Volume"), name="CTP session trade volume"),
        price=_positive_float(raw.get("Price"), name="CTP session trade price"),
        timestamp=timestamp,
    )


class CtpSessionQueryAccumulator:
    """Accept exactly one registered order or trade query generation."""

    def __init__(self, kind: str, *, max_rows: int = _MAX_ROWS) -> None:
        if kind not in {"order", "trade"}:
            raise ValueError("CTP session query kind must be order or trade")
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows <= 0:
            raise ValueError("CTP session query max rows is invalid")
        self.kind = kind
        self.max_rows = max_rows
        self._lock = Lock()
        self._active_request_id: int | None = None
        self._identity: dict[str, str] = {}
        self._rows: dict[str, CtpSessionOrder | CtpSessionTrade] = {}

    def begin(
        self,
        request_id: int,
        *,
        broker_id: str,
        investor_id: str,
        invest_unit_id: str,
        trading_day: str,
    ) -> None:
        request = _request_id(request_id)
        identity = {
            "broker_id": _nonempty(broker_id, name="expected BrokerID"),
            "investor_id": _nonempty(investor_id, name="expected InvestorID"),
            "invest_unit_id": str(invest_unit_id),
            "trading_day": _day(trading_day),
        }
        with self._lock:
            if self._active_request_id is not None:
                raise CtpSessionQueryIntegrityError("CTP session query is already active")
            self._active_request_id = request
            self._identity = identity
            self._rows = {}

    def _fail_unlocked(self, detail: str) -> CtpSessionQueryIntegrityError:
        self._active_request_id = None
        self._identity = {}
        self._rows = {}
        return CtpSessionQueryIntegrityError(detail)

    def abort(self, request_id: int, reason: str) -> None:
        request = _request_id(request_id)
        with self._lock:
            if request != self._active_request_id:
                raise CtpSessionQueryIntegrityError("CTP session query request id mismatch")
            raise self._fail_unlocked(str(reason).strip() or "CTP session query aborted")

    def observe(
        self,
        request_id: int,
        data: Mapping[str, object] | None,
        error: Mapping[str, object] | None,
        *,
        last: bool,
    ) -> tuple[CtpSessionOrder | CtpSessionTrade, ...] | None:
        request = _request_id(request_id)
        with self._lock:
            if self._active_request_id is None:
                raise CtpSessionQueryIntegrityError("CTP session query has no active request")
            if request != self._active_request_id:
                raise self._fail_unlocked("CTP session query request id mismatch")
            try:
                error_id = _nonnegative_int(
                    (error or {}).get("ErrorID", 0),
                    name="CTP session query ErrorID",
                )
            except CtpSessionQueryIntegrityError as exc:
                raise self._fail_unlocked("CTP session query error payload is invalid") from exc
            if error_id:
                message = str((error or {}).get("ErrorMsg", "")).strip()
                raise self._fail_unlocked(
                    f"CTP session query failed: ErrorID={error_id} message={message}"
                )
            if data is not None:
                try:
                    normalizer = (
                        normalize_ctp_session_order
                        if self.kind == "order"
                        else normalize_ctp_session_trade
                    )
                    row = normalizer(data, **self._identity)
                except CtpSessionQueryIntegrityError as exc:
                    raise self._fail_unlocked(str(exc)) from exc
                key = row.identity
                if key in self._rows:
                    raise self._fail_unlocked(f"CTP session {self.kind} query contains duplicate")
                if len(self._rows) >= self.max_rows:
                    raise self._fail_unlocked("CTP session query exceeds memory bound")
                self._rows[key] = row
            if not last:
                return None
            rows = tuple(self._rows[key] for key in sorted(self._rows))
            self._active_request_id = None
            self._identity = {}
            self._rows = {}
            return rows


@dataclass(frozen=True)
class CtpSessionActivityEvidence:
    account_identity_digest: str
    trading_day: str
    order_request_id: int
    trade_request_id: int
    orders: tuple[CtpSessionOrder, ...]
    trades: tuple[CtpSessionTrade, ...]
    critical_generation: int
    query_ingress_generation: int
    orders_digest: str
    trades_digest: str
    evidence_digest: str


@dataclass(frozen=True)
class CtpSessionJournalRecoveryPlan:
    status_updates: tuple[tuple[str, str], ...]
    fill_trades: tuple[tuple[str, tuple[Trade, ...]], ...]
    session_trades: tuple[Trade, ...]
    active_order_ids: tuple[str, ...]


def _order_payload(row: CtpSessionOrder) -> dict[str, object]:
    return {
        **asdict(row),
        "side": row.side.value,
        "offset": row.offset.value,
        "order_type": row.order_type.value,
    }


def _trade_payload(row: CtpSessionTrade) -> dict[str, object]:
    return {
        **asdict(row),
        "side": row.side.value,
        "offset": row.offset.value,
        "timestamp": row.timestamp.isoformat(timespec="microseconds"),
    }


def _validated_order(row: object, *, trading_day: str) -> CtpSessionOrder:
    if not isinstance(row, CtpSessionOrder):
        raise CtpSessionQueryIntegrityError("CTP session order evidence is invalid")
    direction = "0" if row.side is OrderSide.BUY else "1"
    offset = {
        Offset.OPEN: "0",
        Offset.CLOSE: "1",
        Offset.CLOSE_TODAY: "3",
        Offset.CLOSE_YESTERDAY: "4",
    }.get(row.offset)
    order_conditions = {
        OrderType.LIMIT: ("3", "1"),
        OrderType.FAK: ("1", "1"),
        OrderType.FOK: ("1", "3"),
    }.get(row.order_type)
    if offset is None or order_conditions is None:
        raise CtpSessionQueryIntegrityError("CTP session order economics are invalid")
    normalized = normalize_ctp_session_order(
        {
            "BrokerID": row.broker_id,
            "InvestorID": row.investor_id,
            "InvestUnitID": row.invest_unit_id,
            "TradingDay": row.trading_day,
            "InstrumentID": row.instrument_id,
            "ExchangeID": row.exchange_id,
            "OrderSysID": row.order_sys_id,
            "FrontID": row.front_id,
            "SessionID": row.session_id,
            "OrderRef": row.order_ref,
            "OrderStatus": row.order_status,
            "Direction": direction,
            "CombOffsetFlag": offset,
            "VolumeTotalOriginal": row.volume,
            "VolumeTraded": row.volume_traded,
            "VolumeTotal": row.volume_total,
            "LimitPrice": row.price,
            "OrderPriceType": "2",
            "TimeCondition": order_conditions[0],
            "VolumeCondition": order_conditions[1],
        },
        broker_id=row.broker_id,
        investor_id=row.investor_id,
        invest_unit_id=row.invest_unit_id,
        trading_day=trading_day,
    )
    if normalized != row:
        raise CtpSessionQueryIntegrityError("CTP session order evidence is not canonical")
    return normalized


def _validated_trade(row: object, *, trading_day: str) -> CtpSessionTrade:
    if not isinstance(row, CtpSessionTrade):
        raise CtpSessionQueryIntegrityError("CTP session trade evidence is invalid")
    if row.timestamp.tzinfo is None or row.timestamp.utcoffset() is None:
        raise CtpSessionQueryIntegrityError("CTP session trade timestamp is invalid")
    local_timestamp = row.timestamp.astimezone(_CHINA)
    direction = "0" if row.side is OrderSide.BUY else "1"
    offset = {
        Offset.OPEN: "0",
        Offset.CLOSE: "1",
        Offset.CLOSE_TODAY: "3",
        Offset.CLOSE_YESTERDAY: "4",
    }.get(row.offset)
    if offset is None:
        raise CtpSessionQueryIntegrityError("CTP session trade economics are invalid")
    normalized = normalize_ctp_session_trade(
        {
            "BrokerID": row.broker_id,
            "InvestorID": row.investor_id,
            "InvestUnitID": row.invest_unit_id,
            "TradingDay": row.trading_day,
            "InstrumentID": row.instrument_id,
            "ExchangeID": row.exchange_id,
            "TradeID": row.trade_id,
            "OrderSysID": row.order_sys_id,
            "OrderRef": row.order_ref,
            "Direction": direction,
            "OffsetFlag": offset,
            "Volume": row.volume,
            "Price": row.price,
            "TradeDate": local_timestamp.strftime("%Y%m%d"),
            "TradeTime": local_timestamp.strftime("%H:%M:%S"),
        },
        broker_id=row.broker_id,
        investor_id=row.investor_id,
        invest_unit_id=row.invest_unit_id,
        trading_day=trading_day,
    )
    if normalized != row:
        raise CtpSessionQueryIntegrityError("CTP session trade evidence is not canonical")
    return normalized


def build_ctp_session_activity_evidence(
    *,
    account_identity_digest: str,
    trading_day: str,
    order_request_id: int,
    trade_request_id: int,
    orders: tuple[CtpSessionOrder, ...],
    trades: tuple[CtpSessionTrade, ...],
    critical_generation: int,
    query_ingress_generation: int = 0,
) -> CtpSessionActivityEvidence:
    if (
        not isinstance(account_identity_digest, str)
        or _SHA.fullmatch(account_identity_digest) is None
    ):
        raise CtpSessionQueryIntegrityError("CTP session account digest is invalid")
    day = _day(trading_day)
    order_request = _request_id(order_request_id)
    trade_request = _request_id(trade_request_id)
    if (
        isinstance(critical_generation, bool)
        or not isinstance(critical_generation, int)
        or critical_generation < 0
    ):
        raise CtpSessionQueryIntegrityError("CTP session critical generation is invalid")
    if (
        isinstance(query_ingress_generation, bool)
        or not isinstance(query_ingress_generation, int)
        or query_ingress_generation < 0
    ):
        raise CtpSessionQueryIntegrityError("CTP session query ingress generation is invalid")
    if not isinstance(orders, tuple) or not isinstance(trades, tuple):
        raise CtpSessionQueryIntegrityError("CTP session evidence rows must be tuples")
    if len(orders) > _MAX_ROWS or len(trades) > _MAX_ROWS:
        raise CtpSessionQueryIntegrityError("CTP session evidence exceeds row bound")
    canonical_orders: tuple[CtpSessionOrder, ...] = tuple(
        sorted(
            (_validated_order(row, trading_day=day) for row in orders),
            key=lambda row: row.identity,
        )
    )
    canonical_trades: tuple[CtpSessionTrade, ...] = tuple(
        sorted(
            (_validated_trade(row, trading_day=day) for row in trades),
            key=lambda row: row.identity,
        )
    )
    if len({row.identity for row in canonical_orders}) != len(canonical_orders):
        raise CtpSessionQueryIntegrityError("CTP session order evidence contains duplicate rows")
    if len({row.identity for row in canonical_trades}) != len(canonical_trades):
        raise CtpSessionQueryIntegrityError("CTP session trade evidence contains duplicate rows")
    row_identities = {
        (row.broker_id, row.investor_id, row.invest_unit_id) for row in canonical_orders
    } | {(row.broker_id, row.investor_id, row.invest_unit_id) for row in canonical_trades}
    if len(row_identities) > 1:
        raise CtpSessionQueryIntegrityError("CTP session row account identities mismatch")
    order_payload = [_order_payload(row) for row in canonical_orders]
    trade_payload = [_trade_payload(row) for row in canonical_trades]
    orders_digest = _digest(order_payload)
    trades_digest = _digest(trade_payload)
    unsigned = {
        "account_identity_digest": account_identity_digest,
        "trading_day": day,
        "order_request_id": order_request,
        "trade_request_id": trade_request,
        "orders": order_payload,
        "trades": trade_payload,
        "critical_generation": critical_generation,
        "query_ingress_generation": query_ingress_generation,
        "orders_digest": orders_digest,
        "trades_digest": trades_digest,
    }
    return CtpSessionActivityEvidence(
        account_identity_digest,
        day,
        order_request,
        trade_request,
        canonical_orders,
        canonical_trades,
        critical_generation,
        query_ingress_generation,
        orders_digest,
        trades_digest,
        _digest(unsigned),
    )


def validate_ctp_session_activity_ownership(
    evidence: CtpSessionActivityEvidence,
    journal_entries: tuple[CtpOrderSubmissionEntry, ...],
) -> str:
    """Join complete query truth to durable order ids and exact fill identities."""

    entries: dict[str, CtpOrderSubmissionEntry] = {}
    for entry in journal_entries:
        order_id = getattr(entry, "order_id", None)
        if not isinstance(order_id, str) or not order_id or order_id in entries:
            raise CtpSessionQueryIntegrityError("durable CTP journal order identity is invalid")
        if getattr(entry, "account_identity_digest", None) != evidence.account_identity_digest:
            raise CtpSessionQueryIntegrityError("CTP session journal account identity mismatch")
        entries[order_id] = entry

    joined_orders: dict[tuple[str, str], CtpOrderSubmissionEntry] = {}
    observed_order_ids: set[str] = set()
    ownership_rows: list[dict[str, str]] = []
    for order in evidence.orders:
        order_entry = entries.get(order.order_id)
        if order_entry is None:
            raise CtpSessionQueryIntegrityError(f"unknown CTP session order: {order.order_id}")
        request = getattr(order_entry, "request", None)
        if (
            getattr(order_entry, "front_id", None) != order.front_id
            or getattr(order_entry, "session_id", None) != order.session_id
            or getattr(order_entry, "order_ref", None) != order.order_ref
            or getattr(order_entry, "target_trading_day", None) != evidence.trading_day
            or getattr(request, "symbol", None) != order.instrument_id
            or str(getattr(request, "exchange", "")).upper() != order.exchange_id
            or getattr(request, "side", None) is not order.side
            or getattr(request, "offset", None) is not order.offset
            or getattr(request, "volume", None) != order.volume
            or getattr(request, "price", None) != order.price
            or getattr(request, "order_type", None) is not order.order_type
        ):
            raise CtpSessionQueryIntegrityError(
                f"CTP session order/journal identity mismatch: {order.order_id}"
            )
        if getattr(order_entry, "status", None) == "aborted_before_send":
            raise CtpSessionQueryIntegrityError(
                f"aborted CTP journal order appeared in complete query: {order.order_id}"
            )
        if order.active:
            raise CtpSessionQueryIntegrityError(
                f"active CTP session order blocks lifecycle operation: {order.order_id}"
            )
        if getattr(order_entry, "status", None) != "terminal":
            raise CtpSessionQueryIntegrityError(
                f"CTP session order/journal status mismatch: {order.order_id}"
            )
        observed_order_ids.add(order.order_id)
        if order.order_sys_id:
            sys_identity = (order.exchange_id, order.order_sys_id)
            if sys_identity in joined_orders:
                raise CtpSessionQueryIntegrityError("duplicate CTP OrderSysID identity")
            joined_orders[sys_identity] = order_entry
        ownership_rows.append(
            {
                "kind": "order",
                "query_identity": order.identity,
                "order_id": order.order_id,
            }
        )

    relevant_entries: list[CtpOrderSubmissionEntry] = []
    for entry in entries.values():
        if (
            getattr(entry, "target_trading_day", None) == evidence.trading_day
            and getattr(entry, "status", None) != "aborted_before_send"
        ):
            relevant_entries.append(entry)
            if getattr(entry, "order_id", None) not in observed_order_ids:
                raise CtpSessionQueryIntegrityError(
                    f"durable current-day CTP order is absent from complete query: "
                    f"{getattr(entry, 'order_id', '')}"
                )

    observed_fill_keys: set[str] = set()
    queried_volume: dict[str, int] = {}
    for trade in evidence.trades:
        trade_entry = joined_orders.get((trade.exchange_id, trade.order_sys_id))
        if trade_entry is None:
            raise CtpSessionQueryIntegrityError(
                f"CTP session trade OrderSysID is unknown: {trade.order_sys_id}"
            )
        request = getattr(trade_entry, "request", None)
        if (
            getattr(trade_entry, "order_ref", None) != trade.order_ref
            or getattr(request, "symbol", None) != trade.instrument_id
            or str(getattr(request, "exchange", "")).upper() != trade.exchange_id
            or getattr(request, "side", None) is not trade.side
            or getattr(request, "offset", None) is not trade.offset
            or trade.fill_key not in tuple(getattr(trade_entry, "fill_keys", ()))
        ):
            raise CtpSessionQueryIntegrityError(
                f"CTP session trade/journal identity mismatch: {trade.identity}"
            )
        limit = float(getattr(request, "price", 0.0))
        if (trade.side is OrderSide.BUY and trade.price > limit) or (
            trade.side is OrderSide.SELL and trade.price < limit
        ):
            raise CtpSessionQueryIntegrityError(
                f"CTP session trade exceeds durable limit: {trade.identity}"
            )
        observed_fill_keys.add(trade.fill_key)
        order_id = str(trade_entry.order_id)
        queried_volume[order_id] = queried_volume.get(order_id, 0) + trade.volume
        ownership_rows.append(
            {
                "kind": "trade",
                "query_identity": trade.identity,
                "order_id": str(trade_entry.order_id),
            }
        )
    for order in evidence.orders:
        if queried_volume.get(order.order_id, 0) != order.volume_traded:
            raise CtpSessionQueryIntegrityError(
                f"CTP session query trade volume mismatch: {order.order_id}"
            )
    expected_fill_keys = {
        fill_key
        for entry in relevant_entries
        for fill_key in tuple(getattr(entry, "fill_keys", ()))
    }
    missing_fills = sorted(expected_fill_keys - observed_fill_keys)
    if missing_fills:
        raise CtpSessionQueryIntegrityError(
            f"durable CTP fill is absent from complete query: {missing_fills[0]}"
        )
    return _digest(sorted(ownership_rows, key=lambda item: tuple(item.values())))


def plan_ctp_session_journal_recovery(
    evidence: CtpSessionActivityEvidence,
    journal_entries: tuple[CtpOrderSubmissionEntry, ...],
    *,
    current_order_ids: frozenset[str] | None = None,
) -> CtpSessionJournalRecoveryPlan:
    """Derive an exact crash roll-forward from complete CTP truth; never guess rows."""

    from .ctp_order_journal import ctp_order_fill_evidence

    entries: dict[str, CtpOrderSubmissionEntry] = {}
    for entry in journal_entries:
        order_id = getattr(entry, "order_id", None)
        if (
            not isinstance(order_id, str)
            or not order_id
            or order_id in entries
            or getattr(entry, "account_identity_digest", None) != evidence.account_identity_digest
            or getattr(entry, "target_trading_day", None) != evidence.trading_day
        ):
            raise CtpSessionQueryIntegrityError("durable CTP recovery journal identity is invalid")
        entries[order_id] = entry
    mutable_order_ids = frozenset(entries) if current_order_ids is None else current_order_ids
    if not isinstance(mutable_order_ids, frozenset) or not mutable_order_ids.issubset(entries):
        raise CtpSessionQueryIntegrityError("current CTP recovery epoch identity is invalid")
    if any(
        getattr(entry, "status", None) != "terminal"
        for order_id, entry in entries.items()
        if order_id not in mutable_order_ids
    ):
        raise CtpSessionQueryIntegrityError("sealed CTP recovery entry is not terminal")

    observed_order_ids: set[str] = set()
    sys_entries: dict[tuple[str, str], CtpOrderSubmissionEntry] = {}
    status_updates: dict[str, str] = {}
    active_order_ids: list[str] = []
    query_orders: dict[str, CtpSessionOrder] = {}
    for order in evidence.orders:
        order_entry = entries.get(order.order_id)
        if order_entry is None:
            raise CtpSessionQueryIntegrityError(f"unknown CTP session order: {order.order_id}")
        request = getattr(order_entry, "request", None)
        if (
            getattr(order_entry, "front_id", None) != order.front_id
            or getattr(order_entry, "session_id", None) != order.session_id
            or getattr(order_entry, "order_ref", None) != order.order_ref
            or getattr(request, "symbol", None) != order.instrument_id
            or str(getattr(request, "exchange", "")).upper() != order.exchange_id
            or getattr(request, "side", None) is not order.side
            or getattr(request, "offset", None) is not order.offset
            or getattr(request, "volume", None) != order.volume
            or getattr(request, "price", None) != order.price
            or getattr(request, "order_type", None) is not order.order_type
        ):
            raise CtpSessionQueryIntegrityError(
                f"CTP recovery order/journal economics mismatch: {order.order_id}"
            )
        current_status = getattr(order_entry, "status", None)
        if current_status == "aborted_before_send":
            raise CtpSessionQueryIntegrityError(
                f"aborted CTP order appeared in complete query: {order.order_id}"
            )
        if order.active:
            if current_status == "terminal":
                raise CtpSessionQueryIntegrityError(
                    f"terminal CTP journal order became active: {order.order_id}"
                )
            status_updates[order.order_id] = "submitted"
            active_order_ids.append(order.order_id)
        elif order.order_id in mutable_order_ids:
            status_updates[order.order_id] = "terminal"
        observed_order_ids.add(order.order_id)
        query_orders[order.order_id] = order
        if order.order_sys_id:
            sys_key = (order.exchange_id, order.order_sys_id)
            if sys_key in sys_entries:
                raise CtpSessionQueryIntegrityError("duplicate CTP recovery OrderSysID")
            sys_entries[sys_key] = order_entry

    for entry in entries.values():
        order_id = str(entry.order_id)
        status = getattr(entry, "status", None)
        if order_id in observed_order_ids or status == "aborted_before_send":
            continue
        if status == "prepared":
            status_updates[order_id] = "aborted_before_send"
            continue
        raise CtpSessionQueryIntegrityError(
            f"durable submitted CTP order is absent from complete query: {order_id}"
        )

    observed_fill_keys: set[str] = set()
    new_fills: dict[str, list[Trade]] = {}
    session_trades: list[Trade] = []
    queried_volume: dict[str, int] = {}
    for row in evidence.trades:
        trade_entry = sys_entries.get((row.exchange_id, row.order_sys_id))
        if trade_entry is None:
            raise CtpSessionQueryIntegrityError(
                f"CTP recovery trade OrderSysID is unknown: {row.order_sys_id}"
            )
        order_id = str(trade_entry.order_id)
        request = getattr(trade_entry, "request", None)
        if (
            getattr(trade_entry, "order_ref", None) != row.order_ref
            or getattr(request, "symbol", None) != row.instrument_id
            or str(getattr(request, "exchange", "")).upper() != row.exchange_id
            or getattr(request, "side", None) is not row.side
            or getattr(request, "offset", None) is not row.offset
        ):
            raise CtpSessionQueryIntegrityError(
                f"CTP recovery trade/journal economics mismatch: {row.identity}"
            )
        limit = float(getattr(request, "price", 0.0))
        if (row.side is OrderSide.BUY and row.price > limit) or (
            row.side is OrderSide.SELL and row.price < limit
        ):
            raise CtpSessionQueryIntegrityError(
                f"CTP recovery trade exceeds durable limit: {row.identity}"
            )
        trade = row.to_domain_trade(order_id)
        canonical = ctp_order_fill_evidence(evidence.trading_day, trade)
        persisted = {item.key: item for item in tuple(getattr(trade_entry, "fill_evidence", ()))}
        if canonical.key in persisted:
            if persisted[canonical.key] != canonical:
                raise CtpSessionQueryIntegrityError(
                    f"CTP recovery fill fingerprint mismatch: {row.identity}"
                )
        elif order_id in mutable_order_ids:
            new_fills.setdefault(order_id, []).append(trade)
        else:
            raise CtpSessionQueryIntegrityError(
                f"sealed CTP recovery fill is not durable: {row.identity}"
            )
        if order_id in mutable_order_ids:
            session_trades.append(trade)
        observed_fill_keys.add(canonical.key)
        queried_volume[order_id] = queried_volume.get(order_id, 0) + row.volume

    durable_fill_keys = {
        key for entry in entries.values() for key in tuple(getattr(entry, "fill_keys", ()))
    }
    missing = sorted(durable_fill_keys - observed_fill_keys)
    if missing:
        raise CtpSessionQueryIntegrityError(
            f"durable CTP fill is absent from complete recovery query: {missing[0]}"
        )
    for order_id, order in query_orders.items():
        volume = queried_volume.get(order_id, 0)
        request_volume = int(getattr(getattr(entries[order_id], "request", None), "volume", 0))
        if volume != order.volume_traded:
            raise CtpSessionQueryIntegrityError(
                f"CTP recovery query trade volume mismatch: {order_id}"
            )
        if volume > request_volume:
            raise CtpSessionQueryIntegrityError(
                f"CTP recovery fill volume exceeds durable request: {order_id}"
            )
    return CtpSessionJournalRecoveryPlan(
        status_updates=tuple(sorted(status_updates.items())),
        fill_trades=tuple(
            (order_id, tuple(sorted(trades, key=lambda item: item.trade_id)))
            for order_id, trades in sorted(new_fills.items())
        ),
        session_trades=tuple(
            sorted(
                session_trades,
                key=lambda item: (item.timestamp, item.exchange, item.trade_id),
            )
        ),
        active_order_ids=tuple(sorted(active_order_ids)),
    )


@dataclass(frozen=True)
class CtpSessionActivityEvidenceRecord:
    evidence: CtpSessionActivityEvidence
    sequence: int
    parent_checksum: str | None
    checksum: str


def _evidence_payload(evidence: CtpSessionActivityEvidence) -> dict[str, object]:
    return {
        "account_identity_digest": evidence.account_identity_digest,
        "trading_day": evidence.trading_day,
        "order_request_id": evidence.order_request_id,
        "trade_request_id": evidence.trade_request_id,
        "orders": [_order_payload(row) for row in evidence.orders],
        "trades": [_trade_payload(row) for row in evidence.trades],
        "critical_generation": evidence.critical_generation,
        "query_ingress_generation": evidence.query_ingress_generation,
        "orders_digest": evidence.orders_digest,
        "trades_digest": evidence.trades_digest,
        "evidence_digest": evidence.evidence_digest,
    }


class CtpSessionActivityEvidenceStore:
    """Atomic sequence/checksum audit evidence; current is always authoritative."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @property
    def previous_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.prev")

    def save(self, evidence: CtpSessionActivityEvidence) -> CtpSessionActivityEvidenceRecord:
        current = self.load_required_record() if self.path.exists() else None
        sequence = 1 if current is None else current.sequence + 1
        parent = None if current is None else current.checksum
        unsigned = {
            "kind": _KIND,
            "schema_version": _SCHEMA_VERSION,
            "sequence": sequence,
            "parent_checksum": parent,
            "evidence": _evidence_payload(evidence),
        }
        checksum = _digest(unsigned)
        payload = _canonical({**unsigned, "checksum": checksum})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if current is not None:
            self._replace(self.previous_path, self.path.read_bytes())
        self._replace(self.path, payload)
        return CtpSessionActivityEvidenceRecord(evidence, sequence, parent, checksum)

    def load_required_record(self) -> CtpSessionActivityEvidenceRecord:
        if not self.path.exists():
            raise CtpSessionQueryIntegrityError("required CTP session evidence is missing")
        current_payload = self.path.read_bytes()
        current = self._decode(current_payload)
        if current.sequence == 1:
            if self.previous_path.exists():
                raise CtpSessionQueryIntegrityError(
                    "unexpected previous CTP session evidence for initial sequence"
                )
            return current
        if not self.previous_path.exists():
            raise CtpSessionQueryIntegrityError("previous CTP session evidence is missing")
        previous_payload = self.previous_path.read_bytes()
        previous = self._decode(previous_payload)
        if current_payload == previous_payload:
            return current
        if (
            current.sequence != previous.sequence + 1
            or current.parent_checksum != previous.checksum
        ):
            raise CtpSessionQueryIntegrityError("CTP session evidence previous chain mismatch")
        return current

    def load_previous_record(self) -> CtpSessionActivityEvidenceRecord:
        if not self.previous_path.exists():
            raise CtpSessionQueryIntegrityError("previous CTP session evidence is missing")
        return self._decode(self.previous_path.read_bytes())

    @staticmethod
    def _replace(path: Path, payload: bytes) -> None:
        temporary: Path | None = None
        try:
            with NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    @staticmethod
    def _decode(payload: bytes) -> CtpSessionActivityEvidenceRecord:
        try:
            raw = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CtpSessionQueryIntegrityError("invalid CTP session evidence JSON") from exc
        required = {
            "kind",
            "schema_version",
            "sequence",
            "parent_checksum",
            "evidence",
            "checksum",
        }
        if not isinstance(raw, dict) or set(raw) != required:
            raise CtpSessionQueryIntegrityError("CTP session evidence envelope is invalid")
        unsigned = {key: value for key, value in raw.items() if key != "checksum"}
        if raw["checksum"] != _digest(unsigned):
            raise CtpSessionQueryIntegrityError("CTP session evidence checksum mismatch")
        if raw["kind"] != _KIND or raw["schema_version"] != _SCHEMA_VERSION:
            raise CtpSessionQueryIntegrityError("CTP session evidence schema is unsupported")
        evidence_raw = raw["evidence"]
        if not isinstance(evidence_raw, dict):
            raise CtpSessionQueryIntegrityError("CTP session evidence payload is invalid")
        account_identity_digest: object = evidence_raw.get("account_identity_digest")
        trading_day: object = evidence_raw.get("trading_day")
        order_request_id: object = evidence_raw.get("order_request_id")
        trade_request_id: object = evidence_raw.get("trade_request_id")
        critical_generation: object = evidence_raw.get("critical_generation")
        query_ingress_generation: object = evidence_raw.get("query_ingress_generation")
        if (
            not isinstance(account_identity_digest, str)
            or not isinstance(trading_day, str)
            or isinstance(order_request_id, bool)
            or not isinstance(order_request_id, int)
            or isinstance(trade_request_id, bool)
            or not isinstance(trade_request_id, int)
            or isinstance(critical_generation, bool)
            or not isinstance(critical_generation, int)
            or isinstance(query_ingress_generation, bool)
            or not isinstance(query_ingress_generation, int)
        ):
            raise CtpSessionQueryIntegrityError("CTP session evidence payload is invalid")
        identity = {
            "broker_id": "",
            "investor_id": "",
            "invest_unit_id": "",
            "trading_day": "",
        }
        orders: list[CtpSessionOrder] = []
        for item in evidence_raw.get("orders", []):
            if not isinstance(item, dict):
                raise CtpSessionQueryIntegrityError("CTP session order evidence is invalid")
            try:
                orders.append(
                    CtpSessionOrder(
                        **{
                            **item,
                            "side": OrderSide(item.get("side")),
                            "offset": Offset(item.get("offset")),
                            "order_type": OrderType(item.get("order_type")),
                        }
                    )
                )
            except (TypeError, ValueError) as exc:
                raise CtpSessionQueryIntegrityError(
                    "CTP session order evidence is invalid"
                ) from exc
        trades: list[CtpSessionTrade] = []
        for item in evidence_raw.get("trades", []):
            if not isinstance(item, dict):
                raise CtpSessionQueryIntegrityError("CTP session trade evidence is invalid")
            try:
                timestamp = datetime.fromisoformat(str(item.get("timestamp", "")))
                trades.append(
                    CtpSessionTrade(
                        **{
                            **item,
                            "side": OrderSide(item.get("side")),
                            "offset": Offset(item.get("offset")),
                            "timestamp": timestamp,
                        }
                    )
                )
            except (TypeError, ValueError) as exc:
                raise CtpSessionQueryIntegrityError(
                    "CTP session trade evidence is invalid"
                ) from exc
        del identity
        evidence = build_ctp_session_activity_evidence(
            account_identity_digest=account_identity_digest,
            trading_day=trading_day,
            order_request_id=order_request_id,
            orders=tuple(orders),
            trades=tuple(trades),
            trade_request_id=trade_request_id,
            critical_generation=critical_generation,
            query_ingress_generation=query_ingress_generation,
        )
        if _evidence_payload(evidence) != evidence_raw:
            raise CtpSessionQueryIntegrityError("CTP session evidence digest mismatch")
        sequence = raw["sequence"]
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise CtpSessionQueryIntegrityError("CTP session evidence sequence is invalid")
        parent = raw["parent_checksum"]
        if parent is not None and (not isinstance(parent, str) or _SHA.fullmatch(parent) is None):
            raise CtpSessionQueryIntegrityError("CTP session evidence parent is invalid")
        return CtpSessionActivityEvidenceRecord(evidence, sequence, parent, raw["checksum"])
