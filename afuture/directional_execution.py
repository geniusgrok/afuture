"""Low-freedom execution pricing helpers for directional openings.

Risk reductions deliberately remain on the existing aggressive-price path. Opening FAK
orders may avoid one extra price tick only when the currently displayed opposite-side
L1 depth covers the complete requested volume; otherwise the configured aggressive price is
retained. This helper never changes volume, order type, risk authority, or order count.
"""

from __future__ import annotations

from .models import ContractSpec, OrderSide, Tick


def depth_aware_opening_price(
    tick: Tick,
    spec: ContractSpec,
    side: OrderSide,
    *,
    requested_volume: int,
    aggressive_ticks: int,
) -> float:
    """Return a marketable opening limit without weakening reduction execution."""
    volume = int(requested_volume)
    if volume <= 0:
        raise ValueError("requested_volume must be positive")
    price_tick = float(spec.price_tick)
    if price_tick <= 0:
        raise ValueError("price_tick must be positive")
    steps = max(0, int(aggressive_ticks))

    if side is OrderSide.BUY:
        if float(tick.ask_volume) >= volume:
            price = float(tick.ask_price)
        else:
            price = float(tick.ask_price) + steps * price_tick
        return min(price, float(tick.limit_up)) if tick.limit_up > 0 else price

    if side is OrderSide.SELL:
        if float(tick.bid_volume) >= volume:
            price = float(tick.bid_price)
        else:
            price = float(tick.bid_price) - steps * price_tick
        return max(price, float(tick.limit_down)) if tick.limit_down > 0 else price

    raise ValueError(f"unsupported order side: {side}")
