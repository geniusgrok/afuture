"""Durable machine-local binding between economic accounts and runtime lineages."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import struct
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile

_KIND = "afuture.account-runtime-registry"
_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"[0-9a-f]{64}")
_F_OFD_SETLKW = getattr(fcntl, "F_OFD_SETLKW", 38)
_MAX_ACCOUNTS = 256
_MAX_RUNTIME_PATH = 4_096
_MAX_OPERATION_HISTORY = 1_024
PRODUCTION_ACCOUNT_RUNTIME_REGISTRY_PATH = Path("/var/lib/afuture/account-runtime-registry.json")
ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION = "INITIALIZE_AFUTURE_MACHINE_ACCOUNT_REGISTRY"
ACCOUNT_RUNTIME_TRANSFER_CONFIRMATION = "TRANSFER_STRESS90_ACCOUNT_RUNTIME"


class AccountRuntimeRegistryError(RuntimeError):
    """The machine-level account/runtime lineage cannot be trusted or advanced."""


@dataclass(frozen=True)
class AccountRuntimeBinding:
    account_identity_digest: str
    canonical_runtime: str
    runtime_identity_digest: str
    account_epoch: str
    last_operation_id: str
    operation_history: tuple[str, ...]
    retired_runtime_identity_digests: tuple[str, ...] = ()
    retired_account_identity_digests: tuple[str, ...] = ()
    retired_lineage_digests: tuple[str, ...] = ()


@dataclass(frozen=True)
class AccountRuntimeRegistryRecord:
    sequence: int
    parent_checksum: str | None
    bindings: tuple[AccountRuntimeBinding, ...]
    checksum: str


@dataclass(frozen=True)
class AccountRuntimeBindingEvidence:
    registry_sequence: int
    registry_checksum: str
    binding: AccountRuntimeBinding


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AccountRuntimeRegistryError("account runtime registry is not canonical JSON") from exc


def _digest(value: object) -> str:
    return sha256(_canonical_json(value)).hexdigest()


def _sha(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise AccountRuntimeRegistryError(f"{name} must be lowercase SHA-256")
    return value


def _canonical_runtime(value: str | Path) -> tuple[str, str]:
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or len(raw) > _MAX_RUNTIME_PATH:
        raise AccountRuntimeRegistryError("runtime path is invalid")
    path = Path(raw)
    if not path.is_absolute():
        raise AccountRuntimeRegistryError("runtime path must be absolute canonical path")
    canonical = Path(os.path.realpath(os.path.normpath(raw)))
    if canonical == Path(canonical.anchor):
        raise AccountRuntimeRegistryError("runtime path must not be a filesystem root")
    encoded = str(canonical)
    if len(encoded) > _MAX_RUNTIME_PATH:
        raise AccountRuntimeRegistryError("runtime path is too long")
    return encoded, sha256(f"runtime:{encoded}".encode()).hexdigest()


def _binding_payload(binding: AccountRuntimeBinding) -> dict[str, object]:
    return {
        "account_identity_digest": binding.account_identity_digest,
        "canonical_runtime": binding.canonical_runtime,
        "runtime_identity_digest": binding.runtime_identity_digest,
        "account_epoch": binding.account_epoch,
        "last_operation_id": binding.last_operation_id,
        "operation_history": list(binding.operation_history),
        "retired_runtime_identity_digests": list(binding.retired_runtime_identity_digests),
        "retired_account_identity_digests": list(binding.retired_account_identity_digests),
        "retired_lineage_digests": list(binding.retired_lineage_digests),
    }


def _record_payload(
    *,
    sequence: int,
    parent_checksum: str | None,
    bindings: tuple[AccountRuntimeBinding, ...],
) -> dict[str, object]:
    return {
        "kind": _KIND,
        "schema_version": _SCHEMA_VERSION,
        "sequence": sequence,
        "parent_checksum": parent_checksum,
        "bindings": [_binding_payload(item) for item in bindings],
    }


def _new_record(
    *,
    sequence: int,
    parent_checksum: str | None,
    bindings: tuple[AccountRuntimeBinding, ...],
) -> AccountRuntimeRegistryRecord:
    ordered = tuple(sorted(bindings, key=lambda item: item.account_identity_digest))
    payload = _record_payload(
        sequence=sequence,
        parent_checksum=parent_checksum,
        bindings=ordered,
    )
    return AccountRuntimeRegistryRecord(sequence, parent_checksum, ordered, _digest(payload))


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AccountRuntimeRegistryError(f"duplicate account runtime registry key: {key}")
        result[key] = value
    return result


def _decode_binding(raw: object) -> AccountRuntimeBinding:
    expected = {
        "account_identity_digest",
        "canonical_runtime",
        "runtime_identity_digest",
        "account_epoch",
        "last_operation_id",
        "operation_history",
        "retired_runtime_identity_digests",
        "retired_account_identity_digests",
        "retired_lineage_digests",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise AccountRuntimeRegistryError("account runtime binding schema is invalid")
    runtime, runtime_digest = _canonical_runtime(str(raw["canonical_runtime"]))
    if raw["canonical_runtime"] != runtime or raw["runtime_identity_digest"] != runtime_digest:
        raise AccountRuntimeRegistryError("account runtime binding path identity is invalid")
    history_raw = raw["operation_history"]
    retired_raw = raw["retired_runtime_identity_digests"]
    retired_accounts_raw = raw["retired_account_identity_digests"]
    retired_lineages_raw = raw["retired_lineage_digests"]
    if (
        not isinstance(history_raw, list)
        or not history_raw
        or len(history_raw) > _MAX_OPERATION_HISTORY
        or not isinstance(retired_raw, list)
        or len(retired_raw) > _MAX_OPERATION_HISTORY
        or not isinstance(retired_accounts_raw, list)
        or len(retired_accounts_raw) > _MAX_OPERATION_HISTORY
        or not isinstance(retired_lineages_raw, list)
        or len(retired_lineages_raw) > _MAX_OPERATION_HISTORY
    ):
        raise AccountRuntimeRegistryError("account runtime binding history is invalid")
    history = tuple(_sha(item, "account runtime operation") for item in history_raw)
    retired = tuple(_sha(item, "retired runtime identity") for item in retired_raw)
    retired_accounts = tuple(
        _sha(item, "retired economic account identity") for item in retired_accounts_raw
    )
    retired_lineages = tuple(
        _sha(item, "retired account runtime lineage") for item in retired_lineages_raw
    )
    if any(
        len(set(items)) != len(items)
        for items in (history, retired, retired_accounts, retired_lineages)
    ):
        raise AccountRuntimeRegistryError("account runtime binding history is duplicated")
    last_operation = _sha(raw["last_operation_id"], "last account runtime operation")
    if history[-1] != last_operation:
        raise AccountRuntimeRegistryError("account runtime last operation is inconsistent")
    return AccountRuntimeBinding(
        account_identity_digest=_sha(raw["account_identity_digest"], "economic account identity"),
        canonical_runtime=runtime,
        runtime_identity_digest=runtime_digest,
        account_epoch=_sha(raw["account_epoch"], "account runtime epoch"),
        last_operation_id=last_operation,
        operation_history=history,
        retired_runtime_identity_digests=retired,
        retired_account_identity_digests=retired_accounts,
        retired_lineage_digests=retired_lineages,
    )


def _lineage_digest(account: str, runtime_digest: str, epoch: str) -> str:
    return _digest(
        {
            "account_identity_digest": account,
            "runtime_identity_digest": runtime_digest,
            "account_epoch": epoch,
        }
    )


def _decode_record(data: bytes) -> AccountRuntimeRegistryRecord:
    try:
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except AccountRuntimeRegistryError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AccountRuntimeRegistryError("account runtime registry contains invalid JSON") from exc
    expected = {
        "kind",
        "schema_version",
        "sequence",
        "parent_checksum",
        "bindings",
        "checksum",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise AccountRuntimeRegistryError("account runtime registry schema is invalid")
    if raw["kind"] != _KIND or raw["schema_version"] != _SCHEMA_VERSION:
        raise AccountRuntimeRegistryError("account runtime registry identity is invalid")
    sequence = raw["sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
        raise AccountRuntimeRegistryError("account runtime registry sequence is invalid")
    parent_raw = raw["parent_checksum"]
    parent = None if parent_raw is None else _sha(parent_raw, "registry parent checksum")
    if (sequence == 1) != (parent is None):
        raise AccountRuntimeRegistryError("account runtime registry parent is invalid")
    bindings_raw = raw["bindings"]
    if not isinstance(bindings_raw, list) or len(bindings_raw) > _MAX_ACCOUNTS:
        raise AccountRuntimeRegistryError("account runtime registry bindings are invalid")
    bindings = tuple(_decode_binding(item) for item in bindings_raw)
    if tuple(item.account_identity_digest for item in bindings) != tuple(
        sorted(item.account_identity_digest for item in bindings)
    ) or len({item.account_identity_digest for item in bindings}) != len(bindings):
        raise AccountRuntimeRegistryError("account runtime registry binding order is invalid")
    checksum = _sha(raw["checksum"], "account runtime registry checksum")
    expected_checksum = _digest(
        _record_payload(sequence=sequence, parent_checksum=parent, bindings=bindings)
    )
    if checksum != expected_checksum:
        raise AccountRuntimeRegistryError("account runtime registry checksum mismatch")
    return AccountRuntimeRegistryRecord(sequence, parent, bindings, checksum)


class AccountRuntimeRegistry:
    """Checksummed OFD/CAS store for canonical economic-account runtime ownership."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.is_absolute():
            raise AccountRuntimeRegistryError("registry path must be absolute")
        self.previous_path = self.path.with_name(self.path.name + ".prev")
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def _read_bytes(self, path: Path) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise AccountRuntimeRegistryError("account runtime registry cannot be opened") from exc
        try:
            with os.fdopen(descriptor, "rb") as handle:
                return handle.read()
        except OSError as exc:
            raise AccountRuntimeRegistryError("account runtime registry cannot be read") from exc

    def _exists(self, path: Path) -> bool:
        try:
            return path.exists() or path.is_symlink()
        except OSError as exc:
            raise AccountRuntimeRegistryError("account runtime registry path is invalid") from exc

    def _load_unlocked(self, *, required: bool) -> AccountRuntimeRegistryRecord | None:
        current_exists = self._exists(self.path)
        previous_exists = self._exists(self.previous_path)
        if not current_exists:
            if previous_exists:
                raise AccountRuntimeRegistryError(
                    "account runtime registry current is missing while .prev exists"
                )
            if required:
                raise AccountRuntimeRegistryError("required account runtime registry is missing")
            return None
        current_bytes = self._read_bytes(self.path)
        current = _decode_record(current_bytes)
        if previous_exists:
            previous_bytes = self._read_bytes(self.previous_path)
            if previous_bytes == current_bytes:
                # A crash after replacing .prev but before replacing current leaves
                # two byte-identical copies of the last committed record.  Current
                # remains authoritative; this is not fallback or a state advance.
                return current
        if current.sequence == 1:
            if previous_exists:
                raise AccountRuntimeRegistryError("unexpected registry .prev for sequence 1")
            return current
        if not previous_exists:
            raise AccountRuntimeRegistryError("account runtime registry .prev evidence is missing")
        previous = _decode_record(previous_bytes)
        if (
            previous.sequence + 1 != current.sequence
            or current.parent_checksum != previous.checksum
        ):
            raise AccountRuntimeRegistryError("account runtime registry parent chain mismatch")
        return current

    @staticmethod
    def _acquire_kernel_lock(path: Path) -> int:
        descriptor = os.open("/dev/null", os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
        identity = sha256(f"account-runtime-registry:{path}".encode()).digest()
        offset = int.from_bytes(identity[:8], "big") % ((1 << 63) - 1)
        lock = struct.pack("hhqqi4x", fcntl.F_WRLCK, os.SEEK_SET, offset, 1, 0)
        try:
            fcntl.fcntl(descriptor, _F_OFD_SETLKW, lock)
        except OSError as exc:
            os.close(descriptor)
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise AccountRuntimeRegistryError("account runtime registry is locked") from exc
            raise AccountRuntimeRegistryError(
                "account runtime registry kernel lock failed"
            ) from exc
        return descriptor

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        kernel_descriptor = self._acquire_kernel_lock(self.path)
        visible_descriptor: int | None = None
        try:
            flags = (
                os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            visible_descriptor = os.open(self.lock_path, flags, 0o600)
            fcntl.flock(visible_descriptor, fcntl.LOCK_EX)
            yield
        except OSError as exc:
            raise AccountRuntimeRegistryError("account runtime registry lock failed") from exc
        finally:
            if visible_descriptor is not None:
                try:
                    fcntl.flock(visible_descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(visible_descriptor)
            os.close(kernel_descriptor)

    def _atomic_replace(self, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=f".{path.name}.",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except (OSError, UnboundLocalError):
                pass
            raise AccountRuntimeRegistryError("account runtime registry replace failed") from exc

    def _save_unlocked(
        self,
        previous: AccountRuntimeRegistryRecord | None,
        bindings: tuple[AccountRuntimeBinding, ...],
    ) -> AccountRuntimeRegistryRecord:
        record = _new_record(
            sequence=1 if previous is None else previous.sequence + 1,
            parent_checksum=None if previous is None else previous.checksum,
            bindings=bindings,
        )
        payload = {
            **_record_payload(
                sequence=record.sequence,
                parent_checksum=record.parent_checksum,
                bindings=record.bindings,
            ),
            "checksum": record.checksum,
        }
        if previous is not None:
            self._atomic_replace(self.previous_path, self._read_bytes(self.path))
        self._atomic_replace(
            self.path,
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8"),
        )
        return record

    def load(self) -> AccountRuntimeRegistryRecord | None:
        with self._exclusive_lock():
            return self._load_unlocked(required=False)

    def load_required(self) -> AccountRuntimeRegistryRecord:
        with self._exclusive_lock():
            record = self._load_unlocked(required=True)
            assert record is not None
            return record

    def initialize(self, *, strong_confirmation: str) -> AccountRuntimeRegistryRecord:
        """Provision the machine anchor once; ordinary lifecycle writes cannot recreate it."""

        if strong_confirmation != ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION:
            raise AccountRuntimeRegistryError(
                "account runtime registry initialization confirmation is invalid"
            )
        with self._exclusive_lock():
            current = self._load_unlocked(required=False)
            if current is not None:
                if current.sequence == 1 and not current.bindings:
                    return current
                raise AccountRuntimeRegistryError("account runtime registry is already initialized")
            return self._save_unlocked(None, ())

    def bind_new(
        self,
        account_identity_digest: str,
        runtime_dir: str | Path,
        account_epoch: str,
        operation_id: str,
    ) -> AccountRuntimeRegistryRecord:
        account = _sha(account_identity_digest, "economic account identity")
        runtime, runtime_digest = _canonical_runtime(runtime_dir)
        epoch = _sha(account_epoch, "account runtime epoch")
        operation = _sha(operation_id, "account runtime operation")
        with self._exclusive_lock():
            current = self._load_unlocked(required=True)
            assert current is not None
            bindings = {item.account_identity_digest: item for item in current.bindings}
            existing = bindings.get(account)
            if existing is not None:
                if existing.runtime_identity_digest != runtime_digest:
                    raise AccountRuntimeRegistryError(
                        "economic account is already bound to a different runtime"
                    )
                if (
                    existing.canonical_runtime == runtime
                    and existing.account_epoch == epoch
                    and existing.last_operation_id == operation
                ):
                    assert current is not None
                    return current
                raise AccountRuntimeRegistryError(
                    "economic account binding already exists; explicit epoch CAS is required"
                )
            retired_accounts = {
                retired
                for item in bindings.values()
                for retired in item.retired_account_identity_digests
            }
            retired_runtimes = {
                retired
                for item in bindings.values()
                for retired in item.retired_runtime_identity_digests
            }
            retired_lineages = {
                retired for item in bindings.values() for retired in item.retired_lineage_digests
            }
            if (
                account in retired_accounts
                or runtime_digest in retired_runtimes
                or _lineage_digest(account, runtime_digest, epoch) in retired_lineages
            ):
                raise AccountRuntimeRegistryError(
                    "retired account runtime lineage requires an explicit CAS transition"
                )
            if any(item.runtime_identity_digest == runtime_digest for item in bindings.values()):
                raise AccountRuntimeRegistryError(
                    "runtime is already bound to a different economic account"
                )
            if len(bindings) >= _MAX_ACCOUNTS:
                raise AccountRuntimeRegistryError("account runtime registry capacity exhausted")
            bindings[account] = AccountRuntimeBinding(
                account_identity_digest=account,
                canonical_runtime=runtime,
                runtime_identity_digest=runtime_digest,
                account_epoch=epoch,
                last_operation_id=operation,
                operation_history=(operation,),
            )
            return self._save_unlocked(current, tuple(bindings.values()))

    def switch_account_binding(
        self,
        *,
        source_account_identity_digest: str,
        target_account_identity_digest: str,
        runtime_dir: str | Path,
        source_epoch: str,
        target_epoch: str,
        operation_id: str,
    ) -> AccountRuntimeRegistryRecord:
        """Atomically retire one account epoch and bind its runtime to another."""

        source_account = _sha(
            source_account_identity_digest,
            "source economic account identity",
        )
        target_account = _sha(
            target_account_identity_digest,
            "target economic account identity",
        )
        runtime, runtime_digest = _canonical_runtime(runtime_dir)
        source = _sha(source_epoch, "source account runtime epoch")
        target = _sha(target_epoch, "target account runtime epoch")
        operation = _sha(operation_id, "account switch operation")
        if source_account == target_account or source == target:
            raise AccountRuntimeRegistryError(
                "account switch must change account identity and epoch"
            )
        source_lineage = _lineage_digest(source_account, runtime_digest, source)
        target_lineage = _lineage_digest(target_account, runtime_digest, target)
        with self._exclusive_lock():
            current = self._load_unlocked(required=True)
            assert current is not None
            bindings = {item.account_identity_digest: item for item in current.bindings}
            target_binding = bindings.get(target_account)
            if (
                target_binding is not None
                and target_binding.canonical_runtime == runtime
                and target_binding.runtime_identity_digest == runtime_digest
                and target_binding.account_epoch == target
                and target_binding.last_operation_id == operation
                and source_account in target_binding.retired_account_identity_digests
                and source_lineage in target_binding.retired_lineage_digests
            ):
                return current
            source_binding = bindings.get(source_account)
            if source_binding is None:
                raise AccountRuntimeRegistryError("account switch source binding is missing")
            if target_binding is not None:
                raise AccountRuntimeRegistryError(
                    "account switch target already has an active binding"
                )
            if (
                source_binding.canonical_runtime != runtime
                or source_binding.runtime_identity_digest != runtime_digest
                or source_binding.account_epoch != source
            ):
                raise AccountRuntimeRegistryError("account switch source CAS mismatch")
            if target_lineage in source_binding.retired_lineage_digests:
                raise AccountRuntimeRegistryError("account switch target lineage is retired")
            if operation in {
                consumed for item in bindings.values() for consumed in item.operation_history
            }:
                raise AccountRuntimeRegistryError("account switch operation was already consumed")
            if any(
                len(items) >= _MAX_OPERATION_HISTORY
                for items in (
                    source_binding.operation_history,
                    source_binding.retired_account_identity_digests,
                    source_binding.retired_lineage_digests,
                )
            ):
                raise AccountRuntimeRegistryError("account switch lineage history exhausted")
            retired_accounts = tuple(
                dict.fromkeys((*source_binding.retired_account_identity_digests, source_account))
            )
            bindings.pop(source_account)
            bindings[target_account] = replace(
                source_binding,
                account_identity_digest=target_account,
                account_epoch=target,
                last_operation_id=operation,
                operation_history=(*source_binding.operation_history, operation),
                retired_account_identity_digests=retired_accounts,
                retired_lineage_digests=(
                    *source_binding.retired_lineage_digests,
                    source_lineage,
                ),
            )
            return self._save_unlocked(current, tuple(bindings.values()))

    def require_binding(
        self,
        account_identity_digest: str,
        runtime_dir: str | Path,
        account_epoch: str | None = None,
    ) -> AccountRuntimeBinding:
        return self.require_binding_evidence(
            account_identity_digest,
            runtime_dir,
            account_epoch,
        ).binding

    def require_binding_evidence(
        self,
        account_identity_digest: str,
        runtime_dir: str | Path,
        account_epoch: str | None = None,
    ) -> AccountRuntimeBindingEvidence:
        account = _sha(account_identity_digest, "economic account identity")
        runtime, runtime_digest = _canonical_runtime(runtime_dir)
        epoch = None if account_epoch is None else _sha(account_epoch, "account runtime epoch")
        with self._exclusive_lock():
            record = self._load_unlocked(required=True)
            assert record is not None
            binding = next(
                (item for item in record.bindings if item.account_identity_digest == account),
                None,
            )
            if binding is None:
                raise AccountRuntimeRegistryError("economic account runtime binding is missing")
            if (
                binding.canonical_runtime != runtime
                or binding.runtime_identity_digest != runtime_digest
            ):
                raise AccountRuntimeRegistryError(
                    "economic account is bound to a different runtime"
                )
            if epoch is not None and binding.account_epoch != epoch:
                raise AccountRuntimeRegistryError("economic account runtime epoch mismatch")
            return AccountRuntimeBindingEvidence(
                registry_sequence=record.sequence,
                registry_checksum=record.checksum,
                binding=binding,
            )

    def advance_epoch(
        self,
        account_identity_digest: str,
        runtime_dir: str | Path,
        source_epoch: str,
        target_epoch: str,
        operation_id: str,
    ) -> AccountRuntimeRegistryRecord:
        account = _sha(account_identity_digest, "economic account identity")
        runtime, runtime_digest = _canonical_runtime(runtime_dir)
        source = _sha(source_epoch, "source account runtime epoch")
        target = _sha(target_epoch, "target account runtime epoch")
        operation = _sha(operation_id, "account runtime operation")
        if source == target:
            raise AccountRuntimeRegistryError("account runtime epoch CAS requires a new epoch")
        with self._exclusive_lock():
            current = self._load_unlocked(required=True)
            assert current is not None
            bindings = {item.account_identity_digest: item for item in current.bindings}
            binding = bindings.get(account)
            if binding is None:
                raise AccountRuntimeRegistryError("account runtime epoch CAS binding is missing")
            if (
                binding.canonical_runtime != runtime
                or binding.runtime_identity_digest != runtime_digest
            ):
                raise AccountRuntimeRegistryError("account runtime epoch CAS runtime mismatch")
            if binding.account_epoch == target and binding.last_operation_id == operation:
                return current
            if binding.account_epoch != source:
                raise AccountRuntimeRegistryError("account runtime epoch CAS source mismatch")
            if operation in binding.operation_history:
                raise AccountRuntimeRegistryError("account runtime operation was already consumed")
            if len(binding.operation_history) >= _MAX_OPERATION_HISTORY:
                raise AccountRuntimeRegistryError("account runtime operation history exhausted")
            bindings[account] = replace(
                binding,
                account_epoch=target,
                last_operation_id=operation,
                operation_history=(*binding.operation_history, operation),
            )
            return self._save_unlocked(current, tuple(bindings.values()))

    def transfer_binding(
        self,
        *,
        account_identity_digest: str,
        source_runtime_dir: str | Path,
        target_runtime_dir: str | Path,
        source_epoch: str,
        target_epoch: str,
        operation_id: str,
        operator_reason: str,
        halted: bool,
        broker_flat: bool,
        local_flat: bool,
        no_active_orders: bool,
        reconciled: bool,
        strong_confirmation: str,
    ) -> AccountRuntimeRegistryRecord:
        """Explicitly move one account lineage after all mechanical gates pass."""

        if (
            not halted
            or not broker_flat
            or not local_flat
            or not no_active_orders
            or not reconciled
            or strong_confirmation != ACCOUNT_RUNTIME_TRANSFER_CONFIRMATION
        ):
            raise AccountRuntimeRegistryError(
                "account runtime transfer safety gates are not satisfied"
            )
        if (
            not isinstance(operator_reason, str)
            or not operator_reason.strip()
            or operator_reason != operator_reason.strip()
            or len(operator_reason) > 1_000
        ):
            raise AccountRuntimeRegistryError("account runtime transfer reason is invalid")
        account = _sha(account_identity_digest, "economic account identity")
        source_runtime, source_runtime_digest = _canonical_runtime(source_runtime_dir)
        target_runtime, target_runtime_digest = _canonical_runtime(target_runtime_dir)
        source = _sha(source_epoch, "source account runtime epoch")
        target = _sha(target_epoch, "target account runtime epoch")
        operation = _sha(operation_id, "account runtime transfer operation")
        if source_runtime_digest == target_runtime_digest or source == target:
            raise AccountRuntimeRegistryError("account runtime transfer must change lineage")
        with self._exclusive_lock():
            current = self._load_unlocked(required=True)
            assert current is not None
            bindings = {item.account_identity_digest: item for item in current.bindings}
            binding = bindings.get(account)
            if binding is None:
                raise AccountRuntimeRegistryError("account runtime transfer binding is missing")
            if (
                binding.canonical_runtime == target_runtime
                and binding.runtime_identity_digest == target_runtime_digest
                and binding.account_epoch == target
                and binding.last_operation_id == operation
                and source_runtime_digest in binding.retired_runtime_identity_digests
            ):
                return current
            if (
                binding.canonical_runtime != source_runtime
                or binding.runtime_identity_digest != source_runtime_digest
                or binding.account_epoch != source
            ):
                raise AccountRuntimeRegistryError("account runtime transfer source CAS mismatch")
            if (
                operation in binding.operation_history
                or target_runtime_digest in binding.retired_runtime_identity_digests
            ):
                raise AccountRuntimeRegistryError(
                    "account runtime transfer lineage was already consumed"
                )
            if (
                len(binding.operation_history) >= _MAX_OPERATION_HISTORY
                or len(binding.retired_runtime_identity_digests) >= _MAX_OPERATION_HISTORY
            ):
                raise AccountRuntimeRegistryError("account runtime transfer history exhausted")
            bindings[account] = replace(
                binding,
                canonical_runtime=target_runtime,
                runtime_identity_digest=target_runtime_digest,
                account_epoch=target,
                last_operation_id=operation,
                operation_history=(*binding.operation_history, operation),
                retired_runtime_identity_digests=(
                    *binding.retired_runtime_identity_digests,
                    source_runtime_digest,
                ),
            )
            return self._save_unlocked(current, tuple(bindings.values()))
