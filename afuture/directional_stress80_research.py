"""Research-only causal Product x Alpha allocation for the Stress-80 experiment.

The module deliberately reuses the frozen execution-aligned template library and the
existing margin-aware Production simulator.  It does not own account, risk, order, fill,
or position truth and is not imported by live runtime wiring.
"""
from __future__ import annotations

from math import isfinite
from typing import Mapping

import numpy as np
import pandas as pd

from .directional_acceptance import PRODUCT_MULTIPLIERS, TargetLotStages
from .directional_causal_edge import estimate_product_family_edge
from .directional_net_edge_allocator import (
    NetEdgeAllocationResult,
    optimize_net_edge_targets,
)
from .directional_opportunity_ledger import ALLOWED_HORIZONS
from .directional_robustness import MarginAwareDirectionalProductionAcceptance
from .execution_aligned_policy import (
    MAX_ABS_DAILY_RETURN,
    _EXECUTION_TEMPLATES,
    _clean_prices,
    _template_weight_path,
)

ALLOWED_FAMILIES = (
    "breakout",
    "tsmom",
    "momentum",
    "moving_average",
    "reversal",
    "acceleration",
)
MAX_GROSS_LEVERAGE = 2.0
_EPS = 1e-15


def family_signal_history(
    open_prices: pd.DataFrame,
    close: pd.DataFrame,
    *,
    products: tuple[str, ...],
) -> dict[str, pd.DataFrame]:
    """Aggregate the frozen 96 template paths to six family-level signal paths."""
    close = _clean_prices(close, products)
    # Keep the same point-in-time input contract as the execution-aligned policy even
    # though the template signal functions themselves are close-history based.
    _clean_prices(open_prices, products).reindex(close.index)
    returns = close.pct_change(fill_method=None)
    returns = returns.mask(returns.abs() > MAX_ABS_DAILY_RETURN)

    grouped: dict[str, list[pd.DataFrame]] = {
        family: [] for family in ALLOWED_FAMILIES
    }
    for template in _EXECUTION_TEMPLATES:
        family = str(template.family)
        if family not in grouped:
            raise ValueError(f"unexpected frozen directional family: {family}")
        grouped[family].append(_template_weight_path(returns, template))

    result: dict[str, pd.DataFrame] = {}
    for family in ALLOWED_FAMILIES:
        paths = grouped[family]
        if not paths:
            raise ValueError(f"frozen template library has no {family} paths")
        aggregate = paths[0].copy()
        for path in paths[1:]:
            aggregate = aggregate.add(path, fill_value=0.0)
        aggregate = aggregate / float(len(paths))
        gross = aggregate.abs().sum(axis=1)
        if bool((gross > MAX_GROSS_LEVERAGE + 1e-10).any()):
            raise AssertionError(f"{family} family path exceeded 2x gross")
        result[family] = aggregate
    return result


def composite_family_edge(
    ledger: pd.DataFrame,
    *,
    decision_date,
    product: str,
    family: str,
) -> float:
    """Equal-average available 5/10/20-session causal edges on a per-session basis."""
    values: list[float] = []
    for horizon in ALLOWED_HORIZONS:
        estimate = estimate_product_family_edge(
            ledger,
            decision_date=decision_date,
            product=product,
            family=family,
            horizon=horizon,
        )
        if estimate.insufficient_evidence:
            continue
        value = float(estimate.expected_gross_return)
        if isfinite(value):
            values.append(value / float(horizon))
    return float(np.mean(values)) if values else float("nan")


def combine_family_edge_signals(
    *,
    family_signals: Mapping[str, Mapping[str, float]],
    family_edges: Mapping[tuple[str, str], float],
) -> tuple[dict[str, float], dict[str, float]]:
    """Convert positive causal family edge into <=2x product directional intent.

    Negative or zero edge suppresses a family contribution.  It can never reverse a
    raw family signal.  Product edge is an exposure-weighted average of the positive
    family edges that actually contributed to that product on the decision date.
    """
    product_scores: dict[str, float] = {}
    edge_numerator: dict[str, float] = {}
    edge_denominator: dict[str, float] = {}
    for family in ALLOWED_FAMILIES:
        signals = family_signals.get(family, {})
        for raw_product, raw_signal in signals.items():
            product = str(raw_product).upper()
            try:
                signal = float(raw_signal)
                edge = float(family_edges.get((product, family), float("nan")))
            except (TypeError, ValueError):
                continue
            if not isfinite(signal) or abs(signal) <= _EPS:
                continue
            if not isfinite(edge) or edge <= 0.0:
                continue
            product_scores[product] = product_scores.get(product, 0.0) + signal * edge
            exposure = abs(signal)
            edge_numerator[product] = edge_numerator.get(product, 0.0) + exposure * edge
            edge_denominator[product] = edge_denominator.get(product, 0.0) + exposure

    product_scores = {
        product: value
        for product, value in product_scores.items()
        if isfinite(float(value)) and abs(float(value)) > _EPS
    }
    gross_score = float(sum(abs(value) for value in product_scores.values()))
    if gross_score <= _EPS:
        return {}, {}
    weights = {
        product: float(value) * MAX_GROSS_LEVERAGE / gross_score
        for product, value in sorted(product_scores.items())
    }
    edges = {
        product: float(edge_numerator[product] / edge_denominator[product])
        for product in weights
        if edge_denominator.get(product, 0.0) > 0.0
    }
    if bool(sum(abs(value) for value in weights.values()) > MAX_GROSS_LEVERAGE + 1e-10):
        raise AssertionError("Stress80 combined family weights exceeded 2x gross")
    return weights, edges


