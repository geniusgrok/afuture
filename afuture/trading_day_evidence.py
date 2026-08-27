"""Checksummed Broker-derived CTP trading-day and account-lineage evidence."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile

from .account_runtime_registry import (
    AccountRuntimeBinding,
    AccountRuntimeBindingEvidence,
    AccountRuntimeRegistry,
)

_KIND = "afuture.ctp.trading-day-evidence"
_SCHEMA = 3
_SHA = re.compile(r"[0-9a-f]{64}")
_PHASES = {"unbound", "bound"}


class TradingDayEvidenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class TradingDayEvidence:
    phase: str
    trading_day: str
    account_identity_digest: str
    account_epoch: str
    canonical_runtime: str
    runtime_identity_digest: str
    account_binding_payload_digest: str
    account_binding_revision: int
    account_binding_last_operation_id: str
    account_binding_receipt_digest: str
    registry_sequence: int
    registry_checksum: str
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


def _sha_identity(raw: object, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(raw, str) or (not (allow_empty and not raw) and _SHA.fullmatch(raw) is None):
        raise TradingDayEvidenceError(f"CTP {name} evidence is invalid")
    return raw


def _positive_int(raw: object, name: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise TradingDayEvidenceError(f"CTP {name} evidence is invalid")
    return raw


def _nonnegative_int(raw: object, name: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise TradingDayEvidenceError(f"CTP {name} evidence is invalid")
    return raw


def _canonical_runtime(value: str | Path) -> tuple[str, str]:
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or not Path(raw).is_absolute():
        raise TradingDayEvidenceError("CTP evidence runtime must be an absolute path")
    canonical = os.path.realpath(os.path.normpath(raw))
    if canonical == os.path.dirname(canonical):
        raise TradingDayEvidenceError("CTP evidence runtime cannot be a filesystem root")
    return canonical, sha256(f"runtime:{canonical}".encode()).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise TradingDayEvidenceError(f"duplicate CTP trading-day evidence key: {key}")
        result[key] = value
    return result


def _receipt_fields(
    binding_evidence: AccountRuntimeBindingEvidence,
) -> tuple[AccountRuntimeBinding, str, int, str]:
    binding = binding_evidence.binding
    payload_digest = _sha_identity(
        binding_evidence.binding_payload_digest,
        "account binding payload digest",
    )
    revision = _positive_int(
        binding_evidence.binding_revision,
        "account binding revision",
    )
    receipt_digest = _sha_identity(
        binding_evidence.binding_receipt_digest,
        "account binding receipt digest",
    )
    return binding, payload_digest, revision, receipt_digest


def _require_receipt_matches(
    evidence: TradingDayEvidence,
    binding_evidence: AccountRuntimeBindingEvidence,
) -> None:
    binding, payload_digest, revision, receipt_digest = _receipt_fields(binding_evidence)
    expected = (
        getattr(binding, "account_identity_digest", None),
        getattr(binding, "account_epoch", None),
        getattr(binding, "canonical_runtime", None),
        getattr(binding, "runtime_identity_digest", None),
        payload_digest,
        revision,
        getattr(binding, "last_operation_id", None),
        receipt_digest,
    )
    actual = (
        evidence.account_identity_digest,
        evidence.account_epoch,
        evidence.canonical_runtime,
        evidence.runtime_identity_digest,
        evidence.account_binding_payload_digest,
        evidence.account_binding_revision,
        evidence.account_binding_last_operation_id,
        evidence.account_binding_receipt_digest,
    )
    if actual != expected:
        raise TradingDayEvidenceError("CTP trading-day evidence account binding receipt mismatch")


def require_authoritative_trading_day_evidence(
    evidence: TradingDayEvidence,
    *,
    policy_state: object,
    registry: AccountRuntimeRegistry,
    runtime_dir: str | Path,
    lifecycle_transaction: object | None,
) -> TradingDayEvidence:
    """Require one phase-consistent policy/registry/runtime evidence authority."""

    if (
        lifecycle_transaction is not None
        and getattr(lifecycle_transaction, "status", None) == "prepared"
    ):
        raise TradingDayEvidenceError(
            "CTP trading-day evidence authority is blocked while lifecycle is prepared"
        )
    account = getattr(policy_state, "live_account_identity_digest", None)
    epoch = getattr(policy_state, "live_account_epoch", None)
    if (account is None) != (epoch is None):
        raise TradingDayEvidenceError("Stress-90 policy account identity/epoch is partial")
    canonical_runtime, runtime_digest = _canonical_runtime(runtime_dir)
    if (
        evidence.canonical_runtime != canonical_runtime
        or evidence.runtime_identity_digest != runtime_digest
    ):
        raise TradingDayEvidenceError("CTP trading-day evidence runtime identity mismatch")
    if account is None:
        if (
            evidence.phase != "unbound"
            or any(
                (
                    evidence.account_epoch,
                    evidence.account_binding_payload_digest,
                    evidence.account_binding_last_operation_id,
                    evidence.account_binding_receipt_digest,
                    evidence.registry_checksum,
                )
            )
            or evidence.account_binding_revision != 0
            or evidence.registry_sequence != 0
        ):
            raise TradingDayEvidenceError("CTP trading-day evidence unbound phase is invalid")
        try:
            registry.require_no_active_binding(
                evidence.account_identity_digest,
                canonical_runtime,
            )
        except Exception as exc:
            raise TradingDayEvidenceError(
                "CTP trading-day evidence unbound phase conflicts with active binding"
            ) from exc
        return evidence
    if evidence.phase != "bound":
        raise TradingDayEvidenceError("CTP trading-day evidence bound phase is invalid")
    if evidence.account_identity_digest != account or evidence.account_epoch != epoch:
        raise TradingDayEvidenceError("CTP trading-day evidence policy account/epoch mismatch")
    try:
        binding_evidence = registry.require_binding_evidence(
            account,
            canonical_runtime,
            epoch,
        )
    except Exception as exc:
        raise TradingDayEvidenceError(
            "CTP trading-day evidence registry authority mismatch"
        ) from exc
    _require_receipt_matches(evidence, binding_evidence)
    return evidence


class TradingDayEvidenceStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @property
    def previous_path(self) -> Path:
        return self.path.with_name(self.path.name + ".prev")

    def _read_raw(self) -> Mapping[str, object]:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self.path,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise OSError("CTP trading-day evidence is not a regular file")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            visible = os.stat(self.path, follow_symlinks=False)
            if not stat.S_ISREG(visible.st_mode) or (visible.st_dev, visible.st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ):
                raise OSError("CTP trading-day evidence path identity changed")
            encoded = b"".join(chunks)
        except FileNotFoundError as exc:
            raise TradingDayEvidenceError(
                "Broker-derived CTP trading-day evidence is missing"
            ) from exc
        except OSError as exc:
            raise TradingDayEvidenceError(
                "CTP trading-day evidence cannot be opened safely as a regular file"
            ) from exc
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError as exc:
                    raise TradingDayEvidenceError(
                        "CTP trading-day evidence descriptor cleanup failed"
                    ) from exc
        try:
            raw = json.loads(
                encoded.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
            )
        except TradingDayEvidenceError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TradingDayEvidenceError("invalid CTP trading-day evidence JSON") from exc
        if not isinstance(raw, Mapping):
            raise TradingDayEvidenceError("CTP trading-day evidence fields are invalid")
        return raw

    def load_required(self) -> TradingDayEvidence:
        raw = self._read_raw()
        fields = {
            "kind",
            "schema_version",
            "sequence",
            "phase",
            "trading_day",
            "account_identity_digest",
            "account_epoch",
            "canonical_runtime",
            "runtime_identity_digest",
            "account_binding_payload_digest",
            "account_binding_revision",
            "account_binding_last_operation_id",
            "account_binding_receipt_digest",
            "registry_sequence",
            "registry_checksum",
            "rebind_transaction_id",
            "previous_account_identity_digest",
            "checksum",
        }
        if raw.get("kind") != _KIND or raw.get("schema_version") != _SCHEMA:
            raise TradingDayEvidenceError("CTP trading-day evidence schema is invalid")
        if set(raw) != fields:
            raise TradingDayEvidenceError("CTP trading-day evidence fields are invalid")
        phase = raw["phase"]
        if not isinstance(phase, str) or phase not in _PHASES:
            raise TradingDayEvidenceError("CTP trading-day evidence phase is invalid")
        account_epoch = _sha_identity(
            raw["account_epoch"], "account epoch", allow_empty=phase == "unbound"
        )
        binding_payload_digest = _sha_identity(
            raw["account_binding_payload_digest"],
            "account binding payload digest",
            allow_empty=phase == "unbound",
        )
        binding_last_operation = _sha_identity(
            raw["account_binding_last_operation_id"],
            "account binding last operation",
            allow_empty=phase == "unbound",
        )
        binding_receipt_digest = _sha_identity(
            raw["account_binding_receipt_digest"],
            "account binding receipt digest",
            allow_empty=phase == "unbound",
        )
        registry_checksum = _sha_identity(
            raw["registry_checksum"],
            "account registry checksum",
            allow_empty=phase == "unbound",
        )
        binding_revision = _nonnegative_int(
            raw["account_binding_revision"], "account binding revision"
        )
        registry_sequence = _nonnegative_int(raw["registry_sequence"], "registry sequence")
        if phase == "unbound":
            if (
                any(
                    (
                        account_epoch,
                        binding_payload_digest,
                        binding_last_operation,
                        binding_receipt_digest,
                        registry_checksum,
                    )
                )
                or binding_revision != 0
                or registry_sequence != 0
            ):
                raise TradingDayEvidenceError("CTP trading-day evidence unbound phase is invalid")
        elif binding_revision == 0 or registry_sequence == 0:
            raise TradingDayEvidenceError("CTP trading-day evidence bound phase is invalid")
        canonical_runtime, runtime_digest = _canonical_runtime(str(raw["canonical_runtime"]))
        if (
            raw["canonical_runtime"] != canonical_runtime
            or raw["runtime_identity_digest"] != runtime_digest
        ):
            raise TradingDayEvidenceError("CTP trading-day evidence runtime identity is invalid")
        checksum = raw["checksum"]
        unsigned = {key: value for key, value in raw.items() if key != "checksum"}
        if not isinstance(checksum, str) or checksum != _checksum(unsigned):
            raise TradingDayEvidenceError("CTP trading-day evidence checksum mismatch")
        return TradingDayEvidence(
            phase=phase,
            trading_day=_day(raw["trading_day"]),
            account_identity_digest=_sha_identity(
                raw["account_identity_digest"], "account identity"
            ),
            account_epoch=account_epoch,
            canonical_runtime=canonical_runtime,
            runtime_identity_digest=runtime_digest,
            account_binding_payload_digest=binding_payload_digest,
            account_binding_revision=binding_revision,
            account_binding_last_operation_id=binding_last_operation,
            account_binding_receipt_digest=binding_receipt_digest,
            registry_sequence=registry_sequence,
            registry_checksum=registry_checksum,
            rebind_transaction_id=_sha_identity(
                raw["rebind_transaction_id"], "rebind transaction", allow_empty=True
            ),
            previous_account_identity_digest=_sha_identity(
                raw["previous_account_identity_digest"],
                "previous account identity",
                allow_empty=True,
            ),
            sequence=_positive_int(raw["sequence"], "sequence"),
            checksum=checksum,
        )

    def save_observation(
        self,
        *,
        trading_day: str,
        account_identity_digest: str,
        runtime_dir: str | Path,
        policy_state: object,
        registry: AccountRuntimeRegistry,
    ) -> TradingDayEvidence:
        """Checkpoint a day without inventing or changing account lineage."""

        day = _day(trading_day)
        identity = _sha_identity(account_identity_digest, "account identity")
        if not self.path.exists() and self.previous_path.exists():
            raise TradingDayEvidenceError(
                "current CTP trading-day evidence is missing while .prev evidence exists"
            )
        current: TradingDayEvidence | None = None
        if self.path.exists():
            try:
                current = self.load_required()
            except TradingDayEvidenceError as exc:
                raw = self._read_raw()
                if raw.get("kind") == _KIND and raw.get("schema_version") != _SCHEMA:
                    raise TradingDayEvidenceError(
                        "old CTP trading-day evidence requires an exact lifecycle transaction"
                    ) from exc
                raise
        account = getattr(policy_state, "live_account_identity_digest", None)
        epoch = getattr(policy_state, "live_account_epoch", None)
        if (account is None) != (epoch is None):
            raise TradingDayEvidenceError("Stress-90 policy account identity/epoch is partial")
        canonical_runtime, runtime_digest = _canonical_runtime(runtime_dir)
        if account is None:
            try:
                registry.require_no_active_binding(identity, canonical_runtime)
            except Exception as exc:
                raise TradingDayEvidenceError(
                    "CTP trading-day evidence unbound phase conflicts with active binding"
                ) from exc
            phase = "unbound"
            account_epoch = ""
            binding_payload_digest = ""
            binding_revision = 0
            binding_last_operation = ""
            binding_receipt_digest = ""
            registry_sequence = 0
            registry_checksum = ""
        else:
            if identity != account:
                raise TradingDayEvidenceError("CTP trading day account identity changed")
            if current is None:
                raise TradingDayEvidenceError(
                    "bound CTP trading-day evidence requires exact lifecycle creation"
                )
            require_authoritative_trading_day_evidence(
                current,
                policy_state=policy_state,
                registry=registry,
                runtime_dir=canonical_runtime,
                lifecycle_transaction=None,
            )
            receipt = registry.require_binding_evidence(account, canonical_runtime, epoch)
            binding, payload_digest, revision, receipt_digest = _receipt_fields(receipt)
            phase = "bound"
            account_epoch = str(epoch)
            canonical_runtime = binding.canonical_runtime
            runtime_digest = binding.runtime_identity_digest
            binding_payload_digest = payload_digest
            binding_revision = revision
            binding_last_operation = binding.last_operation_id
            binding_receipt_digest = receipt_digest
            registry_sequence = receipt.registry_sequence
            registry_checksum = receipt.registry_checksum
        if current is not None:
            if day < current.trading_day:
                raise TradingDayEvidenceError("CTP trading day evidence moved backward")
            if current.account_identity_digest != identity:
                raise TradingDayEvidenceError("CTP trading day account identity changed")
            current_values = (
                current.phase,
                current.account_epoch,
                current.canonical_runtime,
                current.runtime_identity_digest,
                current.account_binding_payload_digest,
                current.account_binding_revision,
                current.account_binding_last_operation_id,
                current.account_binding_receipt_digest,
                current.registry_sequence,
                current.registry_checksum,
            )
            target_values = (
                phase,
                account_epoch,
                canonical_runtime,
                runtime_digest,
                binding_payload_digest,
                binding_revision,
                binding_last_operation,
                binding_receipt_digest,
                registry_sequence,
                registry_checksum,
            )
            if current_values == target_values and current.trading_day == day:
                return current
        return self._save(
            current=current,
            phase=phase,
            trading_day=day,
            account_identity_digest=identity,
            account_epoch=account_epoch,
            canonical_runtime=canonical_runtime,
            runtime_identity_digest=runtime_digest,
            account_binding_payload_digest=binding_payload_digest,
            account_binding_revision=binding_revision,
            account_binding_last_operation_id=binding_last_operation,
            account_binding_receipt_digest=binding_receipt_digest,
            registry_sequence=registry_sequence,
            registry_checksum=registry_checksum,
            rebind_transaction_id=("" if current is None else current.rebind_transaction_id),
            previous_account_identity_digest=(
                "" if current is None else current.previous_account_identity_digest
            ),
        )

    def bind_for_lifecycle(
        self,
        *,
        transaction: object,
        runtime_dir: str | Path,
        binding_evidence: AccountRuntimeBindingEvidence,
    ) -> TradingDayEvidence:
        """CAS evidence to the exact registry receipt produced by one lifecycle."""

        operation = getattr(transaction, "operation", None)
        status = getattr(transaction, "status", None)
        if operation not in {
            "activation",
            "reactivation",
            "account_rebase",
            "stress90_to_execution_aligned",
        } or status not in {"prepared", "committed"}:
            raise TradingDayEvidenceError(
                "CTP trading-day evidence requires an exact lifecycle transaction"
            )
        transaction_id = _sha_identity(
            getattr(transaction, "transaction_id", None), "lifecycle transaction"
        )
        operation_nonce = _sha_identity(
            getattr(transaction, "operation_nonce", None), "lifecycle operation nonce"
        )
        target_identity = _sha_identity(
            getattr(transaction, "account_identity_digest", None), "target account identity"
        )
        source_identity = _sha_identity(
            getattr(transaction, "source_account_identity_digest", ""),
            "source account identity",
            allow_empty=operation == "activation",
        )
        source_epoch = _sha_identity(
            getattr(transaction, "source_account_epoch", ""),
            "source account epoch",
            allow_empty=operation == "activation",
        )
        policy_target = getattr(transaction, "policy_target", None)
        policy_target_identity = _sha_identity(
            getattr(policy_target, "live_account_identity_digest", None),
            "target policy account identity",
        )
        target_epoch = _sha_identity(
            getattr(policy_target, "live_account_epoch", None), "target account epoch"
        )
        if policy_target_identity != target_identity:
            raise TradingDayEvidenceError(
                "CTP lifecycle account does not match target policy identity"
            )
        if operation == "activation" and source_epoch:
            raise TradingDayEvidenceError("CTP activation lifecycle epoch must be unbound")
        if target_epoch == operation_nonce:
            raise TradingDayEvidenceError(
                "CTP account epoch cannot be substituted with lifecycle operation nonce"
            )
        day = _day(getattr(transaction, "trading_day", None))
        canonical_runtime, runtime_digest = _canonical_runtime(runtime_dir)
        binding, payload_digest, revision, receipt_digest = _receipt_fields(binding_evidence)
        if (
            getattr(binding, "account_identity_digest", None) != target_identity
            or getattr(binding, "account_epoch", None) != target_epoch
            or getattr(binding, "canonical_runtime", None) != canonical_runtime
            or getattr(binding, "runtime_identity_digest", None) != runtime_digest
            or getattr(binding, "last_operation_id", None) != operation_nonce
        ):
            raise TradingDayEvidenceError(
                "CTP lifecycle target does not match exact account binding receipt"
            )
        if not self.path.exists() and self.previous_path.exists():
            raise TradingDayEvidenceError(
                "current CTP trading-day evidence is missing while .prev evidence exists"
            )
        current: TradingDayEvidence | None = None
        legacy: Mapping[str, object] | None = None
        if self.path.exists():
            try:
                current = self.load_required()
            except TradingDayEvidenceError as exc:
                legacy = self._validated_legacy_for_lifecycle()
                if legacy is None:
                    raise exc
        previous_identity = source_identity if source_identity else target_identity
        if current is not None:
            if day < current.trading_day:
                raise TradingDayEvidenceError("CTP trading day evidence moved backward")
            exact = (
                current.phase == "bound"
                and current.trading_day == day
                and current.account_identity_digest == target_identity
                and current.account_epoch == target_epoch
                and current.canonical_runtime == canonical_runtime
                and current.runtime_identity_digest == runtime_digest
                and current.rebind_transaction_id == transaction_id
                and current.previous_account_identity_digest == previous_identity
                and current.account_binding_payload_digest == payload_digest
                and current.account_binding_revision == revision
                and current.account_binding_last_operation_id == operation_nonce
                and current.account_binding_receipt_digest == receipt_digest
            )
            if exact:
                return current
            if operation == "activation":
                if (
                    current.phase != "unbound"
                    or current.account_identity_digest != target_identity
                    or current.canonical_runtime != canonical_runtime
                    or current.runtime_identity_digest != runtime_digest
                ):
                    raise TradingDayEvidenceError(
                        "CTP activation evidence is not exact unbound source"
                    )
            elif (
                current.phase != "bound"
                or current.account_identity_digest != source_identity
                or current.account_epoch != source_epoch
                or current.canonical_runtime != canonical_runtime
                or current.runtime_identity_digest != runtime_digest
            ):
                raise TradingDayEvidenceError(
                    "CTP trading-day evidence does not match lifecycle source account/epoch"
                )
        if legacy is not None:
            if day < _day(legacy["trading_day"]):
                raise TradingDayEvidenceError("CTP trading day evidence moved backward")
            legacy_identity = _sha_identity(
                legacy["account_identity_digest"], "legacy account identity"
            )
            expected_identity = target_identity if operation == "activation" else source_identity
            if legacy_identity != expected_identity:
                raise TradingDayEvidenceError(
                    "legacy CTP evidence does not match exact lifecycle source"
                )
        return self._save(
            current=current,
            sequence=(None if legacy is None else _positive_int(legacy["sequence"], "sequence")),
            phase="bound",
            trading_day=day,
            account_identity_digest=target_identity,
            account_epoch=target_epoch,
            canonical_runtime=canonical_runtime,
            runtime_identity_digest=runtime_digest,
            account_binding_payload_digest=payload_digest,
            account_binding_revision=revision,
            account_binding_last_operation_id=operation_nonce,
            account_binding_receipt_digest=receipt_digest,
            registry_sequence=binding_evidence.registry_sequence,
            registry_checksum=_sha_identity(
                binding_evidence.registry_checksum,
                "account registry checksum",
            ),
            rebind_transaction_id=transaction_id,
            previous_account_identity_digest=previous_identity,
        )

    def _validated_legacy_for_lifecycle(self) -> Mapping[str, object] | None:
        raw = self._read_raw()
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
        if set(raw) != fields or raw.get("kind") != _KIND or raw.get("schema_version") != 2:
            return None
        unsigned = {key: value for key, value in raw.items() if key != "checksum"}
        if raw.get("checksum") != _checksum(unsigned):
            raise TradingDayEvidenceError("legacy CTP trading-day evidence checksum mismatch")
        _positive_int(raw["sequence"], "legacy sequence")
        _day(raw["trading_day"])
        _sha_identity(raw["account_identity_digest"], "legacy account identity")
        _sha_identity(raw["account_epoch"], "legacy account epoch")
        _sha_identity(raw["rebind_transaction_id"], "legacy transaction", allow_empty=True)
        _sha_identity(
            raw["previous_account_identity_digest"],
            "legacy previous account identity",
            allow_empty=True,
        )
        return raw

    def _save(
        self,
        *,
        current: TradingDayEvidence | None,
        phase: str,
        trading_day: str,
        account_identity_digest: str,
        account_epoch: str,
        canonical_runtime: str,
        runtime_identity_digest: str,
        account_binding_payload_digest: str,
        account_binding_revision: int,
        account_binding_last_operation_id: str,
        account_binding_receipt_digest: str,
        registry_sequence: int,
        registry_checksum: str,
        rebind_transaction_id: str,
        previous_account_identity_digest: str,
        sequence: int | None = None,
    ) -> TradingDayEvidence:
        next_sequence = (
            current.sequence + 1
            if current is not None
            else (1 if sequence is None else sequence + 1)
        )
        unsigned = {
            "kind": _KIND,
            "schema_version": _SCHEMA,
            "sequence": next_sequence,
            "phase": phase,
            "trading_day": trading_day,
            "account_identity_digest": account_identity_digest,
            "account_epoch": account_epoch,
            "canonical_runtime": canonical_runtime,
            "runtime_identity_digest": runtime_identity_digest,
            "account_binding_payload_digest": account_binding_payload_digest,
            "account_binding_revision": account_binding_revision,
            "account_binding_last_operation_id": account_binding_last_operation_id,
            "account_binding_receipt_digest": account_binding_receipt_digest,
            "registry_sequence": registry_sequence,
            "registry_checksum": registry_checksum,
            "rebind_transaction_id": rebind_transaction_id,
            "previous_account_identity_digest": previous_account_identity_digest,
        }
        checksum = _checksum(unsigned)
        encoded = json.dumps({**unsigned, "checksum": checksum}, indent=2, sort_keys=True).encode()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._atomic(self.previous_path, self.path.read_bytes())
        self._atomic(self.path, encoded)
        return self.load_required()

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
