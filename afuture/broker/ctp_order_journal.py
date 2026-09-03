"""Crash-safe exact CTP submission identities for Stress-90 orders."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import secrets
import stat
import struct
from base64 import b64decode, b64encode
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from math import isfinite
from pathlib import Path
from types import MappingProxyType

from ..models import Offset, OrderRequest, OrderSide, OrderType, Trade

_KIND = "afuture.ctp.order-submission-journal"
_SCHEMA_VERSION = 4
_GENERATION_KIND = "afuture.ctp.order-submission-journal.generation"
_GENERATION_SCHEMA_VERSION = 2
_ARCHIVE_KIND = "afuture.ctp.order-submission-journal.archive-segment"
_ARCHIVE_SCHEMA_VERSION = 1
_RUNTIME_INDEX_KIND = "afuture.ctp.order-submission-journal.runtime-index"
_RUNTIME_INDEX_SCHEMA_VERSION = 1
_MAX_ENTRIES = 10_000
_ARCHIVE_SEGMENT_MAX_ENTRIES = 1_000
_RUNTIME_RECENT_DAYS = 2
_RUNTIME_RECENT_MAX_ENTRIES = 10_000
CTP_ORDER_RUNTIME_MAX_FILL_IDENTITIES = 10_000
_ORDER_ID_BLOOM_BITS = 2_097_152
_ORDER_ID_BLOOM_HASHES = 7
_ORDER_ID_BLOOM_MAX_ITEMS = 20_000
_EPOCH_MANIFEST_KIND = "afuture.ctp.order-journal-epoch-manifest"
_EPOCH_MANIFEST_SCHEMA_VERSION = 2
_EPOCH_SEAL_KIND = "afuture.ctp.order-journal-epoch-seal"
_EPOCH_SEAL_SCHEMA_VERSION = 2
_MAX_SEALED_EPOCHS = 128
CTP_ORDER_JOURNAL_EPOCH_CONFIRMATION = "I_CONFIRM_STRESS90_CTP_ORDER_JOURNAL_EPOCH_ROLLOVER"
_F_OFD_SETLKW = getattr(fcntl, "F_OFD_SETLKW", 38)
_SHA = re.compile(r"[0-9a-f]{64}")
_EXCHANGE = re.compile(r"[A-Z][A-Z0-9]*")
_TRADE_ID = re.compile(r"[A-Za-z0-9_.-]+")
_JOURNAL_STATUSES = {"prepared", "submitted", "terminal", "aborted_before_send"}
_FINAL_STATUSES = {"terminal", "aborted_before_send"}


class CtpOrderJournalIntegrityError(RuntimeError):
    pass


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
        raise CtpOrderJournalIntegrityError("CTP order journal is not canonical JSON") from exc


def _digest(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CtpOrderJournalIntegrityError(f"duplicate CTP order journal JSON key: {key}")
        result[key] = value
    return result


def _sha(raw: object, name: str) -> str:
    if not isinstance(raw, str) or _SHA.fullmatch(raw) is None:
        raise CtpOrderJournalIntegrityError(f"{name} must be SHA-256")
    return raw


def _day(raw: object) -> str:
    if not isinstance(raw, str):
        raise CtpOrderJournalIntegrityError("CTP order target day must be YYYYMMDD")
    try:
        parsed = datetime.strptime(raw, "%Y%m%d").strftime("%Y%m%d")
    except ValueError as exc:
        raise CtpOrderJournalIntegrityError("CTP order target day must be YYYYMMDD") from exc
    if parsed != raw:
        raise CtpOrderJournalIntegrityError("CTP order target day must be YYYYMMDD")
    return raw


def _positive_int(raw: object, name: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise CtpOrderJournalIntegrityError(f"{name} must be a positive integer")
    return raw


def _nonnegative_int(raw: object, name: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise CtpOrderJournalIntegrityError(f"{name} must be a non-negative integer")
    return raw


def _fill_key(raw: object) -> str:
    if not isinstance(raw, str):
        raise CtpOrderJournalIntegrityError("CTP order journal fill key is invalid")
    parts = raw.split(":", 2)
    if (
        len(parts) != 3
        or _day(parts[0]) != parts[0]
        or _EXCHANGE.fullmatch(parts[1]) is None
        or _TRADE_ID.fullmatch(parts[2]) is None
    ):
        raise CtpOrderJournalIntegrityError("CTP order journal fill key is invalid")
    return raw


def _empty_order_identity_bloom() -> bytes:
    if (
        isinstance(_ORDER_ID_BLOOM_BITS, bool)
        or not isinstance(_ORDER_ID_BLOOM_BITS, int)
        or _ORDER_ID_BLOOM_BITS <= 0
        or _ORDER_ID_BLOOM_BITS % 8
        or isinstance(_ORDER_ID_BLOOM_HASHES, bool)
        or not isinstance(_ORDER_ID_BLOOM_HASHES, int)
        or _ORDER_ID_BLOOM_HASHES <= 0
    ):
        raise CtpOrderJournalIntegrityError("CTP order identity Bloom configuration is invalid")
    return bytes(_ORDER_ID_BLOOM_BITS // 8)


def _order_identity_bloom_positions(order_id: str) -> tuple[int, ...]:
    if not isinstance(order_id, str) or not order_id:
        raise CtpOrderJournalIntegrityError("CTP order identity is invalid")
    payload = order_id.encode("utf-8")
    first = int.from_bytes(sha256(b"ctp-order-id-bloom:0:" + payload).digest()[:8], "big")
    second = int.from_bytes(sha256(b"ctp-order-id-bloom:1:" + payload).digest()[:8], "big")
    second |= 1
    return tuple(
        (first + index * second) % _ORDER_ID_BLOOM_BITS for index in range(_ORDER_ID_BLOOM_HASHES)
    )


def _order_identity_bloom_contains(bloom: bytes, order_id: str) -> bool:
    if len(bloom) != len(_empty_order_identity_bloom()):
        raise CtpOrderJournalIntegrityError("CTP order identity Bloom length is invalid")
    return all(
        bloom[position // 8] & (1 << (position % 8))
        for position in _order_identity_bloom_positions(order_id)
    )


def _order_identity_bloom_add(bloom: bytes, order_ids: tuple[str, ...]) -> bytes:
    if len(bloom) != len(_empty_order_identity_bloom()):
        raise CtpOrderJournalIntegrityError("CTP order identity Bloom length is invalid")
    updated = bytearray(bloom)
    for order_id in order_ids:
        for position in _order_identity_bloom_positions(order_id):
            updated[position // 8] |= 1 << (position % 8)
    return bytes(updated)


def _request_payload(request: OrderRequest) -> dict[str, object]:
    return {
        "symbol": request.symbol,
        "exchange": request.exchange,
        "side": request.side.value,
        "offset": request.offset.value,
        "volume": request.volume,
        "price": request.price,
        "order_type": request.order_type.value,
        "reference": request.reference,
    }


def _request(raw: object) -> OrderRequest:
    fields = {
        "symbol",
        "exchange",
        "side",
        "offset",
        "volume",
        "price",
        "order_type",
        "reference",
    }
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise CtpOrderJournalIntegrityError("CTP order journal request is invalid")
    try:
        request = OrderRequest(
            symbol=str(raw["symbol"]),
            exchange=str(raw["exchange"]),
            side=OrderSide(str(raw["side"])),
            offset=Offset(str(raw["offset"])),
            volume=_positive_int(raw["volume"], "CTP order volume"),
            price=float(raw["price"]),
            order_type=OrderType(str(raw["order_type"])),
            reference=str(raw["reference"]),
        )
    except (TypeError, ValueError) as exc:
        raise CtpOrderJournalIntegrityError("CTP order journal request is invalid") from exc
    if (
        not request.symbol
        or not request.exchange
        or not request.reference
        or not isfinite(request.price)
        or request.price <= 0.0
    ):
        raise CtpOrderJournalIntegrityError("CTP order journal request is invalid")
    return request


def _transition(raw: object) -> Mapping[str, object]:
    if not isinstance(raw, Mapping) or set(raw) != {
        "freeze_authorized_lots",
        "transitions",
    }:
        raise CtpOrderJournalIntegrityError("CTP order journal transition is invalid")
    canonical = json.loads(_canonical(dict(raw)).decode("utf-8"))
    authorized = canonical.get("freeze_authorized_lots")
    transitions = canonical.get("transitions")
    if (
        not isinstance(authorized, dict)
        or any(
            not isinstance(symbol, str)
            or not symbol
            or isinstance(volume, bool)
            or not isinstance(volume, int)
            or volume == 0
            for symbol, volume in authorized.items()
        )
        or not isinstance(transitions, list)
    ):
        raise CtpOrderJournalIntegrityError("CTP order journal transition is invalid")
    transition_fields = {
        "product",
        "kind",
        "source_symbols",
        "target_symbol",
        "target_sign",
        "max_replacement_notional",
    }
    seen_products: set[str] = set()
    for item in transitions:
        if not isinstance(item, dict) or set(item) != transition_fields:
            raise CtpOrderJournalIntegrityError("CTP order journal transition is invalid")
        product = item["product"]
        kind = item["kind"]
        sources = item["source_symbols"]
        target_symbol = item["target_symbol"]
        target_sign = item["target_sign"]
        replacement = item["max_replacement_notional"]
        if (
            not isinstance(product, str)
            or re.fullmatch(r"[A-Z][A-Z0-9]*", product) is None
            or product in seen_products
            or kind not in {"same_product_roll", "reversal_open"}
            or not isinstance(sources, list)
            or not sources
            or any(not isinstance(symbol, str) or not symbol for symbol in sources)
            or sources != sorted(set(sources))
            or not isinstance(target_symbol, str)
            or target_symbol not in authorized
            or isinstance(target_sign, bool)
            or target_sign not in {-1, 1}
            or (authorized[target_symbol] > 0) != (target_sign > 0)
            or isinstance(replacement, bool)
            or not isinstance(replacement, (int, float))
            or not isfinite(float(replacement))
            or float(replacement) <= 0.0
        ):
            raise CtpOrderJournalIntegrityError("CTP order journal transition is invalid")
        seen_products.add(product)
    if [item["product"] for item in transitions] != sorted(seen_products):
        raise CtpOrderJournalIntegrityError("CTP order journal transition is not canonical")
    return MappingProxyType(canonical)


@dataclass(frozen=True)
class CtpOrderSubmissionContext:
    target_trading_day: str
    daily_decision_digest: str
    execution_intent_digest: str
    transition: Mapping[str, object]


@dataclass(frozen=True)
class CtpOrderFillEvidence:
    """Immutable per-fill economics bound to one global CTP trade identity."""

    key: str
    order_id: str
    symbol: str
    exchange: str
    side: OrderSide
    offset: Offset
    volume: int
    price: float
    timestamp: str


def _fill_evidence_payload(evidence: CtpOrderFillEvidence) -> dict[str, object]:
    return {
        "key": evidence.key,
        "order_id": evidence.order_id,
        "symbol": evidence.symbol,
        "exchange": evidence.exchange,
        "side": evidence.side.value,
        "offset": evidence.offset.value,
        "volume": evidence.volume,
        "price": evidence.price,
        "timestamp": evidence.timestamp,
    }


def _timestamp(raw: object) -> str:
    if not isinstance(raw, str) or not raw:
        raise CtpOrderJournalIntegrityError("CTP order fill timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise CtpOrderJournalIntegrityError("CTP order fill timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CtpOrderJournalIntegrityError("CTP order fill timestamp is invalid")
    return parsed.isoformat(timespec="microseconds")


def _fill_evidence(raw: object) -> CtpOrderFillEvidence:
    fields = {
        "key",
        "order_id",
        "symbol",
        "exchange",
        "side",
        "offset",
        "volume",
        "price",
        "timestamp",
    }
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise CtpOrderJournalIntegrityError("CTP order fill evidence is invalid")
    order_id = raw["order_id"]
    symbol = raw["symbol"]
    exchange = raw["exchange"]
    try:
        side = OrderSide(str(raw["side"]))
        offset = Offset(str(raw["offset"]))
        price = float(raw["price"])
    except (TypeError, ValueError) as exc:
        raise CtpOrderJournalIntegrityError("CTP order fill evidence is invalid") from exc
    if (
        not isinstance(order_id, str)
        or not order_id
        or not isinstance(symbol, str)
        or not symbol
        or not isinstance(exchange, str)
        or _EXCHANGE.fullmatch(exchange) is None
        or not isfinite(price)
        or price <= 0.0
    ):
        raise CtpOrderJournalIntegrityError("CTP order fill evidence is invalid")
    key = _fill_key(raw["key"])
    if key.split(":", 2)[1] != exchange:
        raise CtpOrderJournalIntegrityError("CTP order fill key/economic exchange mismatch")
    return CtpOrderFillEvidence(
        key=key,
        order_id=order_id,
        symbol=symbol,
        exchange=exchange,
        side=side,
        offset=offset,
        volume=_positive_int(raw["volume"], "CTP order fill volume"),
        price=price,
        timestamp=_timestamp(raw["timestamp"]),
    )


def ctp_order_fill_evidence(trading_day: str, trade: Trade) -> CtpOrderFillEvidence:
    """Canonicalize one validated Broker trade for persistence and replay checks."""

    try:
        trade.validate()
    except (AttributeError, TypeError, ValueError) as exc:
        raise CtpOrderJournalIntegrityError("CTP order fill evidence is invalid") from exc
    day = _day(trading_day)
    return _fill_evidence(
        {
            "key": f"{day}:{trade.exchange}:{trade.trade_id}",
            "order_id": trade.order_id,
            "symbol": trade.symbol,
            "exchange": trade.exchange,
            "side": trade.side.value,
            "offset": trade.offset.value,
            "volume": trade.volume,
            "price": trade.price,
            "timestamp": trade.timestamp.isoformat(timespec="microseconds"),
        }
    )


def coerce_ctp_order_fill_evidence(raw: object) -> CtpOrderFillEvidence:
    """Validate persisted/adversarial evidence through the production decoder."""

    if isinstance(raw, CtpOrderFillEvidence):
        raw = _fill_evidence_payload(raw)
    return _fill_evidence(raw)


@dataclass(frozen=True)
class CtpOrderSubmissionEntry:
    sequence: int
    account_identity_digest: str
    policy_id: str
    policy_definition_digest: str
    products_manifest_digest: str
    target_trading_day: str
    daily_decision_digest: str
    execution_intent_digest: str
    transition: Mapping[str, object]
    front_id: int
    session_id: int
    order_ref: int
    order_id: str
    request: OrderRequest
    status: str
    authorization_kind: str = "candidate"
    filled_volume: int = 0
    fill_keys: tuple[str, ...] = ()
    fill_evidence: tuple[CtpOrderFillEvidence, ...] = ()


@dataclass(frozen=True)
class CtpOrderSubmissionJournalRecord:
    sequence: int
    entries: tuple[CtpOrderSubmissionEntry, ...]
    archived_entries: tuple[CtpOrderSubmissionEntry, ...]
    parent_checksum: str | None
    checksum: str
    archive_entry_count: int = 0
    archive_complete: bool = True
    archive_head_checksum: str | None = None
    runtime_index_checksum: str | None = None
    order_identity_count: int = 0
    order_identity_bloom_digest: str | None = None

    @property
    def all_entries(self) -> tuple[CtpOrderSubmissionEntry, ...]:
        return tuple(
            sorted((*self.archived_entries, *self.entries), key=lambda item: item.sequence)
        )


@dataclass(frozen=True)
class _ArchivePointer:
    segment_sequence: int
    filename: str
    checksum: str


@dataclass(frozen=True)
class _RuntimeIndexPointer:
    filename: str
    checksum: str


@dataclass(frozen=True)
class _RuntimeArchiveIndex:
    archive_head: _ArchivePointer
    archive_entry_count: int
    policy_identity: tuple[str, str, str, str]
    recent_entries: tuple[CtpOrderSubmissionEntry, ...]
    order_identity_count: int
    order_identity_bloom: bytes
    checksum: str


@dataclass(frozen=True)
class CtpOrderJournalSealedEpoch:
    transaction_id: str
    source_account_identity_digest: str
    target_account_identity_digest: str
    trading_day: str
    operator_reason: str
    directory_name: str
    source_files: tuple[tuple[str, str], ...]
    journal_sequence: int
    journal_checksum: str
    archive_head_checksum: str
    archive_entry_count: int
    entry_count: int
    order_identity_count: int
    order_identity_bloom: bytes
    fill_identity_count: int
    fill_identity_bloom: bytes


@dataclass(frozen=True)
class CtpOrderJournalEpochManifest:
    sequence: int
    parent_checksum: str | None
    current_account_identity_digest: str
    sealed_epochs: tuple[CtpOrderJournalSealedEpoch, ...]
    pending_cleanup_epoch_id: str
    checksum: str


@dataclass(frozen=True)
class CtpOrderJournalEpochAudit:
    manifest: CtpOrderJournalEpochManifest | None
    current_record: CtpOrderSubmissionJournalRecord | None
    sealed_epoch_count: int
    sealed_entry_count: int
    sealed_fill_identity_count: int


@dataclass(frozen=True)
class CtpOrderJournalLifecycleView:
    entries: tuple[CtpOrderSubmissionEntry, ...]
    current_order_ids: frozenset[str]


def _epoch_reason(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip() or len(raw.strip()) > 1_000:
        raise CtpOrderJournalIntegrityError("CTP order journal epoch reason is invalid")
    return raw.strip()


def _sealed_epoch_payload(epoch: CtpOrderJournalSealedEpoch) -> dict[str, object]:
    return {
        "transaction_id": epoch.transaction_id,
        "source_account_identity_digest": epoch.source_account_identity_digest,
        "target_account_identity_digest": epoch.target_account_identity_digest,
        "trading_day": epoch.trading_day,
        "operator_reason": epoch.operator_reason,
        "directory_name": epoch.directory_name,
        "source_files": [
            {"filename": filename, "digest": digest} for filename, digest in epoch.source_files
        ],
        "journal_sequence": epoch.journal_sequence,
        "journal_checksum": epoch.journal_checksum,
        "archive_head_checksum": epoch.archive_head_checksum,
        "archive_entry_count": epoch.archive_entry_count,
        "entry_count": epoch.entry_count,
        "order_identity_count": epoch.order_identity_count,
        "order_identity_bloom": b64encode(epoch.order_identity_bloom).decode("ascii"),
        "fill_identity_count": epoch.fill_identity_count,
        "fill_identity_bloom": b64encode(epoch.fill_identity_bloom).decode("ascii"),
    }


def _sealed_epoch(raw: object) -> CtpOrderJournalSealedEpoch:
    expected = {
        "transaction_id",
        "source_account_identity_digest",
        "target_account_identity_digest",
        "trading_day",
        "operator_reason",
        "directory_name",
        "source_files",
        "journal_sequence",
        "journal_checksum",
        "archive_head_checksum",
        "archive_entry_count",
        "entry_count",
        "order_identity_count",
        "order_identity_bloom",
        "fill_identity_count",
        "fill_identity_bloom",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise CtpOrderJournalIntegrityError("CTP order journal sealed epoch is invalid")
    transaction_id = _sha(raw["transaction_id"], "CTP order journal epoch transaction")
    directory_name = raw["directory_name"]
    if (
        directory_name != f"epoch-{transaction_id}"
        or Path(str(directory_name)).name != directory_name
    ):
        raise CtpOrderJournalIntegrityError("CTP order journal epoch directory is invalid")
    raw_files = raw["source_files"]
    if not isinstance(raw_files, list):
        raise CtpOrderJournalIntegrityError("CTP order journal epoch source files are invalid")
    files: list[tuple[str, str]] = []
    for item in raw_files:
        if not isinstance(item, Mapping) or set(item) != {"filename", "digest"}:
            raise CtpOrderJournalIntegrityError("CTP order journal epoch source files are invalid")
        filename = item["filename"]
        if not isinstance(filename, str) or not filename or Path(filename).name != filename:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal epoch source filename is invalid"
            )
        files.append((filename, _sha(item["digest"], "CTP order journal epoch file")))
    if tuple(files) != tuple(sorted(set(files))):
        raise CtpOrderJournalIntegrityError("CTP order journal epoch source files are invalid")
    encoded_bloom = raw["order_identity_bloom"]
    if not isinstance(encoded_bloom, str):
        raise CtpOrderJournalIntegrityError("CTP order journal epoch Bloom is invalid")
    try:
        bloom = b64decode(encoded_bloom, validate=True)
    except (ValueError, TypeError) as exc:
        raise CtpOrderJournalIntegrityError("CTP order journal epoch Bloom is invalid") from exc
    if len(bloom) != len(_empty_order_identity_bloom()):
        raise CtpOrderJournalIntegrityError("CTP order journal epoch Bloom is invalid")
    encoded_fill_bloom = raw["fill_identity_bloom"]
    if not isinstance(encoded_fill_bloom, str):
        raise CtpOrderJournalIntegrityError("CTP order journal epoch fill Bloom is invalid")
    try:
        fill_bloom = b64decode(encoded_fill_bloom, validate=True)
    except (ValueError, TypeError) as exc:
        raise CtpOrderJournalIntegrityError(
            "CTP order journal epoch fill Bloom is invalid"
        ) from exc
    if len(fill_bloom) != len(_empty_order_identity_bloom()):
        raise CtpOrderJournalIntegrityError("CTP order journal epoch fill Bloom is invalid")
    journal_sequence = _nonnegative_int(raw["journal_sequence"], "CTP sealed journal sequence")
    journal_checksum = raw["journal_checksum"]
    archive_head_checksum = raw["archive_head_checksum"]
    if journal_sequence == 0:
        if journal_checksum != "" or archive_head_checksum != "" or files:
            raise CtpOrderJournalIntegrityError("empty CTP order journal epoch is invalid")
    else:
        journal_checksum = _sha(journal_checksum, "CTP sealed journal")
        if archive_head_checksum != "":
            archive_head_checksum = _sha(archive_head_checksum, "CTP sealed archive head")
    entry_count = _nonnegative_int(raw["entry_count"], "CTP sealed entry count")
    identity_count = _nonnegative_int(
        raw["order_identity_count"], "CTP sealed order identity count"
    )
    if identity_count != entry_count or identity_count > _ORDER_ID_BLOOM_MAX_ITEMS:
        raise CtpOrderJournalIntegrityError("CTP order journal epoch identity count is invalid")
    fill_identity_count = _nonnegative_int(
        raw["fill_identity_count"], "CTP sealed fill identity count"
    )
    if fill_identity_count > CTP_ORDER_RUNTIME_MAX_FILL_IDENTITIES:
        raise CtpOrderJournalIntegrityError(
            "CTP order journal epoch fill identity count is invalid"
        )
    empty_bloom = _empty_order_identity_bloom()
    if (identity_count == 0) != (bloom == empty_bloom) or (fill_identity_count == 0) != (
        fill_bloom == empty_bloom
    ):
        raise CtpOrderJournalIntegrityError("CTP order journal epoch identity Bloom/count mismatch")
    return CtpOrderJournalSealedEpoch(
        transaction_id=transaction_id,
        source_account_identity_digest=_sha(
            raw["source_account_identity_digest"], "CTP epoch source account"
        ),
        target_account_identity_digest=_sha(
            raw["target_account_identity_digest"], "CTP epoch target account"
        ),
        trading_day=_day(raw["trading_day"]),
        operator_reason=_epoch_reason(raw["operator_reason"]),
        directory_name=str(directory_name),
        source_files=tuple(files),
        journal_sequence=journal_sequence,
        journal_checksum=str(journal_checksum),
        archive_head_checksum=str(archive_head_checksum),
        archive_entry_count=_nonnegative_int(
            raw["archive_entry_count"], "CTP sealed archive entry count"
        ),
        entry_count=entry_count,
        order_identity_count=identity_count,
        order_identity_bloom=bloom,
        fill_identity_count=fill_identity_count,
        fill_identity_bloom=fill_bloom,
    )


def _epoch_manifest_unsigned(
    *,
    sequence: int,
    parent_checksum: str | None,
    current_account_identity_digest: str,
    sealed_epochs: tuple[CtpOrderJournalSealedEpoch, ...],
    pending_cleanup_epoch_id: str,
) -> dict[str, object]:
    return {
        "kind": _EPOCH_MANIFEST_KIND,
        "schema_version": _EPOCH_MANIFEST_SCHEMA_VERSION,
        "sequence": sequence,
        "parent_checksum": parent_checksum,
        "current_account_identity_digest": current_account_identity_digest,
        "sealed_epochs": [_sealed_epoch_payload(epoch) for epoch in sealed_epochs],
        "pending_cleanup_epoch_id": pending_cleanup_epoch_id,
    }


def _new_epoch_manifest(
    *,
    sequence: int,
    parent_checksum: str | None,
    current_account_identity_digest: str,
    sealed_epochs: tuple[CtpOrderJournalSealedEpoch, ...],
    pending_cleanup_epoch_id: str,
) -> CtpOrderJournalEpochManifest:
    unsigned = _epoch_manifest_unsigned(
        sequence=sequence,
        parent_checksum=parent_checksum,
        current_account_identity_digest=current_account_identity_digest,
        sealed_epochs=sealed_epochs,
        pending_cleanup_epoch_id=pending_cleanup_epoch_id,
    )
    return CtpOrderJournalEpochManifest(
        sequence=sequence,
        parent_checksum=parent_checksum,
        current_account_identity_digest=current_account_identity_digest,
        sealed_epochs=sealed_epochs,
        pending_cleanup_epoch_id=pending_cleanup_epoch_id,
        checksum=_digest(unsigned),
    )


def _epoch_manifest_payload(manifest: CtpOrderJournalEpochManifest) -> dict[str, object]:
    return {
        **_epoch_manifest_unsigned(
            sequence=manifest.sequence,
            parent_checksum=manifest.parent_checksum,
            current_account_identity_digest=manifest.current_account_identity_digest,
            sealed_epochs=manifest.sealed_epochs,
            pending_cleanup_epoch_id=manifest.pending_cleanup_epoch_id,
        ),
        "checksum": manifest.checksum,
    }


def _parse_epoch_manifest(payload: bytes) -> CtpOrderJournalEpochManifest:
    try:
        raw = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except UnicodeDecodeError as exc:
        raise CtpOrderJournalIntegrityError(
            "CTP order journal epoch manifest is not valid UTF-8"
        ) from exc
    except json.JSONDecodeError as exc:
        raise CtpOrderJournalIntegrityError(
            "CTP order journal epoch manifest is not valid JSON"
        ) from exc
    expected = {
        "kind",
        "schema_version",
        "sequence",
        "parent_checksum",
        "current_account_identity_digest",
        "sealed_epochs",
        "pending_cleanup_epoch_id",
        "checksum",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise CtpOrderJournalIntegrityError("CTP order journal epoch manifest is invalid")
    if (
        raw["kind"] != _EPOCH_MANIFEST_KIND
        or raw["schema_version"] != _EPOCH_MANIFEST_SCHEMA_VERSION
    ):
        raise CtpOrderJournalIntegrityError("CTP order journal epoch manifest schema is invalid")
    sequence = _positive_int(raw["sequence"], "CTP order journal epoch sequence")
    parent_checksum = raw["parent_checksum"]
    if sequence == 1:
        if parent_checksum is not None:
            raise CtpOrderJournalIntegrityError("CTP order journal initial epoch parent is invalid")
    else:
        parent_checksum = _sha(parent_checksum, "CTP order journal epoch parent")
    raw_epochs = raw["sealed_epochs"]
    if not isinstance(raw_epochs, list) or not raw_epochs or len(raw_epochs) > _MAX_SEALED_EPOCHS:
        raise CtpOrderJournalIntegrityError("CTP order journal sealed epoch list is invalid")
    epochs = tuple(_sealed_epoch(item) for item in raw_epochs)
    transaction_ids = [epoch.transaction_id for epoch in epochs]
    if len(transaction_ids) != len(set(transaction_ids)):
        raise CtpOrderJournalIntegrityError("duplicate CTP order journal epoch identity")
    for previous, current in zip(epochs, epochs[1:], strict=False):
        if previous.target_account_identity_digest != current.source_account_identity_digest:
            raise CtpOrderJournalIntegrityError("CTP order journal account epoch chain is broken")
    current_account = _sha(raw["current_account_identity_digest"], "CTP current account identity")
    if epochs[-1].target_account_identity_digest != current_account:
        raise CtpOrderJournalIntegrityError("CTP order journal current account epoch is invalid")
    pending = raw["pending_cleanup_epoch_id"]
    if pending != "":
        pending = _sha(pending, "CTP pending epoch cleanup")
        if pending != epochs[-1].transaction_id:
            raise CtpOrderJournalIntegrityError("CTP pending epoch cleanup identity is invalid")
    checksum = _sha(raw["checksum"], "CTP order journal epoch manifest")
    unsigned = {key: value for key, value in raw.items() if key != "checksum"}
    if checksum != _digest(unsigned):
        raise CtpOrderJournalIntegrityError("CTP order journal epoch checksum mismatch")
    return CtpOrderJournalEpochManifest(
        sequence,
        parent_checksum,
        current_account,
        epochs,
        str(pending),
        checksum,
    )


@dataclass(frozen=True)
class _JournalGeneration:
    sequence: int
    parent_checksum: str | None
    entries: tuple[CtpOrderSubmissionEntry, ...]
    archive_head: _ArchivePointer | None
    archive_entry_count: int
    runtime_index: _RuntimeIndexPointer | None
    checksum: str


@dataclass(frozen=True)
class _LoadedEnvelope:
    generation: _JournalGeneration
    previous_generation: _JournalGeneration | None
    record: CtpOrderSubmissionJournalRecord
    runtime_index: _RuntimeArchiveIndex | None
    payload: bytes


def _entry_payload(entry: CtpOrderSubmissionEntry) -> dict[str, object]:
    return {
        "sequence": entry.sequence,
        "account_identity_digest": entry.account_identity_digest,
        "policy_id": entry.policy_id,
        "policy_definition_digest": entry.policy_definition_digest,
        "products_manifest_digest": entry.products_manifest_digest,
        "target_trading_day": entry.target_trading_day,
        "daily_decision_digest": entry.daily_decision_digest,
        "execution_intent_digest": entry.execution_intent_digest,
        "transition": dict(entry.transition),
        "front_id": entry.front_id,
        "session_id": entry.session_id,
        "order_ref": entry.order_ref,
        "order_id": entry.order_id,
        "request": _request_payload(entry.request),
        "status": entry.status,
        "authorization_kind": entry.authorization_kind,
        "filled_volume": entry.filled_volume,
        "fill_keys": list(entry.fill_keys),
        "fill_evidence": [_fill_evidence_payload(item) for item in entry.fill_evidence],
    }


def _entry(raw: object) -> CtpOrderSubmissionEntry:
    expected = {
        "sequence",
        "account_identity_digest",
        "policy_id",
        "policy_definition_digest",
        "products_manifest_digest",
        "target_trading_day",
        "daily_decision_digest",
        "execution_intent_digest",
        "transition",
        "front_id",
        "session_id",
        "order_ref",
        "order_id",
        "request",
        "status",
        "authorization_kind",
        "filled_volume",
        "fill_keys",
        "fill_evidence",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise CtpOrderJournalIntegrityError("CTP order journal entry is invalid")
    front_id = _positive_int(raw["front_id"], "CTP FrontID")
    session_id = _positive_int(raw["session_id"], "CTP SessionID")
    order_ref = _positive_int(raw["order_ref"], "CTP OrderRef")
    order_id = raw["order_id"]
    expected_id = f"CTP.{front_id}_{session_id}_{order_ref}"
    if order_id != expected_id:
        raise CtpOrderJournalIntegrityError("CTP order journal exact identity is invalid")
    policy_id = raw["policy_id"]
    status = raw["status"]
    authorization_kind = raw["authorization_kind"]
    if (
        not isinstance(policy_id, str)
        or not policy_id
        or not isinstance(authorization_kind, str)
        or authorization_kind not in {"candidate", "risk_reduction"}
        or not isinstance(status, str)
        or status not in _JOURNAL_STATUSES
    ):
        raise CtpOrderJournalIntegrityError("CTP order journal entry is invalid")
    request = _request(raw["request"])
    if authorization_kind == "risk_reduction" and request.offset is Offset.OPEN:
        raise CtpOrderJournalIntegrityError(
            "CTP risk-reduction authorization cannot contain an opening request"
        )
    target_trading_day = _day(raw["target_trading_day"])
    filled_volume = _nonnegative_int(raw["filled_volume"], "CTP order filled volume")
    raw_fill_keys = raw["fill_keys"]
    if not isinstance(raw_fill_keys, list):
        raise CtpOrderJournalIntegrityError("CTP order journal fill keys are invalid")
    fill_keys = tuple(_fill_key(item) for item in raw_fill_keys)
    raw_fill_evidence = raw["fill_evidence"]
    if not isinstance(raw_fill_evidence, list):
        raise CtpOrderJournalIntegrityError("CTP order fill evidence is invalid")
    fill_evidence = tuple(_fill_evidence(item) for item in raw_fill_evidence)
    evidence_keys = tuple(item.key for item in fill_evidence)
    evidence_volume = sum(item.volume for item in fill_evidence)
    economics_match = all(
        item.order_id == expected_id
        and item.symbol == request.symbol
        and item.exchange == request.exchange
        and item.side is request.side
        and item.offset is request.offset
        and item.key.split(":", 1)[0] == target_trading_day
        and not (item.side is OrderSide.BUY and item.price > float(request.price))
        and not (item.side is OrderSide.SELL and item.price < float(request.price))
        for item in fill_evidence
    )
    if (
        fill_keys != tuple(sorted(set(fill_keys)))
        or evidence_keys != tuple(sorted(set(evidence_keys)))
        or evidence_keys != fill_keys
        or evidence_volume != filled_volume
        or not economics_match
        or filled_volume > request.volume
        or bool(filled_volume) != bool(fill_keys)
        or len(fill_keys) > filled_volume
        or (status == "aborted_before_send" and (filled_volume != 0 or fill_keys))
    ):
        raise CtpOrderJournalIntegrityError("CTP order filled volume/fill keys are invalid")
    return CtpOrderSubmissionEntry(
        sequence=_positive_int(raw["sequence"], "CTP order journal entry sequence"),
        account_identity_digest=_sha(raw["account_identity_digest"], "account identity"),
        policy_id=policy_id,
        policy_definition_digest=_sha(raw["policy_definition_digest"], "policy definition"),
        products_manifest_digest=_sha(raw["products_manifest_digest"], "products manifest"),
        target_trading_day=target_trading_day,
        daily_decision_digest=_sha(raw["daily_decision_digest"], "daily decision"),
        execution_intent_digest=_sha(raw["execution_intent_digest"], "execution intent"),
        transition=_transition(raw["transition"]),
        front_id=front_id,
        session_id=session_id,
        order_ref=order_ref,
        order_id=expected_id,
        request=request,
        status=str(status),
        authorization_kind=str(authorization_kind),
        filled_volume=filled_volume,
        fill_keys=fill_keys,
        fill_evidence=fill_evidence,
    )


class CtpOrderSubmissionJournal:
    """One bounded hot generation plus an immutable archive digest chain."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve(strict=False)
        self._runtime_sealed_fill_identity_scopes: tuple[tuple[str, bytes], ...] = ()

    @property
    def previous_path(self) -> Path:
        return self.path.with_name(self.path.name + ".prev")

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    @property
    def epoch_manifest_path(self) -> Path:
        return self.path.with_name(self.path.name + ".epochs.json")

    @property
    def epoch_manifest_previous_path(self) -> Path:
        return self.epoch_manifest_path.with_name(self.epoch_manifest_path.name + ".prev")

    @property
    def epoch_root(self) -> Path:
        return self.path.with_name(self.path.name + ".epochs")

    def _require_epoch_namespace_unlocked(
        self,
        manifest: CtpOrderJournalEpochManifest | None,
        *,
        allow_orphan_transaction_id: str,
    ) -> None:
        expected = (
            set()
            if manifest is None
            else {epoch.directory_name for epoch in manifest.sealed_epochs}
        )
        allowed_extra = (
            {f"epoch-{allow_orphan_transaction_id}"} if allow_orphan_transaction_id else set()
        )
        if not self.epoch_root.exists():
            if expected:
                raise CtpOrderJournalIntegrityError(
                    "sealed CTP order journal epoch namespace is missing"
                )
            return
        try:
            observed = {item.name for item in self.epoch_root.iterdir()}
        except OSError as exc:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal epoch namespace is invalid"
            ) from exc
        if expected - observed or (observed - expected) - allowed_extra:
            raise CtpOrderJournalIntegrityError(
                "unreferenced or missing CTP order journal epoch evidence exists"
            )

    def _load_epoch_manifest_unlocked(
        self,
        *,
        allow_orphan_transaction_id: str = "",
    ) -> CtpOrderJournalEpochManifest | None:
        if not self._exists(self.epoch_manifest_path):
            if self._exists(self.epoch_manifest_previous_path):
                raise CtpOrderJournalIntegrityError(
                    "current CTP order journal epoch manifest is missing"
                )
            self._require_epoch_namespace_unlocked(
                None,
                allow_orphan_transaction_id=allow_orphan_transaction_id,
            )
            return None
        current_payload = self._read_bytes(self.epoch_manifest_path)
        current = _parse_epoch_manifest(current_payload)
        if not self._exists(self.epoch_manifest_previous_path):
            if current.sequence != 1:
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal epoch parent evidence is missing"
                )
            self._require_epoch_namespace_unlocked(
                current,
                allow_orphan_transaction_id=allow_orphan_transaction_id,
            )
            return current
        previous_payload = self._read_bytes(self.epoch_manifest_previous_path)
        previous = _parse_epoch_manifest(previous_payload)
        duplicate_current = current_payload == previous_payload and current == previous
        if not duplicate_current and (
            current.sequence != previous.sequence + 1
            or current.parent_checksum != previous.checksum
            or not previous.sealed_epochs
            or len(current.sealed_epochs) < len(previous.sealed_epochs)
            or current.sealed_epochs[: len(previous.sealed_epochs)] != previous.sealed_epochs
        ):
            raise CtpOrderJournalIntegrityError(
                "CTP order journal epoch parent checksum/sequence mismatch"
            )
        self._require_epoch_namespace_unlocked(
            current,
            allow_orphan_transaction_id=allow_orphan_transaction_id,
        )
        return current

    def _save_epoch_manifest_locked(
        self,
        previous: CtpOrderJournalEpochManifest | None,
        *,
        current_account_identity_digest: str,
        sealed_epochs: tuple[CtpOrderJournalSealedEpoch, ...],
        pending_cleanup_epoch_id: str,
    ) -> CtpOrderJournalEpochManifest:
        manifest = _new_epoch_manifest(
            sequence=1 if previous is None else previous.sequence + 1,
            parent_checksum=None if previous is None else previous.checksum,
            current_account_identity_digest=_sha(
                current_account_identity_digest, "CTP current account identity"
            ),
            sealed_epochs=sealed_epochs,
            pending_cleanup_epoch_id=pending_cleanup_epoch_id,
        )
        payload = json.dumps(
            _epoch_manifest_payload(manifest),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        if previous is not None:
            self._atomic_replace(
                self.epoch_manifest_previous_path,
                self._read_bytes(self.epoch_manifest_path),
            )
        self._atomic_replace(self.epoch_manifest_path, payload)
        return manifest

    @staticmethod
    def _require_epoch_ready(manifest: CtpOrderJournalEpochManifest | None) -> None:
        if manifest is not None and manifest.pending_cleanup_epoch_id:
            raise CtpOrderJournalIntegrityError("CTP order journal epoch cleanup is incomplete")

    def _acquire_kernel_lock(self) -> int:
        descriptor = os.open("/dev/null", os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
        identity = sha256(f"ctp-order-journal:{self.path}".encode()).digest()
        offset = int.from_bytes(identity[:8], "big") % ((1 << 63) - 1)
        lock = struct.pack("hhqqi4x", fcntl.F_WRLCK, os.SEEK_SET, offset, 1, 0)
        try:
            fcntl.fcntl(descriptor, _F_OFD_SETLKW, lock)
        except OSError as exc:
            os.close(descriptor)
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal is locked by another process"
                ) from exc
            raise CtpOrderJournalIntegrityError("CTP order journal kernel lock failed") from exc
        return descriptor

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        """Serialize the whole transaction with an unlink-proof Linux OFD lock."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        kernel_descriptor = self._acquire_kernel_lock()
        visible_descriptor: int | None = None
        visible_locked = False
        try:
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            try:
                visible_descriptor = os.open(self.lock_path, flags, 0o600)
            except OSError as exc:
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal lock identity is invalid"
                ) from exc
            metadata = os.fstat(visible_descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != os.geteuid()
            ):
                raise CtpOrderJournalIntegrityError("CTP order journal lock identity is invalid")
            os.fchmod(visible_descriptor, 0o600)
            fcntl.flock(visible_descriptor, fcntl.LOCK_EX)
            visible_locked = True
            yield
        finally:
            if visible_descriptor is not None:
                if visible_locked:
                    fcntl.flock(visible_descriptor, fcntl.LOCK_UN)
                os.close(visible_descriptor)
            os.close(kernel_descriptor)

    @staticmethod
    def _exists(path: Path) -> bool:
        try:
            os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise CtpOrderJournalIntegrityError("CTP order journal namespace is invalid") from exc
        return True

    @staticmethod
    def _read_bytes(path: Path) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal evidence cannot be read"
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != os.geteuid()
            ):
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal evidence identity is invalid"
                )
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        finally:
            os.close(descriptor)

    @staticmethod
    def _pointer_payload(pointer: _ArchivePointer | None) -> dict[str, object] | None:
        if pointer is None:
            return None
        return {
            "segment_sequence": pointer.segment_sequence,
            "filename": pointer.filename,
            "checksum": pointer.checksum,
        }

    @staticmethod
    def _pointer(raw: object) -> _ArchivePointer | None:
        if raw is None:
            return None
        if not isinstance(raw, Mapping) or set(raw) != {
            "segment_sequence",
            "filename",
            "checksum",
        }:
            raise CtpOrderJournalIntegrityError("CTP order journal archive pointer is invalid")
        filename = raw["filename"]
        if not isinstance(filename, str) or not filename or Path(filename).name != filename:
            raise CtpOrderJournalIntegrityError("CTP order journal archive pointer is invalid")
        return _ArchivePointer(
            _positive_int(raw["segment_sequence"], "CTP archive segment sequence"),
            filename,
            _sha(raw["checksum"], "CTP archive segment checksum"),
        )

    @staticmethod
    def _runtime_index_pointer_payload(
        pointer: _RuntimeIndexPointer | None,
    ) -> dict[str, object] | None:
        if pointer is None:
            return None
        return {"filename": pointer.filename, "checksum": pointer.checksum}

    @staticmethod
    def _runtime_index_pointer(raw: object) -> _RuntimeIndexPointer | None:
        if raw is None:
            return None
        if not isinstance(raw, Mapping) or set(raw) != {"filename", "checksum"}:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal runtime-index pointer is invalid"
            )
        filename = raw["filename"]
        if not isinstance(filename, str) or not filename or Path(filename).name != filename:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal runtime-index pointer is invalid"
            )
        return _RuntimeIndexPointer(
            filename,
            _sha(raw["checksum"], "CTP runtime-index checksum"),
        )

    @classmethod
    def _generation_unsigned(
        cls,
        *,
        sequence: int,
        parent_checksum: str | None,
        entries: tuple[CtpOrderSubmissionEntry, ...],
        archive_head: _ArchivePointer | None,
        archive_entry_count: int,
        runtime_index: _RuntimeIndexPointer | None,
    ) -> dict[str, object]:
        return {
            "kind": _GENERATION_KIND,
            "schema_version": _GENERATION_SCHEMA_VERSION,
            "sequence": sequence,
            "parent_checksum": parent_checksum,
            "entries": [_entry_payload(entry) for entry in entries],
            "archive_head": cls._pointer_payload(archive_head),
            "archive_entry_count": archive_entry_count,
            "runtime_index": cls._runtime_index_pointer_payload(runtime_index),
        }

    @classmethod
    def _new_generation(
        cls,
        *,
        sequence: int,
        parent_checksum: str | None,
        entries: tuple[CtpOrderSubmissionEntry, ...],
        archive_head: _ArchivePointer | None,
        archive_entry_count: int,
        runtime_index: _RuntimeIndexPointer | None,
    ) -> _JournalGeneration:
        unsigned = cls._generation_unsigned(
            sequence=sequence,
            parent_checksum=parent_checksum,
            entries=entries,
            archive_head=archive_head,
            archive_entry_count=archive_entry_count,
            runtime_index=runtime_index,
        )
        return _JournalGeneration(
            sequence,
            parent_checksum,
            entries,
            archive_head,
            archive_entry_count,
            runtime_index,
            _digest(unsigned),
        )

    @classmethod
    def _generation_payload(cls, generation: _JournalGeneration) -> dict[str, object]:
        return {
            **cls._generation_unsigned(
                sequence=generation.sequence,
                parent_checksum=generation.parent_checksum,
                entries=generation.entries,
                archive_head=generation.archive_head,
                archive_entry_count=generation.archive_entry_count,
                runtime_index=generation.runtime_index,
            ),
            "checksum": generation.checksum,
        }

    @classmethod
    def _parse_generation(cls, raw: object) -> _JournalGeneration:
        expected = {
            "kind",
            "schema_version",
            "sequence",
            "parent_checksum",
            "entries",
            "archive_head",
            "archive_entry_count",
            "runtime_index",
            "checksum",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise CtpOrderJournalIntegrityError("CTP order journal generation is invalid")
        if raw["kind"] != _GENERATION_KIND or raw["schema_version"] != _GENERATION_SCHEMA_VERSION:
            raise CtpOrderJournalIntegrityError("CTP order journal generation schema is invalid")
        sequence = _positive_int(raw["sequence"], "CTP order journal sequence")
        parent_checksum = raw["parent_checksum"]
        if sequence == 1:
            if parent_checksum is not None:
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal initial parent evidence is invalid"
                )
        else:
            parent_checksum = _sha(
                parent_checksum,
                "CTP order journal parent checksum",
            )
        raw_entries = raw["entries"]
        if not isinstance(raw_entries, list):
            raise CtpOrderJournalIntegrityError("CTP order journal entries are invalid")
        entries = tuple(_entry(item) for item in raw_entries)
        if len(entries) > _MAX_ENTRIES:
            raise CtpOrderJournalIntegrityError("CTP order journal entry limit exceeded")
        archive_head = cls._pointer(raw["archive_head"])
        archive_entry_count = _nonnegative_int(
            raw["archive_entry_count"],
            "CTP order journal archive entry count",
        )
        if (archive_head is None) != (archive_entry_count == 0):
            raise CtpOrderJournalIntegrityError("CTP order journal archive head/count mismatch")
        runtime_index = cls._runtime_index_pointer(raw["runtime_index"])
        if (archive_head is None) != (runtime_index is None):
            raise CtpOrderJournalIntegrityError("CTP order journal archive/runtime-index mismatch")
        checksum = _sha(raw["checksum"], "CTP order journal checksum")
        unsigned = {key: value for key, value in raw.items() if key != "checksum"}
        if checksum != _digest(unsigned):
            raise CtpOrderJournalIntegrityError("CTP order journal generation checksum mismatch")
        return _JournalGeneration(
            sequence,
            parent_checksum,
            entries,
            archive_head,
            archive_entry_count,
            runtime_index,
            checksum,
        )

    @classmethod
    def _encode_envelope(
        cls,
        current: _JournalGeneration,
        previous: _JournalGeneration | None,
    ) -> bytes:
        unsigned = {
            "kind": _KIND,
            "schema_version": _SCHEMA_VERSION,
            "current": cls._generation_payload(current),
            "previous": None if previous is None else cls._generation_payload(previous),
        }
        return json.dumps(
            {**unsigned, "checksum": _digest(unsigned)},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")

    @classmethod
    def _decode_envelope(
        cls,
        payload: bytes,
    ) -> tuple[_JournalGeneration, _JournalGeneration | None]:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CtpOrderJournalIntegrityError("invalid CTP order journal UTF-8") from exc
        try:
            raw = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
        except json.JSONDecodeError as exc:
            raise CtpOrderJournalIntegrityError("invalid CTP order journal JSON") from exc
        if (
            isinstance(raw, Mapping)
            and raw.get("kind") == _KIND
            and raw.get("schema_version") != _SCHEMA_VERSION
        ):
            raise CtpOrderJournalIntegrityError(
                "CTP order journal schema is invalid; explicit bootstrap is required"
            )
        expected = {"kind", "schema_version", "current", "previous", "checksum"}
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise CtpOrderJournalIntegrityError("CTP order journal envelope is invalid")
        if raw["kind"] != _KIND or raw["schema_version"] != _SCHEMA_VERSION:
            raise CtpOrderJournalIntegrityError("CTP order journal schema is invalid")
        checksum = _sha(raw["checksum"], "CTP order journal envelope checksum")
        unsigned = {key: value for key, value in raw.items() if key != "checksum"}
        if checksum != _digest(unsigned):
            raise CtpOrderJournalIntegrityError("CTP order journal envelope checksum mismatch")
        current = cls._parse_generation(raw["current"])
        previous = None
        if raw["previous"] is not None:
            previous = cls._parse_generation(raw["previous"])
        if current.sequence == 1:
            if previous is not None or current.parent_checksum is not None:
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal initial parent evidence is invalid"
                )
        elif (
            previous is None
            or previous.sequence != current.sequence - 1
            or previous.checksum != current.parent_checksum
        ):
            raise CtpOrderJournalIntegrityError(
                "CTP order journal embedded parent checksum/sequence mismatch"
            )
        return current, previous

    def _archive_segment_name(self, sequence: int, checksum: str) -> str:
        return f"{self.path.name}.archive.{sequence:020d}.{checksum}.json"

    def _runtime_index_name(self, checksum: str) -> str:
        return f"{self.path.name}.runtime-index.{checksum}.json"

    @staticmethod
    def _runtime_index_unsigned(
        *,
        archive_head: _ArchivePointer,
        archive_entry_count: int,
        policy_identity: tuple[str, str, str, str],
        recent_entries: tuple[CtpOrderSubmissionEntry, ...],
        order_identity_count: int,
        order_identity_bloom: bytes,
    ) -> dict[str, object]:
        return {
            "kind": _RUNTIME_INDEX_KIND,
            "schema_version": _RUNTIME_INDEX_SCHEMA_VERSION,
            "archive_head": CtpOrderSubmissionJournal._pointer_payload(archive_head),
            "archive_entry_count": archive_entry_count,
            "policy_identity": list(policy_identity),
            "recent_entries": [_entry_payload(entry) for entry in recent_entries],
            "order_identity_count": order_identity_count,
            "order_identity_bloom": b64encode(order_identity_bloom).decode("ascii"),
        }

    @classmethod
    def _validate_runtime_index(cls, index: _RuntimeArchiveIndex) -> None:
        if (
            index.archive_entry_count != index.order_identity_count
            or index.order_identity_count > _ORDER_ID_BLOOM_MAX_ITEMS
            or len(index.order_identity_bloom) != len(_empty_order_identity_bloom())
            or len(index.recent_entries) > _RUNTIME_RECENT_MAX_ENTRIES
        ):
            raise CtpOrderJournalIntegrityError(
                "CTP order journal runtime-index capacity/count is invalid"
            )
        if not isinstance(_RUNTIME_RECENT_DAYS, int) or _RUNTIME_RECENT_DAYS <= 0:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal runtime-index day bound is invalid"
            )
        recent = index.recent_entries
        if (
            any(entry.status not in _FINAL_STATUSES for entry in recent)
            or tuple(entry.sequence for entry in recent)
            != tuple(sorted(entry.sequence for entry in recent))
            or len({entry.order_id for entry in recent}) != len(recent)
            or len({entry.sequence for entry in recent}) != len(recent)
            or any(cls._policy_identity(entry) != index.policy_identity for entry in recent)
            or any(
                not _order_identity_bloom_contains(
                    index.order_identity_bloom,
                    entry.order_id,
                )
                for entry in recent
            )
        ):
            raise CtpOrderJournalIntegrityError(
                "CTP order journal runtime-index entries are invalid"
            )
        fill_keys = [key for entry in recent for key in entry.fill_keys]
        if (
            len(fill_keys) != len(set(fill_keys))
            or len(fill_keys) > CTP_ORDER_RUNTIME_MAX_FILL_IDENTITIES
        ):
            raise CtpOrderJournalIntegrityError(
                "CTP order journal runtime-index contains duplicate fill identity"
            )

    def _write_runtime_index(
        self,
        *,
        archive_head: _ArchivePointer,
        archive_entry_count: int,
        policy_identity: tuple[str, str, str, str],
        recent_entries: tuple[CtpOrderSubmissionEntry, ...],
        order_identity_count: int,
        order_identity_bloom: bytes,
    ) -> _RuntimeIndexPointer:
        unsigned = self._runtime_index_unsigned(
            archive_head=archive_head,
            archive_entry_count=archive_entry_count,
            policy_identity=policy_identity,
            recent_entries=recent_entries,
            order_identity_count=order_identity_count,
            order_identity_bloom=order_identity_bloom,
        )
        checksum = _digest(unsigned)
        payload = json.dumps(
            {**unsigned, "checksum": checksum},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        filename = self._runtime_index_name(checksum)
        self._write_immutable(self.path.parent / filename, payload)
        return _RuntimeIndexPointer(filename, checksum)

    def _read_runtime_index(
        self,
        pointer: _RuntimeIndexPointer,
        *,
        expected_archive_head: _ArchivePointer,
        expected_archive_entry_count: int,
    ) -> _RuntimeArchiveIndex:
        if pointer.filename != self._runtime_index_name(pointer.checksum):
            raise CtpOrderJournalIntegrityError(
                "CTP order journal runtime-index filename is invalid"
            )
        payload = self._read_bytes(self.path.parent / pointer.filename)
        try:
            raw = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CtpOrderJournalIntegrityError(
                "invalid CTP order journal runtime-index JSON"
            ) from exc
        expected = {
            "kind",
            "schema_version",
            "archive_head",
            "archive_entry_count",
            "policy_identity",
            "recent_entries",
            "order_identity_count",
            "order_identity_bloom",
            "checksum",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal runtime-index envelope is invalid"
            )
        if (
            raw["kind"] != _RUNTIME_INDEX_KIND
            or raw["schema_version"] != _RUNTIME_INDEX_SCHEMA_VERSION
        ):
            raise CtpOrderJournalIntegrityError("CTP order journal runtime-index schema is invalid")
        archive_head = self._pointer(raw["archive_head"])
        if archive_head is None or archive_head != expected_archive_head:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal runtime-index archive head mismatch"
            )
        archive_entry_count = _nonnegative_int(
            raw["archive_entry_count"],
            "CTP runtime-index archive entry count",
        )
        if archive_entry_count != expected_archive_entry_count:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal runtime-index archive count mismatch"
            )
        raw_identity = raw["policy_identity"]
        if not isinstance(raw_identity, list) or len(raw_identity) != 4:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal runtime-index policy identity is invalid"
            )
        policy_id = raw_identity[1]
        if not isinstance(policy_id, str) or not policy_id:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal runtime-index policy identity is invalid"
            )
        policy_identity = (
            _sha(raw_identity[0], "account identity"),
            policy_id,
            _sha(raw_identity[2], "policy definition"),
            _sha(raw_identity[3], "products manifest"),
        )
        raw_recent = raw["recent_entries"]
        if not isinstance(raw_recent, list):
            raise CtpOrderJournalIntegrityError(
                "CTP order journal runtime-index entries are invalid"
            )
        recent_entries = tuple(_entry(item) for item in raw_recent)
        encoded_bloom = raw["order_identity_bloom"]
        if not isinstance(encoded_bloom, str):
            raise CtpOrderJournalIntegrityError("CTP order journal runtime-index Bloom is invalid")
        try:
            bloom = b64decode(encoded_bloom.encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError) as exc:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal runtime-index Bloom is invalid"
            ) from exc
        checksum = _sha(raw["checksum"], "CTP runtime-index checksum")
        unsigned = {key: value for key, value in raw.items() if key != "checksum"}
        if checksum != pointer.checksum or checksum != _digest(unsigned):
            raise CtpOrderJournalIntegrityError("CTP order journal runtime-index checksum mismatch")
        index = _RuntimeArchiveIndex(
            archive_head,
            archive_entry_count,
            policy_identity,
            recent_entries,
            _nonnegative_int(
                raw["order_identity_count"],
                "CTP runtime-index order identity count",
            ),
            bloom,
            checksum,
        )
        self._validate_runtime_index(index)
        return index

    def _read_archive_segment(
        self,
        pointer: _ArchivePointer,
    ) -> tuple[_ArchivePointer | None, tuple[CtpOrderSubmissionEntry, ...]]:
        expected_name = self._archive_segment_name(
            pointer.segment_sequence,
            pointer.checksum,
        )
        if pointer.filename != expected_name:
            raise CtpOrderJournalIntegrityError("CTP order journal archive filename is invalid")
        payload = self._read_bytes(self.path.parent / pointer.filename)
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CtpOrderJournalIntegrityError("invalid CTP order journal archive UTF-8") from exc
        try:
            raw = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
        except json.JSONDecodeError as exc:
            raise CtpOrderJournalIntegrityError("invalid CTP order journal archive JSON") from exc
        expected = {
            "kind",
            "schema_version",
            "segment_sequence",
            "previous_segment",
            "entries",
            "checksum",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise CtpOrderJournalIntegrityError("CTP order journal archive envelope is invalid")
        if (
            raw["kind"] != _ARCHIVE_KIND
            or raw["schema_version"] != _ARCHIVE_SCHEMA_VERSION
            or _positive_int(
                raw["segment_sequence"],
                "CTP archive segment sequence",
            )
            != pointer.segment_sequence
        ):
            raise CtpOrderJournalIntegrityError(
                "CTP order journal archive schema/sequence is invalid"
            )
        previous = self._pointer(raw["previous_segment"])
        if pointer.segment_sequence == 1:
            if previous is not None:
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal initial archive parent is invalid"
                )
        elif previous is None or previous.segment_sequence != pointer.segment_sequence - 1:
            raise CtpOrderJournalIntegrityError("CTP order journal archive chain is not contiguous")
        raw_entries = raw["entries"]
        if (
            not isinstance(raw_entries, list)
            or not raw_entries
            or len(raw_entries) > _ARCHIVE_SEGMENT_MAX_ENTRIES
        ):
            raise CtpOrderJournalIntegrityError(
                "CTP order journal archive segment limit is invalid"
            )
        entries = tuple(_entry(item) for item in raw_entries)
        if any(entry.status not in _FINAL_STATUSES for entry in entries):
            raise CtpOrderJournalIntegrityError(
                "CTP order journal archived entries must be terminal-like"
            )
        if len({entry.order_id for entry in entries}) != len(entries):
            raise CtpOrderJournalIntegrityError(
                "CTP order journal archive segment contains duplicate identity"
            )
        checksum = _sha(raw["checksum"], "CTP archive segment checksum")
        unsigned = {key: value for key, value in raw.items() if key != "checksum"}
        if checksum != pointer.checksum or checksum != _digest(unsigned):
            raise CtpOrderJournalIntegrityError("CTP order journal archive checksum mismatch")
        return previous, entries

    @staticmethod
    def _allowed_status_transition(current: str, target: str) -> bool:
        return (
            target
            in {
                "prepared": {
                    "prepared",
                    "submitted",
                    "terminal",
                    "aborted_before_send",
                },
                "submitted": {"submitted", "terminal"},
                "terminal": {"terminal"},
                "aborted_before_send": {"aborted_before_send"},
            }[current]
        )

    @classmethod
    def _validate_entry_revision(
        cls,
        previous: CtpOrderSubmissionEntry,
        current: CtpOrderSubmissionEntry,
    ) -> None:
        stable_current = replace(
            current,
            status=previous.status,
            filled_volume=previous.filled_volume,
            fill_keys=previous.fill_keys,
            fill_evidence=previous.fill_evidence,
        )
        if stable_current != previous:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal immutable entry identity changed"
            )
        if not cls._allowed_status_transition(previous.status, current.status):
            raise CtpOrderJournalIntegrityError("invalid CTP order journal status transition")
        if current.filled_volume < previous.filled_volume or not set(previous.fill_keys).issubset(
            current.fill_keys
        ):
            raise CtpOrderJournalIntegrityError("CTP order journal fill evidence regressed")
        previous_evidence = {item.key: item for item in previous.fill_evidence}
        current_evidence = {item.key: item for item in current.fill_evidence}
        if any(
            current_evidence.get(key) != evidence for key, evidence in previous_evidence.items()
        ):
            raise CtpOrderJournalIntegrityError(
                "CTP order journal fill fingerprint changed for an existing identity"
            )

    def _load_archive(
        self,
        head: _ArchivePointer | None,
    ) -> tuple[tuple[CtpOrderSubmissionEntry, ...], frozenset[str]]:
        if head is None:
            return (), frozenset()
        reverse_segments: list[tuple[CtpOrderSubmissionEntry, ...]] = []
        checksums: set[str] = set()
        pointer: _ArchivePointer | None = head
        expected_sequence = head.segment_sequence
        while pointer is not None:
            if pointer.segment_sequence != expected_sequence or pointer.checksum in checksums:
                raise CtpOrderJournalIntegrityError("CTP order journal archive chain is invalid")
            checksums.add(pointer.checksum)
            pointer, entries = self._read_archive_segment(pointer)
            reverse_segments.append(entries)
            expected_sequence -= 1
        if expected_sequence != 0:
            raise CtpOrderJournalIntegrityError("CTP order journal archive chain is incomplete")

        by_id: dict[str, CtpOrderSubmissionEntry] = {}
        by_sequence: dict[int, str] = {}
        for entries in reversed(reverse_segments):
            for entry in entries:
                previous = by_id.get(entry.order_id)
                if previous is None:
                    duplicate_id = by_sequence.get(entry.sequence)
                    if duplicate_id is not None:
                        raise CtpOrderJournalIntegrityError(
                            "CTP order journal archive contains duplicate sequence"
                        )
                    by_id[entry.order_id] = entry
                    by_sequence[entry.sequence] = entry.order_id
                else:
                    self._validate_entry_revision(previous, entry)
                    by_id[entry.order_id] = entry
        return (
            tuple(sorted(by_id.values(), key=lambda entry: entry.sequence)),
            frozenset(checksums),
        )

    @classmethod
    def _validate_snapshot(
        cls,
        entries: tuple[CtpOrderSubmissionEntry, ...],
        archived_entries: tuple[CtpOrderSubmissionEntry, ...],
    ) -> None:
        if len(entries) > _MAX_ENTRIES:
            raise CtpOrderJournalIntegrityError("CTP order journal entry limit exceeded")
        if any(entry.status not in _FINAL_STATUSES for entry in archived_entries):
            raise CtpOrderJournalIntegrityError(
                "CTP order journal archived entries must be terminal-like"
            )
        if tuple(entry.sequence for entry in entries) != tuple(
            sorted(entry.sequence for entry in entries)
        ) or tuple(entry.sequence for entry in archived_entries) != tuple(
            sorted(entry.sequence for entry in archived_entries)
        ):
            raise CtpOrderJournalIntegrityError("CTP order journal entry sequence is invalid")
        all_entries = tuple(sorted((*archived_entries, *entries), key=lambda item: item.sequence))
        if len(all_entries) > _ORDER_ID_BLOOM_MAX_ITEMS:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal identity capacity exceeded; HALTED epoch rollover is required"
            )
        if tuple(entry.sequence for entry in all_entries) != tuple(range(1, len(all_entries) + 1)):
            raise CtpOrderJournalIntegrityError(
                "CTP order journal global entry sequence is invalid"
            )
        exact_ids = [entry.order_id for entry in all_entries]
        exact_tuples = [
            (entry.front_id, entry.session_id, entry.order_ref) for entry in all_entries
        ]
        if len(exact_ids) != len(set(exact_ids)) or len(exact_tuples) != len(set(exact_tuples)):
            raise CtpOrderJournalIntegrityError(
                "CTP order journal contains duplicate exact identity"
            )
        policy_identities = {cls._policy_identity(entry) for entry in all_entries}
        if len(policy_identities) > 1:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal contains inconsistent entry identity"
            )
        fill_keys = [fill_key for entry in all_entries for fill_key in entry.fill_keys]
        if len(fill_keys) != len(set(fill_keys)):
            raise CtpOrderJournalIntegrityError(
                "CTP order journal contains duplicate global fill identity"
            )

    def _record_for_generation(
        self,
        generation: _JournalGeneration,
    ) -> tuple[
        CtpOrderSubmissionJournalRecord,
        frozenset[str],
        _RuntimeArchiveIndex | None,
    ]:
        archived_entries, segment_checksums = self._load_archive(generation.archive_head)
        if len(archived_entries) != generation.archive_entry_count:
            raise CtpOrderJournalIntegrityError("CTP order journal archive entry count mismatch")
        self._validate_snapshot(generation.entries, archived_entries)
        runtime_index = self._runtime_index_for_generation(generation)
        if archived_entries:
            assert runtime_index is not None
            rebuilt_bloom = _order_identity_bloom_add(
                _empty_order_identity_bloom(),
                tuple(entry.order_id for entry in archived_entries),
            )
            archived_by_id = {entry.order_id: entry for entry in archived_entries}
            runtime_recent_by_id = {entry.order_id: entry for entry in runtime_index.recent_entries}
            latest_days = set(
                sorted(
                    {entry.target_trading_day for entry in archived_entries},
                    reverse=True,
                )[:_RUNTIME_RECENT_DAYS]
            )
            required_recent_ids = {
                entry.order_id
                for entry in archived_entries
                if entry.target_trading_day in latest_days
            }
            if (
                not required_recent_ids.issubset(runtime_recent_by_id)
                or any(
                    archived_by_id.get(order_id) != entry
                    for order_id, entry in runtime_recent_by_id.items()
                )
                or runtime_index.order_identity_count != len(archived_entries)
                or runtime_index.order_identity_bloom != rebuilt_bloom
                or runtime_index.policy_identity != self._policy_identity(archived_entries[0])
            ):
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal runtime-index does not match cold archive"
                )
        return (
            CtpOrderSubmissionJournalRecord(
                generation.sequence,
                generation.entries,
                archived_entries,
                generation.parent_checksum,
                generation.checksum,
                generation.archive_entry_count,
                True,
                (None if generation.archive_head is None else generation.archive_head.checksum),
                None if runtime_index is None else runtime_index.checksum,
                0 if runtime_index is None else runtime_index.order_identity_count,
                (
                    None
                    if runtime_index is None
                    else sha256(runtime_index.order_identity_bloom).hexdigest()
                ),
            ),
            segment_checksums,
            runtime_index,
        )

    def _runtime_index_for_generation(
        self,
        generation: _JournalGeneration,
    ) -> _RuntimeArchiveIndex | None:
        if generation.archive_head is None:
            if generation.runtime_index is not None or generation.archive_entry_count:
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal empty archive index is invalid"
                )
            return None
        if generation.runtime_index is None:
            raise CtpOrderJournalIntegrityError("CTP order journal runtime-index is missing")
        return self._read_runtime_index(
            generation.runtime_index,
            expected_archive_head=generation.archive_head,
            expected_archive_entry_count=generation.archive_entry_count,
        )

    @classmethod
    def _validate_runtime_snapshot(
        cls,
        generation: _JournalGeneration,
        runtime_index: _RuntimeArchiveIndex | None,
    ) -> None:
        entries = generation.entries
        archived = () if runtime_index is None else runtime_index.recent_entries
        if len(entries) > _MAX_ENTRIES:
            raise CtpOrderJournalIntegrityError("CTP order journal entry limit exceeded")
        if any(entry.status not in _FINAL_STATUSES for entry in archived):
            raise CtpOrderJournalIntegrityError(
                "CTP order journal runtime archive must be terminal-like"
            )
        combined = (*archived, *entries)
        sequences = [entry.sequence for entry in combined]
        total_entries = generation.archive_entry_count + len(entries)
        if (
            total_entries > _ORDER_ID_BLOOM_MAX_ITEMS
            or tuple(entry.sequence for entry in entries)
            != tuple(sorted(entry.sequence for entry in entries))
            or tuple(entry.sequence for entry in archived)
            != tuple(sorted(entry.sequence for entry in archived))
            or len(sequences) != len(set(sequences))
            or any(sequence > total_entries for sequence in sequences)
        ):
            raise CtpOrderJournalIntegrityError("CTP order journal runtime sequence is invalid")
        exact_ids = [entry.order_id for entry in combined]
        if len(exact_ids) != len(set(exact_ids)):
            raise CtpOrderJournalIntegrityError(
                "CTP order journal runtime contains duplicate exact identity"
            )
        policy_identities = {cls._policy_identity(entry) for entry in combined}
        if runtime_index is not None:
            policy_identities.add(runtime_index.policy_identity)
        if len(policy_identities) > 1:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal runtime contains inconsistent entry identity"
            )
        fill_keys = [fill_key for entry in combined for fill_key in entry.fill_keys]
        if (
            len(fill_keys) != len(set(fill_keys))
            or len(fill_keys) > CTP_ORDER_RUNTIME_MAX_FILL_IDENTITIES
        ):
            raise CtpOrderJournalIntegrityError(
                "CTP order journal runtime contains duplicate fill identity"
            )

    def _runtime_record_for_generation(
        self,
        generation: _JournalGeneration,
    ) -> tuple[CtpOrderSubmissionJournalRecord, _RuntimeArchiveIndex | None]:
        runtime_index = self._runtime_index_for_generation(generation)
        self._validate_runtime_snapshot(generation, runtime_index)
        archived = () if runtime_index is None else runtime_index.recent_entries
        return (
            CtpOrderSubmissionJournalRecord(
                generation.sequence,
                generation.entries,
                archived,
                generation.parent_checksum,
                generation.checksum,
                generation.archive_entry_count,
                False,
                (None if generation.archive_head is None else generation.archive_head.checksum),
                None if runtime_index is None else runtime_index.checksum,
                0 if runtime_index is None else runtime_index.order_identity_count,
                (
                    None
                    if runtime_index is None
                    else sha256(runtime_index.order_identity_bloom).hexdigest()
                ),
            ),
            runtime_index,
        )

    @classmethod
    def _validate_generation_transition(
        cls,
        previous: CtpOrderSubmissionJournalRecord,
        current: CtpOrderSubmissionJournalRecord,
    ) -> None:
        previous_by_id = {entry.order_id: entry for entry in previous.all_entries}
        current_by_id = {entry.order_id: entry for entry in current.all_entries}
        if not set(previous_by_id).issubset(current_by_id):
            raise CtpOrderJournalIntegrityError(
                "CTP order journal generation deleted durable identity"
            )
        if len(current_by_id) not in {len(previous_by_id), len(previous_by_id) + 1}:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal generation entry count transition is invalid"
            )
        for order_id, old_entry in previous_by_id.items():
            cls._validate_entry_revision(old_entry, current_by_id[order_id])
        added = set(current_by_id) - set(previous_by_id)
        if added:
            new_entry = current_by_id[added.pop()]
            if new_entry.sequence != len(current_by_id) or new_entry.status != "prepared":
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal new identity transition is invalid"
                )

    @classmethod
    def _validate_runtime_generation_transition(
        cls,
        previous: CtpOrderSubmissionJournalRecord,
        previous_index: _RuntimeArchiveIndex | None,
        current: CtpOrderSubmissionJournalRecord,
        current_index: _RuntimeArchiveIndex | None,
    ) -> None:
        previous_total = previous.archive_entry_count + len(previous.entries)
        current_total = current.archive_entry_count + len(current.entries)
        if current_total not in {previous_total, previous_total + 1}:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal runtime entry-count transition is invalid"
            )
        if current.archive_entry_count < previous.archive_entry_count:
            raise CtpOrderJournalIntegrityError("CTP order journal runtime archive count regressed")
        previous_by_id = {entry.order_id: entry for entry in previous.all_entries}
        current_by_id = {entry.order_id: entry for entry in current.all_entries}
        for order_id in set(previous_by_id).intersection(current_by_id):
            cls._validate_entry_revision(previous_by_id[order_id], current_by_id[order_id])
        lost = set(previous_by_id) - set(current_by_id)
        for order_id in lost:
            previous_entry = previous_by_id[order_id]
            if previous_entry.status not in _FINAL_STATUSES:
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal runtime deleted unresolved identity"
                )
            if current_index is None or not _order_identity_bloom_contains(
                current_index.order_identity_bloom,
                order_id,
            ):
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal runtime dropped unindexed final identity"
                )
        added = set(current_by_id) - set(previous_by_id)
        new_prepared = [
            current_by_id[order_id]
            for order_id in added
            if current_by_id[order_id].status == "prepared"
        ]
        if current_total == previous_total + 1:
            if len(new_prepared) != 1 or new_prepared[0].sequence != current_total:
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal runtime new identity transition is invalid"
                )
        elif new_prepared:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal runtime introduced unexpected identity"
            )
        if previous_index is not None and current_index is not None:
            if current_index.order_identity_count < previous_index.order_identity_count:
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal runtime identity count regressed"
                )
            if any(
                previous_byte & ~current_byte
                for previous_byte, current_byte in zip(
                    previous_index.order_identity_bloom,
                    current_index.order_identity_bloom,
                    strict=True,
                )
            ):
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal runtime Bloom evidence regressed"
                )
            for entry in previous_index.recent_entries:
                if not _order_identity_bloom_contains(
                    current_index.order_identity_bloom,
                    entry.order_id,
                ):
                    raise CtpOrderJournalIntegrityError(
                        "CTP order journal runtime Bloom evidence regressed"
                    )

    def _validate_archive_extension(
        self,
        previous: _ArchivePointer | None,
        current: _ArchivePointer | None,
    ) -> None:
        if previous == current:
            return
        if current is None:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal archive chain dropped parent evidence"
            )
        previous_sequence = 0 if previous is None else previous.segment_sequence
        segment_limit = (_MAX_ENTRIES + _RUNTIME_RECENT_MAX_ENTRIES) // max(
            1, _ARCHIVE_SEGMENT_MAX_ENTRIES
        ) + 2
        if current.segment_sequence - previous_sequence > segment_limit:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal archive generation advance is unbounded"
            )
        pointer: _ArchivePointer | None = current
        while pointer is not None and pointer.segment_sequence > previous_sequence:
            pointer, _entries = self._read_archive_segment(pointer)
        if pointer != previous:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal archive chain dropped parent evidence"
            )

    def _read_envelope(self, path: Path, *, full_archive: bool) -> _LoadedEnvelope:
        payload = self._read_bytes(path)
        generation, previous_generation = self._decode_envelope(payload)
        if full_archive:
            record, current_archive_checksums, runtime_index = self._record_for_generation(
                generation
            )
        else:
            record, runtime_index = self._runtime_record_for_generation(generation)
            current_archive_checksums = frozenset()
        if previous_generation is not None:
            if full_archive:
                previous_record, _, previous_index = self._record_for_generation(
                    previous_generation
                )
                if (
                    previous_generation.archive_head is not None
                    and previous_generation.archive_head.checksum not in current_archive_checksums
                ):
                    raise CtpOrderJournalIntegrityError(
                        "CTP order journal archive chain dropped parent evidence"
                    )
                self._validate_generation_transition(previous_record, record)
            else:
                previous_record, previous_index = self._runtime_record_for_generation(
                    previous_generation
                )
                self._validate_archive_extension(
                    previous_generation.archive_head,
                    generation.archive_head,
                )
                self._validate_runtime_generation_transition(
                    previous_record,
                    previous_index,
                    record,
                    runtime_index,
                )
        return _LoadedEnvelope(
            generation,
            previous_generation,
            record,
            runtime_index,
            payload,
        )

    def load(self) -> CtpOrderSubmissionJournalRecord | None:
        with self._exclusive_lock():
            manifest = self._load_epoch_manifest_unlocked()
            self._require_epoch_ready(manifest)
            loaded = self._load_state_unlocked(full_archive=True)
            return None if loaded is None else loaded.record

    def load_runtime(
        self,
        *,
        account_identity_digest: str | None = None,
    ) -> CtpOrderSubmissionJournalRecord | None:
        """Load only hot and fixed-capacity recent identities for live startup."""

        with self._exclusive_lock():
            manifest = self._load_epoch_manifest_unlocked()
            self._require_epoch_ready(manifest)
            if account_identity_digest is not None:
                account_identity = _sha(account_identity_digest, "CTP runtime account identity")
                if (
                    manifest is not None
                    and manifest.current_account_identity_digest != account_identity
                ):
                    raise CtpOrderJournalIntegrityError(
                        "CTP order journal account epoch identity mismatch"
                    )
            loaded = self._load_state_unlocked(full_archive=False)
            self._runtime_sealed_fill_identity_scopes = (
                ()
                if manifest is None
                else tuple(
                    (
                        epoch.source_account_identity_digest,
                        epoch.fill_identity_bloom,
                    )
                    for epoch in manifest.sealed_epochs
                )
            )
            return None if loaded is None else loaded.record

    def contains_runtime_sealed_fill_identity(
        self,
        *,
        account_identity_digest: str,
        fill_identity: str,
    ) -> bool:
        """Check the bounded startup snapshot without callback-path file I/O."""

        account_identity = _sha(account_identity_digest, "CTP runtime account identity")
        fill_key = _fill_key(fill_identity)
        return any(
            epoch_account == account_identity and _order_identity_bloom_contains(bloom, fill_key)
            for epoch_account, bloom in self._runtime_sealed_fill_identity_scopes
        )

    def load_epoch_manifest(self) -> CtpOrderJournalEpochManifest | None:
        with self._exclusive_lock():
            manifest = self._load_epoch_manifest_unlocked()
            self._require_epoch_ready(manifest)
            return manifest

    def _load_state_unlocked(self, *, full_archive: bool = False) -> _LoadedEnvelope | None:
        if not self._exists(self.path):
            archive_evidence = any(self.path.parent.glob(f"{self.path.name}.archive.*.json"))
            runtime_index_evidence = any(
                self.path.parent.glob(f"{self.path.name}.runtime-index.*.json")
            )
            if self._exists(self.previous_path) or archive_evidence or runtime_index_evidence:
                raise CtpOrderJournalIntegrityError(
                    "current CTP order journal is missing while durable evidence exists"
                )
            return None
        current = self._read_envelope(self.path, full_archive=full_archive)
        if not self._exists(self.previous_path):
            if current.generation.sequence != 1:
                raise CtpOrderJournalIntegrityError("CTP order journal parent evidence is missing")
            return current

        previous_file = self._read_envelope(
            self.previous_path,
            full_archive=full_archive,
        )
        duplicate_current = (
            previous_file.payload == current.payload
            and previous_file.generation == current.generation
        )
        expected_parent = (
            current.previous_generation is not None
            and previous_file.generation == current.previous_generation
        )
        if not duplicate_current and not expected_parent:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal parent checksum/sequence mismatch"
            )
        return current

    def _load_unlocked(self) -> CtpOrderSubmissionJournalRecord | None:
        loaded = self._load_state_unlocked(full_archive=False)
        return None if loaded is None else loaded.record

    def load_required(self) -> CtpOrderSubmissionJournalRecord:
        record = self.load()
        if record is None:
            raise CtpOrderJournalIntegrityError("required CTP order journal is missing")
        return record

    def prepare(
        self,
        entry: CtpOrderSubmissionEntry,
        *,
        fill_identity_capacity: int = CTP_ORDER_RUNTIME_MAX_FILL_IDENTITIES,
        observed_fill_identity_count: int = 0,
    ) -> CtpOrderSubmissionEntry:
        if (
            isinstance(fill_identity_capacity, bool)
            or not isinstance(fill_identity_capacity, int)
            or fill_identity_capacity <= 0
            or fill_identity_capacity > CTP_ORDER_RUNTIME_MAX_FILL_IDENTITIES
            or isinstance(observed_fill_identity_count, bool)
            or not isinstance(observed_fill_identity_count, int)
            or observed_fill_identity_count < 0
            or observed_fill_identity_count > fill_identity_capacity
        ):
            raise ValueError("CTP order journal fill identity capacity is invalid")
        with self._exclusive_lock():
            epoch_manifest = self._load_epoch_manifest_unlocked()
            self._require_epoch_ready(epoch_manifest)
            loaded = self._load_state_unlocked(full_archive=False)
            record = None if loaded is None else loaded.record
            entries = () if record is None else record.entries
            runtime_archived = () if record is None else record.archived_entries
            total_identity_count = (
                0 if record is None else record.archive_entry_count + len(record.entries)
            )
            if total_identity_count + 1 > _ORDER_ID_BLOOM_MAX_ITEMS:
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal identity capacity cannot reserve this order; "
                    "HALTED epoch rollover is required"
                )
            archive_versions: tuple[CtpOrderSubmissionEntry, ...] = ()
            archive_count_delta = 0
            if len(entries) >= _MAX_ENTRIES:
                entries, _unused_recent, archive_count_delta = self._compact(
                    entries,
                    runtime_archived,
                )
                archive_versions = tuple(
                    item
                    for item in (() if record is None else record.entries)
                    if item.status in _FINAL_STATUSES
                )
            if len(entries) >= _MAX_ENTRIES:
                raise CtpOrderJournalIntegrityError("CTP order journal entry limit exceeded")
            # Entries compacted by this same prepare transaction are not cold yet:
            # they become pinned recent archive evidence in the generation we are
            # about to write, so their identities and late-fill capacity still count.
            all_runtime_entries = (*runtime_archived, *entries, *archive_versions)
            runtime_index = None if loaded is None else loaded.runtime_index
            if any(existing.order_id == entry.order_id for existing in all_runtime_entries) or (
                runtime_index is not None
                and _order_identity_bloom_contains(
                    runtime_index.order_identity_bloom,
                    entry.order_id,
                )
            ):
                raise CtpOrderJournalIntegrityError("CTP order identity was already reserved")
            if (
                entry.status != "prepared"
                or entry.filled_volume != 0
                or entry.fill_keys
                or entry.fill_evidence
            ):
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal prepared entry contains prior lifecycle evidence"
                )
            canonical_entry = _entry(
                _entry_payload(
                    replace(
                        entry,
                        sequence=(
                            1
                            if record is None
                            else record.archive_entry_count + len(record.entries) + 1
                        ),
                    )
                )
            )
            if epoch_manifest is not None:
                if (
                    canonical_entry.account_identity_digest
                    != epoch_manifest.current_account_identity_digest
                ):
                    raise CtpOrderJournalIntegrityError(
                        "CTP order journal account identity does not match current epoch"
                    )
                if any(
                    epoch.source_account_identity_digest == canonical_entry.account_identity_digest
                    and _order_identity_bloom_contains(
                        epoch.order_identity_bloom,
                        canonical_entry.order_id,
                    )
                    for epoch in epoch_manifest.sealed_epochs
                ):
                    raise CtpOrderJournalIntegrityError(
                        "CTP order identity was already reserved in a sealed epoch"
                    )
            durable_fill_keys = {
                fill_key for existing in all_runtime_entries for fill_key in existing.fill_keys
            }
            unresolved_fill_slots = sum(
                existing.request.volume - existing.filled_volume
                for existing in all_runtime_entries
                if existing.status != "aborted_before_send"
            )
            consumed_fill_slots = max(
                observed_fill_identity_count,
                len(durable_fill_keys),
            )
            if (
                consumed_fill_slots + unresolved_fill_slots + canonical_entry.request.volume
                > fill_identity_capacity
            ):
                raise CtpOrderJournalIntegrityError(
                    "CTP durable fill identity capacity cannot reserve this order; "
                    "HALTED journal epoch rollover is required"
                )
            if all_runtime_entries:
                existing_policy_identity = self._policy_identity(all_runtime_entries[0])
            elif runtime_index is not None:
                existing_policy_identity = runtime_index.policy_identity
            else:
                existing_policy_identity = None
            if (
                existing_policy_identity is not None
                and self._policy_identity(canonical_entry) != existing_policy_identity
            ):
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal contains inconsistent entry identity"
                )
            self._save_locked(
                record,
                (*entries, canonical_entry),
                archive_versions=archive_versions,
                archive_count_delta=archive_count_delta,
            )
            return canonical_entry

    def update_status(self, order_id: str, status: str) -> None:
        if status not in {"submitted", "terminal", "aborted_before_send"}:
            raise ValueError("unsupported CTP order journal status")
        self.apply_updates(statuses={order_id: status})

    def apply_updates(
        self,
        *,
        statuses: Mapping[str, str] | None = None,
        fills: Mapping[str, tuple[CtpOrderFillEvidence, ...]] | None = None,
    ) -> tuple[CtpOrderSubmissionEntry, ...]:
        """Atomically persist a whole callback batch in one journal revision."""

        status_updates = {} if statuses is None else dict(statuses)
        fill_updates = {} if fills is None else dict(fills)
        if any(not isinstance(order_id, str) or not order_id for order_id in status_updates):
            raise ValueError("CTP order journal status identity is invalid")
        if any(
            not isinstance(status, str)
            or status not in {"submitted", "terminal", "aborted_before_send"}
            for status in status_updates.values()
        ):
            raise ValueError("unsupported CTP order journal status")
        if any(not isinstance(order_id, str) or not order_id for order_id in fill_updates):
            raise ValueError("CTP order journal fill identity is invalid")

        with self._exclusive_lock():
            epoch_manifest = self._load_epoch_manifest_unlocked()
            self._require_epoch_ready(epoch_manifest)
            loaded = self._load_state_unlocked(full_archive=False)
            if loaded is None:
                raise CtpOrderJournalIntegrityError("required CTP order journal is missing")
            record = loaded.record
            active = list(record.entries)
            archived = list(record.archived_entries)
            locations: dict[str, tuple[str, int]] = {
                entry.order_id: ("active", index) for index, entry in enumerate(active)
            }
            locations.update(
                {entry.order_id: ("archived", index) for index, entry in enumerate(archived)}
            )
            requested = set(status_updates) | set(fill_updates)
            missing = requested - set(locations)
            if missing:
                raise CtpOrderJournalIntegrityError("unknown CTP order journal identity")

            all_fill_keys = {
                fill_key for existing in (*archived, *active) for fill_key in existing.fill_keys
            }
            changed = False
            updated_requested: dict[str, CtpOrderSubmissionEntry] = {}
            archived_versions: list[CtpOrderSubmissionEntry] = []
            for order_id in sorted(requested):
                section, index = locations[order_id]
                target_entries = active if section == "active" else archived
                current = target_entries[index]
                updated = current
                if order_id in status_updates:
                    status = status_updates[order_id]
                    if not self._allowed_status_transition(current.status, status):
                        raise CtpOrderJournalIntegrityError(
                            "invalid CTP order journal status transition"
                        )
                    updated = replace(updated, status=status)
                if order_id in fill_updates:
                    new_fill_evidence = self._validate_fill_update(fill_updates[order_id])
                    if epoch_manifest is not None and any(
                        epoch.source_account_identity_digest == current.account_identity_digest
                        and any(
                            _order_identity_bloom_contains(
                                epoch.fill_identity_bloom,
                                evidence.key,
                            )
                            for evidence in new_fill_evidence
                        )
                        for epoch in epoch_manifest.sealed_epochs
                    ):
                        raise CtpOrderJournalIntegrityError(
                            "CTP fill identity was already recorded in a sealed epoch"
                        )
                    new_fill_keys = tuple(item.key for item in new_fill_evidence)
                    delta = sum(item.volume for item in new_fill_evidence)
                    duplicates = all_fill_keys.intersection(new_fill_keys)
                    if duplicates:
                        raise CtpOrderJournalIntegrityError(
                            "CTP order journal contains duplicate global fill identity"
                        )
                    filled_volume = updated.filled_volume + delta
                    if filled_volume > updated.request.volume:
                        raise CtpOrderJournalIntegrityError(
                            "CTP order filled volume exceeds request volume"
                        )
                    updated = replace(
                        updated,
                        filled_volume=filled_volume,
                        fill_keys=tuple(sorted((*updated.fill_keys, *new_fill_keys))),
                        fill_evidence=tuple(
                            sorted(
                                (*updated.fill_evidence, *new_fill_evidence),
                                key=lambda item: item.key,
                            )
                        ),
                    )
                    all_fill_keys.update(new_fill_keys)
                canonical = _entry(_entry_payload(updated))
                if canonical != current:
                    changed = True
                    target_entries[index] = canonical
                    if section == "archived":
                        archived_versions.append(canonical)
                updated_requested[order_id] = canonical

            active_tuple = tuple(active)
            compacted_entries: tuple[CtpOrderSubmissionEntry, ...] = ()
            if len(active_tuple) >= _MAX_ENTRIES:
                compacted_entries = tuple(
                    entry for entry in active_tuple if entry.status in _FINAL_STATUSES
                )
                active_tuple = tuple(
                    entry for entry in active_tuple if entry.status not in _FINAL_STATUSES
                )
            archive_versions = tuple((*archived_versions, *compacted_entries))
            if changed or compacted_entries:
                self._save_locked(
                    record,
                    active_tuple,
                    archive_versions=archive_versions,
                    archive_count_delta=len(compacted_entries),
                )
            return tuple(updated_requested[order_id] for order_id in sorted(requested))

    def compact_terminal(self) -> int:
        with self._exclusive_lock():
            loaded = self._load_state_unlocked(full_archive=False)
            if loaded is None:
                return 0
            record = loaded.record
            terminal = tuple(entry for entry in record.entries if entry.status in _FINAL_STATUSES)
            entries = tuple(
                entry for entry in record.entries if entry.status not in _FINAL_STATUSES
            )
            count = len(terminal)
            if count:
                self._save_locked(
                    record,
                    entries,
                    archive_versions=terminal,
                    archive_count_delta=count,
                )
            return count

    def _epoch_source_paths(self) -> tuple[Path, ...]:
        candidates = [self.path, self.previous_path]
        candidates.extend(self.path.parent.glob(f"{self.path.name}.archive.*.json"))
        candidates.extend(self.path.parent.glob(f"{self.path.name}.runtime-index.*.json"))
        return tuple(
            sorted(
                {path for path in candidates if self._exists(path)},
                key=lambda item: item.name,
            )
        )

    @staticmethod
    def _unlink_epoch_source(source: Path) -> None:
        os.unlink(source)

    def _write_epoch_seal(
        self,
        epoch: CtpOrderJournalSealedEpoch,
        source_paths: tuple[Path, ...],
    ) -> None:
        self.epoch_root.mkdir(mode=0o700, parents=False, exist_ok=True)
        epoch_dir = self.epoch_root / epoch.directory_name
        epoch_dir.mkdir(mode=0o700, exist_ok=True)
        metadata = os.stat(epoch_dir, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise CtpOrderJournalIntegrityError(
                "CTP order journal epoch directory identity is invalid"
            )
        expected_paths = {path.name: path for path in source_paths}
        if set(expected_paths) != {filename for filename, _digest_value in epoch.source_files}:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal epoch source set changed before seal"
            )
        for filename, expected_digest in epoch.source_files:
            payload = self._read_bytes(expected_paths[filename])
            if sha256(payload).hexdigest() != expected_digest:
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal epoch source changed before seal"
                )
            self._write_immutable(epoch_dir / filename, payload)
        unsigned = {
            "kind": _EPOCH_SEAL_KIND,
            "schema_version": _EPOCH_SEAL_SCHEMA_VERSION,
            "epoch": _sealed_epoch_payload(epoch),
        }
        seal_payload = json.dumps(
            {**unsigned, "checksum": _digest(unsigned)},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        self._write_immutable(epoch_dir / "epoch-seal.json", seal_payload)

    def _validate_sealed_epoch_unlocked(
        self,
        epoch: CtpOrderJournalSealedEpoch,
    ) -> tuple[CtpOrderSubmissionEntry, ...]:
        epoch_dir = self.epoch_root / epoch.directory_name
        try:
            metadata = os.stat(epoch_dir, follow_symlinks=False)
        except OSError as exc:
            raise CtpOrderJournalIntegrityError(
                "sealed CTP order journal epoch directory is missing"
            ) from exc
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise CtpOrderJournalIntegrityError(
                "sealed CTP order journal epoch directory is invalid"
            )
        try:
            observed_names = {item.name for item in epoch_dir.iterdir()}
        except OSError as exc:
            raise CtpOrderJournalIntegrityError(
                "sealed CTP order journal epoch cannot be listed"
            ) from exc
        expected_names = {filename for filename, _digest_value in epoch.source_files} | {
            "epoch-seal.json"
        }
        if observed_names != expected_names:
            raise CtpOrderJournalIntegrityError("sealed CTP order journal epoch file set changed")
        for filename, expected_digest in epoch.source_files:
            if sha256(self._read_bytes(epoch_dir / filename)).hexdigest() != expected_digest:
                raise CtpOrderJournalIntegrityError(
                    "sealed CTP order journal epoch file digest mismatch"
                )
        seal_payload = self._read_bytes(epoch_dir / "epoch-seal.json")
        try:
            raw_seal = json.loads(
                seal_payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CtpOrderJournalIntegrityError(
                "sealed CTP order journal epoch seal is invalid"
            ) from exc
        if not isinstance(raw_seal, Mapping) or set(raw_seal) != {
            "kind",
            "schema_version",
            "epoch",
            "checksum",
        }:
            raise CtpOrderJournalIntegrityError("sealed CTP order journal epoch seal is invalid")
        unsigned = {key: value for key, value in raw_seal.items() if key != "checksum"}
        if (
            raw_seal["kind"] != _EPOCH_SEAL_KIND
            or raw_seal["schema_version"] != _EPOCH_SEAL_SCHEMA_VERSION
            or _sha(raw_seal["checksum"], "CTP order journal epoch seal") != _digest(unsigned)
            or _sealed_epoch(raw_seal["epoch"]) != epoch
        ):
            raise CtpOrderJournalIntegrityError("sealed CTP order journal epoch seal is invalid")
        if epoch.journal_sequence == 0:
            return ()
        sealed = CtpOrderSubmissionJournal(epoch_dir / self.path.name)
        loaded = sealed._load_state_unlocked(full_archive=True)
        if loaded is None:
            raise CtpOrderJournalIntegrityError("sealed CTP order journal epoch is missing")
        record = loaded.record
        entries = record.all_entries
        fill_keys = tuple(fill_key for entry in entries for fill_key in entry.fill_keys)
        rebuilt_bloom = _order_identity_bloom_add(
            _empty_order_identity_bloom(), tuple(item.order_id for item in entries)
        )
        rebuilt_fill_bloom = _order_identity_bloom_add(_empty_order_identity_bloom(), fill_keys)
        if (
            record.sequence != epoch.journal_sequence
            or record.checksum != epoch.journal_checksum
            or (record.archive_head_checksum or "") != epoch.archive_head_checksum
            or record.archive_entry_count != epoch.archive_entry_count
            or len(entries) != epoch.entry_count
            or len({entry.order_id for entry in entries}) != len(entries)
            or any(entry.status not in _FINAL_STATUSES for entry in entries)
            or rebuilt_bloom != epoch.order_identity_bloom
            or len(fill_keys) != len(set(fill_keys))
            or len(fill_keys) != epoch.fill_identity_count
            or rebuilt_fill_bloom != epoch.fill_identity_bloom
        ):
            raise CtpOrderJournalIntegrityError("sealed CTP order journal epoch summary mismatch")
        return entries

    def _cleanup_sealed_epoch_sources(
        self,
        epoch: CtpOrderJournalSealedEpoch,
    ) -> None:
        for filename, expected_digest in epoch.source_files:
            source = self.path.parent / filename
            if not self._exists(source):
                continue
            if sha256(self._read_bytes(source)).hexdigest() != expected_digest:
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal epoch cleanup source changed"
                )
            self._unlink_epoch_source(source)
        if self._epoch_source_paths():
            raise CtpOrderJournalIntegrityError(
                "CTP order journal epoch cleanup left unsealed evidence"
            )
        directory_descriptor = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)

    def _audit_epoch_history_unlocked(
        self,
        manifest: CtpOrderJournalEpochManifest | None,
        current_entries: tuple[CtpOrderSubmissionEntry, ...],
    ) -> tuple[int, int]:
        if manifest is None:
            return 0, 0
        seen_order_ids_by_account: dict[str, set[str]] = {}
        seen_fill_ids_by_account: dict[str, set[str]] = {}
        sealed_entry_count = 0
        sealed_fill_identity_count = 0
        for epoch in manifest.sealed_epochs:
            sealed_entries = self._validate_sealed_epoch_unlocked(epoch)
            order_ids = {entry.order_id for entry in sealed_entries}
            fill_ids = {fill_key for entry in sealed_entries for fill_key in entry.fill_keys}
            account_order_ids = seen_order_ids_by_account.setdefault(
                epoch.source_account_identity_digest,
                set(),
            )
            if account_order_ids.intersection(order_ids):
                raise CtpOrderJournalIntegrityError(
                    "duplicate CTP order identity across sealed account epochs"
                )
            account_order_ids.update(order_ids)
            account_fill_ids = seen_fill_ids_by_account.setdefault(
                epoch.source_account_identity_digest,
                set(),
            )
            if account_fill_ids.intersection(fill_ids):
                raise CtpOrderJournalIntegrityError(
                    "duplicate CTP fill identity across sealed account epochs"
                )
            account_fill_ids.update(fill_ids)
            sealed_entry_count += len(sealed_entries)
            sealed_fill_identity_count += len(fill_ids)
        current_order_ids_by_account: dict[str, set[str]] = {}
        for entry in current_entries:
            current_order_ids_by_account.setdefault(entry.account_identity_digest, set()).add(
                entry.order_id
            )
        if any(
            seen_order_ids_by_account.get(account, set()).intersection(order_ids)
            for account, order_ids in current_order_ids_by_account.items()
        ):
            raise CtpOrderJournalIntegrityError(
                "current CTP order identity duplicates a sealed account epoch"
            )
        current_fill_ids_by_account: dict[str, set[str]] = {}
        for entry in current_entries:
            current_fill_ids_by_account.setdefault(entry.account_identity_digest, set()).update(
                entry.fill_keys
            )
        if any(
            seen_fill_ids_by_account.get(account, set()).intersection(fill_ids)
            for account, fill_ids in current_fill_ids_by_account.items()
        ):
            raise CtpOrderJournalIntegrityError(
                "current CTP fill identity duplicates a sealed account epoch"
            )
        return sealed_entry_count, sealed_fill_identity_count

    def seal_epoch(
        self,
        *,
        transaction_id: str,
        source_account_identity_digest: str,
        target_account_identity_digest: str,
        trading_day: str,
        operator_reason: str,
        halted: bool,
        broker_flat: bool,
        local_flat: bool,
        no_active_orders: bool,
        reconciled: bool,
        strong_confirmation: str,
    ) -> CtpOrderJournalEpochManifest:
        """Seal one flat/reconciled order epoch without deleting its audit evidence."""

        transaction = _sha(transaction_id, "CTP order journal epoch transaction")
        source_account = _sha(source_account_identity_digest, "CTP epoch source account")
        target_account = _sha(target_account_identity_digest, "CTP epoch target account")
        day = _day(trading_day)
        reason = _epoch_reason(operator_reason)
        if (
            not halted
            or not broker_flat
            or not local_flat
            or not no_active_orders
            or not reconciled
            or strong_confirmation != CTP_ORDER_JOURNAL_EPOCH_CONFIRMATION
        ):
            raise CtpOrderJournalIntegrityError(
                "CTP order journal epoch rollover safety gates are not satisfied"
            )
        with self._exclusive_lock():
            manifest = self._load_epoch_manifest_unlocked(allow_orphan_transaction_id=transaction)
            existing = (
                None
                if manifest is None
                else next(
                    (
                        epoch
                        for epoch in manifest.sealed_epochs
                        if epoch.transaction_id == transaction
                    ),
                    None,
                )
            )
            if existing is not None:
                assert manifest is not None
                if (
                    existing.source_account_identity_digest != source_account
                    or existing.target_account_identity_digest != target_account
                    or existing.trading_day != day
                    or existing.operator_reason != reason
                    or existing != manifest.sealed_epochs[-1]
                ):
                    raise CtpOrderJournalIntegrityError(
                        "CTP order journal epoch retry identity mismatch"
                    )
                self._audit_epoch_history_unlocked(manifest, ())
                if manifest.pending_cleanup_epoch_id:
                    self._cleanup_sealed_epoch_sources(existing)
                    manifest = self._save_epoch_manifest_locked(
                        manifest,
                        current_account_identity_digest=target_account,
                        sealed_epochs=manifest.sealed_epochs,
                        pending_cleanup_epoch_id="",
                    )
                current_loaded = self._load_state_unlocked(full_archive=True)
                if current_loaded is not None:
                    raise CtpOrderJournalIntegrityError(
                        "CTP order journal epoch operation id was already consumed"
                    )
                current_entries = (
                    () if current_loaded is None else current_loaded.record.all_entries
                )
                self._audit_epoch_history_unlocked(manifest, current_entries)
                return manifest
            self._require_epoch_ready(manifest)
            if manifest is not None and manifest.current_account_identity_digest != source_account:
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal epoch source account identity mismatch"
                )
            if manifest is not None and len(manifest.sealed_epochs) >= _MAX_SEALED_EPOCHS:
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal sealed epoch capacity is exhausted"
                )
            loaded = self._load_state_unlocked(full_archive=True)
            record = None if loaded is None else loaded.record
            entries = () if record is None else record.all_entries
            self._audit_epoch_history_unlocked(manifest, entries)
            if any(entry.status not in _FINAL_STATUSES for entry in entries):
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal epoch rollover requires no unresolved orders"
                )
            if entries and any(
                entry.account_identity_digest != source_account for entry in entries
            ):
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal epoch source account identity mismatch"
                )
            source_paths = self._epoch_source_paths()
            source_files = tuple(
                sorted(
                    (
                        path.name,
                        sha256(self._read_bytes(path)).hexdigest(),
                    )
                    for path in source_paths
                )
            )
            order_ids = tuple(entry.order_id for entry in entries)
            fill_ids = tuple(fill_key for entry in entries for fill_key in entry.fill_keys)
            if len(order_ids) != len(set(order_ids)) or len(order_ids) > _ORDER_ID_BLOOM_MAX_ITEMS:
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal epoch identity set is invalid"
                )
            if (
                len(fill_ids) != len(set(fill_ids))
                or len(fill_ids) > CTP_ORDER_RUNTIME_MAX_FILL_IDENTITIES
            ):
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal epoch fill identity set is invalid"
                )
            epoch = CtpOrderJournalSealedEpoch(
                transaction_id=transaction,
                source_account_identity_digest=source_account,
                target_account_identity_digest=target_account,
                trading_day=day,
                operator_reason=reason,
                directory_name=f"epoch-{transaction}",
                source_files=source_files,
                journal_sequence=0 if record is None else record.sequence,
                journal_checksum="" if record is None else record.checksum,
                archive_head_checksum=(
                    "" if record is None else (record.archive_head_checksum or "")
                ),
                archive_entry_count=0 if record is None else record.archive_entry_count,
                entry_count=len(entries),
                order_identity_count=len(entries),
                order_identity_bloom=_order_identity_bloom_add(
                    _empty_order_identity_bloom(), order_ids
                ),
                fill_identity_count=len(fill_ids),
                fill_identity_bloom=_order_identity_bloom_add(
                    _empty_order_identity_bloom(), fill_ids
                ),
            )
            self._write_epoch_seal(epoch, source_paths)
            epochs = (*(manifest.sealed_epochs if manifest is not None else ()), epoch)
            manifest = self._save_epoch_manifest_locked(
                manifest,
                current_account_identity_digest=target_account,
                sealed_epochs=epochs,
                pending_cleanup_epoch_id=transaction,
            )
            self._cleanup_sealed_epoch_sources(epoch)
            return self._save_epoch_manifest_locked(
                manifest,
                current_account_identity_digest=target_account,
                sealed_epochs=epochs,
                pending_cleanup_epoch_id="",
            )

    def audit_epochs(self) -> CtpOrderJournalEpochAudit:
        """Fully verify current plus every immutable sealed epoch cold chain."""

        with self._exclusive_lock():
            manifest = self._load_epoch_manifest_unlocked()
            self._require_epoch_ready(manifest)
            current_loaded = self._load_state_unlocked(full_archive=True)
            current = None if current_loaded is None else current_loaded.record
            if manifest is None:
                return CtpOrderJournalEpochAudit(None, current, 0, 0, 0)
            current_entries = () if current is None else current.all_entries
            if current_entries and any(
                entry.account_identity_digest != manifest.current_account_identity_digest
                for entry in current_entries
            ):
                raise CtpOrderJournalIntegrityError(
                    "current CTP order journal account epoch identity mismatch"
                )
            sealed_entry_count, sealed_fill_identity_count = self._audit_epoch_history_unlocked(
                manifest, current_entries
            )
            return CtpOrderJournalEpochAudit(
                manifest,
                current,
                len(manifest.sealed_epochs),
                sealed_entry_count,
                sealed_fill_identity_count,
            )

    def load_lifecycle_view(
        self,
        *,
        account_identity_digest: str,
        trading_day: str,
    ) -> CtpOrderJournalLifecycleView:
        """Return atomic current/sealed classification for current-day ownership."""

        account = _sha(account_identity_digest, "CTP lifecycle account identity")
        day = _day(trading_day)
        with self._exclusive_lock():
            manifest = self._load_epoch_manifest_unlocked()
            self._require_epoch_ready(manifest)
            current_loaded = self._load_state_unlocked(full_archive=True)
            current = None if current_loaded is None else current_loaded.record
            current_entries = () if current is None else current.all_entries
            self._audit_epoch_history_unlocked(manifest, current_entries)
            current_candidates = [
                entry
                for entry in current_entries
                if entry.account_identity_digest == account and entry.target_trading_day == day
            ]
            candidates = list(current_candidates)
            if manifest is not None:
                for epoch in manifest.sealed_epochs:
                    if epoch.source_account_identity_digest != account:
                        continue
                    candidates.extend(
                        entry
                        for entry in self._validate_sealed_epoch_unlocked(epoch)
                        if entry.account_identity_digest == account
                        and entry.target_trading_day == day
                    )
            result = tuple(sorted(candidates, key=lambda item: item.order_id))
            if len({entry.order_id for entry in result}) != len(result):
                raise CtpOrderJournalIntegrityError("duplicate CTP lifecycle ownership identity")
            return CtpOrderJournalLifecycleView(
                entries=result,
                current_order_ids=frozenset(entry.order_id for entry in current_candidates),
            )

    def load_lifecycle_entries(
        self,
        *,
        account_identity_digest: str,
        trading_day: str,
    ) -> tuple[CtpOrderSubmissionEntry, ...]:
        """Return exact current-day ownership rows across current and sealed epochs."""

        return self.load_lifecycle_view(
            account_identity_digest=account_identity_digest,
            trading_day=trading_day,
        ).entries

    def get_entry(self, order_id: str) -> CtpOrderSubmissionEntry | None:
        if not isinstance(order_id, str) or not order_id:
            raise ValueError("CTP order journal identity is invalid")
        record = self.load()
        if record is None:
            return None
        return next(
            (entry for entry in record.all_entries if entry.order_id == order_id),
            None,
        )

    def load_all_entries(self) -> tuple[CtpOrderSubmissionEntry, ...]:
        record = self.load()
        return () if record is None else record.all_entries

    @staticmethod
    def _validate_fill_update(raw: object) -> tuple[CtpOrderFillEvidence, ...]:
        if not isinstance(raw, tuple) or not raw:
            raise CtpOrderJournalIntegrityError("CTP order journal fill update is invalid")
        evidence = tuple(coerce_ctp_order_fill_evidence(item) for item in raw)
        keys = tuple(item.key for item in evidence)
        if keys != tuple(sorted(set(keys))):
            raise CtpOrderJournalIntegrityError("CTP order journal fill update is invalid")
        return evidence

    @staticmethod
    def _compact(
        entries: tuple[CtpOrderSubmissionEntry, ...],
        archived_entries: tuple[CtpOrderSubmissionEntry, ...],
    ) -> tuple[
        tuple[CtpOrderSubmissionEntry, ...],
        tuple[CtpOrderSubmissionEntry, ...],
        int,
    ]:
        terminal = tuple(entry for entry in entries if entry.status in _FINAL_STATUSES)
        if not terminal:
            return entries, archived_entries, 0
        active = tuple(entry for entry in entries if entry.status not in _FINAL_STATUSES)
        archived = tuple(sorted((*archived_entries, *terminal), key=lambda item: item.sequence))
        return active, archived, len(terminal)

    @staticmethod
    def _bounded_recent_archive(
        entries: tuple[CtpOrderSubmissionEntry, ...],
        *,
        pinned_order_ids: frozenset[str] = frozenset(),
    ) -> tuple[CtpOrderSubmissionEntry, ...]:
        by_id: dict[str, CtpOrderSubmissionEntry] = {}
        for entry in entries:
            by_id[entry.order_id] = entry
        latest = tuple(by_id.values())
        days = tuple(
            sorted({entry.target_trading_day for entry in latest}, reverse=True)[
                :_RUNTIME_RECENT_DAYS
            ]
        )
        required_ids = {
            entry.order_id for entry in latest if entry.target_trading_day in days
        } | set(pinned_order_ids)
        if not required_ids.issubset(by_id) or len(required_ids) > _RUNTIME_RECENT_MAX_ENTRIES:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal recent archive capacity is exhausted; "
                "HALTED epoch rollover is required"
            )
        recent = tuple(
            sorted(
                (entry for order_id, entry in by_id.items() if order_id in required_ids),
                key=lambda item: item.sequence,
            )
        )
        return recent

    @staticmethod
    def _policy_identity(
        entry: CtpOrderSubmissionEntry,
    ) -> tuple[str, str, str, str]:
        return (
            entry.account_identity_digest,
            entry.policy_id,
            entry.policy_definition_digest,
            entry.products_manifest_digest,
        )

    def _append_archive_versions(
        self,
        head: _ArchivePointer | None,
        versions: tuple[CtpOrderSubmissionEntry, ...],
    ) -> _ArchivePointer | None:
        if not versions:
            return head
        if (
            isinstance(_ARCHIVE_SEGMENT_MAX_ENTRIES, bool)
            or not isinstance(_ARCHIVE_SEGMENT_MAX_ENTRIES, int)
            or _ARCHIVE_SEGMENT_MAX_ENTRIES <= 0
        ):
            raise CtpOrderJournalIntegrityError(
                "CTP order journal archive segment limit is invalid"
            )
        current_head = head
        for start in range(0, len(versions), _ARCHIVE_SEGMENT_MAX_ENTRIES):
            chunk = versions[start : start + _ARCHIVE_SEGMENT_MAX_ENTRIES]
            segment_sequence = 1 if current_head is None else current_head.segment_sequence + 1
            unsigned = {
                "kind": _ARCHIVE_KIND,
                "schema_version": _ARCHIVE_SCHEMA_VERSION,
                "segment_sequence": segment_sequence,
                "previous_segment": self._pointer_payload(current_head),
                "entries": [_entry_payload(entry) for entry in chunk],
            }
            checksum = _digest(unsigned)
            filename = self._archive_segment_name(segment_sequence, checksum)
            payload = json.dumps(
                {**unsigned, "checksum": checksum},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
            self._write_immutable(self.path.parent / filename, payload)
            current_head = _ArchivePointer(
                segment_sequence,
                filename,
                checksum,
            )
        return current_head

    def _save_locked(
        self,
        current: CtpOrderSubmissionJournalRecord | None,
        entries: tuple[CtpOrderSubmissionEntry, ...],
        *,
        archive_versions: tuple[CtpOrderSubmissionEntry, ...] = (),
        archive_count_delta: int = 0,
    ) -> None:
        observed = self._load_state_unlocked(full_archive=False)
        expected_identity = None if current is None else (current.sequence, current.checksum)
        observed_identity = (
            None if observed is None else (observed.record.sequence, observed.record.checksum)
        )
        if observed_identity != expected_identity:
            raise CtpOrderJournalIntegrityError("CTP order journal changed concurrently")
        if (
            isinstance(archive_count_delta, bool)
            or not isinstance(archive_count_delta, int)
            or archive_count_delta < 0
            or any(entry.status not in _FINAL_STATUSES for entry in archive_versions)
        ):
            raise CtpOrderJournalIntegrityError("CTP order journal archive update is invalid")
        previous_archive_count = 0 if observed is None else observed.generation.archive_entry_count
        archive_entry_count = previous_archive_count + archive_count_delta
        if archive_entry_count > _ORDER_ID_BLOOM_MAX_ITEMS:
            raise CtpOrderJournalIntegrityError(
                "CTP order identity Bloom capacity is exhausted; HALTED epoch rollover is required"
            )
        previous_index = None if observed is None else observed.runtime_index
        previous_recent = () if previous_index is None else previous_index.recent_entries
        previous_bloom = (
            _empty_order_identity_bloom()
            if previous_index is None
            else previous_index.order_identity_bloom
        )
        if len({entry.order_id for entry in archive_versions}) != len(archive_versions):
            raise CtpOrderJournalIntegrityError(
                "CTP order journal archive update contains duplicate identity"
            )
        previous_recent_by_id = {entry.order_id: entry for entry in previous_recent}
        for version in archive_versions:
            previous_version = previous_recent_by_id.get(version.order_id)
            if previous_version is not None:
                self._validate_entry_revision(previous_version, version)
        new_order_ids = tuple(
            entry.order_id
            for entry in archive_versions
            if not _order_identity_bloom_contains(previous_bloom, entry.order_id)
        )
        if len(new_order_ids) != archive_count_delta:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal archive identity-count transition is invalid"
            )
        archive_head = self._append_archive_versions(
            None if observed is None else observed.generation.archive_head,
            archive_versions,
        )
        runtime_index_pointer = None if observed is None else observed.generation.runtime_index
        runtime_index: _RuntimeArchiveIndex | None = previous_index
        if archive_versions:
            assert archive_head is not None
            recent_entries = self._bounded_recent_archive(
                (*previous_recent, *archive_versions),
                pinned_order_ids=frozenset(entry.order_id for entry in archive_versions),
            )
            policy_source = (*entries, *recent_entries)
            if not policy_source:
                raise CtpOrderJournalIntegrityError(
                    "CTP order journal archive policy identity is missing"
                )
            policy_identity = self._policy_identity(policy_source[0])
            updated_bloom = _order_identity_bloom_add(previous_bloom, new_order_ids)
            runtime_index_pointer = self._write_runtime_index(
                archive_head=archive_head,
                archive_entry_count=archive_entry_count,
                policy_identity=policy_identity,
                recent_entries=recent_entries,
                order_identity_count=archive_entry_count,
                order_identity_bloom=updated_bloom,
            )
            runtime_index = _RuntimeArchiveIndex(
                archive_head,
                archive_entry_count,
                policy_identity,
                recent_entries,
                archive_entry_count,
                updated_bloom,
                runtime_index_pointer.checksum,
            )
        elif archive_count_delta:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal archive count advanced without evidence"
            )
        generation = self._new_generation(
            sequence=1 if observed is None else observed.generation.sequence + 1,
            parent_checksum=(None if observed is None else observed.generation.checksum),
            entries=entries,
            archive_head=archive_head,
            archive_entry_count=archive_entry_count,
            runtime_index=runtime_index_pointer,
        )
        self._validate_runtime_snapshot(generation, runtime_index)
        current_runtime_record = CtpOrderSubmissionJournalRecord(
            generation.sequence,
            generation.entries,
            () if runtime_index is None else runtime_index.recent_entries,
            generation.parent_checksum,
            generation.checksum,
            generation.archive_entry_count,
            False,
            None if archive_head is None else archive_head.checksum,
            None if runtime_index is None else runtime_index.checksum,
            0 if runtime_index is None else runtime_index.order_identity_count,
            (
                None
                if runtime_index is None
                else sha256(runtime_index.order_identity_bloom).hexdigest()
            ),
        )
        if observed is not None:
            self._validate_archive_extension(
                observed.generation.archive_head,
                generation.archive_head,
            )
            self._validate_runtime_generation_transition(
                observed.record,
                observed.runtime_index,
                current_runtime_record,
                runtime_index,
            )
        payload = self._encode_envelope(
            generation,
            None if observed is None else observed.generation,
        )
        if observed is not None:
            self._atomic_replace(self.previous_path, observed.payload)
        self._atomic_replace(self.path, payload)

    @staticmethod
    def _write_all(descriptor: int, payload: bytes) -> None:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("CTP order journal write made no progress")
            remaining = remaining[written:]

    @classmethod
    def _write_immutable(cls, path: Path, payload: bytes) -> None:
        flags = (
            os.O_CREAT
            | os.O_EXCL
            | os.O_WRONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError as exc:
            existing_descriptor: int | None = None
            try:
                try:
                    existing_descriptor = os.open(
                        path,
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                    )
                except OSError as identity_exc:
                    raise CtpOrderJournalIntegrityError(
                        "immutable CTP order archive segment changed"
                    ) from identity_exc
                metadata = os.fstat(existing_descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or metadata.st_uid != os.geteuid()
                ):
                    raise CtpOrderJournalIntegrityError(
                        "immutable CTP order archive segment changed"
                    ) from exc
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(existing_descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                if b"".join(chunks) != payload:
                    raise CtpOrderJournalIntegrityError(
                        "immutable CTP order archive segment changed"
                    ) from exc
                os.fsync(existing_descriptor)
            finally:
                if existing_descriptor is not None:
                    os.close(existing_descriptor)
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
            return
        except OSError as exc:
            raise CtpOrderJournalIntegrityError(
                "CTP order journal archive segment cannot be created"
            ) from exc
        try:
            cls._write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)

    @classmethod
    def _atomic_replace(cls, path: Path, payload: bytes) -> None:
        temporary = path.with_name(f".{path.name}.tmp-{secrets.token_hex(16)}")
        descriptor: int | None = None
        try:
            flags = (
                os.O_CREAT
                | os.O_EXCL
                | os.O_WRONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            descriptor = os.open(temporary, flags, 0o600)
            cls._write_all(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, path)
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
