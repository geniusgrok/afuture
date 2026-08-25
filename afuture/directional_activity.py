"""Completed-trading-day activity evidence for directional contract selection.

This sidecar owns only market-selection evidence. Account, order, fill and position truth
remain exclusively in Broker/TradingEngine state.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from .directional import DirectionalConfig
from .models import ContractInfo, Tick


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


class DirectionalActivityStore:
    """Atomically persist the latest completed CTP trading-day activity snapshot."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> DirectionalActivitySnapshot | None:
        if not self.path.exists():
            return None
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        trading_day = str(raw["trading_day"])
        contracts: dict[str, ContractActivity] = {}
        for symbol, item in dict(raw.get("contracts", {})).items():
            contracts[str(symbol)] = ContractActivity(
                symbol=str(item["symbol"]),
                exchange=str(item["exchange"]),
                product=str(item["product"]),
                trading_day=str(item["trading_day"]),
                volume=float(item["volume"]),
                open_interest=float(item["open_interest"]),
                timestamp=datetime.fromisoformat(str(item["timestamp"])),
            )
        return DirectionalActivitySnapshot(trading_day, contracts)

    def save(self, snapshot: DirectionalActivitySnapshot) -> None:
        payload = {
            "trading_day": snapshot.trading_day,
            "contracts": {
                symbol: {
                    **asdict(item),
                    "timestamp": item.timestamp.isoformat(),
                }
                for symbol, item in sorted(snapshot.contracts.items())
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent, delete=False
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            temp = Path(handle.name)
        temp.replace(self.path)


class DirectionalActivityTracker:
    """Freeze the last observations from D only when CTP advances to a new trading day."""

    def __init__(self, store: DirectionalActivityStore) -> None:
        self.store = store
        self.completed_snapshot = store.load()
        self._current_trading_day = ""
        self._current: dict[str, ContractActivity] = {}

    @property
    def current_trading_day(self) -> str:
        return self._current_trading_day

    def observe(self, tick: Tick, contract: ContractInfo) -> None:
        trading_day = str(tick.trading_day or "")
        if not trading_day:
            return
        if self._current_trading_day and trading_day != self._current_trading_day:
            if self._current:
                completed = DirectionalActivitySnapshot(
                    self._current_trading_day, dict(self._current)
                )
                self.store.save(completed)
                self.completed_snapshot = completed
            self._current = {}
        if trading_day != self._current_trading_day:
            self._current_trading_day = trading_day
        self._current[tick.symbol] = ContractActivity(
            symbol=tick.symbol,
            exchange=contract.exchange,
            product=contract.product.upper(),
            trading_day=trading_day,
            volume=float(tick.volume),
            open_interest=float(tick.open_interest),
            timestamp=tick.timestamp,
        )


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
