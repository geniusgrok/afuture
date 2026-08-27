"""Atomic, bounded CTP futures-catalog query generations."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
from threading import Lock

from ..models import ContractInfo

MAX_CTP_CATALOG_ROWS = 10_000
_CTP_FUTURES_PRODUCT_CLASS = "1"


class CtpContractCatalogIntegrityError(RuntimeError):
    """A complete CTP instrument response cannot be trusted."""


@dataclass(frozen=True)
class CtpContractCatalogSnapshot:
    trading_day: str
    request_id: int
    generation: int
    contracts: tuple[ContractInfo, ...]
    filtered_non_futures: int
    catalog_digest: str


@dataclass(frozen=True)
class CtpContractCatalogFailure:
    trading_day: str
    request_id: int
    reason: str


def _trading_day(raw: object) -> str:
    if not isinstance(raw, str):
        raise CtpContractCatalogIntegrityError("catalog trading day must be YYYYMMDD")
    try:
        parsed = datetime.strptime(raw, "%Y%m%d").strftime("%Y%m%d")
    except ValueError as exc:
        raise CtpContractCatalogIntegrityError("catalog trading day must be YYYYMMDD") from exc
    if parsed != raw:
        raise CtpContractCatalogIntegrityError("catalog trading day must be YYYYMMDD")
    return raw


def _request_id(raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise CtpContractCatalogIntegrityError("catalog request id must be positive")
    return raw


def _date(raw: object, *, name: str, optional: bool = False) -> str:
    if optional and raw in {None, ""}:
        return ""
    if not isinstance(raw, str):
        raise CtpContractCatalogIntegrityError("catalog contract lifecycle is invalid")
    try:
        parsed = datetime.strptime(raw, "%Y%m%d").date().isoformat()
    except ValueError as exc:
        raise CtpContractCatalogIntegrityError("catalog contract lifecycle is invalid") from exc
    if not parsed:
        raise CtpContractCatalogIntegrityError(f"catalog {name} is invalid")
    return parsed


def _futures_contract(raw: Mapping[str, object]) -> ContractInfo | None:
    if "ProductClass" not in raw:
        raise CtpContractCatalogIntegrityError("catalog ProductClass is missing")
    product_class = raw["ProductClass"]
    if not isinstance(product_class, str):
        raise CtpContractCatalogIntegrityError("catalog ProductClass is invalid")
    if product_class != _CTP_FUTURES_PRODUCT_CLASS:
        return None
    required = {"InstrumentID", "ExchangeID", "ProductID", "ExpireDate", "OpenDate"}
    if not required.issubset(raw):
        raise CtpContractCatalogIntegrityError("catalog futures row fields are invalid")
    symbol = raw["InstrumentID"]
    exchange = raw["ExchangeID"]
    product = raw["ProductID"]
    if (
        not isinstance(symbol, str)
        or not symbol.strip()
        or not isinstance(exchange, str)
        or not exchange.strip()
        or not isinstance(product, str)
        or not product.strip()
    ):
        raise CtpContractCatalogIntegrityError("catalog futures identity is invalid")
    symbol = symbol.strip()
    exchange = exchange.strip().upper()
    product = product.strip().upper()
    match = re.fullmatch(r"([A-Za-z]{1,4})\d{3,4}", symbol)
    if (
        match is None
        or match.group(1).upper() != product
        or re.fullmatch(r"[A-Z][A-Z0-9]*", exchange) is None
    ):
        raise CtpContractCatalogIntegrityError("catalog futures identity is invalid")
    expiry = _date(raw["ExpireDate"], name="expiry")
    listing = _date(raw["OpenDate"], name="listing", optional=True)
    if listing and listing > expiry:
        raise CtpContractCatalogIntegrityError("catalog contract lifecycle is invalid")
    return ContractInfo(symbol, exchange, product, expiry, listing)


def _catalog_digest(trading_day: str, contracts: tuple[ContractInfo, ...]) -> str:
    payload = {
        "source": "ctp_instrument_query",
        "trading_day": trading_day,
        "contracts": [asdict(contract) for contract in contracts],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return sha256(encoded).hexdigest()


class CtpContractCatalogAccumulator:
    """Publish one immutable catalog only after the matching CTP ``last`` boundary."""

    def __init__(self, *, max_rows: int = MAX_CTP_CATALOG_ROWS) -> None:
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
            raise ValueError("catalog row bound must be a positive integer")
        if max_rows > MAX_CTP_CATALOG_ROWS:
            raise ValueError("catalog row bound exceeds the production maximum")
        self.max_rows = max_rows
        self._lock = Lock()
        self._active_request_id: int | None = None
        self._active_trading_day = ""
        self._rows: dict[str, ContractInfo] = {}
        self._row_count = 0
        self._filtered_non_futures = 0
        self._latest_verified: CtpContractCatalogSnapshot | None = None
        self._last_failure: CtpContractCatalogFailure | None = None

    @property
    def latest_verified(self) -> CtpContractCatalogSnapshot | None:
        with self._lock:
            return self._latest_verified

    @property
    def active_request_id(self) -> int | None:
        with self._lock:
            return self._active_request_id

    @property
    def last_failure(self) -> CtpContractCatalogFailure | None:
        with self._lock:
            return self._last_failure

    def begin(self, *, request_id: int, trading_day: str) -> None:
        request = _request_id(request_id)
        day = _trading_day(trading_day)
        with self._lock:
            if self._active_request_id is not None:
                raise CtpContractCatalogIntegrityError("catalog query is already active")
            self._active_request_id = request
            self._active_trading_day = day
            self._rows = {}
            self._row_count = 0
            self._filtered_non_futures = 0

    def _fail_unlocked(self, reason: str) -> CtpContractCatalogIntegrityError:
        request = self._active_request_id
        day = self._active_trading_day
        if request is not None:
            self._last_failure = CtpContractCatalogFailure(day, request, reason)
        self._active_request_id = None
        self._active_trading_day = ""
        self._rows = {}
        self._row_count = 0
        self._filtered_non_futures = 0
        return CtpContractCatalogIntegrityError(reason)

    def observe(
        self,
        request_id: int,
        data: Mapping[str, object] | None,
        error: Mapping[str, object] | None,
        *,
        last: bool,
    ) -> CtpContractCatalogSnapshot | None:
        request = _request_id(request_id)
        with self._lock:
            if request != self._active_request_id:
                raise CtpContractCatalogIntegrityError("catalog response request id mismatch")
            error_id = 0
            if error:
                try:
                    raw_error_id = error.get("ErrorID", 0)
                    if isinstance(raw_error_id, bool) or not isinstance(raw_error_id, (int, str)):
                        raise TypeError
                    error_id = int(raw_error_id)
                except (TypeError, ValueError) as exc:
                    raise self._fail_unlocked("catalog query error payload is invalid") from exc
            if error_id:
                message = "" if error is None else str(error.get("ErrorMsg", "")).strip()
                raise self._fail_unlocked(
                    f"catalog query failed: ErrorID={error_id} message={message}"
                )
            if data is not None:
                self._row_count += 1
                if self._row_count > self.max_rows:
                    raise self._fail_unlocked("catalog query exceeded its memory bound")
                try:
                    contract = _futures_contract(data)
                except CtpContractCatalogIntegrityError as exc:
                    raise self._fail_unlocked(str(exc)) from exc
                if contract is None:
                    self._filtered_non_futures += 1
                elif contract.symbol in self._rows:
                    raise self._fail_unlocked(
                        f"catalog contains duplicate futures symbol: {contract.symbol}"
                    )
                else:
                    self._rows[contract.symbol] = contract
            if not last:
                return None
            if not self._rows:
                raise self._fail_unlocked("catalog complete response contains no futures")
            contracts = tuple(
                sorted(
                    self._rows.values(),
                    key=lambda item: (item.product, item.expiry, item.symbol, item.exchange),
                )
            )
            generation = (
                1 if self._latest_verified is None else self._latest_verified.generation + 1
            )
            snapshot = CtpContractCatalogSnapshot(
                trading_day=self._active_trading_day,
                request_id=request,
                generation=generation,
                contracts=contracts,
                filtered_non_futures=self._filtered_non_futures,
                catalog_digest=_catalog_digest(self._active_trading_day, contracts),
            )
            self._latest_verified = snapshot
            self._last_failure = None
            self._active_request_id = None
            self._active_trading_day = ""
            self._rows = {}
            self._row_count = 0
            self._filtered_non_futures = 0
            return snapshot

    def abort(self, *, request_id: int, reason: str) -> None:
        request = _request_id(request_id)
        detail = str(reason).strip()
        if not detail:
            raise ValueError("catalog abort reason is required")
        with self._lock:
            if request != self._active_request_id:
                raise CtpContractCatalogIntegrityError("catalog abort request id mismatch")
            self._fail_unlocked(detail)
