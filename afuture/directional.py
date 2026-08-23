"""Shared directional production primitives.

The frozen signal engine lives only in ``execution_aligned_policy``. This module owns the
configuration, point-in-time contract selector, integer target-lot conversion and
reduction-first rebalance primitives shared by runtime and tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from math import floor
import re
from statistics import stdev
from typing import Iterable, Mapping

from .directional_efficiency import stabilize_one_lot_increases
from .models import (
    AccountSnapshot,
    ContractInfo,
    ContractPosition,
    ContractSpec,
    Tick,
)

MAX_GROSS_LEVERAGE = 2.0


@dataclass(frozen=True)
class DirectionalConfig:
    enabled: bool = False
    products: tuple[str, ...] = ()
    exchanges: tuple[str, ...] = ("DCE", "CZCE", "SHFE", "INE")
    max_gross_leverage: float = MAX_GROSS_LEVERAGE
    min_days_to_expiry: int = 20
    min_volume: float = 1000.0
    min_open_interest: float = 5000.0
    max_contract_volume: int = 35
    rebalance_window: str = "21:00-21:10"
    signal_max_age_hours: float = 36.0
    account_exclusive: bool = True

    def validate(self) -> None:
        if not self.enabled:
            return
        if not self.products:
            raise ValueError("directional products cannot be empty")
        if not self.exchanges:
            raise ValueError("directional exchanges cannot be empty")
        if not 0 < self.max_gross_leverage <= MAX_GROSS_LEVERAGE:
            raise ValueError("directional gross leverage must be in (0, 2.0]")
        if self.min_days_to_expiry < 0:
            raise ValueError("directional min_days_to_expiry cannot be negative")
        if self.min_volume < 0 or self.min_open_interest < 0:
            raise ValueError("directional activity thresholds cannot be negative")
        if self.max_contract_volume <= 0:
            raise ValueError("directional max_contract_volume must be positive")
        if self.signal_max_age_hours <= 0:
            raise ValueError("directional signal_max_age_hours must be positive")
        if not self.account_exclusive:
            raise ValueError("directional production mode must be account-exclusive")
        _parse_window(self.rebalance_window)


class DirectionalContractSelector:
    def __init__(self, config: DirectionalConfig) -> None:
        config.validate()
        self.config = config

    def select(
        self,
        catalog: Iterable[ContractInfo],
        ticks: Mapping[str, Tick],
        today: date,
    ) -> dict[str, ContractInfo]:
        products = {item.upper() for item in self.config.products}
        exchanges = {item.upper() for item in self.config.exchanges}
        candidates: dict[str, list[tuple[float, float, date, ContractInfo]]] = {}
        for item in catalog:
            product = item.product.upper()
            if product not in products or item.exchange.upper() not in exchanges:
                continue
            if item.listing:
                try:
                    if date.fromisoformat(item.listing) > today:
                        continue
                except ValueError:
                    continue
            try:
                expiry = date.fromisoformat(item.expiry)
            except ValueError:
                continue
            if (expiry - today).days < self.config.min_days_to_expiry:
                continue
            tick = ticks.get(item.symbol)
            if tick is None:
                continue
            if tick.volume < self.config.min_volume:
                continue
            if tick.open_interest < self.config.min_open_interest:
                continue
            candidates.setdefault(product, []).append(
                (tick.open_interest, tick.volume, expiry, item)
            )

        result: dict[str, ContractInfo] = {}
        for product, rows in candidates.items():
            rows.sort(
                key=lambda row: (-row[0], -row[1], row[2], row[3].symbol)
            )
            result[product] = rows[0][3]
        return result


@dataclass(frozen=True)
class RebalancePlan:
    reductions: dict[str, int] = field(default_factory=dict)
    openings: dict[str, int] = field(default_factory=dict)


def fit_target_lots_to_margin_budget(
    target_lots: Mapping[str, int],
    per_lot_margin: Mapping[str, float],
    *,
    margin_budget: float,
) -> dict[str, int]:
    """Fit a signed integer target to a hard margin budget without increasing risk.

    Scaling is proportional across requested contracts, then any remaining budget is
    allocated one lot at a time by largest fractional remainder with symbol ordering as
    the deterministic tie-break. Missing/invalid margin evidence fails closed instead
    of allowing an opening batch to rely on a guessed margin rate.
    """
    budget = float(margin_budget)
    if budget < 0:
        raise ValueError("margin_budget cannot be negative")

    requested: dict[str, int] = {}
    margins: dict[str, float] = {}
    total_margin = 0.0
    for symbol in sorted(target_lots):
        volume = int(target_lots[symbol])
        if volume == 0:
            continue
        unit_margin = float(per_lot_margin.get(symbol, 0.0))
        if unit_margin <= 0:
            raise ValueError(f"missing positive per-lot margin: {symbol}")
        requested[symbol] = volume
        margins[symbol] = unit_margin
        total_margin += abs(volume) * unit_margin

    if not requested or budget == 0:
        return {}
    if total_margin <= budget + 1e-10:
        return dict(requested)

    scale = budget / total_margin
    magnitudes: dict[str, int] = {}
    candidates: list[tuple[float, str]] = []
    used_margin = 0.0
    for symbol in sorted(requested):
        magnitude = abs(requested[symbol])
        ideal = magnitude * scale
        fitted = min(magnitude, floor(ideal))
        magnitudes[symbol] = fitted
        used_margin += fitted * margins[symbol]
        if fitted < magnitude:
            candidates.append((ideal - fitted, symbol))

    for _, symbol in sorted(candidates, key=lambda item: (-item[0], item[1])):
        if used_margin + margins[symbol] > budget + 1e-10:
            continue
        if magnitudes[symbol] >= abs(requested[symbol]):
            continue
        magnitudes[symbol] += 1
        used_margin += margins[symbol]

    return {
        symbol: magnitude if requested[symbol] > 0 else -magnitude
        for symbol, magnitude in magnitudes.items()
        if magnitude > 0
    }


def adaptive_margin_sizing_share(
    *,
    max_margin_ratio: float,
    min_available_ratio: float,
    max_daily_loss_ratio: float,
    completed_returns: Iterable[float] = (),
    volatility_trigger: float = 0.03,
) -> float:
    """Causal soft margin envelope below unchanged account hard gates.

    Missing or calm completed-return evidence keeps the conservative 30%-equivalent
    envelope. Completed shocks above the volatility trigger contract that envelope
    further; this helper never expands normal target margin toward the 35% hard gate.
    """
    margin_ratio = float(max_margin_ratio)
    available_ratio = float(min_available_ratio)
    daily_loss_ratio = float(max_daily_loss_ratio)
    if not 0 < margin_ratio < 1:
        raise ValueError("max_margin_ratio must be in (0, 1)")
    if not 0 <= available_ratio < 1:
        raise ValueError("min_available_ratio must be in [0, 1)")
    if not 0 < daily_loss_ratio < 1:
        raise ValueError("max_daily_loss_ratio must be in (0, 1)")
    if volatility_trigger <= 0:
        raise ValueError("volatility_trigger must be positive")
    hard_share = min(margin_ratio, 1.0 - available_ratio)
    conservative = max(0.0, hard_share - daily_loss_ratio)
    values = [float(value) for value in completed_returns]
    if not values:
        return min(hard_share, conservative)
    sample = values[-2:]
    sample_vol = stdev(sample) if len(sample) >= 2 else 0.0
    shock = max(volatility_trigger, abs(values[-1]), sample_vol)
    shock = min(max(shock, volatility_trigger), daily_loss_ratio)
    excess_shock = max(0.0, shock - volatility_trigger)
    adaptive = conservative * (1.0 - excess_shock)
    return min(hard_share, max(0.0, min(conservative, adaptive)))


def margin_sizing_share(
    *,
    max_margin_ratio: float,
    min_available_ratio: float,
    max_daily_loss_ratio: float,
) -> float:
    """Compatibility wrapper for the no-history conservative soft margin envelope."""
    return adaptive_margin_sizing_share(
        max_margin_ratio=max_margin_ratio,
        min_available_ratio=min_available_ratio,
        max_daily_loss_ratio=max_daily_loss_ratio,
        completed_returns=(),
    )


def build_target_lots(
    account: AccountSnapshot,
    product_weights: Mapping[str, float],
    product_ticks: Mapping[str, Tick],
    specs: Mapping[str, ContractSpec],
    *,
    max_contract_volume: int,
) -> dict[str, int]:
    targets: dict[str, int] = {}
    gross = sum(abs(float(value)) for value in product_weights.values())
    if gross > MAX_GROSS_LEVERAGE + 1e-10:
        raise ValueError(f"target product weights exceed 2x gross: {gross}")
    for product, raw_weight in product_weights.items():
        weight = float(raw_weight)
        if abs(weight) <= 1e-15:
            continue
        tick = product_ticks.get(product)
        if tick is None:
            continue
        spec = specs.get(tick.symbol)
        if spec is None:
            continue
        price = tick.mid_price
        notional = price * spec.multiplier
        if price <= 0 or notional <= 0:
            continue
        lots = min(
            max_contract_volume,
            floor(account.equity * abs(weight) / notional),
        )
        if lots > 0:
            targets[tick.symbol] = lots if weight > 0 else -lots
    return targets


def build_margin_aware_target_lots(
    account: AccountSnapshot,
    product_weights: Mapping[str, float],
    product_ticks: Mapping[str, Tick],
    specs: Mapping[str, ContractSpec],
    *,
    max_contract_volume: int,
    max_margin_ratio: float,
    min_available_ratio: float,
    max_daily_loss_ratio: float,
    margin_estimate_buffer: float,
    completed_returns: Iterable[float] = (),
    current_lots: Mapping[str, int] | None = None,
) -> dict[str, int]:
    """Build the requested target then fit it inside a soft account margin envelope.

    The sizing envelope reserves the configured daily-loss capacity; the existing
    ``RiskManager.check_open_orders`` remains the final fail-closed authority against the
    unchanged hard margin and cash-reserve gates using a fresh Broker snapshot.
    """
    requested = build_target_lots(
        account,
        product_weights,
        product_ticks,
        specs,
        max_contract_volume=max_contract_volume,
    )
    if not requested:
        return {}
    if account.equity <= 0:
        return {}
    if float(margin_estimate_buffer) < 1:
        raise ValueError("margin_estimate_buffer must be at least 1")

    ticks_by_symbol = {tick.symbol: tick for tick in product_ticks.values()}
    per_lot_margin: dict[str, float] = {}
    for symbol, volume in requested.items():
        tick = ticks_by_symbol.get(symbol)
        spec = specs.get(symbol)
        if tick is None or spec is None:
            raise ValueError(f"missing target margin evidence: {symbol}")
        rate = spec.margin_rate_long if volume > 0 else spec.margin_rate_short
        unit_margin = (
            float(tick.mid_price)
            * float(spec.multiplier)
            * float(rate)
            * float(margin_estimate_buffer)
        )
        if unit_margin <= 0:
            raise ValueError(f"missing positive per-lot margin: {symbol}")
        per_lot_margin[symbol] = unit_margin

    sizing_share = adaptive_margin_sizing_share(
        max_margin_ratio=max_margin_ratio,
        min_available_ratio=min_available_ratio,
        max_daily_loss_ratio=max_daily_loss_ratio,
        completed_returns=completed_returns,
    )
    fitted = fit_target_lots_to_margin_budget(
        requested,
        per_lot_margin,
        margin_budget=float(account.equity) * sizing_share,
    )
    current = {str(symbol): int(volume) for symbol, volume in (current_lots or {}).items() if int(volume)}
    if not current:
        return fitted
    lot_notionals = {
        symbol: float(ticks_by_symbol[symbol].mid_price) * float(specs[symbol].multiplier)
        for symbol in requested
        if symbol in ticks_by_symbol and symbol in specs
    }
    if not set(current).issubset(lot_notionals) or not set(current).issubset(per_lot_margin):
        return fitted
    return stabilize_one_lot_increases(
        current_lots=current,
        target_lots=fitted,
        lot_notionals=lot_notionals,
        per_lot_margin=per_lot_margin,
        equity=float(account.equity),
        soft_margin_share=sizing_share,
        max_gross_ratio=MAX_GROSS_LEVERAGE,
    )


def build_realized_gross_reductions(
    current_lots: Mapping[str, int],
    lot_notionals: Mapping[str, float],
    *,
    equity: float,
    max_gross_ratio: float = MAX_GROSS_LEVERAGE,
    cost_rate: float = 0.0,
) -> dict[str, int]:
    """Return reduction-only deltas required by the observed gross hard ceiling.

    The target policy is never pre-haircut. This guard acts only after broker/mark truth
    shows gross above the configured ceiling. Integer reductions include the equity cost
    of the reduction itself so a historical cost model cannot leave the account above
    the same ceiling immediately after enforcing it.
    """
    if max_gross_ratio <= 0:
        raise ValueError("max_gross_ratio must be positive")
    if cost_rate < 0:
        raise ValueError("cost_rate cannot be negative")
    if equity <= 0 or not current_lots:
        return {}

    notionals: dict[str, float] = {}
    gross = 0.0
    for symbol, raw_volume in current_lots.items():
        volume = int(raw_volume)
        if volume == 0:
            continue
        lot_notional = float(lot_notionals.get(symbol, 0.0))
        if lot_notional <= 0:
            raise ValueError(f"missing positive lot notional: {symbol}")
        notionals[symbol] = lot_notional
        gross += abs(volume) * lot_notional

    limit = float(max_gross_ratio) * float(equity)
    if gross <= limit + 1e-10:
        return {}

    denominator = 1.0 - float(max_gross_ratio) * float(cost_rate)
    if denominator <= 0:
        return {
            symbol: -int(volume)
            for symbol, volume in current_lots.items()
            if int(volume) != 0
        }
    required_reduction = max(0.0, (gross - limit) / denominator)
    retain_scale = max(0.0, 1.0 - required_reduction / gross)

    targets: dict[str, int] = {}
    candidates: list[tuple[float, str, float]] = []
    reduction_notional = 0.0
    for symbol in sorted(notionals):
        volume = int(current_lots[symbol])
        magnitude = abs(volume)
        lot_notional = notionals[symbol]
        ideal = magnitude * retain_scale
        target_magnitude = floor(ideal)
        targets[symbol] = target_magnitude if volume > 0 else -target_magnitude
        reduction_notional += (magnitude - target_magnitude) * lot_notional
        if target_magnitude < magnitude:
            candidates.append((ideal - target_magnitude, symbol, lot_notional))

    for _, symbol, lot_notional in sorted(
        candidates, key=lambda item: (-item[0], item[1])
    ):
        if reduction_notional - lot_notional + 1e-10 < required_reduction:
            continue
        volume = int(current_lots[symbol])
        targets[symbol] += 1 if volume > 0 else -1
        reduction_notional -= lot_notional

    return {
        symbol: int(targets[symbol]) - int(current_lots[symbol])
        for symbol in sorted(targets)
        if int(targets[symbol]) != int(current_lots[symbol])
    }


def build_rebalance_plan(
    positions: Iterable[ContractPosition], target_lots: Mapping[str, int]
) -> RebalancePlan:
    current = {
        position.symbol: position.net_volume
        for position in positions
        if position.net_volume != 0
    }
    symbols = set(current) | set(target_lots)
    reductions: dict[str, int] = {}
    potential_openings: dict[str, int] = {}

    for symbol in sorted(symbols):
        have = int(current.get(symbol, 0))
        target = int(target_lots.get(symbol, 0))
        if have == target:
            continue
        if have == 0:
            if target:
                potential_openings[symbol] = target
            continue
        if target == 0:
            reductions[symbol] = -have
            continue
        if (have > 0) != (target > 0):
            reductions[symbol] = -have
            continue
        if abs(target) < abs(have):
            reductions[symbol] = target - have
        elif abs(target) > abs(have):
            potential_openings[symbol] = target - have

    return RebalancePlan(
        reductions=reductions,
        openings={} if reductions else potential_openings,
    )


def _parse_window(raw: str) -> tuple[str, str]:
    match = re.fullmatch(r"(\d{2}:\d{2})-(\d{2}:\d{2})", str(raw))
    if match is None or match.group(1) == match.group(2):
        raise ValueError(f"invalid directional rebalance window: {raw}")
    return match.group(1), match.group(2)
