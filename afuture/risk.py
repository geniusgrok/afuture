"""账户级、组合级和市场微观结构事前风险控制。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, time, timezone
from math import floor, isfinite
from zoneinfo import ZoneInfo

from .config_validation import require_finite_number, require_integer
from .models import (
    AccountSnapshot,
    ContractSpec,
    Offset,
    OrderRequest,
    OrderSide,
    PairConfig,
    RiskDecision,
    SignalAction,
    Tick,
)

_CHINA_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class RiskConfig:
    """默认参数偏保守，生产配置仍需按账户和品种校准。"""

    max_margin_ratio: float = 0.35
    max_daily_loss_ratio: float = 0.01
    max_total_drawdown_ratio: float = 0.08
    max_open_pairs: int = 3
    max_contract_volume: int = 10
    max_quote_age_seconds: float = 10.0
    max_leg_skew_seconds: float = 2.0
    expiry_blackout_days: int = 5
    min_available_ratio: float = 0.50
    margin_estimate_buffer: float = 1.20
    max_orders_per_minute: int = 20
    min_depth_multiple: float = 2.0
    max_bid_ask_ticks: float = 4.0
    limit_distance_ticks: float = 3.0
    risk_budget_ratio: float = 0.002
    risk_sigma_multiplier: float = 2.0
    open_cooldown_minutes: int = 0
    close_blackout_minutes: int = 0

    def validate(self) -> None:
        for field_name, value in self.__dict__.items():
            require_finite_number(value, f"risk.{field_name}")
        for field_name in (
            "max_open_pairs",
            "max_contract_volume",
            "expiry_blackout_days",
            "max_orders_per_minute",
            "open_cooldown_minutes",
            "close_blackout_minutes",
        ):
            require_integer(getattr(self, field_name), f"risk.{field_name}")
        ratios = (
            self.max_margin_ratio,
            self.max_daily_loss_ratio,
            self.max_total_drawdown_ratio,
            self.min_available_ratio,
            self.risk_budget_ratio,
        )
        if any(value <= 0 or value >= 1 for value in ratios):
            raise ValueError("risk ratios must be between 0 and 1")
        if self.margin_estimate_buffer < 1:
            raise ValueError("margin_estimate_buffer must be at least 1")
        if self.min_depth_multiple < 1:
            raise ValueError("min_depth_multiple must be at least 1")
        if self.risk_sigma_multiplier < 1:
            raise ValueError("risk_sigma_multiplier must be at least 1")
        if self.max_open_pairs <= 0 or self.max_contract_volume <= 0:
            raise ValueError("position limits must be positive")
        if self.max_quote_age_seconds <= 0:
            raise ValueError("max_quote_age_seconds must be positive")
        if self.max_leg_skew_seconds <= 0:
            raise ValueError("max_leg_skew_seconds must be positive")
        if self.max_orders_per_minute <= 0:
            raise ValueError("max_orders_per_minute must be positive")
        if self.expiry_blackout_days < 0:
            raise ValueError("expiry_blackout_days cannot be negative")
        if self.max_bid_ask_ticks <= 0:
            raise ValueError("max_bid_ask_ticks must be positive")
        if self.limit_distance_ticks < 0:
            raise ValueError("limit_distance_ticks cannot be negative")
        if self.open_cooldown_minutes < 0:
            raise ValueError("open_cooldown_minutes cannot be negative")
        if self.close_blackout_minutes < 0:
            raise ValueError("close_blackout_minutes cannot be negative")


class OrderRateLimiter:
    """限制单位时间内普通报单次数，避免策略异常形成报单风暴。"""

    def __init__(self, max_orders_per_minute: int) -> None:
        if max_orders_per_minute <= 0:
            raise ValueError("max_orders_per_minute must be positive")
        self.max_orders_per_minute = max_orders_per_minute
        self._timestamps: deque[float] = deque()

    def allow(self, now_monotonic: float) -> bool:
        cutoff = now_monotonic - 60.0
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()
        if len(self._timestamps) >= self.max_orders_per_minute:
            return False
        self._timestamps.append(now_monotonic)
        return True


class RiskManager:
    """决定是否允许交易和最大手数，不负责挑选套利机会。"""

    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()
        self._validate_config()
        self._day_start_equity: float | None = None
        self._trading_day = ""
        self._high_watermark: float | None = None

    @property
    def high_watermark(self) -> float:
        return self._high_watermark or 0.0

    def restore_high_watermark(self, equity: float) -> None:
        """从持久化状态恢复权益高水位，防止重启绕过总回撤限制。"""
        if isfinite(equity) and equity > 0:
            self._high_watermark = max(self._high_watermark or 0.0, equity)

    def set_day_start_equity(self, equity: float, trading_day: str) -> None:
        if not isfinite(equity) or equity <= 0:
            raise ValueError("equity must be finite and positive")
        self._day_start_equity = equity
        self._trading_day = trading_day
        self._high_watermark = max(self._high_watermark or equity, equity)

    def check_account(self, account: AccountSnapshot) -> RiskDecision:
        """检查日亏损、总回撤、保证金和现金储备。"""
        try:
            account.validate()
        except (TypeError, ValueError) as exc:
            return RiskDecision(False, f"invalid account snapshot: {exc}")
        if self._day_start_equity is None or self._trading_day != account.trading_day:
            self.set_day_start_equity(account.equity, account.trading_day)

        assert self._day_start_equity is not None
        self._high_watermark = max(self._high_watermark or account.equity, account.equity)
        daily_loss = max(0.0, self._day_start_equity - account.equity) / self._day_start_equity
        drawdown = max(0.0, self._high_watermark - account.equity) / self._high_watermark
        margin_ratio = account.margin / account.equity
        available_ratio = account.available / account.equity

        if daily_loss >= self.config.max_daily_loss_ratio:
            return RiskDecision(False, "daily loss limit reached")
        if drawdown >= self.config.max_total_drawdown_ratio:
            return RiskDecision(False, "drawdown limit reached")
        if margin_ratio > self.config.max_margin_ratio:
            return RiskDecision(False, "margin ratio limit reached")
        if available_ratio < self.config.min_available_ratio:
            return RiskDecision(False, "available cash reserve too low")
        return RiskDecision(True)

    def check_quotes(
        self,
        ticks: list[Tick],
        now: datetime | None = None,
    ) -> RiskDecision:
        """检查行情合法性、交易日一致性、时效性和跨腿时间差。"""
        if not ticks:
            return RiskDecision(False, "missing quotes")
        now = now or datetime.now(timezone.utc)
        if len({tick.trading_day for tick in ticks}) != 1:
            return RiskDecision(False, "quotes belong to different trading days")
        timestamps: list[datetime] = []
        for tick in ticks:
            try:
                tick.validate()
            except ValueError as exc:
                return RiskDecision(False, f"invalid quote: {exc}")
            timestamp = tick.timestamp.astimezone(timezone.utc)
            timestamps.append(timestamp)
            age = (now.astimezone(timezone.utc) - timestamp).total_seconds()
            if age < -2:
                return RiskDecision(False, "quote timestamp is in the future")
            if age > self.config.max_quote_age_seconds:
                return RiskDecision(False, "stale quote")
        if len(timestamps) > 1:
            skew = (max(timestamps) - min(timestamps)).total_seconds()
            if skew > self.config.max_leg_skew_seconds:
                return RiskDecision(False, "cross-leg quote skew limit exceeded")
        return RiskDecision(True)

    def check_pair_calendar(
        self,
        pair: PairConfig,
        now: datetime,
        *,
        opening: bool,
    ) -> RiskDecision:
        """按中国期货本地日期检查临近最后交易日的新风险。"""
        if not opening:
            return RiskDecision(True)
        local_date = self._china_timestamp(now).date()
        for raw in (pair.expiry_near, pair.expiry_far):
            if not raw:
                continue
            expiry = datetime.fromisoformat(raw).date()
            days = (expiry - local_date).days
            if days <= self.config.expiry_blackout_days:
                return RiskDecision(False, "contract is inside expiry blackout window")
        return RiskDecision(True)

    def check_market_entry(
        self,
        pair: PairConfig,
        near: Tick,
        far: Tick,
        action: SignalAction,
        requested_volume: int,
        specs: dict[str, ContractSpec] | None = None,
    ) -> RiskDecision:
        """开仓前检查交易时段、盘口宽度、深度和涨跌停距离。"""
        if action not in {
            SignalAction.LONG_SPREAD,
            SignalAction.SHORT_SPREAD,
        }:
            return RiskDecision(True)
        if requested_volume <= 0:
            return RiskDecision(False, "requested volume is not positive")

        timestamp = max(near.timestamp, far.timestamp)
        if pair.session_windows and not self._inside_sessions(timestamp, pair.session_windows):
            return RiskDecision(False, "outside configured trading session")
        if pair.session_windows and not self._inside_open_close_buffer(
            timestamp, pair.session_windows
        ):
            return RiskDecision(False, "inside session open/close safety window")

        for tick in (near, far):
            spec = (specs or {}).get(tick.symbol)
            price_tick = spec.price_tick if spec else 1.0
            width_ticks = (tick.ask_price - tick.bid_price) / price_tick
            if width_ticks > self.config.max_bid_ask_ticks:
                return RiskDecision(False, "bid/ask spread too wide")

        if action is SignalAction.LONG_SPREAD:
            depths = (near.ask_volume, far.bid_volume)
            legs = (
                (near, OrderSide.BUY),
                (far, OrderSide.SELL),
            )
        else:
            depths = (near.bid_volume, far.ask_volume)
            legs = (
                (near, OrderSide.SELL),
                (far, OrderSide.BUY),
            )
        if min(depths) < requested_volume * self.config.min_depth_multiple:
            return RiskDecision(False, "top-of-book depth is insufficient")

        for tick, side in legs:
            spec = (specs or {}).get(tick.symbol)
            price_tick = spec.price_tick if spec else 1.0
            if side is OrderSide.BUY and tick.limit_up > 0:
                distance = (tick.limit_up - tick.ask_price) / price_tick
                if distance < self.config.limit_distance_ticks:
                    return RiskDecision(False, "buy leg is too close to upper limit")
            if side is OrderSide.SELL and tick.limit_down > 0:
                distance = (tick.bid_price - tick.limit_down) / price_tick
                if distance < self.config.limit_distance_ticks:
                    return RiskDecision(False, "sell leg is too close to lower limit")
        return RiskDecision(True)

    def check_contract_entry(
        self,
        tick: Tick,
        side: OrderSide,
        *,
        requested_volume: int,
        spec: ContractSpec,
        session_windows: tuple[str, ...] = (),
        now: datetime | None = None,
    ) -> RiskDecision:
        """方向组合单合约开仓前复用实盘微观结构硬门。"""
        if requested_volume <= 0:
            return RiskDecision(False, "requested volume is not positive")
        reference = tick.timestamp if now is None else now
        quote_decision = self.check_quotes([tick], reference)
        if not quote_decision.allowed:
            return quote_decision
        if session_windows and not self._inside_sessions(reference, session_windows):
            return RiskDecision(False, "outside configured trading session")
        if session_windows and not self._inside_open_close_buffer(reference, session_windows):
            return RiskDecision(False, "inside session open/close safety window")

        width_ticks = (tick.ask_price - tick.bid_price) / spec.price_tick
        if width_ticks > self.config.max_bid_ask_ticks:
            return RiskDecision(False, "bid/ask spread too wide")
        depth = tick.ask_volume if side is OrderSide.BUY else tick.bid_volume
        if depth < requested_volume * self.config.min_depth_multiple:
            return RiskDecision(False, "top-of-book depth is insufficient")

        if side is OrderSide.BUY and tick.limit_up > 0:
            distance = (tick.limit_up - tick.ask_price) / spec.price_tick
            if distance < self.config.limit_distance_ticks:
                return RiskDecision(False, "buy leg is too close to upper limit")
        if side is OrderSide.SELL and tick.limit_down > 0:
            distance = (tick.bid_price - tick.limit_down) / spec.price_tick
            if distance < self.config.limit_distance_ticks:
                return RiskDecision(False, "sell leg is too close to lower limit")
        return RiskDecision(True)

    def size_pair(
        self,
        account: AccountSnapshot,
        pair: PairConfig,
        specs: dict[str, ContractSpec],
        near: Tick,
        far: Tick,
        *,
        spread_std: float,
    ) -> int:
        """按风险预算、价差波动、盘口深度和硬上限计算开仓手数。"""
        if spread_std <= 0 or account.equity <= 0:
            return 0
        near_spec = specs[pair.near_symbol]
        far_spec = specs[pair.far_symbol]
        per_lot_risk = (
            spread_std
            * max(near_spec.multiplier, far_spec.multiplier)
            * self.config.risk_sigma_multiplier
        )
        risk_cash = account.equity * self.config.risk_budget_ratio
        risk_lots = floor(risk_cash / max(per_lot_risk, 1e-9))
        depth_lots = floor(
            min(
                near.bid_volume,
                near.ask_volume,
                far.bid_volume,
                far.ask_volume,
            )
            / self.config.min_depth_multiple
        )
        return max(
            0,
            min(
                pair.volume,
                self.config.max_contract_volume,
                risk_lots,
                depth_lots,
            ),
        )

    def check_open_orders(
        self,
        account: AccountSnapshot,
        orders: list[OrderRequest],
        specs: dict[str, ContractSpec],
        *,
        current_contract_volumes: dict[str, int] | None = None,
    ) -> RiskDecision:
        """通用开仓批次账户/保证金/单合约上限门，不包含策略类型限制。"""
        account_decision = self.check_account(account)
        if not account_decision.allowed:
            return account_decision
        current_contract_volumes = current_contract_volumes or {}
        requested_by_symbol: dict[str, int] = {}
        estimated_margin = 0.0
        for order in orders:
            if order.offset is not Offset.OPEN:
                continue
            if order.volume <= 0:
                return RiskDecision(False, "contract volume must be positive")
            requested_by_symbol[order.symbol] = (
                requested_by_symbol.get(order.symbol, 0) + order.volume
            )
            total_volume = (
                current_contract_volumes.get(order.symbol, 0) + requested_by_symbol[order.symbol]
            )
            if total_volume > self.config.max_contract_volume:
                return RiskDecision(False, "contract volume limit reached")
            spec = specs.get(order.symbol)
            if spec is None:
                return RiskDecision(False, f"missing contract spec: {order.symbol}")
            rate = spec.margin_rate_long if order.side is OrderSide.BUY else spec.margin_rate_short
            estimated_margin += order.price * spec.multiplier * rate * order.volume

        estimated_margin *= self.config.margin_estimate_buffer
        post_margin = account.margin + estimated_margin
        if post_margin / account.equity > self.config.max_margin_ratio:
            return RiskDecision(False, "combined margin ratio would exceed limit")
        if (account.equity - post_margin) / account.equity < self.config.min_available_ratio:
            return RiskDecision(False, "combined cash reserve would fall below limit")
        return RiskDecision(True)

    def check_open_batch(
        self,
        account: AccountSnapshot,
        orders: list[OrderRequest],
        specs: dict[str, ContractSpec],
        *,
        open_pair_count: int,
        current_contract_volumes: dict[str, int] | None = None,
    ) -> RiskDecision:
        """套利开仓额外保留 pair 数量门，再复用通用批次风险。"""
        if open_pair_count >= self.config.max_open_pairs:
            return RiskDecision(False, "open pair limit reached")
        return self.check_open_orders(
            account,
            orders,
            specs,
            current_contract_volumes=current_contract_volumes,
        )

    def is_pair_session_active(self, pair: PairConfig, timestamp: datetime) -> bool:
        """判断组合当前是否处于中国本地交易时段；未配置时默认活跃。"""
        if not pair.session_windows:
            return True
        return self._inside_sessions(timestamp, pair.session_windows)

    def _inside_sessions(self, timestamp: datetime, windows: tuple[str, ...]) -> bool:
        local = self._china_timestamp(timestamp)
        now = local.timetz().replace(tzinfo=None)
        return any(self._inside_window(now, raw) for raw in windows)

    def _inside_open_close_buffer(self, timestamp: datetime, windows: tuple[str, ...]) -> bool:
        local = self._china_timestamp(timestamp)
        now_minutes = local.hour * 60 + local.minute
        for raw in windows:
            start_raw, end_raw = raw.split("-", 1)
            start_hour, start_minute = map(int, start_raw.split(":"))
            end_hour, end_minute = map(int, end_raw.split(":"))
            start = start_hour * 60 + start_minute
            end = end_hour * 60 + end_minute
            current = now_minutes
            if end < start:
                if current < start:
                    current += 24 * 60
                end += 24 * 60
            if start <= current <= end:
                return (
                    current >= start + self.config.open_cooldown_minutes
                    and current <= end - self.config.close_blackout_minutes
                )
        return False

    @staticmethod
    def _china_timestamp(timestamp: datetime) -> datetime:
        if timestamp.tzinfo is None:
            return timestamp.replace(tzinfo=_CHINA_TZ)
        return timestamp.astimezone(_CHINA_TZ)

    @staticmethod
    def _inside_window(now: time, raw: str) -> bool:
        start_raw, end_raw = raw.split("-", 1)
        start_hour, start_minute = map(int, start_raw.split(":"))
        end_hour, end_minute = map(int, end_raw.split(":"))
        start = time(start_hour, start_minute)
        end = time(end_hour, end_minute)
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end

    def _validate_config(self) -> None:
        self.config.validate()
