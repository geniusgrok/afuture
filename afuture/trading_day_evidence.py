"""Checksummed Broker-derived CTP trading-day evidence for offline data preparation."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile

_KIND = "afuture.ctp.trading-day-evidence"
_SCHEMA = 2
_SHA = re.compile(r"[0-9a-f]{64}")


class TradingDayEvidenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class TradingDayEvidence:
    trading_day: str
    account_identity_digest: str
    account_epoch: str
    rebind_transaction_id: str
    previous_account_identity_digest: str
    sequence: int
    checksum: str


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def _checksum(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _day(raw: object) -> str:
    if not isinstance(raw, str):
        raise TradingDayEvidenceError("CTP trading day evidence must be YYYYMMDD")
    try:
        parsed = datetime.strptime(raw, "%Y%m%d").strftime("%Y%m%d")
    except ValueError as exc:
        raise TradingDayEvidenceError("CTP trading day evidence must be YYYYMMDD") from exc
    if parsed != raw:
        raise TradingDayEvidenceError("CTP trading day evidence must be YYYYMMDD")
    return raw


def _sha_identity(raw: object, name: str) -> str:
    if not isinstance(raw, str) or _SHA.fullmatch(raw) is None:
        raise TradingDayEvidenceError(f"CTP {name} evidence is invalid")
    return raw


class TradingDayEvidenceStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @property
    def previous_path(self) -> Path:
        return self.path.with_name(self.path.name + ".prev")

    def load_required(self) -> TradingDayEvidence:
        if not self.path.exists():
            raise TradingDayEvidenceError("Broker-derived CTP trading-day evidence is missing")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise TradingDayEvidenceError("invalid CTP trading-day evidence JSON") from exc
        fields = {
            "kind",
            "schema_version",
            "sequence",
            "trading_day",
            "account_identity_digest",
            "account_epoch",
            "rebind_transaction_id",
            "previous_account_identity_digest",
            "checksum",
        }
        if not isinstance(raw, dict) or set(raw) != fields:
            raise TradingDayEvidenceError("CTP trading-day evidence fields are invalid")
        if raw["kind"] != _KIND or raw["schema_version"] != _SCHEMA:
            raise TradingDayEvidenceError("CTP trading-day evidence schema is invalid")
        sequence = raw["sequence"]
        identity = raw["account_identity_digest"]
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise TradingDayEvidenceError("CTP trading-day evidence sequence is invalid")
        if not isinstance(identity, str) or _SHA.fullmatch(identity) is None:
            raise TradingDayEvidenceError("CTP account identity evidence is invalid")
        epoch = raw["account_epoch"]
        rebind_transaction_id = raw["rebind_transaction_id"]
        previous_identity = raw["previous_account_identity_digest"]
        if not isinstance(epoch, str) or _SHA.fullmatch(epoch) is None:
            raise TradingDayEvidenceError("CTP account epoch evidence is invalid")
        for value, name in (
            (rebind_transaction_id, "rebind transaction"),
            (previous_identity, "previous account identity"),
        ):
            if not isinstance(value, str) or (value and _SHA.fullmatch(value) is None):
                raise TradingDayEvidenceError(f"CTP {name} evidence is invalid")
        checksum = raw["checksum"]
        unsigned = {key: value for key, value in raw.items() if key != "checksum"}
        if not isinstance(checksum, str) or checksum != _checksum(unsigned):
            raise TradingDayEvidenceError("CTP trading-day evidence checksum mismatch")
        return TradingDayEvidence(
            _day(raw["trading_day"]),
            identity,
            epoch,
            rebind_transaction_id,
            previous_identity,
            sequence,
            checksum,
        )

    def save(self, *, trading_day: str, account_identity_digest: str) -> TradingDayEvidence:
        day = _day(trading_day)
        if _SHA.fullmatch(account_identity_digest or "") is None:
            raise TradingDayEvidenceError("CTP account identity evidence is invalid")
        if not self.path.exists() and self.previous_path.exists():
            raise TradingDayEvidenceError(
                "current CTP trading-day evidence is missing while .prev evidence exists"
            )
        current = self.load_required() if self.path.exists() else None
        if current is not None and day < current.trading_day:
            raise TradingDayEvidenceError("CTP trading day evidence moved backward")
        if current is not None and current.account_identity_digest != account_identity_digest:
            raise TradingDayEvidenceError("CTP trading day account identity changed")
        return self._save(
            current=current,
            trading_day=day,
            account_identity_digest=account_identity_digest,
            account_epoch=(account_identity_digest if current is None else current.account_epoch),
            rebind_transaction_id=("" if current is None else current.rebind_transaction_id),
            previous_account_identity_digest=(
                "" if current is None else current.previous_account_identity_digest
            ),
        )

    def rebind_for_lifecycle(
        self,
        *,
        transaction: object,
        halted: bool,
        broker_flat: bool,
        local_flat: bool,
        no_active_orders: bool,
        reconciled: bool,
        strong_confirmation: str,
    ) -> TradingDayEvidence:
        """Bind evidence to one prepared rebase; ordinary saves can never rebind."""

        from .directional_stress90_state import REBASE_CONFIRMATION

        if any(
            value is not True
            for value in (halted, broker_flat, local_flat, no_active_orders, reconciled)
        ):
            raise TradingDayEvidenceError("CTP trading-day evidence rebind lifecycle gate failed")
        if strong_confirmation != REBASE_CONFIRMATION:
            raise TradingDayEvidenceError("CTP trading-day evidence rebind confirmation is invalid")
        operation = getattr(transaction, "operation", None)
        status = getattr(transaction, "status", None)
        if operation != "account_rebase" or status not in {"prepared", "committed"}:
            raise TradingDayEvidenceError(
                "CTP trading-day evidence rebind requires an account-rebase transaction"
            )
        transaction_id = _sha_identity(
            getattr(transaction, "transaction_id", None),
            "lifecycle transaction",
        )
        operation_nonce = _sha_identity(
            getattr(transaction, "operation_nonce", None),
            "account epoch",
        )
        source_identity = _sha_identity(
            getattr(transaction, "source_account_identity_digest", None),
            "source account identity",
        )
        target_identity = _sha_identity(
            getattr(transaction, "account_identity_digest", None),
            "target account identity",
        )
        trading_day = getattr(transaction, "trading_day", None)
        day = _day(trading_day)
        current = self.load_required()
        if day < current.trading_day:
            raise TradingDayEvidenceError("CTP trading day evidence moved backward")
        if (
            current.account_identity_digest == target_identity
            and current.rebind_transaction_id == transaction_id
            and current.account_epoch == operation_nonce
        ):
            return current
        if current.account_identity_digest != source_identity:
            raise TradingDayEvidenceError(
                "CTP trading-day evidence does not match lifecycle source account"
            )
        return self._save(
            current=current,
            trading_day=day,
            account_identity_digest=target_identity,
            account_epoch=operation_nonce,
            rebind_transaction_id=transaction_id,
            previous_account_identity_digest=source_identity,
        )

    def _save(
        self,
        *,
        current: TradingDayEvidence | None,
        trading_day: str,
        account_identity_digest: str,
        account_epoch: str,
        rebind_transaction_id: str,
        previous_account_identity_digest: str,
    ) -> TradingDayEvidence:
        sequence = 1 if current is None else current.sequence + 1
        unsigned = {
            "kind": _KIND,
            "schema_version": _SCHEMA,
            "sequence": sequence,
            "trading_day": trading_day,
            "account_identity_digest": account_identity_digest,
            "account_epoch": account_epoch,
            "rebind_transaction_id": rebind_transaction_id,
            "previous_account_identity_digest": previous_account_identity_digest,
        }
        checksum = _checksum(unsigned)
        encoded = json.dumps({**unsigned, "checksum": checksum}, indent=2, sort_keys=True).encode()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if current is not None:
            self._atomic(self.previous_path, self.path.read_bytes())
        self._atomic(self.path, encoded)
        return TradingDayEvidence(
            trading_day,
            account_identity_digest,
            account_epoch,
            rebind_transaction_id,
            previous_account_identity_digest,
            sequence,
            checksum,
        )

    @staticmethod
    def _atomic(path: Path, payload: bytes) -> None:
        temporary: Path | None = None
        try:
            with NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
