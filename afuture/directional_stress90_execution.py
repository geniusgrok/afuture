"""Durable mechanical transition intent for reduction-first Stress-90 execution."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from math import isfinite
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import MappingProxyType

from .directional_stress90_policy import STRESS90_POLICY

_KIND = "afuture.directional.stress90.execution-intent"
_SCHEMA_VERSION = 7
_SHA = re.compile(r"[0-9a-f]{64}")


class Stress90ExecutionIntentIntegrityError(RuntimeError):
    pass


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Stress90ExecutionIntentIntegrityError(
                f"duplicate Stress-90 execution intent JSON key: {key}"
            )
        result[key] = value
    return result


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
        raise Stress90ExecutionIntentIntegrityError(
            "Stress-90 execution intent is not canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _day(raw: object) -> str:
    if not isinstance(raw, str):
        raise Stress90ExecutionIntentIntegrityError("execution intent day must be YYYYMMDD")
    try:
        value = datetime.strptime(raw, "%Y%m%d").strftime("%Y%m%d")
    except ValueError as exc:
        raise Stress90ExecutionIntentIntegrityError(
            "execution intent day must be YYYYMMDD"
        ) from exc
    if value != raw:
        raise Stress90ExecutionIntentIntegrityError("execution intent day must be YYYYMMDD")
    return raw


def _sha(raw: object, *, name: str) -> str:
    if not isinstance(raw, str) or _SHA.fullmatch(raw) is None:
        raise Stress90ExecutionIntentIntegrityError(f"{name} must be SHA-256")
    return raw


def _lots(raw: object, *, name: str) -> Mapping[str, int]:
    if not isinstance(raw, Mapping):
        raise Stress90ExecutionIntentIntegrityError(f"{name} must be an object")
    result: dict[str, int] = {}
    for symbol, volume in raw.items():
        if (
            not isinstance(symbol, str)
            or not symbol
            or isinstance(volume, bool)
            or not isinstance(volume, int)
            or volume == 0
        ):
            raise Stress90ExecutionIntentIntegrityError(f"{name} is invalid")
        result[symbol] = volume
    return MappingProxyType(dict(sorted(result.items())))


@dataclass(frozen=True)
class Stress90ExecutionTransition:
    product: str
    kind: str
    source_symbols: tuple[str, ...]
    target_symbol: str
    target_sign: int
    max_replacement_notional: float


@dataclass(frozen=True)
class Stress90ExecutionIntent:
    policy_definition_digest: str
    products_manifest_digest: str
    risk_overlay_digest: str
    account_identity_digest: str
    account_epoch: str
    target_trading_day: str
    daily_decision_digest: str
    initial_current_lots: Mapping[str, int]
    initial_margin_fitted_lots: Mapping[str, int]
    freeze_authorized_lots: Mapping[str, int]
    transitions: tuple[Stress90ExecutionTransition, ...]
    source_digest: str

    @property
    def authorized_transition_products(self) -> tuple[str, ...]:
        return tuple(transition.product for transition in self.transitions)

    @property
    def authorized_transition_kinds(self) -> Mapping[str, str]:
        return MappingProxyType(
            {transition.product: transition.kind for transition in self.transitions}
        )

    @property
    def authorized_transition_max_replacement_notionals(self) -> Mapping[str, float]:
        return MappingProxyType(
            {
                transition.product: transition.max_replacement_notional
                for transition in self.transitions
            }
        )


@dataclass(frozen=True)
class Stress90ExecutionIntentRetirement:
    transaction_id: str
    operation: str
    operation_nonce: str
    policy_source_checksum: str
    source_account_identity_digest: str
    source_account_epoch: str
    target_account_identity_digest: str
    target_account_epoch: str
    trading_day: str


@dataclass(frozen=True)
class Stress90ExecutionIntentRecord:
    intent: Stress90ExecutionIntent
    retirement: Stress90ExecutionIntentRetirement | None
    sequence: int
    parent_checksum: str | None
    checksum: str

    @property
    def retired(self) -> bool:
        return self.retirement is not None

    @property
    def effective_account_identity_digest(self) -> str:
        if self.retirement is None:
            return self.intent.account_identity_digest
        return self.retirement.target_account_identity_digest

    @property
    def effective_account_epoch(self) -> str:
        if self.retirement is None:
            return self.intent.account_epoch
        return self.retirement.target_account_epoch


def _payload(intent: Stress90ExecutionIntent) -> dict[str, object]:
    return {
        "policy_definition_digest": intent.policy_definition_digest,
        "products_manifest_digest": intent.products_manifest_digest,
        "risk_overlay_digest": intent.risk_overlay_digest,
        "account_identity_digest": intent.account_identity_digest,
        "account_epoch": intent.account_epoch,
        "target_trading_day": intent.target_trading_day,
        "daily_decision_digest": intent.daily_decision_digest,
        "initial_current_lots": dict(intent.initial_current_lots),
        "initial_margin_fitted_lots": dict(intent.initial_margin_fitted_lots),
        "freeze_authorized_lots": dict(intent.freeze_authorized_lots),
        "transitions": [
            {
                "product": transition.product,
                "kind": transition.kind,
                "source_symbols": list(transition.source_symbols),
                "target_symbol": transition.target_symbol,
                "target_sign": transition.target_sign,
                "max_replacement_notional": transition.max_replacement_notional,
            }
            for transition in intent.transitions
        ],
        "source_digest": intent.source_digest,
    }


def _retirement_payload(
    retirement: Stress90ExecutionIntentRetirement | None,
) -> dict[str, str] | None:
    if retirement is None:
        return None
    return {
        "transaction_id": retirement.transaction_id,
        "operation": retirement.operation,
        "operation_nonce": retirement.operation_nonce,
        "policy_source_checksum": retirement.policy_source_checksum,
        "source_account_identity_digest": retirement.source_account_identity_digest,
        "source_account_epoch": retirement.source_account_epoch,
        "target_account_identity_digest": retirement.target_account_identity_digest,
        "target_account_epoch": retirement.target_account_epoch,
        "trading_day": retirement.trading_day,
    }


def _retirement(raw: object) -> Stress90ExecutionIntentRetirement | None:
    if raw is None:
        return None
    fields = {
        "transaction_id",
        "operation",
        "operation_nonce",
        "policy_source_checksum",
        "source_account_identity_digest",
        "source_account_epoch",
        "target_account_identity_digest",
        "target_account_epoch",
        "trading_day",
    }
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise Stress90ExecutionIntentIntegrityError(
            "execution intent lifecycle retirement is invalid"
        )
    operation = raw["operation"]
    if operation not in {"account_rebase", "reactivation"}:
        raise Stress90ExecutionIntentIntegrityError(
            "execution intent retirement operation is invalid"
        )
    result = Stress90ExecutionIntentRetirement(
        transaction_id=_sha(raw["transaction_id"], name="retirement transaction"),
        operation=str(operation),
        operation_nonce=_sha(raw["operation_nonce"], name="retirement operation nonce"),
        policy_source_checksum=_sha(
            raw["policy_source_checksum"],
            name="retirement policy source checksum",
        ),
        source_account_identity_digest=_sha(
            raw["source_account_identity_digest"],
            name="retirement source account identity",
        ),
        source_account_epoch=_sha(
            raw["source_account_epoch"],
            name="retirement source account epoch",
        ),
        target_account_identity_digest=_sha(
            raw["target_account_identity_digest"],
            name="retirement target account identity",
        ),
        target_account_epoch=_sha(
            raw["target_account_epoch"],
            name="retirement target account epoch",
        ),
        trading_day=_day(raw["trading_day"]),
    )
    from .stress90_lifecycle_transaction import derive_stress90_account_epoch

    expected_epoch = derive_stress90_account_epoch(
        operation=result.operation,
        operation_nonce=result.operation_nonce,
        policy_source_checksum=result.policy_source_checksum,
        account_identity_digest=result.target_account_identity_digest,
        trading_day=result.trading_day,
    )
    if result.target_account_epoch != expected_epoch:
        raise Stress90ExecutionIntentIntegrityError(
            "execution intent retirement account epoch lineage mismatch"
        )
    return result


def _transitions(raw: object) -> tuple[Stress90ExecutionTransition, ...]:
    if not isinstance(raw, list):
        raise Stress90ExecutionIntentIntegrityError("execution transitions must be a list")
    result: list[Stress90ExecutionTransition] = []
    fields = {
        "product",
        "kind",
        "source_symbols",
        "target_symbol",
        "target_sign",
        "max_replacement_notional",
    }
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != fields:
            raise Stress90ExecutionIntentIntegrityError("execution transition is invalid")
        product = item["product"]
        kind = item["kind"]
        sources = item["source_symbols"]
        target_symbol = item["target_symbol"]
        target_sign = item["target_sign"]
        replacement = item["max_replacement_notional"]
        if (
            not isinstance(product, str)
            or product not in STRESS90_POLICY.products
            or kind not in {"same_product_roll", "reversal_open"}
            or not isinstance(sources, list)
            or not sources
            or any(not isinstance(symbol, str) or not symbol for symbol in sources)
            or tuple(sources) != tuple(sorted(set(sources)))
            or not isinstance(target_symbol, str)
            or not target_symbol
            or isinstance(target_sign, bool)
            or target_sign not in {-1, 1}
            or isinstance(replacement, bool)
            or not isinstance(replacement, (int, float))
            or not isfinite(float(replacement))
            or float(replacement) <= 0.0
        ):
            raise Stress90ExecutionIntentIntegrityError("execution transition is invalid")
        result.append(
            Stress90ExecutionTransition(
                product=product,
                kind=str(kind),
                source_symbols=tuple(sources),
                target_symbol=target_symbol,
                target_sign=target_sign,
                max_replacement_notional=float(replacement),
            )
        )
    normalized = tuple(sorted(result, key=lambda item: item.product))
    if tuple(result) != normalized or len({item.product for item in result}) != len(result):
        raise Stress90ExecutionIntentIntegrityError("execution transitions are not canonical")
    return normalized


def _intent(raw: object) -> Stress90ExecutionIntent:
    fields = {
        "policy_definition_digest",
        "products_manifest_digest",
        "account_identity_digest",
        "account_epoch",
        "target_trading_day",
        "daily_decision_digest",
        "initial_current_lots",
        "initial_margin_fitted_lots",
        "freeze_authorized_lots",
        "transitions",
        "source_digest",
        "risk_overlay_digest",
    }
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise Stress90ExecutionIntentIntegrityError("execution intent fields are invalid")
    result = Stress90ExecutionIntent(
        policy_definition_digest=_sha(
            raw["policy_definition_digest"], name="execution policy digest"
        ),
        products_manifest_digest=_sha(
            raw["products_manifest_digest"], name="execution products digest"
        ),
        risk_overlay_digest=_sha(raw["risk_overlay_digest"], name="execution risk overlay digest"),
        account_identity_digest=_sha(
            raw["account_identity_digest"], name="execution account identity"
        ),
        account_epoch=_sha(raw["account_epoch"], name="execution account epoch"),
        target_trading_day=_day(raw["target_trading_day"]),
        daily_decision_digest=_sha(raw["daily_decision_digest"], name="execution decision digest"),
        initial_current_lots=_lots(raw["initial_current_lots"], name="initial current lots"),
        initial_margin_fitted_lots=_lots(
            raw["initial_margin_fitted_lots"], name="initial margin-fitted lots"
        ),
        freeze_authorized_lots=_lots(raw["freeze_authorized_lots"], name="freeze-authorized lots"),
        transitions=_transitions(raw["transitions"]),
        source_digest=_sha(raw["source_digest"], name="execution source digest"),
    )
    if result.policy_definition_digest != STRESS90_POLICY.policy_definition_digest:
        raise Stress90ExecutionIntentIntegrityError("execution policy definition mismatch")
    if result.products_manifest_digest != STRESS90_POLICY.products_manifest_digest:
        raise Stress90ExecutionIntentIntegrityError("execution products manifest mismatch")
    for symbol, volume in result.freeze_authorized_lots.items():
        fitted_volume = result.initial_margin_fitted_lots.get(symbol)
        if (
            fitted_volume is None
            or (volume > 0) != (fitted_volume > 0)
            or abs(volume) > abs(fitted_volume)
        ):
            raise Stress90ExecutionIntentIntegrityError(
                "freeze-authorized lots exceed margin-fitted intent"
            )
    unsigned = {
        "account_epoch": result.account_epoch,
        "account_identity_digest": result.account_identity_digest,
        "risk_overlay_digest": result.risk_overlay_digest,
        "current_lots": dict(result.initial_current_lots),
        "decision_digest": result.daily_decision_digest,
        "freeze_authorized_lots": dict(result.freeze_authorized_lots),
        "margin_fitted_lots": dict(result.initial_margin_fitted_lots),
        "target_trading_day": result.target_trading_day,
        "transitions": _payload(result)["transitions"],
    }
    if result.source_digest != _digest(unsigned):
        raise Stress90ExecutionIntentIntegrityError("execution intent source digest mismatch")
    return result


class Stress90ExecutionIntentStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve(strict=False)

    @property
    def previous_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.prev")

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.lock")

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise Stress90ExecutionIntentIntegrityError("execution intent lock failed") from exc
        locked = False
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != os.geteuid()
            ):
                raise Stress90ExecutionIntentIntegrityError(
                    "execution intent lock identity is invalid"
                )
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = True
            path_metadata = os.stat(self.lock_path, follow_symlinks=False)
            current_metadata = os.fstat(descriptor)
            if (path_metadata.st_dev, path_metadata.st_ino) != (
                current_metadata.st_dev,
                current_metadata.st_ino,
            ) or current_metadata.st_nlink != 1:
                raise Stress90ExecutionIntentIntegrityError(
                    "execution intent lock was replaced concurrently"
                )
            yield
        finally:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def load_record(self) -> Stress90ExecutionIntentRecord | None:
        with self._exclusive_lock():
            return self._load_unlocked()

    def _load_unlocked(self) -> Stress90ExecutionIntentRecord | None:
        if not self.path.exists():
            if self.previous_path.exists():
                raise Stress90ExecutionIntentIntegrityError(
                    "current execution intent is missing while .prev evidence exists"
                )
            return None
        record = self._read_record(self.path)
        previous = self._read_record(self.previous_path) if self.previous_path.exists() else None
        if previous is not None and (
            previous.sequence == record.sequence
            and previous.checksum == record.checksum
            and previous == record
        ):
            # A crash after atomically replacing `.prev` but before replacing the
            # current file leaves two byte-equivalent records.  Current remains the
            # sole authority; the duplicate is forensic evidence, never fallback.
            if record.sequence == 1 and (record.parent_checksum is not None or record.retired):
                raise Stress90ExecutionIntentIntegrityError(
                    "execution intent initial parent evidence is invalid"
                )
            return record
        if record.sequence == 1:
            if record.parent_checksum is not None or previous is not None or record.retired:
                raise Stress90ExecutionIntentIntegrityError(
                    "execution intent initial parent evidence is invalid"
                )
            return record
        if record.parent_checksum is None or previous is None:
            raise Stress90ExecutionIntentIntegrityError(
                "execution intent parent evidence is missing"
            )
        if previous.sequence != record.sequence - 1 or previous.checksum != record.parent_checksum:
            raise Stress90ExecutionIntentIntegrityError(
                "execution intent parent checksum/sequence mismatch"
            )
        if record.retirement is not None and (
            record.intent != previous.intent
            or record.retirement.source_account_identity_digest
            != previous.effective_account_identity_digest
            or record.retirement.source_account_epoch != previous.effective_account_epoch
        ):
            raise Stress90ExecutionIntentIntegrityError(
                "execution intent retirement source lineage mismatch"
            )
        return record

    def _read_record(self, path: Path) -> Stress90ExecutionIntentRecord:
        try:
            raw = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except json.JSONDecodeError as exc:
            raise Stress90ExecutionIntentIntegrityError("invalid execution intent JSON") from exc
        fields = {
            "kind",
            "schema_version",
            "sequence",
            "parent_checksum",
            "intent",
            "retirement",
            "checksum",
        }
        if not isinstance(raw, Mapping) or set(raw) != fields:
            raise Stress90ExecutionIntentIntegrityError("execution intent envelope is invalid")
        if raw["kind"] != _KIND or raw["schema_version"] != _SCHEMA_VERSION:
            raise Stress90ExecutionIntentIntegrityError("execution intent schema is invalid")
        sequence = raw["sequence"]
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise Stress90ExecutionIntentIntegrityError("execution intent sequence is invalid")
        parent_checksum = raw["parent_checksum"]
        if parent_checksum is not None:
            parent_checksum = _sha(parent_checksum, name="execution intent parent checksum")
        checksum = _sha(raw["checksum"], name="execution intent checksum")
        unsigned = {key: value for key, value in raw.items() if key != "checksum"}
        if checksum != _digest(unsigned):
            raise Stress90ExecutionIntentIntegrityError("execution intent checksum mismatch")
        return Stress90ExecutionIntentRecord(
            _intent(raw["intent"]),
            _retirement(raw["retirement"]),
            sequence,
            parent_checksum,
            checksum,
        )

    def load_required_record(self) -> Stress90ExecutionIntentRecord:
        record = self.load_record()
        if record is None:
            raise Stress90ExecutionIntentIntegrityError("required execution intent is missing")
        return record

    def save(
        self,
        intent: Stress90ExecutionIntent,
        *,
        expected_sequence: int | None = None,
    ) -> Stress90ExecutionIntentRecord:
        validated = _intent(_payload(intent))
        with self._exclusive_lock():
            current = self._load_unlocked()
            current_sequence = 0 if current is None else current.sequence
            if expected_sequence is not None and expected_sequence != current_sequence:
                raise Stress90ExecutionIntentIntegrityError(
                    "execution intent sequence changed concurrently"
                )
            return self._save_unlocked(current, validated, retirement=None)

    def retire_for_account_rebase(
        self,
        *,
        transaction_id: str,
        operation_nonce: str,
        policy_source_checksum: str,
        source_account_identity_digest: str,
        source_account_epoch: str,
        target_account_identity_digest: str,
        target_account_epoch: str,
        trading_day: str,
        halted: bool,
        broker_flat: bool,
        local_flat: bool,
        no_active_orders: bool,
        reconciled: bool,
        strong_confirmation: str,
    ) -> Stress90ExecutionIntentRecord | None:
        """Append an idempotent lifecycle fence; never delete stale intent evidence."""

        from .directional_stress90_state import REBASE_CONFIRMATION

        if strong_confirmation != REBASE_CONFIRMATION:
            raise Stress90ExecutionIntentIntegrityError(
                "execution intent retirement lifecycle gate failed"
            )
        return self._retire_for_lifecycle(
            operation="account_rebase",
            transaction_id=transaction_id,
            operation_nonce=operation_nonce,
            policy_source_checksum=policy_source_checksum,
            source_account_identity_digest=source_account_identity_digest,
            source_account_epoch=source_account_epoch,
            target_account_identity_digest=target_account_identity_digest,
            target_account_epoch=target_account_epoch,
            trading_day=trading_day,
            halted=halted,
            broker_flat=broker_flat,
            local_flat=local_flat,
            no_active_orders=no_active_orders,
            reconciled=reconciled,
        )

    def retire_for_reactivation(
        self,
        *,
        transaction_id: str,
        operation_nonce: str,
        policy_source_checksum: str,
        source_account_identity_digest: str,
        source_account_epoch: str,
        target_account_identity_digest: str,
        target_account_epoch: str,
        trading_day: str,
        halted: bool,
        broker_flat: bool,
        local_flat: bool,
        no_active_orders: bool,
        reconciled: bool,
        activation_confirmation: str,
        rebase_confirmation: str,
    ) -> Stress90ExecutionIntentRecord | None:
        """Fence the pre-migration epoch before a Stress-90 reactivation can commit."""

        from .directional_policy_activation import STRESS90_ACTIVATION_CONFIRMATION
        from .directional_stress90_state import REBASE_CONFIRMATION

        if (
            activation_confirmation != STRESS90_ACTIVATION_CONFIRMATION
            or rebase_confirmation != REBASE_CONFIRMATION
        ):
            raise Stress90ExecutionIntentIntegrityError(
                "execution intent reactivation retirement confirmation is invalid"
            )
        return self._retire_for_lifecycle(
            operation="reactivation",
            transaction_id=transaction_id,
            operation_nonce=operation_nonce,
            policy_source_checksum=policy_source_checksum,
            source_account_identity_digest=source_account_identity_digest,
            source_account_epoch=source_account_epoch,
            target_account_identity_digest=target_account_identity_digest,
            target_account_epoch=target_account_epoch,
            trading_day=trading_day,
            halted=halted,
            broker_flat=broker_flat,
            local_flat=local_flat,
            no_active_orders=no_active_orders,
            reconciled=reconciled,
        )

    def _retire_for_lifecycle(
        self,
        *,
        operation: str,
        transaction_id: str,
        operation_nonce: str,
        policy_source_checksum: str,
        source_account_identity_digest: str,
        source_account_epoch: str,
        target_account_identity_digest: str,
        target_account_epoch: str,
        trading_day: str,
        halted: bool,
        broker_flat: bool,
        local_flat: bool,
        no_active_orders: bool,
        reconciled: bool,
    ) -> Stress90ExecutionIntentRecord | None:
        from .stress90_lifecycle_transaction import derive_stress90_account_epoch

        if any(
            value is not True
            for value in (halted, broker_flat, local_flat, no_active_orders, reconciled)
        ):
            raise Stress90ExecutionIntentIntegrityError(
                "execution intent retirement lifecycle gate failed"
            )
        transaction = _sha(transaction_id, name="retirement transaction")
        nonce = _sha(operation_nonce, name="retirement operation nonce")
        source_checksum = _sha(
            policy_source_checksum,
            name="retirement policy source checksum",
        )
        source_identity = _sha(
            source_account_identity_digest,
            name="retirement source account identity",
        )
        source_epoch = _sha(
            source_account_epoch,
            name="retirement source account epoch",
        )
        target_identity = _sha(
            target_account_identity_digest,
            name="retirement target account identity",
        )
        target_epoch = _sha(
            target_account_epoch,
            name="retirement target account epoch",
        )
        day = _day(trading_day)
        expected_target_epoch = derive_stress90_account_epoch(
            operation=operation,
            operation_nonce=nonce,
            policy_source_checksum=source_checksum,
            account_identity_digest=target_identity,
            trading_day=day,
        )
        if target_epoch != expected_target_epoch:
            raise Stress90ExecutionIntentIntegrityError(
                "execution intent retirement account epoch lineage mismatch"
            )
        retirement = Stress90ExecutionIntentRetirement(
            transaction,
            operation,
            nonce,
            source_checksum,
            source_identity,
            source_epoch,
            target_identity,
            target_epoch,
            day,
        )
        with self._exclusive_lock():
            current = self._load_unlocked()
            if current is None:
                return None
            if current.retirement == retirement:
                return current
            if (
                not current.retired
                and current.effective_account_identity_digest == target_identity
                and current.effective_account_epoch == target_epoch
            ):
                # Exact retry after the committed lifecycle must not retire a newly
                # prepared target-epoch intent.
                return current
            if (
                current.effective_account_identity_digest != source_identity
                or current.effective_account_epoch != source_epoch
            ):
                raise Stress90ExecutionIntentIntegrityError(
                    "execution intent retirement source lifecycle mismatch"
                )
            return self._save_unlocked(current, current.intent, retirement=retirement)

    def _save_unlocked(
        self,
        current: Stress90ExecutionIntentRecord | None,
        intent: Stress90ExecutionIntent,
        *,
        retirement: Stress90ExecutionIntentRetirement | None,
    ) -> Stress90ExecutionIntentRecord:
        sequence = 1 if current is None else current.sequence + 1
        unsigned: dict[str, object] = {
            "kind": _KIND,
            "schema_version": _SCHEMA_VERSION,
            "sequence": sequence,
            "parent_checksum": None if current is None else current.checksum,
            "intent": _payload(intent),
            "retirement": _retirement_payload(retirement),
        }
        checksum = _digest(unsigned)
        encoded = json.dumps(
            {**unsigned, "checksum": checksum},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if current is not None:
            self._atomic_replace(self.previous_path, self.path.read_bytes())
        self._atomic_replace(self.path, encoded)
        return Stress90ExecutionIntentRecord(
            intent,
            retirement,
            sequence,
            None if current is None else current.checksum,
            checksum,
        )

    @staticmethod
    def _atomic_replace(path: Path, payload: bytes) -> None:
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


def _typed_transitions(
    current_lots: Mapping[str, int],
    target_lots: Mapping[str, int],
    symbol_products: Mapping[str, str],
    lot_notionals: Mapping[str, float],
) -> tuple[Stress90ExecutionTransition, ...]:
    current = dict(_lots(current_lots, name="current lots"))
    target = dict(_lots(target_lots, name="margin-fitted lots"))
    symbols = set(current) | set(target)
    products: dict[str, str] = {}
    for symbol in symbols:
        product = str(symbol_products.get(symbol, "")).upper()
        if product not in STRESS90_POLICY.products:
            raise Stress90ExecutionIntentIntegrityError(
                f"execution symbol product is missing: {symbol}"
            )
        products[symbol] = product
    result: list[Stress90ExecutionTransition] = []
    for product in STRESS90_POLICY.products:
        current_rows = {
            symbol: value for symbol, value in current.items() if products[symbol] == product
        }
        target_rows = {
            symbol: value for symbol, value in target.items() if products[symbol] == product
        }
        if not current_rows or not target_rows:
            continue
        current_signs = {value > 0 for value in current_rows.values()}
        target_signs = {value > 0 for value in target_rows.values()}
        if len(current_signs) != 1 or len(target_signs) != 1:
            raise Stress90ExecutionIntentIntegrityError(
                f"opposing product lots cannot authorize transition: {product}"
            )
        if current_signs == target_signs and set(current_rows) == set(target_rows):
            continue
        if len(target_rows) != 1:
            raise Stress90ExecutionIntentIntegrityError(
                f"execution transition target is ambiguous: {product}"
            )
        target_symbol, target_volume = next(iter(target_rows.items()))
        notionals: dict[str, float] = {}
        for symbol in current_rows:
            if symbol not in lot_notionals:
                raise Stress90ExecutionIntentIntegrityError(
                    f"execution lot notional is missing: {symbol}"
                )
            value = float(lot_notionals[symbol])
            if not isfinite(value) or value <= 0.0:
                raise Stress90ExecutionIntentIntegrityError(
                    f"execution lot notional is invalid: {symbol}"
                )
            notionals[symbol] = value
        result.append(
            Stress90ExecutionTransition(
                product=product,
                kind=("reversal_open" if current_signs != target_signs else "same_product_roll"),
                source_symbols=tuple(sorted(current_rows)),
                target_symbol=target_symbol,
                target_sign=1 if target_volume > 0 else -1,
                max_replacement_notional=sum(
                    abs(volume) * notionals[symbol] for symbol, volume in current_rows.items()
                ),
            )
        )
    return tuple(result)


def prepare_stress90_execution_intent(
    store: Stress90ExecutionIntentStore,
    *,
    target_trading_day: str,
    daily_decision_digest: str,
    account_identity_digest: str,
    account_epoch: str,
    risk_overlay_digest: str = "0000000000000000000000000000000000000000000000000000000000000000",
    current_lots: Mapping[str, int],
    margin_fitted_lots: Mapping[str, int],
    freeze_authorized_lots: Mapping[str, int] | None = None,
    symbol_products: Mapping[str, str],
    lot_notionals: Mapping[str, float] | None = None,
) -> Stress90ExecutionIntent:
    target = _day(target_trading_day)
    decision = _sha(daily_decision_digest, name="execution decision digest")
    account_identity = _sha(
        account_identity_digest,
        name="execution account identity",
    )
    epoch = _sha(account_epoch, name="execution account epoch")
    risk_overlay = _sha(risk_overlay_digest, name="execution risk overlay digest")
    current = _lots(current_lots, name="current lots")
    fitted = _lots(margin_fitted_lots, name="margin-fitted lots")
    authorized = _lots(
        fitted if freeze_authorized_lots is None else freeze_authorized_lots,
        name="freeze-authorized lots",
    )
    for symbol, volume in authorized.items():
        fitted_volume = fitted.get(symbol)
        if (
            fitted_volume is None
            or (volume > 0) != (fitted_volume > 0)
            or abs(volume) > abs(fitted_volume)
        ):
            raise Stress90ExecutionIntentIntegrityError(
                "freeze-authorized lots exceed margin-fitted intent"
            )
    existing = store.load_record()
    if existing is not None and not existing.retired:
        intent = existing.intent
        if intent.target_trading_day == target:
            if intent.daily_decision_digest != decision:
                raise Stress90ExecutionIntentIntegrityError(
                    "same-day execution decision identity changed"
                )
            if intent.account_identity_digest != account_identity or intent.account_epoch != epoch:
                raise Stress90ExecutionIntentIntegrityError(
                    "same-day execution intent belongs to a stale account lifecycle epoch"
                )
            if intent.risk_overlay_digest != risk_overlay:
                raise Stress90ExecutionIntentIntegrityError(
                    "same-day execution intent risk overlay changed; old intent cannot be reinterpreted"
                )
            return intent
        if target < intent.target_trading_day:
            raise Stress90ExecutionIntentIntegrityError("execution intent day moved backward")
    transitions = _typed_transitions(
        current,
        authorized,
        symbol_products,
        lot_notionals or {},
    )
    transition_payload = [
        {
            "product": transition.product,
            "kind": transition.kind,
            "source_symbols": list(transition.source_symbols),
            "target_symbol": transition.target_symbol,
            "target_sign": transition.target_sign,
            "max_replacement_notional": transition.max_replacement_notional,
        }
        for transition in transitions
    ]
    source = {
        "account_epoch": epoch,
        "account_identity_digest": account_identity,
        "risk_overlay_digest": risk_overlay,
        "current_lots": dict(current),
        "decision_digest": decision,
        "freeze_authorized_lots": dict(authorized),
        "margin_fitted_lots": dict(fitted),
        "target_trading_day": target,
        "transitions": transition_payload,
    }
    intent = Stress90ExecutionIntent(
        policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
        products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
        risk_overlay_digest=risk_overlay,
        account_identity_digest=account_identity,
        account_epoch=epoch,
        target_trading_day=target,
        daily_decision_digest=decision,
        initial_current_lots=current,
        initial_margin_fitted_lots=fitted,
        freeze_authorized_lots=authorized,
        transitions=transitions,
        source_digest=_digest(source),
    )
    return store.save(
        intent,
        expected_sequence=0 if existing is None else existing.sequence,
    ).intent
