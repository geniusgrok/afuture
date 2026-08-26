"""Atomic, checksummed market-input evidence for directional OHLC history.

This sidecar never stores account, order, fill, position, or strategy state.  It is only
the last provider history whose complete open/close panel passed the live input contract.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from math import isfinite
from numbers import Integral, Real
from pathlib import Path
from tempfile import NamedTemporaryFile

import pandas as pd

from .directional_data_validation import validate_daily_index
from .durable_file_creation import (
    DurableFileCreationToken,
    create_durable_file_exclusive,
    durable_file_lock,
)

OHLC_CACHE_SCHEMA_VERSION = 1
_CACHE_KIND = "afuture.directional_ohlc"


class DirectionalOHLCCacheIntegrityError(RuntimeError):
    """Persisted directional market input is malformed or cannot be trusted."""


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DirectionalOHLCCacheIntegrityError(
                f"duplicate directional OHLC cache JSON key: {key}"
            )
        result[key] = value
    return result


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return sha256(_canonical_json(value)).hexdigest()


def _normalized_products(raw: object, *, name: str) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise DirectionalOHLCCacheIntegrityError(f"{name} must be a non-empty sequence")
    products: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item or item != item.upper():
            raise DirectionalOHLCCacheIntegrityError(
                f"{name} must contain non-empty uppercase strings"
            )
        products.append(item)
    if len(products) != len(set(products)):
        raise DirectionalOHLCCacheIntegrityError(f"{name} contains duplicates")
    return tuple(products)


def _validated_index(raw: object, *, name: str) -> pd.DatetimeIndex:
    try:
        index = pd.DatetimeIndex(pd.to_datetime(raw, errors="raise"))
    except Exception as exc:
        raise DirectionalOHLCCacheIntegrityError(f"{name} contains an invalid date") from exc
    if index.tz is not None or not index.equals(index.normalize()):
        raise DirectionalOHLCCacheIntegrityError(
            f"{name} index must contain naive calendar-day midnight values"
        )
    try:
        validate_daily_index(
            pd.DataFrame({"present": [1] * len(index)}, index=index),
            name=name,
        )
        return index.as_unit("ns")
    except DirectionalOHLCCacheIntegrityError:
        raise
    except Exception as exc:
        raise DirectionalOHLCCacheIntegrityError(f"{name} contains an invalid date: {exc}") from exc


def _validated_dates(raw: object) -> pd.DatetimeIndex:
    if not isinstance(raw, list) or not raw:
        raise DirectionalOHLCCacheIntegrityError(
            "directional OHLC cache dates must be a non-empty array"
        )
    parsed: list[date] = []
    for item in raw:
        if not isinstance(item, str):
            raise DirectionalOHLCCacheIntegrityError(
                "directional OHLC cache dates must be ISO strings"
            )
        try:
            value = datetime.strptime(item, "%Y-%m-%d").date()
        except ValueError as exc:
            raise DirectionalOHLCCacheIntegrityError(
                f"invalid directional OHLC cache date: {item!r}"
            ) from exc
        if value.isoformat() != item:
            raise DirectionalOHLCCacheIntegrityError(
                f"invalid directional OHLC cache date: {item!r}"
            )
        parsed.append(value)
    try:
        return _validated_index(parsed, name="directional OHLC cache")
    except DirectionalOHLCCacheIntegrityError:
        raise
    except Exception as exc:  # pragma: no cover - defensive codec boundary
        raise DirectionalOHLCCacheIntegrityError(
            "directional OHLC cache dates cannot be represented"
        ) from exc


def _validated_float64(item: object, *, name: str, location: str) -> float:
    if isinstance(item, bool) or not isinstance(item, Real):
        raise DirectionalOHLCCacheIntegrityError(f"{name} must be finite at {location}")
    try:
        finite = isfinite(item)
        value = float(item)
    except (OverflowError, TypeError, ValueError) as exc:
        raise DirectionalOHLCCacheIntegrityError(f"{name} must be finite at {location}") from exc
    if not finite or not isfinite(value):
        raise DirectionalOHLCCacheIntegrityError(f"{name} must be finite at {location}")
    if isinstance(item, Integral):
        if int(value) != item:
            raise DirectionalOHLCCacheIntegrityError(
                f"{name} value at {location} cannot be losslessly represented as float64"
            )
    elif item != value:
        raise DirectionalOHLCCacheIntegrityError(
            f"{name} value at {location} cannot be losslessly represented as float64"
        )
    if value <= 0:
        raise DirectionalOHLCCacheIntegrityError(f"{name} must be positive at {location}")
    return value


def _validated_matrix(
    raw: object,
    *,
    name: str,
    row_count: int,
    column_count: int,
) -> list[list[float]]:
    if not isinstance(raw, list) or len(raw) != row_count:
        raise DirectionalOHLCCacheIntegrityError(f"{name} row count does not match dates")
    result: list[list[float]] = []
    for row_index, row in enumerate(raw):
        if not isinstance(row, list) or len(row) != column_count:
            raise DirectionalOHLCCacheIntegrityError(
                f"{name} column count is invalid at row {row_index}"
            )
        values: list[float] = []
        for column_index, item in enumerate(row):
            values.append(
                _validated_float64(
                    item,
                    name=name,
                    location=f"row {row_index}, column {column_index}",
                )
            )
        result.append(values)
    return result


def canonicalize_ohlc_frames(
    products: tuple[str, ...],
    open_prices: pd.DataFrame,
    close: pd.DataFrame,
    *,
    name: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Validate one exact panel and return its lossless float64 representation."""
    normalized_products = _normalized_products(products, name=f"{name} products")
    if not isinstance(open_prices, pd.DataFrame) or not isinstance(close, pd.DataFrame):
        raise DirectionalOHLCCacheIntegrityError(f"{name} open/close values must be data frames")
    if (
        tuple(open_prices.columns) != normalized_products
        or tuple(close.columns) != normalized_products
    ):
        raise DirectionalOHLCCacheIntegrityError(f"{name} product set/order does not match frames")
    open_index = _validated_index(open_prices.index, name=f"{name} open")
    close_index = _validated_index(close.index, name=f"{name} close")
    if not open_index.equals(close_index):
        raise DirectionalOHLCCacheIntegrityError(f"{name} open/close indexes must match")
    open_values = _validated_matrix(
        open_prices.to_numpy(dtype=object).tolist(),
        name=f"{name} open",
        row_count=len(open_index),
        column_count=len(normalized_products),
    )
    close_values = _validated_matrix(
        close.to_numpy(dtype=object).tolist(),
        name=f"{name} close",
        row_count=len(close_index),
        column_count=len(normalized_products),
    )
    canonical_open = pd.DataFrame(
        open_values,
        index=open_index,
        columns=normalized_products,
        dtype="float64",
    )
    canonical_close = pd.DataFrame(
        close_values,
        index=close_index,
        columns=normalized_products,
        dtype="float64",
    )
    return canonical_open, canonical_close


