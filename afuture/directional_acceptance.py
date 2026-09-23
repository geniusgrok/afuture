"""Deterministic production-mechanics proxy for the frozen directional portfolio.

This module deliberately does not search Alpha or parameters. It translates frozen
product weights into integer contract lots and applies production-style account hard
gates. Historical broker margin schedules are unavailable, so margin is explicitly a
proxy assumption rather than claimed exact CTP history.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from math import floor, isfinite

import pandas as pd

from .directional import RebalancePlan
from .directional_attribution import (
    AUDIT_EVENT_COLUMNS,
    classify_rebalance_action,
    exposure_side,
)
from .directional_data_validation import (
    validate_daily_index,
    validate_finite_columns,
    validate_unique_keys,
)
from .directional_efficiency import attribute_rebalance_deltas
from .directional_risk import DirectionalRiskGovernor, DirectionalRiskScale
from .directional_stress90_planner import (
    Stress90LotStages,
    build_stress90_rebalance_stages,
)
from .models import AccountSnapshot, ContractSpec, Tick

PRODUCT_MULTIPLIERS: dict[str, float] = {
    "A": 10.0,
    "B": 10.0,
    "C": 10.0,
    "CS": 10.0,
    "M": 10.0,
    "P": 10.0,
    "Y": 10.0,
    "OI": 10.0,
    "RM": 10.0,
    "SR": 10.0,
    "TA": 10.0,
    "MA": 10.0,
    "AG": 15.0,
    "AL": 5.0,
    "CU": 5.0,
    "PB": 5.0,
    "ZN": 5.0,
    "AU": 1000.0,
    "AP": 10.0,
    "BC": 5.0,
    "BU": 10.0,
    "FU": 10.0,
    "HC": 10.0,
    "RB": 10.0,
    "RU": 10.0,
    "SP": 10.0,
    "CF": 5.0,
    "CJ": 5.0,
    "EB": 5.0,
    "EG": 10.0,
    "FG": 20.0,
    "I": 100.0,
    "J": 100.0,
    "JM": 60.0,
    "L": 5.0,
    "PP": 5.0,
    "V": 5.0,
    "PF": 5.0,
    "PK": 5.0,
    "SF": 5.0,
    "SM": 5.0,
    "SS": 5.0,
    "LH": 16.0,
    "LU": 10.0,
    "NR": 10.0,
    "NI": 1.0,
    "SN": 1.0,
    "PG": 20.0,
    "SA": 20.0,
    "UR": 20.0,
}


@dataclass(frozen=True)
class ProductionMechanicsConfig:
    initial_capital: float = 500000.0
    margin_rate_proxy: float = 0.12
    margin_estimate_buffer: float = 1.25
    max_margin_ratio: float = 0.35
    min_available_ratio: float = 0.25
    max_contract_volume: int = 35
    max_daily_loss_ratio: float = 0.05
    max_total_drawdown_ratio: float = 0.30
    max_realized_gross_ratio: float = 2.0
    min_days_to_delivery: int = 20
    min_volume: float = 1000.0
    min_open_interest: float = 5000.0

    def validate(self) -> None:
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if not 0 < self.margin_rate_proxy < 1:
            raise ValueError("margin_rate_proxy must be in (0, 1)")
        if self.margin_estimate_buffer < 1:
            raise ValueError("margin_estimate_buffer must be >= 1")
        if not 0 < self.max_margin_ratio < 1:
            raise ValueError("max_margin_ratio must be in (0, 1)")
        if not 0 <= self.min_available_ratio < 1:
            raise ValueError("min_available_ratio must be in [0, 1)")
        if self.max_contract_volume <= 0:
            raise ValueError("max_contract_volume must be positive")
        if not 0 < self.max_daily_loss_ratio < 1:
            raise ValueError("max_daily_loss_ratio must be in (0, 1)")
        if not 0 < self.max_total_drawdown_ratio < 1:
            raise ValueError("max_total_drawdown_ratio must be in (0, 1)")
        if self.max_realized_gross_ratio <= 0:
            raise ValueError("max_realized_gross_ratio must be positive")


@dataclass(frozen=True)
class TargetLotStages:
    """Audit-only target construction stages; ``final_lots`` remains authoritative."""

    raw_integer_lots: dict[str, int]
    margin_fitted_lots: dict[str, int]
    final_lots: dict[str, int]
    desired_notional: float
    raw_integer_notional: float
    margin_fitted_notional: float
    final_notional: float
    integer_rounding_loss_notional: float
    max_volume_clipping_notional: float
    unavailable_contract_notional: float
    soft_margin_share: float | None = None


@dataclass(frozen=True)
class DirectionalSimulationCheckpoint:
    """Serializable end-of-session state for resuming a historical account proxy."""

    last_day: pd.Timestamp
    equity: float
    high_watermark: float
    lots: tuple[tuple[str, int], ...]
    previous_close: tuple[tuple[str, float], ...]
    completed_returns: tuple[float, ...]
    halted: bool
    first_divergence: str
    strategy_state: tuple[tuple[str, object], ...]
    configuration_digest: str

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "last_day": pd.Timestamp(self.last_day).strftime("%Y-%m-%d"),
            "equity": self.equity,
            "high_watermark": self.high_watermark,
            "lots": dict(self.lots),
            "previous_close": dict(self.previous_close),
            "completed_returns": list(self.completed_returns),
            "halted": self.halted,
            "first_divergence": self.first_divergence,
            "strategy_state": {
                name: list(value) if isinstance(value, tuple) else value
                for name, value in self.strategy_state
            },
            "configuration_digest": self.configuration_digest,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> DirectionalSimulationCheckpoint:
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported directional simulation checkpoint schema")
        try:
            lots_raw = payload["lots"]
            previous_close_raw = payload["previous_close"]
            completed_returns_raw = payload["completed_returns"]
            if not isinstance(lots_raw, Mapping):
                raise TypeError("lots must be a mapping")
            if not isinstance(previous_close_raw, Mapping):
                raise TypeError("previous_close must be a mapping")
            if not isinstance(completed_returns_raw, (list, tuple)):
                raise TypeError("completed_returns must be a sequence")
            strategy_state = payload["strategy_state"]
            if not isinstance(strategy_state, Mapping):
                raise TypeError("strategy_state must be a mapping")
            equity = float(str(payload["equity"]))
            high_watermark = float(str(payload["high_watermark"]))
            if not isfinite(equity) or not isfinite(high_watermark):
                raise ValueError("checkpoint equity values must be finite")
            return cls(
                last_day=pd.Timestamp(str(payload["last_day"])).normalize(),
                equity=equity,
                high_watermark=high_watermark,
                lots=tuple(
                    sorted((str(symbol), int(str(volume))) for symbol, volume in lots_raw.items())
                ),
                previous_close=tuple(
                    sorted(
                        (str(symbol), float(str(price)))
                        for symbol, price in previous_close_raw.items()
                    )
                ),
                completed_returns=tuple(float(str(value)) for value in completed_returns_raw),
                halted=bool(payload["halted"]),
                first_divergence=str(payload["first_divergence"]),
                strategy_state=tuple(
                    sorted(
                        (
                            str(name),
                            tuple(value) if isinstance(value, (list, tuple)) else value,
                        )
                        for name, value in strategy_state.items()
                    )
                ),
                configuration_digest=str(payload["configuration_digest"]),
            )
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            raise ValueError("invalid directional simulation checkpoint") from exc


@dataclass(frozen=True)
class ProductionSimulationResult:
    daily: pd.DataFrame
    events: pd.DataFrame
    final_equity: float
    first_divergence: str = ""
    final_checkpoint: DirectionalSimulationCheckpoint | None = None


@dataclass(frozen=True)
class PreparedDirectionalContracts:
    """Immutable-by-convention indexes shared across independent account simulations."""

    frame: pd.DataFrame
    by_day_symbol: Mapping[tuple[pd.Timestamp, str], pd.Series]
    activity_by_day: Mapping[pd.Timestamp, pd.DataFrame]
    available_activity_days: pd.DatetimeIndex


class Stress90ProductionAcceptance:
    """Offline adapter that delegates every Stress-90 lot stage to production code."""

    def __init__(self, config: ProductionMechanicsConfig | None = None) -> None:
        self.config = config or ProductionMechanicsConfig()
        self.config.validate()

    def target_lot_stages(
        self,
        *,
        equity: float,
        product_weights: Mapping[str, float],
        product_open_prices: Mapping[str, float],
        selected_symbols: Mapping[str, str],
        live_margin_rates: Mapping[str, tuple[float, float]],
        current_lots: Mapping[str, int] | None = None,
        completed_returns: tuple[float, ...] = (),
        drawdown_reserve_freeze: bool,
        concentration_freeze: bool,
        unavailable_products: tuple[str, ...] = (),
        entry_blocked_products: tuple[str, ...] = (),
        authorized_transition_products: tuple[str, ...] = (),
    ) -> Stress90LotStages:
        if equity <= 0:
            raise ValueError("Stress-90 acceptance equity must be positive")
        ticks: dict[str, Tick] = {}
        specs: dict[str, ContractSpec] = {}
        symbol_products: dict[str, str] = {}
        for raw_product, raw_symbol in selected_symbols.items():
            product = str(raw_product).upper()
            symbol = str(raw_symbol)
            price = float(product_open_prices.get(product, 0.0))
            rates = live_margin_rates.get(symbol)
            multiplier = PRODUCT_MULTIPLIERS.get(product)
            if price <= 0 or rates is None or multiplier is None:
                raise ValueError(f"missing Stress-90 acceptance mechanics: {product}")
            long_rate, short_rate = (float(rates[0]), float(rates[1]))
            ticks[product] = Tick(
                symbol=symbol,
                exchange="DCE",
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
                bid_price=price,
                ask_price=price,
                last_price=price,
                bid_volume=1.0,
                ask_volume=1.0,
                trading_day="20260101",
            )
            specs[symbol] = ContractSpec(
                symbol=symbol,
                exchange="DCE",
                multiplier=float(multiplier),
                price_tick=1.0,
                margin_rate_long=long_rate,
                margin_rate_short=short_rate,
            )
            symbol_products[symbol] = product
        for symbol in current_lots or {}:
            symbol_products.setdefault(symbol, self._product(symbol))
        account = AccountSnapshot(
            balance=float(equity),
            equity=float(equity),
            available=float(equity),
            margin=0.0,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            trading_day="20260101",
        )
        return build_stress90_rebalance_stages(
            account=account,
            product_weights=product_weights,
            product_ticks=ticks,
            specs=specs,
            current_lots=current_lots or {},
            symbol_products=symbol_products,
            max_contract_volume=self.config.max_contract_volume,
            max_gross_leverage=self.config.max_realized_gross_ratio,
            max_margin_ratio=self.config.max_margin_ratio,
            min_available_ratio=self.config.min_available_ratio,
            max_daily_loss_ratio=self.config.max_daily_loss_ratio,
            margin_estimate_buffer=self.config.margin_estimate_buffer,
            completed_returns=completed_returns,
            drawdown_reserve_freeze=drawdown_reserve_freeze,
            concentration_freeze=concentration_freeze,
            unavailable_products=unavailable_products,
            entry_blocked_products=entry_blocked_products,
            authorized_transition_products=authorized_transition_products,
        )

    @staticmethod
    def _product(symbol: str) -> str:
        product = "".join(char for char in str(symbol).upper() if char.isalpha())
        if product not in PRODUCT_MULTIPLIERS:
            raise ValueError(f"unknown frozen product multiplier: {symbol}")
        return product


class DirectionalProductionAcceptance:
    """Pure deterministic mechanics used by tests and the final L4 proxy tool."""

    def __init__(self, config: ProductionMechanicsConfig | None = None) -> None:
        self.config = config or ProductionMechanicsConfig()
        self.config.validate()
        self.risk_governor: DirectionalRiskScale = DirectionalRiskGovernor()

    def retain_completed_returns(self, completed_returns: list[float]) -> list[float]:
        """Keep the default governor's exact two-completed-session state."""
        return list(completed_returns[-2:])

    def observe_target_state(
        self,
        *,
        day: pd.Timestamp,
        product_weights: Mapping[str, float],
    ) -> None:
        """Behavior-neutral hook for adapters using exogenous target state."""
        del day, product_weights

    def _checkpoint_strategy_state(self) -> dict[str, object]:
        return {}

    def _restore_checkpoint_strategy_state(self, state: Mapping[str, object]) -> None:
        if state:
            raise ValueError("checkpoint contains unsupported strategy state")

    def _checkpoint_configuration_digest(self, cost_bps: float) -> str:
        payload = {
            "simulator": f"{type(self).__module__}.{type(self).__qualname__}",
            "config": asdict(self.config),
            "cost_bps": float(cost_bps),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _product(symbol: str) -> str:
        value = str(symbol).upper()
        prefix = "".join(char for char in value if char.isalpha())
        if prefix not in PRODUCT_MULTIPLIERS:
            raise ValueError(f"unknown frozen product multiplier: {symbol}")
        return prefix

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
        """Expose integer construction losses without changing target-lot behavior."""
        if equity <= 0:
            return TargetLotStages({}, {}, {}, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        result: dict[str, int] = {}
        desired_notional = 0.0
        integer_rounding_loss = 0.0
        max_volume_clipping = 0.0
        unavailable_contract = 0.0
        for raw_product, raw_weight in sorted(product_weights.items()):
            product = str(raw_product).upper()
            weight = float(raw_weight)
            if abs(weight) <= 1e-15:
                continue
            desired = float(equity) * abs(weight)
            desired_notional += desired
            symbol = selected_symbols.get(product)
            price = float(product_open_prices.get(product, 0.0))
            multiplier = PRODUCT_MULTIPLIERS.get(product)
            if symbol is None or multiplier is None or price <= 0:
                unavailable_contract += desired
                continue
            lot_notional = price * float(multiplier)
            unconstrained_lots = floor(desired / lot_notional)
            integer_rounding_loss += desired - unconstrained_lots * lot_notional
            clipped_lots = min(self.config.max_contract_volume, unconstrained_lots)
            if unconstrained_lots > clipped_lots:
                max_volume_clipping += (unconstrained_lots - clipped_lots) * lot_notional
            if clipped_lots > 0:
                result[str(symbol)] = clipped_lots if weight > 0 else -clipped_lots
        integer_notional = (
            desired_notional - integer_rounding_loss - max_volume_clipping - unavailable_contract
        )
        integer_notional = max(0.0, float(integer_notional))
        return TargetLotStages(
            raw_integer_lots=dict(result),
            margin_fitted_lots=dict(result),
            final_lots=dict(result),
            desired_notional=float(desired_notional),
            raw_integer_notional=integer_notional,
            margin_fitted_notional=integer_notional,
            final_notional=integer_notional,
            integer_rounding_loss_notional=float(integer_rounding_loss),
            max_volume_clipping_notional=float(max_volume_clipping),
            unavailable_contract_notional=float(unavailable_contract),
        )

    def target_lots(
        self,
        *,
        equity: float,
        product_weights: Mapping[str, float],
        product_open_prices: Mapping[str, float],
        selected_symbols: Mapping[str, str],
        current_lots: Mapping[str, int] | None = None,
        completed_returns: tuple[float, ...] = (),
    ) -> dict[str, int]:
        return self.target_lot_stages(
            equity=equity,
            product_weights=product_weights,
            product_open_prices=product_open_prices,
            selected_symbols=selected_symbols,
            current_lots=current_lots,
            completed_returns=completed_returns,
        ).final_lots

    @staticmethod
    def rebalance_plan(
        *, current_lots: Mapping[str, int], target_lots: Mapping[str, int]
    ) -> RebalancePlan:
        reductions: dict[str, int] = {}
        openings: dict[str, int] = {}
        for symbol in sorted(set(current_lots) | set(target_lots)):
            have = int(current_lots.get(symbol, 0))
            target = int(target_lots.get(symbol, 0))
            if have == target:
                continue
            if have == 0:
                if target:
                    openings[symbol] = target
                continue
            if target == 0 or (have > 0) != (target > 0):
                reductions[symbol] = -have
                continue
            if abs(target) < abs(have):
                reductions[symbol] = target - have
            elif abs(target) > abs(have):
                openings[symbol] = target - have
        return RebalancePlan(
            reductions=reductions,
            openings={} if reductions else openings,
        )

    def check_opening_batch(
        self,
        *,
        equity: float,
        current_margin: float,
        current_lots: Mapping[str, int],
        openings: Mapping[str, int],
        open_prices: Mapping[str, float],
    ) -> tuple[bool, str, float]:
        if equity <= 0:
            return False, "equity is not positive", 0.0
        estimated = 0.0
        for symbol, delta in openings.items():
            requested = abs(int(delta))
            if requested <= 0:
                continue
            existing = abs(int(current_lots.get(symbol, 0)))
            if existing + requested > self.config.max_contract_volume:
                return False, "contract volume limit reached", estimated
            price = float(open_prices.get(symbol, 0.0))
            if price <= 0:
                return False, f"missing opening price: {symbol}", estimated
            multiplier = PRODUCT_MULTIPLIERS[self._product(symbol)]
            estimated += (
                price
                * multiplier
                * requested
                * self.config.margin_rate_proxy
                * self.config.margin_estimate_buffer
            )
        post_margin = float(current_margin) + estimated
        if post_margin / equity > self.config.max_margin_ratio:
            return False, "combined margin ratio would exceed limit", float(estimated)
        if (equity - post_margin) / equity < self.config.min_available_ratio:
            return False, "combined cash reserve would fall below limit", float(estimated)
        return True, "", float(estimated)

    def account_risk_reason(
        self,
        *,
        equity: float,
        day_start_equity: float,
        high_watermark: float,
        margin: float = 0.0,
    ) -> str:
        if equity <= 0 or day_start_equity <= 0 or high_watermark <= 0:
            return "equity is not positive"
        daily_loss = max(0.0, day_start_equity - equity) / day_start_equity
        drawdown = max(0.0, high_watermark - equity) / high_watermark
        margin_ratio = max(0.0, float(margin)) / equity
        available_ratio = (equity - max(0.0, float(margin))) / equity
        # Hard account solvency/risk gates outrank the recoverable daily circuit.
        # Otherwise a large one-day loss could mask a simultaneous total-drawdown or
        # margin breach and incorrectly make the account eligible to auto-recover.
        if drawdown + 1e-12 >= self.config.max_total_drawdown_ratio:
            return "drawdown limit reached"
        if margin_ratio > self.config.max_margin_ratio:
            return "margin ratio limit reached"
        if available_ratio < self.config.min_available_ratio:
            return "available cash reserve too low"
        if daily_loss + 1e-12 >= self.config.max_daily_loss_ratio:
            return "daily loss limit reached"
        return ""

    @staticmethod
    def _normalize_contracts(raw: pd.DataFrame) -> pd.DataFrame:
        required = {
            "date",
            "delivery",
            "product",
            "symbol",
            "open",
            "close",
            "volume",
            "hold",
        }
        missing = sorted(required.difference(raw.columns))
        if missing:
            raise ValueError("directional contracts missing columns: " + ", ".join(missing))
        frame = raw.copy()
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
        frame["delivery"] = pd.to_datetime(frame["delivery"], errors="coerce")
        frame["product"] = frame["product"].astype(str).str.upper()
        frame["symbol"] = frame["symbol"].astype(str).str.upper()
        for column in ("open", "close", "volume", "hold"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        for column in ("date", "delivery"):
            invalid = frame[column].isna()
            if bool(invalid.any()):
                row = frame.index[invalid][0]
                raise ValueError(
                    f"directional contracts column {column} has invalid date; row={row!r}"
                )
        for column in ("product", "symbol"):
            invalid = frame[column].isin({"", "NAN", "NONE"})
            if bool(invalid.any()):
                row = frame.index[invalid][0]
                raise ValueError(
                    f"directional contracts column {column} cannot be empty; row={row!r}"
                )
        validate_finite_columns(
            frame,
            ("open", "close", "volume", "hold"),
            name="directional contracts",
            positive=("open", "close"),
        )
        for column in ("volume", "hold"):
            invalid = frame[column] < 0
            if bool(invalid.any()):
                row = frame.index[invalid][0]
                raise ValueError(
                    f"directional contracts column {column} cannot be negative; row={row!r}"
                )
        validate_unique_keys(
            frame,
            ("date", "symbol"),
            name="directional contracts",
        )
        return frame

    def prepare_contracts(self, raw: pd.DataFrame) -> PreparedDirectionalContracts:
        frame = self._normalize_contracts(raw)
        by_day_symbol = {
            (pd.Timestamp(day).normalize(), str(symbol)): group.iloc[-1]
            for (day, symbol), group in frame.groupby(["date", "symbol"], sort=False)
        }
        activity_by_day = {
            pd.Timestamp(day).normalize(): group for day, group in frame.groupby("date", sort=False)
        }
        return PreparedDirectionalContracts(
            frame=frame,
            by_day_symbol=by_day_symbol,
            activity_by_day=activity_by_day,
            available_activity_days=pd.DatetimeIndex(sorted(activity_by_day)),
        )

    def _select_contracts_from_snapshot(
        self,
        snapshot: pd.DataFrame,
        target_day: pd.Timestamp,
        preferred_symbols: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        day = pd.Timestamp(target_day).normalize()
        eligible = snapshot[
            (snapshot["delivery"] - day).dt.days >= self.config.min_days_to_delivery
        ]
        eligible = eligible[
            (eligible["volume"] >= self.config.min_volume)
            & (eligible["hold"] >= self.config.min_open_interest)
        ]
        preferred = {str(k).upper(): str(v).upper() for k, v in (preferred_symbols or {}).items()}
        result: dict[str, str] = {}
        for product, rows in eligible.groupby("product"):
            rows = rows.sort_values(
                ["hold", "volume", "delivery", "symbol"], ascending=[False, False, True, True]
            )
            if rows.empty:
                continue
            incumbent_symbol = preferred.get(str(product).upper())
            incumbent_rows = (
                rows[rows["symbol"] == incumbent_symbol] if incumbent_symbol else rows.iloc[0:0]
            )
            if not incumbent_rows.empty:
                incumbent = incumbent_rows.iloc[0]
                dominant = rows[
                    (rows["hold"] > float(incumbent["hold"]))
                    & (rows["volume"] > float(incumbent["volume"]))
                ]
                if dominant.empty:
                    result[str(product)] = str(incumbent["symbol"])
                    continue
                dominant = dominant.sort_values(
                    ["hold", "volume", "delivery", "symbol"], ascending=[False, False, True, True]
                )
                result[str(product)] = str(dominant.iloc[0]["symbol"])
            else:
                result[str(product)] = str(rows.iloc[0]["symbol"])
        return result

    def _select_contracts_from_normalized(
        self, frame: pd.DataFrame, target_day: pd.Timestamp
    ) -> dict[str, str]:
        day = pd.Timestamp(target_day).normalize()
        prior_dates = frame.loc[frame["date"] < day, "date"]
        if prior_dates.empty:
            return {}
        completed = pd.Timestamp(prior_dates.max()).normalize()
        snapshot = frame[frame["date"] == completed]
        return self._select_contracts_from_snapshot(snapshot, day)

    def select_contracts_for_day(
        self, raw: pd.DataFrame, target_day: pd.Timestamp
    ) -> dict[str, str]:
        return self._select_contracts_from_normalized(self._normalize_contracts(raw), target_day)

    def _margin(
        self,
        lots: Mapping[str, int],
        prices: Mapping[str, float],
    ) -> float:
        return float(
            sum(
                abs(int(volume))
                * float(prices.get(symbol, 0.0))
                * PRODUCT_MULTIPLIERS[self._product(symbol)]
                * self.config.margin_rate_proxy
                * self.config.margin_estimate_buffer
                for symbol, volume in lots.items()
            )
        )

    @staticmethod
    def _apply_deltas(lots: dict[str, int], deltas: Mapping[str, int]) -> None:
        for symbol, delta in deltas.items():
            lots[symbol] = int(lots.get(symbol, 0)) + int(delta)
            if lots[symbol] == 0:
                lots.pop(symbol, None)

    def _turnover(
        self,
        deltas: Mapping[str, int],
        prices: Mapping[str, float],
    ) -> float:
        return float(
            sum(
                abs(int(delta))
                * float(prices.get(symbol, 0.0))
                * PRODUCT_MULTIPLIERS[self._product(symbol)]
                for symbol, delta in deltas.items()
            )
        )

    def _attribute_normal_turnover(
        self,
        *,
        original_lots: Mapping[str, int],
        target_lots: Mapping[str, int],
        deltas: Mapping[str, int],
        prices: Mapping[str, float],
    ) -> dict[str, float]:
        symbols = set(original_lots) | set(target_lots) | set(deltas)
        lot_notionals = {
            symbol: float(prices.get(symbol, 0.0)) * PRODUCT_MULTIPLIERS[self._product(symbol)]
            for symbol in symbols
        }
        products = {symbol: self._product(symbol) for symbol in symbols}
        return attribute_rebalance_deltas(
            original_lots=original_lots,
            target_lots=target_lots,
            executed_deltas=deltas,
            lot_notionals=lot_notionals,
            symbol_products=products,
        )

    def _gross_notional(
        self,
        lots: Mapping[str, int],
        prices: Mapping[str, float],
    ) -> float:
        return float(
            sum(
                abs(int(volume))
                * float(prices.get(symbol, 0.0))
                * PRODUCT_MULTIPLIERS[self._product(symbol)]
                for symbol, volume in lots.items()
            )
        )

    def _trade_audit_rows(
        self,
        *,
        day: pd.Timestamp,
        starting_lots: Mapping[str, int],
        deltas: Mapping[str, int],
        prices: Mapping[str, float],
        cost_rate: float,
        original_lots: Mapping[str, int] | None = None,
        target_lots: Mapping[str, int] | None = None,
        action_override: str | None = None,
    ) -> list[dict]:
        """Describe executed deltas after the simulator has already decided them."""
        symbols = set(starting_lots) | set(deltas)
        if original_lots is not None:
            symbols.update(original_lots)
        if target_lots is not None:
            symbols.update(target_lots)
        products = {symbol: self._product(symbol) for symbol in symbols}
        rows: list[dict] = []
        for symbol in sorted(deltas):
            delta = int(deltas[symbol])
            if not delta:
                continue
            before = int(starting_lots.get(symbol, 0))
            after = before + delta
            price = float(prices.get(symbol, 0.0))
            if price <= 0:
                raise ValueError(f"missing positive audit execution price: {symbol}")
            product = products[symbol]
            if action_override is not None:
                action = str(action_override)
            else:
                if original_lots is None or target_lots is None:
                    raise ValueError("normal trade audit requires original and target lots")
                action = classify_rebalance_action(
                    symbol=symbol,
                    original_lots=original_lots,
                    target_lots=target_lots,
                    symbol_products=products,
                )
            turnover = abs(delta) * price * PRODUCT_MULTIPLIERS[product]
            exposure = after if after else before
            rows.append(
                {
                    "date": day,
                    "kind": "trade",
                    "action": action,
                    "product": product,
                    "symbol": symbol,
                    "side": exposure_side(exposure),
                    "lots_before": before,
                    "lots_after": after,
                    "delta_lots": delta,
                    "price": price,
                    "turnover_notional": float(turnover),
                    "transaction_cost": float(turnover * cost_rate),
                    "gross_pnl": 0.0,
                }
            )
        return rows

    def _pnl_audit_row(
        self,
        *,
        day: pd.Timestamp,
        action: str,
        symbol: str,
        volume: int,
        price: float,
        gross_pnl: float,
    ) -> dict:
        return {
            "date": day,
            "kind": "pnl",
            "action": str(action),
            "product": self._product(symbol),
            "symbol": str(symbol),
            "side": exposure_side(int(volume)),
            "lots_before": int(volume),
            "lots_after": int(volume),
            "delta_lots": 0,
            "price": float(price),
            "turnover_notional": 0.0,
            "transaction_cost": 0.0,
            "gross_pnl": float(gross_pnl),
        }

    def realized_gross_reductions(
        self,
        *,
        lots: Mapping[str, int],
        prices: Mapping[str, float],
        equity: float,
        cost_rate: float,
    ) -> dict[str, int]:
        """Return reduction-only deltas required by the observed gross hard ceiling."""
        if equity <= 0 or not lots:
            return {}
        gross = self._gross_notional(lots, prices)
        limit = self.config.max_realized_gross_ratio * float(equity)
        if gross <= limit + 1e-10:
            return {}

        denominator = 1.0 - self.config.max_realized_gross_ratio * float(cost_rate)
        if denominator <= 0:
            return {symbol: -int(volume) for symbol, volume in lots.items()}
        required_reduction = max(0.0, (gross - limit) / denominator)
        retain_scale = max(0.0, 1.0 - required_reduction / gross)

        targets: dict[str, int] = {}
        candidates: list[tuple[float, str, float]] = []
        reduction_notional = 0.0
        for symbol in sorted(lots):
            volume = int(lots[symbol])
            magnitude = abs(volume)
            lot_notional = (
                float(prices.get(symbol, 0.0)) * PRODUCT_MULTIPLIERS[self._product(symbol)]
            )
            if magnitude <= 0 or lot_notional <= 0:
                targets[symbol] = volume
                continue
            ideal = magnitude * retain_scale
            target_magnitude = floor(ideal)
            targets[symbol] = target_magnitude if volume > 0 else -target_magnitude
            reduction_notional += (magnitude - target_magnitude) * lot_notional
            if target_magnitude < magnitude:
                candidates.append((ideal - target_magnitude, symbol, lot_notional))

        # Restore at most one lot per symbol when discrete capacity permits. Ordering by
        # largest fractional remainder keeps the reduction close to proportional while
        # retaining as much frozen exposure as the hard ceiling allows.
        for _, symbol, lot_notional in sorted(candidates, key=lambda item: (-item[0], item[1])):
            if reduction_notional - lot_notional + 1e-10 < required_reduction:
                continue
            volume = int(lots[symbol])
            targets[symbol] += 1 if volume > 0 else -1
            reduction_notional -= lot_notional

        reductions = {
            symbol: int(targets[symbol]) - int(lots[symbol])
            for symbol in sorted(lots)
            if int(targets[symbol]) != int(lots[symbol])
        }
        return reductions

    def simulate(
        self,
        raw: pd.DataFrame,
        weights: pd.DataFrame,
        *,
        cost_bps: float,
        prepared: PreparedDirectionalContracts | None = None,
        checkpoint: DirectionalSimulationCheckpoint | Mapping[str, object] | None = None,
    ) -> ProductionSimulationResult:
        cost_bps = float(cost_bps)
        if not isfinite(cost_bps) or cost_bps < 0:
            raise ValueError("cost_bps must be finite and non-negative")
        context = prepared or self.prepare_contracts(raw)
        weight_frame = weights.copy()
        weight_frame.index = pd.to_datetime(weight_frame.index, errors="coerce").normalize()
        validate_daily_index(
            weight_frame,
            name="directional target weights",
        )
        numeric_weights = weight_frame.apply(
            pd.to_numeric,
            errors="coerce",
        )
        nonnumeric = numeric_weights.isna() & weight_frame.notna()
        if bool(nonnumeric.any(axis=None)):
            row, column = nonnumeric.stack().loc[lambda item: item].index[0]
            raise ValueError(
                f"directional target weights must be numeric; row={row!r}, column={column!r}"
            )
        weight_frame = numeric_weights.sort_index().fillna(0.0)
        validate_finite_columns(
            weight_frame,
            tuple(str(column) for column in weight_frame.columns),
            name="directional target weights",
        )
        weight_frame.columns = [str(column).upper() for column in weight_frame.columns]
        if bool((weight_frame.abs().sum(axis=1) > 2.0 + 1e-10).any()):
            raise ValueError("production mechanics weights exceed 2x gross")

        if isinstance(checkpoint, Mapping):
            checkpoint = DirectionalSimulationCheckpoint.from_dict(checkpoint)
        if checkpoint is not None:
            if not isinstance(checkpoint, DirectionalSimulationCheckpoint):
                raise ValueError("invalid directional simulation checkpoint")
            if checkpoint.configuration_digest != self._checkpoint_configuration_digest(cost_bps):
                raise ValueError("checkpoint configuration does not match this simulation")
            if (
                not weight_frame.empty
                and pd.Timestamp(weight_frame.index[0]) <= checkpoint.last_day
            ):
                raise ValueError("resumed simulation dates must follow the checkpoint day")
            equity = float(checkpoint.equity)
            high_watermark = float(checkpoint.high_watermark)
            lots = dict(checkpoint.lots)
            previous_close = dict(checkpoint.previous_close)
            completed_returns = list(checkpoint.completed_returns)
            halted = bool(checkpoint.halted)
            first_divergence = str(checkpoint.first_divergence)
            self._restore_checkpoint_strategy_state(dict(checkpoint.strategy_state))
        else:
            equity = float(self.config.initial_capital)
            high_watermark = equity
            lots = {}
            previous_close = {}
            completed_returns = []
            halted = False
            first_divergence = ""

        by_day_symbol = context.by_day_symbol
        activity_by_day = context.activity_by_day
        available_activity_days = context.available_activity_days
        output_rows: list[dict] = []
        event_rows: list[dict] = []
        cost_rate = cost_bps / 10000.0

        for day, weight_row in weight_frame.iterrows():
            day = pd.Timestamp(day).normalize()
            previous_equity = equity
            day_start_equity = previous_equity
            turnover_notional = 0.0
            turnover_roll = 0.0
            turnover_resize = 0.0
            turnover_reversal = 0.0
            turnover_entry = 0.0
            turnover_exit = 0.0
            turnover_daily_circuit = 0.0
            turnover_hard_halt = 0.0
            turnover_gross_guard = 0.0
            normal_original_lots: dict[str, int] = {}
            normal_target_lots: dict[str, int] = {}
            normal_deltas: dict[str, int] = {}
            risk_reason = ""
            margin_reject = ""
            daily_circuit = False
            gross_guard = False
            raw_product_weights = {
                str(product).upper(): float(value) for product, value in weight_row.items()
            }
            self.observe_target_state(
                day=day,
                product_weights=raw_product_weights,
            )
            risk_scale = self.risk_governor.scale(completed_returns)
            raw_target_gross_ratio = float(weight_row.abs().sum())
            governor_target_gross_ratio = raw_target_gross_ratio * float(risk_scale)
            raw_integer_target_gross_notional = 0.0
            margin_fitted_target_gross_notional = 0.0
            final_target_gross_notional = 0.0
            integer_rounding_loss_notional = 0.0
            max_volume_clipping_notional = 0.0
            unavailable_contract_notional = 0.0
            margin_capacity_loss_notional = 0.0
            lot_stabilization_loss_notional = 0.0

            if halted:
                output_rows.append(
                    {
                        "date": day,
                        "equity": equity,
                        "daily_return": 0.0,
                        "turnover_notional": 0.0,
                        "turnover_roll": 0.0,
                        "turnover_resize": 0.0,
                        "turnover_reversal": 0.0,
                        "turnover_entry": 0.0,
                        "turnover_exit": 0.0,
                        "turnover_daily_circuit": 0.0,
                        "turnover_hard_halt": 0.0,
                        "turnover_gross_guard": 0.0,
                        "raw_target_gross_ratio": raw_target_gross_ratio,
                        "governor_target_gross_ratio": governor_target_gross_ratio,
                        "raw_integer_target_gross_notional": 0.0,
                        "margin_fitted_target_gross_notional": 0.0,
                        "final_target_gross_notional": 0.0,
                        "integer_rounding_loss_notional": 0.0,
                        "max_volume_clipping_notional": 0.0,
                        "unavailable_contract_notional": 0.0,
                        "margin_capacity_loss_notional": 0.0,
                        "lot_stabilization_loss_notional": 0.0,
                        "gross_notional": 0.0,
                        "margin": 0.0,
                        "risk_reason": first_divergence,
                        "margin_reject": "",
                        "daily_circuit": False,
                        "gross_guard": False,
                        "risk_scale": risk_scale,
                        "halted": True,
                    }
                )
                continue

            open_prices: dict[str, float] = {}
            close_prices: dict[str, float] = {}
            missing_existing = False
            for symbol, volume in list(lots.items()):
                row = by_day_symbol.get((day, symbol))
                if row is None or symbol not in previous_close:
                    risk_reason = f"missing same-contract next price: {symbol}"
                    first_divergence = first_divergence or risk_reason
                    missing_existing = True
                    break
                open_price = float(row["open"])
                close_price = float(row["close"])
                open_prices[symbol] = open_price
                close_prices[symbol] = close_price
                gap_pnl = (
                    (open_price - float(previous_close[symbol]))
                    * int(volume)
                    * PRODUCT_MULTIPLIERS[self._product(symbol)]
                )
                equity += gap_pnl
                event_rows.append(
                    self._pnl_audit_row(
                        day=day,
                        action="gap",
                        symbol=symbol,
                        volume=int(volume),
                        price=open_price,
                        gross_pnl=gap_pnl,
                    )
                )
            if missing_existing:
                halted = True
                output_rows.append(
                    {
                        "date": day,
                        "equity": equity,
                        "daily_return": equity / previous_equity - 1.0,
                        "turnover_notional": 0.0,
                        "turnover_roll": 0.0,
                        "turnover_resize": 0.0,
                        "turnover_reversal": 0.0,
                        "turnover_entry": 0.0,
                        "turnover_exit": 0.0,
                        "turnover_daily_circuit": 0.0,
                        "turnover_hard_halt": 0.0,
                        "turnover_gross_guard": 0.0,
                        "raw_target_gross_ratio": raw_target_gross_ratio,
                        "governor_target_gross_ratio": governor_target_gross_ratio,
                        "raw_integer_target_gross_notional": 0.0,
                        "margin_fitted_target_gross_notional": 0.0,
                        "final_target_gross_notional": 0.0,
                        "integer_rounding_loss_notional": 0.0,
                        "max_volume_clipping_notional": 0.0,
                        "unavailable_contract_notional": 0.0,
                        "margin_capacity_loss_notional": 0.0,
                        "lot_stabilization_loss_notional": 0.0,
                        "gross_notional": 0.0,
                        "margin": 0.0,
                        "risk_reason": risk_reason,
                        "margin_reject": "",
                        "daily_circuit": False,
                        "gross_guard": False,
                        "risk_scale": risk_scale,
                        "halted": True,
                    }
                )
                continue

            # Production observes account equity/margin at the session open before
            # normal target rebalance. A favorable gap also establishes a new HWM.
            high_watermark = max(high_watermark, equity)
            current_margin = self._margin(lots, open_prices) if lots else 0.0
            risk_reason = self.account_risk_reason(
                equity=equity,
                day_start_equity=day_start_equity,
                high_watermark=high_watermark,
                margin=current_margin,
            )
            if risk_reason:
                first_divergence = first_divergence or risk_reason
                if lots:
                    closing = {symbol: -volume for symbol, volume in lots.items()}
                    risk_action = (
                        "daily_circuit"
                        if risk_reason == "daily loss limit reached"
                        else "hard_halt"
                    )
                    event_rows.extend(
                        self._trade_audit_rows(
                            day=day,
                            starting_lots=dict(lots),
                            deltas=closing,
                            prices=open_prices,
                            cost_rate=cost_rate,
                            action_override=risk_action,
                        )
                    )
                    close_turnover = self._turnover(closing, open_prices)
                    turnover_notional += close_turnover
                    if risk_reason == "daily loss limit reached":
                        turnover_daily_circuit += close_turnover
                    else:
                        turnover_hard_halt += close_turnover
                    equity -= close_turnover * cost_rate
                    lots.clear()
                if risk_reason == "daily loss limit reached":
                    daily_circuit = True
                else:
                    halted = True

            if not halted and not daily_circuit:
                activity_position = int(available_activity_days.searchsorted(day, side="left")) - 1
                if activity_position < 0:
                    selected = {}
                else:
                    completed_activity_day = pd.Timestamp(
                        available_activity_days[activity_position]
                    ).normalize()
                    preferred_symbols = {self._product(symbol): symbol for symbol in lots}
                    selected = self._select_contracts_from_snapshot(
                        activity_by_day[completed_activity_day],
                        day,
                        preferred_symbols=preferred_symbols,
                    )
                product_open: dict[str, float] = {}
                selected_symbols: dict[str, str] = {}
                for product, symbol in selected.items():
                    row = by_day_symbol.get((day, symbol))
                    if row is None:
                        continue
                    selected_symbols[product] = symbol
                    product_open[product] = float(row["open"])
                    open_prices[symbol] = float(row["open"])
                    close_prices[symbol] = float(row["close"])

                product_weights = {
                    product: value * risk_scale for product, value in raw_product_weights.items()
                }
                target_stages = self.target_lot_stages(
                    equity=equity,
                    product_weights=product_weights,
                    product_open_prices=product_open,
                    selected_symbols=selected_symbols,
                    current_lots=lots,
                    completed_returns=tuple(completed_returns),
                )
                target = dict(target_stages.final_lots)
                raw_integer_target_gross_notional = target_stages.raw_integer_notional
                margin_fitted_target_gross_notional = target_stages.margin_fitted_notional
                integer_rounding_loss_notional = target_stages.integer_rounding_loss_notional
                max_volume_clipping_notional = target_stages.max_volume_clipping_notional
                unavailable_contract_notional = target_stages.unavailable_contract_notional
                margin_capacity_loss_notional = max(
                    0.0,
                    raw_integer_target_gross_notional - margin_fitted_target_gross_notional,
                )
                lot_stabilization_loss_notional = max(
                    0.0,
                    margin_fitted_target_gross_notional - target_stages.final_notional,
                )
                required_products = {
                    product for product, value in product_weights.items() if abs(value) > 1e-15
                }
                unavailable_products = required_products - set(selected_symbols)
                for symbol, volume in lots.items():
                    if self._product(symbol) in unavailable_products:
                        target[symbol] = int(volume)
                final_target_gross_notional = (
                    self._gross_notional(target, open_prices) if target else 0.0
                )

                normal_original_lots = dict(lots)
                normal_target_lots = dict(target)
                phase = self.rebalance_plan(
                    current_lots=lots,
                    target_lots=target,
                )
                if phase.reductions:
                    reduction_turnover = self._turnover(phase.reductions, open_prices)
                    turnover_notional += reduction_turnover
                    for symbol, delta in phase.reductions.items():
                        normal_deltas[symbol] = normal_deltas.get(symbol, 0) + int(delta)
                    event_rows.extend(
                        self._trade_audit_rows(
                            day=day,
                            starting_lots=dict(lots),
                            deltas=phase.reductions,
                            prices=open_prices,
                            cost_rate=cost_rate,
                            original_lots=normal_original_lots,
                            target_lots=normal_target_lots,
                        )
                    )
                    equity -= reduction_turnover * cost_rate
                    self._apply_deltas(lots, phase.reductions)

                phase = self.rebalance_plan(
                    current_lots=lots,
                    target_lots=target,
                )
                if phase.openings:
                    current_margin = self._margin(lots, open_prices)
                    allowed, margin_reject, _ = self.check_opening_batch(
                        equity=equity,
                        current_margin=current_margin,
                        current_lots=lots,
                        openings=phase.openings,
                        open_prices=open_prices,
                    )
                    if allowed:
                        opening_turnover = self._turnover(phase.openings, open_prices)
                        turnover_notional += opening_turnover
                        for symbol, delta in phase.openings.items():
                            normal_deltas[symbol] = normal_deltas.get(symbol, 0) + int(delta)
                        event_rows.extend(
                            self._trade_audit_rows(
                                day=day,
                                starting_lots=dict(lots),
                                deltas=phase.openings,
                                prices=open_prices,
                                cost_rate=cost_rate,
                                original_lots=normal_original_lots,
                                target_lots=normal_target_lots,
                            )
                        )
                        equity -= opening_turnover * cost_rate
                        self._apply_deltas(lots, phase.openings)
                    else:
                        first_divergence = first_divergence or margin_reject

                if normal_deltas:
                    attributed = self._attribute_normal_turnover(
                        original_lots=normal_original_lots,
                        target_lots=normal_target_lots,
                        deltas=normal_deltas,
                        prices=open_prices,
                    )
                    turnover_roll += attributed["roll"]
                    turnover_resize += attributed["resize"]
                    turnover_reversal += attributed["reversal"]
                    turnover_entry += attributed["entry"]
                    turnover_exit += attributed["exit"]

                intraday_pnl = 0.0
                for symbol, volume in lots.items():
                    if symbol not in open_prices or symbol not in close_prices:
                        continue
                    symbol_pnl = (
                        (close_prices[symbol] - open_prices[symbol])
                        * int(volume)
                        * PRODUCT_MULTIPLIERS[self._product(symbol)]
                    )
                    intraday_pnl += symbol_pnl
                    event_rows.append(
                        self._pnl_audit_row(
                            day=day,
                            action="intraday",
                            symbol=symbol,
                            volume=int(volume),
                            price=close_prices[symbol],
                            gross_pnl=symbol_pnl,
                        )
                    )
                equity += intraday_pnl
                high_watermark = max(high_watermark, equity)
                close_margin = self._margin(lots, close_prices) if lots else 0.0
                risk_reason = self.account_risk_reason(
                    equity=equity,
                    day_start_equity=day_start_equity,
                    high_watermark=high_watermark,
                    margin=close_margin,
                )
                if risk_reason:
                    first_divergence = first_divergence or risk_reason
                    if lots:
                        closing = {symbol: -volume for symbol, volume in lots.items()}
                        risk_action = (
                            "daily_circuit"
                            if risk_reason == "daily loss limit reached"
                            else "hard_halt"
                        )
                        event_rows.extend(
                            self._trade_audit_rows(
                                day=day,
                                starting_lots=dict(lots),
                                deltas=closing,
                                prices=close_prices,
                                cost_rate=cost_rate,
                                action_override=risk_action,
                            )
                        )
                        close_turnover = self._turnover(closing, close_prices)
                        turnover_notional += close_turnover
                        if risk_reason == "daily loss limit reached":
                            turnover_daily_circuit += close_turnover
                        else:
                            turnover_hard_halt += close_turnover
                        equity -= close_turnover * cost_rate
                        lots.clear()
                    if risk_reason == "daily loss limit reached":
                        daily_circuit = True
                    else:
                        halted = True
                elif lots:
                    guard_reductions = self.realized_gross_reductions(
                        lots=lots,
                        prices=close_prices,
                        equity=equity,
                        cost_rate=cost_rate,
                    )
                    if guard_reductions:
                        gross_guard = True
                        guard_turnover = self._turnover(guard_reductions, close_prices)
                        turnover_notional += guard_turnover
                        turnover_gross_guard += guard_turnover
                        event_rows.extend(
                            self._trade_audit_rows(
                                day=day,
                                starting_lots=dict(lots),
                                deltas=guard_reductions,
                                prices=close_prices,
                                cost_rate=cost_rate,
                                action_override="gross_guard",
                            )
                        )
                        equity -= guard_turnover * cost_rate
                        self._apply_deltas(lots, guard_reductions)

                        # Reduction costs are economically real and can themselves move
                        # the account through an existing hard gate; re-evaluate once.
                        close_margin = self._margin(lots, close_prices) if lots else 0.0
                        post_guard_reason = self.account_risk_reason(
                            equity=equity,
                            day_start_equity=day_start_equity,
                            high_watermark=high_watermark,
                            margin=close_margin,
                        )
                        if post_guard_reason:
                            risk_reason = post_guard_reason
                            first_divergence = first_divergence or risk_reason
                            if lots:
                                closing = {symbol: -volume for symbol, volume in lots.items()}
                                risk_action = (
                                    "daily_circuit"
                                    if risk_reason == "daily loss limit reached"
                                    else "hard_halt"
                                )
                                event_rows.extend(
                                    self._trade_audit_rows(
                                        day=day,
                                        starting_lots=dict(lots),
                                        deltas=closing,
                                        prices=close_prices,
                                        cost_rate=cost_rate,
                                        action_override=risk_action,
                                    )
                                )
                                close_turnover = self._turnover(closing, close_prices)
                                turnover_notional += close_turnover
                                if risk_reason == "daily loss limit reached":
                                    turnover_daily_circuit += close_turnover
                                else:
                                    turnover_hard_halt += close_turnover
                                equity -= close_turnover * cost_rate
                                lots.clear()
                            if risk_reason == "daily loss limit reached":
                                daily_circuit = True
                            else:
                                halted = True

            valuation_prices = close_prices if close_prices else open_prices
            margin = self._margin(lots, valuation_prices) if lots else 0.0
            gross_notional = self._gross_notional(lots, valuation_prices) if lots else 0.0
            previous_close = {
                symbol: float(close_prices[symbol]) for symbol in lots if symbol in close_prices
            }
            daily_return = equity / previous_equity - 1.0
            completed_returns.append(float(daily_return))
            completed_returns = self.retain_completed_returns(completed_returns)
            output_rows.append(
                {
                    "date": day,
                    "equity": equity,
                    "daily_return": daily_return,
                    "turnover_notional": turnover_notional,
                    "turnover_roll": turnover_roll,
                    "turnover_resize": turnover_resize,
                    "turnover_reversal": turnover_reversal,
                    "turnover_entry": turnover_entry,
                    "turnover_exit": turnover_exit,
                    "turnover_daily_circuit": turnover_daily_circuit,
                    "turnover_hard_halt": turnover_hard_halt,
                    "turnover_gross_guard": turnover_gross_guard,
                    "raw_target_gross_ratio": raw_target_gross_ratio,
                    "governor_target_gross_ratio": governor_target_gross_ratio,
                    "raw_integer_target_gross_notional": raw_integer_target_gross_notional,
                    "margin_fitted_target_gross_notional": margin_fitted_target_gross_notional,
                    "final_target_gross_notional": final_target_gross_notional,
                    "integer_rounding_loss_notional": integer_rounding_loss_notional,
                    "max_volume_clipping_notional": max_volume_clipping_notional,
                    "unavailable_contract_notional": unavailable_contract_notional,
                    "margin_capacity_loss_notional": margin_capacity_loss_notional,
                    "lot_stabilization_loss_notional": lot_stabilization_loss_notional,
                    "gross_notional": gross_notional,
                    "margin": margin,
                    "risk_reason": risk_reason,
                    "margin_reject": margin_reject,
                    "daily_circuit": daily_circuit,
                    "gross_guard": gross_guard,
                    "risk_scale": risk_scale,
                    "halted": halted,
                }
            )

        daily = pd.DataFrame(output_rows)
        if daily.empty:
            daily = pd.DataFrame(
                columns=[
                    "equity",
                    "daily_return",
                    "turnover_notional",
                    "turnover_roll",
                    "turnover_resize",
                    "turnover_reversal",
                    "turnover_entry",
                    "turnover_exit",
                    "turnover_daily_circuit",
                    "turnover_hard_halt",
                    "turnover_gross_guard",
                    "raw_target_gross_ratio",
                    "governor_target_gross_ratio",
                    "raw_integer_target_gross_notional",
                    "margin_fitted_target_gross_notional",
                    "final_target_gross_notional",
                    "integer_rounding_loss_notional",
                    "max_volume_clipping_notional",
                    "unavailable_contract_notional",
                    "margin_capacity_loss_notional",
                    "lot_stabilization_loss_notional",
                    "gross_notional",
                    "margin",
                    "risk_reason",
                    "margin_reject",
                    "daily_circuit",
                    "gross_guard",
                    "risk_scale",
                    "halted",
                ]
            )
        else:
            daily.set_index("date", inplace=True)
        events = pd.DataFrame(event_rows, columns=AUDIT_EVENT_COLUMNS)
        if weight_frame.empty:
            final_checkpoint = checkpoint
        else:
            final_checkpoint = DirectionalSimulationCheckpoint(
                last_day=pd.Timestamp(weight_frame.index[-1]).normalize(),
                equity=float(equity),
                high_watermark=float(high_watermark),
                lots=tuple(sorted((str(symbol), int(volume)) for symbol, volume in lots.items())),
                previous_close=tuple(
                    sorted((str(symbol), float(price)) for symbol, price in previous_close.items())
                ),
                completed_returns=tuple(float(value) for value in completed_returns),
                halted=bool(halted),
                first_divergence=str(first_divergence),
                strategy_state=tuple(sorted(self._checkpoint_strategy_state().items())),
                configuration_digest=self._checkpoint_configuration_digest(cost_bps),
            )
        return ProductionSimulationResult(
            daily=daily,
            events=events,
            final_equity=float(equity),
            first_divergence=first_divergence,
            final_checkpoint=final_checkpoint,
        )
