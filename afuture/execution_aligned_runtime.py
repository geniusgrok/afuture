"""Production adapter for the frozen execution-aligned directional policy."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from .directional import build_margin_aware_target_lots, build_rebalance_plan
from .directional_activity import (
    DirectionalActivityStore,
    DirectionalActivityTracker,
    select_contracts_from_activity,
)
from .directional_data_validation import (
    validate_daily_index,
    validate_finite_columns,
)
from .directional_ohlc_cache import (
    DirectionalOHLCCacheEntry,
    DirectionalOHLCCacheStore,
    canonicalize_ohlc_frames,
    require_unchanged_overlap,
)
from .directional_runtime import (
    _CHINA_TZ,
    DirectionalActionResult,
    DirectionalPortfolioManager,
)
from .execution_aligned_policy import ExecutionAlignedAggressivePolicy
from .models import ContractInfo

FROZEN_PRODUCTS = (
    "A",
    "AG",
    "AL",
    "AP",
    "AU",
    "B",
    "BC",
    "BU",
    "C",
    "CF",
    "CJ",
    "CS",
    "CU",
    "EB",
    "EG",
    "FG",
    "FU",
    "HC",
    "I",
    "J",
    "JM",
    "L",
    "LH",
    "LU",
    "M",
    "MA",
    "NI",
    "NR",
    "OI",
    "P",
    "PB",
    "PF",
    "PG",
    "PK",
    "PP",
    "RB",
    "RM",
    "RU",
    "SA",
    "SF",
    "SM",
    "SN",
    "SP",
    "SR",
    "SS",
    "TA",
    "UR",
    "V",
    "Y",
    "ZN",
)


@dataclass(frozen=True)
class ExecutionAlignedSignalHistory:
    open: pd.DataFrame
    close: pd.DataFrame


class SinaContinuousOHLCProvider:
    """Load continuous daily open/close history once per production signal refresh."""

    def __init__(self, max_workers: int = 8) -> None:
        self.max_workers = max(1, int(max_workers))

    @staticmethod
    def _load_one(product: str) -> pd.DataFrame:
        try:
            import akshare as ak
        except ImportError as exc:
            raise RuntimeError(
                "directional live mode requires the 'live' extra with akshare"
            ) from exc
        frame = ak.futures_zh_daily_sina(symbol=f"{product.upper()}0").copy()
        required = {"date", "open", "close"}
        if frame.empty or not required.issubset(frame.columns):
            raise RuntimeError(f"continuous OHLC history unavailable: {product}")
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        for column in ("open", "close"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        indexed = frame.set_index("date")
        validate_daily_index(
            indexed,
            name=f"{product.upper()} continuous OHLC",
        )
        validate_finite_columns(
            frame,
            ("open", "close"),
            name=f"{product.upper()} continuous OHLC",
            positive=("open", "close"),
        )
        frame.sort_values("date", inplace=True)
        return frame.set_index("date")[["open", "close"]]

    def load(self, products: tuple[str, ...]) -> ExecutionAlignedSignalHistory:
        unique = tuple(dict.fromkeys(item.upper() for item in products))
        frames: dict[str, pd.DataFrame] = {}
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=min(self.max_workers, max(len(unique), 1))) as executor:
            futures = {executor.submit(self._load_one, product): product for product in unique}
            for future in as_completed(futures):
                product = futures[future]
                try:
                    frames[product] = future.result()
                except Exception as exc:
                    errors.append(f"{product}: {type(exc).__name__}: {exc}")
        if errors:
            raise RuntimeError("directional signal refresh failed: " + "; ".join(sorted(errors)))
        open_prices = pd.concat(
            [frames[product]["open"].rename(product) for product in unique], axis=1
        ).sort_index()
        close = pd.concat(
            [frames[product]["close"].rename(product) for product in unique], axis=1
        ).sort_index()
        validate_daily_index(open_prices, name="execution-aligned open history")
        validate_daily_index(close, name="execution-aligned close history")
        return ExecutionAlignedSignalHistory(open_prices, close)


class ExecutionAlignedDirectionalPortfolioManager(DirectionalPortfolioManager):
    """Use completed-day activity and causal OHLC history for production target weights."""

    def __init__(
        self,
        config,
        broker,
        risk_manager,
        *,
        signal_provider=None,
        policy=None,
        activity_store_path: str | Path | None = None,
        ohlc_cache_path: str | Path | None = None,
        activity_tracker: DirectionalActivityTracker | None = None,
        completed_returns_provider=None,
        **kwargs,
    ):
        if policy is None:
            configured = tuple(sorted({str(item).upper() for item in config.products}))
            if configured != FROZEN_PRODUCTS:
                raise ValueError(
                    "execution-aligned production requires the frozen 50-product universe"
                )
            policy = ExecutionAlignedAggressivePolicy(products=configured)
        super().__init__(
            config,
            broker,
            risk_manager,
            signal_provider=signal_provider or SinaContinuousOHLCProvider(),
            policy=policy,
            **kwargs,
        )
        self._execution_signal_history: ExecutionAlignedSignalHistory | None = None
        self._signal_products = tuple(str(item).upper() for item in config.products)
        self._ohlc_cache = (
            DirectionalOHLCCacheStore(ohlc_cache_path) if ohlc_cache_path is not None else None
        )
        self.activity_tracker: DirectionalActivityTracker | None
        if activity_tracker is not None:
            self.activity_tracker = activity_tracker
        elif activity_store_path is not None:
            self.activity_tracker = DirectionalActivityTracker(
                DirectionalActivityStore(activity_store_path)
            )
        else:
            self.activity_tracker = None
        self.completed_returns_provider = completed_returns_provider
        self._catalog_by_symbol: dict[str, ContractInfo] = {}

    @property
    def signal_cache_path(self) -> Path | None:
        """Return the configured market-evidence path without reading or fetching data."""
        return self._ohlc_cache.path if self._ohlc_cache is not None else None

    def bootstrap(self, now: datetime) -> None:
        super().bootstrap(now)
        self._catalog_by_symbol = {item.symbol: item for item in self._catalog}

    def observe(self, tick) -> None:
        super().observe(tick)
        if self.activity_tracker is None:
            return
        contract = self._catalog_by_symbol.get(tick.symbol)
        if contract is not None:
            self.activity_tracker.observe(tick, contract)

    def close(self) -> None:
        try:
            self.checkpoint_activity()
        finally:
            super().close()

    def checkpoint_activity(self) -> None:
        """Durably commit activity after one bounded broker event batch."""
        if self.activity_tracker is not None:
            self.activity_tracker.checkpoint()

    def _ordered_provider_frame(self, frame: pd.DataFrame) -> pd.DataFrame:
        result = frame.copy()
        result.columns = [str(item).upper() for item in result.columns]
        if result.columns.has_duplicates:
            raise RuntimeError("execution-aligned signal history has duplicate products")
        if set(result.columns) != set(self._signal_products):
            raise RuntimeError(
                "execution-aligned signal history product set does not match configuration"
            )
        return result.reindex(columns=self._signal_products)

    def _canonicalize_history(
        self,
        history: ExecutionAlignedSignalHistory,
        *,
        latest_allowed_date: date,
    ) -> ExecutionAlignedSignalHistory:
        open_prices, close = canonicalize_ohlc_frames(
            self._signal_products,
            self._ordered_provider_frame(history.open),
            self._ordered_provider_frame(history.close),
            name="execution-aligned signal history",
        )
        latest_provider_day = pd.Timestamp(close.index[-1]).date()
        if latest_provider_day > latest_allowed_date:
            raise RuntimeError(
                "directional signal history is from the future beyond authoritative planning day "
                f"{latest_allowed_date.isoformat()}: latest={latest_provider_day.isoformat()}"
            )
        return ExecutionAlignedSignalHistory(open_prices, close)

    @staticmethod
    def _history_through(
        history: ExecutionAlignedSignalHistory,
        max_date: date,
    ) -> ExecutionAlignedSignalHistory:
        keep = history.close.index <= pd.Timestamp(max_date)
        close = history.close.loc[keep]
        open_prices = history.open.loc[keep]
        if len(close) < 140:
            raise RuntimeError("directional signal history is shorter than 140 days")
        return ExecutionAlignedSignalHistory(open_prices, close)

    @staticmethod
    def _history_from_cache(entry: DirectionalOHLCCacheEntry) -> ExecutionAlignedSignalHistory:
        return ExecutionAlignedSignalHistory(entry.open.copy(), entry.close.copy())

    def _validated_cache_fallback(
        self,
        entry: DirectionalOHLCCacheEntry,
        *,
        local: datetime,
        max_date: date,
        latest_allowed_date: date,
        required_signal_day: date | None,
    ) -> ExecutionAlignedSignalHistory:
        full_history = self._canonicalize_history(
            self._history_from_cache(entry),
            latest_allowed_date=latest_allowed_date,
        )
        self._validate_activity_signal_alignment(full_history, required_signal_day)
        history = self._history_through(full_history, max_date)
        self._validate_signal_history(history, local, required_signal_day)
        return history

    def _current_ctp_trading_date(self) -> date | None:
        if self.activity_tracker is None:
            return None
        raw = str(getattr(self.activity_tracker, "current_trading_day", "") or "")
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%Y%m%d").date()
        except ValueError:
            return None

    def _planned_trading_date(self, now: datetime) -> date:
        """Prefer the CTP trading day, especially for the cross-calendar-date night session."""
        return self._current_ctp_trading_date() or self._local(now).date()

    def _latest_allowed_signal_date(
        self,
        now: datetime,
        required_signal_day: date | None,
    ) -> date:
        current_trading_date = self._current_ctp_trading_date()
        if current_trading_date is not None:
            return current_trading_date
        if self.activity_tracker is not None:
            if required_signal_day is None:
                raise RuntimeError("completed directional activity is unavailable")
            return required_signal_day
        return self._local(now).date()

    def _validate_activity_signal_alignment(
        self,
        raw: ExecutionAlignedSignalHistory,
        required_signal_day: date | None,
    ) -> None:
        """Reject an old activity snapshot when OHLC proves a newer day completed.

        The current CTP trading day itself may appear as an unfinished daily bar, so only
        common OHLC dates strictly before the CTP trading day count as completed evidence.
        """
        if required_signal_day is None:
            return
        current_trading_date = self._current_ctp_trading_date()
        if current_trading_date is None:
            return
        completed = raw.close.index[raw.close.index < pd.Timestamp(current_trading_date)]
        if completed.empty:
            return
        latest_completed = pd.Timestamp(completed.max()).date()
        if latest_completed > required_signal_day:
            raise RuntimeError(
                "completed directional activity is stale: "
                f"activity={required_signal_day.isoformat()}, "
                f"latest_completed_signal={latest_completed.isoformat()}, "
                f"current_trading_day={current_trading_date.isoformat()}"
            )

    def _validate_signal_history(
        self,
        history: ExecutionAlignedSignalHistory,
        local: datetime,
        required_signal_day: date | None,
    ) -> None:
        latest_day = pd.Timestamp(history.close.index[-1]).date()
        if required_signal_day is not None and latest_day < required_signal_day:
            raise RuntimeError(
                "directional history does not cover required signal trading day "
                f"{required_signal_day.isoformat()}; latest={latest_day.isoformat()}"
            )
        latest = pd.Timestamp(history.close.index[-1]).to_pydatetime().replace(tzinfo=_CHINA_TZ)
        age_hours = (local - latest).total_seconds() / 3600.0
        if age_hours < -1:
            raise RuntimeError("directional signal history is from the future")
        if required_signal_day is not None:
            return
        if age_hours > self.config.signal_max_age_hours:
            raise RuntimeError(f"directional signal history is stale by {age_hours:.1f}h")

    def _load_signal(
        self,
        now: datetime,
        required_signal_day: date | None = None,
    ) -> ExecutionAlignedSignalHistory:
        local = self._local(now)
        latest_allowed_date = self._latest_allowed_signal_date(now, required_signal_day)
        max_date = required_signal_day or latest_allowed_date
        refresh = (
            self._execution_signal_history is None or self._signal_refresh_date != local.date()
        )
        if refresh:
            provider = self.signal_provider
            if provider is None:
                raise RuntimeError("execution-aligned signal provider is not configured")
            cached_entry: DirectionalOHLCCacheEntry | None = None
            cache_error: Exception | None = None
            if self._ohlc_cache is not None:
                try:
                    cached_entry = self._ohlc_cache.load(self._signal_products)
                except Exception as exc:
                    cache_error = exc
            try:
                raw = provider.load(tuple(item.upper() for item in self.config.products))
                if not isinstance(raw, ExecutionAlignedSignalHistory):
                    raise RuntimeError("execution-aligned signal provider must return OHLC history")
                full_history = self._canonicalize_history(
                    raw,
                    latest_allowed_date=latest_allowed_date,
                )
                self._validate_activity_signal_alignment(full_history, required_signal_day)
                candidate = self._history_through(full_history, max_date)
                self._validate_signal_history(candidate, local, required_signal_day)
                if cache_error is not None:
                    raise cache_error
                if cached_entry is not None:
                    require_unchanged_overlap(cached_entry, candidate.open, candidate.close)
                if self._ohlc_cache is not None and required_signal_day is not None:
                    cached_entry = self._ohlc_cache.save(
                        self._signal_products,
                        candidate.open,
                        candidate.close,
                    )
                    candidate = self._history_from_cache(cached_entry)
                self._execution_signal_history = candidate
            except Exception as provider_error:
                if cache_error is not None:
                    raise cache_error from provider_error
                if cached_entry is not None:
                    self._execution_signal_history = self._validated_cache_fallback(
                        cached_entry,
                        local=local,
                        max_date=max_date,
                        latest_allowed_date=latest_allowed_date,
                        required_signal_day=required_signal_day,
                    )
                elif self._ohlc_cache is None and self._execution_signal_history is not None:
                    full_history = self._canonicalize_history(
                        self._execution_signal_history,
                        latest_allowed_date=latest_allowed_date,
                    )
                    self._execution_signal_history = self._history_through(full_history, max_date)
                else:
                    raise
            self._signal_refresh_date = local.date()

        history = self._execution_signal_history
        if history is None:
            raise RuntimeError("execution-aligned signal history is unavailable")
        full_history = self._canonicalize_history(
            history,
            latest_allowed_date=latest_allowed_date,
        )
        history = self._history_through(full_history, max_date)
        self._validate_signal_history(history, local, required_signal_day)
        self._execution_signal_history = history
        return ExecutionAlignedSignalHistory(history.open.copy(), history.close.copy())

    def _next_target_weights(self, history: ExecutionAlignedSignalHistory) -> dict[str, float]:
        close = history.close
        open_prices = history.open
        last = pd.Timestamp(close.index[-1])
        synthetic_index = last + pd.offsets.BDay(1)
        synthetic_close = close.iloc[[-1]].copy()
        synthetic_close.index = pd.DatetimeIndex([synthetic_index])
        synthetic_open = close.iloc[[-1]].copy()
        synthetic_open.index = pd.DatetimeIndex([synthetic_index])
        weights = self.policy.target_weights(
            pd.concat([open_prices, synthetic_open]),
            pd.concat([close, synthetic_close]),
        )
        gross = sum(abs(float(value)) for value in weights.values())
        if gross > self.config.max_gross_leverage + 1e-10:
            raise RuntimeError(f"directional signal exceeds configured gross leverage: {gross:.6f}")
        return {str(key).upper(): float(value) for key, value in weights.items()}

    def _completed_returns(self) -> tuple[float, ...]:
        provider = self.completed_returns_provider
        if callable(provider):
            return tuple(float(value) for value in provider())
        return ()

    @staticmethod
    def _post_reduction_target(positions, reductions) -> dict[str, int]:
        target = {
            position.symbol: int(position.net_volume)
            for position in positions
            if not position.empty
        }
        for symbol, delta in reductions.items():
            target[symbol] = target.get(symbol, 0) + int(delta)
            if target[symbol] == 0:
                target.pop(symbol)
        return target

    def maybe_rebalance(self, now: datetime) -> DirectionalActionResult:
        if not self._initialized:
            return DirectionalActionResult("reject", "directional manager is not initialized")
        if not self.broker.is_ready():
            return DirectionalActionResult("reject", "broker is not ready")
        if not self._inside_rebalance_window(now):
            return DirectionalActionResult("hold", "outside directional rebalance window")
        if self.broker.get_active_orders():
            return DirectionalActionResult("wait", "active orders must settle before rebalance")
        self._finalize_quality_cycle_if_settled(now)

        positions = self.broker.get_positions()
        snapshot = self.activity_tracker.completed_snapshot if self.activity_tracker else None
        if self.activity_tracker is not None and snapshot is None:
            action = "risk_off" if any(not item.empty for item in positions) else "reject"
            return DirectionalActionResult(action, "completed directional activity is unavailable")
        required_signal_day = snapshot.trading_date if snapshot is not None else None
        try:
            signal = self._load_signal(now, required_signal_day=required_signal_day)
            target_weights = self._next_target_weights(signal)
        except Exception as exc:
            action = "risk_off" if any(not item.empty for item in positions) else "reject"
            return DirectionalActionResult(action, f"directional signal unavailable: {exc}")

        planned_date = self._planned_trading_date(now)
        catalog_by_symbol = {item.symbol: item for item in self._catalog}
        preferred_symbols = {
            catalog_by_symbol[position.symbol].product.upper(): position.symbol
            for position in positions
            if not position.empty and position.symbol in catalog_by_symbol
        }
        selected = (
            select_contracts_from_activity(
                self.config,
                self._catalog,
                snapshot,
                planned_date,
                preferred_symbols=preferred_symbols,
            )
            if snapshot is not None
            else self.selector.select(self._catalog, self._ticks, planned_date)
        )
        required_products = {
            product.upper()
            for product, weight in target_weights.items()
            if abs(float(weight)) > 1e-15
        }
        available_products = {
            product
            for product in required_products
            if product in selected and selected[product].symbol in self._ticks
        }
        unavailable_products = required_products - available_products

        symbols = {item.symbol for item in positions if not item.empty} | {
            selected[product].symbol for product in available_products
        }
        try:
            specs = self._ensure_specs(symbols)
        except Exception as exc:
            return DirectionalActionResult("reject", f"directional metadata unavailable: {exc}")

        product_ticks = {
            product: self._ticks[selected[product].symbol] for product in available_products
        }
        account = self.broker.get_account()
        target_lots = build_margin_aware_target_lots(
            account,
            {product: target_weights[product] for product in available_products},
            product_ticks,
            specs,
            max_contract_volume=min(
                self.config.max_contract_volume,
                self.risk_manager.config.max_contract_volume,
            ),
            max_margin_ratio=self.risk_manager.config.max_margin_ratio,
            min_available_ratio=self.risk_manager.config.min_available_ratio,
            max_daily_loss_ratio=self.risk_manager.config.max_daily_loss_ratio,
            margin_estimate_buffer=self.risk_manager.config.margin_estimate_buffer,
            completed_returns=self._completed_returns(),
            current_lots={
                position.symbol: position.net_volume for position in positions if not position.empty
            },
        )

        symbol_product = {item.symbol: item.product.upper() for item in self._catalog}
        for position in positions:
            if position.empty:
                continue
            product = symbol_product.get(position.symbol)
            if product in unavailable_products:
                target_lots[position.symbol] = position.net_volume

        plan = build_rebalance_plan(positions, target_lots)
        signal_day = pd.Timestamp(signal.close.index[-1]).date().isoformat()
        activity_day = snapshot.trading_day if snapshot is not None else ""
        target_gross = sum(abs(float(value)) for value in target_weights.values())

        if plan.reductions:
            phase_target = self._post_reduction_target(positions, plan.reductions)
            self._start_quality_cycle(
                now,
                signal_day=signal_day,
                activity_day=activity_day,
                target_gross=target_gross,
                target_lots=phase_target,
                reductions=plan.reductions,
                openings={},
                planned_turnover_notional=self._planned_turnover_notional(plan.reductions),
                reason="reduce-before-open",
            )
            return self._submit_reductions(
                positions,
                plan.reductions,
                now,
                reference="directional:rebalance",
            )
        if plan.openings:
            self._start_quality_cycle(
                now,
                signal_day=signal_day,
                activity_day=activity_day,
                target_gross=target_gross,
                target_lots=target_lots,
                reductions={},
                openings=plan.openings,
                planned_turnover_notional=self._planned_turnover_notional(plan.openings),
                reason="open-to-target",
            )
            return self._submit_openings(
                positions,
                plan.openings,
                {product: selected[product] for product in available_products},
                specs,
                now,
            )
        if unavailable_products:
            return DirectionalActionResult(
                "reject" if not any(not item.empty for item in positions) else "hold",
                "directional target unavailable for products: "
                + ",".join(sorted(unavailable_products)),
            )
        return DirectionalActionResult("hold", "directional portfolio is at target")

    def flatten(self, now: datetime) -> DirectionalActionResult:
        positions = self.broker.get_positions()
        if any(position.long_total > 0 and position.short_total > 0 for position in positions):
            # Hedged gross exposure cannot be represented by the net-lot quality schema.
            # Delegate the abnormal safety path to the base gross-position flattener.
            return super().flatten(now)
        if self.broker.get_active_orders():
            return DirectionalActionResult("wait", "active orders must settle before flatten")
        self._finalize_quality_cycle_if_settled(now)
        plan = build_rebalance_plan(positions, {})
        if not plan.reductions:
            return DirectionalActionResult("hold", "directional portfolio is flat")
        snapshot = self.activity_tracker.completed_snapshot if self.activity_tracker else None
        self._start_quality_cycle(
            now,
            signal_day="",
            activity_day=snapshot.trading_day if snapshot is not None else "",
            target_gross=0.0,
            target_lots={},
            reductions=plan.reductions,
            openings={},
            planned_turnover_notional=self._planned_turnover_notional(plan.reductions),
            reason="risk-off-flatten",
        )
        return self._submit_reductions(
            positions,
            plan.reductions,
            now,
            reference="directional:flatten",
        )
