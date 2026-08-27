"""Frozen Chinese-futures product sessions and first-entry timing rules.

The manifest is an auditable production constant.  It never infers a trading calendar:
CTP supplies the authoritative ``trading_day`` and this module only classifies a
timezone-aware tick within that day/product's fixed clock sessions.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from enum import Enum
from hashlib import sha256
from types import MappingProxyType
from zoneinfo import ZoneInfo

from .execution_aligned_policy import FROZEN_PRODUCTS

_CHINA_TZ = ZoneInfo("Asia/Shanghai")
_DAY_SESSIONS = ("09:00-10:15", "10:30-11:30", "13:30-15:00")
_NIGHT_2300 = "21:00-23:00"
_NIGHT_0100 = "21:00-01:00"
_NIGHT_0230 = "21:00-02:30"

SESSION_MANIFEST_VERSION = "stress90-cn-futures-sessions-v1"
SESSION_MANIFEST_EFFECTIVE_DATE = "2026-08-25"
SESSION_MANIFEST_SOURCE_URLS = MappingProxyType(
    {
        "CZCE": "https://www.czce.com.cn/cn/sspz/index.shtml",
        "DCE": "https://www.dce.com.cn/dalianshangpin/sspz/index.html",
        "INE": "https://www.ine.cn/services/calenderandholidays/tradinghours/",
        "SHFE": "https://www.shfe.com.cn/services/calenderandholidays/tradinghours/",
    }
)

_EXCHANGE_PRODUCTS = {
    "DCE": (
        "A",
        "B",
        "C",
        "CS",
        "EB",
        "EG",
        "I",
        "J",
        "JM",
        "L",
        "LH",
        "M",
        "P",
        "PG",
        "PP",
        "V",
        "Y",
    ),
    "CZCE": (
        "AP",
        "CF",
        "CJ",
        "FG",
        "MA",
        "OI",
        "PF",
        "PK",
        "RM",
        "SA",
        "SF",
        "SM",
        "SR",
        "TA",
        "UR",
    ),
    "SHFE": (
        "AG",
        "AL",
        "AU",
        "BU",
        "CU",
        "FU",
        "HC",
        "NI",
        "PB",
        "RB",
        "RU",
        "SN",
        "SP",
        "SS",
        "ZN",
    ),
    "INE": ("BC", "LU", "NR"),
}

# Products with no night session are intentionally conservative: they can only add risk
# at their first day window.  This constant is not learned or revised at runtime.
_DAY_ONLY_PRODUCTS = frozenset({"AP", "CJ", "LH", "PK", "SF", "SM", "UR"})
_NIGHT_0230_PRODUCTS = frozenset({"AG", "AU"})
_NIGHT_0100_PRODUCTS = frozenset({"AL", "BC", "BU", "CU", "HC", "NI", "PB", "RB", "SN", "SS", "ZN"})


@dataclass(frozen=True)
class ProductSessionDefinition:
    product: str
    exchange: str
    sessions: tuple[str, ...]
    first_entry_window: str
    has_night_session: bool


def _build_manifest() -> MappingProxyType[str, ProductSessionDefinition]:
    exchange_by_product = {
        product: exchange
        for exchange, products in _EXCHANGE_PRODUCTS.items()
        for product in products
    }
    if set(exchange_by_product) != set(FROZEN_PRODUCTS):
        raise RuntimeError("directional session exchange manifest does not cover frozen products")
    result: dict[str, ProductSessionDefinition] = {}
    for product in FROZEN_PRODUCTS:
        has_night = product not in _DAY_ONLY_PRODUCTS
        sessions: tuple[str, ...]
        if not has_night:
            sessions = _DAY_SESSIONS
        elif product in _NIGHT_0230_PRODUCTS:
            sessions = (_NIGHT_0230, *_DAY_SESSIONS)
        elif product in _NIGHT_0100_PRODUCTS:
            sessions = (_NIGHT_0100, *_DAY_SESSIONS)
        else:
            sessions = (_NIGHT_2300, *_DAY_SESSIONS)
        result[product] = ProductSessionDefinition(
            product=product,
            exchange=exchange_by_product[product],
            sessions=sessions,
            first_entry_window="20:55-21:10" if has_night else "08:55-09:10",
            has_night_session=has_night,
        )
    return MappingProxyType(result)


PRODUCT_SESSION_MANIFEST = _build_manifest()
SESSION_MANIFEST_DIGEST = sha256(
    json.dumps(
        {
            "effective_date": SESSION_MANIFEST_EFFECTIVE_DATE,
            "products": [asdict(PRODUCT_SESSION_MANIFEST[product]) for product in FROZEN_PRODUCTS],
            "source_urls": dict(SESSION_MANIFEST_SOURCE_URLS),
            "version": SESSION_MANIFEST_VERSION,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()


class OpeningWindowStatus(str, Enum):
    BEFORE = "before"
    OPEN = "open"
    MISSED = "missed"
    BYPASS = "bypass"


@dataclass(frozen=True)
class SessionBucket:
    product: str
    exchange: str
    trading_day: str
    session: str
    session_start: datetime
    session_end: datetime
    bucket_start: datetime
    bucket_end: datetime


def _definition(product: str) -> ProductSessionDefinition:
    normalized = str(product).upper()
    try:
        return PRODUCT_SESSION_MANIFEST[normalized]
    except KeyError as exc:
        raise ValueError(f"unknown frozen product: {product}") from exc


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def _parse_clock(raw: str) -> tuple[int, int]:
    try:
        parsed = datetime.strptime(raw, "%H:%M").time()
    except ValueError as exc:  # pragma: no cover - immutable module constants
        raise RuntimeError(f"invalid fixed product session clock: {raw}") from exc
    return parsed.hour, parsed.minute


def _validate_trading_day(raw: str) -> str:
    if not isinstance(raw, str):
        raise ValueError("CTP trading day must be YYYYMMDD")
    try:
        parsed = datetime.strptime(raw, "%Y%m%d").strftime("%Y%m%d")
    except ValueError as exc:
        raise ValueError("CTP trading day must be YYYYMMDD") from exc
    if parsed != raw:
        raise ValueError("CTP trading day must be YYYYMMDD")
    return raw


def opening_window_status(
    product: str,
    now: datetime,
    *,
    action_category: str,
) -> OpeningWindowStatus:
    """Classify only risk-increasing actions; reductions/exits/rolls always bypass."""

    definition = _definition(product)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("opening-window time must be timezone-aware")
    category = str(action_category)
    if category in {"reduction", "exit", "same_product_roll", "hard_risk_exit"}:
        return OpeningWindowStatus.BYPASS
    if category not in {"entry", "same_sign_add", "reversal_open"}:
        raise ValueError(f"unknown directional action category: {action_category}")
    local_minutes = _minutes(now.astimezone(_CHINA_TZ).time())
    if definition.has_night_session:
        if 20 * 60 + 55 <= local_minutes <= 21 * 60 + 10:
            return OpeningWindowStatus.OPEN
        if 15 * 60 <= local_minutes < 20 * 60 + 55:
            return OpeningWindowStatus.BEFORE
        return OpeningWindowStatus.MISSED
    if 8 * 60 + 55 <= local_minutes <= 9 * 60 + 10:
        return OpeningWindowStatus.OPEN
    if local_minutes < 8 * 60 + 55 or local_minutes >= 15 * 60:
        return OpeningWindowStatus.BEFORE
    return OpeningWindowStatus.MISSED


def session_bucket_for_tick(
    product: str,
    timestamp: datetime,
    trading_day: str,
) -> SessionBucket | None:
    """Map one timestamp to a fixed 60m bucket while preserving CTP day ownership."""

    definition = _definition(product)
    day = _validate_trading_day(trading_day)
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
        raise ValueError("session tick timestamp must be timezone-aware")
    local = timestamp.astimezone(_CHINA_TZ)
    local_minutes = _minutes(local.time())
    for raw_session in definition.sessions:
        start_raw, end_raw = raw_session.split("-", 1)
        start_hour, start_minute = _parse_clock(start_raw)
        end_hour, end_minute = _parse_clock(end_raw)
        start_minutes = start_hour * 60 + start_minute
        end_minutes = end_hour * 60 + end_minute
        crosses_midnight = end_minutes <= start_minutes
        inside = (
            local_minutes >= start_minutes or local_minutes <= end_minutes
            if crosses_midnight
            else start_minutes <= local_minutes <= end_minutes
        )
        if not inside:
            continue
        anchor_date = local.date()
        if crosses_midnight and local_minutes <= end_minutes:
            anchor_date -= timedelta(days=1)
        session_start = datetime.combine(
            anchor_date,
            time(start_hour, start_minute),
            tzinfo=_CHINA_TZ,
        )
        end_date = anchor_date + timedelta(days=1) if crosses_midnight else anchor_date
        session_end = datetime.combine(
            end_date,
            time(end_hour, end_minute),
            tzinfo=_CHINA_TZ,
        )
        elapsed_seconds = max(0.0, (local - session_start).total_seconds())
        bucket_number = int(elapsed_seconds // 3600)
        maximum_bucket = max(0, int((session_end - session_start).total_seconds() - 1) // 3600)
        bucket_number = min(bucket_number, maximum_bucket)
        bucket_start = session_start + timedelta(hours=bucket_number)
        bucket_end = min(bucket_start + timedelta(hours=1), session_end)
        return SessionBucket(
            product=definition.product,
            exchange=definition.exchange,
            trading_day=day,
            session=raw_session,
            session_start=session_start,
            session_end=session_end,
            bucket_start=bucket_start,
            bucket_end=bucket_end,
        )
    return None
