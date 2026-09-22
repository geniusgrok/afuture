"""Versioned expected exchange sessions; never an account/settlement authority.

Runtime schedules are deliberately separate from frozen research manifests.
Only explicitly covered dates/products can authorize a production order.
"""

from __future__ import annotations

import json
import re
from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

_CHINA = ZoneInfo("Asia/Shanghai")
_FIELDS = {
    "schema_version",
    "version",
    "timezone",
    "coverage_start",
    "coverage_end",
    "sources",
    "exchanges",
    "products",
}
_EXCHANGES = {"SHFE", "INE", "DCE", "CZCE", "GFEX"}


class RuntimeCalendarError(RuntimeError):
    """Expected session coverage, source identity or counter agreement is missing."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeCalendarError("duplicate calendar JSON field")
        result[key] = value
    return result


def _day(value: object) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise RuntimeCalendarError("invalid calendar date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeCalendarError("invalid calendar date") from exc


def _window(value: object, *, overnight: bool) -> tuple[time, time]:
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}-\d{2}:\d{2}", value):
        raise RuntimeCalendarError("invalid calendar session window")
    try:
        start, end = (time.fromisoformat(item) for item in value.split("-"))
    except ValueError as exc:
        raise RuntimeCalendarError("invalid calendar session clock") from exc
    if start == end or (not overnight and start > end):
        raise RuntimeCalendarError("empty or reversed calendar session")
    if overnight and (start < time(18) or (end > time(6) and end < start)):
        raise RuntimeCalendarError("invalid overnight calendar session")
    return start, end


@dataclass(frozen=True)
class RuntimeProductSessions:
    exchange: str
    day_sessions: tuple[tuple[time, time], ...]
    night_session: tuple[time, time] | None


@dataclass(frozen=True)
class RuntimeTradingCalendar:
    version: str
    digest: str
    coverage_start: date
    coverage_end: date
    open_days: Mapping[str, tuple[date, ...]]
    no_night_dates: Mapping[str, frozenset[date]]
    products: Mapping[str, RuntimeProductSessions]

    @classmethod
    def load(cls, path: str | Path | None = None) -> RuntimeTradingCalendar:
        location = (
            Path(path) if path is not None else Path(__file__).with_name("runtime_calendar.json")
        )
        try:
            with location.open("rb") as source:
                raw = source.read(1_048_577)
            if not raw or len(raw) > 1_048_576:
                raise RuntimeCalendarError("calendar file is empty or exceeds size bound")
            envelope = json.loads(raw, object_pairs_hook=_unique_object)
            if not isinstance(envelope, dict) or set(envelope) != {"payload", "sha256"}:
                raise RuntimeCalendarError("invalid calendar envelope")
            data = envelope["payload"]
            canonical = json.dumps(
                data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            ).encode()
            digest = sha256(canonical).hexdigest()
            if digest != envelope["sha256"]:
                raise RuntimeCalendarError("calendar checksum mismatch")
            return cls._from_payload(data, digest)
        except RuntimeCalendarError:
            raise
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise RuntimeCalendarError("calendar cannot be read or verified") from exc

    @classmethod
    def _from_payload(cls, data: object, digest: str) -> RuntimeTradingCalendar:
        if not isinstance(data, dict) or set(data) != _FIELDS:
            raise RuntimeCalendarError("calendar fields are invalid")
        if (
            type(data["schema_version"]) is not int
            or data["schema_version"] != 1
            or data["timezone"] != "Asia/Shanghai"
        ):
            raise RuntimeCalendarError("calendar schema/timezone mismatch")
        version = data["version"]
        if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", version):
            raise RuntimeCalendarError("calendar version is invalid")
        start, end = _day(data["coverage_start"]), _day(data["coverage_end"])
        if not 1 <= (end - start).days <= 731:
            raise RuntimeCalendarError("calendar coverage is invalid")
        sources = data["sources"]
        if not isinstance(sources, dict) or not 1 <= len(sources) <= 64:
            raise RuntimeCalendarError("calendar sources are missing")
        for entry in sources.values():
            if not isinstance(entry, dict) or set(entry) != {
                "issuer",
                "document",
                "url",
                "publication_date",
            }:
                raise RuntimeCalendarError("calendar source fields are invalid")
            if any(not isinstance(value, str) or not value.strip() for value in entry.values()):
                raise RuntimeCalendarError("calendar source identity is missing")
            if entry["issuer"] not in _EXCHANGES:
                raise RuntimeCalendarError("calendar source issuer is unknown")
            uri = urlsplit(entry["url"])
            if uri.scheme != "https" or not uri.netloc or uri.username or uri.password:
                raise RuntimeCalendarError("calendar source URL is invalid")
            if _day(entry["publication_date"]) > end:
                raise RuntimeCalendarError("calendar source date exceeds coverage")
        exchanges = data["exchanges"]
        if not isinstance(exchanges, dict) or not exchanges or not set(exchanges) <= _EXCHANGES:
            raise RuntimeCalendarError("calendar exchange set is invalid")
        open_days: dict[str, tuple[date, ...]] = {}
        no_nights: dict[str, frozenset[date]] = {}
        for exchange, entry in exchanges.items():
            if not isinstance(entry, dict) or set(entry) != {
                "sources",
                "open_days",
                "closed_days",
                "no_night_dates",
            }:
                raise RuntimeCalendarError("calendar exchange fields are invalid")
            cls._require_sources(entry["sources"], sources, exchange)
            arrays = []
            for key in ("open_days", "closed_days", "no_night_dates"):
                values = entry[key]
                if not isinstance(values, list):
                    raise RuntimeCalendarError("calendar date list is invalid")
                days = tuple(_day(value) for value in values)
                if tuple(sorted(set(days))) != days:
                    raise RuntimeCalendarError("calendar dates are duplicate or unordered")
                if any(day < start or day > end for day in days):
                    raise RuntimeCalendarError("calendar dates exceed declared coverage")
                arrays.append(days)
            opened, closed, cancelled = arrays
            if (
                not opened
                or set(opened) & set(closed)
                or len(opened) + len(closed) != (end - start).days + 1
            ):
                raise RuntimeCalendarError("calendar date coverage is incomplete or conflicting")
            if not set(cancelled) <= set(opened):
                raise RuntimeCalendarError("night cancellation is not an open natural date")
            open_days[exchange] = opened
            no_nights[exchange] = frozenset(cancelled)
        products = data["products"]
        if not isinstance(products, dict) or not 1 <= len(products) <= 200:
            raise RuntimeCalendarError("calendar products are invalid")
        sessions = {}
        for product, entry in products.items():
            if (
                not re.fullmatch(r"[A-Z]{1,4}", product)
                or not isinstance(entry, dict)
                or set(entry) != {"exchange", "sources", "day_sessions", "night_session"}
            ):
                raise RuntimeCalendarError("calendar product fields are invalid")
            exchange = entry["exchange"]
            if exchange not in open_days:
                raise RuntimeCalendarError("product exchange calendar is missing")
            cls._require_sources(entry["sources"], sources, exchange)
            day = entry["day_sessions"]
            if not isinstance(day, list) or not 1 <= len(day) <= 5:
                raise RuntimeCalendarError("calendar day sessions are missing")
            windows = tuple(_window(item, overnight=False) for item in day)
            if any(windows[i][1] >= windows[i + 1][0] for i in range(len(windows) - 1)):
                raise RuntimeCalendarError("calendar day sessions overlap")
            night = (
                None
                if entry["night_session"] is None
                else _window(entry["night_session"], overnight=True)
            )
            sessions[product] = RuntimeProductSessions(exchange, windows, night)
        return cls(
            version,
            digest,
            start,
            end,
            MappingProxyType(open_days),
            MappingProxyType(no_nights),
            MappingProxyType(sessions),
        )

    @staticmethod
    def _require_sources(refs: object, sources: dict, exchange: str) -> None:
        if not isinstance(refs, list) or not refs or len(set(refs)) != len(refs):
            raise RuntimeCalendarError("calendar source references are invalid")
        if any(
            not isinstance(key, str) or key not in sources or sources[key]["issuer"] != exchange
            for key in refs
        ):
            raise RuntimeCalendarError("calendar source/exchange identity mismatch")

    def _local(self, now: datetime) -> datetime:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeCalendarError("calendar clock must be timezone-aware")
        local = now.astimezone(_CHINA)
        if not self.coverage_start <= local.date() <= self.coverage_end:
            raise RuntimeCalendarError("runtime calendar coverage is unknown or expired")
        return local

    def product_sessions(self, symbol: str, exchange: str | None = None) -> RuntimeProductSessions:
        match = re.fullmatch(r"([A-Za-z]{1,4})\d{3,4}", symbol)
        if match is None or match.group(1).upper() not in self.products:
            raise RuntimeCalendarError(f"runtime calendar has no contract coverage: {symbol}")
        profile = self.products[match.group(1).upper()]
        if exchange is not None and profile.exchange != exchange:
            raise RuntimeCalendarError("runtime calendar contract/exchange mismatch")
        return profile

    def next_trading_day(self, trading_day: str, exchange: str) -> str:
        try:
            day = datetime.strptime(trading_day, "%Y%m%d").date()
            days = self.open_days[exchange]
        except (ValueError, KeyError) as exc:
            raise RuntimeCalendarError("calendar source trading day/exchange is invalid") from exc
        if day not in days:
            raise RuntimeCalendarError("source is not a covered exchange trading day")
        index = bisect_right(days, day)
        if index >= len(days):
            raise RuntimeCalendarError("next trading day exceeds calendar coverage")
        return days[index].strftime("%Y%m%d")

    def expected_trading_day(
        self, symbol: str, now: datetime, exchange: str | None = None
    ) -> str | None:
        local = self._local(now)
        profile = self.product_sessions(symbol, exchange)
        natural = local.date()
        clock = local.time()
        opened = self.open_days[profile.exchange]
        if natural in opened and any(start <= clock < end for start, end in profile.day_sessions):
            return natural.strftime("%Y%m%d")
        night = profile.night_session
        if night is None:
            return None
        start, end = night
        night_date = natural
        if end < start:
            if clock < end:
                night_date -= timedelta(days=1)
            elif clock < start:
                return None
        elif not start <= clock < end:
            return None
        if night_date < self.coverage_start:
            raise RuntimeCalendarError("night-session origin is outside calendar coverage")
        if night_date not in opened or night_date in self.no_night_dates[profile.exchange]:
            return None
        return self.next_trading_day(night_date.strftime("%Y%m%d"), profile.exchange)

    def require_order_session(
        self, symbol: str, exchange: str, now: datetime, counter_day: str
    ) -> None:
        expected = self.expected_trading_day(symbol, now, exchange)
        if expected is None:
            raise RuntimeCalendarError("exchange calendar is outside continuous trading session")
        if expected != counter_day:
            raise RuntimeCalendarError("exchange calendar and CTP trading day disagree")
