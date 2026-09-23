"""Request-bound capture of CTP SettlementInfo, without interpreting account facts.

Completion of the transport query is NOT proof of financial continuity.  In
particular, the supported vnpy binding exposes already-decoded Content strings,
not native GBK bytes.  No balance, funding or position flag is upgraded here.
"""

from __future__ import annotations

import json
import math
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
from threading import Event, Lock, RLock
from time import monotonic, sleep

_FIELDS = frozenset(
    {
        "TradingDay",
        "SettlementID",
        "BrokerID",
        "InvestorID",
        "SequenceNo",
        "Content",
        "AccountID",
        "CurrencyID",
    }
)
_MAX_FRAGMENTS = 4096
_MAX_CONTENT_BYTES = 2_000_000


class CtpSettlementQueryError(RuntimeError):
    """A requested statement has no complete, identity-consistent transport result."""


def _day(value: object) -> str:
    if type(value) is not str or len(value) != 8 or not value.isascii() or not value.isdigit():
        raise CtpSettlementQueryError("settlement trading day must be YYYYMMDD")
    try:
        parsed = datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise CtpSettlementQueryError("settlement trading day is invalid") from exc
    if parsed.strftime("%Y%m%d") != value:
        raise CtpSettlementQueryError("settlement trading day is invalid")
    return value


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise CtpSettlementQueryError(f"settlement {name} is invalid")
    return value


def _identity(value: object) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise CtpSettlementQueryError("settlement account identity is incomplete")
    return value


@dataclass(frozen=True)
class CtpSettlementFragment:
    sequence: int
    content: str


@dataclass(frozen=True)
class CtpSettlementDocument:
    """Immutable query evidence. Not a normalized or approved settlement statement."""

    request_id: int
    broker_id: str
    investor_id: str
    account_id: str
    currency_id: str
    trading_day: str
    settlement_id: int
    fragments: tuple[CtpSettlementFragment, ...]
    received_at_utc: str
    query_elapsed_seconds: float
    evidence_digest: str

    @property
    def content(self) -> str:
        return "".join(fragment.content for fragment in self.fragments)

    def to_dict(self) -> dict[str, object]:
        body = asdict(self)
        body.update(
            kind="afuture.ctp-settlement-capture",
            schema_version=1,
            query_end_received=True,
            native_bytes_preserved=False,
            content_representation="vnpy-decoded-unicode-fragments",
            sequence_origin_verified=False,
            financial_continuity_verified=False,
            orders_sent=0,
            cancels_sent=0,
        )
        return body


