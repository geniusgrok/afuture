"""Request-bound raw account/position query generations, before SDK conversion.

A last callback completes one registered request, not all cached SDK positions.
This buffer owns no account state and can only publish a complete raw generation.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime
from math import isfinite
from threading import RLock
from time import monotonic
from typing import Any, NoReturn


class CtpSnapshotQueryError(RuntimeError):
    """Raw snapshot identity or completion is not trustworthy."""


class CtpSnapshotQuery:
    def __init__(
        self,
        kind: str,
        *,
        broker_id: str,
        investor_id: str,
        account_id: str = "",
        currency_id: str = "",
        timeout_seconds: float = 30.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if kind not in {"account", "position"}:
            raise ValueError("invalid snapshot query kind")
        self.kind = kind
        self.broker_id = broker_id
        self.investor_id = investor_id
        self.account_id = account_id
        self.currency_id = currency_id
        self.timeout_seconds = timeout_seconds
        self.clock = clock
        self._lock = RLock()
        self._last_request_id = 0
        self._request_id: int | None = None
        self._completed_id: int | None = None
        self._retired: deque[int] = deque(maxlen=128)
        self._day = ""
        self._deadline = 0.0
        self._rows: dict[tuple[str, ...], dict[str, Any]] = {}
        self._error = ""

    def _fail(self, detail: str) -> NoReturn:
        self._error = self._error or f"CTP {self.kind} snapshot: {detail}"
        raise CtpSnapshotQueryError(self._error)

    def begin(self, request_id: int, trading_day: str) -> bool:
        with self._lock:
            if self._error:
                self._fail(self._error)
            if type(request_id) is not int or request_id <= 0:
                self._fail("invalid request id")
            if type(trading_day) is not str or len(trading_day) != 8:
                self._fail("invalid broker trading day")
            try:
                if datetime.strptime(trading_day, "%Y%m%d").strftime("%Y%m%d") != trading_day:
                    self._fail("invalid broker trading day")
            except ValueError:
                self._fail("invalid broker trading day")
            if self._request_id is not None:
                if self.clock() < self._deadline:
                    return False  # No new native request was sent.
                self._retired.append(self._request_id)
            if request_id <= self._last_request_id or request_id in self._retired:
                self._fail("request id reused")
            self._last_request_id = request_id
            self._request_id = request_id
            self._day = trading_day
            self._deadline = self.clock() + self.timeout_seconds
            self._rows = {}
            return True

    def submitted(self, request_id: int, status: object) -> None:
        with self._lock:
            if type(status) is not int:
                self._fail("invalid native submission result")
            if status == 0:
                return
            if self._completed_id == request_id or self._rows:
                self._fail("callback received for rejected native request")
            if self._request_id == request_id:
                self._retired.append(request_id)
                self._request_id = None
            if status not in {-2, -3}:
                self._fail(f"native request rejected ({status})")

    def observe(
        self,
        data: object,
        error: object,
        request_id: object,
        last: object,
        *,
        trading_day: str,
    ) -> tuple[dict[str, Any], ...] | None:
        with self._lock:
            if self._error:
                self._fail(self._error)
            if type(request_id) is not int or request_id <= 0 or type(last) is not bool:
                self._fail("invalid callback envelope")
            if request_id in self._retired:
                return None  # A known timed-out/rejected generation cannot become fresh.
            if request_id != self._request_id:
                self._fail("unregistered or already completed request")
            if trading_day != self._day:
                self._fail("broker trading day changed during query")
            if self.clock() >= self._deadline:
                self._retired.append(request_id)
                self._request_id = None
                self._rows = {}
                return None
            if not isinstance(error, dict) or type(error.get("ErrorID", 0)) is not int:
                self._fail("invalid query error envelope")
            if error.get("ErrorID", 0):
                self._fail(f"query error ({error['ErrorID']})")
            if data is not None and not isinstance(data, dict):
                self._fail("invalid raw snapshot row")
            if data:
                row = deepcopy(data)
                key = self._validate_row(row)
                previous = self._rows.get(key)
                if previous is not None and previous != row:
                    self._fail("conflicting duplicate row")
                self._rows[key] = row
                if len(self._rows) > (1 if self.kind == "account" else 10000):
                    self._fail("snapshot row bound exceeded")
            if not last:
                return None
            if self.kind == "account" and len(self._rows) != 1:
                self._fail("complete account query must contain exactly one bound account")
            result = tuple(self._rows.values())
            self._completed_id = request_id
            self._request_id = None
            self._rows = {}
            return result

    def _validate_row(self, row: dict[str, Any]) -> tuple[str, ...]:
        expected = {"BrokerID": self.broker_id, "TradingDay": self._day}
        keys: tuple[str, ...]
        if self.kind == "account":
            if self.account_id:
                expected["AccountID"] = self.account_id
            if self.currency_id:
                expected["CurrencyID"] = self.currency_id
            keys = ("AccountID", "CurrencyID")
        else:
            expected["InvestorID"] = self.investor_id
            keys = ("InstrumentID", "ExchangeID", "PosiDirection", "HedgeFlag", "PositionDate")
        if any(type(row.get(k)) is not str or row.get(k) != v for k, v in expected.items()):
            self._fail("row account/trading-day identity mismatch")
        if any(type(row.get(k)) is not str or not row[k] for k in keys):
            self._fail("row key is missing or invalid")
        if self.kind == "position":
            if row["PosiDirection"] not in {"2", "3"} or row["HedgeFlag"] != "1":
                self._fail("unsupported net/hedged position semantics")
            if row["PositionDate"] not in {"1", "2"}:
                self._fail("invalid position date bucket")
            if type(row.get("Position")) is not int or row["Position"] < 0:
                self._fail("invalid position volume")
        for value in row.values():
            if isinstance(value, float) and not isfinite(value):
                self._fail("non-finite raw snapshot value")
            if not isinstance(value, (str, int, float)):
                self._fail("non-scalar raw snapshot value")
        return tuple(row[k] for k in keys)
