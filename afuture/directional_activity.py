"""Completed-trading-day activity evidence for directional contract selection.

This sidecar owns only market-selection evidence. Account, order, fill and position truth
remain exclusively in Broker/TradingEngine state.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime
from hashlib import sha256
from math import isfinite
from pathlib import Path
from tempfile import NamedTemporaryFile

from .directional import DirectionalConfig
from .durable_file_creation import (
    DurableFileCreationToken,
    create_durable_file_exclusive,
    durable_file_lock,
)
from .models import ContractInfo, Tick

ACTIVITY_SCHEMA_VERSION = 1


class DirectionalActivityIntegrityError(ValueError):
    """Persisted market-selection evidence is malformed or cannot be trusted."""


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DirectionalActivityIntegrityError(
                f"duplicate directional activity JSON key: {key}"
            )
        result[key] = value
    return result


@dataclass(frozen=True)
class ContractActivity:
    symbol: str
    exchange: str
    product: str
    trading_day: str
    volume: float
    open_interest: float
    timestamp: datetime


@dataclass(frozen=True)
class DirectionalActivitySnapshot:
    trading_day: str
    contracts: dict[str, ContractActivity]

    @property
    def trading_date(self) -> date:
        return datetime.strptime(self.trading_day, "%Y%m%d").date()


@dataclass(frozen=True)
class DirectionalActivityState:
    """Completed and in-progress market observations; never account or position truth."""

    completed: DirectionalActivitySnapshot | None = None
    in_progress: DirectionalActivitySnapshot | None = None


def _validate_trading_day(value: object) -> str:
    if not isinstance(value, str):
        raise DirectionalActivityIntegrityError("directional activity trading day must be string")
    try:
        parsed = datetime.strptime(value, "%Y%m%d").strftime("%Y%m%d")
    except ValueError as exc:
        raise DirectionalActivityIntegrityError("invalid directional activity trading day") from exc
    if parsed != value:
        raise DirectionalActivityIntegrityError("invalid directional activity trading day")
    return value


def validate_directional_activity_snapshot(snapshot: DirectionalActivitySnapshot) -> None:
    """Validate the complete internal identity and numeric integrity of one snapshot."""
    if not isinstance(snapshot, DirectionalActivitySnapshot):
        raise DirectionalActivityIntegrityError("directional activity snapshot has invalid type")
    trading_day = _validate_trading_day(snapshot.trading_day)
    if not isinstance(snapshot.contracts, dict) or not snapshot.contracts:
        raise DirectionalActivityIntegrityError(
            "directional activity contracts must be a non-empty object"
        )
    for symbol, item in snapshot.contracts.items():
        if not isinstance(item, ContractActivity):
            raise DirectionalActivityIntegrityError(
                "directional activity contracts must contain activity objects"
            )
        if not isinstance(symbol, str) or not symbol or symbol != item.symbol:
            raise DirectionalActivityIntegrityError(
                "directional activity contract symbol identity mismatch"
            )
        if any(
            not isinstance(value, str) or not value
            for value in (item.symbol, item.exchange, item.product, item.trading_day)
        ):
            raise DirectionalActivityIntegrityError(
                f"directional activity identity fields must be non-empty strings: {symbol}"
            )
        if item.trading_day != trading_day:
            raise DirectionalActivityIntegrityError(
                f"directional activity contract trading day mismatch: {symbol}"
            )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            or value < 0
            for value in (item.volume, item.open_interest)
        ):
            raise DirectionalActivityIntegrityError(
                f"directional activity values must be finite and non-negative: {symbol}"
            )
        if not isinstance(item.timestamp, datetime) or item.timestamp.tzinfo is None:
            raise DirectionalActivityIntegrityError(
                f"directional activity timestamp must be timezone-aware: {symbol}"
            )


def _validate_state(state: DirectionalActivityState) -> None:
    for snapshot in (state.completed, state.in_progress):
        if snapshot is not None:
            validate_directional_activity_snapshot(snapshot)
    if (
        state.completed is not None
        and state.in_progress is not None
        and state.completed.trading_date >= state.in_progress.trading_date
    ):
        raise DirectionalActivityIntegrityError(
            "completed directional activity must precede in-progress activity"
        )


class DirectionalActivityStore:
    """Atomically persist verified completed and in-progress activity evidence."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._last_creation_token: DurableFileCreationToken | None = None

    @property
    def last_creation_token(self) -> DurableFileCreationToken | None:
        return self._last_creation_token

    def load(self) -> DirectionalActivitySnapshot | None:
        """Load only completed evidence for callers that do not track observations."""
        return self.load_state().completed

    def load_state(self) -> DirectionalActivityState:
        if not self.path.exists():
            return DirectionalActivityState()
        try:
            text = self.path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DirectionalActivityIntegrityError("invalid directional activity UTF-8") from exc
        try:
            raw = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
        except json.JSONDecodeError as exc:
            raise DirectionalActivityIntegrityError("invalid directional activity JSON") from exc
        if not isinstance(raw, dict):
            raise DirectionalActivityIntegrityError(
                "directional activity envelope must be a JSON object"
            )
        required = {"schema_version", "completed", "in_progress", "checksum"}
        if set(raw) != required:
            raise DirectionalActivityIntegrityError(
                "directional activity envelope fields are invalid"
            )
        schema_version = raw["schema_version"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise DirectionalActivityIntegrityError(
                "directional activity schema version must be integer"
            )
        if schema_version != ACTIVITY_SCHEMA_VERSION:
            raise DirectionalActivityIntegrityError(
                "directional activity schema version is unsupported"
            )
        unsigned = {key: value for key, value in raw.items() if key != "checksum"}
        if raw["checksum"] != self._checksum(unsigned):
            raise DirectionalActivityIntegrityError("directional activity checksum mismatch")
        state = DirectionalActivityState(
            completed=self._snapshot_from_payload(raw["completed"]),
            in_progress=self._snapshot_from_payload(raw["in_progress"]),
        )
        _validate_state(state)
        return state

    @staticmethod
    def _snapshot_from_payload(raw: object) -> DirectionalActivitySnapshot | None:
        if raw is None:
            return None
        if not isinstance(raw, dict) or set(raw) != {"trading_day", "contracts"}:
            raise DirectionalActivityIntegrityError(
                "directional activity snapshot fields are invalid"
            )
        trading_day = raw["trading_day"]
        raw_contracts = raw["contracts"]
        if not isinstance(raw_contracts, dict):
            raise DirectionalActivityIntegrityError(
                "directional activity contracts must be an object"
            )
        contracts: dict[str, ContractActivity] = {}
        for symbol, item in raw_contracts.items():
            if not isinstance(symbol, str) or not isinstance(item, dict):
                raise DirectionalActivityIntegrityError(
                    "directional activity contract entries are invalid"
                )
            required = {
                "symbol",
                "exchange",
                "product",
                "trading_day",
                "volume",
                "open_interest",
                "timestamp",
            }
            if set(item) != required:
                raise DirectionalActivityIntegrityError(
                    f"directional activity contract fields are invalid: {symbol}"
                )
            try:
                timestamp = datetime.fromisoformat(item["timestamp"])
            except (TypeError, ValueError) as exc:
                raise DirectionalActivityIntegrityError(
                    f"invalid directional activity timestamp: {symbol}"
                ) from exc
            contracts[symbol] = ContractActivity(
                symbol=item["symbol"],
                exchange=item["exchange"],
                product=item["product"],
                trading_day=item["trading_day"],
                volume=item["volume"],
                open_interest=item["open_interest"],
                timestamp=timestamp,
            )
        return DirectionalActivitySnapshot(trading_day, contracts)

    def save(self, snapshot: DirectionalActivitySnapshot) -> None:
        self.save_state(DirectionalActivityState(completed=snapshot))

    def save_state(self, state: DirectionalActivityState) -> None:
        encoded = self._encoded_state(state)
        with durable_file_lock(self.path):
            self._replace_encoded(encoded)

    def save_new(
        self,
        snapshot: DirectionalActivitySnapshot,
    ) -> tuple[DirectionalActivityState, DurableFileCreationToken]:
        """Create first activity evidence without replacing a concurrent writer."""

        state = DirectionalActivityState(completed=snapshot)
        encoded = self._encoded_state(state)
        try:
            token = create_durable_file_exclusive(self.path, encoded)
        except FileExistsError as exc:
            raise DirectionalActivityIntegrityError(
                "directional activity appeared concurrently"
            ) from exc
        self._last_creation_token = token
        return state, token

    def _encoded_state(self, state: DirectionalActivityState) -> bytes:
        _validate_state(state)
        unsigned = {
            "schema_version": ACTIVITY_SCHEMA_VERSION,
            "completed": self._snapshot_payload(state.completed),
            "in_progress": self._snapshot_payload(state.in_progress),
        }
        envelope = {**unsigned, "checksum": self._checksum(unsigned)}
        encoded = json.dumps(
            envelope,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        return encoded

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
    def _snapshot_payload(snapshot: DirectionalActivitySnapshot | None) -> dict | None:
        if snapshot is None:
            return None
        return {
            "trading_day": snapshot.trading_day,
            "contracts": {
                symbol: {**asdict(item), "timestamp": item.timestamp.isoformat()}
                for symbol, item in sorted(snapshot.contracts.items())
            },
        }

    @staticmethod
    def _checksum(unsigned: dict) -> str:
        encoded = json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


class DirectionalActivityTracker:
    """Track live activity in memory and explicitly checkpoint durable evidence.

    Restart restores only the last successful checkpoint. A trading-day transition is
    itself a forced checkpoint so completed-day evidence is never exposed in memory
    without the same transition being durable.
    """

    def __init__(self, store: DirectionalActivityStore) -> None:
        self.store = store
        state = store.load_state()
        self.completed_snapshot = state.completed
        self._current_trading_day = (
            state.in_progress.trading_day if state.in_progress is not None else ""
        )
        self._current = dict(state.in_progress.contracts) if state.in_progress is not None else {}
        self._dirty = False

    @property
    def current_trading_day(self) -> str:
        return self._current_trading_day

    def checkpoint(self) -> None:
        """Durably commit the latest in-memory activity, if it changed."""
        if not self._dirty:
            return
        self.store.save_state(
            DirectionalActivityState(
                completed=self.completed_snapshot,
                in_progress=DirectionalActivitySnapshot(
                    self._current_trading_day, dict(self._current)
                ),
            )
        )
        self._dirty = False

    def observe(self, tick: Tick, contract: ContractInfo) -> None:
        trading_day = tick.trading_day
        if not isinstance(trading_day, str):
            raise DirectionalActivityIntegrityError(
                "directional activity trading day must be string"
            )
        if not trading_day:
            return
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            or value < 0
            for value in (tick.volume, tick.open_interest)
        ):
            raise DirectionalActivityIntegrityError(
                "raw activity must contain finite numbers and be non-negative"
            )
        tick.validate()
        _validate_trading_day(trading_day)
        if tick.symbol != contract.symbol or tick.exchange.upper() != contract.exchange.upper():
            raise DirectionalActivityIntegrityError("tick and contract activity identity mismatch")
        if not contract.product:
            raise DirectionalActivityIntegrityError("contract activity product is missing")
        activity = ContractActivity(
            symbol=tick.symbol,
            exchange=contract.exchange,
            product=contract.product.upper(),
            trading_day=trading_day,
            volume=float(tick.volume),
            open_interest=float(tick.open_interest),
            timestamp=tick.timestamp,
        )
        observed_date = datetime.strptime(trading_day, "%Y%m%d").date()
        if self._current_trading_day:
            current_date = datetime.strptime(self._current_trading_day, "%Y%m%d").date()
            if observed_date < current_date:
                raise DirectionalActivityIntegrityError(
                    "directional activity trading day cannot move backward"
                )
        elif (
            self.completed_snapshot is not None
            and observed_date <= self.completed_snapshot.trading_date
        ):
            raise DirectionalActivityIntegrityError(
                "directional activity trading day cannot move backward or reopen"
            )
        previous = self._current.get(tick.symbol)
        if previous is None and self.completed_snapshot is not None:
            previous = self.completed_snapshot.contracts.get(tick.symbol)
        if previous is not None and activity.timestamp < previous.timestamp:
            raise DirectionalActivityIntegrityError(
                f"observation is older than the latest activity: {tick.symbol}"
            )
        if (
            previous is not None
            and activity.timestamp == previous.timestamp
            and activity != previous
        ):
            raise DirectionalActivityIntegrityError(
                f"observation conflicts at the latest activity timestamp: {tick.symbol}"
            )
        if self._current.get(tick.symbol) == activity:
            return
        if self._current_trading_day and trading_day != self._current_trading_day:
            completed = DirectionalActivitySnapshot(self._current_trading_day, dict(self._current))
            next_current = {tick.symbol: activity}
            self.store.save_state(
                DirectionalActivityState(
                    completed=completed,
                    in_progress=DirectionalActivitySnapshot(trading_day, next_current),
                )
            )
            self.completed_snapshot = completed
            self._current_trading_day = trading_day
            self._current = next_current
            self._dirty = False
            return
        if trading_day != self._current_trading_day:
            self._current_trading_day = trading_day
        self._current[tick.symbol] = activity
        self._dirty = True


def select_contracts_from_activity(
    config: DirectionalConfig,
    catalog: Iterable[ContractInfo],
    snapshot: DirectionalActivitySnapshot | None,
    planned_date: date,
    preferred_symbols: Mapping[str, str] | None = None,
) -> dict[str, ContractInfo]:
    """Choose next-day contracts from completed activity with causal roll hysteresis.

    An eligible incumbent is retained unless one challenger has both strictly higher
    completed-day open interest and strictly higher volume. Expiry/listing/activity
    eligibility remains authoritative and immediately forces a deterministic roll.
    """
    if snapshot is None:
        return {}
    validate_directional_activity_snapshot(snapshot)
    preferred = {str(k).upper(): str(v) for k, v in (preferred_symbols or {}).items()}
    products = {item.upper() for item in config.products}
    exchanges = {item.upper() for item in config.exchanges}
    candidates: dict[str, list[tuple[float, float, date, ContractInfo]]] = {}
    for item in catalog:
        product = item.product.upper()
        if product not in products or item.exchange.upper() not in exchanges:
            continue
        if item.listing:
            try:
                if date.fromisoformat(item.listing) > planned_date:
                    continue
            except ValueError:
                continue
        try:
            expiry = date.fromisoformat(item.expiry)
        except ValueError:
            continue
        if (expiry - planned_date).days < config.min_days_to_expiry:
            continue
        activity = snapshot.contracts.get(item.symbol)
        if activity is None or activity.trading_day != snapshot.trading_day:
            continue
        if (
            activity.exchange.upper() != item.exchange.upper()
            or activity.product.upper() != product
        ):
            continue
        if activity.volume < config.min_volume or activity.open_interest < config.min_open_interest:
            continue
        candidates.setdefault(product, []).append(
            (activity.open_interest, activity.volume, expiry, item)
        )

    result: dict[str, ContractInfo] = {}
    for product, rows in candidates.items():
        rows.sort(key=lambda row: (-row[0], -row[1], row[2], row[3].symbol))
        incumbent_symbol = preferred.get(product)
        incumbent = next((row for row in rows if row[3].symbol == incumbent_symbol), None)
        if incumbent is not None:
            dominant = [row for row in rows if row[0] > incumbent[0] and row[1] > incumbent[1]]
            if not dominant:
                result[product] = incumbent[3]
                continue
            dominant.sort(key=lambda row: (-row[0], -row[1], row[2], row[3].symbol))
            result[product] = dominant[0][3]
            continue
        result[product] = rows[0][3]
    return result