class CtpSettlementQuery:
    """One bounded native query; callbacks can never mix request generations.

    A timeout retires that id. Delayed replies to a retired timeout are ignored;
    a second end marker or a reply to a successfully completed query is a sticky
    protocol failure. No caller may reuse a captured document after that failure.
    """

    def __init__(self) -> None:
        self._serial = Lock()
        self._lock = RLock()
        self._event = Event()
        self._active_id: int | None = None
        self._completed_id: int | None = None
        self._completed_document: CtpSettlementDocument | None = None
        self._retired: deque[int] = deque(maxlen=128)
        self._identity: dict[str, str] = {}
        self._rows: dict[int, CtpSettlementFragment] = {}
        self._settlement_id: int | None = None
        self._byte_count = 0
        self._error = ""
        self._integrity_error = ""

    @property
    def integrity_error(self) -> str:
        with self._lock:
            return self._integrity_error

    def observe(self, data, error, request_id: int, last: bool) -> None:
        """Native callback ingress; never throw into the SDK callback thread."""
        with self._lock:
            try:
                request_id = _integer(request_id, "request ID", minimum=1)
                if type(last) is not bool:
                    raise CtpSettlementQueryError("settlement end marker must be boolean")
                if request_id in self._retired:
                    return
                if request_id != self._active_id or self._event.is_set():
                    raise CtpSettlementQueryError(
                        "settlement reply outside active query generation"
                    )
                if error is not None and type(error) is not dict:
                    raise CtpSettlementQueryError("settlement error envelope is invalid")
                code = _integer((error or {}).get("ErrorID", 0), "ErrorID")
                if code:
                    # The caller may retry a known broker refusal in a new query.
                    # Do not retain potentially sensitive free-text ErrorMsg.
                    self._error = f"CTP settlement query rejected: ErrorID={code}"
                    self._event.set()
                    return
                if data is not None and data != {}:
                    if type(data) is not dict or set(data) != _FIELDS:
                        raise CtpSettlementQueryError("settlement fragment fields changed")
                    if any(data[key] != value for key, value in self._identity.items()):
                        raise CtpSettlementQueryError("settlement fragment account/day mismatch")
                    settlement_id = _integer(data["SettlementID"], "settlement ID")
                    if self._settlement_id is None:
                        self._settlement_id = settlement_id
                    if self._settlement_id != settlement_id:
                        raise CtpSettlementQueryError("settlement identity changed within query")
                    sequence = _integer(data["SequenceNo"], "sequence")
                    content = data["Content"]
                    if (
                        type(content) is not str
                        or not content
                        or "\x00" in content
                        or "\ufffd" in content
                    ):
                        raise CtpSettlementQueryError(
                            "settlement Content is missing or undecodable"
                        )
                    encoded = content.encode("utf-8", errors="strict")
                    if len(encoded) > 8192:
                        raise CtpSettlementQueryError("settlement fragment is oversized")
                    fragment = CtpSettlementFragment(sequence, content)
                    prior = self._rows.get(sequence)
                    if prior is not None and prior != fragment:
                        raise CtpSettlementQueryError("conflicting duplicate settlement fragment")
                    if prior is None:
                        if (
                            len(self._rows) >= _MAX_FRAGMENTS
                            or self._byte_count + len(encoded) > _MAX_CONTENT_BYTES
                        ):
                            raise CtpSettlementQueryError("settlement query exceeded capture bound")
                        self._rows[sequence] = fragment
                        self._byte_count += len(encoded)
                if last:
                    if not self._rows:
                        self._error = "requested settlement document is not available"
                    else:
                        numbers = sorted(self._rows)
                        if numbers[-1] - numbers[0] + 1 != len(numbers):
                            raise CtpSettlementQueryError("settlement fragment sequence has a gap")
                    self._event.set()
            except (CtpSettlementQueryError, UnicodeError, TypeError, ValueError) as exc:
                self._integrity_error = self._integrity_error or str(exc)
                self._error = self._integrity_error
                self._event.set()

    def require_current(self, document: CtpSettlementDocument) -> None:
        with self._lock:
            if (
                self._integrity_error
                or self._completed_id != document.request_id
                or self._completed_document != document
            ):
                raise CtpSettlementQueryError(
                    "settlement capture is no longer the current query result"
                )

    def query(
        self,
        native_query: Callable[[dict[str, str], int], int],
        *,
        request_id: int,
        broker_id: str,
        investor_id: str,
        account_id: str,
        currency_id: str,
        trading_day: str,
        timeout_seconds: float,
    ) -> CtpSettlementDocument:
        request_id = _integer(request_id, "request ID", minimum=1)
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("settlement query timeout must be finite and positive")
        request = {
            "BrokerID": _identity(broker_id),
            "InvestorID": _identity(investor_id),
            "AccountID": _identity(account_id),
            "CurrencyID": _identity(currency_id),
            "TradingDay": _day(trading_day),
        }
        with self._serial:
            with self._lock:
                if self._integrity_error:
                    raise CtpSettlementQueryError(self._integrity_error)
                if request_id in self._retired or request_id == self._completed_id:
                    raise CtpSettlementQueryError("settlement request ID was already used")
                self._active_id = request_id
                self._completed_id = None
                self._completed_document = None
                self._event.clear()
                self._identity = request
                self._rows = {}
                self._settlement_id = None
                self._byte_count = 0
                self._error = ""
            start = monotonic()
            deadline = start + float(timeout_seconds)
            success = False
            try:
                while True:
                    status = native_query(dict(request), request_id)
                    if type(status) is not int:
                        raise CtpSettlementQueryError("CTP settlement request status is invalid")
                    if status == 0:
                        break
                    if status not in {-2, -3}:
                        raise CtpSettlementQueryError(
                            f"CTP settlement query submit failed: {status}"
                        )
                    if self._event.is_set():
                        raise CtpSettlementQueryError(
                            "refused settlement request received callbacks"
                        )
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise CtpSettlementQueryError("CTP settlement query rate-limit timeout")
                    sleep(min(0.2, remaining))
                remaining = deadline - monotonic()
                if remaining <= 0 or not self._event.wait(remaining):
                    raise CtpSettlementQueryError(
                        "CTP settlement query did not finish before deadline"
                    )
                with self._lock:
                    if self._error:
                        raise CtpSettlementQueryError(self._error)
                    if self._settlement_id is None:
                        raise CtpSettlementQueryError("settlement identity is missing")
                    fragments = tuple(self._rows[key] for key in sorted(self._rows))
                    body = dict(
                        request_id=request_id,
                        broker_id=broker_id,
                        investor_id=investor_id,
                        account_id=account_id,
                        currency_id=currency_id,
                        trading_day=trading_day,
                        settlement_id=self._settlement_id,
                        fragments=fragments,
                        received_at_utc=datetime.now(timezone.utc).isoformat(),
                        query_elapsed_seconds=monotonic() - start,
                    )
                    digest_body = {**body, "fragments": [asdict(row) for row in fragments]}
                    digest = sha256(
                        json.dumps(
                            digest_body,
                            sort_keys=True,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            allow_nan=False,
                        ).encode()
                    ).hexdigest()
                    document = CtpSettlementDocument(**body, evidence_digest=digest)
                    self._completed_id = request_id
                    self._completed_document = document
                    success = True
                    return document
            finally:
                with self._lock:
                    if not success:
                        self._retired.append(request_id)
                    self._active_id = None
