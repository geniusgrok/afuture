"""双腿套利执行器。

开仓依次经过动态手数、盘口、净边际和组合保证金硬门；异常恢复路径只允许减仓。
"""

from __future__ import annotations

from dataclasses import replace

from .economics import estimate_net_edge
from .models import (
    ContractSpec,
    ExecutionResult,
    Offset,
    OrderRequest,
    OrderSide,
    OrderType,
    PairConfig,
    SignalAction,
    SpreadSignal,
    Tick,
)
from .position import PositionBook
from .risk import OrderRateLimiter, RiskManager


class PairExecutor:
    """把价差信号转换成双腿订单，并负责提交失败后的只减仓回滚。"""

    def __init__(
        self,
        broker,
        risk_manager: RiskManager,
        specs: dict[str, ContractSpec],
        *,
        aggressive_ticks: int = 1,
        slippage_ticks: int = 1,
        close_today_first: bool = False,
    ) -> None:
        self.broker = broker
        self.risk_manager = risk_manager
        self.specs = specs
        self.aggressive_ticks = max(0, aggressive_ticks)
        self.slippage_ticks = max(0, slippage_ticks)
        self.close_today_first = close_today_first
        self.rate_limiter = OrderRateLimiter(risk_manager.config.max_orders_per_minute)

    def execute_signal(
        self,
        pair: PairConfig,
        signal: SpreadSignal,
        near: Tick,
        far: Tick,
        *,
        open_pair_count: int,
        spread_std: float = 0.0,
        rate_limit_time: float | None = None,
    ) -> ExecutionResult:
        if signal.action is SignalAction.HOLD:
            return ExecutionResult(False, reason="hold signal")
        if not self.broker.is_ready():
            return ExecutionResult(False, reason="broker is not ready")

        quote_decision = self.risk_manager.check_quotes([near, far], signal.timestamp)
        if not quote_decision.allowed:
            return ExecutionResult(False, reason=quote_decision.reason)

        opening = signal.action in {
            SignalAction.LONG_SPREAD,
            SignalAction.SHORT_SPREAD,
        }
        calendar_decision = self.risk_manager.check_pair_calendar(
            pair, signal.timestamp, opening=opening
        )
        if not calendar_decision.allowed:
            return ExecutionResult(False, reason=calendar_decision.reason)

        if opening:
            volume, requests, rejection = self._prepare_open(
                pair,
                signal,
                near,
                far,
                open_pair_count=open_pair_count,
                spread_std=spread_std,
            )
            if rejection:
                return ExecutionResult(False, reason=rejection)
        else:
            requests = self._build_close_orders(pair, near, far)
            if not requests:
                return ExecutionResult(False, reason="pair has no matching position to close")
            volume = max(request.volume for request in requests)

        # 两腿属于同一批交易意图，必须使用同一个限速时钟值。信号时间来自已经
        # 通过行情时效/双腿同步校验的市场事件：历史回放因此不会把数月订单压缩
        # 到 CPU 的几秒钟；实盘若行情时间戳陈旧或跳变，则上游健康门会先失败关闭。
        limiter_now = (
            signal.timestamp.timestamp() if rate_limit_time is None else float(rate_limit_time)
        )
        requests = self._prioritize_requests(requests, near, far)
        order_ids: list[str] = []
        try:
            for request in requests:
                if not self.rate_limiter.allow(limiter_now):
                    raise RuntimeError("order rate limit reached")
                order_ids.append(self.broker.send_order(request))
        except Exception as exc:
            self._rollback_submitted(order_ids, near, far)
            return ExecutionResult(
                False,
                tuple(order_ids),
                f"pair submission failed: {exc}",
                volume,
            )
        return ExecutionResult(True, tuple(order_ids), volume=volume)

    def _prepare_open(
        self,
        pair: PairConfig,
        signal: SpreadSignal,
        near: Tick,
        far: Tick,
        *,
        open_pair_count: int,
        spread_std: float,
    ) -> tuple[int, list[OrderRequest], str]:
        account = self.broker.get_account()
        volume = self.risk_manager.size_pair(
            account,
            pair,
            self.specs,
            near,
            far,
            spread_std=max(spread_std, signal.reference_std, 1e-9),
        )
        if volume <= 0:
            return 0, [], "risk budget produced zero volume"

        market_decision = self.risk_manager.check_market_entry(
            pair,
            near,
            far,
            signal.action,
            volume,
            self.specs,
        )
        if not market_decision.allowed:
            return 0, [], market_decision.reason

        edge = estimate_net_edge(
            signal.action,
            reference_mean=signal.reference_mean,
            near=near,
            far=far,
            specs=self.specs,
            volume=volume,
            slippage_ticks=self.slippage_ticks,
            legging_buffer=pair.legging_buffer,
        )
        if edge.net_edge <= pair.min_net_edge:
            return (
                0,
                [],
                f"net edge is insufficient: {edge.net_edge:.2f}",
            )

        requests = self._build_open_orders(pair, signal.action, near, far, volume)
        current_volumes = {
            position.symbol: position.long_total + position.short_total
            for position in self.broker.get_positions()
        }
        batch_decision = self.risk_manager.check_open_batch(
            account,
            requests,
            self.specs,
            open_pair_count=open_pair_count,
            current_contract_volumes=current_volumes,
        )
        if not batch_decision.allowed:
            return 0, [], batch_decision.reason
        return volume, requests, ""

    def pair_is_balanced(self, pair: PairConfig) -> bool:
        """没有活动组合委托时，两腿必须等量且方向相反。"""
        active = [
            order
            for order in self.broker.get_active_orders()
            if order.request.reference.startswith(pair.pair_id)
        ]
        if active:
            return True

        book = PositionBook(self.broker.get_positions())
        near = book.get(pair.near_symbol, pair.exchange)
        far = book.get(pair.far_symbol, pair.exchange)
        long_spread = (
            near.long_total == far.short_total and near.short_total == 0 and far.long_total == 0
        )
        short_spread = (
            near.short_total == far.long_total and near.long_total == 0 and far.short_total == 0
        )
        return (near.empty and far.empty) or long_spread or short_spread

    def flatten_imbalance(self, pair: PairConfig, near: Tick, far: Tick) -> list[str]:
        """异常状态只发送减仓 FAK；普通报单限速不能阻止紧急减仓。"""
        book = PositionBook(self.broker.get_positions())
        order_ids: list[str] = []
        for symbol, tick in (
            (pair.near_symbol, near),
            (pair.far_symbol, far),
        ):
            position = book.get(symbol, pair.exchange)
            for side, volume in (
                (OrderSide.SELL, position.long_total),
                (OrderSide.BUY, position.short_total),
            ):
                if volume <= 0:
                    continue
                for child in book.plan_close(
                    symbol,
                    pair.exchange,
                    side,
                    volume,
                    close_today_first=self.close_today_first,
                    price=self._aggressive_price(tick, side),
                    reference=f"{pair.pair_id}:repair",
                ):
                    order_ids.append(
                        self.broker.send_order(replace(child, order_type=OrderType.FAK))
                    )
        return order_ids

    def _build_open_orders(
        self,
        pair: PairConfig,
        action: SignalAction,
        near: Tick,
        far: Tick,
        volume: int,
    ) -> list[OrderRequest]:
        if action is SignalAction.LONG_SPREAD:
            near_side, far_side = OrderSide.BUY, OrderSide.SELL
        else:
            near_side, far_side = OrderSide.SELL, OrderSide.BUY
        return [
            OrderRequest(
                pair.near_symbol,
                pair.exchange,
                near_side,
                Offset.OPEN,
                volume,
                self._aggressive_price(near, near_side),
                OrderType.FAK,
                pair.pair_id,
            ),
            OrderRequest(
                pair.far_symbol,
                pair.exchange,
                far_side,
                Offset.OPEN,
                volume,
                self._aggressive_price(far, far_side),
                OrderType.FAK,
                pair.pair_id,
            ),
        ]

    def _build_close_orders(self, pair: PairConfig, near: Tick, far: Tick) -> list[OrderRequest]:
        book = PositionBook(self.broker.get_positions())
        near_position = book.get(pair.near_symbol, pair.exchange)
        far_position = book.get(pair.far_symbol, pair.exchange)

        legs: list[tuple[str, Tick, OrderSide, int]] = []
        if near_position.long_total and far_position.short_total:
            volume = min(near_position.long_total, far_position.short_total)
            legs = [
                (pair.near_symbol, near, OrderSide.SELL, volume),
                (pair.far_symbol, far, OrderSide.BUY, volume),
            ]
        elif near_position.short_total and far_position.long_total:
            volume = min(near_position.short_total, far_position.long_total)
            legs = [
                (pair.near_symbol, near, OrderSide.BUY, volume),
                (pair.far_symbol, far, OrderSide.SELL, volume),
            ]

        requests: list[OrderRequest] = []
        for symbol, tick, side, volume in legs:
            children = book.plan_close(
                symbol,
                pair.exchange,
                side,
                volume,
                close_today_first=self.close_today_first,
                price=self._aggressive_price(tick, side),
                reference=pair.pair_id,
            )
            requests.extend(replace(child, order_type=OrderType.FAK) for child in children)
        return requests

    @staticmethod
    def _request_depth(request: OrderRequest, tick: Tick) -> float:
        return tick.ask_volume if request.side is OrderSide.BUY else tick.bid_volume

    def _prioritize_requests(
        self,
        requests: list[OrderRequest],
        near: Tick,
        far: Tick,
    ) -> list[OrderRequest]:
        tick_map = {near.symbol: near, far.symbol: far}
        # Python 排序稳定；深度相同则保留策略构造的原始腿顺序。
        return sorted(
            requests,
            key=lambda request: self._request_depth(request, tick_map[request.symbol]),
        )

    def _aggressive_price(self, tick: Tick, side: OrderSide) -> float:
        spec = self.specs[tick.symbol]
        if side is OrderSide.BUY:
            price = tick.ask_price + self.aggressive_ticks * spec.price_tick
            return min(price, tick.limit_up) if tick.limit_up > 0 else price
        price = tick.bid_price - self.aggressive_ticks * spec.price_tick
        return max(price, tick.limit_down) if tick.limit_down > 0 else price

    def _rollback_submitted(self, order_ids: list[str], near: Tick, far: Tick) -> None:
        """撤销未成交余量，并对已经成交的开仓腿发送只减仓回滚。"""
        for order_id in order_ids:
            self.broker.cancel_order(order_id)

        tick_map = {near.symbol: near, far.symbol: far}
        book = PositionBook(self.broker.get_positions())
        for order_id in order_ids:
            order = self.broker.get_order(order_id)
            if order is None or order.traded <= 0 or order.request.offset is not Offset.OPEN:
                continue
            side = OrderSide.SELL if order.request.side is OrderSide.BUY else OrderSide.BUY
            position = book.get(order.request.symbol, order.request.exchange)
            available = position.long_total if side is OrderSide.SELL else position.short_total
            volume = min(order.traded, available)
            if volume <= 0:
                continue
            for child in book.plan_close(
                order.request.symbol,
                order.request.exchange,
                side,
                volume,
                close_today_first=self.close_today_first,
                price=self._aggressive_price(tick_map[order.request.symbol], side),
                reference=f"{order.request.reference}:rollback",
            ):
                try:
                    self.broker.send_order(replace(child, order_type=OrderType.FAK))
                except Exception:
                    # 后续由引擎的失衡审计进入 REDUCE_ONLY，不在此处假装回滚成功。
                    pass
