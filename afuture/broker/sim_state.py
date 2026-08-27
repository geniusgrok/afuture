"""Durable, fail-closed state for the simulated broker."""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import stat
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
from math import isclose, isfinite
from pathlib import Path

from ..fees import calculate_commission
from ..models import (
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

SIM_BROKER_STATE_KIND = "afuture.broker.sim-state"
SIM_BROKER_STATE_SCHEMA_VERSION = 1

_ORDER_ID = re.compile(r"SIM-([1-9][0-9]*)")
_TRADE_ID = re.compile(r"SIM-T-([1-9][0-9]*)")
_ENVELOPE_FIELDS = {
    "kind",
    "schema_version",
    "sequence",
    "previous_checksum",
    "configuration",
    "operation",
    "state",
    "checksum",
}
_STATE_FIELDS = {
    "accounting_baseline",
    "balance",
    "realized_pnl",
    "commission",
    "trading_day",
    "positions",
    "orders",
    "trades",
    "ticks",
    "next_order_sequence",
    "next_trade_sequence",
    "tick_sequence",
    "eligible_sequence",
    "depth",
    "order_arrival",
    "first_fill_sequence",
    "contract_specs",
    "trade_trading_days",
    "deposit",
    "withdrawal",
    "cash_flow_verified",
    "previous_settlement_equity",
    "settlement_verified",
    "settlement_id",
}

_BASELINE_FIELDS = {
    "balance",
    "realized_pnl",
    "commission",
    "trading_day",
    "positions",
    "next_order_sequence",
    "next_trade_sequence",
    "compacted_order_count",
    "compacted_trade_count",
    "compacted_through_trading_day",
    "history_digest",
}

_MARKET_STATE_FIELDS = {
    "broker_sequence",
    "broker_checksum",
    "trading_day",
    "ticks",
    "tick_sequence",
    "depth",
    "market_digest",
}

EMPTY_ACCOUNTING_HISTORY_DIGEST = sha256(b"afuture.sim.accounting-history.v1\0").hexdigest()


class SimBrokerStateIntegrityError(RuntimeError):
    """Persisted simulated-broker truth cannot be trusted."""


@dataclass(frozen=True)
class SimBrokerStateRecord:
    sequence: int
    previous_checksum: str | None
    operation: str | None
    state: dict[str, object]
    checksum: str


@dataclass(frozen=True)
class SimAccountingBaseline:
    balance: float
    realized_pnl: float
    commission: float
    trading_day: str
    positions: list[ContractPosition]
    next_order_sequence: int
    next_trade_sequence: int
    compacted_order_count: int
    compacted_trade_count: int
    compacted_through_trading_day: str
    history_digest: str


@dataclass(frozen=True)
class DecodedSimMarketState:
    broker_sequence: int
    broker_checksum: str
    trading_day: str
    ticks: dict[str, Tick]
    tick_sequence: int
    depth: dict[str, list[int]]
    market_digest: str


@dataclass(frozen=True)
class DecodedSimBrokerState:
    accounting_baseline: SimAccountingBaseline
    balance: float
    realized_pnl: float
    commission: float
    trading_day: str
    positions: list[ContractPosition]
    orders: list[Order]
    trades: list[Trade]
    ticks: dict[str, Tick]
    next_order_sequence: int
    next_trade_sequence: int
    tick_sequence: int
    eligible_sequence: dict[str, int]
    depth: dict[str, list[int]]
    order_arrival: dict[str, tuple[float, float, int]]
    first_fill_sequence: dict[str, int]
    contract_specs: dict[str, ContractSpec]
    trade_trading_days: dict[str, str]
    deposit: float
    withdrawal: float
    cash_flow_verified: bool
    previous_settlement_equity: float | None
    settlement_verified: bool
    settlement_id: int


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SimBrokerStateIntegrityError(f"duplicate simulated-broker JSON key: {key}")
        result[key] = value
    return result


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SimBrokerStateIntegrityError("simulated-broker state is not canonical JSON") from exc


def _checksum(unsigned: Mapping[str, object]) -> str:
    return sha256(_canonical_json(dict(unsigned))).hexdigest()


def _mapping(raw: object, fields: set[str], name: str) -> Mapping[str, object]:
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise SimBrokerStateIntegrityError(f"{name} fields are invalid")
    return raw


def _list(raw: object, name: str) -> list[object]:
    if not isinstance(raw, list):
        raise SimBrokerStateIntegrityError(f"{name} must be a list")
    return raw


def _string(raw: object, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(raw, str) or (not allow_empty and not raw):
        raise SimBrokerStateIntegrityError(f"{name} must be a string")
    return raw


def _integer(raw: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < minimum:
        raise SimBrokerStateIntegrityError(f"{name} must be an integer >= {minimum}")
    return raw


def _number(raw: object, name: str, *, minimum: float | None = None) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not isfinite(raw):
        raise SimBrokerStateIntegrityError(f"{name} must be finite")
    value = float(raw)
    if minimum is not None and value < minimum:
        raise SimBrokerStateIntegrityError(f"{name} must be >= {minimum}")
    return value


def _enum(enum_type, raw: object, name: str):
    try:
        return enum_type(raw)
    except (TypeError, ValueError) as exc:
        raise SimBrokerStateIntegrityError(f"{name} is unsupported") from exc


def _timestamp(raw: object, name: str) -> datetime:
    value = _string(raw, name)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SimBrokerStateIntegrityError(f"{name} is invalid") from exc
    if parsed.tzinfo is None:
        raise SimBrokerStateIntegrityError(f"{name} must be timezone-aware")
    return parsed


def _decode_position(raw: object) -> ContractPosition:
    fields = set(ContractPosition.__dataclass_fields__)
    values = _mapping(raw, fields, "position")
    try:
        position = ContractPosition(
            symbol=_string(values["symbol"], "position symbol"),
            exchange=_string(values["exchange"], "position exchange"),
            long_today=_integer(values["long_today"], "long-today position"),
            long_yesterday=_integer(values["long_yesterday"], "long-yesterday position"),
            short_today=_integer(values["short_today"], "short-today position"),
            short_yesterday=_integer(values["short_yesterday"], "short-yesterday position"),
            long_price=_number(values["long_price"], "long position price", minimum=0.0),
            short_price=_number(values["short_price"], "short position price", minimum=0.0),
        )
        position.validate()
    except (TypeError, ValueError) as exc:
        raise SimBrokerStateIntegrityError("persisted position is invalid") from exc
    return position


def _decode_fee(raw: object) -> FeeSpec:
    fields = set(FeeSpec.__dataclass_fields__)
    values = _mapping(raw, fields, "contract fee")
    decoded = {name: _number(values[name], f"contract fee {name}", minimum=0.0) for name in fields}
    return FeeSpec(**decoded)


def _decode_spec(raw: object) -> ContractSpec:
    fields = set(ContractSpec.__dataclass_fields__)
    values = _mapping(raw, fields, "contract spec")
    symbol = _string(values["symbol"], "contract spec symbol")
    exchange = _string(values["exchange"], "contract spec exchange")
    multiplier = _number(values["multiplier"], "contract multiplier", minimum=0.0)
    price_tick = _number(values["price_tick"], "contract price tick", minimum=0.0)
    if multiplier <= 0 or price_tick <= 0:
        raise SimBrokerStateIntegrityError("contract multiplier/price tick must be positive")
    return ContractSpec(
        symbol=symbol,
        exchange=exchange,
        multiplier=multiplier,
        price_tick=price_tick,
        margin_rate_long=_number(values["margin_rate_long"], "long margin rate", minimum=0.0),
        margin_rate_short=_number(values["margin_rate_short"], "short margin rate", minimum=0.0),
        fee=_decode_fee(values["fee"]),
    )


def encode_contract_specs(specs: Mapping[str, ContractSpec]) -> dict[str, object]:
    """Return a canonical, strictly validated contract-spec manifest."""
    encoded: dict[str, object] = {}
    for raw_symbol, spec in sorted(specs.items()):
        symbol = _string(raw_symbol, "contract spec manifest symbol")
        if not isinstance(spec, ContractSpec) or spec.symbol != symbol:
            raise SimBrokerStateIntegrityError("contract spec manifest identity mismatch")
        encoded[symbol] = asdict(_decode_spec(asdict(spec)))
    return encoded


def decode_contract_specs(raw: object) -> dict[str, ContractSpec]:
    if not isinstance(raw, Mapping):
        raise SimBrokerStateIntegrityError("contract specs must be an object")
    result: dict[str, ContractSpec] = {}
    for raw_symbol, raw_spec in raw.items():
        symbol = _string(raw_symbol, "contract spec manifest symbol")
        spec = _decode_spec(raw_spec)
        if spec.symbol != symbol or symbol in result:
            raise SimBrokerStateIntegrityError("contract spec manifest identity mismatch")
        result[symbol] = spec
    return result


def encode_contract_catalog(catalog: list[ContractInfo]) -> list[dict[str, str]]:
    """Return a canonical fixed replay catalog, rejecting ambiguous identities."""
    rows: list[dict[str, str]] = []
    seen_symbols: set[str] = set()
    for item in catalog:
        if not isinstance(item, ContractInfo):
            raise SimBrokerStateIntegrityError("contract catalog row is invalid")
        values = {
            "symbol": item.symbol,
            "exchange": item.exchange,
            "product": item.product,
            "expiry": item.expiry,
            "listing": item.listing,
        }
        for name in ("symbol", "exchange", "product", "expiry"):
            value = values[name]
            if not isinstance(value, str) or not value or value.strip() != value:
                raise SimBrokerStateIntegrityError(f"contract catalog {name} is invalid")
        listing = values["listing"]
        if not isinstance(listing, str) or listing.strip() != listing:
            raise SimBrokerStateIntegrityError("contract catalog listing is invalid")
        for name in ("expiry", "listing"):
            value = values[name]
            if not value:
                continue
            try:
                normalized = datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
            except ValueError as exc:
                raise SimBrokerStateIntegrityError(f"contract catalog {name} is invalid") from exc
            if normalized != value:
                raise SimBrokerStateIntegrityError(f"contract catalog {name} is invalid")
        symbol = values["symbol"]
        if symbol in seen_symbols:
            raise SimBrokerStateIntegrityError("contract catalog identity is duplicated")
        seen_symbols.add(symbol)
        rows.append(values)
    return sorted(
        rows,
        key=lambda row: (
            row["symbol"],
            row["exchange"],
            row["product"],
            row["expiry"],
            row["listing"],
        ),
    )


def _decode_order(raw: object) -> Order:
    values = _mapping(
        raw,
        {"order_id", "request", "status", "traded", "average_price", "message"},
        "order",
    )
    request_raw = _mapping(
        values["request"],
        {
            "symbol",
            "exchange",
            "side",
            "offset",
            "volume",
            "price",
            "order_type",
            "reference",
        },
        "order request",
    )
    request = OrderRequest(
        symbol=_string(request_raw["symbol"], "order symbol"),
        exchange=_string(request_raw["exchange"], "order exchange"),
        side=_enum(OrderSide, request_raw["side"], "order side"),
        offset=_enum(Offset, request_raw["offset"], "order offset"),
        volume=_integer(request_raw["volume"], "order volume", minimum=1),
        price=_number(request_raw["price"], "order price", minimum=0.0),
        order_type=_enum(OrderType, request_raw["order_type"], "order type"),
        reference=_string(request_raw["reference"], "order reference", allow_empty=True),
    )
    if request.price <= 0:
        raise SimBrokerStateIntegrityError("order price must be positive")
    order_id = _string(values["order_id"], "order id")
    if _ORDER_ID.fullmatch(order_id) is None:
        raise SimBrokerStateIntegrityError("order id is invalid")
    status = _enum(OrderStatus, values["status"], "order status")
    traded = _integer(values["traded"], "order traded volume")
    average_price = _number(values["average_price"], "order average price", minimum=0.0)
    message = _string(values["message"], "order message", allow_empty=True)
    if traded > request.volume:
        raise SimBrokerStateIntegrityError("order traded volume exceeds requested volume")
    if (traded == 0) != (average_price == 0.0):
        raise SimBrokerStateIntegrityError("order average price is inconsistent with fills")
    if status is OrderStatus.NOT_TRADED and traded != 0:
        raise SimBrokerStateIntegrityError("not-traded order contains fills")
    if status is OrderStatus.PART_TRADED and not 0 < traded < request.volume:
        raise SimBrokerStateIntegrityError("partial order fill quantity is invalid")
    if status is OrderStatus.FILLED and traded != request.volume:
        raise SimBrokerStateIntegrityError("filled order quantity is invalid")
    if status is OrderStatus.CANCELLED and traded >= request.volume:
        raise SimBrokerStateIntegrityError("cancelled order fill quantity is invalid")
    if status not in {
        OrderStatus.NOT_TRADED,
        OrderStatus.PART_TRADED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
    }:
        raise SimBrokerStateIntegrityError("order status is not produced by SimBroker")
    return Order(order_id, request, status, traded, average_price, message)


def _decode_trade(raw: object) -> Trade:
    values = _mapping(
        raw,
        {
            "trade_id",
            "order_id",
            "symbol",
            "exchange",
            "side",
            "offset",
            "volume",
            "price",
            "timestamp",
            "commission",
        },
        "trade",
    )
    trade = Trade(
        trade_id=_string(values["trade_id"], "trade id"),
        order_id=_string(values["order_id"], "trade order id"),
        symbol=_string(values["symbol"], "trade symbol"),
        exchange=_string(values["exchange"], "trade exchange"),
        side=_enum(OrderSide, values["side"], "trade side"),
        offset=_enum(Offset, values["offset"], "trade offset"),
        volume=_integer(values["volume"], "trade volume", minimum=1),
        price=_number(values["price"], "trade price", minimum=0.0),
        timestamp=_timestamp(values["timestamp"], "trade timestamp"),
        commission=_number(values["commission"], "trade commission", minimum=0.0),
    )
    try:
        trade.validate()
    except ValueError as exc:
        raise SimBrokerStateIntegrityError("persisted trade is invalid") from exc
    if _TRADE_ID.fullmatch(trade.trade_id) is None:
        raise SimBrokerStateIntegrityError("trade id is invalid")
    return trade


def _decode_tick(raw: object) -> Tick:
    values = _mapping(raw, set(Tick.__dataclass_fields__), "tick")
    tick = Tick(
        symbol=_string(values["symbol"], "tick symbol"),
        exchange=_string(values["exchange"], "tick exchange"),
        timestamp=_timestamp(values["timestamp"], "tick timestamp"),
        bid_price=_number(values["bid_price"], "tick bid price"),
        ask_price=_number(values["ask_price"], "tick ask price"),
        last_price=_number(values["last_price"], "tick last price"),
        bid_volume=_number(values["bid_volume"], "tick bid volume"),
        ask_volume=_number(values["ask_volume"], "tick ask volume"),
        trading_day=_string(values["trading_day"], "tick trading day"),
        limit_up=_number(values["limit_up"], "tick upper limit", minimum=0.0),
        limit_down=_number(values["limit_down"], "tick lower limit", minimum=0.0),
        volume=_number(values["volume"], "tick volume", minimum=0.0),
        open_interest=_number(values["open_interest"], "tick open interest", minimum=0.0),
    )
    try:
        tick.validate()
    except ValueError as exc:
        raise SimBrokerStateIntegrityError("persisted tick is invalid") from exc
    return tick


def _encode_order(order: Order) -> dict[str, object]:
    request = order.request
    return {
        "order_id": order.order_id,
        "request": {
            "symbol": request.symbol,
            "exchange": request.exchange,
            "side": request.side.value,
            "offset": request.offset.value,
            "volume": request.volume,
            "price": request.price,
            "order_type": request.order_type.value,
            "reference": request.reference,
        },
        "status": order.status.value,
        "traded": order.traded,
        "average_price": order.average_price,
        "message": order.message,
    }


def _encode_trade(trade: Trade) -> dict[str, object]:
    return {
        "trade_id": trade.trade_id,
        "order_id": trade.order_id,
        "symbol": trade.symbol,
        "exchange": trade.exchange,
        "side": trade.side.value,
        "offset": trade.offset.value,
        "volume": trade.volume,
        "price": trade.price,
        "timestamp": trade.timestamp.isoformat(),
        "commission": trade.commission,
    }


def _encode_tick(tick: Tick) -> dict[str, object]:
    values = asdict(tick)
    values["timestamp"] = tick.timestamp.isoformat()
    return values


def calculate_market_digest(
    trading_day: str,
    ticks: Mapping[str, Tick],
    tick_sequence: int,
    depth: Mapping[str, list[int]],
) -> str:
    """Hash canonical latest-mark content independently of its store generation."""
    payload = {
        "trading_day": trading_day,
        "ticks": [_encode_tick(ticks[symbol]) for symbol in sorted(ticks)],
        "tick_sequence": tick_sequence,
        "depth": {symbol: list(depth[symbol]) for symbol in sorted(depth)},
    }
    return sha256(_canonical_json(payload)).hexdigest()


def advance_accounting_history_digest(
    previous_digest: str,
    orders: list[Order],
    trades: list[Trade],
    trade_trading_days: Mapping[str, str],
    *,
    compacted_through_trading_day: str,
) -> str:
    """Fold one completed session's canonical identities into an audit digest chain."""
    if re.fullmatch(r"[0-9a-f]{64}", previous_digest) is None:
        raise SimBrokerStateIntegrityError("accounting history digest is invalid")
    payload = {
        "previous_digest": previous_digest,
        "compacted_through_trading_day": compacted_through_trading_day,
        "orders": [_encode_order(order) for order in orders],
        "trades": [
            {
                **_encode_trade(trade),
                "trading_day": trade_trading_days[trade.trade_id],
            }
            for trade in trades
        ],
    }
    return sha256(_canonical_json(payload)).hexdigest()


def encode_sim_broker_state(
    state: DecodedSimBrokerState,
    *,
    initial_capital: float,
) -> dict[str, object]:
    """Encode and re-validate one complete simulated-broker snapshot."""
    baseline = state.accounting_baseline
    payload: dict[str, object] = {
        "accounting_baseline": {
            "balance": baseline.balance,
            "realized_pnl": baseline.realized_pnl,
            "commission": baseline.commission,
            "trading_day": baseline.trading_day,
            "positions": [asdict(position) for position in baseline.positions],
            "next_order_sequence": baseline.next_order_sequence,
            "next_trade_sequence": baseline.next_trade_sequence,
            "compacted_order_count": baseline.compacted_order_count,
            "compacted_trade_count": baseline.compacted_trade_count,
            "compacted_through_trading_day": (baseline.compacted_through_trading_day),
            "history_digest": baseline.history_digest,
        },
        "balance": state.balance,
        "realized_pnl": state.realized_pnl,
        "commission": state.commission,
        "trading_day": state.trading_day,
        "positions": [asdict(position) for position in state.positions],
        "orders": [_encode_order(order) for order in state.orders],
        "trades": [_encode_trade(trade) for trade in state.trades],
        "ticks": [_encode_tick(tick) for tick in state.ticks.values()],
        "next_order_sequence": state.next_order_sequence,
        "next_trade_sequence": state.next_trade_sequence,
        "tick_sequence": state.tick_sequence,
        "eligible_sequence": dict(state.eligible_sequence),
        "depth": {symbol: list(depth) for symbol, depth in state.depth.items()},
        "order_arrival": {
            order_id: list(arrival) for order_id, arrival in state.order_arrival.items()
        },
        "first_fill_sequence": dict(state.first_fill_sequence),
        "contract_specs": encode_contract_specs(state.contract_specs),
        "trade_trading_days": dict(state.trade_trading_days),
        "deposit": state.deposit,
        "withdrawal": state.withdrawal,
        "cash_flow_verified": state.cash_flow_verified,
        "previous_settlement_equity": state.previous_settlement_equity,
        "settlement_verified": state.settlement_verified,
        "settlement_id": state.settlement_id,
    }
    decode_sim_broker_state(payload, initial_capital=initial_capital)
    return payload


def decode_sim_broker_state(
    raw: object,
    *,
    initial_capital: float,
) -> DecodedSimBrokerState:
    """Strictly decode one complete simulated-broker snapshot."""
    values = _mapping(raw, _STATE_FIELDS, "simulated-broker state")
    baseline_values = _mapping(
        values["accounting_baseline"],
        _BASELINE_FIELDS,
        "accounting baseline",
    )
    baseline_balance = _number(baseline_values["balance"], "baseline balance")
    baseline_realized_pnl = _number(baseline_values["realized_pnl"], "baseline realized PnL")
    baseline_commission = _number(baseline_values["commission"], "baseline commission", minimum=0.0)
    if not isclose(
        baseline_balance,
        float(initial_capital) + baseline_realized_pnl - baseline_commission,
        rel_tol=1e-12,
        abs_tol=1e-9,
    ):
        raise SimBrokerStateIntegrityError("accounting baseline mismatch")
    baseline_trading_day = _string(
        baseline_values["trading_day"],
        "baseline trading day",
        allow_empty=True,
    )
    if baseline_trading_day:
        try:
            parsed_baseline_day = datetime.strptime(baseline_trading_day, "%Y%m%d").strftime(
                "%Y%m%d"
            )
        except ValueError as exc:
            raise SimBrokerStateIntegrityError("baseline trading day is invalid") from exc
        if parsed_baseline_day != baseline_trading_day:
            raise SimBrokerStateIntegrityError("baseline trading day is invalid")
    baseline_positions = [
        _decode_position(item) for item in _list(baseline_values["positions"], "baseline positions")
    ]
    baseline_position_keys = [
        (position.symbol, position.exchange) for position in baseline_positions
    ]
    if len(baseline_position_keys) != len(set(baseline_position_keys)):
        raise SimBrokerStateIntegrityError("duplicate baseline position")
    baseline_next_order_sequence = _integer(
        baseline_values["next_order_sequence"],
        "baseline next order sequence",
        minimum=1,
    )
    baseline_next_trade_sequence = _integer(
        baseline_values["next_trade_sequence"],
        "baseline next trade sequence",
        minimum=1,
    )
    compacted_order_count = _integer(
        baseline_values["compacted_order_count"], "compacted order count"
    )
    compacted_trade_count = _integer(
        baseline_values["compacted_trade_count"], "compacted trade count"
    )
    compacted_through_trading_day = _string(
        baseline_values["compacted_through_trading_day"],
        "compacted-through trading day",
        allow_empty=True,
    )
    if compacted_through_trading_day:
        try:
            parsed_compacted_day = datetime.strptime(
                compacted_through_trading_day, "%Y%m%d"
            ).strftime("%Y%m%d")
        except ValueError as exc:
            raise SimBrokerStateIntegrityError("compacted-through trading day is invalid") from exc
        if (
            parsed_compacted_day != compacted_through_trading_day
            or not baseline_trading_day
            or compacted_through_trading_day >= baseline_trading_day
        ):
            raise SimBrokerStateIntegrityError("compacted-through trading day is invalid")
    history_digest = _string(baseline_values["history_digest"], "accounting history digest")
    if re.fullmatch(r"[0-9a-f]{64}", history_digest) is None:
        raise SimBrokerStateIntegrityError("accounting history digest is invalid")
    if (
        baseline_next_order_sequence != compacted_order_count + 1
        or baseline_next_trade_sequence != compacted_trade_count + 1
        or (
            compacted_order_count == 0
            and compacted_trade_count == 0
            and not compacted_through_trading_day
            and history_digest != EMPTY_ACCOUNTING_HISTORY_DIGEST
        )
    ):
        raise SimBrokerStateIntegrityError("accounting history identity mismatch")
    accounting_baseline = SimAccountingBaseline(
        balance=baseline_balance,
        realized_pnl=baseline_realized_pnl,
        commission=baseline_commission,
        trading_day=baseline_trading_day,
        positions=baseline_positions,
        next_order_sequence=baseline_next_order_sequence,
        next_trade_sequence=baseline_next_trade_sequence,
        compacted_order_count=compacted_order_count,
        compacted_trade_count=compacted_trade_count,
        compacted_through_trading_day=compacted_through_trading_day,
        history_digest=history_digest,
    )
    balance = _number(values["balance"], "broker balance")
    realized_pnl = _number(values["realized_pnl"], "broker realized PnL")
    commission = _number(values["commission"], "broker commission", minimum=0.0)
    expected_balance = float(initial_capital) + realized_pnl - commission
    if not isclose(balance, expected_balance, rel_tol=1e-12, abs_tol=1e-9):
        raise SimBrokerStateIntegrityError("simulated-broker accounting mismatch")
    trading_day = _string(values["trading_day"], "broker trading day", allow_empty=True)
    if trading_day:
        try:
            parsed_day = datetime.strptime(trading_day, "%Y%m%d").strftime("%Y%m%d")
        except ValueError as exc:
            raise SimBrokerStateIntegrityError("broker trading day is invalid") from exc
        if parsed_day != trading_day:
            raise SimBrokerStateIntegrityError("broker trading day is invalid")
    if baseline_trading_day and (not trading_day or baseline_trading_day > trading_day):
        raise SimBrokerStateIntegrityError("accounting baseline trading day mismatch")

    positions = [_decode_position(item) for item in _list(values["positions"], "positions")]
    position_keys = [(position.symbol, position.exchange) for position in positions]
    if len(position_keys) != len(set(position_keys)):
        raise SimBrokerStateIntegrityError("duplicate persisted position")

    orders = [_decode_order(item) for item in _list(values["orders"], "orders")]
    order_ids = [order.order_id for order in orders]
    if len(order_ids) != len(set(order_ids)):
        raise SimBrokerStateIntegrityError("duplicate persisted order")
    order_by_id = {order.order_id: order for order in orders}

    contract_specs = decode_contract_specs(values["contract_specs"])
    required_spec_symbols = {
        *(position.symbol for position in baseline_positions),
        *(position.symbol for position in positions),
        *(order.request.symbol for order in orders),
    }
    if not required_spec_symbols.issubset(contract_specs):
        raise SimBrokerStateIntegrityError("persisted broker truth lacks contract specs")
    for position in [*baseline_positions, *positions]:
        if contract_specs[position.symbol].exchange != position.exchange:
            raise SimBrokerStateIntegrityError("position/contract spec identity mismatch")
    for order in orders:
        if contract_specs[order.request.symbol].exchange != order.request.exchange:
            raise SimBrokerStateIntegrityError("order/contract spec identity mismatch")

    trades = [_decode_trade(item) for item in _list(values["trades"], "trades")]
    trade_ids = [trade.trade_id for trade in trades]
    if len(trade_ids) != len(set(trade_ids)):
        raise SimBrokerStateIntegrityError("duplicate persisted trade")
    if any(trade.order_id not in order_by_id for trade in trades):
        raise SimBrokerStateIntegrityError("persisted trade refers to an unknown order")
    trade_trading_days_raw = values["trade_trading_days"]
    if not isinstance(trade_trading_days_raw, Mapping):
        raise SimBrokerStateIntegrityError("trade trading days must be an object")
    trade_trading_days = {
        _string(trade_id, "trade-day trade id"): _string(day, "trade trading day")
        for trade_id, day in trade_trading_days_raw.items()
    }
    if set(trade_trading_days) != set(trade_ids):
        raise SimBrokerStateIntegrityError("trade trading days do not match persisted trades")
    traded_volume: dict[str, int] = {}
    traded_notional: dict[str, float] = {}
    for trade in trades:
        order = order_by_id[trade.order_id]
        request = order.request
        if (
            trade.symbol != request.symbol
            or trade.exchange != request.exchange
            or trade.side is not request.side
            or trade.offset is not request.offset
        ):
            raise SimBrokerStateIntegrityError("trade/order identity mismatch")
        spec = contract_specs.get(trade.symbol)
        if spec is None:
            raise SimBrokerStateIntegrityError("persisted trade lacks contract spec")
        if spec.exchange != trade.exchange:
            raise SimBrokerStateIntegrityError("trade/contract spec identity mismatch")
        expected_commission = calculate_commission(
            spec,
            trade.offset,
            trade.price,
            trade.volume,
        )
        if not isclose(
            trade.commission,
            expected_commission,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise SimBrokerStateIntegrityError("persisted trade commission mismatch")
        traded_volume[trade.order_id] = traded_volume.get(trade.order_id, 0) + trade.volume
        traded_notional[trade.order_id] = (
            traded_notional.get(trade.order_id, 0.0) + trade.price * trade.volume
        )
    for order in orders:
        if traded_volume.get(order.order_id, 0) != order.traded:
            raise SimBrokerStateIntegrityError("order and trade fill quantities do not match")
        if order.traded and not isclose(
            traded_notional[order.order_id] / order.traded,
            order.average_price,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise SimBrokerStateIntegrityError("order and trade average prices do not match")
    if not isclose(
        baseline_commission + sum(trade.commission for trade in trades),
        commission,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise SimBrokerStateIntegrityError("broker and trade commissions do not match")

    replay = PositionBook(baseline_positions)
    replay_day = baseline_trading_day
    replay_realized = baseline_realized_pnl
    for trade in trades:
        trade_day = trade_trading_days[trade.trade_id]
        try:
            normalized_day = datetime.strptime(trade_day, "%Y%m%d").strftime("%Y%m%d")
        except ValueError as exc:
            raise SimBrokerStateIntegrityError("trade trading day is invalid") from exc
        if (
            normalized_day != trade_day
            or not trading_day
            or trade_day > trading_day
            or (replay_day and trade_day < replay_day)
        ):
            raise SimBrokerStateIntegrityError("trade trading day is invalid")
        if replay_day and trade_day != replay_day:
            replay.roll_trading_day()
        replay_day = trade_day
        try:
            realized_points = replay.apply_trade(trade)
        except ValueError as exc:
            raise SimBrokerStateIntegrityError("persisted trade history is invalid") from exc
        replay_realized += realized_points * contract_specs[trade.symbol].multiplier
    if replay_day and trading_day and replay_day < trading_day:
        replay.roll_trading_day()
    expected_positions = {
        (position.symbol, position.exchange): asdict(position) for position in replay.all()
    }
    persisted_positions = {
        (position.symbol, position.exchange): asdict(position) for position in positions
    }
    if expected_positions != persisted_positions or not isclose(
        replay_realized,
        realized_pnl,
        rel_tol=1e-12,
        abs_tol=1e-9,
    ):
        raise SimBrokerStateIntegrityError("simulated-broker accounting mismatch")

    tick_rows = [_decode_tick(item) for item in _list(values["ticks"], "ticks")]
    tick_symbols = [tick.symbol for tick in tick_rows]
    if len(tick_symbols) != len(set(tick_symbols)):
        raise SimBrokerStateIntegrityError("duplicate persisted tick symbol")
    ticks = {tick.symbol: tick for tick in tick_rows}
    for tick in tick_rows:
        spec = contract_specs.get(tick.symbol)
        if spec is None or spec.exchange != tick.exchange:
            raise SimBrokerStateIntegrityError("tick/contract spec identity mismatch")
        try:
            normalized_tick_day = datetime.strptime(tick.trading_day, "%Y%m%d").strftime("%Y%m%d")
        except ValueError as exc:
            raise SimBrokerStateIntegrityError("tick trading day is invalid") from exc
        if (
            normalized_tick_day != tick.trading_day
            or not trading_day
            or tick.trading_day > trading_day
        ):
            raise SimBrokerStateIntegrityError("tick trading day is invalid")

    next_order_sequence = _integer(values["next_order_sequence"], "next order sequence", minimum=1)
    next_trade_sequence = _integer(values["next_trade_sequence"], "next trade sequence", minimum=1)
    if (
        baseline_next_order_sequence > next_order_sequence
        or baseline_next_trade_sequence > next_trade_sequence
    ):
        raise SimBrokerStateIntegrityError("accounting baseline sequence mismatch")
    order_sequences: list[int] = []
    for order_id in order_ids:
        match = _ORDER_ID.fullmatch(order_id)
        if match is None:  # Already rejected by _decode_order; keep this boundary total.
            raise SimBrokerStateIntegrityError("order id is invalid")
        order_sequences.append(int(match.group(1)))
    trade_sequences: list[int] = []
    for trade_id in trade_ids:
        match = _TRADE_ID.fullmatch(trade_id)
        if match is None:  # Already rejected by _decode_trade; keep this boundary total.
            raise SimBrokerStateIntegrityError("trade id is invalid")
        trade_sequences.append(int(match.group(1)))
    if order_sequences != list(range(baseline_next_order_sequence, next_order_sequence)):
        raise SimBrokerStateIntegrityError("order sequence history is inconsistent")
    if trade_sequences != list(range(baseline_next_trade_sequence, next_trade_sequence)):
        raise SimBrokerStateIntegrityError("trade sequence history is inconsistent")

    tick_sequence = _integer(values["tick_sequence"], "tick sequence")
    eligible_raw = values["eligible_sequence"]
    if not isinstance(eligible_raw, Mapping):
        raise SimBrokerStateIntegrityError("eligible sequence must be an object")
    eligible_sequence = {
        _string(order_id, "eligible order id"): _integer(sequence, "eligible sequence")
        for order_id, sequence in eligible_raw.items()
    }
    if set(eligible_sequence) != set(order_ids):
        raise SimBrokerStateIntegrityError("eligible sequences do not match persisted orders")

    depth_raw = values["depth"]
    if not isinstance(depth_raw, Mapping):
        raise SimBrokerStateIntegrityError("depth must be an object")
    depth: dict[str, list[int]] = {}
    for symbol, raw_depth in depth_raw.items():
        levels = _list(raw_depth, "depth levels")
        if len(levels) != 2:
            raise SimBrokerStateIntegrityError("depth must contain bid and ask levels")
        depth[_string(symbol, "depth symbol")] = [
            _integer(levels[0], "bid depth"),
            _integer(levels[1], "ask depth"),
        ]
    if set(depth) != set(ticks):
        raise SimBrokerStateIntegrityError("depth and tick symbols do not match")

    arrival_raw = values["order_arrival"]
    if not isinstance(arrival_raw, Mapping):
        raise SimBrokerStateIntegrityError("order arrival must be an object")
    order_arrival: dict[str, tuple[float, float, int]] = {}
    for order_id, raw_arrival in arrival_raw.items():
        arrival = _list(raw_arrival, "order arrival")
        if len(arrival) != 3:
            raise SimBrokerStateIntegrityError("order arrival is invalid")
        bid = _number(arrival[0], "arrival bid", minimum=0.0)
        ask = _number(arrival[1], "arrival ask", minimum=0.0)
        sequence = _integer(arrival[2], "arrival sequence")
        if bid <= 0 or ask < bid or sequence > tick_sequence:
            raise SimBrokerStateIntegrityError("order arrival is invalid")
        order_arrival[_string(order_id, "arrival order id")] = (bid, ask, sequence)
    if not set(order_arrival).issubset(order_by_id):
        raise SimBrokerStateIntegrityError("order arrival refers to an unknown order")

    first_fill_raw = values["first_fill_sequence"]
    if not isinstance(first_fill_raw, Mapping):
        raise SimBrokerStateIntegrityError("first-fill sequence must be an object")
    first_fill_sequence = {
        _string(order_id, "first-fill order id"): _integer(sequence, "first-fill sequence")
        for order_id, sequence in first_fill_raw.items()
    }
    filled_order_ids = {order.order_id for order in orders if order.traded}
    if set(first_fill_sequence) != filled_order_ids or any(
        sequence > tick_sequence for sequence in first_fill_sequence.values()
    ):
        raise SimBrokerStateIntegrityError("first-fill sequences do not match persisted fills")

    deposit = _number(values["deposit"], "broker deposit", minimum=0.0)
    withdrawal = _number(values["withdrawal"], "broker withdrawal", minimum=0.0)
    cash_flow_verified = values["cash_flow_verified"]
    settlement_verified = values["settlement_verified"]
    if not isinstance(cash_flow_verified, bool) or not isinstance(settlement_verified, bool):
        raise SimBrokerStateIntegrityError("broker verification fields must be bool")
    previous_settlement_raw = values["previous_settlement_equity"]
    previous_settlement_equity = (
        None
        if previous_settlement_raw is None
        else _number(
            previous_settlement_raw,
            "previous settlement equity",
            minimum=0.0,
        )
    )
    if previous_settlement_equity is not None and previous_settlement_equity <= 0:
        raise SimBrokerStateIntegrityError("previous settlement equity must be positive")
    settlement_id = _integer(values["settlement_id"], "settlement id")
    if (
        deposit != 0.0
        or withdrawal != 0.0
        or not cash_flow_verified
        or not settlement_verified
        or previous_settlement_equity is None
    ):
        raise SimBrokerStateIntegrityError("simulated-broker settlement evidence is invalid")

    return DecodedSimBrokerState(
        accounting_baseline=accounting_baseline,
        balance=balance,
        realized_pnl=realized_pnl,
        commission=commission,
        trading_day=trading_day,
        positions=positions,
        orders=orders,
        trades=trades,
        ticks=ticks,
        next_order_sequence=next_order_sequence,
        next_trade_sequence=next_trade_sequence,
        tick_sequence=tick_sequence,
        eligible_sequence=eligible_sequence,
        depth=depth,
        order_arrival=order_arrival,
        first_fill_sequence=first_fill_sequence,
        contract_specs=contract_specs,
        trade_trading_days=trade_trading_days,
        deposit=deposit,
        withdrawal=withdrawal,
        cash_flow_verified=cash_flow_verified,
        previous_settlement_equity=previous_settlement_equity,
        settlement_verified=settlement_verified,
        settlement_id=settlement_id,
    )


def encode_sim_market_state(state: DecodedSimMarketState) -> dict[str, object]:
    """Encode the bounded latest-mark overlay without order or trade history."""
    payload: dict[str, object] = {
        "broker_sequence": state.broker_sequence,
        "broker_checksum": state.broker_checksum,
        "trading_day": state.trading_day,
        "ticks": [_encode_tick(tick) for tick in state.ticks.values()],
        "tick_sequence": state.tick_sequence,
        "depth": {symbol: list(depth) for symbol, depth in state.depth.items()},
        "market_digest": state.market_digest,
    }
    return payload


def decode_sim_market_state(
    raw: object,
    *,
    contract_specs: Mapping[str, ContractSpec],
) -> DecodedSimMarketState:
    """Strictly decode one bounded market overlay bound to main broker truth."""
    values = _mapping(raw, _MARKET_STATE_FIELDS, "simulated-broker market state")
    broker_sequence = _integer(values["broker_sequence"], "market broker sequence", minimum=1)
    broker_checksum = _string(values["broker_checksum"], "market broker checksum")
    if re.fullmatch(r"[0-9a-f]{64}", broker_checksum) is None:
        raise SimBrokerStateIntegrityError("market broker checksum is invalid")
    trading_day = _string(values["trading_day"], "market trading day", allow_empty=True)
    if trading_day:
        try:
            normalized_day = datetime.strptime(trading_day, "%Y%m%d").strftime("%Y%m%d")
        except ValueError as exc:
            raise SimBrokerStateIntegrityError("market trading day is invalid") from exc
        if normalized_day != trading_day:
            raise SimBrokerStateIntegrityError("market trading day is invalid")

    tick_rows = [_decode_tick(item) for item in _list(values["ticks"], "market ticks")]
    tick_symbols = [tick.symbol for tick in tick_rows]
    if len(tick_symbols) != len(set(tick_symbols)):
        raise SimBrokerStateIntegrityError("duplicate market tick symbol")
    ticks = {tick.symbol: tick for tick in tick_rows}
    for tick in tick_rows:
        spec = contract_specs.get(tick.symbol)
        if (
            spec is None
            or spec.exchange != tick.exchange
            or not trading_day
            or tick.trading_day != trading_day
        ):
            raise SimBrokerStateIntegrityError("market tick identity mismatch")

    tick_sequence = _integer(values["tick_sequence"], "market tick sequence")
    depth_raw = values["depth"]
    if not isinstance(depth_raw, Mapping):
        raise SimBrokerStateIntegrityError("market depth must be an object")
    depth: dict[str, list[int]] = {}
    for symbol, raw_depth in depth_raw.items():
        levels = _list(raw_depth, "market depth levels")
        if len(levels) != 2:
            raise SimBrokerStateIntegrityError("market depth must contain bid and ask levels")
        depth[_string(symbol, "market depth symbol")] = [
            _integer(levels[0], "market bid depth"),
            _integer(levels[1], "market ask depth"),
        ]
    if set(depth) != set(ticks):
        raise SimBrokerStateIntegrityError("market depth and ticks do not match")
    if (not trading_day and (ticks or tick_sequence)) or tick_sequence < len(ticks):
        raise SimBrokerStateIntegrityError("market tick sequence is inconsistent")
    market_digest = _string(values["market_digest"], "market content digest")
    expected_market_digest = calculate_market_digest(
        trading_day,
        ticks,
        tick_sequence,
        depth,
    )
    if market_digest != expected_market_digest:
        raise SimBrokerStateIntegrityError("market content digest mismatch")
    return DecodedSimMarketState(
        broker_sequence=broker_sequence,
        broker_checksum=broker_checksum,
        trading_day=trading_day,
        ticks=ticks,
        tick_sequence=tick_sequence,
        depth=depth,
        market_digest=market_digest,
    )


def _decoded_equity(state: DecodedSimBrokerState) -> float:
    unrealized = 0.0
    for position in state.positions:
        spec = state.contract_specs[position.symbol]
        tick = state.ticks.get(position.symbol)
        mark = (
            tick.last_price
            if tick is not None
            else max(position.long_price, position.short_price, 0.0)
        )
        unrealized += (mark - position.long_price) * position.long_total * spec.multiplier
        unrealized += (position.short_price - mark) * position.short_total * spec.multiplier
    return state.balance + unrealized


def validate_sim_broker_state_transition(
    previous: DecodedSimBrokerState | None,
    current: DecodedSimBrokerState,
    *,
    initial_capital: float,
) -> None:
    """Validate monotonic cash-flow and settlement evidence across generations."""
    current_previous_equity = current.previous_settlement_equity
    if current_previous_equity is None:  # Kept total if the decoder changes later.
        raise SimBrokerStateIntegrityError("simulated-broker settlement evidence is invalid")
    if previous is None:
        if current.settlement_id != 0 or not isclose(
            current_previous_equity,
            float(initial_capital),
            rel_tol=1e-12,
            abs_tol=1e-9,
        ):
            raise SimBrokerStateIntegrityError(
                "initial simulated-broker settlement evidence is invalid"
            )
        return

    previous_baseline = previous.accounting_baseline
    current_baseline = current.accounting_baseline
    baseline_advanced = (
        current_baseline.history_digest != previous_baseline.history_digest
        or current_baseline.compacted_through_trading_day
        != previous_baseline.compacted_through_trading_day
    )
    if not baseline_advanced:
        if current_baseline != previous_baseline:
            raise SimBrokerStateIntegrityError("accounting baseline changed unexpectedly")
    else:
        expected_history_digest = advance_accounting_history_digest(
            previous_baseline.history_digest,
            previous.orders,
            previous.trades,
            previous.trade_trading_days,
            compacted_through_trading_day=previous.trading_day,
        )
        rolled_positions = PositionBook(previous.positions)
        rolled_positions.roll_trading_day()
        expected_positions = {
            (position.symbol, position.exchange): asdict(position)
            for position in rolled_positions.all()
        }
        baseline_positions = {
            (position.symbol, position.exchange): asdict(position)
            for position in current_baseline.positions
        }
        if (
            current_baseline.history_digest != expected_history_digest
            or current_baseline.compacted_order_count
            != previous_baseline.compacted_order_count + len(previous.orders)
            or current_baseline.compacted_trade_count
            != previous_baseline.compacted_trade_count + len(previous.trades)
            or current_baseline.compacted_through_trading_day != previous.trading_day
            or current_baseline.next_order_sequence != previous.next_order_sequence
            or current_baseline.next_trade_sequence != previous.next_trade_sequence
            or current_baseline.trading_day != current.trading_day
            or not isclose(
                current_baseline.balance,
                previous.balance,
                rel_tol=1e-12,
                abs_tol=1e-9,
            )
            or not isclose(
                current_baseline.realized_pnl,
                previous.realized_pnl,
                rel_tol=1e-12,
                abs_tol=1e-9,
            )
            or not isclose(
                current_baseline.commission,
                previous.commission,
                rel_tol=1e-12,
                abs_tol=1e-9,
            )
            or baseline_positions != expected_positions
        ):
            raise SimBrokerStateIntegrityError("accounting history compaction transition mismatch")

    previous_equity = previous.previous_settlement_equity
    if previous_equity is None:  # Kept total if the decoder changes later.
        raise SimBrokerStateIntegrityError("simulated-broker settlement evidence is invalid")
    if previous.trading_day and (
        not current.trading_day or current.trading_day < previous.trading_day
    ):
        raise SimBrokerStateIntegrityError("simulated-broker trading day moved backward")

    crossed_established_day = bool(
        previous.trading_day and current.trading_day and current.trading_day != previous.trading_day
    )
    expected_settlement_id = previous.settlement_id + int(crossed_established_day)
    if current.settlement_id != expected_settlement_id:
        raise SimBrokerStateIntegrityError("simulated-broker settlement id is not adjacent")
    expected_previous_equity = (
        _decoded_equity(previous) if crossed_established_day else previous_equity
    )
    if not isclose(
        current_previous_equity,
        expected_previous_equity,
        rel_tol=1e-12,
        abs_tol=1e-9,
    ):
        raise SimBrokerStateIntegrityError("simulated-broker previous settlement equity mismatch")


class SimBrokerStateStore:
    """Advance one authoritative broker snapshot through atomic replacements."""

    def __init__(self, path: str | Path, configuration: Mapping[str, object]) -> None:
        if isinstance(path, str) and not path.strip():
            raise ValueError("simulated-broker state path must not be empty")
        self.path = Path(path)
        if not self.path.name:
            raise ValueError("simulated-broker state path must name a file")
        self.configuration = json.loads(_canonical_json(dict(configuration)).decode("utf-8"))
        self._sequence = 0
        self._current_checksum = ""
        self._previous_record: SimBrokerStateRecord | None = None

    @property
    def previous_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.prev")

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.lock")

    @property
    def previous_record(self) -> SimBrokerStateRecord | None:
        return self._previous_record

    @property
    def sequence(self) -> int:
        if self._sequence < 1:
            raise SimBrokerStateIntegrityError("simulated-broker store is not initialized")
        return self._sequence

    @property
    def current_checksum(self) -> str:
        if not self._current_checksum:
            raise SimBrokerStateIntegrityError("simulated-broker store is not initialized")
        return self._current_checksum

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_descriptor = os.open(self.path.parent, directory_flags)
        descriptor: int | None = None
        try:
            fcntl.flock(directory_descriptor, fcntl.LOCK_EX)
            self._validate_open_path_identity(
                directory_descriptor,
                self.path.parent,
                expected_directory=True,
            )
            lock_flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(
                    self.lock_path.name,
                    lock_flags,
                    0o600,
                    dir_fd=directory_descriptor,
                )
            except OSError as exc:
                raise SimBrokerStateIntegrityError(
                    "simulated-broker lock identity is invalid"
                ) from exc
            self._validate_open_relative_identity(
                descriptor,
                directory_descriptor,
                self.lock_path.name,
            )
            try:
                yield directory_descriptor
            finally:
                self._validate_open_relative_identity(
                    descriptor,
                    directory_descriptor,
                    self.lock_path.name,
                )
                self._validate_open_path_identity(
                    directory_descriptor,
                    self.path.parent,
                    expected_directory=True,
                )
        finally:
            if descriptor is not None:
                os.close(descriptor)
            fcntl.flock(directory_descriptor, fcntl.LOCK_UN)
            os.close(directory_descriptor)

    @staticmethod
    def _validate_open_path_identity(
        descriptor: int,
        path: Path,
        *,
        expected_directory: bool,
    ) -> None:
        try:
            opened = os.fstat(descriptor)
            current = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise SimBrokerStateIntegrityError(
                "simulated-broker lock identity changed while held"
            ) from exc
        expected_type = stat.S_ISDIR if expected_directory else stat.S_ISREG
        if (
            not expected_type(opened.st_mode)
            or not expected_type(current.st_mode)
            or opened.st_dev != current.st_dev
            or opened.st_ino != current.st_ino
            or opened.st_nlink < 1
        ):
            raise SimBrokerStateIntegrityError("simulated-broker lock identity changed while held")

    @staticmethod
    def _validate_open_relative_identity(
        descriptor: int,
        directory_descriptor: int,
        name: str,
    ) -> None:
        try:
            opened = os.fstat(descriptor)
            current = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise SimBrokerStateIntegrityError(
                "simulated-broker lock identity changed while held"
            ) from exc
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or opened.st_dev != current.st_dev
            or opened.st_ino != current.st_ino
            or opened.st_nlink < 1
        ):
            raise SimBrokerStateIntegrityError("simulated-broker lock identity changed while held")

    @staticmethod
    def _exists_at(directory_descriptor: int, name: str) -> bool:
        try:
            os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise SimBrokerStateIntegrityError(
                "simulated-broker state namespace is invalid"
            ) from exc
        return True

    @staticmethod
    def _read_at(directory_descriptor: int, name: str) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=directory_descriptor)
        except OSError as exc:
            raise SimBrokerStateIntegrityError("simulated-broker state cannot be read") from exc
        try:
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        finally:
            os.close(descriptor)

    def load(self) -> SimBrokerStateRecord | None:
        with self._locked() as directory_descriptor:
            if not self._exists_at(directory_descriptor, self.path.name):
                if self._exists_at(directory_descriptor, self.previous_path.name):
                    raise SimBrokerStateIntegrityError(
                        "current simulated-broker state is missing while previous evidence exists"
                    )
                self._previous_record = None
                return None
            current = self._decode(self._read_at(directory_descriptor, self.path.name))
            self._previous_record = self._validate_previous(
                current,
                directory_descriptor,
            )
            self._sequence = current.sequence
            self._current_checksum = current.checksum
            return current

    def initialize(self, state: Mapping[str, object]) -> None:
        with self._locked() as directory_descriptor:
            if self._exists_at(directory_descriptor, self.path.name) or self._exists_at(
                directory_descriptor, self.previous_path.name
            ):
                raise SimBrokerStateIntegrityError("simulated-broker state already exists")
            encoded = self._encode(1, None, None, state)
            self._atomic_replace(directory_descriptor, self.path, encoded)
            record = self._decode(encoded)
            self._previous_record = None
            self._sequence = record.sequence
            self._current_checksum = record.checksum

    def begin(self, state: Mapping[str, object], operation: str) -> None:
        if not operation:
            raise ValueError("broker operation name must not be empty")
        self._advance(state, operation=operation, expected_operation=None)

    def complete(self, state: Mapping[str, object], operation: str) -> None:
        self._advance(state, operation=None, expected_operation=operation)

    def _advance(
        self,
        state: Mapping[str, object],
        *,
        operation: str | None,
        expected_operation: str | None,
    ) -> None:
        with self._locked() as directory_descriptor:
            if not self._exists_at(directory_descriptor, self.path.name):
                raise SimBrokerStateIntegrityError("current simulated-broker state is missing")
            previous_bytes = self._read_at(directory_descriptor, self.path.name)
            previous = self._decode(previous_bytes)
            if previous.sequence != self._sequence or previous.checksum != self._current_checksum:
                raise SimBrokerStateIntegrityError("competing simulated-broker writer detected")
            if previous.operation != expected_operation:
                raise SimBrokerStateIntegrityError("simulated-broker operation transition mismatch")
            encoded = self._encode(
                previous.sequence + 1,
                previous.checksum,
                operation,
                state,
            )
            self._atomic_replace(directory_descriptor, self.previous_path, previous_bytes)
            self._atomic_replace(directory_descriptor, self.path, encoded)
            record = self._decode(encoded)
            self._previous_record = previous
            self._sequence = record.sequence
            self._current_checksum = record.checksum

    def _validate_previous(
        self,
        current: SimBrokerStateRecord,
        directory_descriptor: int,
    ) -> SimBrokerStateRecord | None:
        if not self._exists_at(directory_descriptor, self.previous_path.name):
            if current.sequence != 1:
                raise SimBrokerStateIntegrityError(
                    "previous simulated-broker state evidence is missing"
                )
            return None
        try:
            previous = self._decode(self._read_at(directory_descriptor, self.previous_path.name))
        except SimBrokerStateIntegrityError as exc:
            raise SimBrokerStateIntegrityError(f"previous simulated-broker state {exc}") from exc
        if previous.sequence == current.sequence and previous.checksum == current.checksum:
            # A failed replace can leave two identical verified generations. No
            # transition was lost; a pending current marker is still rejected by Sim.
            return previous
        if previous.sequence + 1 != current.sequence:
            raise SimBrokerStateIntegrityError(
                "previous simulated-broker state sequence is not the predecessor"
            )
        if current.previous_checksum != previous.checksum:
            raise SimBrokerStateIntegrityError(
                "previous simulated-broker predecessor checksum mismatch"
            )
        return previous

    def _encode(
        self,
        sequence: int,
        previous_checksum: str | None,
        operation: str | None,
        state: Mapping[str, object],
    ) -> bytes:
        unsigned: dict[str, object] = {
            "kind": SIM_BROKER_STATE_KIND,
            "schema_version": SIM_BROKER_STATE_SCHEMA_VERSION,
            "sequence": sequence,
            "previous_checksum": previous_checksum,
            "configuration": self.configuration,
            "operation": operation,
            "state": dict(state),
        }
        envelope = {**unsigned, "checksum": _checksum(unsigned)}
        try:
            return json.dumps(
                envelope,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise SimBrokerStateIntegrityError(
                "simulated-broker state is not serializable"
            ) from exc

    def _decode(self, payload: bytes) -> SimBrokerStateRecord:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SimBrokerStateIntegrityError("invalid simulated-broker state UTF-8") from exc
        try:
            raw = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
        except json.JSONDecodeError as exc:
            raise SimBrokerStateIntegrityError("invalid simulated-broker state JSON") from exc
        values = _mapping(raw, _ENVELOPE_FIELDS, "simulated-broker envelope")
        if values["kind"] != SIM_BROKER_STATE_KIND:
            raise SimBrokerStateIntegrityError("simulated-broker state kind mismatch")
        schema_version = values["schema_version"]
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != SIM_BROKER_STATE_SCHEMA_VERSION
        ):
            raise SimBrokerStateIntegrityError("simulated-broker state schema mismatch")
        sequence = _integer(values["sequence"], "simulated-broker sequence", minimum=1)
        previous_checksum = values["previous_checksum"]
        if sequence == 1:
            if previous_checksum is not None:
                raise SimBrokerStateIntegrityError(
                    "initial simulated-broker predecessor checksum is invalid"
                )
        elif (
            not isinstance(previous_checksum, str)
            or re.fullmatch(r"[0-9a-f]{64}", previous_checksum) is None
        ):
            raise SimBrokerStateIntegrityError("simulated-broker predecessor checksum is invalid")
        configuration = values["configuration"]
        if not isinstance(configuration, Mapping):
            raise SimBrokerStateIntegrityError("simulated-broker configuration is invalid")
        operation = values["operation"]
        if operation is not None and (not isinstance(operation, str) or not operation):
            raise SimBrokerStateIntegrityError("simulated-broker operation marker is invalid")
        state = values["state"]
        if not isinstance(state, Mapping):
            raise SimBrokerStateIntegrityError("simulated-broker state payload is invalid")
        checksum = values["checksum"]
        unsigned = {key: value for key, value in values.items() if key != "checksum"}
        if not isinstance(checksum, str) or checksum != _checksum(unsigned):
            raise SimBrokerStateIntegrityError("simulated-broker state checksum mismatch")
        if _canonical_json(dict(configuration)) != _canonical_json(self.configuration):
            raise SimBrokerStateIntegrityError("simulated-broker configuration mismatch")
        return SimBrokerStateRecord(
            sequence,
            previous_checksum,
            operation,
            dict(state),
            checksum,
        )

    @staticmethod
    def _replace_at(
        directory_descriptor: int,
        source_name: str,
        target: Path,
    ) -> None:
        os.replace(
            source_name,
            target.name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )

    @classmethod
    def _atomic_replace(
        cls,
        directory_descriptor: int,
        target: Path,
        payload: bytes,
    ) -> None:
        temp_name = f".{target.name}.tmp-{secrets.token_hex(16)}"
        descriptor: int | None = None
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(
                temp_name,
                flags,
                0o600,
                dir_fd=directory_descriptor,
            )
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("simulated-broker state write made no progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            cls._replace_at(directory_descriptor, temp_name, target)
            os.fsync(directory_descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temp_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass
