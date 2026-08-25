"""系统内部统一数据模型。

策略、风控、模拟交易和 CTP 适配器只通过这些英文模型交换数据，
代码标识符保持英文，中文只用于注释、文档和面向人的日志。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class OrderSide(str, Enum):
    """报单买卖方向。"""

    BUY = "BUY"
    SELL = "SELL"


class Offset(str, Enum):
    """开平仓方向。"""

    OPEN = "OPEN"
    CLOSE = "CLOSE"
    CLOSE_TODAY = "CLOSE_TODAY"
    CLOSE_YESTERDAY = "CLOSE_YESTERDAY"


class OrderType(str, Enum):
    """订单类型。"""

    LIMIT = "LIMIT"
    FAK = "FAK"
    FOK = "FOK"


class OrderStatus(str, Enum):
    """统一订单状态。"""

    SUBMITTING = "SUBMITTING"
    NOT_TRADED = "NOT_TRADED"
    PART_TRADED = "PART_TRADED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class SignalAction(str, Enum):
    """套利策略输出。"""

    LONG_SPREAD = "LONG_SPREAD"
    SHORT_SPREAD = "SHORT_SPREAD"
    EXIT = "EXIT"
    EMERGENCY_EXIT = "EMERGENCY_EXIT"
    HOLD = "HOLD"


class RuntimeMode(str, Enum):
    """生产状态机状态。"""

    RUNNING = "RUNNING"
    REDUCE_ONLY = "REDUCE_ONLY"
    HALTED = "HALTED"


@dataclass(frozen=True)
class FeeSpec:
    """手续费模型，同时支持按手和按成交额收费。"""

    open_fixed: float = 0.0
    open_rate: float = 0.0
    close_fixed: float = 0.0
    close_rate: float = 0.0
    close_today_fixed: float = 0.0
    close_today_rate: float = 0.0


@dataclass(frozen=True)
class ContractSpec:
    """研究、回放和事前风控所需的合约参数。"""

    symbol: str
    exchange: str
    multiplier: float
    price_tick: float
    margin_rate_long: float
    margin_rate_short: float
    fee: FeeSpec = field(default_factory=FeeSpec)


@dataclass(frozen=True)
class ContractInfo:
    """用于自动发现的期货合约目录信息。

    ``listing`` 仅用于历史研究重建“当日真实可见目录”；实盘 CTP 本身只返回已挂牌
    合约，因此该字段可以为空，不会把研究逻辑扩散成第二套生产 Universe。
    """

    symbol: str
    exchange: str
    product: str
    expiry: str
    listing: str = ""


@dataclass(frozen=True)
class PairConfig:
    """同品种跨期套利组合配置。

    ``volume`` 是允许的最大手数；实际开仓手数由风险预算、波动和流动性共同决定。
    相对价值扩展默认关闭，因此旧配置继续使用绝对价差和原有入场逻辑。
    """

    pair_id: str
    near_symbol: str
    far_symbol: str
    exchange: str
    volume: int
    lookback: int = 60
    entry_z: float = 2.0
    exit_z: float = 0.5
    stop_z: float = 4.0
    sample_seconds: int = 0
    expiry_near: str = ""
    expiry_far: str = ""
    max_holding_samples: int = 120
    structural_mean_shift_z: float = 3.0
    structural_vol_ratio: float = 2.5
    min_net_edge: float = 0.0
    legging_buffer: float = 0.0
    risk_group: str = ""
    session_windows: tuple[str, ...] = ()
    signal_transform: str = "spread"
    confirm_entry: bool = False
    confirmation_retrace_z: float = 0.0
    min_confirmed_entry_z: float = 0.0
    entry_trend_window: int = 6
    max_entry_z_slope: float = 999.0
    min_stationarity_score: float = 0.0
    max_half_life: float = 999.0
    daily_sample_window: str = ""


@dataclass(frozen=True)
class Tick:
    """统一一档行情。时间戳必须带时区。"""

    symbol: str
    exchange: str
    timestamp: datetime
    bid_price: float
    ask_price: float
    last_price: float
    bid_volume: float
    ask_volume: float
    trading_day: str
    limit_up: float = 0.0
    limit_down: float = 0.0
    volume: float = 0.0
    open_interest: float = 0.0

    def validate(self) -> None:
        """拒绝会导致错误成交或风险估计的异常行情。"""
        if self.timestamp.tzinfo is None:
            raise ValueError("tick timestamp must be timezone-aware")
        if self.bid_price <= 0 or self.ask_price <= 0:
            raise ValueError("bid/ask price must be positive")
        if self.ask_price < self.bid_price:
            raise ValueError("ask price cannot be below bid price")
        if self.bid_volume <= 0 or self.ask_volume <= 0:
            raise ValueError("quote volume must be positive")
        if self.limit_up and self.limit_down and self.limit_up <= self.limit_down:
            raise ValueError("daily price limits are invalid")
        if self.volume < 0 or self.open_interest < 0:
            raise ValueError("volume/open_interest cannot be negative")

    @property
    def mid_price(self) -> float:
        return (self.bid_price + self.ask_price) / 2.0


@dataclass(frozen=True)
class SpreadSignal:
    """策略信号只表达目标，不直接操作交易账户。"""

    pair_id: str
    action: SignalAction
    zscore: float
    timestamp: datetime
    spread: float
    reference_mean: float
    reference_std: float = 0.0
    reason: str = ""


@dataclass(frozen=True)
class OrderRequest:
    """统一下单请求。reference 用于跟踪套利组合。"""

    symbol: str
    exchange: str
    side: OrderSide
    offset: Offset
    volume: int
    price: float
    order_type: OrderType = OrderType.LIMIT
    reference: str = ""


@dataclass
class Order:
    """统一订单状态。"""

    order_id: str
    request: OrderRequest
    status: OrderStatus = OrderStatus.SUBMITTING
    traded: int = 0
    average_price: float = 0.0
    message: str = ""

    @property
    def active(self) -> bool:
        return self.status in {
            OrderStatus.SUBMITTING,
            OrderStatus.NOT_TRADED,
            OrderStatus.PART_TRADED,
        }


@dataclass(frozen=True)
class Trade:
    """成交记录。"""

    trade_id: str
    order_id: str
    symbol: str
    exchange: str
    side: OrderSide
    offset: Offset
    volume: int
    price: float
    timestamp: datetime
    commission: float = 0.0


@dataclass
class ContractPosition:
    """按今昨仓拆分的合约持仓。"""

    symbol: str
    exchange: str
    long_today: int = 0
    long_yesterday: int = 0
    short_today: int = 0
    short_yesterday: int = 0
    long_price: float = 0.0
    short_price: float = 0.0

    @property
    def long_total(self) -> int:
        return self.long_today + self.long_yesterday

    @property
    def short_total(self) -> int:
        return self.short_today + self.short_yesterday

    @property
    def net_volume(self) -> int:
        return self.long_total - self.short_total

    @property
    def empty(self) -> bool:
        return self.long_total == 0 and self.short_total == 0


@dataclass(frozen=True)
class AccountSnapshot:
    """统一账户快照。"""

    balance: float
    equity: float
    available: float
    margin: float
    realized_pnl: float
    unrealized_pnl: float
    trading_day: str


@dataclass(frozen=True)
class RiskDecision:
    """风险规则判断结果。"""

    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class ExecutionResult:
    """一次套利组合执行结果。"""

    accepted: bool
    order_ids: tuple[str, ...] = ()
    reason: str = ""
    volume: int = 0


@dataclass(frozen=True)
class BrokerEvent:
    """柜台向交易引擎投递的统一事件。"""

    event_type: str
    payload: object
