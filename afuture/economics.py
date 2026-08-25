"""跨期套利的可成交价差和净交易边际。

统计偏离只有在扣除 bid/ask、手续费、滑点和裸腿风险缓冲后仍为正，
才具备实际开仓意义。
"""

from __future__ import annotations

from dataclasses import dataclass

from .fees import calculate_commission
from .models import ContractSpec, Offset, SignalAction, Tick


@dataclass(frozen=True)
class EdgeEstimate:
    """一次完整开平往返的预期经济边际，单位为账户货币。"""

    executable_spread: float
    gross_edge: float
    transaction_cost: float
    legging_buffer: float
    total_cost: float
    net_edge: float


def executable_spreads(near: Tick, far: Tick) -> tuple[float, float]:
    """返回多价差和空价差真正可成交的一档价差。"""
    long_spread = near.ask_price - far.bid_price
    short_spread = near.bid_price - far.ask_price
    return long_spread, short_spread


def estimate_net_edge(
    action: SignalAction,
    *,
    reference_mean: float,
    near: Tick,
    far: Tick,
    specs: dict[str, ContractSpec],
    volume: int,
    slippage_ticks: int = 1,
    legging_buffer: float = 0.0,
    cost_multiplier: float = 1.0,
) -> EdgeEstimate:
    """估计价差回归到参考均值后的净收益。

    ``cost_multiplier`` 仅用于研究压力测试。正常交易固定使用 1.0；压力测试会同时
    放大手续费、滑点和裸腿风险缓冲，防止只对单一成本项做乐观压力假设。
    """
    if cost_multiplier <= 0:
        raise ValueError("cost_multiplier must be positive")
    if volume <= 0:
        buffer_cost = max(0.0, legging_buffer) * cost_multiplier
        return EdgeEstimate(
            executable_spread=0.0,
            gross_edge=0.0,
            transaction_cost=0.0,
            legging_buffer=buffer_cost,
            total_cost=buffer_cost,
            net_edge=-buffer_cost,
        )

    near_spec = specs[near.symbol]
    far_spec = specs[far.symbol]
    long_spread, short_spread = executable_spreads(near, far)

    if action is SignalAction.LONG_SPREAD:
        executable = long_spread
        spread_points = reference_mean - executable
    elif action is SignalAction.SHORT_SPREAD:
        executable = short_spread
        spread_points = executable - reference_mean
    else:
        executable = (long_spread + short_spread) / 2.0
        spread_points = 0.0

    # 同品种跨期合约乘数正常应相同；取较小值避免把异常配置变成乐观收益估计。
    spread_multiplier = min(near_spec.multiplier, far_spec.multiplier)
    gross_edge = max(0.0, spread_points) * spread_multiplier * volume

    commission = 0.0
    for spec, price in (
        (near_spec, near.mid_price),
        (far_spec, far.mid_price),
    ):
        open_fee = calculate_commission(spec, Offset.OPEN, price, volume)
        close_fee = calculate_commission(spec, Offset.CLOSE, price, volume)
        close_today_fee = calculate_commission(spec, Offset.CLOSE_TODAY, price, volume)
        # 平今费用可能显著高于普通平仓，往返估算取更保守的一项。
        commission += open_fee + max(close_fee, close_today_fee)

    slippage = (
        2.0
        * max(0, slippage_ticks)
        * volume
        * (near_spec.price_tick * near_spec.multiplier + far_spec.price_tick * far_spec.multiplier)
    )
    transaction_cost = (commission + slippage) * cost_multiplier
    buffer_cost = max(0.0, legging_buffer) * cost_multiplier
    total_cost = transaction_cost + buffer_cost
    return EdgeEstimate(
        executable_spread=executable,
        gross_edge=gross_edge,
        transaction_cost=transaction_cost,
        legging_buffer=buffer_cost,
        total_cost=total_cost,
        net_edge=gross_edge - total_cost,
    )
