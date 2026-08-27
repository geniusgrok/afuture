"""Atomic, checksummed market-input evidence for directional OHLC history.

This sidecar never stores account, order, fill, position, or strategy state.  It is only
the last provider history whose complete open/close panel passed the live input contract.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from math import isfinite
from numbers import Integral, Real
from pathlib import Path

import pandas as pd

from .directional_data_validation import validate_daily_index
from .durable_file_creation import (
    DurableFileCreationToken,
    canonical_file_path,
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
    """Persist one verified panel through a locked, witnessed inode commit."""

    def __init__(self, path: str | Path) -> None:
        self.path = canonical_file_path(path)
        self._last_creation_token: DurableFileCreationToken | None = None

    @property
    def last_creation_token(self) -> DurableFileCreationToken | None:
        return self._last_creation_token

    @property
    def pending_path(self) -> Path:
        return self.path.with_name(self.path.name + ".pending")

    @property
    def cleanup_guard_path(self) -> Path:
        return self.pending_path.with_name(self.pending_path.name + ".cleanup")

    @property
    def terminal_guard_path(self) -> Path:
        return self.pending_path.with_name(self.pending_path.name + ".terminal")

    @property
    def completion_path(self) -> Path:
        return self.pending_path.with_name(self.pending_path.name + ".complete")

    @contextmanager
    def authority(self) -> Iterator[Path]:
        """Hold canonical ownership from path validation through durable commit."""

        with durable_file_lock(self.path) as canonical:
            if canonical != self.path:
                raise DirectionalOHLCCacheIntegrityError(
                    "directional OHLC cache canonical path changed"
                )
            self._require_safe_paths_unlocked()
            yield canonical

    def _require_safe_paths_unlocked(self) -> None:
        for witness_path in (
            self.pending_path,
            self.cleanup_guard_path,
            self.terminal_guard_path,
        ):
            try:
                pending = os.lstat(witness_path)
            except FileNotFoundError:
                pending = None
            except OSError as exc:
                raise DirectionalOHLCCacheIntegrityError(
                    "directional OHLC cache pending witness cannot be inspected"
                ) from exc
            if pending is not None:
                raise DirectionalOHLCCacheIntegrityError(
                    "directional OHLC cache has a pending mutation witness"
                )
        try:
            current = os.lstat(self.path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise DirectionalOHLCCacheIntegrityError(
                "directional OHLC cache path cannot be inspected"
            ) from exc
        if stat.S_ISLNK(current.st_mode):
            raise DirectionalOHLCCacheIntegrityError(
                "directional OHLC cache final path is a symlink"
            )
        if not stat.S_ISREG(current.st_mode):
            raise DirectionalOHLCCacheIntegrityError(
                "directional OHLC cache final path is not a regular file"
            )

    def load(
        self,
        expected_products: tuple[str, ...],
    ) -> DirectionalOHLCCacheEntry | None:
        with self.authority():
            return self.load_unlocked(expected_products)

    def load_unlocked(
        self,
        expected_products: tuple[str, ...],
    ) -> DirectionalOHLCCacheEntry | None:
        """Decode current while the caller already holds this artifact lock."""

        entry, descriptor = self.load_for_update_unlocked(
            expected_products,
            writable=False,
        )
        if descriptor is not None:
            os.close(descriptor)
        return entry

    def load_for_update_unlocked(
        self,
        expected_products: tuple[str, ...],
        *,
        writable: bool = True,
    ) -> tuple[DirectionalOHLCCacheEntry | None, int | None]:
        """Open and retain the exact current inode while ownership is held."""

        self._require_safe_paths_unlocked()
        flags = (
            (os.O_RDWR if writable else os.O_RDONLY)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(self.path, flags)
        except FileNotFoundError:
            return None, None
        except OSError as exc:
            raise DirectionalOHLCCacheIntegrityError(
                "directional OHLC cache cannot be opened safely"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            visible = os.stat(self.path, follow_symlinks=False)
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(visible.st_mode)
                or (opened.st_dev, opened.st_ino) != (visible.st_dev, visible.st_ino)
            ):
                raise DirectionalOHLCCacheIntegrityError(
                    "directional OHLC cache inode identity changed"
                )
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            os.lseek(descriptor, 0, os.SEEK_SET)
            text = b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            os.close(descriptor)
            raise DirectionalOHLCCacheIntegrityError(
                "invalid directional OHLC cache UTF-8"
            ) from exc
        except BaseException:
            os.close(descriptor)
            raise
        try:
            raw = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
        except json.JSONDecodeError as exc:
            os.close(descriptor)
            raise DirectionalOHLCCacheIntegrityError("invalid directional OHLC cache JSON") from exc
        try:
            return self._entry_from_envelope(raw, expected_products), descriptor
        except DirectionalOHLCCacheIntegrityError:
            os.close(descriptor)
            raise
        except Exception as exc:  # malformed content must never escape the codec boundary
            os.close(descriptor)
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
        with self.authority():
            _existing, descriptor = self.load_for_update_unlocked(products)
            try:
                self.commit_encoded_unlocked(encoded, existing_descriptor=descriptor)
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        return self._entry_from_envelope(envelope, products)

    def save_new(
        self,
        products: tuple[str, ...],
        open_prices: pd.DataFrame,
        close: pd.DataFrame,
    ) -> tuple[DirectionalOHLCCacheEntry, DurableFileCreationToken]:
        """Create the first cache record without overwriting concurrent evidence."""

        envelope, encoded = self._encoded_envelope(products, open_prices, close)
        with self.authority():
            existing, descriptor = self.load_for_update_unlocked(products)
            if descriptor is not None:
                os.close(descriptor)
            if existing is not None:
                raise DirectionalOHLCCacheIntegrityError(
                    "directional OHLC cache appeared concurrently"
                )
            token = self.commit_encoded_unlocked(encoded, existing_descriptor=None)
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

    def commit_encoded_unlocked(
        self,
        encoded: bytes,
        *,
        existing_descriptor: int | None,
    ) -> DurableFileCreationToken:
        """Commit to one exact fd/create and preserve ambiguity as `.pending`."""

        self._require_safe_paths_unlocked()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        witness_payload = _canonical_json(
            {
                "kind": "afuture.directional-ohlc-pending",
                "schema_version": 1,
                "cache_path": str(self.path),
                "payload_sha256": sha256(encoded).hexdigest(),
            }
        )
        witness_identity = self._create_pending_unlocked(witness_payload)
        descriptor = existing_descriptor
        created_descriptor = False
        mutation_started = False
        try:
            if descriptor is None:
                try:
                    descriptor = os.open(
                        self.path,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                    )
                except FileExistsError as exc:
                    self._cleanup_pending_unlocked(witness_identity, witness_payload)
                    raise DirectionalOHLCCacheIntegrityError(
                        "directional OHLC cache appeared concurrently"
                    ) from exc
                created_descriptor = True
                mutation_started = True
            else:
                self._require_descriptor_current_unlocked(descriptor)
                mutation_started = True
                os.ftruncate(descriptor, 0)
                os.lseek(descriptor, 0, os.SEEK_SET)
            self._write_all(descriptor, encoded)
            os.fsync(descriptor)
            self._require_descriptor_current_unlocked(descriptor)
            self._fsync_parent()
            opened = os.fstat(descriptor)
            token = DurableFileCreationToken(
                path=self.path,
                device=opened.st_dev,
                inode=opened.st_ino,
                size=len(encoded),
                payload_sha256=sha256(encoded).hexdigest(),
            )
            self._cleanup_pending_unlocked(witness_identity, witness_payload)
            self._last_creation_token = token
            return token
        except DirectionalOHLCCacheIntegrityError:
            raise
        except BaseException as exc:
            detail = "after mutation" if mutation_started else "before mutation"
            raise DirectionalOHLCCacheIntegrityError(
                f"directional OHLC cache commit failed {detail}; pending witness preserved"
            ) from exc
        finally:
            if created_descriptor and descriptor is not None:
                os.close(descriptor)

    def _create_pending_unlocked(self, payload: bytes) -> tuple[int, int]:
        try:
            return self._create_witness_unlocked(self.pending_path, payload)
        except FileExistsError as exc:
            raise DirectionalOHLCCacheIntegrityError(
                "directional OHLC cache has a pending mutation witness"
            ) from exc
        except BaseException as exc:
            raise DirectionalOHLCCacheIntegrityError(
                "directional OHLC pending witness creation failed; witness preserved"
            ) from exc

    def _create_witness_unlocked(
        self,
        path: Path,
        payload: bytes,
    ) -> tuple[int, int]:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise OSError("pending witness is not regular")
            self._write_all(descriptor, payload)
            os.fsync(descriptor)
            visible = os.stat(path, follow_symlinks=False)
            if not stat.S_ISREG(visible.st_mode) or (visible.st_dev, visible.st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ):
                raise OSError("pending witness identity changed")
            self._fsync_parent()
            return opened.st_dev, opened.st_ino
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _cleanup_pending_unlocked(
        self,
        identity: tuple[int, int],
        payload: bytes,
    ) -> None:
        try:
            self._require_witness_identity_unlocked(
                self.pending_path,
                identity,
                "pending witness",
            )
            os.link(
                self.pending_path,
                self.cleanup_guard_path,
                follow_symlinks=False,
            )
            self._require_witness_identity_unlocked(
                self.cleanup_guard_path,
                identity,
                "pending cleanup guard",
            )
            self._fsync_parent()
            os.unlink(self.pending_path)
            self._fsync_parent()
            os.link(
                self.cleanup_guard_path,
                self.terminal_guard_path,
                follow_symlinks=False,
            )
            self._require_witness_identity_unlocked(
                self.terminal_guard_path,
                identity,
                "pending terminal guard",
            )
            self._fsync_parent()
            os.unlink(self.cleanup_guard_path)
            self._fsync_parent()
            os.replace(self.terminal_guard_path, self.completion_path)
            self._require_witness_identity_unlocked(
                self.completion_path,
                identity,
                "pending completion receipt",
            )
            self._fsync_parent()
        except BaseException as exc:
            try:
                blocker_exists = self._blocking_witness_exists_unlocked()
            except BaseException as inspection_exc:
                failure = DirectionalOHLCCacheIntegrityError(
                    "directional OHLC pending witness cleanup restoration failed "
                    f"after cleanup failure: {exc!r}"
                )
                raise failure from inspection_exc
            if not blocker_exists:
                try:
                    self._create_witness_unlocked(self.cleanup_guard_path, payload)
                except BaseException as restoration_exc:
                    failure = DirectionalOHLCCacheIntegrityError(
                        "directional OHLC pending witness cleanup restoration failed "
                        f"after cleanup failure: {exc!r}"
                    )
                    raise failure from restoration_exc
            raise DirectionalOHLCCacheIntegrityError(
                "directional OHLC pending witness cleanup failed; witness preserved"
            ) from exc

    def _require_witness_identity_unlocked(
        self,
        path: Path,
        identity: tuple[int, int],
        label: str,
    ) -> None:
        visible = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(visible.st_mode) or (visible.st_dev, visible.st_ino) != identity:
            raise OSError(f"{label} identity changed")

    def _blocking_witness_exists_unlocked(self) -> bool:
        for path in (
            self.pending_path,
            self.cleanup_guard_path,
            self.terminal_guard_path,
        ):
            try:
                os.lstat(path)
            except FileNotFoundError:
                continue
            return True
        return False

    def _require_descriptor_current_unlocked(self, descriptor: int) -> None:
        opened = os.fstat(descriptor)
        visible = os.stat(self.path, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(visible.st_mode)
            or (opened.st_dev, opened.st_ino) != (visible.st_dev, visible.st_ino)
        ):
            raise DirectionalOHLCCacheIntegrityError(
                "directional OHLC cache inode identity changed"
            )

    def _fsync_parent(self) -> None:
        descriptor = os.open(
            self.path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _write_all(descriptor: int, payload: bytes) -> None:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("directional OHLC write made no progress")
            offset += written

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
