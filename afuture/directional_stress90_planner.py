"""Pure lot-stage planner shared by Stress-90 live and acceptance paths."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

from .directional import build_margin_aware_target_lots, build_target_lots
from .models import AccountSnapshot, ContractSpec, Tick


def _lots(raw: Mapping[str, int]) -> dict[str, int]:
    return {str(symbol): int(volume) for symbol, volume in raw.items() if int(volume) != 0}


def _products(
    symbols: Collection[str],
    symbol_products: Mapping[str, str],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for symbol in symbols:
        product = str(symbol_products.get(symbol, "")).upper()
        if not product:
            raise ValueError(f"missing symbol product: {symbol}")
        result[symbol] = product
    return result


def freeze_new_risk_target(
    *,
    current_lots: Mapping[str, int],
    target_lots: Mapping[str, int],
    symbol_products: Mapping[str, str],
    triggered: bool,
    blocked_products: Collection[str] | None = None,
    authorized_transition_products: Collection[str] = (),
) -> dict[str, int]:
    """Freeze only entries/adds while preserving exits, reversals and product rolls.

    ``authorized_transition_products`` is durable execution intent created before a
    reduction/roll starts.  It lets a later Broker-truth pass finish the opening leg
    after the old position has reached zero without misclassifying it as a fresh entry.
    """

    current = _lots(current_lots)
    target = _lots(target_lots)
    symbols = set(current) | set(target)
    products = _products(symbols, symbol_products)
    if not triggered:
        return dict(target)

    blocked = (
        {str(product).upper() for product in blocked_products}
        if blocked_products is not None
        else set(products.values())
    )
    authorized = {str(product).upper() for product in authorized_transition_products}
    current_by_product: dict[str, set[str]] = {}
    for symbol in current:
        current_by_product.setdefault(products[symbol], set()).add(symbol)

    result: dict[str, int] = {}
    for symbol, wanted in sorted(target.items()):
        product = products[symbol]
        if product not in blocked:
            result[symbol] = wanted
            continue
        have = current.get(symbol, 0)
        if have:
            if (have > 0) != (wanted > 0) or abs(wanted) <= abs(have):
                result[symbol] = wanted
            else:
                result[symbol] = have
            continue
        if current_by_product.get(product) or product in authorized:
            result[symbol] = wanted

    return _lots(result)


def _rebalance_from_lots(
    current_lots: Mapping[str, int],
    target_lots: Mapping[str, int],
) -> tuple[dict[str, int], dict[str, int]]:
    current = _lots(current_lots)
    target = _lots(target_lots)
    reductions: dict[str, int] = {}
    potential_openings: dict[str, int] = {}
    for symbol in sorted(set(current) | set(target)):
        have = current.get(symbol, 0)
        wanted = target.get(symbol, 0)
        if have == wanted:
            continue
        if have == 0:
            potential_openings[symbol] = wanted
        elif wanted == 0 or (have > 0) != (wanted > 0):
            reductions[symbol] = -have
        elif abs(wanted) < abs(have):
            reductions[symbol] = wanted - have
        else:
            potential_openings[symbol] = wanted - have
    return reductions, {} if reductions else potential_openings


def _action_categories(
    current_lots: Mapping[str, int],
    target_lots: Mapping[str, int],
    symbol_products: Mapping[str, str],
    *,
    authorized_transition_products: Collection[str],
) -> dict[str, str]:
    current = _lots(current_lots)
    target = _lots(target_lots)
    products = _products(set(current) | set(target), symbol_products)
    authorized = {str(product).upper() for product in authorized_transition_products}
    current_by_product = {
        product
        for symbol, product in products.items()
        if symbol in current and current[symbol] != 0
    }
    target_by_product = {
        product for symbol, product in products.items() if symbol in target and target[symbol] != 0
    }
    result: dict[str, str] = {}
    for symbol in sorted(set(current) | set(target)):
        have = current.get(symbol, 0)
        wanted = target.get(symbol, 0)
        if have == wanted:
            continue
        product = products[symbol]
        if have and wanted and (have > 0) != (wanted > 0):
            result[symbol] = "reversal"
        elif have and not wanted:
            result[symbol] = "same_product_roll" if product in target_by_product else "exit"
        elif not have and wanted:
            if product in current_by_product:
                result[symbol] = "same_product_roll"
            elif product in authorized:
                result[symbol] = "reversal_open"
            else:
                result[symbol] = "entry"
        elif abs(wanted) < abs(have):
            result[symbol] = "reduction"
        else:
            result[symbol] = "same_sign_add"
    return result


@dataclass(frozen=True)
class Stress90LotStages:
    raw_integer_lots: dict[str, int]
    margin_fitted_lots: dict[str, int]
    drawdown_frozen_lots: dict[str, int]
    hhi_frozen_lots: dict[str, int]
    final_frozen_lots: dict[str, int]
    reductions: dict[str, int]
    openings: dict[str, int]
    action_categories: dict[str, str]


def build_stress90_rebalance_stages(
    *,
    account: AccountSnapshot,
    product_weights: Mapping[str, float],
    product_ticks: Mapping[str, Tick],
    specs: Mapping[str, ContractSpec],
    current_lots: Mapping[str, int],
    symbol_products: Mapping[str, str],
    max_contract_volume: int,
    max_margin_ratio: float,
    min_available_ratio: float,
    max_daily_loss_ratio: float,
    margin_estimate_buffer: float,
    completed_returns: Iterable[float],
    drawdown_reserve_freeze: bool,
    concentration_freeze: bool,
    unavailable_products: Collection[str] = (),
    entry_blocked_products: Collection[str] = (),
    authorized_transition_products: Collection[str] = (),
    persisted_margin_fitted_lots: Mapping[str, int] | None = None,
) -> Stress90LotStages:
    """Apply 1x integer sizing, margin fitting, reserve freeze, then HHI freeze."""

    raw = build_target_lots(
        account,
        product_weights,
        product_ticks,
        specs,
        max_contract_volume=max_contract_volume,
    )
    fitted = (
        _lots(persisted_margin_fitted_lots)
        if persisted_margin_fitted_lots is not None
        else build_margin_aware_target_lots(
            account,
            product_weights,
            product_ticks,
            specs,
            max_contract_volume=max_contract_volume,
            max_margin_ratio=max_margin_ratio,
            min_available_ratio=min_available_ratio,
            max_daily_loss_ratio=max_daily_loss_ratio,
            margin_estimate_buffer=margin_estimate_buffer,
            completed_returns=completed_returns,
            current_lots=current_lots,
        )
    )
    unavailable = {str(product).upper() for product in unavailable_products}
    margin_target = _lots(fitted)
    current = _lots(current_lots)
    known_products = _products(set(current) | set(margin_target), symbol_products)
    for symbol, volume in current.items():
        if known_products[symbol] in unavailable:
            margin_target[symbol] = volume

    drawdown = freeze_new_risk_target(
        current_lots=current,
        target_lots=margin_target,
        symbol_products=symbol_products,
        triggered=drawdown_reserve_freeze,
        authorized_transition_products=authorized_transition_products,
    )
    hhi = freeze_new_risk_target(
        current_lots=current,
        target_lots=drawdown,
        symbol_products=symbol_products,
        triggered=concentration_freeze,
        authorized_transition_products=authorized_transition_products,
    )
    blocked = {str(product).upper() for product in entry_blocked_products}
    final = freeze_new_risk_target(
        current_lots=current,
        target_lots=hhi,
        symbol_products=symbol_products,
        triggered=bool(blocked),
        blocked_products=blocked,
        authorized_transition_products=authorized_transition_products,
    )
    reductions, openings = _rebalance_from_lots(current, final)
    categories = _action_categories(
        current,
        final,
        symbol_products,
        authorized_transition_products=authorized_transition_products,
    )
    return Stress90LotStages(
        raw_integer_lots=_lots(raw),
        margin_fitted_lots=margin_target,
        drawdown_frozen_lots=drawdown,
        hhi_frozen_lots=hhi,
        final_frozen_lots=final,
        reductions=reductions,
        openings=openings,
        action_categories=categories,
    )
