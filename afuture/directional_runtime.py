"""Shared execution lifecycle for the account-exclusive directional portfolio.

Broker/TradingEngine remain the only order, fill, account and position truth. This manager
translates target product weights into concrete risk-gated FAK orders. Its quality ledger
is observability-only and never participates in trading decisions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from threading import Event, Lock, Thread
from typing import Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from .directional import (
    DirectionalConfig,
    DirectionalContractSelector,
    build_realized_gross_reductions,
    build_rebalance_plan,
    build_target_lots,
)
from .directional_execution import depth_aware_opening_price
from .directional_risk import DirectionalRiskResponseMode
from .models import (
    ContractInfo,
    ContractPosition,
    ContractSpec,
    Offset,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Tick,
    Trade,
)
from .position import PositionBook
from .risk import OrderRateLimiter, RiskManager

_CHINA_TZ = ZoneInfo("Asia/Shanghai")


class DirectionalSignalProvider(Protocol):
    def load(self, products: tuple[str, ...]): ...


@dataclass(frozen=True)
class DirectionalActionResult:
    action: str
    reason: str = ""
    order_ids: tuple[str, ...] = ()


class DirectionalPortfolioManager:
    """Translate directional targets into concrete orders through shared hard risk gates."""

    policy_risk_response_mode = DirectionalRiskResponseMode.TARGET_SCALE
    requires_explicit_settlement_roll_forward = False

    def __init__(
        self,
        config: DirectionalConfig,
        broker,
        risk_manager: RiskManager,
        *,
        signal_provider: DirectionalSignalProvider | None = None,
        policy=None,
        aggressive_ticks: int = 1,
        close_today_first: bool = False,
        metadata_timeout_seconds: float = 10.0,
        static_specs: Mapping[str, ContractSpec] | None = None,
        quality_recorder=None,
        raw_tick_observer=None,
    ) -> None:
        config.validate()
        self.config = config
        self.broker = broker
        self.risk_manager = risk_manager
        self.selector = DirectionalContractSelector(config)
        self.signal_provider = signal_provider
        self.policy = policy
        self.aggressive_ticks = max(0, int(aggressive_ticks))
        self.close_today_first = bool(close_today_first)
        self.metadata_timeout_seconds = max(float(metadata_timeout_seconds), 0.1)
        self.rate_limiter = OrderRateLimiter(risk_manager.config.max_orders_per_minute)
        self.quality = quality_recorder
        self.raw_tick_observer = raw_tick_observer
        self._catalog: list[ContractInfo] = []
        self._subscribed_contracts: set[tuple[str, str]] = set()
        self._ticks: dict[str, Tick] = {}
        self._specs: dict[str, ContractSpec] = dict(static_specs or {})
        self._signal_frame: pd.DataFrame | None = None
        self._signal_refresh_date: date | None = None
        self._initialized = False
        self._subscription_lock = Lock()
        self._subscription_stop = Event()
        self._subscription_thread: Thread | None = None
        self._subscription_error = ""

        self._quality_cycle_seq = 0
        self._quality_cycle: dict | None = None
        self._quality_expectations: dict[str, dict] = {}

    def bootstrap(self, now: datetime) -> None:
        catalog = self.broker.get_contract_catalog()
        if not catalog:
            raise RuntimeError("directional contract catalog is empty")
        products = {item.upper() for item in self.config.products}
        exchanges = {item.upper() for item in self.config.exchanges}
        local_date = self._local(now).date()
        trading_day: str | None = None
        catalog_date = local_date
        if self.raw_tick_observer is not None:
            trading_day = self.broker.get_trading_day()
            try:
                catalog_date = datetime.strptime(trading_day, "%Y%m%d").date()
            except (TypeError, ValueError) as exc:
                raise RuntimeError("directional CTP trading day is invalid") from exc
        allowed: list[ContractInfo] = []
        for item in catalog:
            if item.product.upper() not in products:
                continue
            if item.exchange.upper() not in exchanges:
                continue
            if item.listing:
                try:
                    if datetime.fromisoformat(item.listing).date() > catalog_date:
                        continue
                except ValueError:
                    continue
            if self.raw_tick_observer is not None:
                try:
                    if datetime.fromisoformat(item.expiry).date() < catalog_date:
                        continue
                except ValueError:
                    continue
            allowed.append(item)
        if not allowed:
            raise RuntimeError("directional contract catalog has no allowed products")
        self._catalog = allowed
        if self.raw_tick_observer is not None:
            self.raw_tick_observer.set_expected_contracts(
                trading_day,
                catalog,
            )
            self.broker.set_raw_tick_observer(self.raw_tick_observer)
        for item in allowed:
            self.broker.subscribe(item.symbol, item.exchange)
            self._subscribed_contracts.add((item.symbol, item.exchange))
        self._initialized = True
        if self.raw_tick_observer is not None and bool(
            getattr(self.broker, "metadata_query_blocks", False)
        ):
            self._subscription_thread = Thread(
                target=self._subscription_maintenance_loop,
                name="afuture-market-subscriptions",
                daemon=True,
            )
            self._subscription_thread.start()

    def close(self) -> None:
        try:
            if self.raw_tick_observer is not None:
                self._subscription_stop.set()
                if self._subscription_thread is not None:
                    self._subscription_thread.join(timeout=2.0)
                self.broker.set_raw_tick_observer(None)
                self.checkpoint_oi_evidence()
        finally:
            closer = getattr(self.signal_provider, "close", None)
            if callable(closer):
                closer()

    def checkpoint_oi_evidence(self) -> None:
        """Durably commit raw OI evidence only after a bounded broker batch."""
        if self.raw_tick_observer is not None:
            with self._subscription_lock:
                error = self._subscription_error
            if error:
                raise RuntimeError(f"market subscription maintenance failed: {error}")
            self.raw_tick_observer.checkpoint()

    def _subscription_maintenance_loop(self) -> None:
        """Refresh catalog subscriptions outside run_once and every Broker callback."""

        while not self._subscription_stop.wait(5.0):
            try:
                self._maintain_market_subscriptions_once()
            except Exception as exc:
                with self._subscription_lock:
                    self._subscription_error = str(exc)

    def _maintain_market_subscriptions_once(self) -> None:
        if self.raw_tick_observer is None or not self._initialized:
            return
        trading_day = self.broker.get_trading_day()
        try:
            catalog_date = datetime.strptime(trading_day, "%Y%m%d").date()
        except (TypeError, ValueError) as exc:
            raise RuntimeError("directional CTP trading day is invalid") from exc
        refresh_broker_catalog = getattr(self.broker, "refresh_contract_catalog", None)
        if callable(refresh_broker_catalog):
            try:
                refresh_broker_catalog(timeout_seconds=self.metadata_timeout_seconds)
            except Exception:
                verified_for_day = getattr(
                    self.broker,
                    "contract_catalog_verified_for_day",
                    None,
                )
                if not callable(verified_for_day) or not verified_for_day(trading_day):
                    raise
        catalog = self.broker.get_contract_catalog()
        if not catalog:
            raise RuntimeError("directional contract catalog is empty")
        products = {item.upper() for item in self.config.products}
        exchanges = {item.upper() for item in self.config.exchanges}
        allowed: list[ContractInfo] = []
        for item in catalog:
            if item.product.upper() not in products or item.exchange.upper() not in exchanges:
                continue
            try:
                expiry = datetime.fromisoformat(item.expiry).date()
                listing = datetime.fromisoformat(item.listing).date() if item.listing else None
            except ValueError:
                continue
            if expiry < catalog_date or (listing is not None and listing > catalog_date):
                continue
            allowed.append(item)
        if not allowed:
            raise RuntimeError("directional contract catalog has no allowed products")
        refresh = getattr(self.raw_tick_observer, "refresh_contract_catalog", None)
        if callable(refresh):
            refresh(trading_day, catalog)
        with self._subscription_lock:
            newly_subscribed = [
                item
                for item in allowed
                if (item.symbol, item.exchange) not in self._subscribed_contracts
            ]
        for item in newly_subscribed:
            self.broker.subscribe(item.symbol, item.exchange)
        note_subscriptions = getattr(self.raw_tick_observer, "note_contract_subscriptions", None)
        if newly_subscribed and callable(note_subscriptions):
            note_subscriptions(trading_day, tuple(item.symbol for item in newly_subscribed))
        with self._subscription_lock:
            self._subscribed_contracts.update(
                (item.symbol, item.exchange) for item in newly_subscribed
            )
            self._catalog = allowed
            if hasattr(self, "_catalog_by_symbol"):
                self._catalog_by_symbol = {item.symbol: item for item in allowed}
            self._subscription_error = ""

    def observe(self, tick: Tick) -> None:
        if not self._initialized:
            return
        allowed = {item.symbol for item in self._catalog}
        if tick.symbol in allowed:
            self._ticks[tick.symbol] = tick

    def maybe_rebalance(self, now: datetime) -> DirectionalActionResult:
        """Generic/test lifecycle; production execution-aligned mode overrides selection."""
        if not self._initialized:
            return DirectionalActionResult("reject", "directional manager is not initialized")
        if not self.broker.is_ready():
            return DirectionalActionResult("reject", "broker is not ready")
        if not self._inside_rebalance_window(now):
            return DirectionalActionResult("hold", "outside directional rebalance window")
        if self.broker.get_active_orders():
            return DirectionalActionResult("wait", "active orders must settle before rebalance")
        self._finalize_quality_cycle_if_settled(now)

        try:
            signal = self._load_signal(now)
            target_weights = self._next_target_weights(signal)
        except Exception as exc:
            return DirectionalActionResult("reject", f"directional signal unavailable: {exc}")

        local_date = self._local(now).date()
        selected = self.selector.select(self._catalog, self._ticks, local_date)
        required_products = {
            product.upper()
            for product, weight in target_weights.items()
            if abs(float(weight)) > 1e-15
        }
        missing = sorted(required_products - set(selected))
        if missing:
            return DirectionalActionResult(
                "reject", f"no eligible concrete contract for products: {missing}"
            )

        positions = self.broker.get_positions()
        symbols = {item.symbol for item in positions if not item.empty} | {
            selected[product].symbol for product in required_products
        }
        try:
            specs = self._ensure_specs(symbols)
        except Exception as exc:
            return DirectionalActionResult("reject", f"directional metadata unavailable: {exc}")

        product_ticks = {
            product: self._ticks[selected[product].symbol] for product in required_products
        }
        target_lots = build_target_lots(
            self.broker.get_account(),
            {product: target_weights[product] for product in required_products},
            product_ticks,
            specs,
            max_contract_volume=min(
                self.config.max_contract_volume,
                self.risk_manager.config.max_contract_volume,
            ),
        )
        plan = build_rebalance_plan(positions, target_lots)
        if plan.reductions:
            return self._submit_reductions(
                positions,
                plan.reductions,
                now,
                reference="directional:rebalance",
            )
        if not plan.openings:
            return DirectionalActionResult("hold", "directional portfolio is at target")
        return self._submit_openings(positions, plan.openings, selected, specs, now)

    def flatten(self, now: datetime) -> DirectionalActionResult:
        if self.broker.get_active_orders():
            return DirectionalActionResult("wait", "active orders must settle before flatten")
        self._finalize_quality_cycle_if_settled(now)
        positions = self.broker.get_positions()
        long_reductions = {
            position.symbol: -int(position.long_total)
            for position in positions
            if position.long_total > 0
        }
        short_reductions = {
            position.symbol: int(position.short_total)
            for position in positions
            if position.short_total > 0
        }
        if not long_reductions and not short_reductions:
            return DirectionalActionResult("hold", "directional portfolio is flat")

        order_ids: list[str] = []
        for reductions in (long_reductions, short_reductions):
            if not reductions:
                continue
            result = self._submit_reductions(
                positions,
                reductions,
                now,
                reference="directional:flatten",
            )
            order_ids.extend(result.order_ids)
            if result.action != "reduce":
                return DirectionalActionResult(
                    result.action,
                    result.reason,
                    tuple(order_ids),
                )
        return DirectionalActionResult("reduce", order_ids=tuple(order_ids))

    def enforce_realized_gross_limit(self, now: datetime) -> DirectionalActionResult:
        """Reduce broker-truth positions only after marked gross exceeds the hard cap."""
        if not self._initialized:
            return DirectionalActionResult("reject", "directional manager is not initialized")
        if not self.broker.is_ready():
            return DirectionalActionResult("reject", "broker is not ready")
        if self.broker.get_active_orders():
            return DirectionalActionResult(
                "wait", "active orders must settle before realized gross guard"
            )

        positions = [position for position in self.broker.get_positions() if not position.empty]
        if not positions:
            return DirectionalActionResult("hold", "directional portfolio is flat")
        symbols = {position.symbol for position in positions}
        try:
            specs = self._ensure_specs(symbols)
            account = self.broker.get_account()
        except Exception as exc:
            return DirectionalActionResult("reject", f"realized gross guard unavailable: {exc}")
        if account.equity <= 0:
            return DirectionalActionResult(
                "reject", "realized gross guard requires positive equity"
            )

        current_lots: dict[str, int] = {}
        lot_notionals: dict[str, float] = {}
        for position in positions:
            if position.long_total > 0 and position.short_total > 0:
                return DirectionalActionResult(
                    "reject",
                    f"realized gross guard cannot net opposing position: {position.symbol}",
                )
            tick = self._ticks.get(position.symbol)
            if tick is None:
                return DirectionalActionResult(
                    "wait", f"realized gross guard awaits quote: {position.symbol}"
                )
            spec = specs[position.symbol]
            lot_notional = float(tick.mid_price) * float(spec.multiplier)
            if lot_notional <= 0:
                return DirectionalActionResult(
                    "reject", f"realized gross guard invalid quote: {position.symbol}"
                )
            current_lots[position.symbol] = int(position.net_volume)
            lot_notionals[position.symbol] = lot_notional

        try:
            reductions = build_realized_gross_reductions(
                current_lots,
                lot_notionals,
                equity=float(account.equity),
                max_gross_ratio=float(self.config.max_gross_leverage),
            )
        except ValueError as exc:
            return DirectionalActionResult("reject", str(exc))
        if not reductions:
            return DirectionalActionResult("hold", "realized gross is within directional limit")
        return self._submit_reductions(
            positions,
            reductions,
            now,
            reference="directional:gross-guard",
        )

    def has_risk(self) -> bool:
        return any(not position.empty for position in self.broker.get_positions())

    def required_symbols(self) -> set[str]:
        symbols = {
            position.symbol for position in self.broker.get_positions() if not position.empty
        }
        for order in self.broker.get_active_orders():
            if order.request.reference.startswith("directional:"):
                symbols.add(order.request.symbol)
        return symbols

    def _load_signal(self, now: datetime) -> pd.DataFrame:
        if self.signal_provider is None:
            raise RuntimeError("directional signal provider is not configured")
        local = self._local(now)
        if self._signal_frame is None or self._signal_refresh_date != local.date():
            frame = self.signal_provider.load(
                tuple(item.upper() for item in self.config.products)
            ).copy()
            frame.index = pd.to_datetime(frame.index, errors="coerce")
            frame = frame[~frame.index.isna()].sort_index()
            frame.columns = [str(item).upper() for item in frame.columns]
            frame = frame.loc[frame.index.normalize() <= pd.Timestamp(local.date())]
            frame = frame.dropna(how="all")
            if len(frame) < 140:
                raise RuntimeError("directional signal history is shorter than 140 days")
            latest = pd.Timestamp(frame.index[-1]).to_pydatetime().replace(tzinfo=_CHINA_TZ)
            age_hours = (local - latest).total_seconds() / 3600.0
            if age_hours < -1:
                raise RuntimeError("directional signal history is from the future")
            if age_hours > self.config.signal_max_age_hours:
                raise RuntimeError(f"directional signal history is stale by {age_hours:.1f}h")
            self._signal_frame = frame
            self._signal_refresh_date = local.date()
        return self._signal_frame.copy()

    def _next_target_weights(self, close: pd.DataFrame) -> dict[str, float]:
        if self.policy is None:
            raise RuntimeError("directional policy is not configured")
        last = pd.Timestamp(close.index[-1])
        synthetic_index = last + pd.offsets.BDay(1)
        synthetic = close.iloc[[-1]].copy()
        synthetic.index = pd.DatetimeIndex([synthetic_index])
        weights = self.policy.target_weights(pd.concat([close, synthetic]))
        gross = sum(abs(float(value)) for value in weights.values())
        if gross > self.config.max_gross_leverage + 1e-10:
            raise RuntimeError(f"directional signal exceeds configured gross leverage: {gross:.6f}")
        return {str(key).upper(): float(value) for key, value in weights.items()}

    def _ensure_specs(self, symbols: set[str]) -> dict[str, ContractSpec]:
        missing = sorted(symbol for symbol in symbols if symbol not in self._specs)
        if missing:
            getter = getattr(self.broker, "get_live_contract_specs", None)
            if getter is None:
                raise RuntimeError(f"missing contract metadata: {missing}")
            self._specs.update(getter(missing, timeout_seconds=self.metadata_timeout_seconds))
        still_missing = sorted(symbol for symbol in symbols if symbol not in self._specs)
        if still_missing:
            raise RuntimeError(f"missing contract metadata: {still_missing}")
        return {symbol: self._specs[symbol] for symbol in symbols}

    def _submit_reductions(
        self,
        positions: list[ContractPosition],
        reductions: Mapping[str, int],
        now: datetime,
        *,
        reference: str,
    ) -> DirectionalActionResult:
        book = PositionBook(positions)
        position_map = {item.symbol: item for item in positions}
        order_ids: list[str] = []
        for symbol, delta in sorted(reductions.items()):
            position = position_map.get(symbol)
            tick = self._ticks.get(symbol)
            spec = self._specs.get(symbol)
            if position is None or tick is None:
                return DirectionalActionResult(
                    "reject", f"missing reduction quote/position for {symbol}"
                )
            if spec is None:
                try:
                    spec = self._ensure_specs({symbol})[symbol]
                except Exception as exc:
                    return DirectionalActionResult("reject", str(exc))
            quote = self.risk_manager.check_quotes([tick], now)
            if not quote.allowed:
                return DirectionalActionResult("reject", quote.reason)
            side = OrderSide.BUY if delta > 0 else OrderSide.SELL
            try:
                children = book.plan_close(
                    symbol,
                    position.exchange,
                    side,
                    abs(int(delta)),
                    close_today_first=self.close_today_first,
                    price=self._aggressive_price(tick, spec, side),
                    reference=reference,
                )
            except ValueError as exc:
                return DirectionalActionResult("reject", str(exc))
            for child in children:
                if not self.rate_limiter.allow(now.timestamp()):
                    return DirectionalActionResult("reject", "order rate limit reached")
                request = replace(child, order_type=OrderType.FAK)
                try:
                    order_id = self.broker.send_order(request)
                    order_ids.append(order_id)
                    self._register_quality_order(order_id, request, spec, now)
                except Exception as exc:
                    return DirectionalActionResult(
                        "reject",
                        f"directional reduction submission failed: {exc}",
                        tuple(order_ids),
                    )
        return DirectionalActionResult("reduce", order_ids=tuple(order_ids))

    def _submit_openings(
        self,
        positions: list[ContractPosition],
        openings: Mapping[str, int],
        selected: Mapping[str, ContractInfo],
        specs: Mapping[str, ContractSpec],
        now: datetime,
        *,
        reference_prefix: str | None = None,
    ) -> DirectionalActionResult:
        selected_by_symbol = {item.symbol: item for item in selected.values()}
        requests: list[OrderRequest] = []
        for symbol, delta in sorted(openings.items()):
            item = selected_by_symbol.get(symbol)
            tick = self._ticks.get(symbol)
            spec = specs.get(symbol)
            if item is None or tick is None or spec is None:
                return DirectionalActionResult(
                    "reject", f"missing opening quote/metadata for {symbol}"
                )
            side = OrderSide.BUY if delta > 0 else OrderSide.SELL
            volume = abs(int(delta))
            quote = self.risk_manager.check_quotes([tick], now)
            if not quote.allowed:
                return DirectionalActionResult("reject", quote.reason)
            market = self.risk_manager.check_contract_entry(
                tick,
                side,
                requested_volume=volume,
                spec=spec,
                session_windows=self._entry_session_windows(item.product),
            )
            if not market.allowed:
                return DirectionalActionResult("reject", market.reason)
            requests.append(
                OrderRequest(
                    symbol=symbol,
                    exchange=item.exchange,
                    side=side,
                    offset=Offset.OPEN,
                    volume=volume,
                    price=depth_aware_opening_price(
                        tick,
                        spec,
                        side,
                        requested_volume=volume,
                        aggressive_ticks=self.aggressive_ticks,
                    ),
                    order_type=OrderType.FAK,
                    reference=(
                        f"directional:{item.product.upper()}"
                        if reference_prefix is None
                        else f"{reference_prefix}:{item.product.upper()}"
                    ),
                )
            )

        current_volumes = {
            position.symbol: position.long_total + position.short_total for position in positions
        }
        account = self.broker.get_account()
        policy_rejection = self._opening_policy_rejection(
            account,
            positions,
            requests,
            specs,
            now,
        )
        if policy_rejection:
            return DirectionalActionResult("reject", policy_rejection)
        account_decision = self.risk_manager.check_open_orders(
            account,
            requests,
            dict(specs),
            current_contract_volumes=current_volumes,
        )
        if not account_decision.allowed:
            return DirectionalActionResult("reject", account_decision.reason)

        authorize_candidate = getattr(
            self.broker,
            "authorize_candidate_order_requests",
            None,
        )
        if callable(authorize_candidate):
            try:
                authorize_candidate(tuple(requests))
            except Exception as exc:
                return DirectionalActionResult(
                    "reject",
                    f"directional exact opening authorization failed: {exc}",
                )

        order_ids: list[str] = []
        for request in requests:
            if not self.rate_limiter.allow(now.timestamp()):
                return DirectionalActionResult(
                    "reject", "order rate limit reached", tuple(order_ids)
                )
            try:
                order_id = self.broker.send_order(request)
                order_ids.append(order_id)
                self._register_quality_order(order_id, request, specs[request.symbol], now)
            except Exception as exc:
                return DirectionalActionResult(
                    "reject",
                    f"directional opening submission failed: {exc}",
                    tuple(order_ids),
                )
        return DirectionalActionResult("open", order_ids=tuple(order_ids))

    def _opening_policy_rejection(
        self,
        account,
        positions: list[ContractPosition],
        requests: list[OrderRequest],
        specs: Mapping[str, ContractSpec],
        now: datetime,
    ) -> str:
        """Optional strategy-specific check immediately before the hard account gate."""

        del account, positions, requests, specs, now
        return ""

    def _entry_session_windows(self, product: str) -> tuple[str, ...]:
        del product
        return (self.config.rebalance_window,)

    def _start_quality_cycle(
        self,
        now: datetime,
        *,
        signal_day: str,
        activity_day: str,
        target_gross: float,
        target_lots: Mapping[str, int],
        reductions: Mapping[str, int],
        openings: Mapping[str, int],
        planned_turnover_notional: float,
        reason: str,
    ) -> str:
        self._quality_cycle_seq += 1
        cycle_id = f"directional-{self._quality_cycle_seq}"
        if self.quality is None:
            return cycle_id
        self._quality_cycle = {
            "cycle_id": cycle_id,
            "started": now,
            "target_lots": {str(k): int(v) for k, v in target_lots.items()},
            "order_ids": set(),
            "partial_ids": set(),
            "rejected_ids": set(),
            "realized_turnover_notional": 0.0,
        }
        self.quality.record_directional_rebalance(
            cycle_id=cycle_id,
            signal_day=signal_day,
            activity_day=activity_day,
            target_gross=target_gross,
            target_lots=dict(target_lots),
            reductions=dict(reductions),
            openings=dict(openings),
            planned_turnover_notional=planned_turnover_notional,
            reason=reason,
        )
        return cycle_id

    def _register_quality_order(
        self,
        order_id: str,
        request: OrderRequest,
        spec: ContractSpec,
        now: datetime,
    ) -> None:
        if self.quality is None or self._quality_cycle is None:
            return
        cycle_id = str(self._quality_cycle["cycle_id"])
        product = str(request.reference).split(":", 1)[1] if ":" in request.reference else ""
        contract = getattr(self, "_catalog_by_symbol", {}).get(request.symbol)
        if contract is not None:
            product = contract.product.upper()
        expected_windows = self._entry_session_windows(product) if product else ()
        self._quality_expectations[order_id] = {
            "cycle_id": cycle_id,
            "product": product,
            "symbol": request.symbol,
            "side": request.side,
            "offset": request.offset,
            "expected_price": float(request.price),
            "volume": int(request.volume),
            "multiplier": float(spec.multiplier),
            "submitted": now,
        }
        self._quality_cycle["order_ids"].add(order_id)
        self.quality.record_directional_order_plan(
            cycle_id=cycle_id,
            order_id=order_id,
            product=product,
            symbol=request.symbol,
            side=request.side.value,
            offset=request.offset.value,
            volume=request.volume,
            expected_open_window=",".join(expected_windows),
            planned_order_price=request.price,
            planning_timestamp=now.isoformat(),
        )

    def directional_order_expectation(self, order_id: str) -> dict | None:
        item = self._quality_expectations.get(order_id)
        return dict(item) if item is not None else None

    def note_directional_quality_fill(
        self,
        trade: Trade,
        *,
        commission: float,
        commission_source: str,
    ) -> None:
        if self.quality is None:
            return
        expected = self._quality_expectations.get(trade.order_id)
        if expected is None:
            return
        expected_price = float(expected["expected_price"])
        if expected_price <= 0:
            slippage_bps = 0.0
        elif trade.side is OrderSide.BUY:
            slippage_bps = (float(trade.price) - expected_price) / expected_price * 10000.0
        else:
            slippage_bps = (expected_price - float(trade.price)) / expected_price * 10000.0
        multiplier = float(expected["multiplier"])
        fill_notional = abs(float(trade.price) * int(trade.volume) * multiplier)
        self.quality.record_directional_fill(
            cycle_id=str(expected["cycle_id"]),
            order_id=trade.order_id,
            product=str(expected["product"]),
            symbol=trade.symbol,
            side=trade.side.value,
            offset=trade.offset.value,
            expected_price=expected_price,
            fill_price=float(trade.price),
            volume=int(trade.volume),
            multiplier=multiplier,
            slippage_bps=slippage_bps,
            commission=float(commission),
            commission_source=commission_source,
            fill_notional=fill_notional,
            actual_fill_timestamp=trade.timestamp.isoformat(),
        )
        if self._quality_cycle and self._quality_cycle["cycle_id"] == expected["cycle_id"]:
            self._quality_cycle["realized_turnover_notional"] += fill_notional

    def note_directional_quality_order(self, order: Order) -> None:
        if self.quality is None or self._quality_cycle is None:
            return
        if order.order_id not in self._quality_cycle["order_ids"]:
            return
        if order.status is OrderStatus.PART_TRADED or (
            order.status is OrderStatus.CANCELLED and int(order.traded) > 0
        ):
            self._quality_cycle["partial_ids"].add(order.order_id)
        if order.status is OrderStatus.REJECTED:
            self._quality_cycle["rejected_ids"].add(order.order_id)

    def _finalize_quality_cycle_if_settled(self, now: datetime) -> None:
        if self.quality is None or self._quality_cycle is None:
            return
        active_ids = {
            order.order_id
            for order in self.broker.get_active_orders()
            if order.request.reference.startswith("directional:")
        }
        if active_ids & set(self._quality_cycle["order_ids"]):
            return
        actual = {
            position.symbol: int(position.net_volume)
            for position in self.broker.get_positions()
            if not position.empty
        }
        target = dict(self._quality_cycle["target_lots"])
        symbols = set(actual) | set(target)
        error = sum(abs(actual.get(symbol, 0) - target.get(symbol, 0)) for symbol in symbols)
        scale = max(1, sum(abs(int(value)) for value in target.values()))
        tracking_error = float(error / scale)
        latency_ms = max(
            0.0,
            (now - self._quality_cycle["started"]).total_seconds() * 1000.0,
        )
        cycle_id = str(self._quality_cycle["cycle_id"])
        self.quality.record_directional_cycle(
            cycle_id=cycle_id,
            target_tracking_error=tracking_error,
            completion_latency_ms=latency_ms,
            partial_count=len(self._quality_cycle["partial_ids"]),
            rejected_count=len(self._quality_cycle["rejected_ids"]),
            realized_turnover_notional=float(self._quality_cycle["realized_turnover_notional"]),
        )
        for order_id in list(self._quality_expectations):
            if self._quality_expectations[order_id].get("cycle_id") == cycle_id:
                self._quality_expectations.pop(order_id, None)
        self._quality_cycle = None

    def _planned_turnover_notional(self, deltas: Mapping[str, int]) -> float:
        total = 0.0
        for symbol, delta in deltas.items():
            tick = self._ticks.get(symbol)
            spec = self._specs.get(symbol)
            if tick is None or spec is None:
                continue
            total += abs(int(delta)) * float(tick.last_price) * float(spec.multiplier)
        return float(total)

    def _inside_rebalance_window(self, timestamp: datetime) -> bool:
        local_time = self._local(timestamp).timetz().replace(tzinfo=None)
        start_raw, end_raw = self.config.rebalance_window.split("-", 1)
        start = time.fromisoformat(start_raw)
        end = time.fromisoformat(end_raw)
        if start <= end:
            return start <= local_time <= end
        return local_time >= start or local_time <= end

    def _aggressive_price(self, tick: Tick, spec: ContractSpec, side: OrderSide) -> float:
        if side is OrderSide.BUY:
            price = tick.ask_price + self.aggressive_ticks * spec.price_tick
            return min(price, tick.limit_up) if tick.limit_up > 0 else price
        price = tick.bid_price - self.aggressive_ticks * spec.price_tick
        return max(price, tick.limit_down) if tick.limit_down > 0 else price

    @staticmethod
    def _local(timestamp: datetime) -> datetime:
        if timestamp.tzinfo is None:
            return timestamp.replace(tzinfo=_CHINA_TZ)
        return timestamp.astimezone(_CHINA_TZ)
