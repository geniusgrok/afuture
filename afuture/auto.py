"""轻量级自动合约发现与跨期组合选择。

从 CTP 合约目录生成同品种相邻月份，观察盘口后按活动度、统计质量和净边际排名。
选标只负责“从哪里交易”，正式策略、风险和执行仍走唯一生产链路。
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from math import log
from zoneinfo import ZoneInfo

from .auto_runtime import MetadataPrefetcher
from .models import ContractInfo, ContractSpec, PairConfig, Tick
from .sample_store import MarketSampleStore
from .scanner import SpreadScanner

_CHINA_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class AutoConfig:
    """自动发现的最小配置。"""

    enabled: bool = False
    products: tuple[str, ...] = ("m", "rb", "TA", "c", "p")
    exchanges: tuple[str, ...] = ("DCE", "SHFE", "CZCE")
    max_active_pairs: int = 2
    max_pairs_per_product: int = 1
    max_contracts_per_product: int = 3
    min_days_to_expiry: int = 20
    scan_interval_seconds: float = 30.0
    max_sync_seconds: float = 2.0

    lookback: int = 60
    entry_z: float = 2.0
    exit_z: float = 0.5
    stop_z: float = 4.0
    max_pair_volume: int = 3
    sample_seconds: int = 60
    max_holding_samples: int = 120
    structural_mean_shift_z: float = 3.0
    structural_vol_ratio: float = 2.5
    legging_buffer: float = 0.0

    signal_transform: str = "spread"
    confirm_entry: bool = False
    confirmation_retrace_z: float = 0.0
    min_confirmed_entry_z: float = 0.0
    entry_trend_window: int = 6
    max_entry_z_slope: float = 999.0
    daily_sample_window: str = ""

    min_volume: float = 5000.0
    min_open_interest: float = 10000.0
    min_liquidity_score: float = 0.5
    min_stationarity_score: float = 0.02
    max_half_life: float = 120.0
    min_net_edge: float = 0.0
    slippage_ticks: int = 1
    metadata_timeout_seconds: float = 10.0
    session_windows: tuple[str, ...] = (
        "09:00-10:15",
        "10:30-11:30",
        "13:30-15:00",
    )

    def validate(self) -> None:
        if not self.enabled:
            return
        if not self.products or not self.exchanges:
            raise ValueError("auto products/exchanges cannot be empty")
        if self.max_active_pairs <= 0 or self.max_pairs_per_product <= 0:
            raise ValueError("auto pair limits must be positive")
        if self.max_contracts_per_product < 2:
            raise ValueError("auto max_contracts_per_product must be at least 2")
        if self.min_days_to_expiry < 0 or self.scan_interval_seconds < 0:
            raise ValueError("auto calendar/scan limits cannot be negative")
        if self.max_sync_seconds <= 0:
            raise ValueError("auto max_sync_seconds must be positive")
        if self.lookback < 2 or not 0 <= self.exit_z < self.entry_z < self.stop_z:
            raise ValueError("auto z-score thresholds are invalid")
        if self.max_pair_volume <= 0:
            raise ValueError("auto max_pair_volume must be positive")
        if self.signal_transform not in {"spread", "log_ratio"}:
            raise ValueError("auto signal_transform must be spread or log_ratio")
        if self.confirm_entry:
            if self.confirmation_retrace_z <= 0:
                raise ValueError("auto confirmation_retrace_z must be positive")
            if not 0 < self.min_confirmed_entry_z < self.entry_z:
                raise ValueError("auto min_confirmed_entry_z must be below entry_z")
        if self.entry_trend_window < 2 or self.max_entry_z_slope <= 0:
            raise ValueError("auto entry trend limits are invalid")
        if self.daily_sample_window:
            self._parse_daily_window(self.daily_sample_window)
        if self.min_volume < 0 or self.min_open_interest < 0:
            raise ValueError("auto activity thresholds cannot be negative")
        if not 0 <= self.min_liquidity_score <= 1:
            raise ValueError("auto min_liquidity_score must be between 0 and 1")
        if not 0 <= self.min_stationarity_score <= 1:
            raise ValueError("auto min_stationarity_score must be between 0 and 1")
        if self.max_half_life <= 0 or self.metadata_timeout_seconds <= 0:
            raise ValueError("auto half-life/metadata timeout must be positive")

    @staticmethod
    def _parse_daily_window(raw: str) -> tuple[time, time]:
        try:
            left, right = raw.split("-", 1)
            start, end = time.fromisoformat(left), time.fromisoformat(right)
        except ValueError as exc:
            raise ValueError(f"invalid auto daily sample window: {raw}") from exc
        if start >= end:
            raise ValueError("auto daily sample window must be increasing")
        return start, end


class AutoPairSelector:
    """把合约目录转换为少量、可解释的相邻月份候选。"""

    def __init__(self, config: AutoConfig) -> None:
        config.validate()
        self.config = config

    def build_pairs(self, catalog: Iterable[ContractInfo], today: date) -> list[PairConfig]:
        allowed_products = {item.lower() for item in self.config.products}
        allow_all_products = "*" in allowed_products
        allowed_exchanges = {item.upper() for item in self.config.exchanges}
        grouped: dict[tuple[str, str], list[tuple[date, ContractInfo]]] = defaultdict(list)

        for item in catalog:
            product_key = item.product.lower()
            if not allow_all_products and product_key not in allowed_products:
                continue
            if item.exchange.upper() not in allowed_exchanges:
                continue
            try:
                expiry = date.fromisoformat(item.expiry)
            except ValueError:
                continue
            if (expiry - today).days < self.config.min_days_to_expiry:
                continue
            grouped[(product_key, item.exchange.upper())].append((expiry, item))

        pairs: list[PairConfig] = []
        for (product, exchange), rows in sorted(grouped.items()):
            rows.sort(key=lambda row: (row[0], row[1].symbol))
            rows = rows[: self.config.max_contracts_per_product]
            for (_, near), (_, far) in zip(rows, rows[1:], strict=False):
                pairs.append(self._pair(product, exchange, near, far))
        return pairs

    def _pair(
        self,
        product: str,
        exchange: str,
        near: ContractInfo,
        far: ContractInfo,
    ) -> PairConfig:
        return PairConfig(
            pair_id=f"auto_{product}_{near.symbol}_{far.symbol}",
            near_symbol=near.symbol,
            far_symbol=far.symbol,
            exchange=exchange,
            volume=self.config.max_pair_volume,
            lookback=self.config.lookback,
            entry_z=self.config.entry_z,
            exit_z=self.config.exit_z,
            stop_z=self.config.stop_z,
            sample_seconds=self.config.sample_seconds,
            expiry_near=near.expiry,
            expiry_far=far.expiry,
            max_holding_samples=self.config.max_holding_samples,
            structural_mean_shift_z=self.config.structural_mean_shift_z,
            structural_vol_ratio=self.config.structural_vol_ratio,
            min_net_edge=self.config.min_net_edge,
            legging_buffer=self.config.legging_buffer,
            risk_group=product,
            session_windows=self.config.session_windows,
            signal_transform=self.config.signal_transform,
            confirm_entry=self.config.confirm_entry,
            confirmation_retrace_z=self.config.confirmation_retrace_z,
            min_confirmed_entry_z=self.config.min_confirmed_entry_z,
            entry_trend_window=self.config.entry_trend_window,
            max_entry_z_slope=self.config.max_entry_z_slope,
            min_stationarity_score=self.config.min_stationarity_score,
            max_half_life=self.config.max_half_life,
            daily_sample_window=self.config.daily_sample_window,
        )


class AutoPairManager:
    """维护自动候选、有限 warm history、候选证据和非阻塞元数据缓存。"""

    def __init__(
        self,
        config: AutoConfig,
        *,
        sample_store: MarketSampleStore | None = None,
        metadata_prefetcher: MetadataPrefetcher | None = None,
        evidence_recorder=None,
    ) -> None:
        config.validate()
        self.config = config
        self.selector = AutoPairSelector(config)
        self.scanner = SpreadScanner(
            min_liquidity_score=config.min_liquidity_score,
            slippage_ticks=config.slippage_ticks,
            max_sync_seconds=config.max_sync_seconds,
        )
        self.candidate_pairs: list[PairConfig] = []
        self._pairs: dict[str, PairConfig] = {}
        self._history: dict[str, deque[Tick]] = {}
        self._specs: dict[str, ContractSpec] = {}
        self._initialized = False
        self._last_scan: datetime | None = None
        self._catalog_day: date | None = None
        self.last_eligible_ids: set[str] = set()
        self.sample_store = sample_store
        self.metadata = metadata_prefetcher or MetadataPrefetcher(config.metadata_timeout_seconds)
        self.evidence = evidence_recorder

    @property
    def initialized(self) -> bool:
        return self._initialized

    def close(self) -> None:
        self._flush_sample_store()
        self.metadata.close()

    def prepare_catalog(self, catalog: Iterable[ContractInfo], today: date) -> list[PairConfig]:
        self.candidate_pairs = self.selector.build_pairs(catalog, today)
        self._pairs = {pair.pair_id: pair for pair in self.candidate_pairs}
        self._catalog_day = today
        max_history = max(self.config.lookback * 4, self.config.lookback + 8)
        for pair in self.candidate_pairs:
            for symbol in (pair.near_symbol, pair.far_symbol):
                if symbol in self._history:
                    continue
                history: deque[Tick] = deque(maxlen=max_history)
                if self.sample_store is not None:
                    for row in self.sample_store.load(symbol)[-max_history:]:
                        history.append(row)
                self._history[symbol] = history
        return list(self.candidate_pairs)

    def bootstrap(
        self,
        broker,
        today: date,
        restored_pairs: dict[str, dict] | None = None,
        restored_history: dict[str, list[dict]] | None = None,
    ) -> list[tuple[PairConfig, dict[str, ContractSpec]]]:
        """读取柜台目录、恢复采样/动态组合状态并订阅候选。"""
        catalog = broker.get_contract_catalog()
        if not catalog:
            raise RuntimeError("auto discovery contract catalog is empty")
        self.prepare_catalog(catalog, today)
        if restored_history:
            self.restore_history(restored_history)

        restored: list[tuple[PairConfig, dict[str, ContractSpec]]] = []
        catalog_symbols = {item.symbol for item in catalog}
        for pair_id, raw in (restored_pairs or {}).items():
            row = dict(raw)
            if "session_windows" in row:
                row["session_windows"] = tuple(row["session_windows"])
            pair = PairConfig(**row)
            if pair.near_symbol not in catalog_symbols or pair.far_symbol not in catalog_symbols:
                raise RuntimeError(f"persisted auto pair is no longer in CTP catalog: {pair_id}")
            self._pairs[pair.pair_id] = pair
            if all(existing.pair_id != pair.pair_id for existing in self.candidate_pairs):
                self.candidate_pairs.append(pair)
            pair_specs = self._ensure_specs(broker, pair)
            restored.append((pair, pair_specs))

        symbols = {
            symbol
            for pair in self.candidate_pairs
            for symbol in (pair.near_symbol, pair.far_symbol)
        }
        for symbol in sorted(symbols):
            pair = next(
                item
                for item in self.candidate_pairs
                if symbol in {item.near_symbol, item.far_symbol}
            )
            broker.subscribe(symbol, pair.exchange)
        self._initialized = True
        return restored

    def refresh_if_needed(
        self,
        broker,
        today: date,
        *,
        retained_pairs: Iterable[PairConfig] = (),
    ) -> bool:
        """交易日变化时重新应用到期过滤并使前一日元数据缓存失效。"""
        if self._catalog_day == today:
            return False
        self._flush_sample_store()
        self._specs.clear()
        self.metadata.invalidate()
        catalog = broker.get_contract_catalog()
        if not catalog:
            raise RuntimeError("auto discovery contract catalog is empty")
        fresh = self.selector.build_pairs(catalog, today)
        retained = {pair.pair_id: pair for pair in retained_pairs}
        self.candidate_pairs = list(fresh)
        self._pairs = {pair.pair_id: pair for pair in fresh}
        self._pairs.update(retained)
        for pair in retained.values():
            if all(item.pair_id != pair.pair_id for item in self.candidate_pairs):
                self.candidate_pairs.append(pair)
        max_history = max(self.config.lookback * 4, self.config.lookback + 8)
        symbols = {
            symbol
            for pair in self.candidate_pairs
            for symbol in (pair.near_symbol, pair.far_symbol)
        }
        for symbol in symbols:
            if symbol not in self._history:
                history: deque[Tick] = deque(maxlen=max_history)
                if self.sample_store is not None:
                    for row in self.sample_store.load(symbol)[-max_history:]:
                        history.append(row)
                self._history[symbol] = history
            pair = next(
                item
                for item in self.candidate_pairs
                if symbol in {item.near_symbol, item.far_symbol}
            )
            broker.subscribe(symbol, pair.exchange)
        self._catalog_day = today
        self._last_scan = None
        return True

    def observe(self, tick: Tick) -> None:
        """按采样语义保留行情；日频模式每天只保留收盘窗口内最后一笔。"""
        history = self._history.get(tick.symbol)
        if history is None:
            return
        if self.config.daily_sample_window:
            current = tick.timestamp.astimezone(_CHINA_TZ).timetz().replace(tzinfo=None)
            start, end = AutoConfig._parse_daily_window(self.config.daily_sample_window)
            if not start <= current <= end:
                return
            if history and history[-1].trading_day == tick.trading_day:
                if tick.timestamp >= history[-1].timestamp:
                    history[-1] = tick
            else:
                history.append(tick)
            return

        sample_seconds = self.config.sample_seconds
        if sample_seconds <= 0 or not history:
            history.append(tick)
            return
        current_bucket = int(tick.timestamp.timestamp() // sample_seconds)
        last_bucket = int(history[-1].timestamp.timestamp() // sample_seconds)
        if current_bucket < last_bucket:
            return
        if current_bucket == last_bucket:
            history[-1] = tick
            return
        history.append(tick)

    def snapshot_history(self) -> dict[str, list[dict]]:
        """导出有界采样历史，供兼容状态恢复和测试使用。"""
        result: dict[str, list[dict]] = {}
        for symbol, rows in self._history.items():
            if not rows:
                continue
            payload: list[dict] = []
            for tick in rows:
                row = asdict(tick)
                row["timestamp"] = tick.timestamp.isoformat()
                payload.append(row)
            result[symbol] = payload
        return result

    def restore_history(self, raw: dict[str, list[dict]]) -> None:
        """只恢复当前候选目录仍需要的合约历史，并重新执行 Tick 校验。"""
        for symbol, rows in raw.items():
            history = self._history.get(symbol)
            if history is None:
                continue
            history.clear()
            maxlen = history.maxlen
            if maxlen is None:
                raise RuntimeError("auto sample history must be bounded")
            for item in rows[-maxlen:]:
                row = dict(item)
                row["timestamp"] = datetime.fromisoformat(str(row["timestamp"]))
                tick = Tick(**row)
                tick.validate()
                history.append(tick)

    def should_scan(self, now: datetime) -> bool:
        if self._last_scan is None:
            return True
        return (now - self._last_scan).total_seconds() >= self.config.scan_interval_seconds

    def select(
        self,
        broker,
        *,
        now: datetime,
        protected_pair_ids: set[str],
    ) -> list[PairConfig] | None:
        """按硬门筛选并排序；已有持仓只保留管理权，不自动获得新开仓资格。"""
        if not self._initialized or not self.should_scan(now):
            return None
        self._last_scan = now
        self._flush_sample_store()

        scored: list[tuple[PairConfig, float]] = []
        self.last_eligible_ids = set()
        for pair in self.candidate_pairs:
            near_history = self._history.get(pair.near_symbol)
            far_history = self._history.get(pair.far_symbol)
            if near_history is None or far_history is None:
                continue
            if len(near_history) < pair.lookback + 1 or len(far_history) < pair.lookback + 1:
                continue
            near, far = near_history[-1], far_history[-1]
            if min(near.volume, far.volume) < self.config.min_volume:
                self._record_candidate(pair, near, far, None, None, "volume below minimum")
                continue
            if min(near.open_interest, far.open_interest) < self.config.min_open_interest:
                self._record_candidate(
                    pair,
                    near,
                    far,
                    None,
                    None,
                    "open interest below minimum",
                )
                continue

            ticks = list(near_history) + list(far_history)
            synchronized = self.scanner.synchronized_ticks(pair, ticks)
            statistics = self.scanner.statistics(pair, ticks, synchronized=synchronized)
            if statistics is None:
                self._record_candidate(
                    pair,
                    near,
                    far,
                    None,
                    None,
                    "insufficient synchronized statistics",
                )
                continue
            if statistics.stationarity_score < self.config.min_stationarity_score:
                self._record_candidate(
                    pair,
                    near,
                    far,
                    statistics,
                    None,
                    "stationarity below minimum",
                )
                continue
            if statistics.half_life > self.config.max_half_life:
                self._record_candidate(
                    pair,
                    near,
                    far,
                    statistics,
                    None,
                    "half-life above maximum",
                )
                continue
            if self.scanner.candidate_entry(pair, synchronized, statistics) is None:
                self._record_candidate(
                    pair,
                    near,
                    far,
                    statistics,
                    None,
                    "confirmed executable entry threshold not reached",
                )
                continue

            pair_specs = self._prefetched_specs(broker, pair)
            if pair_specs is None:
                reason = (
                    self.metadata.error((pair.near_symbol, pair.far_symbol)) or "metadata pending"
                )
                self._record_candidate(pair, near, far, statistics, None, reason)
                continue
            candidate = self.scanner.scan_pair(pair, ticks, pair_specs)
            if candidate is None:
                self._record_candidate(
                    pair,
                    near,
                    far,
                    statistics,
                    None,
                    "net-edge candidate unavailable",
                )
                continue
            if candidate.liquidity_score < self.config.min_liquidity_score:
                self._record_candidate(
                    pair,
                    near,
                    far,
                    statistics,
                    candidate,
                    "liquidity below minimum",
                )
                continue
            if candidate.net_edge <= self.config.min_net_edge:
                self._record_candidate(
                    pair,
                    near,
                    far,
                    statistics,
                    candidate,
                    "net edge below minimum",
                )
                continue
            self.last_eligible_ids.add(pair.pair_id)
            scored.append((pair, candidate.score))
            self._record_candidate(pair, near, far, statistics, candidate, "")

        return self.rank_candidates(scored, protected_pair_ids=protected_pair_ids)

    def rank_candidates(
        self,
        scored: list[tuple[PairConfig, float]],
        *,
        protected_pair_ids: set[str],
    ) -> list[PairConfig]:
        selected: list[PairConfig] = []
        product_counts: dict[str, int] = defaultdict(int)

        for pair_id in sorted(protected_pair_ids):
            pair = self._pairs.get(pair_id)
            if pair is None or len(selected) >= self.config.max_active_pairs:
                continue
            selected.append(pair)
            product_counts[pair.risk_group] += 1

        for pair, _score in sorted(scored, key=lambda row: row[1], reverse=True):
            if len(selected) >= self.config.max_active_pairs:
                break
            if pair.pair_id in {item.pair_id for item in selected}:
                continue
            if product_counts[pair.risk_group] >= self.config.max_pairs_per_product:
                continue
            selected.append(pair)
            product_counts[pair.risk_group] += 1
        return selected

    def pair_specs(self, pair: PairConfig) -> dict[str, ContractSpec]:
        return {symbol: self._specs[symbol] for symbol in (pair.near_symbol, pair.far_symbol)}

    def strategy_seed(self, pair: PairConfig) -> dict:
        """用扫描阶段历史预热正式策略，并保留确认入场的武装状态。"""
        ticks = list(self._history.get(pair.near_symbol, ())) + list(
            self._history.get(pair.far_symbol, ())
        )
        synchronized = self.scanner.synchronized_ticks(pair, ticks)
        historical = synchronized[:-1]
        signal_history = [
            log(near.mid_price / far.mid_price)
            if pair.signal_transform == "log_ratio"
            else near.mid_price - far.mid_price
            for near, far in historical
        ][-pair.lookback :]
        raw_history = [near.mid_price - far.mid_price for near, far in historical][-pair.lookback :]
        z_history, armed, extreme = self.scanner.confirmation_seed(pair, historical)
        last_ts = ""
        last_day = ""
        if historical:
            last_ts = max(
                historical[-1][0].timestamp,
                historical[-1][1].timestamp,
            ).isoformat()
            last_day = historical[-1][0].trading_day
        return {
            "history": signal_history,
            "raw_history": raw_history,
            "z_history": z_history,
            "position": 0,
            "entry_mean": 0.0,
            "entry_std": 0.0,
            "last_sample_ts": last_ts,
            "last_sample_trading_day": last_day,
            "armed_direction": armed,
            "armed_extreme": extreme,
        }

    def _record_candidate(self, pair, near, far, statistics, candidate, reason: str) -> None:
        if self.evidence is None:
            return
        self.evidence.record_candidate(
            pair_id=pair.pair_id,
            timestamp=max(near.timestamp, far.timestamp).isoformat(),
            zscore=(float(statistics.zscore) if statistics is not None else None),
            stationarity=(float(statistics.stationarity_score) if statistics is not None else None),
            half_life=(float(statistics.half_life) if statistics is not None else None),
            volume=float(min(near.volume, far.volume)),
            open_interest=float(min(near.open_interest, far.open_interest)),
            depth=float(
                min(
                    near.bid_volume,
                    near.ask_volume,
                    far.bid_volume,
                    far.ask_volume,
                )
            ),
            expected_net_edge=(float(candidate.net_edge) if candidate is not None else None),
            candidate_score=(float(candidate.score) if candidate is not None else None),
            reject_reason=str(reason),
        )

    def _prefetched_specs(self, broker, pair: PairConfig) -> dict[str, ContractSpec] | None:
        symbols = (pair.near_symbol, pair.far_symbol)
        if all(symbol in self._specs for symbol in symbols):
            return self.pair_specs(pair)
        rows = self.metadata.get(symbols)
        if rows is None:
            ready = self.metadata.request(broker, symbols)
            rows = self.metadata.get(symbols) if ready else None
        if rows is None:
            return None
        self._specs.update(rows)
        return self.pair_specs(pair)

    def _ensure_specs(self, broker, pair: PairConfig) -> dict[str, ContractSpec]:
        """启动恢复路径允许等待；Tick 关键路径使用 _prefetched_specs。"""
        missing = [
            symbol for symbol in (pair.near_symbol, pair.far_symbol) if symbol not in self._specs
        ]
        if missing:
            rows = broker.get_live_contract_specs(missing, self.config.metadata_timeout_seconds)
            for symbol in missing:
                if symbol not in rows:
                    raise RuntimeError(f"live contract spec missing: {symbol}")
            self._specs.update(rows)
        return self.pair_specs(pair)

    def _flush_sample_store(self) -> None:
        if self.sample_store is None:
            return
        for symbol, history in self._history.items():
            if history:
                self.sample_store.save(symbol, list(history))
