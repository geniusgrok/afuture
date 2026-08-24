"""Research-only freeze-new-risk replacement for global defensive scaling.

The existing DirectionalRiskGovernor trigger remains unchanged: a completed daily loss
of at least 2% or two-day sample volatility of at least 3% activates defense. The research
candidate changes only the *soft response*: instead of multiplying every product target
by 0.25, it passes the raw frozen target into normal margin-aware construction and then
suppresses only new entries and same-sign increases while the trigger is active.

Reductions, exits, reversals and same-product contract rolls are never blocked. Hard
account gates (5% daily loss, 30% total drawdown, 35% margin, 25% available, 2x realized
gross and 35 lots) remain owned by the existing Production mechanics / RiskManager path.
This module is not imported by live runtime wiring.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .directional_acceptance import PRODUCT_MULTIPLIERS, TargetLotStages
from .directional_risk import DirectionalRiskGovernor
from .directional_robustness import MarginAwareDirectionalProductionAcceptance

_EPS = 1e-15


def risk_freeze_triggered(completed_returns: Iterable[float]) -> bool:
    """Use the existing governor's exact completed-return trigger semantics."""
    return DirectionalRiskGovernor().scale(completed_returns) < 1.0 - _EPS


def freeze_new_risk_target(
    *,
    current_lots: Mapping[str, int],
    target_lots: Mapping[str, int],
    symbol_products: Mapping[str, str],
    triggered: bool,
) -> dict[str, int]:
    """Freeze new/same-sign increases without delaying risk reduction actions."""
    target = {
        str(symbol): int(volume)
        for symbol, volume in target_lots.items()
        if int(volume) != 0
    }
    if not triggered:
        return dict(target)

    current = {
        str(symbol): int(volume)
        for symbol, volume in current_lots.items()
        if int(volume) != 0
    }
    products = {
        str(symbol): str(product).upper()
        for symbol, product in symbol_products.items()
    }
    by_product: dict[str, list[str]] = {}
    for symbol in current:
        product = products.get(symbol)
        if not product:
            raise ValueError(f"missing current symbol product: {symbol}")
        by_product.setdefault(product, []).append(symbol)

    result: dict[str, int] = {}
    for symbol, wanted in sorted(target.items()):
        product = products.get(symbol)
        if not product:
            raise ValueError(f"missing target symbol product: {symbol}")

        have = int(current.get(symbol, 0))
        if have != 0:
            # Reversals and same-sign reductions remain exact targets.
            if (have > 0) != (wanted > 0) or abs(wanted) <= abs(have):
                result[symbol] = wanted
            else:
                # Same-contract, same-sign increase is the only incumbent action frozen.
                result[symbol] = have
            continue

        # A target on a different symbol of an already-held product is a contract roll,
        # not a fresh product-level risk entry, and must remain executable.
        if by_product.get(product):
            result[symbol] = wanted
            continue

        # No incumbent product exposure: suppress the new risk entry.

    return {symbol: volume for symbol, volume in result.items() if volume != 0}


@dataclass(frozen=True)
class _UnitScaleGovernor:
    """Expose raw weights to target construction while defense is applied as a freeze."""

    lookback_days: int = 2
    volatility_trigger: float = 0.03
    loss_trigger: float = 0.02
    defensive_scale: float = 1.0

    def scale(self, completed_returns: Iterable[float]) -> float:
        del completed_returns
        return 1.0


class FreezeNewRiskDirectionalProductionAcceptance(
    MarginAwareDirectionalProductionAcceptance
):
    """Production-mechanics research adapter with freeze-only soft defense."""

    def __init__(self, config=None) -> None:
        super().__init__(config)
        self._freeze_trigger_governor = DirectionalRiskGovernor()
        # The simulator multiplies the input weight row by ``risk_governor.scale`` before
        # calling target construction. Keep that stage at 1x and apply the unchanged
        # trigger below as a discrete freeze so existing positions are not churned to 0.25x.
        self.risk_governor = _UnitScaleGovernor(
            lookback_days=self._freeze_trigger_governor.lookback_days,
            volatility_trigger=self._freeze_trigger_governor.volatility_trigger,
            loss_trigger=self._freeze_trigger_governor.loss_trigger,
        )

    def freeze_triggered(self, completed_returns: Iterable[float]) -> bool:
        return self._freeze_trigger_governor.scale(completed_returns) < 1.0 - _EPS

    def target_lot_stages(
        self,
        *,
        equity: float,
        product_weights: Mapping[str, float],
        product_open_prices: Mapping[str, float],
        selected_symbols: Mapping[str, str],
        current_lots: Mapping[str, int] | None = None,
        completed_returns: tuple[float, ...] = (),
    ) -> TargetLotStages:
        baseline = super().target_lot_stages(
            equity=equity,
            product_weights=product_weights,
            product_open_prices=product_open_prices,
            selected_symbols=selected_symbols,
            current_lots=current_lots,
            completed_returns=completed_returns,
        )
        triggered = self.freeze_triggered(completed_returns)
        if not triggered:
            return baseline

        current = {
            str(symbol): int(volume)
            for symbol, volume in (current_lots or {}).items()
            if int(volume) != 0
        }
        symbols = set(current) | set(baseline.final_lots)
        symbol_products = {symbol: self._product(symbol) for symbol in symbols}
        frozen = freeze_new_risk_target(
            current_lots=current,
            target_lots=baseline.final_lots,
            symbol_products=symbol_products,
            triggered=True,
        )

        product_for_symbol = {
            str(symbol): str(product).upper()
            for product, symbol in selected_symbols.items()
        }
        final_notional = 0.0
        for symbol, volume in frozen.items():
            product = product_for_symbol.get(str(symbol), self._product(symbol))
            price = float(product_open_prices.get(product, 0.0))
            multiplier = PRODUCT_MULTIPLIERS.get(product)
            if price <= 0.0 or multiplier is None:
                # A current incumbent that is unavailable in today's selected target is
                # not created by this stage; fail closed rather than invent valuation.
                continue
            final_notional += abs(int(volume)) * price * float(multiplier)

        return TargetLotStages(
            raw_integer_lots=dict(baseline.raw_integer_lots),
            margin_fitted_lots=dict(baseline.margin_fitted_lots),
            final_lots=dict(frozen),
            desired_notional=float(baseline.desired_notional),
            raw_integer_notional=float(baseline.raw_integer_notional),
            margin_fitted_notional=float(baseline.margin_fitted_notional),
            final_notional=float(final_notional),
            integer_rounding_loss_notional=float(
                baseline.integer_rounding_loss_notional
            ),
            max_volume_clipping_notional=float(
                baseline.max_volume_clipping_notional
            ),
            unavailable_contract_notional=float(
                baseline.unavailable_contract_notional
            ),
            soft_margin_share=baseline.soft_margin_share,
        )
