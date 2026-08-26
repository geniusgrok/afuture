"""Authoritative raw-tick Price x OI evidence for Stress-90.

Callbacks update bounded memory only.  A caller checkpoints the complete in-memory state
after a bounded broker event batch; persistence never occurs from ``observe_raw_tick``.
Missing coverage, late/out-of-order evidence and identity conflicts remain distinct from
a legal zero flow and make the affected product fail closed.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256
from math import isfinite
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from types import MappingProxyType
from typing import TypedDict
from zoneinfo import ZoneInfo

from .broker.base import RawMarketEvidenceError, RawMarketEvidenceFatalError
from .directional_sessions import PRODUCT_SESSION_MANIFEST, session_bucket_for_tick
from .directional_stress90_policy import STRESS90_POLICY, canonical_stress90_digest
from .models import ContractInfo, Tick

OI_EVIDENCE_KIND = "afuture.directional.stress90.oi-evidence"
OI_EVIDENCE_SCHEMA_VERSION = 2
CTP_RAW_TICK_SOURCE = "ctp_raw_tick"
FIXED_HISTORICAL_60M_SOURCE = "fixed_historical_60m"
DEFAULT_COMPLETED_RETENTION_DAYS = 512
MAX_COMPLETED_RETENTION_DAYS = 2_048
MAX_EXPECTED_CONTRACTS = 5_000
MAX_BARS_PER_CONTRACT = 16
MAX_OI_ISSUES = 256
_BOUNDARY_GRACE_SECONDS = 5 * 60
_CHINA = ZoneInfo("Asia/Shanghai")


class OiEvidenceIntegrityError(RawMarketEvidenceError):
    """Raw or persisted OI evidence cannot be trusted."""


class Stress90EvidenceOnlyBroker:
    """Expose market/account reads while making order capabilities unreachable."""

    _ALLOWED = frozenset(
        {
            "account_identity_verified",
            "contract_catalog_verified_for_day",
            "get_account",
            "get_account_identity_digest",
            "get_active_orders",
            "get_contract_catalog",
            "get_contract_catalog_status",
            "get_positions",
            "get_session_trades",
            "get_session_activity_account_identity_digest",
            "get_trading_day",
            "has_pending_critical_events",
            "health_error",
            "is_ready",
            "owns_order",
            "poll_events",
            "refresh_contract_catalog",
            "refresh_session_activity",
            "require_session_activity_evidence_current",
            "set_raw_tick_observer",
            "snapshot_marker",
            "snapshot_ready",
            "start",
            "stop",
            "subscribe",
        }
    )

    def __init__(self, source) -> None:
        self._source = source

    @property
    def metadata_query_blocks(self) -> bool:
        return bool(getattr(self._source, "metadata_query_blocks", True))

    def __getattr__(self, name: str):
        if name not in self._ALLOWED:
            raise AttributeError(f"evidence-only Broker capability is unavailable: {name}")
        return getattr(self._source, name)

    def send_order(self, _request):
        raise RuntimeError("Stress-90 evidence-only Broker cannot send orders")

    def cancel_order(self, _order_id) -> None:
        raise RuntimeError("Stress-90 evidence-only Broker cannot cancel orders")


class _FixedHistoricalRow(TypedDict):
    datetime: datetime
    product: str
    symbol: str
    open: float
    close: float
    volume: float
    hold: float


@dataclass(frozen=True)
class OiBarEvidence:
    session: str
    bucket_start: datetime
    bucket_end: datetime
    first_open: float
    last_close: float
    first_hold: float
    last_hold: float
    total_volume: float
    first_tick_timestamp: datetime
    last_tick_timestamp: datetime


@dataclass(frozen=True)
class ContractOiEvidence:
    symbol: str
    exchange: str
    product: str
    trading_day: str
    first_open: float
    last_close: float
    first_hold: float
    last_hold: float
    total_volume: float
    first_tick_timestamp: datetime
    last_tick_timestamp: datetime
    last_cumulative_volume: float
    bars: tuple[OiBarEvidence, ...]
    raw_tick_count: int
    duplicate_tick_count: int
    volume_reset_count: int
    complete: bool
    issues: tuple[str, ...]


@dataclass(frozen=True)
class CompletedOiEvidence:
    source: str
    trading_day: str
    expected_contracts: Mapping[str, tuple[str, ...]]
    received_contracts: tuple[str, ...]
    missing_contracts: tuple[str, ...]
    contracts: Mapping[str, ContractOiEvidence]
    dominant_symbols: Mapping[str, str | None]
    flows: Mapping[str, int | None]
    complete: bool
    issues: tuple[str, ...]
    evidence_digest: str


@dataclass(frozen=True)
class InProgressOiEvidence:
    trading_day: str
    expected_contracts: Mapping[str, tuple[str, ...]]
    contracts: Mapping[str, ContractOiEvidence]
    issues: tuple[str, ...]


@dataclass(frozen=True)
class ObservedTradingDayTransition:
    """One source→target CTP day change observed without a process restart."""

    source_trading_day: str
    target_trading_day: str
    completed_oi_evidence_digest: str


@dataclass(frozen=True)
class Stress90OiEvidenceState:
    completed: tuple[CompletedOiEvidence, ...] = ()
    in_progress: InProgressOiEvidence | None = None
    observed_transitions: tuple[ObservedTradingDayTransition, ...] = ()
    raw_ticks_observed: int = 0
    duplicate_ticks: int = 0
    volume_resets: int = 0


@dataclass(frozen=True)
class Stress90OiEvidenceRecord:
    state: Stress90OiEvidenceState
    sequence: int
    checksum: str


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise OiEvidenceIntegrityError(f"duplicate OI evidence JSON key: {key}")
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
        raise OiEvidenceIntegrityError("OI evidence is not canonical JSON") from exc


def _checksum(value: Mapping[str, object]) -> str:
    return sha256(_canonical_json(dict(value))).hexdigest()


def _valid_day(raw: object, *, name: str) -> str:
    if not isinstance(raw, str):
        raise OiEvidenceIntegrityError(f"{name} must be YYYYMMDD")
    try:
        parsed = datetime.strptime(raw, "%Y%m%d").strftime("%Y%m%d")
    except ValueError as exc:
        raise OiEvidenceIntegrityError(f"{name} must be YYYYMMDD") from exc
    if parsed != raw:
        raise OiEvidenceIntegrityError(f"{name} must be YYYYMMDD")
    return raw


def _valid_datetime(raw: object, *, name: str) -> datetime:
    if isinstance(raw, datetime):
        value = raw
    elif isinstance(raw, str):
        try:
            value = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise OiEvidenceIntegrityError(f"{name} must be ISO datetime") from exc
    else:
        raise OiEvidenceIntegrityError(f"{name} must be ISO datetime")
    if value.tzinfo is None:
        raise OiEvidenceIntegrityError(f"{name} must be timezone-aware")
    return value


def _finite_nonnegative(raw: object, *, name: str) -> float:
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not isfinite(raw)
        or float(raw) < 0.0
    ):
        raise OiEvidenceIntegrityError(f"{name} must be finite and non-negative")
    return float(raw)


def _finite_positive(raw: object, *, name: str) -> float:
    value = _finite_nonnegative(raw, name=name)
    if value <= 0.0:
        raise OiEvidenceIntegrityError(f"{name} must be finite and positive")
    return value


def _valid_count(raw: object, *, name: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise OiEvidenceIntegrityError(f"{name} must be a non-negative integer")
    return raw


def _expected_contracts(
    contracts: Sequence[ContractInfo],
    *,
    trading_day: str,
) -> Mapping[str, tuple[str, ...]]:
    target_date = datetime.strptime(trading_day, "%Y%m%d").date()
    by_product: dict[str, list[str]] = {product: [] for product in STRESS90_POLICY.oi_products}
    seen: set[str] = set()
    for contract in contracts:
        if not isinstance(contract, ContractInfo):
            raise OiEvidenceIntegrityError("OI expected universe contains invalid contract")
        product = str(contract.product).upper()
        if product not in by_product:
            continue
        expected_exchange = PRODUCT_SESSION_MANIFEST[product].exchange
        if str(contract.exchange).upper() != expected_exchange:
            raise OiEvidenceIntegrityError(
                f"OI expected contract exchange mismatch: {contract.symbol}"
            )
        symbol = str(contract.symbol).upper()
        match = re.fullmatch(r"([A-Z]{1,4})\d{3,4}", symbol)
        if match is None or match.group(1) != product:
            raise OiEvidenceIntegrityError(
                f"OI expected contract is not a futures symbol: {symbol}"
            )
        try:
            expiry = datetime.fromisoformat(str(contract.expiry)).date()
            listing = (
                datetime.fromisoformat(str(contract.listing)).date()
                if str(contract.listing)
                else None
            )
        except ValueError as exc:
            raise OiEvidenceIntegrityError(
                f"OI expected contract lifecycle is invalid: {symbol}"
            ) from exc
        if expiry < target_date or (listing is not None and listing > target_date):
            continue
        if symbol in seen:
            raise OiEvidenceIntegrityError("OI expected contract symbols are invalid or duplicate")
        seen.add(symbol)
        by_product[product].append(symbol)
    if len(seen) > MAX_EXPECTED_CONTRACTS:
        raise OiEvidenceIntegrityError("OI expected contract universe exceeds memory bound")
    missing_products = sorted(product for product, symbols in by_product.items() if not symbols)
    if missing_products:
        raise OiEvidenceIntegrityError(
            f"OI expected contract universe missing products: {missing_products}"
        )
    return MappingProxyType(
        {product: tuple(sorted(by_product[product])) for product in STRESS90_POLICY.oi_products}
    )


def _all_expected_symbols(expected: Mapping[str, tuple[str, ...]]) -> set[str]:
    return {symbol for symbols in expected.values() for symbol in symbols}


def _bar_payload(bar: OiBarEvidence) -> dict[str, object]:
    return {
        "session": bar.session,
        "bucket_start": bar.bucket_start.isoformat(),
        "bucket_end": bar.bucket_end.isoformat(),
        "first_open": bar.first_open,
        "last_close": bar.last_close,
        "first_hold": bar.first_hold,
        "last_hold": bar.last_hold,
        "total_volume": bar.total_volume,
        "first_tick_timestamp": bar.first_tick_timestamp.isoformat(),
        "last_tick_timestamp": bar.last_tick_timestamp.isoformat(),
    }


def _contract_payload(contract: ContractOiEvidence) -> dict[str, object]:
    return {
        "symbol": contract.symbol,
        "exchange": contract.exchange,
        "product": contract.product,
        "trading_day": contract.trading_day,
        "first_open": contract.first_open,
        "last_close": contract.last_close,
        "first_hold": contract.first_hold,
        "last_hold": contract.last_hold,
        "total_volume": contract.total_volume,
        "first_tick_timestamp": contract.first_tick_timestamp.isoformat(),
        "last_tick_timestamp": contract.last_tick_timestamp.isoformat(),
        "last_cumulative_volume": contract.last_cumulative_volume,
        "bars": [_bar_payload(bar) for bar in contract.bars],
        "raw_tick_count": contract.raw_tick_count,
        "duplicate_tick_count": contract.duplicate_tick_count,
        "volume_reset_count": contract.volume_reset_count,
        "complete": contract.complete,
        "issues": list(contract.issues),
    }


def _expected_payload(expected: Mapping[str, tuple[str, ...]]) -> dict[str, list[str]]:
    return {product: list(expected[product]) for product in STRESS90_POLICY.oi_products}


def _completed_unsigned_payload(evidence: CompletedOiEvidence) -> dict[str, object]:
    return {
        "source": evidence.source,
        "trading_day": evidence.trading_day,
        "expected_contracts": _expected_payload(evidence.expected_contracts),
        "received_contracts": list(evidence.received_contracts),
        "missing_contracts": list(evidence.missing_contracts),
        "contracts": {
            symbol: _contract_payload(contract)
            for symbol, contract in sorted(evidence.contracts.items())
        },
        "dominant_symbols": dict(evidence.dominant_symbols),
        "flows": dict(evidence.flows),
        "complete": evidence.complete,
        "issues": list(evidence.issues),
    }


def _completed_payload(evidence: CompletedOiEvidence) -> dict[str, object]:
    return {**_completed_unsigned_payload(evidence), "evidence_digest": evidence.evidence_digest}


def _fixed_historical_timestamp(raw: object) -> datetime:
    if isinstance(raw, datetime):
        value = raw
    elif isinstance(raw, str):
        try:
            value = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise OiEvidenceIntegrityError("fixed historical 60m timestamp is invalid") from exc
    else:
        raise OiEvidenceIntegrityError("fixed historical 60m timestamp is invalid")
    if value.tzinfo is None:
        value = value.replace(tzinfo=_CHINA)
    return value.astimezone(_CHINA)


def _fixed_historical_number(
    raw: object,
    *,
    name: str,
    positive: bool = False,
) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not isfinite(raw):
        raise OiEvidenceIntegrityError(f"fixed historical 60m {name} is invalid")
    value = float(raw)
    if value < 0.0 or (positive and value <= 0.0):
        raise OiEvidenceIntegrityError(f"fixed historical 60m {name} is invalid")
    return value


def build_fixed_historical_60m_evidence(
    trading_day: str,
    rows: Sequence[Mapping[str, object]],
) -> CompletedOiEvidence:
    """Build one completed bootstrap bridge from already-verified fixed 60m rows."""

    day = _valid_day(trading_day, name="fixed historical OI trading day")
    required = {"datetime", "product", "symbol", "open", "close", "volume", "hold"}
    normalized: list[_FixedHistoricalRow] = []
    seen: set[tuple[datetime, str, str]] = set()
    for raw in rows:
        if not isinstance(raw, Mapping) or not required.issubset(raw):
            raise OiEvidenceIntegrityError("fixed historical 60m row fields are invalid")
        timestamp = _fixed_historical_timestamp(raw["datetime"])
        if timestamp.strftime("%Y%m%d") != day:
            raise OiEvidenceIntegrityError("fixed historical 60m row is outside trading day")
        product = str(raw["product"]).upper()
        symbol = str(raw["symbol"]).upper()
        if product not in STRESS90_POLICY.oi_products:
            continue
        match = re.fullmatch(r"([A-Z]{1,4})\d{3,4}", symbol)
        if match is None or match.group(1) != product:
            raise OiEvidenceIntegrityError("fixed historical 60m contract identity is invalid")
        key = (timestamp, product, symbol)
        if key in seen:
            raise OiEvidenceIntegrityError("fixed historical 60m rows are duplicate")
        seen.add(key)
        normalized.append(
            {
                "datetime": timestamp,
                "product": product,
                "symbol": symbol,
                "open": _fixed_historical_number(raw["open"], name="open", positive=True),
                "close": _fixed_historical_number(raw["close"], name="close", positive=True),
                "volume": _fixed_historical_number(raw["volume"], name="volume"),
                "hold": _fixed_historical_number(raw["hold"], name="hold"),
            }
        )
    received_products = {str(row["product"]) for row in normalized}
    missing_products = sorted(set(STRESS90_POLICY.oi_products) - received_products)
    if missing_products:
        raise OiEvidenceIntegrityError(
            f"fixed historical 60m OI coverage is incomplete for {day}: missing={missing_products}"
        )
    by_contract: dict[tuple[str, str], list[_FixedHistoricalRow]] = {}
    for row in normalized:
        contract_key = (row["product"], row["symbol"])
        by_contract.setdefault(contract_key, []).append(row)
    contracts: dict[str, ContractOiEvidence] = {}
    expected: dict[str, list[str]] = {product: [] for product in STRESS90_POLICY.oi_products}
    for (product, symbol), contract_rows in sorted(by_contract.items()):
        contract_rows.sort(key=lambda item: item["datetime"])
        if len(contract_rows) > MAX_BARS_PER_CONTRACT:
            raise OiEvidenceIntegrityError("fixed historical 60m bar memory bound exceeded")
        bars = tuple(
            OiBarEvidence(
                session=FIXED_HISTORICAL_60M_SOURCE,
                bucket_start=row["datetime"],
                bucket_end=row["datetime"] + timedelta(hours=1),
                first_open=float(row["open"]),
                last_close=float(row["close"]),
                first_hold=float(row["hold"]),
                last_hold=float(row["hold"]),
                total_volume=float(row["volume"]),
                first_tick_timestamp=row["datetime"],
                last_tick_timestamp=row["datetime"] + timedelta(hours=1),
            )
            for row in contract_rows
        )
        first = contract_rows[0]
        last = contract_rows[-1]
        contracts[symbol] = ContractOiEvidence(
            symbol=symbol,
            exchange=PRODUCT_SESSION_MANIFEST[product].exchange,
            product=product,
            trading_day=day,
            first_open=float(first["open"]),
            last_close=float(last["close"]),
            first_hold=float(first["hold"]),
            last_hold=float(last["hold"]),
            total_volume=sum(float(row["volume"]) for row in contract_rows),
            first_tick_timestamp=bars[0].first_tick_timestamp,
            last_tick_timestamp=bars[-1].last_tick_timestamp,
            last_cumulative_volume=float(last["volume"]),
            bars=bars,
            raw_tick_count=len(bars),
            duplicate_tick_count=0,
            volume_reset_count=0,
            complete=True,
            issues=(),
        )
        expected[product].append(symbol)

    dominant: dict[str, str | None] = {}
    flows: dict[str, int | None] = {}
    for product in STRESS90_POLICY.oi_products:
        product_contracts = [contracts[symbol] for symbol in expected[product]]
        product_contracts.sort(key=lambda item: (-item.last_hold, -item.total_volume, item.symbol))
        selected = product_contracts[0]
        dominant[product] = selected.symbol
        change = selected.last_close - selected.first_open
        if selected.last_hold <= selected.first_hold or abs(change) <= 1e-15:
            flows[product] = 0
        else:
            flows[product] = 1 if change > 0.0 else -1
    expected_proxy = MappingProxyType(
        {product: tuple(sorted(expected[product])) for product in STRESS90_POLICY.oi_products}
    )
    contract_proxy = MappingProxyType(dict(sorted(contracts.items())))
    unsigned = CompletedOiEvidence(
        source=FIXED_HISTORICAL_60M_SOURCE,
        trading_day=day,
        expected_contracts=expected_proxy,
        received_contracts=tuple(sorted(contracts)),
        missing_contracts=(),
        contracts=contract_proxy,
        dominant_symbols=MappingProxyType(dominant),
        flows=MappingProxyType(flows),
        complete=True,
        issues=(),
        evidence_digest="",
    )
    return replace(
        unsigned,
        evidence_digest=canonical_stress90_digest(_completed_unsigned_payload(unsigned)),
    )


def _in_progress_payload(evidence: InProgressOiEvidence | None) -> dict[str, object] | None:
    if evidence is None:
        return None
    return {
        "trading_day": evidence.trading_day,
        "expected_contracts": _expected_payload(evidence.expected_contracts),
        "contracts": {
            symbol: _contract_payload(contract)
            for symbol, contract in sorted(evidence.contracts.items())
        },
        "issues": list(evidence.issues),
    }


def _state_payload(state: Stress90OiEvidenceState) -> dict[str, object]:
    return {
        "completed": [_completed_payload(item) for item in state.completed],
        "in_progress": _in_progress_payload(state.in_progress),
        "observed_transitions": [
            {
                "source_trading_day": item.source_trading_day,
                "target_trading_day": item.target_trading_day,
                "completed_oi_evidence_digest": item.completed_oi_evidence_digest,
            }
            for item in state.observed_transitions
        ],
        "raw_ticks_observed": state.raw_ticks_observed,
        "duplicate_ticks": state.duplicate_ticks,
        "volume_resets": state.volume_resets,
    }


_BAR_FIELDS = {
    "session",
    "bucket_start",
    "bucket_end",
    "first_open",
    "last_close",
    "first_hold",
    "last_hold",
    "total_volume",
    "first_tick_timestamp",
    "last_tick_timestamp",
}
_CONTRACT_FIELDS = {
    "symbol",
    "exchange",
    "product",
    "trading_day",
    "first_open",
    "last_close",
    "first_hold",
    "last_hold",
    "total_volume",
    "first_tick_timestamp",
    "last_tick_timestamp",
    "last_cumulative_volume",
    "bars",
    "raw_tick_count",
    "duplicate_tick_count",
    "volume_reset_count",
    "complete",
    "issues",
}
_COMPLETED_FIELDS = {
    "source",
    "trading_day",
    "expected_contracts",
    "received_contracts",
    "missing_contracts",
    "contracts",
    "dominant_symbols",
    "flows",
    "complete",
    "issues",
    "evidence_digest",
}


def _string_tuple(raw: object, *, name: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise OiEvidenceIntegrityError(f"{name} must be a string list")
    return tuple(raw)


def _expected_from_payload(raw: object) -> Mapping[str, tuple[str, ...]]:
    if not isinstance(raw, Mapping) or set(raw) != set(STRESS90_POLICY.oi_products):
        raise OiEvidenceIntegrityError("OI expected product coverage fields are invalid")
    result: dict[str, tuple[str, ...]] = {}
    seen: set[str] = set()
    for product in STRESS90_POLICY.oi_products:
        symbols = _string_tuple(raw[product], name=f"OI expected contracts {product}")
        if tuple(sorted(symbols)) != symbols or not symbols:
            raise OiEvidenceIntegrityError(
                "OI expected contract lists must be non-empty and sorted"
            )
        if any(not symbol or symbol in seen for symbol in symbols):
            raise OiEvidenceIntegrityError("OI expected contract identity is invalid")
        seen.update(symbols)
        result[product] = symbols
    if len(seen) > MAX_EXPECTED_CONTRACTS:
        raise OiEvidenceIntegrityError("OI expected contract universe exceeds memory bound")
    return MappingProxyType(result)


def _bar_from_payload(raw: object) -> OiBarEvidence:
    if not isinstance(raw, Mapping) or set(raw) != _BAR_FIELDS:
        raise OiEvidenceIntegrityError("OI bar fields are invalid")
    start = _valid_datetime(raw["bucket_start"], name="OI bar start")
    end = _valid_datetime(raw["bucket_end"], name="OI bar end")
    first_tick = _valid_datetime(raw["first_tick_timestamp"], name="OI bar first tick")
    last_tick = _valid_datetime(raw["last_tick_timestamp"], name="OI bar last tick")
    if not start < end or not start <= first_tick <= last_tick <= end:
        raise OiEvidenceIntegrityError("OI bar time ordering is invalid")
    session = raw["session"]
    if not isinstance(session, str) or not session:
        raise OiEvidenceIntegrityError("OI bar session is invalid")
    return OiBarEvidence(
        session=session,
        bucket_start=start,
        bucket_end=end,
        first_open=_finite_positive(raw["first_open"], name="OI bar first open"),
        last_close=_finite_positive(raw["last_close"], name="OI bar last close"),
        first_hold=_finite_nonnegative(raw["first_hold"], name="OI bar first hold"),
        last_hold=_finite_nonnegative(raw["last_hold"], name="OI bar last hold"),
        total_volume=_finite_nonnegative(raw["total_volume"], name="OI bar volume"),
        first_tick_timestamp=first_tick,
        last_tick_timestamp=last_tick,
    )


def _contract_from_payload(raw: object) -> ContractOiEvidence:
    if not isinstance(raw, Mapping) or set(raw) != _CONTRACT_FIELDS:
        raise OiEvidenceIntegrityError("OI contract evidence fields are invalid")
    symbol = raw["symbol"]
    exchange = raw["exchange"]
    product = raw["product"]
    if any(not isinstance(value, str) or not value for value in (symbol, exchange, product)):
        raise OiEvidenceIntegrityError("OI contract identity is invalid")
    product = product.upper()
    if product not in STRESS90_POLICY.oi_products:
        raise OiEvidenceIntegrityError("OI contract product is unsupported")
    if exchange.upper() != PRODUCT_SESSION_MANIFEST[product].exchange:
        raise OiEvidenceIntegrityError("OI contract exchange identity mismatch")
    bars_raw = raw["bars"]
    if not isinstance(bars_raw, list) or not 0 < len(bars_raw) <= MAX_BARS_PER_CONTRACT:
        raise OiEvidenceIntegrityError("OI contract bar count is invalid")
    bars = tuple(_bar_from_payload(item) for item in bars_raw)
    if tuple(sorted(bars, key=lambda item: item.bucket_start)) != bars:
        raise OiEvidenceIntegrityError("OI contract bars are out of order")
    first_tick = _valid_datetime(raw["first_tick_timestamp"], name="OI contract first tick")
    last_tick = _valid_datetime(raw["last_tick_timestamp"], name="OI contract last tick")
    if first_tick != bars[0].first_tick_timestamp or last_tick != bars[-1].last_tick_timestamp:
        raise OiEvidenceIntegrityError("OI contract tick/bar boundary mismatch")
    issues = _string_tuple(raw["issues"], name="OI contract issues")
    complete = raw["complete"]
    if not isinstance(complete, bool):
        raise OiEvidenceIntegrityError("OI contract completeness must be bool")
    contract = ContractOiEvidence(
        symbol=symbol,
        exchange=exchange.upper(),
        product=product,
        trading_day=_valid_day(raw["trading_day"], name="OI contract trading day"),
        first_open=_finite_positive(raw["first_open"], name="OI contract first open"),
        last_close=_finite_positive(raw["last_close"], name="OI contract last close"),
        first_hold=_finite_nonnegative(raw["first_hold"], name="OI contract first hold"),
        last_hold=_finite_nonnegative(raw["last_hold"], name="OI contract last hold"),
        total_volume=_finite_nonnegative(raw["total_volume"], name="OI contract volume"),
        first_tick_timestamp=first_tick,
        last_tick_timestamp=last_tick,
        last_cumulative_volume=_finite_nonnegative(
            raw["last_cumulative_volume"], name="OI contract cumulative volume"
        ),
        bars=bars,
        raw_tick_count=_valid_count(raw["raw_tick_count"], name="OI contract tick count"),
        duplicate_tick_count=_valid_count(
            raw["duplicate_tick_count"], name="OI contract duplicate count"
        ),
        volume_reset_count=_valid_count(raw["volume_reset_count"], name="OI contract reset count"),
        complete=complete,
        issues=issues,
    )
    if contract.raw_tick_count < 1 + contract.duplicate_tick_count:
        raise OiEvidenceIntegrityError("OI contract tick counters are inconsistent")
    if abs(sum(bar.total_volume for bar in bars) - contract.total_volume) > 1e-9:
        raise OiEvidenceIntegrityError("OI contract bar volume does not reconcile")
    return contract


def _contracts_from_payload(raw: object) -> Mapping[str, ContractOiEvidence]:
    if not isinstance(raw, Mapping) or len(raw) > MAX_EXPECTED_CONTRACTS:
        raise OiEvidenceIntegrityError("OI contract evidence map is invalid")
    result: dict[str, ContractOiEvidence] = {}
    for symbol, item in raw.items():
        if not isinstance(symbol, str):
            raise OiEvidenceIntegrityError("OI contract map key is invalid")
        contract = _contract_from_payload(item)
        if symbol != contract.symbol:
            raise OiEvidenceIntegrityError("OI contract map identity mismatch")
        result[symbol] = contract
    return MappingProxyType(dict(sorted(result.items())))


def _completed_from_payload(raw: object) -> CompletedOiEvidence:
    if not isinstance(raw, Mapping) or set(raw) != _COMPLETED_FIELDS:
        raise OiEvidenceIntegrityError("completed OI evidence fields are invalid")
    expected = _expected_from_payload(raw["expected_contracts"])
    contracts = _contracts_from_payload(raw["contracts"])
    dominant_raw = raw["dominant_symbols"]
    flows_raw = raw["flows"]
    if (
        not isinstance(dominant_raw, Mapping)
        or set(dominant_raw) != set(STRESS90_POLICY.oi_products)
        or not isinstance(flows_raw, Mapping)
        or set(flows_raw) != set(STRESS90_POLICY.oi_products)
    ):
        raise OiEvidenceIntegrityError("completed OI product result fields are invalid")
    dominant: dict[str, str | None] = {}
    flows: dict[str, int | None] = {}
    for product in STRESS90_POLICY.oi_products:
        symbol = dominant_raw[product]
        if symbol is not None and (not isinstance(symbol, str) or symbol not in contracts):
            raise OiEvidenceIntegrityError("completed OI dominant symbol is invalid")
        flow = flows_raw[product]
        if isinstance(flow, bool) or flow not in (-1, 0, 1, None):
            raise OiEvidenceIntegrityError("completed OI flow is invalid")
        dominant[product] = symbol
        flows[product] = flow
    complete = raw["complete"]
    if not isinstance(complete, bool):
        raise OiEvidenceIntegrityError("completed OI completeness must be bool")
    evidence = CompletedOiEvidence(
        source=str(raw["source"]),
        trading_day=_valid_day(raw["trading_day"], name="completed OI trading day"),
        expected_contracts=expected,
        received_contracts=_string_tuple(raw["received_contracts"], name="received OI contracts"),
        missing_contracts=_string_tuple(raw["missing_contracts"], name="missing OI contracts"),
        contracts=contracts,
        dominant_symbols=MappingProxyType(dominant),
        flows=MappingProxyType(flows),
        complete=complete,
        issues=_string_tuple(raw["issues"], name="completed OI issues"),
        evidence_digest=str(raw["evidence_digest"]),
    )
    if evidence.source not in {CTP_RAW_TICK_SOURCE, FIXED_HISTORICAL_60M_SOURCE}:
        raise OiEvidenceIntegrityError("completed OI source identity is invalid")
    if evidence.received_contracts != tuple(sorted(contracts)):
        raise OiEvidenceIntegrityError("completed OI received-contract identity mismatch")
    expected_symbols = _all_expected_symbols(expected)
    if evidence.missing_contracts != tuple(sorted(expected_symbols - set(contracts))):
        raise OiEvidenceIntegrityError("completed OI missing-contract identity mismatch")
    expected_digest = canonical_stress90_digest(_completed_unsigned_payload(evidence))
    if evidence.evidence_digest != expected_digest:
        raise OiEvidenceIntegrityError("completed OI evidence digest mismatch")
    if evidence.complete != all(value is not None for value in evidence.flows.values()):
        raise OiEvidenceIntegrityError("completed OI completeness/flow mismatch")
    if evidence.source == FIXED_HISTORICAL_60M_SOURCE and (
        not evidence.complete
        or evidence.missing_contracts
        or set(contracts) != expected_symbols
        or any(not contract.complete for contract in contracts.values())
    ):
        raise OiEvidenceIntegrityError("fixed historical 60m OI evidence is incomplete")
    return evidence


def _in_progress_from_payload(raw: object) -> InProgressOiEvidence | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or set(raw) != {
        "trading_day",
        "expected_contracts",
        "contracts",
        "issues",
    }:
        raise OiEvidenceIntegrityError("in-progress OI evidence fields are invalid")
    expected = _expected_from_payload(raw["expected_contracts"])
    contracts = _contracts_from_payload(raw["contracts"])
    if not set(contracts).issubset(_all_expected_symbols(expected)):
        raise OiEvidenceIntegrityError("in-progress OI evidence contains unexpected contracts")
    return InProgressOiEvidence(
        trading_day=_valid_day(raw["trading_day"], name="in-progress OI trading day"),
        expected_contracts=expected,
        contracts=contracts,
        issues=_string_tuple(raw["issues"], name="in-progress OI issues"),
    )


def _state_from_payload(raw: object) -> Stress90OiEvidenceState:
    if not isinstance(raw, Mapping) or set(raw) != {
        "completed",
        "in_progress",
        "observed_transitions",
        "raw_ticks_observed",
        "duplicate_ticks",
        "volume_resets",
    }:
        raise OiEvidenceIntegrityError("OI evidence state fields are invalid")
    completed_raw = raw["completed"]
    if not isinstance(completed_raw, list) or len(completed_raw) > MAX_COMPLETED_RETENTION_DAYS:
        raise OiEvidenceIntegrityError("completed OI evidence history is invalid")
    completed = tuple(_completed_from_payload(item) for item in completed_raw)
    days = tuple(item.trading_day for item in completed)
    if days != tuple(sorted(set(days))):
        raise OiEvidenceIntegrityError("completed OI evidence days are not unique/increasing")
    in_progress = _in_progress_from_payload(raw["in_progress"])
    if in_progress is not None and days and in_progress.trading_day <= days[-1]:
        raise OiEvidenceIntegrityError("in-progress OI day must follow completed evidence")
    transitions_raw = raw["observed_transitions"]
    if not isinstance(transitions_raw, list) or len(transitions_raw) > (
        MAX_COMPLETED_RETENTION_DAYS
    ):
        raise OiEvidenceIntegrityError("observed CTP day transition history is invalid")
    completed_by_day = {item.trading_day: item for item in completed}
    transitions: list[ObservedTradingDayTransition] = []
    for item in transitions_raw:
        fields = {
            "source_trading_day",
            "target_trading_day",
            "completed_oi_evidence_digest",
        }
        if not isinstance(item, Mapping) or set(item) != fields:
            raise OiEvidenceIntegrityError("observed CTP day transition fields are invalid")
        source = _valid_day(
            item["source_trading_day"],
            name="observed source CTP trading day",
        )
        target = _valid_day(
            item["target_trading_day"],
            name="observed target CTP trading day",
        )
        digest = str(item["completed_oi_evidence_digest"])
        completed_source = completed_by_day.get(source)
        if (
            source >= target
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or completed_source is None
            or completed_source.evidence_digest != digest
        ):
            raise OiEvidenceIntegrityError("observed CTP day transition identity is invalid")
        transitions.append(ObservedTradingDayTransition(source, target, digest))
    transition_keys = tuple(
        (item.source_trading_day, item.target_trading_day) for item in transitions
    )
    if transition_keys != tuple(sorted(set(transition_keys))):
        raise OiEvidenceIntegrityError("observed CTP day transitions are not unique/increasing")
    state = Stress90OiEvidenceState(
        completed=completed,
        in_progress=in_progress,
        observed_transitions=tuple(transitions),
        raw_ticks_observed=_valid_count(raw["raw_ticks_observed"], name="raw OI tick count"),
        duplicate_ticks=_valid_count(raw["duplicate_ticks"], name="duplicate OI tick count"),
        volume_resets=_valid_count(raw["volume_resets"], name="OI volume reset count"),
    )
    if state.duplicate_ticks > state.raw_ticks_observed:
        raise OiEvidenceIntegrityError("OI evidence counters are inconsistent")
    return state


class Stress90OiEvidenceStore:
    """Checksummed atomic sequence store; ``.prev`` is evidence, never fallback."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @property
    def previous_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.prev")

    def load_record(self) -> Stress90OiEvidenceRecord | None:
        if not self.path.exists():
            if self.previous_path.exists():
                raise OiEvidenceIntegrityError(
                    "current Stress-90 OI evidence is missing while previous evidence exists"
                )
            return None
        return self._read(self.path)

    def load_required_record(self) -> Stress90OiEvidenceRecord:
        record = self.load_record()
        if record is None:
            raise OiEvidenceIntegrityError("required Stress-90 OI evidence is missing")
        return record

    def load_previous_record(self) -> Stress90OiEvidenceRecord:
        if not self.previous_path.exists():
            raise OiEvidenceIntegrityError("previous Stress-90 OI evidence is missing")
        return self._read(self.previous_path)

    def _read(self, path: Path) -> Stress90OiEvidenceRecord:
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OiEvidenceIntegrityError("invalid OI evidence UTF-8") from exc
        try:
            raw = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
        except json.JSONDecodeError as exc:
            raise OiEvidenceIntegrityError("invalid OI evidence JSON") from exc
        fields = {"kind", "schema_version", "sequence", "state", "checksum"}
        if not isinstance(raw, Mapping) or set(raw) != fields:
            raise OiEvidenceIntegrityError("OI evidence envelope fields are invalid")
        if raw["kind"] != OI_EVIDENCE_KIND:
            raise OiEvidenceIntegrityError("OI evidence kind is invalid")
        if raw["schema_version"] != OI_EVIDENCE_SCHEMA_VERSION:
            raise OiEvidenceIntegrityError("OI evidence schema is unsupported")
        sequence = raw["sequence"]
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise OiEvidenceIntegrityError("OI evidence sequence must be positive")
        checksum = raw["checksum"]
        unsigned = {key: value for key, value in raw.items() if key != "checksum"}
        if not isinstance(checksum, str) or checksum != _checksum(unsigned):
            raise OiEvidenceIntegrityError("OI evidence checksum mismatch")
        return Stress90OiEvidenceRecord(
            state=_state_from_payload(raw["state"]),
            sequence=sequence,
            checksum=checksum,
        )

    def save_state(
        self,
        state: Stress90OiEvidenceState,
        *,
        expected_sequence: int | None = None,
    ) -> Stress90OiEvidenceRecord:
        # Round-trip validation makes every persisted object obey the same strict parser.
        payload = _state_payload(state)
        validated = _state_from_payload(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        current = self.load_record()
        current_sequence = 0 if current is None else current.sequence
        if expected_sequence is not None and expected_sequence != current_sequence:
            raise OiEvidenceIntegrityError("OI evidence sequence changed concurrently")
        unsigned: dict[str, object] = {
            "kind": OI_EVIDENCE_KIND,
            "schema_version": OI_EVIDENCE_SCHEMA_VERSION,
            "sequence": current_sequence + 1,
            "state": _state_payload(validated),
        }
        checksum = _checksum(unsigned)
        encoded = json.dumps(
            {**unsigned, "checksum": checksum},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        if current is not None:
            self._atomic_replace(self.previous_path, self.path.read_bytes())
        self._atomic_replace(self.path, encoded)
        return Stress90OiEvidenceRecord(validated, current_sequence + 1, checksum)

    @staticmethod
    def _atomic_replace(target: Path, payload: bytes) -> None:
        temporary: Path | None = None
        try:
            with NamedTemporaryFile("wb", dir=target.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(target)
            directory_descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()


def _append_issue(issues: tuple[str, ...], issue: str) -> tuple[str, ...]:
    if issue in issues:
        return issues
    if len(issues) < MAX_OI_ISSUES:
        return (*issues, issue)
    marker = "issue_memory_bound_exceeded"
    if marker in issues:
        return issues
    return (*issues[:-1], marker)


def _product_hint_from_futures_symbol(symbol: str) -> str | None:
    match = re.fullmatch(r"([A-Z]{1,4})\d{3,4}", str(symbol).upper())
    return None if match is None else match.group(1)


def _new_bar(
    tick: Tick,
    bucket,
    volume_delta: float,
    *,
    first_open: float | None = None,
) -> OiBarEvidence:
    return OiBarEvidence(
        session=bucket.session,
        bucket_start=bucket.bucket_start,
        bucket_end=bucket.bucket_end,
        first_open=float(tick.last_price if first_open is None else first_open),
        last_close=float(tick.last_price),
        first_hold=float(tick.open_interest),
        last_hold=float(tick.open_interest),
        total_volume=float(volume_delta),
        first_tick_timestamp=tick.timestamp,
        last_tick_timestamp=tick.timestamp,
    )


def _update_bar(bar: OiBarEvidence, tick: Tick, volume_delta: float) -> OiBarEvidence:
    return replace(
        bar,
        last_close=float(tick.last_price),
        last_hold=float(tick.open_interest),
        total_volume=float(bar.total_volume + volume_delta),
        last_tick_timestamp=tick.timestamp,
    )


def _contract_boundary_complete(contract: ContractOiEvidence) -> tuple[bool, tuple[str, ...]]:
    first_bucket = session_bucket_for_tick(
        contract.product, contract.first_tick_timestamp, contract.trading_day
    )
    last_bucket = session_bucket_for_tick(
        contract.product, contract.last_tick_timestamp, contract.trading_day
    )
    issues = contract.issues
    first_session = PRODUCT_SESSION_MANIFEST[contract.product].sessions[0]
    if (
        first_bucket is None
        or first_bucket.session != first_session
        or (
            contract.first_tick_timestamp.astimezone(first_bucket.session_start.tzinfo)
            - first_bucket.session_start
        ).total_seconds()
        > _BOUNDARY_GRACE_SECONDS
    ):
        issues = _append_issue(issues, "opening_session_boundary_missing")
    final_session = PRODUCT_SESSION_MANIFEST[contract.product].sessions[-1]
    if (
        last_bucket is None
        or last_bucket.session != final_session
        or (
            last_bucket.session_end
            - contract.last_tick_timestamp.astimezone(last_bucket.session_end.tzinfo)
        ).total_seconds()
        > _BOUNDARY_GRACE_SECONDS
    ):
        issues = _append_issue(issues, "closing_session_boundary_missing")
    return not issues, issues


def _finalize_day(in_progress: InProgressOiEvidence) -> CompletedOiEvidence:
    expected_symbols = _all_expected_symbols(in_progress.expected_contracts)
    received = set(in_progress.contracts)
    missing = tuple(sorted(expected_symbols - received))
    contracts: dict[str, ContractOiEvidence] = {}
    for symbol, raw in in_progress.contracts.items():
        complete, issues = _contract_boundary_complete(raw)
        # The frozen batch candidate defines ``first_hold`` as the hold at the
        # end of the first completed 60m bar, not the session-open raw Tick.
        # Normalize the contract summary from completed bar mechanics so live
        # flow is byte-for-byte comparable with the historical bridge.
        contracts[symbol] = replace(
            raw,
            first_open=raw.bars[0].first_open,
            first_hold=raw.bars[0].last_hold,
            last_close=raw.bars[-1].last_close,
            last_hold=raw.bars[-1].last_hold,
            complete=complete,
            issues=issues,
        )

    dominant: dict[str, str | None] = {}
    flows: dict[str, int | None] = {}
    for product in STRESS90_POLICY.oi_products:
        symbols = in_progress.expected_contracts[product]
        product_complete = all(
            symbol in contracts and contracts[symbol].complete for symbol in symbols
        )
        if not product_complete or in_progress.issues:
            dominant[product] = None
            flows[product] = None
            continue
        rows = [contracts[symbol] for symbol in symbols]
        rows.sort(key=lambda item: (-item.last_hold, -item.total_volume, item.symbol))
        selected = rows[0]
        dominant[product] = selected.symbol
        if selected.last_hold <= selected.first_hold or selected.last_close == selected.first_open:
            flows[product] = 0
        else:
            flows[product] = 1 if selected.last_close > selected.first_open else -1
    complete = all(value is not None for value in flows.values())
    unsigned = CompletedOiEvidence(
        source=CTP_RAW_TICK_SOURCE,
        trading_day=in_progress.trading_day,
        expected_contracts=in_progress.expected_contracts,
        received_contracts=tuple(sorted(received)),
        missing_contracts=missing,
        contracts=MappingProxyType(dict(sorted(contracts.items()))),
        dominant_symbols=MappingProxyType(dominant),
        flows=MappingProxyType(flows),
        complete=complete,
        issues=in_progress.issues,
        evidence_digest="",
    )
    return replace(
        unsigned,
        evidence_digest=canonical_stress90_digest(_completed_unsigned_payload(unsigned)),
    )


class Stress90OiEvidenceAggregator:
    """Lock-bounded raw observer with explicit post-batch persistence."""

    def __init__(
        self,
        *,
        store: Stress90OiEvidenceStore | None = None,
        completed_retention_days: int = DEFAULT_COMPLETED_RETENTION_DAYS,
    ) -> None:
        if (
            isinstance(completed_retention_days, bool)
            or not isinstance(completed_retention_days, int)
            or not 1 <= completed_retention_days <= MAX_COMPLETED_RETENTION_DAYS
        ):
            raise ValueError("completed OI retention days are invalid")
        self.store = store
        self.completed_retention_days = completed_retention_days
        record = store.load_record() if store is not None else None
        state = record.state if record is not None else Stress90OiEvidenceState()
        self._sequence = 0 if record is None else record.sequence
        self._completed = list(state.completed)
        self._observed_transitions = list(state.observed_transitions)
        self._in_progress = state.in_progress
        self._in_progress_observed_in_process = False
        # Process-local connection proof is deliberately not restored.  A restart,
        # disconnect, or MD login generation change must break day continuity even
        # when the persisted in-progress bars themselves remain usable evidence.
        self._raw_market_connected = False
        self._raw_market_connection_generation: int | None = None
        self._in_progress_connection_generation: int | None = None
        self._pending_observed_transition: (
            tuple[
                ObservedTradingDayTransition,
                int,
            ]
            | None
        ) = None
        self._contracts: dict[str, ContractOiEvidence] = (
            {} if self._in_progress is None else dict(self._in_progress.contracts)
        )
        self._expected_symbols: frozenset[str] = (
            frozenset()
            if self._in_progress is None
            else frozenset(_all_expected_symbols(self._in_progress.expected_contracts))
        )
        if self._in_progress is not None:
            self._in_progress = replace(self._in_progress, contracts=self._contracts)
        self._raw_ticks_observed = state.raw_ticks_observed
        self._duplicate_ticks = state.duplicate_ticks
        self._volume_resets = state.volume_resets
        self._contract_catalog: tuple[ContractInfo, ...] = ()
        self._dirty = False
        self._generation = 0
        self._lock = Lock()

    @property
    def in_progress_complete(self) -> bool:
        with self._lock:
            if self._in_progress is None:
                return False
            return _finalize_day(self._in_progress).complete

    def counters(self) -> dict[str, int]:
        with self._lock:
            return {
                "raw_ticks_observed": self._raw_ticks_observed,
                "duplicate_ticks": self._duplicate_ticks,
                "volume_resets": self._volume_resets,
            }

    def completed_evidence(self, trading_day: str) -> CompletedOiEvidence:
        day = _valid_day(trading_day, name="completed OI lookup day")
        with self._lock:
            for evidence in self._completed:
                if evidence.trading_day == day:
                    return evidence
        raise OiEvidenceIntegrityError(f"completed OI evidence is unavailable: {day}")

    def in_progress_contract(self, symbol: str) -> ContractOiEvidence:
        with self._lock:
            if self._in_progress is None or symbol not in self._contracts:
                raise OiEvidenceIntegrityError(f"in-progress OI contract is unavailable: {symbol}")
            return self._contracts[symbol]

    def in_progress_issues(self) -> tuple[str, ...]:
        with self._lock:
            return () if self._in_progress is None else self._in_progress.issues

    def _changed(self) -> None:
        self._dirty = True
        self._generation += 1

    def _fixed_bridge_covers_day_unlocked(self, day: str) -> bool:
        return bool(
            self._in_progress is None
            and self._completed
            and day == self._completed[-1].trading_day
            and self._completed[-1].source == FIXED_HISTORICAL_60M_SOURCE
        )

    def _rollover_unlocked(
        self,
        day: str,
        expected: Mapping[str, tuple[str, ...]],
        *,
        observed_target_raw_tick: bool = False,
    ) -> None:
        if self._in_progress is not None:
            if day < self._in_progress.trading_day:
                raise OiEvidenceIntegrityError("OI trading day cannot move backward or reopen")
            if day == self._in_progress.trading_day:
                if self._in_progress.expected_contracts != expected:
                    raise OiEvidenceIntegrityError(
                        "OI expected universe changed during trading day"
                    )
                return
            completed = _finalize_day(self._in_progress)
            self._completed.append(completed)
            self._completed = self._completed[-self.completed_retention_days :]
            source_generation = self._in_progress_connection_generation
            natural_days = (
                datetime.strptime(day, "%Y%m%d")
                - datetime.strptime(completed.trading_day, "%Y%m%d")
            ).days
            uninterrupted = bool(
                self._in_progress_observed_in_process
                and self._raw_market_connected
                and source_generation is not None
                and source_generation == self._raw_market_connection_generation
                # A raw MD socket cannot prove that an entire intervening CTP
                # trading day was not silently missed.  Non-adjacent civil dates
                # therefore require a separate authoritative session manifest;
                # until that authority is wired, fail closed rather than guessing
                # weekends or exchange holidays.
                and natural_days == 1
            )
            if uninterrupted:
                if source_generation is None:
                    raise OiEvidenceIntegrityError(
                        "OI observed transition source generation is missing"
                    )
                transition = ObservedTradingDayTransition(
                    source_trading_day=completed.trading_day,
                    target_trading_day=day,
                    completed_oi_evidence_digest=completed.evidence_digest,
                )
                if observed_target_raw_tick:
                    self._observed_transitions.append(transition)
                else:
                    self._pending_observed_transition = (transition, source_generation)
            else:
                self._pending_observed_transition = None
            retained_days = {item.trading_day for item in self._completed}
            self._observed_transitions = [
                item
                for item in self._observed_transitions
                if item.source_trading_day in retained_days
            ]
        elif self._completed and day <= self._completed[-1].trading_day:
            raise OiEvidenceIntegrityError("OI trading day cannot move backward or reopen")
        self._in_progress = InProgressOiEvidence(
            trading_day=day,
            expected_contracts=expected,
            contracts={},
            issues=(),
        )
        self._contracts = {}
        self._in_progress = replace(self._in_progress, contracts=self._contracts)
        self._expected_symbols = frozenset(_all_expected_symbols(expected))
        self._in_progress_observed_in_process = False
        self._in_progress_connection_generation = None
        self._changed()

    def note_raw_market_connection(
        self,
        *,
        connected: bool,
        generation: int,
    ) -> None:
        """Record a callback-safe CTP/Sim market-stream generation boundary.

        The generation is process-local authority supplied by the Broker.  It is
        never persisted, so neither restart nor a stale evidence file can fabricate
        uninterrupted market coverage.
        """

        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise OiEvidenceIntegrityError("raw market connection generation is invalid")
        if not isinstance(connected, bool):
            raise OiEvidenceIntegrityError("raw market connection state is invalid")
        with self._lock:
            previous = self._raw_market_connection_generation
            if previous is not None and generation < previous:
                raise OiEvidenceIntegrityError("raw market connection generation moved backward")
            changed_generation = previous is not None and generation != previous
            interrupted = bool(self._raw_market_connected and (not connected or changed_generation))
            if interrupted and self._in_progress_observed_in_process:
                self._mark_day_issue_unlocked("raw_market_connection_interrupted")
            if interrupted or not connected:
                self._pending_observed_transition = None
            self._raw_market_connection_generation = generation
            self._raw_market_connected = connected

    def _confirm_pending_transition_unlocked(self, day: str) -> None:
        pending = self._pending_observed_transition
        if pending is None:
            return
        transition, generation = pending
        self._pending_observed_transition = None
        if (
            transition.target_trading_day == day
            and self._raw_market_connected
            and self._raw_market_connection_generation == generation
        ):
            self._observed_transitions.append(transition)

    def set_expected_contracts(
        self,
        trading_day: str,
        contracts: Sequence[ContractInfo],
    ) -> tuple[str, ...]:
        day = _valid_day(trading_day, name="expected OI trading day")
        catalog = tuple(contracts)
        expected = _expected_contracts(catalog, trading_day=day)
        expected_symbols = tuple(sorted(_all_expected_symbols(expected)))
        with self._lock:
            if self._fixed_bridge_covers_day_unlocked(day):
                self._contract_catalog = catalog
                return expected_symbols
            self._rollover_unlocked(day, expected)
            self._contract_catalog = catalog
        return expected_symbols

    def refresh_contract_catalog(
        self,
        trading_day: str,
        contracts: Sequence[ContractInfo],
    ) -> None:
        """Arm a refreshed in-memory catalog without hiding an intra-day universe change."""

        day = _valid_day(trading_day, name="refreshed OI trading day")
        catalog = tuple(contracts)
        expected = _expected_contracts(catalog, trading_day=day)
        with self._lock:
            if self._fixed_bridge_covers_day_unlocked(day):
                self._contract_catalog = catalog
                return
            if self._in_progress is None or day != self._in_progress.trading_day:
                self._rollover_unlocked(day, expected)
            elif self._in_progress.expected_contracts != expected:
                issues = self._in_progress.issues
                if self._in_progress.contracts:
                    issues = _append_issue(
                        issues,
                        "expected_universe_changed_after_raw_ticks",
                    )
                self._in_progress = replace(
                    self._in_progress,
                    expected_contracts=expected,
                    issues=issues,
                )
                self._expected_symbols = frozenset(_all_expected_symbols(expected))
                self._changed()
            self._contract_catalog = catalog

    def note_contract_subscriptions(
        self,
        trading_day: str,
        symbols: Sequence[str],
    ) -> None:
        """Fail the day closed when a required subscription starts after evidence began."""

        day = _valid_day(trading_day, name="OI subscription trading day")
        normalized = tuple(sorted({str(symbol).upper() for symbol in symbols if str(symbol)}))
        if not normalized:
            return
        with self._lock:
            if self._in_progress is None or self._in_progress.trading_day != day:
                raise OiEvidenceIntegrityError(
                    "OI contract subscription day is not the armed trading day"
                )
            expected = _all_expected_symbols(self._in_progress.expected_contracts)
            if not set(normalized).issubset(expected):
                raise OiEvidenceIntegrityError(
                    "OI contract subscription is outside the expected universe"
                )
            if self._in_progress.contracts:
                issue = "late_contract_subscription:" + ",".join(normalized)
                self._in_progress = replace(
                    self._in_progress,
                    issues=_append_issue(self._in_progress.issues, issue),
                )
                self._changed()

    def _mark_day_issue_unlocked(self, issue: str) -> None:
        if self._in_progress is None:
            return
        self._in_progress = replace(
            self._in_progress,
            issues=_append_issue(self._in_progress.issues, issue),
        )
        self._changed()

    def _mark_contract_issue_unlocked(
        self,
        contract: ContractOiEvidence,
        issue: str,
    ) -> None:
        if self._in_progress is None:
            return
        self._contracts[contract.symbol] = replace(
            contract,
            issues=_append_issue(contract.issues, issue),
            complete=False,
        )
        self._changed()

    def observe_raw_tick(self, tick: Tick, contract: ContractInfo | None) -> None:
        """Validate and aggregate one raw tick without any file or network operation."""

        with self._lock:
            self._raw_ticks_observed += 1
            self._changed()
            try:
                tick.validate()
                day = _valid_day(tick.trading_day, name="raw OI trading day")
            except Exception as exc:
                self._mark_day_issue_unlocked("invalid_raw_tick")
                raise OiEvidenceIntegrityError(f"invalid raw OI tick: {exc}") from exc
            if contract is None:
                product_hint = _product_hint_from_futures_symbol(tick.symbol)
                if product_hint is not None and product_hint not in STRESS90_POLICY.oi_products:
                    return
                self._mark_day_issue_unlocked(f"contract_metadata_missing:{tick.symbol}")
                raise OiEvidenceIntegrityError(f"raw OI contract metadata missing: {tick.symbol}")
            product = str(contract.product).upper()
            if (
                tick.symbol.upper() != str(contract.symbol).upper()
                or tick.exchange.upper() != str(contract.exchange).upper()
            ):
                self._mark_day_issue_unlocked(f"contract_identity_mismatch:{tick.symbol}")
                raise OiEvidenceIntegrityError("raw OI tick/contract identity mismatch")
            if product not in STRESS90_POLICY.oi_products:
                return
            if tick.source_trading_day_verified is not True or (
                tick.source_trading_day and tick.source_trading_day != day
            ):
                self._mark_day_issue_unlocked("source_trading_day_unverified")
                raise OiEvidenceIntegrityError("raw OI source trading day is unverified")
            if tick.exchange.upper() != PRODUCT_SESSION_MANIFEST[product].exchange:
                self._mark_day_issue_unlocked(f"contract_identity_mismatch:{tick.symbol}")
                raise OiEvidenceIntegrityError("raw OI tick/contract identity mismatch")
            if self._in_progress is None:
                if (
                    self._completed
                    and day > self._completed[-1].trading_day
                    and self._contract_catalog
                ):
                    expected = _expected_contracts(self._contract_catalog, trading_day=day)
                    self._rollover_unlocked(day, expected, observed_target_raw_tick=True)
                else:
                    self._mark_day_issue_unlocked("expected_contract_universe_missing")
                    raise OiEvidenceIntegrityError("raw OI expected contract universe is missing")
            in_progress = self._in_progress
            if in_progress is None:  # pragma: no cover - rollover contract
                raise OiEvidenceIntegrityError("raw OI expected contract universe is missing")
            if day != in_progress.trading_day:
                if day < in_progress.trading_day:
                    # A prior day may already be consumed by the next target decision.
                    # Never revise it and never poison the current, otherwise-valid day.
                    raise RawMarketEvidenceFatalError(
                        "late raw Tick would revise a completed OI day"
                    )
                if not self._contract_catalog:
                    self._mark_day_issue_unlocked("expected_contract_catalog_missing")
                    raise OiEvidenceIntegrityError("raw OI expected contract catalog is missing")
                expected = _expected_contracts(self._contract_catalog, trading_day=day)
                self._rollover_unlocked(day, expected, observed_target_raw_tick=True)
            symbol = str(contract.symbol).upper()
            if symbol not in self._expected_symbols:
                self._mark_day_issue_unlocked(f"unexpected_contract:{symbol}")
                raise OiEvidenceIntegrityError(
                    f"raw OI contract is outside expected universe: {symbol}"
                )
            bucket = session_bucket_for_tick(product, tick.timestamp, day)
            if bucket is None:
                self._mark_day_issue_unlocked(f"outside_fixed_session:{symbol}")
                raise OiEvidenceIntegrityError(f"raw OI tick outside fixed session: {symbol}")
            self._confirm_pending_transition_unlocked(day)
            self._in_progress_observed_in_process = True
            if self._raw_market_connected:
                generation = self._raw_market_connection_generation
                if generation is None:  # pragma: no cover - protected by connection setter
                    raise OiEvidenceIntegrityError(
                        "raw market connection generation is unavailable"
                    )
                if self._in_progress_connection_generation is None:
                    self._in_progress_connection_generation = generation
                elif self._in_progress_connection_generation != generation:
                    self._mark_day_issue_unlocked("raw_market_connection_interrupted")

            previous = self._contracts.get(symbol)
            if previous is None and float(tick.volume) == 0.0:
                # CTP can publish a pre-trade snapshot whose last price is the
                # prior close. It is market evidence, but not a 60m bar open.
                return
            if previous is not None and tick.timestamp < previous.last_tick_timestamp:
                self._mark_contract_issue_unlocked(previous, "out_of_order_tick")
                raise OiEvidenceIntegrityError(f"raw OI tick is out-of-order: {symbol}")
            if previous is not None and tick.timestamp == previous.last_tick_timestamp:
                exact = (
                    float(tick.last_price) == previous.last_close
                    and float(tick.open_interest) == previous.last_hold
                    and float(tick.volume) == previous.last_cumulative_volume
                )
                if not exact:
                    self._mark_contract_issue_unlocked(previous, "conflicting_duplicate_tick")
                    raise OiEvidenceIntegrityError(f"raw OI tick conflicts at timestamp: {symbol}")
                self._duplicate_ticks += 1
                self._contracts[symbol] = replace(
                    previous,
                    raw_tick_count=previous.raw_tick_count + 1,
                    duplicate_tick_count=previous.duplicate_tick_count + 1,
                )
                self._changed()
                return

            reset = bool(previous is not None and tick.volume < previous.last_cumulative_volume)
            if previous is None or reset:
                volume_delta = float(tick.volume)
            else:
                volume_delta = float(tick.volume - previous.last_cumulative_volume)
            if reset:
                self._volume_resets += 1
            if previous is None:
                authoritative_open = float(tick.open_price)
                initial_issues: tuple[str, ...] = ()
                if authoritative_open <= 0.0:
                    authoritative_open = float(tick.last_price)
                    initial_issues = ("authoritative_open_price_missing",)
                bars = (
                    _new_bar(
                        tick,
                        bucket,
                        volume_delta,
                        first_open=authoritative_open,
                    ),
                )
                updated = ContractOiEvidence(
                    symbol=symbol,
                    exchange=tick.exchange.upper(),
                    product=product,
                    trading_day=day,
                    first_open=authoritative_open,
                    last_close=float(tick.last_price),
                    first_hold=float(tick.open_interest),
                    last_hold=float(tick.open_interest),
                    total_volume=volume_delta,
                    first_tick_timestamp=tick.timestamp,
                    last_tick_timestamp=tick.timestamp,
                    last_cumulative_volume=float(tick.volume),
                    bars=bars,
                    raw_tick_count=1,
                    duplicate_tick_count=0,
                    volume_reset_count=0,
                    complete=False,
                    issues=initial_issues,
                )
            else:
                bars_list = list(previous.bars)
                if bars_list[-1].bucket_start == bucket.bucket_start:
                    bars_list[-1] = _update_bar(bars_list[-1], tick, volume_delta)
                else:
                    if len(bars_list) >= MAX_BARS_PER_CONTRACT:
                        self._mark_contract_issue_unlocked(previous, "bar_memory_bound_exceeded")
                        raise OiEvidenceIntegrityError(
                            f"raw OI bar memory bound exceeded: {symbol}"
                        )
                    bars_list.append(_new_bar(tick, bucket, volume_delta))
                updated = replace(
                    previous,
                    last_close=float(tick.last_price),
                    last_hold=float(tick.open_interest),
                    total_volume=float(previous.total_volume + volume_delta),
                    last_tick_timestamp=tick.timestamp,
                    last_cumulative_volume=float(tick.volume),
                    bars=tuple(bars_list),
                    raw_tick_count=previous.raw_tick_count + 1,
                    volume_reset_count=previous.volume_reset_count + int(reset),
                )
            self._contracts[symbol] = updated
            self._changed()

    def _state_unlocked(self) -> Stress90OiEvidenceState:
        in_progress = self._in_progress
        if in_progress is not None:
            in_progress = replace(
                in_progress,
                contracts=MappingProxyType(dict(sorted(self._contracts.items()))),
            )
        return Stress90OiEvidenceState(
            completed=tuple(self._completed),
            in_progress=in_progress,
            observed_transitions=tuple(self._observed_transitions),
            raw_ticks_observed=self._raw_ticks_observed,
            duplicate_ticks=self._duplicate_ticks,
            volume_resets=self._volume_resets,
        )

    def checkpoint(self) -> None:
        """Persist one lock-consistent snapshot outside the callback/aggregation lock."""

        if self.store is None:
            return
        with self._lock:
            if not self._dirty:
                return
            state = self._state_unlocked()
            generation = self._generation
            expected_sequence = self._sequence
        record = self.store.save_state(state, expected_sequence=expected_sequence)
        with self._lock:
            self._sequence = record.sequence
            if self._generation == generation:
                self._dirty = False


def arm_stress90_raw_evidence_collection(
    broker,
    aggregator: Stress90OiEvidenceAggregator,
    *,
    trading_day: str,
    catalog: Sequence[ContractInfo],
    subscription_catalog: Sequence[ContractInfo] | None = None,
) -> tuple[str, ...]:
    """Install pre-coalescing OI observation before subscribing the validated universe."""

    set_observer = getattr(broker, "set_raw_tick_observer", None)
    subscribe = getattr(broker, "subscribe", None)
    if not callable(set_observer) or not callable(subscribe):
        raise OiEvidenceIntegrityError(
            "raw OI evidence Broker lacks observer/subscription capability"
        )
    expected = aggregator.set_expected_contracts(trading_day, catalog)
    by_symbol: dict[str, ContractInfo] = {}
    for contract in catalog:
        if not isinstance(contract, ContractInfo):
            raise OiEvidenceIntegrityError("raw OI evidence catalog row is invalid")
        symbol = contract.symbol.upper()
        if symbol in by_symbol:
            raise OiEvidenceIntegrityError("raw OI evidence catalog has duplicate symbols")
        by_symbol[symbol] = contract
    subscriptions = (
        tuple(subscription_catalog)
        if subscription_catalog is not None
        else tuple(by_symbol[symbol] for symbol in expected)
    )
    subscription_by_symbol: dict[str, ContractInfo] = {}
    for contract in subscriptions:
        if not isinstance(contract, ContractInfo):
            raise OiEvidenceIntegrityError("raw OI subscription catalog row is invalid")
        symbol = contract.symbol.upper()
        if symbol in subscription_by_symbol:
            raise OiEvidenceIntegrityError("raw OI subscription catalog has duplicate symbols")
        canonical = by_symbol.get(symbol)
        if canonical is None or canonical != contract:
            raise OiEvidenceIntegrityError(
                f"raw OI subscription identity is absent from catalog: {contract.symbol}"
            )
        subscription_by_symbol[symbol] = contract
    missing = sorted(set(expected) - set(subscription_by_symbol))
    if missing:
        raise OiEvidenceIntegrityError(
            "raw OI evidence expected catalog identities are missing: " + ",".join(missing)
        )
    set_observer(aggregator)
    for symbol in sorted(subscription_by_symbol):
        contract = subscription_by_symbol[symbol]
        subscribe(contract.symbol, contract.exchange)
    # ``set_expected_contracts`` declared the whole initial universe before the
    # observer became visible.  Missing/failed subscriptions therefore remain
    # missing coverage; recording them after individual subscribe calls would
    # race the first raw Tick and falsely label a complete initial batch late.
    return expected