@dataclass(frozen=True)
class DirectionalOHLCCacheEntry:
    products: tuple[str, ...]
    open: pd.DataFrame
    close: pd.DataFrame
    content_digest: str

    @property
    def latest_date(self) -> date:
        return pd.Timestamp(self.close.index[-1]).date()

    @property
    def row_count(self) -> int:
        return len(self.close)


class DirectionalOHLCCacheStore:
    """Persist one verified open/close panel with atomic replacement."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._last_creation_token: DurableFileCreationToken | None = None

    @property
    def last_creation_token(self) -> DurableFileCreationToken | None:
        return self._last_creation_token

    def load(
        self,
        expected_products: tuple[str, ...],
    ) -> DirectionalOHLCCacheEntry | None:
        if not self.path.exists():
            return None
        try:
            text = self.path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DirectionalOHLCCacheIntegrityError(
                "invalid directional OHLC cache UTF-8"
            ) from exc
        try:
            raw = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
        except json.JSONDecodeError as exc:
            raise DirectionalOHLCCacheIntegrityError("invalid directional OHLC cache JSON") from exc
        try:
            return self._entry_from_envelope(raw, expected_products)
        except DirectionalOHLCCacheIntegrityError:
            raise
        except Exception as exc:  # malformed content must never escape the codec boundary
            raise DirectionalOHLCCacheIntegrityError(
                "directional OHLC cache cannot be decoded safely"
            ) from exc

    @staticmethod
    def _entry_from_envelope(
        raw: object,
        expected_products: tuple[str, ...],
    ) -> DirectionalOHLCCacheEntry:
        if not isinstance(raw, dict):
            raise DirectionalOHLCCacheIntegrityError(
                "directional OHLC cache envelope must be an object"
            )
        required = {
            "schema_version",
            "kind",
            "content",
            "content_digest",
            "checksum",
        }
        if set(raw) != required:
            raise DirectionalOHLCCacheIntegrityError(
                "directional OHLC cache envelope fields are invalid"
            )
        schema_version = raw["schema_version"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise DirectionalOHLCCacheIntegrityError(
                "directional OHLC cache schema version must be integer"
            )
        if schema_version != OHLC_CACHE_SCHEMA_VERSION:
            raise DirectionalOHLCCacheIntegrityError(
                "directional OHLC cache schema version is unsupported"
            )
        if raw["kind"] != _CACHE_KIND:
            raise DirectionalOHLCCacheIntegrityError("directional OHLC cache kind is invalid")
        content = raw["content"]
        if not isinstance(content, dict) or set(content) != {"products", "dates", "open", "close"}:
            raise DirectionalOHLCCacheIntegrityError(
                "directional OHLC cache content fields are invalid"
            )
        products = _normalized_products(
            content["products"],
            name="directional OHLC cache products",
        )
        expected = _normalized_products(
            expected_products,
            name="expected directional OHLC products",
        )
        if products != expected:
            raise DirectionalOHLCCacheIntegrityError(
                "directional OHLC cache product set/order does not match configuration"
            )
        index = _validated_dates(content["dates"])
        open_values = _validated_matrix(
            content["open"],
            name="directional OHLC cache open",
            row_count=len(index),
            column_count=len(products),
        )
        close_values = _validated_matrix(
            content["close"],
            name="directional OHLC cache close",
            row_count=len(index),
            column_count=len(products),
        )
        expected_content_digest = _digest(content)
        if raw["content_digest"] != expected_content_digest:
            raise DirectionalOHLCCacheIntegrityError(
                "directional OHLC cache content digest mismatch"
            )
        unsigned = {key: value for key, value in raw.items() if key != "checksum"}
        if raw["checksum"] != _digest(unsigned):
            raise DirectionalOHLCCacheIntegrityError("directional OHLC cache checksum mismatch")
        open_prices = pd.DataFrame(open_values, index=index, columns=products)
        close = pd.DataFrame(close_values, index=index, columns=products)
        if not open_prices.index.equals(close.index):
            raise DirectionalOHLCCacheIntegrityError(
                "directional OHLC cache open/close indexes must match"
            )
        return DirectionalOHLCCacheEntry(
            products=products,
            open=open_prices,
            close=close,
            content_digest=expected_content_digest,
        )

    def save(
        self,
        products: tuple[str, ...],
        open_prices: pd.DataFrame,
        close: pd.DataFrame,
    ) -> DirectionalOHLCCacheEntry:
        envelope, encoded = self._encoded_envelope(products, open_prices, close)
        with durable_file_lock(self.path):
            self._replace_encoded(encoded)
        return self._entry_from_envelope(envelope, products)

    def save_new(
        self,
        products: tuple[str, ...],
        open_prices: pd.DataFrame,
        close: pd.DataFrame,
    ) -> tuple[DirectionalOHLCCacheEntry, DurableFileCreationToken]:
        """Create the first cache record without overwriting concurrent evidence."""

        envelope, encoded = self._encoded_envelope(products, open_prices, close)
        try:
            token = create_durable_file_exclusive(self.path, encoded)
        except FileExistsError as exc:
            raise DirectionalOHLCCacheIntegrityError(
                "directional OHLC cache appeared concurrently"
            ) from exc
        self._last_creation_token = token
        return self._entry_from_envelope(envelope, products), token

    def _encoded_envelope(
        self,
        products: tuple[str, ...],
        open_prices: pd.DataFrame,
        close: pd.DataFrame,
    ) -> tuple[dict[str, object], bytes]:
        content = self._content_from_frames(products, open_prices, close)
        content_digest = _digest(content)
        unsigned: dict[str, object] = {
            "schema_version": OHLC_CACHE_SCHEMA_VERSION,
            "kind": _CACHE_KIND,
            "content": content,
            "content_digest": content_digest,
        }
        envelope = {**unsigned, "checksum": _digest(unsigned)}
        return envelope, _canonical_json(envelope)

    def _replace_encoded(self, encoded: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        temp: Path | None = None
        try:
            with NamedTemporaryFile("wb", dir=self.path.parent, delete=False) as handle:
                temp = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(self.path)
        finally:
            if temp is not None and temp.exists():
                temp.unlink()

    @staticmethod
    def _content_from_frames(
        products: tuple[str, ...],
        open_prices: pd.DataFrame,
        close: pd.DataFrame,
    ) -> dict[str, object]:
        normalized_products = _normalized_products(products, name="directional OHLC cache products")
        canonical_open, canonical_close = canonicalize_ohlc_frames(
            normalized_products,
            open_prices,
            close,
            name="directional OHLC cache",
        )
        return {
            "products": list(normalized_products),
            "dates": [item.date().isoformat() for item in canonical_open.index],
            "open": canonical_open.to_numpy().tolist(),
            "close": canonical_close.to_numpy().tolist(),
        }


def require_unchanged_overlap(
    cached: DirectionalOHLCCacheEntry,
    open_prices: pd.DataFrame,
    close: pd.DataFrame,
) -> None:
    """Reject a provider response that silently revises cached historical values."""
    missing_open = cached.open.index.difference(open_prices.index)
    missing_close = cached.close.index.difference(close.index)
    if not missing_open.empty or not missing_close.empty:
        raise DirectionalOHLCCacheIntegrityError(
            "directional OHLC provider history dropped dates from the verified cache"
        )
    products = list(cached.products)
    cached_open = cached.open.loc[:, products]
    cached_close = cached.close.loc[:, products]
    provider_open = open_prices.loc[cached.open.index, products]
    provider_close = close.loc[cached.close.index, products]
    if not cached_open.equals(provider_open) or not cached_close.equals(provider_close):
        raise DirectionalOHLCCacheIntegrityError(
            "directional OHLC provider revised values overlapping the verified cache"
        )
