"""Authenticated immutable nonce membership ledger for the machine registry.

The dictionary is a persistent 256-bit Patricia-Merkle trie.  A proof follows
strictly increasing bit indexes and is therefore bounded by 256 node reads,
while path compression avoids materialising 256 unary nodes for every nonce.
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

_SHA = re.compile(r"[0-9a-f]{64}")
_KIND_RECEIPT = "afuture.account-runtime-nonce-receipt"
_KIND_NODE = "afuture.account-runtime-nonce-node"
_KIND_READY = "afuture.account-runtime-nonce-ready"
_KIND_TRANSITION = "afuture.account-runtime-nonce-transition"
_SCHEMA = 1
EMPTY_NONCE_ROOT = sha256(b"afuture.account-runtime-nonce-empty-v1").hexdigest()
MAX_NONCE_RECEIPTS = 1_000_000
NONCE_RECEIPT_WARNING_THRESHOLD = 800_000
_MAX_RECEIPT_BYTES = 16_000
_MAX_NODE_BYTES = 4_000


class AccountRuntimeNonceLedgerError(RuntimeError):
    """The authenticated nonce membership proof is unavailable or invalid."""


@dataclass(frozen=True)
class AccountRuntimeNonceReceipt:
    operation_nonce: str
    operation_kind: str
    account_identity_digest: str
    canonical_runtime: str
    runtime_identity_digest: str
    account_epoch: str
    semantic_request_digest: str
    checksum: str = ""


@dataclass(frozen=True)
class AccountRuntimeNonceInsertion:
    old_root: str
    new_root: str
    old_count: int
    new_count: int
    receipt: AccountRuntimeNonceReceipt


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
        raise AccountRuntimeNonceLedgerError("nonce ledger value is not canonical JSON") from exc


def _digest(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _sha(value: object, name: str) -> str:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise AccountRuntimeNonceLedgerError(f"{name} must be lowercase SHA-256")
    return value


def _receipt_unsigned(receipt: AccountRuntimeNonceReceipt) -> dict[str, object]:
    return {
        "kind": _KIND_RECEIPT,
        "schema_version": _SCHEMA,
        "operation_nonce": _sha(receipt.operation_nonce, "operation nonce"),
        "operation_kind": receipt.operation_kind,
        "account_identity_digest": _sha(receipt.account_identity_digest, "account identity"),
        "canonical_runtime": receipt.canonical_runtime,
        "runtime_identity_digest": _sha(receipt.runtime_identity_digest, "runtime identity"),
        "account_epoch": _sha(receipt.account_epoch, "account epoch"),
        "semantic_request_digest": _sha(receipt.semantic_request_digest, "semantic request digest"),
    }


def build_nonce_receipt(
    *,
    operation_nonce: str,
    operation_kind: str,
    account_identity_digest: str,
    canonical_runtime: str,
    runtime_identity_digest: str,
    account_epoch: str,
    semantic_request_digest: str,
) -> AccountRuntimeNonceReceipt:
    if (
        type(operation_kind) is not str
        or not operation_kind
        or type(canonical_runtime) is not str
        or not canonical_runtime
    ):
        raise AccountRuntimeNonceLedgerError("nonce receipt identity is invalid")
    receipt = AccountRuntimeNonceReceipt(
        operation_nonce=operation_nonce,
        operation_kind=operation_kind,
        account_identity_digest=account_identity_digest,
        canonical_runtime=canonical_runtime,
        runtime_identity_digest=runtime_identity_digest,
        account_epoch=account_epoch,
        semantic_request_digest=semantic_request_digest,
    )
    return AccountRuntimeNonceReceipt(
        **{
            **receipt.__dict__,
            "checksum": _digest(_receipt_unsigned(receipt)),
        }
    )


def _receipt_bytes(receipt: AccountRuntimeNonceReceipt) -> bytes:
    unsigned = _receipt_unsigned(receipt)
    if receipt.checksum != _digest(unsigned):
        raise AccountRuntimeNonceLedgerError("nonce receipt checksum mismatch")
    return json.dumps(
        {**unsigned, "checksum": receipt.checksum},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def _decode_json(payload: bytes, *, maximum: int, label: str) -> dict[str, object]:
    if len(payload) > maximum:
        raise AccountRuntimeNonceLedgerError(f"nonce ledger {label} exceeds size limit")
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AccountRuntimeNonceLedgerError(f"nonce ledger {label} is invalid JSON") from exc
    if type(raw) is not dict:
        raise AccountRuntimeNonceLedgerError(f"nonce ledger {label} is not an object")
    return raw


def _decode_receipt(payload: bytes) -> AccountRuntimeNonceReceipt:
    raw = _decode_json(payload, maximum=_MAX_RECEIPT_BYTES, label="receipt")
    fields = {
        "kind",
        "schema_version",
        "operation_nonce",
        "operation_kind",
        "account_identity_digest",
        "canonical_runtime",
        "runtime_identity_digest",
        "account_epoch",
        "semantic_request_digest",
        "checksum",
    }
    if set(raw) != fields or raw["kind"] != _KIND_RECEIPT or raw["schema_version"] != _SCHEMA:
        raise AccountRuntimeNonceLedgerError("nonce ledger receipt schema is invalid")
    if (
        type(raw["operation_kind"]) is not str
        or not raw["operation_kind"]
        or type(raw["canonical_runtime"]) is not str
        or not raw["canonical_runtime"]
    ):
        raise AccountRuntimeNonceLedgerError("nonce ledger receipt identity is invalid")
    receipt = AccountRuntimeNonceReceipt(
        operation_nonce=_sha(raw["operation_nonce"], "operation nonce"),
        operation_kind=raw["operation_kind"],
        account_identity_digest=_sha(raw["account_identity_digest"], "account identity"),
        canonical_runtime=raw["canonical_runtime"],
        runtime_identity_digest=_sha(raw["runtime_identity_digest"], "runtime identity"),
        account_epoch=_sha(raw["account_epoch"], "account epoch"),
        semantic_request_digest=_sha(raw["semantic_request_digest"], "semantic request digest"),
        checksum=_sha(raw["checksum"], "receipt checksum"),
    )
    if receipt.checksum != _digest(_receipt_unsigned(receipt)):
        raise AccountRuntimeNonceLedgerError("nonce ledger receipt checksum mismatch")
    return receipt


def _bit(key: str, index: int) -> int:
    return (int(key[index // 4], 16) >> (3 - (index % 4))) & 1


def _first_different_bit(left: str, right: str) -> int:
    xor = int(left, 16) ^ int(right, 16)
    if xor == 0:
        raise AccountRuntimeNonceLedgerError("duplicate nonce leaf cannot be split")
    return 256 - xor.bit_length()


def _node_hash(unsigned: dict[str, object]) -> str:
    return _digest(unsigned)


class AccountRuntimeNonceLedger:
    """Immutable content-addressed Patricia-Merkle nonce dictionary."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.receipts = directory / "receipts"
        self.nodes = directory / "nodes"
        self.transitions = directory / "transitions"
        self.ready_path = directory / "ready.json"
        self.pending_path = directory / "pending.json"

    @classmethod
    def for_registry(cls, registry_path: str | Path) -> AccountRuntimeNonceLedger:
        path = Path(registry_path)
        return cls(path.with_name(path.name + ".nonce-ledger"))

    def receipt_path(self, nonce: str) -> Path:
        return self.receipts / f"{_sha(nonce, 'operation nonce')}.json"

    def _node_path(self, node_hash: str) -> Path:
        return self.nodes / f"{_sha(node_hash, 'nonce node hash')}.json"

    @staticmethod
    def _fsync_parent(path: Path) -> None:
        descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _read_exact(path: Path, *, maximum: int, label: str) -> bytes:
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError as exc:
            raise AccountRuntimeNonceLedgerError(f"nonce ledger {label} is missing") from exc
        try:
            opened = os.fstat(descriptor)
            visible = os.lstat(path)
            if (
                not stat.S_ISREG(opened.st_mode)
                or visible.st_dev != opened.st_dev
                or visible.st_ino != opened.st_ino
                or opened.st_size > maximum
            ):
                raise AccountRuntimeNonceLedgerError(f"nonce ledger {label} path is invalid")
            chunks: list[bytes] = []
            remaining = maximum + 1
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > maximum:
                raise AccountRuntimeNonceLedgerError(f"nonce ledger {label} exceeds size limit")
            return payload
        finally:
            os.close(descriptor)

    def _durable_create_exact(self, path: Path, payload: bytes, *, maximum: int) -> None:
        if len(payload) > maximum:
            raise AccountRuntimeNonceLedgerError("nonce ledger immutable object exceeds size limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor: int | None = None
        created = False
        try:
            descriptor = os.open(
                path,
                os.O_CREAT
                | os.O_EXCL
                | os.O_WRONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            created = True
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError(errno.EIO, "short immutable nonce ledger write")
                offset += written
            os.fsync(descriptor)
            opened = os.fstat(descriptor)
            visible = os.lstat(path)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != visible.st_dev
                or opened.st_ino != visible.st_ino
            ):
                raise OSError(errno.ESTALE, "nonce ledger immutable path changed")
            os.close(descriptor)
            descriptor = None
            self._fsync_parent(path)
        except FileExistsError as exc:
            if self._read_exact(path, maximum=maximum, label="immutable object") != payload:
                raise AccountRuntimeNonceLedgerError(
                    "nonce ledger immutable object conflicts with existing evidence"
                ) from exc
        except OSError as exc:
            # Ambiguous durability is deliberately retained and exact-verified on retry.
            raise AccountRuntimeNonceLedgerError("nonce ledger immutable create failed") from exc
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    if not created:
                        raise

    def transition_path(self, nonce_count: int) -> Path:
        if type(nonce_count) is not int or nonce_count <= 0:
            raise AccountRuntimeNonceLedgerError("nonce transition count is invalid")
        return self.transitions / f"{nonce_count:016d}.json"

    def _reject_reserved_artifact(self) -> None:
        artifact = self.directory / "migration.json"
        try:
            exists = artifact.exists() or artifact.is_symlink()
        except OSError as exc:
            raise AccountRuntimeNonceLedgerError(
                "nonce ledger directory contains an invalid reserved artifact"
            ) from exc
        if exists:
            raise AccountRuntimeNonceLedgerError(
                "nonce ledger directory contains an unsupported reserved artifact"
            )

    def initialize_ready(
        self,
        *,
        source_digest: str,
        base_registry_sequence: int | None = None,
        base_registry_checksum: str | None = None,
    ) -> None:
        self.write_ready(
            source_digest=source_digest,
            initial_root=EMPTY_NONCE_ROOT,
            initial_count=0,
            base_registry_sequence=base_registry_sequence,
            base_registry_checksum=base_registry_checksum,
        )

    def write_ready(
        self,
        *,
        source_digest: str,
        initial_root: str,
        initial_count: int,
        base_registry_sequence: int | None = None,
        base_registry_checksum: str | None = None,
    ) -> None:
        self._reject_reserved_artifact()
        _sha(source_digest, "nonce ledger source digest")
        _sha(initial_root, "nonce ledger initial root")
        if type(initial_count) is not int or initial_count < 0:
            raise AccountRuntimeNonceLedgerError("nonce ledger initial count is invalid")
        if (base_registry_sequence is None) != (base_registry_checksum is None):
            raise AccountRuntimeNonceLedgerError("nonce ledger base registry anchor is invalid")
        if base_registry_sequence is not None and (
            type(base_registry_sequence) is not int or base_registry_sequence <= 0
        ):
            raise AccountRuntimeNonceLedgerError("nonce ledger base registry sequence is invalid")
        if base_registry_checksum is not None:
            _sha(base_registry_checksum, "nonce ledger base registry checksum")
        self.receipts.mkdir(parents=True, exist_ok=True)
        self.nodes.mkdir(parents=True, exist_ok=True)
        self.transitions.mkdir(parents=True, exist_ok=True)
        unsigned = {
            "kind": _KIND_READY,
            "schema_version": _SCHEMA,
            "source_digest": source_digest,
            "initial_root": initial_root,
            "initial_count": initial_count,
            "base_registry_sequence": base_registry_sequence,
            "base_registry_checksum": base_registry_checksum,
        }
        payload = _canonical({**unsigned, "checksum": _digest(unsigned)})
        self._durable_create_exact(self.ready_path, payload, maximum=_MAX_RECEIPT_BYTES)

    def load_ready_anchor(self) -> dict[str, object]:
        self._reject_reserved_artifact()
        raw = _decode_json(
            self._read_exact(self.ready_path, maximum=_MAX_RECEIPT_BYTES, label="ready marker"),
            maximum=_MAX_RECEIPT_BYTES,
            label="ready marker",
        )
        fields = {
            "kind",
            "schema_version",
            "source_digest",
            "initial_root",
            "initial_count",
            "base_registry_sequence",
            "base_registry_checksum",
            "checksum",
        }
        if set(raw) != fields or raw["kind"] != _KIND_READY or raw["schema_version"] != _SCHEMA:
            raise AccountRuntimeNonceLedgerError("nonce ledger ready marker is invalid")
        unsigned = {key: value for key, value in raw.items() if key != "checksum"}
        if _sha(raw["checksum"], "ready checksum") != _digest(unsigned):
            raise AccountRuntimeNonceLedgerError("nonce ledger ready checksum mismatch")
        root = _sha(raw["initial_root"], "ready root")
        count = raw["initial_count"]
        if type(count) is not int or count < 0:
            raise AccountRuntimeNonceLedgerError("nonce ledger ready count is invalid")
        base_sequence = raw["base_registry_sequence"]
        base_checksum = raw["base_registry_checksum"]
        if (base_sequence is None) != (base_checksum is None):
            raise AccountRuntimeNonceLedgerError("nonce ledger base registry anchor is invalid")
        if base_sequence is not None and (type(base_sequence) is not int or base_sequence <= 0):
            raise AccountRuntimeNonceLedgerError("nonce ledger base registry sequence is invalid")
        if base_checksum is not None:
            _sha(base_checksum, "nonce ledger base registry checksum")
        return {
            "initial_root": root,
            "initial_count": count,
            "base_registry_sequence": base_sequence,
            "base_registry_checksum": base_checksum,
            "checksum": raw["checksum"],
        }

    def load_ready_root(self) -> tuple[str, int]:
        anchor = self.load_ready_anchor()
        root = anchor["initial_root"]
        count = anchor["initial_count"]
        if type(root) is not str or type(count) is not int:
            raise AccountRuntimeNonceLedgerError("nonce ledger ready anchor types are invalid")
        return root, count

    def _validate_transition_unsigned(self, unsigned: dict[str, object]) -> None:
        expected = {
            "kind",
            "schema_version",
            "parent_transition_checksum",
            "old_registry_sequence",
            "old_registry_checksum",
            "old_root",
            "old_count",
            "new_registry_sequence",
            "new_registry_checksum",
            "new_root",
            "new_count",
            "operation_nonce",
            "receipt_checksum",
        }
        if set(unsigned) != expected or unsigned["kind"] != _KIND_TRANSITION:
            raise AccountRuntimeNonceLedgerError("nonce transition schema is invalid")
        if unsigned["schema_version"] != _SCHEMA:
            raise AccountRuntimeNonceLedgerError("nonce transition version is invalid")
        old_count = unsigned["old_count"]
        new_count = unsigned["new_count"]
        old_sequence = unsigned["old_registry_sequence"]
        new_sequence = unsigned["new_registry_sequence"]
        if (
            type(old_count) is not int
            or type(new_count) is not int
            or new_count != old_count + 1
            or type(old_sequence) is not int
            or type(new_sequence) is not int
            or new_sequence != old_sequence + 1
        ):
            raise AccountRuntimeNonceLedgerError("nonce transition progression is invalid")
        for key in (
            "parent_transition_checksum",
            "old_registry_checksum",
            "old_root",
            "new_registry_checksum",
            "new_root",
            "operation_nonce",
            "receipt_checksum",
        ):
            _sha(unsigned[key], f"nonce transition {key}")

    def _load_transition_raw(self, nonce_count: int) -> dict[str, object] | None:
        path = self.transition_path(nonce_count)
        if not path.exists():
            return None
        raw = _decode_json(
            self._read_exact(path, maximum=_MAX_RECEIPT_BYTES, label="transition"),
            maximum=_MAX_RECEIPT_BYTES,
            label="transition",
        )
        checksum = raw.pop("checksum", None)
        if checksum != _digest(raw):
            raise AccountRuntimeNonceLedgerError("nonce transition checksum mismatch")
        self._validate_transition_unsigned(raw)
        return {**raw, "checksum": checksum}

    def _canonical_insertion_root(
        self,
        old_root: str,
        receipt: AccountRuntimeNonceReceipt,
    ) -> str:
        proof, old_leaf = self._proof(old_root, receipt.operation_nonce)
        if old_leaf is not None and old_leaf["operation_nonce"] == receipt.operation_nonce:
            raise AccountRuntimeNonceLedgerError(
                "nonce transition old root already contains declared receipt"
            )
        new_leaf = _node_hash(
            {
                "kind": _KIND_NODE,
                "schema_version": _SCHEMA,
                "node_type": "leaf",
                "operation_nonce": receipt.operation_nonce,
                "receipt_checksum": receipt.checksum,
            }
        )
        if old_root == EMPTY_NONCE_ROOT:
            return new_leaf
        assert old_leaf is not None
        old_key = str(old_leaf["operation_nonce"])
        different = _first_different_bit(old_key, receipt.operation_nonce)
        prefix: list[tuple[dict[str, object], int]] = []
        subtree = old_root
        for _, node in proof:
            if node["node_type"] != "branch":
                break
            bit_value = node["bit_index"]
            if type(bit_value) is not int:
                raise AccountRuntimeNonceLedgerError("nonce ledger branch bit is invalid")
            if bit_value >= different:
                break
            direction = _bit(receipt.operation_nonce, bit_value)
            prefix.append((node, direction))
            subtree = str(node["right"] if direction else node["left"])

        def branch_hash(bit_index: int, left: str, right: str) -> str:
            return _node_hash(
                {
                    "kind": _KIND_NODE,
                    "schema_version": _SCHEMA,
                    "node_type": "branch",
                    "bit_index": bit_index,
                    "left": left,
                    "right": right,
                }
            )

        rebuilt = (
            branch_hash(different, subtree, new_leaf)
            if _bit(receipt.operation_nonce, different)
            else branch_hash(different, new_leaf, subtree)
        )
        for node, direction in reversed(prefix):
            bit_index = node["bit_index"]
            assert type(bit_index) is int
            rebuilt = (
                branch_hash(bit_index, str(node["left"]), rebuilt)
                if direction
                else branch_hash(bit_index, rebuilt, str(node["right"]))
            )
        return rebuilt

    def _validate_transition_proof(self, transition: dict[str, object]) -> None:
        new_count = transition["new_count"]
        if type(new_count) is not int:
            raise AccountRuntimeNonceLedgerError("nonce transition count is invalid")
        anchor = self.load_ready_anchor()
        base_count = anchor["initial_count"]
        if type(base_count) is not int:
            raise AccountRuntimeNonceLedgerError("nonce ready count is invalid")
        if new_count == base_count + 1:
            if (
                transition["parent_transition_checksum"] != anchor["checksum"]
                or transition["old_registry_sequence"] != anchor["base_registry_sequence"]
                or transition["old_registry_checksum"] != anchor["base_registry_checksum"]
                or transition["old_root"] != anchor["initial_root"]
                or transition["old_count"] != base_count
            ):
                raise AccountRuntimeNonceLedgerError(
                    "first nonce transition does not extend ready anchor"
                )
        elif new_count > base_count + 1:
            previous = self._load_transition_raw(new_count - 1)
            if previous is None or (
                transition["parent_transition_checksum"] != previous["checksum"]
                or transition["old_registry_sequence"] != previous["new_registry_sequence"]
                or transition["old_registry_checksum"] != previous["new_registry_checksum"]
                or transition["old_root"] != previous["new_root"]
                or transition["old_count"] != previous["new_count"]
            ):
                raise AccountRuntimeNonceLedgerError("nonce transition chain continuity is invalid")
        else:
            raise AccountRuntimeNonceLedgerError("nonce transition precedes ready anchor")
        nonce = transition["operation_nonce"]
        if type(nonce) is not str:
            raise AccountRuntimeNonceLedgerError("nonce transition operation is invalid")
        receipt = self._load_receipt(nonce)
        if receipt.checksum != transition["receipt_checksum"]:
            raise AccountRuntimeNonceLedgerError("nonce transition receipt checksum mismatch")
        computed = self._canonical_insertion_root(str(transition["old_root"]), receipt)
        if computed != transition["new_root"]:
            raise AccountRuntimeNonceLedgerError(
                "nonce transition root is not the canonical receipt insertion"
            )

    def create_transition(self, unsigned: dict[str, object]) -> dict[str, object]:
        self._reject_reserved_artifact()
        self._validate_transition_unsigned(unsigned)
        self._validate_transition_proof(unsigned)
        transition = {**unsigned, "checksum": _digest(unsigned)}
        new_count = unsigned["new_count"]
        if type(new_count) is not int:
            raise AccountRuntimeNonceLedgerError("nonce transition count is invalid")
        self._durable_create_exact(
            self.transition_path(new_count),
            _canonical(transition),
            maximum=_MAX_RECEIPT_BYTES,
        )
        return transition

    def load_transition(self, nonce_count: int) -> dict[str, object] | None:
        self._reject_reserved_artifact()
        transition = self._load_transition_raw(nonce_count)
        if transition is None:
            return None
        unsigned = {key: value for key, value in transition.items() if key != "checksum"}
        self._validate_transition_proof(unsigned)
        return transition

    def _create_receipt(self, receipt: AccountRuntimeNonceReceipt) -> None:
        self._durable_create_exact(
            self.receipt_path(receipt.operation_nonce),
            _receipt_bytes(receipt),
            maximum=_MAX_RECEIPT_BYTES,
        )

    def _load_receipt(self, nonce: str) -> AccountRuntimeNonceReceipt:
        receipt = _decode_receipt(
            self._read_exact(self.receipt_path(nonce), maximum=_MAX_RECEIPT_BYTES, label="receipt")
        )
        if receipt.operation_nonce != nonce:
            raise AccountRuntimeNonceLedgerError("nonce ledger receipt key mismatch")
        return receipt

    def _create_node(self, unsigned: dict[str, object]) -> str:
        node_hash = _node_hash(unsigned)
        payload = _canonical({**unsigned, "node_hash": node_hash})
        self._durable_create_exact(self._node_path(node_hash), payload, maximum=_MAX_NODE_BYTES)
        return node_hash

    def _leaf(self, receipt: AccountRuntimeNonceReceipt) -> str:
        return self._create_node(
            {
                "kind": _KIND_NODE,
                "schema_version": _SCHEMA,
                "node_type": "leaf",
                "operation_nonce": receipt.operation_nonce,
                "receipt_checksum": receipt.checksum,
            }
        )

    def _branch(self, bit_index: int, left: str, right: str) -> str:
        if type(bit_index) is not int or not 0 <= bit_index < 256 or left == right:
            raise AccountRuntimeNonceLedgerError("nonce ledger branch is non-canonical")
        return self._create_node(
            {
                "kind": _KIND_NODE,
                "schema_version": _SCHEMA,
                "node_type": "branch",
                "bit_index": bit_index,
                "left": _sha(left, "left node hash"),
                "right": _sha(right, "right node hash"),
            }
        )

    def _load_node(self, node_hash: str) -> dict[str, object]:
        if node_hash == EMPTY_NONCE_ROOT:
            raise AccountRuntimeNonceLedgerError("empty nonce root has no node")
        raw = _decode_json(
            self._read_exact(
                self._node_path(node_hash), maximum=_MAX_NODE_BYTES, label="path node"
            ),
            maximum=_MAX_NODE_BYTES,
            label="path node",
        )
        claimed = raw.pop("node_hash", None)
        if (
            claimed != node_hash
            or _node_hash(raw) != node_hash
            or raw.get("kind") != _KIND_NODE
            or raw.get("schema_version") != _SCHEMA
        ):
            raise AccountRuntimeNonceLedgerError("nonce ledger path node hash mismatch")
        kind = raw.get("node_type")
        if kind == "leaf":
            if set(raw) != {
                "kind",
                "schema_version",
                "node_type",
                "operation_nonce",
                "receipt_checksum",
            }:
                raise AccountRuntimeNonceLedgerError("nonce ledger leaf schema is invalid")
            _sha(raw["operation_nonce"], "leaf nonce")
            _sha(raw["receipt_checksum"], "leaf receipt checksum")
        elif kind == "branch":
            if set(raw) != {"kind", "schema_version", "node_type", "bit_index", "left", "right"}:
                raise AccountRuntimeNonceLedgerError("nonce ledger branch schema is invalid")
            bit_index = raw["bit_index"]
            if type(bit_index) is not int or not 0 <= bit_index < 256:
                raise AccountRuntimeNonceLedgerError("nonce ledger branch bit is invalid")
            if _sha(raw["left"], "left node hash") == _sha(raw["right"], "right node hash"):
                raise AccountRuntimeNonceLedgerError("nonce ledger branch children are ambiguous")
        else:
            raise AccountRuntimeNonceLedgerError("nonce ledger node type is invalid")
        return raw

    def _proof(
        self, root: str, nonce: str
    ) -> tuple[list[tuple[str, dict[str, object]]], dict[str, object] | None]:
        _sha(root, "nonce root")
        nonce = _sha(nonce, "operation nonce")
        if root == EMPTY_NONCE_ROOT:
            return [], None
        path: list[tuple[str, dict[str, object]]] = []
        current = root
        previous_bit = -1
        for _ in range(257):
            node = self._load_node(current)
            path.append((current, node))
            if node["node_type"] == "leaf":
                return path, node
            bit_index = node["bit_index"]
            assert type(bit_index) is int
            if bit_index <= previous_bit:
                raise AccountRuntimeNonceLedgerError(
                    "nonce ledger proof branch order is non-canonical"
                )
            previous_bit = bit_index
            current = str(node["right"] if _bit(nonce, bit_index) else node["left"])
        raise AccountRuntimeNonceLedgerError("nonce ledger proof exceeds 256 bits")

    def lookup(self, root: str, nonce: str) -> AccountRuntimeNonceReceipt | None:
        self._reject_reserved_artifact()
        path, leaf = self._proof(root, nonce)
        del path
        if leaf is None or leaf["operation_nonce"] != nonce:
            return None
        receipt = self._load_receipt(nonce)
        if receipt.checksum != leaf["receipt_checksum"]:
            raise AccountRuntimeNonceLedgerError("nonce ledger receipt is not anchored by root")
        return receipt

    def require_receipt(self, root: str, nonce: str) -> AccountRuntimeNonceReceipt:
        self._reject_reserved_artifact()
        receipt = self.lookup(root, nonce)
        if receipt is None:
            raise AccountRuntimeNonceLedgerError("nonce is not a member of the anchored ledger")
        return receipt

    def membership_node_paths(self, root: str, nonce: str) -> tuple[Path, ...]:
        self._reject_reserved_artifact()
        path, leaf = self._proof(root, nonce)
        if leaf is None or leaf["operation_nonce"] != nonce:
            raise AccountRuntimeNonceLedgerError("nonce membership proof is absent")
        return tuple(self._node_path(node_hash) for node_hash, _ in path)

    def insert(
        self,
        *,
        old_root: str,
        old_count: int,
        receipt: AccountRuntimeNonceReceipt,
        fault_after_receipt: bool = False,
    ) -> AccountRuntimeNonceInsertion:
        self._reject_reserved_artifact()
        if type(old_count) is not int or old_count < 0:
            raise AccountRuntimeNonceLedgerError("nonce ledger count is invalid")
        if old_count >= MAX_NONCE_RECEIPTS:
            raise AccountRuntimeNonceLedgerError("nonce ledger capacity cap is exhausted")
        anchored = self.lookup(old_root, receipt.operation_nonce)
        if anchored is not None:
            if anchored != receipt:
                raise AccountRuntimeNonceLedgerError(
                    "nonce was already consumed for a different request"
                )
            return AccountRuntimeNonceInsertion(old_root, old_root, old_count, old_count, receipt)
        self._create_receipt(receipt)
        if self._load_receipt(receipt.operation_nonce) != receipt:
            raise AccountRuntimeNonceLedgerError("nonce receipt revalidation failed")
        if fault_after_receipt:
            raise AccountRuntimeNonceLedgerError(
                "injected crash after nonce receipt before registry"
            )
        new_leaf = self._leaf(receipt)
        if old_root == EMPTY_NONCE_ROOT:
            new_root = new_leaf
        else:
            proof, old_leaf = self._proof(old_root, receipt.operation_nonce)
            assert old_leaf is not None
            old_key = str(old_leaf["operation_nonce"])
            different = _first_different_bit(old_key, receipt.operation_nonce)
            prefix: list[tuple[dict[str, object], int]] = []
            subtree = old_root
            for _, node in proof:
                if node["node_type"] != "branch":
                    break
                bit_value = node["bit_index"]
                if type(bit_value) is not int:
                    raise AccountRuntimeNonceLedgerError("nonce ledger branch bit is invalid")
                bit_index = bit_value
                if bit_index >= different:
                    break
                direction = _bit(receipt.operation_nonce, bit_index)
                prefix.append((node, direction))
                subtree = str(node["right"] if direction else node["left"])
            if _bit(receipt.operation_nonce, different):
                rebuilt = self._branch(different, subtree, new_leaf)
            else:
                rebuilt = self._branch(different, new_leaf, subtree)
            for node, direction in reversed(prefix):
                bit_value = node["bit_index"]
                if type(bit_value) is not int:
                    raise AccountRuntimeNonceLedgerError("nonce ledger branch bit is invalid")
                if direction:
                    rebuilt = self._branch(bit_value, str(node["left"]), rebuilt)
                else:
                    rebuilt = self._branch(bit_value, rebuilt, str(node["right"]))
            new_root = rebuilt
        if self.require_receipt(new_root, receipt.operation_nonce) != receipt:
            raise AccountRuntimeNonceLedgerError("new nonce root proof is incomplete")
        return AccountRuntimeNonceInsertion(
            old_root=old_root,
            new_root=new_root,
            old_count=old_count,
            new_count=old_count + 1,
            receipt=receipt,
        )

    def largest_receipt_size(self) -> int:
        self._reject_reserved_artifact()
        return max((path.stat().st_size for path in self.receipts.glob("*.json")), default=0)

    def largest_node_size(self) -> int:
        self._reject_reserved_artifact()
        return max((path.stat().st_size for path in self.nodes.glob("*.json")), default=0)
