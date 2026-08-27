"""Pure lot-stage planner shared by Stress-90 live and acceptance paths."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from math import isfinite

from .directional import (
    build_margin_aware_target_lots,
    build_target_lots,
    fit_target_lots_to_margin_budget,
)
from .directional_stress90_policy import STRESS90_POLICY
from .models import AccountSnapshot, ContractSpec, Tick
from .stress90_risk_overlay import scale_stress90_product_weights


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
    authorized_transition_kinds: Mapping[str, str] | None = None,
    authorized_transition_max_replacement_notionals: Mapping[str, float] | None = None,
    lot_notionals: Mapping[str, float] | None = None,
    persisted_authorized_target_lots: Mapping[str, int] | None = None,
) -> dict[str, int]:
    """Freeze only entries/adds while preserving exits, reversals and product rolls.

    ``authorized_transition_products`` is durable execution intent created before a
    reduction/roll starts.  It lets a later Broker-truth pass finish the opening leg
    after the old position has reached zero without misclassifying it as a fresh entry.
    """

    current = _lots(current_lots)
    target = _lots(target_lots)
    if persisted_authorized_target_lots is not None:
        authorized_target = _lots(persisted_authorized_target_lots)
        target = {
            symbol: (1 if wanted > 0 else -1) * min(abs(wanted), abs(authorized_target[symbol]))
            for symbol, wanted in target.items()
            if symbol in authorized_target and (wanted > 0) == (authorized_target[symbol] > 0)
        }
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
    transition_kinds = {
        str(product).upper(): str(kind)
        for product, kind in (authorized_transition_kinds or {}).items()
    }
    authorized.update(transition_kinds)
    transition_budgets = {
        str(product).upper(): float(value)
        for product, value in (authorized_transition_max_replacement_notionals or {}).items()
    }
    if any(
        not product or not isfinite(value) or value <= 0.0
        for product, value in transition_budgets.items()
    ):
        raise ValueError("authorized transition replacement notional is invalid")
    if not set(transition_budgets).issubset(authorized):
        raise ValueError("replacement notional lacks an authorized transition")
    notionals = {symbol: float(value) for symbol, value in (lot_notionals or {}).items()}

    def fit_authorized_replacement(
        rows: Mapping[str, int],
        replacement_budget: float,
    ) -> dict[str, int]:
        per_lot = {symbol: notionals.get(symbol) for symbol in rows}
        if any(value is None or not isfinite(value) or value <= 0.0 for value in per_lot.values()):
            return {}
        return fit_target_lots_to_margin_budget(
            rows,
            {symbol: float(value) for symbol, value in per_lot.items() if value is not None},
            margin_budget=replacement_budget,
        )

    result: dict[str, int] = {}
    for product in sorted(set(products.values())):
        target_rows = {
            symbol: wanted for symbol, wanted in target.items() if products[symbol] == product
        }
        if product not in blocked:
            result.update(target_rows)
            continue
        current_rows = {
            symbol: have for symbol, have in current.items() if products[symbol] == product
        }
        if not target_rows:
            continue
        durable_replacement_budget = transition_budgets.get(product)
        if not current_rows:
            if product in authorized:
                result.update(
                    target_rows
                    if durable_replacement_budget is None
                    else fit_authorized_replacement(
                        target_rows,
                        durable_replacement_budget,
                    )
                )
            continue
        current_signs = {have > 0 for have in current_rows.values()}
        target_signs = {wanted > 0 for wanted in target_rows.values()}
        if len(current_signs) != 1 or len(target_signs) != 1:
            raise ValueError(f"opposing product lots cannot enter freeze planner: {product}")
        if current_signs != target_signs:
            # A reversal removes the old direction before entering the new direction.
            result.update(
                target_rows
                if durable_replacement_budget is None
                else fit_authorized_replacement(
                    target_rows,
                    durable_replacement_budget,
                )
            )
            continue
        if current_rows == target_rows:
            # Exact incumbent risk is not a transition and needs no market evidence.
            result.update(target_rows)
            continue
        if durable_replacement_budget is not None:
            result.update(
                fit_authorized_replacement(
                    target_rows,
                    durable_replacement_budget,
                )
            )
            continue
        required_symbols = set(current_rows) | set(target_rows)
        per_lot: dict[str, float] = {}
        missing_notional = False
        for symbol in required_symbols:
            value = notionals.get(symbol)
            if value is None or not isfinite(value) or value <= 0.0:
                missing_notional = True
                break
            per_lot[symbol] = value
        if missing_notional:
            # Missing evidence can never manufacture a replacement/opening. Preserve
            # only same-symbol incumbent risk up to the requested amount, so exits and
            # reductions remain available.
            for symbol, have in current_rows.items():
                wanted = target_rows.get(symbol, 0)
                if wanted and (wanted > 0) == (have > 0):
                    result[symbol] = (1 if have > 0 else -1) * min(abs(have), abs(wanted))
            continue
        replacement_budget = sum(
            abs(volume) * per_lot[symbol] for symbol, volume in current_rows.items()
        )
        result.update(
            fit_target_lots_to_margin_budget(
                target_rows,
                {symbol: per_lot[symbol] for symbol in target_rows},
                margin_budget=replacement_budget,
            )
        )

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
    authorized_transition_kinds: Mapping[str, str] | None = None,
) -> dict[str, str]:
    current = _lots(current_lots)
    target = _lots(target_lots)
    products = _products(set(current) | set(target), symbol_products)
    authorized = {str(product).upper() for product in authorized_transition_products}
    transition_kinds = {
        str(product).upper(): str(kind)
        for product, kind in (authorized_transition_kinds or {}).items()
    }
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
            if transition_kinds.get(product) == "reversal_open":
                result[symbol] = "reversal"
            else:
                result[symbol] = "same_product_roll" if product in target_by_product else "exit"
        elif not have and wanted:
            if product in transition_kinds:
                result[symbol] = transition_kinds[product]
            elif product in current_by_product:
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
    scaled_integer_lots: dict[str, int]
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
    live_risk_scale: float = 1.0,
    specs: Mapping[str, ContractSpec],
    incumbent_ticks: Mapping[str, Tick] | None = None,
    current_lots: Mapping[str, int],
    symbol_products: Mapping[str, str],
    max_contract_volume: int,
    max_gross_leverage: float = 2.0,
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
    authorized_transition_kinds: Mapping[str, str] | None = None,
    authorized_transition_max_replacement_notionals: Mapping[str, float] | None = None,
    persisted_margin_fitted_lots: Mapping[str, int] | None = None,
    persisted_freeze_authorized_lots: Mapping[str, int] | None = None,
) -> Stress90LotStages:
    """Scale live product targets, then apply integer sizing, margin fit and freezes."""

    gross_limit = float(max_gross_leverage)
    if not 0.0 < gross_limit <= STRESS90_POLICY.max_gross_leverage:
        raise ValueError("Stress-90 execution gross limit must be in (0, 2]")
    if (
        isinstance(max_contract_volume, bool)
        or not isinstance(max_contract_volume, int)
        or not 0 < max_contract_volume <= STRESS90_POLICY.max_contract_lots
    ):
        raise ValueError("Stress-90 contract cap must be a positive integer at most 35")

    scaled_weights = scale_stress90_product_weights(product_weights, live_risk_scale)
    raw = build_target_lots(
        account,
        product_weights,
        product_ticks,
        specs,
        max_contract_volume=max_contract_volume,
    )
    scaled = build_target_lots(
        account,
        scaled_weights,
        product_ticks,
        specs,
        max_contract_volume=max_contract_volume,
    )
    if persisted_margin_fitted_lots is not None:
        fitted = {
            symbol: (1 if volume > 0 else -1) * min(abs(volume), max_contract_volume)
            for symbol, volume in _lots(persisted_margin_fitted_lots).items()
        }
    else:
        fitted = build_margin_aware_target_lots(
            account,
            scaled_weights,
            product_ticks,
            specs,
            max_contract_volume=max_contract_volume,
            max_margin_ratio=max_margin_ratio,
            min_available_ratio=min_available_ratio,
            max_daily_loss_ratio=max(
                float(max_daily_loss_ratio),
                STRESS90_POLICY.daily_loss_ratio,
            ),
            margin_estimate_buffer=margin_estimate_buffer,
            completed_returns=completed_returns,
            current_lots=current_lots,
        )
    unavailable = {str(product).upper() for product in unavailable_products}
    current = _lots(current_lots)
    margin_target = _lots(fitted)
    known_products = _products(set(current) | set(margin_target), symbol_products)
    candidate_signs = {
        str(product).upper(): float(weight) > 0.0
        for product, weight in product_weights.items()
        if isfinite(float(weight)) and abs(float(weight)) > 1e-15
    }
    unavailable_current: dict[str, int] = {}
    for product in sorted(unavailable):
        current_rows = {
            symbol: volume
            for symbol, volume in current.items()
            if known_products[symbol] == product
        }
        target_rows = {
            symbol: volume
            for symbol, volume in margin_target.items()
            if known_products[symbol] == product
        }
        if not current_rows:
            continue
        current_signs = {volume > 0 for volume in current_rows.values()}
        target_signs = {volume > 0 for volume in target_rows.values()}
        if not target_signs and persisted_margin_fitted_lots is None:
            candidate_sign = candidate_signs.get(product)
            if candidate_sign is not None:
                target_signs = {candidate_sign}
        if not target_signs:
            continue
        if len(current_signs) != 1 or len(target_signs) != 1:
            continue
        if current_signs != target_signs:
            # Close the incumbent reversal leg, but do not open the unavailable target.
            continue
        for symbol, volume in current_rows.items():
            # Without complete target mechanics, retain the incumbent exactly: no add,
            # no roll, and no inferred reduction. A stricter hard lot cap still wins.
            retained = (1 if volume > 0 else -1) * min(abs(volume), max_contract_volume)
            unavailable_current[symbol] = retained
    margin_target = {
        symbol: volume
        for symbol, volume in margin_target.items()
        if known_products[symbol] not in unavailable
    }
    ticks_by_symbol = {tick.symbol: tick for tick in product_ticks.values()}
    ticks_by_symbol.update(incumbent_ticks or {})
    lot_notionals: dict[str, float] = {}
    retained_unavailable_gross = 0.0
    retained_unavailable_gross_unknown = False
    for symbol, volume in {**margin_target, **unavailable_current}.items():
        tick = ticks_by_symbol.get(symbol)
        spec = specs.get(symbol)
        if tick is None or spec is None:
            if symbol in unavailable_current:
                retained_unavailable_gross_unknown = True
                continue
            raise ValueError(f"missing target gross evidence: {symbol}")
        lot_notional = float(tick.mid_price) * float(spec.multiplier)
        if lot_notional <= 0.0:
            raise ValueError(f"missing positive per-lot notional: {symbol}")
        if symbol in margin_target:
            lot_notionals[symbol] = lot_notional
        else:
            retained_unavailable_gross += abs(volume) * lot_notional
    margin_target = fit_target_lots_to_margin_budget(
        margin_target,
        lot_notionals,
        margin_budget=(
            0.0
            if retained_unavailable_gross_unknown
            else max(
                0.0,
                max(0.0, float(account.equity)) * gross_limit - retained_unavailable_gross,
            )
        ),
    )
    margin_target.update(unavailable_current)
    freeze_lot_notionals = {
        symbol: float(tick.mid_price) * float(specs[symbol].multiplier)
        for symbol, tick in ticks_by_symbol.items()
        if symbol in specs
    }

    drawdown = freeze_new_risk_target(
        current_lots=current,
        target_lots=margin_target,
        symbol_products=symbol_products,
        triggered=drawdown_reserve_freeze,
        authorized_transition_products=authorized_transition_products,
        authorized_transition_kinds=authorized_transition_kinds,
        authorized_transition_max_replacement_notionals=(
            authorized_transition_max_replacement_notionals
        ),
        lot_notionals=freeze_lot_notionals,
        persisted_authorized_target_lots=persisted_freeze_authorized_lots,
    )
    hhi = freeze_new_risk_target(
        current_lots=current,
        target_lots=drawdown,
        symbol_products=symbol_products,
        triggered=concentration_freeze,
        authorized_transition_products=authorized_transition_products,
        authorized_transition_kinds=authorized_transition_kinds,
        authorized_transition_max_replacement_notionals=(
            authorized_transition_max_replacement_notionals
        ),
        lot_notionals=freeze_lot_notionals,
        persisted_authorized_target_lots=persisted_freeze_authorized_lots,
    )
    blocked = {str(product).upper() for product in entry_blocked_products}
    final = freeze_new_risk_target(
        current_lots=current,
        target_lots=hhi,
        symbol_products=symbol_products,
        triggered=bool(blocked),
        blocked_products=blocked,
        authorized_transition_products=authorized_transition_products,
        authorized_transition_kinds=authorized_transition_kinds,
        authorized_transition_max_replacement_notionals=(
            authorized_transition_max_replacement_notionals
        ),
        lot_notionals=freeze_lot_notionals,
        persisted_authorized_target_lots=persisted_freeze_authorized_lots,
    )
    reductions, openings = _rebalance_from_lots(current, final)
    categories = _action_categories(
        current,
        final,
        symbol_products,
        authorized_transition_products=authorized_transition_products,
        authorized_transition_kinds=authorized_transition_kinds,
    )
    return Stress90LotStages(
        raw_integer_lots=_lots(raw),
        scaled_integer_lots=_lots(scaled),
        margin_fitted_lots=margin_target,
        drawdown_frozen_lots=drawdown,
        hhi_frozen_lots=hhi,
        final_frozen_lots=final,
        reductions=reductions,
        openings=openings,
        action_categories=categories,
    )
