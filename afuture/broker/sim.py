"""确定性模拟柜台，支持普通和保守撮合模式。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from itertools import count
from math import ceil

from ..fees import calculate_commission
from ..models import (
    AccountSnapshot,
    BrokerEvent,
    ContractInfo,
    ContractPosition,
    ContractSpec,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Tick,
    Trade,
)
from ..position import PositionBook
from .base import Broker


class SimBroker(Broker):
    """按一档盘口撮合，并可启用深度消耗、延迟和市场冲击的保守模式。"""

    def __init__(
        self,
        initial_capital: float,
        specs: dict[str, ContractSpec],
        *,
        slippage_ticks: int = 0,
        conservative: bool = False,
        latency_ticks: int = 0,
        market_impact_ticks: int = 0,
        depth_haircut: float = 1.0,
        size_impact_ticks: int = 0,
        contract_catalog: list[ContractInfo] | None = None,
    ) -> None:
        if initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if not 0.0 < float(depth_haircut) <= 1.0:
            raise ValueError("depth_haircut must be in (0, 1]")
        self.initial_capital = initial_capital
        self.specs = specs
        self.slippage_ticks = max(0, slippage_ticks)
        self.conservative = conservative
        self.latency_ticks = max(0, latency_ticks)
        self.market_impact_ticks = max(0, market_impact_ticks)
        self.depth_haircut = float(depth_haircut)
        self.size_impact_ticks = max(0, int(size_impact_ticks))
        self._contract_catalog = list(contract_catalog or [])
        self._contract_catalog_by_symbol = {
            contract.symbol: contract for contract in self._contract_catalog
        }
        self.position_book = PositionBook()
        self._orders: dict[str, Order] = {}
        self._trades: list[Trade] = []
        self._ticks: dict[str, Tick] = {}
        self._events: list[BrokerEvent] = []
        self._order_seq = count(1)
        self._trade_seq = count(1)
        self._started = False
        self._balance = initial_capital
        self._realized_pnl = 0.0
        self._commission = 0.0
        self._trading_day = ""
        self._tick_seq = 0
        self._eligible_seq: dict[str, int] = {}
        self._depth: dict[str, list[int]] = {}
        self._order_arrival: dict[str, tuple[float, float, int]] = {}
        self._first_fill_seq: dict[str, int] = {}

    def start(self) -> None:
        self._started = True

    def stop(self) -> None:
        self._started = False

    def is_ready(self) -> bool:
        return self._started

    def subscribe(self, symbol: str, exchange: str) -> None:
        if symbol not in self.specs:
            raise KeyError(f"unknown contract: {symbol}")

    def get_live_contract_specs(
        self, symbols: list[str], timeout_seconds: float = 10.0
    ) -> dict[str, ContractSpec]:
        """模拟柜台直接返回本地参数，便于测试元数据安全门。"""
        return {symbol: self.specs[symbol] for symbol in symbols}

    def get_contract_catalog(self) -> list[ContractInfo]:
        """历史回放只暴露当日已经挂牌的合约，实盘空 listing 语义保持不变。"""
        if not self._trading_day:
            return list(self._contract_catalog)
        try:
            today = datetime.strptime(self._trading_day, "%Y%m%d").date()
        except ValueError:
            return list(self._contract_catalog)

        visible: list[ContractInfo] = []
        for item in self._contract_catalog:
            if item.listing:
                try:
                    listing = date.fromisoformat(item.listing)
                except ValueError:
                    # 历史可见性元数据损坏时 fail-closed，避免未来合约泄漏进 Universe。
                    continue
                if listing > today:
                    continue
            visible.append(item)
        return visible

    def _displayed_depth(self, volume: float) -> int:
        raw = max(0, int(volume))
        if not self.conservative:
            return raw
        return max(0, int(raw * self.depth_haircut))

    def publish_tick(self, tick: Tick) -> None:
        tick.validate()
        self._notify_raw_tick_observer(
            tick,
            self._contract_catalog_by_symbol.get(tick.symbol),
        )
        self._tick_seq += 1
        if self._trading_day and tick.trading_day != self._trading_day:
            self.position_book.roll_trading_day()
        self._trading_day = tick.trading_day
        self._ticks[tick.symbol] = tick
        # 每个新 Tick 只有一份一档深度；同一 Tick 内的多个订单共享并消耗这份深度。
        # 保守模式可对显示深度做固定 haircut，用于模拟排队优先级和不可获得的队列份额。
        self._depth[tick.symbol] = [
            self._displayed_depth(tick.bid_volume),
            self._displayed_depth(tick.ask_volume),
        ]
        # 当前行情可能使上一轮延迟 FAK/FOK 首次具备成交资格。先产生这些成交/撤单
        # 回报，再把同一行情交给策略生成新决策，避免 broker 内部仓位已经变化而
        # TradingEngine 的期望状态尚未推进的时间倒置。
        self._match_symbol(tick.symbol)
        self._events.append(BrokerEvent("tick", tick))

    def send_order(self, request: OrderRequest) -> str:
        if not self._started:
            raise RuntimeError("sim broker is not started")
        if request.symbol not in self.specs:
            raise KeyError(f"unknown contract: {request.symbol}")
        if request.volume <= 0 or request.price <= 0:
            raise ValueError("invalid order request")

        order_id = f"SIM-{next(self._order_seq)}"
        order = Order(
            order_id=order_id,
            request=request,
            status=OrderStatus.NOT_TRADED,
        )
        self._orders[order_id] = order
        arrival = self._ticks.get(request.symbol)
        if arrival is not None:
            self._order_arrival[order_id] = (
                float(arrival.bid_price),
                float(arrival.ask_price),
                self._tick_seq,
            )
        self._events.append(BrokerEvent("order", order))
        self._eligible_seq[order_id] = self._tick_seq + self.latency_ticks

        if not self.conservative or self.latency_ticks == 0:
            self._match_order(order)
            self._cancel_ioc_remainder(order)
        return order_id

    def cancel_order(self, order_id: str) -> None:
        order = self._orders.get(order_id)
        if order and order.active:
            order.status = OrderStatus.CANCELLED
            self._events.append(BrokerEvent("order", order))

    def get_order(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    def get_active_orders(self) -> list[Order]:
        return [order for order in self._orders.values() if order.active]

    def get_trades(self) -> list[Trade]:
        return list(self._trades)

    def get_orders(self) -> list[Order]:
        return list(self._orders.values())

    def get_positions(self) -> list[ContractPosition]:
        return self.position_book.all()

    def get_execution_stress_summary(self) -> dict[str, float | int]:
        """Summarize observed matching friction without influencing broker behavior."""
        requested = sum(int(order.request.volume) for order in self._orders.values())
        filled = sum(int(order.traded) for order in self._orders.values())
        unfilled = max(0, requested - filled)
        turnover = 0.0
        spread_cost = 0.0
        slippage_impact_cost = 0.0
        commission_cost = 0.0
        latency_volume = 0.0
        latency_weight = 0
        for trade in self._trades:
            spec = self.specs[trade.symbol]
            notional_scale = float(trade.volume) * float(spec.multiplier)
            turnover += abs(float(trade.price)) * notional_scale
            commission_cost += float(trade.commission)
            order = self._orders.get(trade.order_id)
            arrival = self._order_arrival.get(trade.order_id)
            if order is None or arrival is None:
                continue
            bid, ask, submit_seq = arrival
            mid = (bid + ask) / 2.0
            if order.request.side is OrderSide.BUY:
                best = ask
                spread_cost += max(0.0, best - mid) * notional_scale
                slippage_impact_cost += (float(trade.price) - best) * notional_scale
            else:
                best = bid
                spread_cost += max(0.0, mid - best) * notional_scale
                slippage_impact_cost += (best - float(trade.price)) * notional_scale
            fill_seq = self._first_fill_seq.get(trade.order_id, submit_seq)
            latency_volume += max(0, fill_seq - submit_seq) * int(trade.volume)
            latency_weight += int(trade.volume)
        return {
            "order_count": len(self._orders),
            "trade_count": len(self._trades),
            "requested_volume": requested,
            "filled_volume": filled,
            "unfilled_volume": unfilled,
            "fill_ratio": float(filled / requested) if requested else 0.0,
            "turnover_notional": float(turnover),
            "spread_cost": float(spread_cost),
            "slippage_impact_cost": float(slippage_impact_cost),
            "commission_cost": float(commission_cost),
            "total_execution_cost": float(spread_cost + slippage_impact_cost + commission_cost),
            "volume_weighted_latency_ticks": float(latency_volume / latency_weight)
            if latency_weight
            else 0.0,
        }

    def get_account(self) -> AccountSnapshot:
        unrealized = 0.0
        margin = 0.0
        for position in self.position_book.all():
            spec = self.specs[position.symbol]
            tick = self._ticks.get(position.symbol)
            mark = (
                tick.last_price
                if tick is not None
                else max(position.long_price, position.short_price, 0.0)
            )
            unrealized += (mark - position.long_price) * position.long_total * spec.multiplier
            unrealized += (position.short_price - mark) * position.short_total * spec.multiplier
            margin += (
                mark
                * spec.multiplier
                * (
                    position.long_total * spec.margin_rate_long
                    + position.short_total * spec.margin_rate_short
                )
            )
        equity = self._balance + unrealized
        return AccountSnapshot(
            balance=self._balance,
            equity=equity,
            available=equity - margin,
            margin=margin,
            realized_pnl=self._realized_pnl - self._commission,
            unrealized_pnl=unrealized,
            trading_day=(self._trading_day or datetime.now(timezone.utc).strftime("%Y%m%d")),
        )

    def poll_events(self) -> list[BrokerEvent]:
        events = list(self._events)
        self._events.clear()
        return events

    def _match_symbol(self, symbol: str) -> None:
        for order in list(self._orders.values()):
            if not order.active or order.request.symbol != symbol:
                continue
            self._match_order(order)
            if self._tick_seq >= self._eligible_seq.get(order.order_id, 0):
                self._cancel_ioc_remainder(order)

    def _match_order(self, order: Order) -> None:
        tick = self._ticks.get(order.request.symbol)
        if tick is None or not order.active:
            return
        eligible = self._eligible_seq.get(order.order_id, 0)
        if self.conservative and self._tick_seq < eligible:
            return

        request = order.request
        depth = self._depth.setdefault(
            request.symbol,
            [
                self._displayed_depth(tick.bid_volume),
                self._displayed_depth(tick.ask_volume),
            ],
        )
        if request.side is OrderSide.BUY:
            marketable = request.price >= tick.ask_price
            available = depth[1]
            raw_price = tick.ask_price
            sign = 1
            depth_index = 1
        else:
            marketable = request.price <= tick.bid_price
            available = depth[0]
            raw_price = tick.bid_price
            sign = -1
            depth_index = 0
        if not marketable or available <= 0:
            return

        remaining = request.volume - order.traded
        if request.order_type is OrderType.FOK and available < remaining:
            return
        fill_volume = min(remaining, available)
        spec = self.specs[request.symbol]
        impact_ticks = self.market_impact_ticks if self.conservative else 0
        if self.conservative and self.size_impact_ticks > 0:
            # L1 cannot reveal deeper-book prices. Penalize orders that ask for more than
            # the queue-adjusted opposite depth by one declared tick per additional full
            # depth multiple. This changes price only; FAK fill quantity remains limited
            # by the actually available L1 queue above.
            extra_multiples = max(0, int(ceil(remaining / available)) - 1)
            impact_ticks += extra_multiples * self.size_impact_ticks
        fill_price = raw_price + sign * (self.slippage_ticks + impact_ticks) * spec.price_tick
        if request.side is OrderSide.BUY:
            fill_price = min(fill_price, float(request.price))
            if tick.limit_up > 0:
                fill_price = min(fill_price, tick.limit_up)
        else:
            fill_price = max(fill_price, float(request.price))
            if tick.limit_down > 0:
                fill_price = max(fill_price, tick.limit_down)

        depth[depth_index] -= fill_volume
        self._fill(order, fill_volume, fill_price)

    def _cancel_ioc_remainder(self, order: Order) -> None:
        if order.request.order_type in {OrderType.FAK, OrderType.FOK} and order.active:
            order.status = OrderStatus.CANCELLED
            self._events.append(BrokerEvent("order", order))

    def _fill(self, order: Order, volume: int, price: float) -> None:
        previous_traded = order.traded
        order.traded += volume
        order.average_price = (
            order.average_price * previous_traded + price * volume
        ) / order.traded
        order.status = (
            OrderStatus.FILLED if order.traded == order.request.volume else OrderStatus.PART_TRADED
        )
        self._first_fill_seq.setdefault(order.order_id, self._tick_seq)

        tick = self._ticks[order.request.symbol]
        trade = Trade(
            trade_id=f"SIM-T-{next(self._trade_seq)}",
            order_id=order.order_id,
            symbol=order.request.symbol,
            exchange=order.request.exchange,
            side=order.request.side,
            offset=order.request.offset,
            volume=volume,
            price=price,
            timestamp=tick.timestamp,
        )
        realized_points = self.position_book.apply_trade(trade)
        spec = self.specs[trade.symbol]
        realized = realized_points * spec.multiplier
        commission = calculate_commission(spec, trade.offset, trade.price, trade.volume)
        self._realized_pnl += realized
        self._commission += commission
        self._balance += realized - commission

        trade = Trade(**{**trade.__dict__, "commission": commission})
        self._trades.append(trade)
        self._events.extend([BrokerEvent("trade", trade), BrokerEvent("order", order)])
