"""持仓簿和交易所平今/平昨拆单规则。"""

from dataclasses import replace

from .models import ContractPosition, Offset, OrderRequest, OrderSide, Trade

_SPECIAL_CLOSE_EXCHANGES = {"SHFE", "INE"}


class PositionBook:
    """维护今昨、多空数量和持仓均价。"""

    def __init__(self, positions: list[ContractPosition] | None = None) -> None:
        self._positions: dict[str, ContractPosition] = {}
        for source in positions or []:
            position = replace(source)
            self._validate_position(position)
            if not position.empty:
                self._positions[position.symbol] = position

    def get(self, symbol: str, exchange: str = "") -> ContractPosition:
        if symbol not in self._positions:
            self._positions[symbol] = ContractPosition(symbol, exchange)
        return self._positions[symbol]

    def all(self) -> list[ContractPosition]:
        return [replace(position) for position in self._positions.values() if not position.empty]

    def roll_trading_day(self) -> None:
        """进入新交易日时把今仓转成昨仓。"""
        for position in self._positions.values():
            position.long_yesterday += position.long_today
            position.long_today = 0
            position.short_yesterday += position.short_today
            position.short_today = 0
            self._validate_position(position)

    def apply_trade(self, trade: Trade) -> float:
        """应用成交并返回价格点口径的已实现盈亏。"""
        trade.validate()
        position = self.get(trade.symbol, trade.exchange)

        if trade.offset is Offset.OPEN:
            self._apply_open(position, trade)
            self._validate_position(position)
            return 0.0

        self._validate_close_volume(position, trade)
        if trade.side is OrderSide.SELL:
            realized = (trade.price - position.long_price) * trade.volume
            self._consume_long(position, trade.offset, trade.volume)
            if position.long_total == 0:
                position.long_price = 0.0
        else:
            realized = (position.short_price - trade.price) * trade.volume
            self._consume_short(position, trade.offset, trade.volume)
            if position.short_total == 0:
                position.short_price = 0.0
        self._validate_position(position)
        return realized

    @staticmethod
    def _validate_position(position: ContractPosition) -> None:
        position.validate()

    @staticmethod
    def _validate_close_volume(
        position: ContractPosition,
        trade: Trade,
    ) -> None:
        """A close-today/close-yesterday fill cannot borrow another bucket."""
        if trade.side is OrderSide.SELL:
            total, today, yesterday, label = (
                position.long_total,
                position.long_today,
                position.long_yesterday,
                "long",
            )
        else:
            total, today, yesterday, label = (
                position.short_total,
                position.short_today,
                position.short_yesterday,
                "short",
            )
        available = {
            Offset.CLOSE: total,
            Offset.CLOSE_TODAY: today,
            Offset.CLOSE_YESTERDAY: yesterday,
        }[trade.offset]
        bucket = {
            Offset.CLOSE: "total",
            Offset.CLOSE_TODAY: "today",
            Offset.CLOSE_YESTERDAY: "yesterday",
        }[trade.offset]
        if trade.volume > available:
            raise ValueError(f"close volume exceeds {bucket} {label} position")

    @staticmethod
    def _apply_open(position: ContractPosition, trade: Trade) -> None:
        if trade.side is OrderSide.BUY:
            total = position.long_total + trade.volume
            position.long_price = (
                position.long_price * position.long_total + trade.price * trade.volume
            ) / total
            position.long_today += trade.volume
        else:
            total = position.short_total + trade.volume
            position.short_price = (
                position.short_price * position.short_total + trade.price * trade.volume
            ) / total
            position.short_today += trade.volume

    def plan_close(
        self,
        symbol: str,
        exchange: str,
        side: OrderSide,
        volume: int,
        *,
        close_today_first: bool = False,
        price: float = 0.0,
        reference: str = "",
    ) -> list[OrderRequest]:
        """按交易所规则把目标平仓拆成合法的子订单。"""
        if isinstance(volume, bool) or not isinstance(volume, int) or volume <= 0:
            raise ValueError("close volume must be a positive integer")
        if not isinstance(side, OrderSide):
            raise ValueError("close side must be an OrderSide")
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("close symbol must be a non-empty string")
        if not isinstance(exchange, str) or not exchange.strip():
            raise ValueError("close exchange must be a non-empty string")
        position = self.get(symbol, exchange)
        if side is OrderSide.SELL:
            today = position.long_today
            yesterday = position.long_yesterday
        else:
            today = position.short_today
            yesterday = position.short_yesterday

        if volume > today + yesterday:
            raise ValueError("close volume exceeds position")
        if exchange not in _SPECIAL_CLOSE_EXCHANGES:
            return [
                OrderRequest(
                    symbol,
                    exchange,
                    side,
                    Offset.CLOSE,
                    volume,
                    price,
                    reference=reference,
                )
            ]

        if close_today_first:
            buckets = [
                (Offset.CLOSE_TODAY, today),
                (Offset.CLOSE_YESTERDAY, yesterday),
            ]
        else:
            buckets = [
                (Offset.CLOSE_YESTERDAY, yesterday),
                (Offset.CLOSE_TODAY, today),
            ]

        orders: list[OrderRequest] = []
        remaining = volume
        for offset, available in buckets:
            child_volume = min(remaining, available)
            if child_volume:
                orders.append(
                    OrderRequest(
                        symbol,
                        exchange,
                        side,
                        offset,
                        child_volume,
                        price,
                        reference=reference,
                    )
                )
                remaining -= child_volume
        return orders

    @staticmethod
    def _consume_long(
        position: ContractPosition,
        offset: Offset,
        volume: int,
    ) -> None:
        if offset is Offset.CLOSE_TODAY:
            position.long_today -= volume
            return
        if offset is Offset.CLOSE_YESTERDAY:
            position.long_yesterday -= volume
            return
        from_yesterday = min(volume, position.long_yesterday)
        position.long_yesterday -= from_yesterday
        position.long_today -= volume - from_yesterday

    @staticmethod
    def _consume_short(
        position: ContractPosition,
        offset: Offset,
        volume: int,
    ) -> None:
        if offset is Offset.CLOSE_TODAY:
            position.short_today -= volume
            return
        if offset is Offset.CLOSE_YESTERDAY:
            position.short_yesterday -= volume
            return
        from_yesterday = min(volume, position.short_yesterday)
        position.short_yesterday -= from_yesterday
        position.short_today -= volume - from_yesterday