class Stress80DirectionalProductionAcceptance(MarginAwareDirectionalProductionAcceptance):
    """Research adapter that changes target allocation inside the validated envelope."""

    def __init__(
        self,
        expected_product_returns_by_day: pd.DataFrame,
        config=None,
    ) -> None:
        super().__init__(config)
        edges = expected_product_returns_by_day.copy()
        edges.index = pd.to_datetime(edges.index, errors="coerce").normalize()
        edges = edges.loc[~edges.index.isna()]
        edges.columns = [str(column).upper() for column in edges.columns]
        self.expected_product_returns_by_day = edges.sort_index().apply(
            pd.to_numeric, errors="coerce"
        )
        self._stress80_decision_day: pd.Timestamp | None = None
        self._stress80_cost_rate = 0.0
        self.last_stress80_allocation: NetEdgeAllocationResult | None = None

    def simulate(self, raw, weights, *, cost_bps: float, prepared=None):
        self._stress80_decision_day = None
        self._stress80_cost_rate = float(cost_bps) / 10000.0
        self.last_stress80_allocation = None
        return super().simulate(raw, weights, cost_bps=cost_bps, prepared=prepared)

    def _select_contracts_from_snapshot(
        self,
        snapshot: pd.DataFrame,
        target_day: pd.Timestamp,
        preferred_symbols: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        self._stress80_decision_day = pd.Timestamp(target_day).normalize()
        return super()._select_contracts_from_snapshot(
            snapshot,
            target_day,
            preferred_symbols=preferred_symbols,
        )

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
        day = self._stress80_decision_day
        requested = dict(baseline.raw_integer_lots)
        reference = dict(baseline.final_lots)
        if (
            day is None
            or not requested
            or baseline.soft_margin_share is None
            or equity <= 0.0
            or self.expected_product_returns_by_day.empty
            or day not in self.expected_product_returns_by_day.index
        ):
            return baseline

        symbol_products = {
            str(symbol): str(product).upper()
            for product, symbol in selected_symbols.items()
            if str(symbol) in requested
        }
        if set(requested) - set(symbol_products):
            return baseline

        lot_notionals: dict[str, float] = {}
        per_lot_margin: dict[str, float] = {}
        for symbol, product in symbol_products.items():
            price = float(product_open_prices.get(product, 0.0))
            multiplier = PRODUCT_MULTIPLIERS.get(product)
            if price <= 0.0 or multiplier is None:
                return baseline
            notional = price * float(multiplier)
            lot_notionals[symbol] = notional
            per_lot_margin[symbol] = (
                notional
                * float(self.config.margin_rate_proxy)
                * float(self.config.margin_estimate_buffer)
            )

        current = {
            str(symbol): int(volume)
            for symbol, volume in (current_lots or {}).items()
            if int(volume)
        }
        # On a concrete-contract roll mismatch, exact fallback is safer than inventing
        # notional/margin evidence for the incumbent contract.
        if not set(current).issubset(lot_notionals):
            return baseline

        edge_row = self.expected_product_returns_by_day.loc[day]
        if isinstance(edge_row, pd.DataFrame):
            edge_row = edge_row.iloc[-1]
        expected: dict[str, float] = {}
        for product in sorted(set(symbol_products.values())):
            if product not in edge_row.index:
                return baseline
            value = float(edge_row[product])
            if not isfinite(value):
                return baseline
            # This is a direction-conditioned value.  Negative edge may suppress risk,
            # but must never invert the requested directional signal.
            expected[product] = max(0.0, value)

        result = optimize_net_edge_targets(
            reference_lots=reference,
            requested_lots=requested,
            current_lots=current,
            symbol_products=symbol_products,
            lot_notionals=lot_notionals,
            per_lot_margin=per_lot_margin,
            equity=float(equity),
            soft_margin_budget=float(equity) * float(baseline.soft_margin_share),
            max_gross_ratio=float(self.config.max_realized_gross_ratio),
            max_abs_lots=min(int(self.config.max_contract_volume), 35),
            expected_product_returns=expected,
            cost_rate=float(self._stress80_cost_rate),
        )
        self.last_stress80_allocation = result
        if result.optimization.fallback_reason:
            return baseline
        final = dict(result.optimization.target_lots)

        def gross(lots: Mapping[str, int]) -> float:
            return float(
                sum(
                    abs(int(volume)) * lot_notionals[str(symbol)]
                    for symbol, volume in lots.items()
                )
            )

        return TargetLotStages(
            raw_integer_lots=dict(baseline.raw_integer_lots),
            margin_fitted_lots=dict(baseline.margin_fitted_lots),
            final_lots=final,
            desired_notional=baseline.desired_notional,
            raw_integer_notional=baseline.raw_integer_notional,
            margin_fitted_notional=baseline.margin_fitted_notional,
            final_notional=gross(final),
            integer_rounding_loss_notional=baseline.integer_rounding_loss_notional,
            max_volume_clipping_notional=baseline.max_volume_clipping_notional,
            unavailable_contract_notional=baseline.unavailable_contract_notional,
            soft_margin_share=baseline.soft_margin_share,
        )
