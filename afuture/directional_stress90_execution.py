"""Durable mechanical transition intent for reduction-first Stress-90 execution."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import MappingProxyType

from .directional_stress90_policy import STRESS90_POLICY

_KIND = "afuture.directional.stress90.execution-intent"
_SCHEMA_VERSION = 1
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
class Stress90ExecutionIntent:
    policy_definition_digest: str
    products_manifest_digest: str
    target_trading_day: str
    daily_decision_digest: str
    initial_current_lots: Mapping[str, int]
    initial_margin_fitted_lots: Mapping[str, int]
    authorized_transition_products: tuple[str, ...]
    source_digest: str


@dataclass(frozen=True)
class Stress90ExecutionIntentRecord:
    intent: Stress90ExecutionIntent
    sequence: int
    checksum: str


def _payload(intent: Stress90ExecutionIntent) -> dict[str, object]:
    return {
        "policy_definition_digest": intent.policy_definition_digest,
        "products_manifest_digest": intent.products_manifest_digest,
        "target_trading_day": intent.target_trading_day,
        "daily_decision_digest": intent.daily_decision_digest,
        "initial_current_lots": dict(intent.initial_current_lots),
        "initial_margin_fitted_lots": dict(intent.initial_margin_fitted_lots),
        "authorized_transition_products": list(intent.authorized_transition_products),
        "source_digest": intent.source_digest,
    }


def _intent(raw: object) -> Stress90ExecutionIntent:
    fields = {
        "policy_definition_digest",
        "products_manifest_digest",
        "target_trading_day",
        "daily_decision_digest",
        "initial_current_lots",
        "initial_margin_fitted_lots",
        "authorized_transition_products",
        "source_digest",
    }
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise Stress90ExecutionIntentIntegrityError("execution intent fields are invalid")
    transitions = raw["authorized_transition_products"]
    if not isinstance(transitions, list) or any(
        not isinstance(product, str) for product in transitions
    ):
        raise Stress90ExecutionIntentIntegrityError("execution transition products are invalid")
    normalized = tuple(sorted(set(transitions)))
    if tuple(transitions) != normalized or not set(normalized).issubset(STRESS90_POLICY.products):
        raise Stress90ExecutionIntentIntegrityError("execution transition products are invalid")
    result = Stress90ExecutionIntent(
        policy_definition_digest=_sha(
            raw["policy_definition_digest"], name="execution policy digest"
        ),
        products_manifest_digest=_sha(
            raw["products_manifest_digest"], name="execution products digest"
        ),
        target_trading_day=_day(raw["target_trading_day"]),
        daily_decision_digest=_sha(raw["daily_decision_digest"], name="execution decision digest"),
        initial_current_lots=_lots(raw["initial_current_lots"], name="initial current lots"),
        initial_margin_fitted_lots=_lots(
            raw["initial_margin_fitted_lots"], name="initial margin-fitted lots"
        ),
        authorized_transition_products=normalized,
        source_digest=_sha(raw["source_digest"], name="execution source digest"),
    )
    if result.policy_definition_digest != STRESS90_POLICY.policy_definition_digest:
        raise Stress90ExecutionIntentIntegrityError("execution policy definition mismatch")
    if result.products_manifest_digest != STRESS90_POLICY.products_manifest_digest:
        raise Stress90ExecutionIntentIntegrityError("execution products manifest mismatch")
    unsigned = {
        "current_lots": dict(result.initial_current_lots),
        "decision_digest": result.daily_decision_digest,
        "margin_fitted_lots": dict(result.initial_margin_fitted_lots),
        "target_trading_day": result.target_trading_day,
        "transition_products": result.authorized_transition_products,
    }
    if result.source_digest != _digest(unsigned):
        raise Stress90ExecutionIntentIntegrityError("execution intent source digest mismatch")
    return result


class Stress90ExecutionIntentStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @property
    def previous_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.prev")

    def load_record(self) -> Stress90ExecutionIntentRecord | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(
                self.path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except json.JSONDecodeError as exc:
            raise Stress90ExecutionIntentIntegrityError("invalid execution intent JSON") from exc
        fields = {"kind", "schema_version", "sequence", "intent", "checksum"}
        if not isinstance(raw, Mapping) or set(raw) != fields:
            raise Stress90ExecutionIntentIntegrityError("execution intent envelope is invalid")
        if raw["kind"] != _KIND or raw["schema_version"] != _SCHEMA_VERSION:
            raise Stress90ExecutionIntentIntegrityError("execution intent schema is invalid")
        sequence = raw["sequence"]
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise Stress90ExecutionIntentIntegrityError("execution intent sequence is invalid")
        checksum = _sha(raw["checksum"], name="execution intent checksum")
        unsigned = {key: value for key, value in raw.items() if key != "checksum"}
        if checksum != _digest(unsigned):
            raise Stress90ExecutionIntentIntegrityError("execution intent checksum mismatch")
        return Stress90ExecutionIntentRecord(_intent(raw["intent"]), sequence, checksum)

    def load_required_record(self) -> Stress90ExecutionIntentRecord:
        record = self.load_record()
        if record is None:
            raise Stress90ExecutionIntentIntegrityError("required execution intent is missing")
        return record

    def save(self, intent: Stress90ExecutionIntent) -> Stress90ExecutionIntentRecord:
        validated = _intent(_payload(intent))
        current = self.load_record()
        sequence = 1 if current is None else current.sequence + 1
        unsigned: dict[str, object] = {
            "kind": _KIND,
            "schema_version": _SCHEMA_VERSION,
            "sequence": sequence,
            "intent": _payload(validated),
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
        return Stress90ExecutionIntentRecord(validated, sequence, checksum)

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
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()


def _transition_products(
    current_lots: Mapping[str, int],
    target_lots: Mapping[str, int],
    symbol_products: Mapping[str, str],
) -> tuple[str, ...]:
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
    result: list[str] = []
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
        if current_signs != target_signs or set(current_rows) != set(target_rows):
            result.append(product)
    return tuple(result)


def prepare_stress90_execution_intent(
    store: Stress90ExecutionIntentStore,
    *,
    target_trading_day: str,
    daily_decision_digest: str,
    current_lots: Mapping[str, int],
    margin_fitted_lots: Mapping[str, int],
    symbol_products: Mapping[str, str],
) -> Stress90ExecutionIntent:
    target = _day(target_trading_day)
    decision = _sha(daily_decision_digest, name="execution decision digest")
    current = _lots(current_lots, name="current lots")
    fitted = _lots(margin_fitted_lots, name="margin-fitted lots")
    existing = store.load_record()
    if existing is not None:
        intent = existing.intent
        if intent.target_trading_day == target:
            if intent.daily_decision_digest != decision:
                raise Stress90ExecutionIntentIntegrityError(
                    "same-day execution decision identity changed"
                )
            return intent
        if target < intent.target_trading_day:
            raise Stress90ExecutionIntentIntegrityError("execution intent day moved backward")
    transitions = _transition_products(current, fitted, symbol_products)
    source = {
        "current_lots": dict(current),
        "decision_digest": decision,
        "margin_fitted_lots": dict(fitted),
        "target_trading_day": target,
        "transition_products": transitions,
    }
    intent = Stress90ExecutionIntent(
        policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
        products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
        target_trading_day=target,
        daily_decision_digest=decision,
        initial_current_lots=current,
        initial_margin_fitted_lots=fitted,
        authorized_transition_products=transitions,
        source_digest=_digest(source),
    )
    return store.save(intent).intent
