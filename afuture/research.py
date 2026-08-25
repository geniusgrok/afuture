"""Walk-forward、OOS 和交易成本压力测试研究流程。

研究流程和生产交易分离：参数只能使用训练与验证窗口选择，OOS 只用于验收，
不得反向参与参数选择。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import sqrt

from .calibration import ParameterCalibrator
from .economics import estimate_net_edge, executable_spreads
from .models import ContractSpec, PairConfig, SignalAction, Tick
from .strategy import CalendarSpreadStrategy


@dataclass(frozen=True)
class ResearchConfig:
    """研究窗口和成本压力配置。"""

    train_days: int = 60
    validation_days: int = 20
    oos_days: int = 20
    step_days: int = 20
    cost_stress_multipliers: tuple[float, ...] = (1.0, 1.5, 2.0)
    parameter_grid: tuple[dict, ...] = ()


@dataclass(frozen=True)
class FoldResult:
    parameters: dict
    train_metrics: dict
    validation_metrics: dict
    oos_metrics: dict


@dataclass(frozen=True)
class ResearchResult:
    folds: list[FoldResult]
    stress_results: dict[float, dict]
    selected_parameters: dict


@dataclass(frozen=True)
class AcceptanceDecision:
    accepted: bool
    reasons: tuple[str, ...] = ()


class WalkForwardRunner:
    """按交易日真实切分 Train/Validation/OOS 并执行完整策略回放。"""

    def __init__(
        self,
        specs: dict[str, ContractSpec],
        initial_capital: float = 500000,
        *,
        slippage_ticks: int = 1,
    ) -> None:
        if initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if slippage_ticks < 0:
            raise ValueError("slippage_ticks cannot be negative")
        self.specs = specs
        self.initial_capital = initial_capital
        self.slippage_ticks = slippage_ticks

    def run(
        self,
        pair: PairConfig,
        ticks: list[Tick],
        config: ResearchConfig,
    ) -> ResearchResult:
        """执行滚动研究；任何参数选择都发生在对应 OOS 之前。"""
        self._validate_config(config)
        days = sorted({tick.trading_day for tick in ticks})
        total_days = config.train_days + config.validation_days + config.oos_days
        grid = list(config.parameter_grid) or [self._parameter_row(pair)]
        folds: list[FoldResult] = []
        selected_rows: list[dict] = []

        for start in range(
            0,
            max(0, len(days) - total_days + 1),
            config.step_days,
        ):
            train_end = start + config.train_days
            validation_end = train_end + config.validation_days
            oos_end = validation_end + config.oos_days
            if oos_end > len(days):
                break

            train = self._slice_days(ticks, days[start:train_end])
            validation = self._slice_days(ticks, days[train_end:validation_end])
            oos = self._slice_days(ticks, days[validation_end:oos_end])

            candidates: list[dict] = []
            for parameters in grid:
                candidate_pair = replace(pair, **parameters)
                train_metrics = self._backtest(candidate_pair, train, 1.0)
                validation_metrics = self._backtest(candidate_pair, validation, 1.0)
                candidates.append(
                    {
                        **parameters,
                        # 验证窗口权重略高，降低仅拟合训练段的参数晋级概率。
                        "score": (
                            0.35 * train_metrics["sharpe"] + 0.65 * validation_metrics["sharpe"]
                        ),
                        "max_drawdown": max(
                            abs(train_metrics["max_drawdown"]),
                            abs(validation_metrics["max_drawdown"]),
                        ),
                        "_train": train_metrics,
                        "_validation": validation_metrics,
                    }
                )

            selected = ParameterCalibrator().select_best(candidates)
            if selected is None:
                # 多组参数都只有孤立峰值时，不用 OOS 帮忙挑赢家，直接让该折无晋级参数。
                continue
            parameters = {
                key: selected[key]
                for key in ("lookback", "entry_z", "exit_z", "stop_z")
                if key in selected
            }
            selected_pair = replace(pair, **parameters)
            oos_metrics = self._backtest(selected_pair, oos, 1.0)
            folds.append(
                FoldResult(
                    parameters=parameters,
                    train_metrics=selected["_train"],
                    validation_metrics=selected["_validation"],
                    oos_metrics=oos_metrics,
                )
            )
            selected_rows.append(parameters)

        # 最后一个 fold 的参数来自当时可见的训练+验证数据，符合真实滚动生产选择语义。
        selected_parameters = selected_rows[-1] if selected_rows else self._parameter_row(pair)
        final_pair = replace(pair, **selected_parameters)
        stress_results = {
            multiplier: self._backtest(final_pair, ticks, multiplier)
            for multiplier in config.cost_stress_multipliers
        }
        return ResearchResult(
            folds=folds,
            stress_results=stress_results,
            selected_parameters=selected_parameters,
        )

    def _backtest(
        self,
        pair: PairConfig,
        ticks: list[Tick],
        cost_multiplier: float,
    ) -> dict:
        """用方向性可成交价差执行轻量研究回放，并计入完整往返成本。"""
        near_by_time = {tick.timestamp: tick for tick in ticks if tick.symbol == pair.near_symbol}
        far_by_time = {tick.timestamp: tick for tick in ticks if tick.symbol == pair.far_symbol}
        common = sorted(set(near_by_time) & set(far_by_time))
        strategy = CalendarSpreadStrategy(pair)
        cash = self.initial_capital
        high_watermark = cash
        max_drawdown = 0.0
        trade_count = 0
        position = 0
        entry_spread = 0.0
        equity_by_day: dict[str, float] = {}

        for timestamp in common:
            near = near_by_time[timestamp]
            far = far_by_time[timestamp]
            signal = strategy.on_quotes(near, far)
            long_spread, short_spread = executable_spreads(near, far)

            if position == 0 and signal.action in {
                SignalAction.LONG_SPREAD,
                SignalAction.SHORT_SPREAD,
            }:
                edge = estimate_net_edge(
                    signal.action,
                    reference_mean=signal.reference_mean,
                    near=near,
                    far=far,
                    specs=self.specs,
                    volume=pair.volume,
                    slippage_ticks=self.slippage_ticks,
                    legging_buffer=pair.legging_buffer,
                    cost_multiplier=cost_multiplier,
                )
                if edge.net_edge > pair.min_net_edge:
                    position = 1 if signal.action is SignalAction.LONG_SPREAD else -1
                    entry_spread = edge.executable_spread
                    # 一次性扣除保守估算的完整往返成本，避免退出时漏算费用。
                    cash -= edge.total_cost
                    trade_count += 2
                else:
                    strategy.set_position(0)

            elif position != 0 and signal.action in {
                SignalAction.EXIT,
                SignalAction.EMERGENCY_EXIT,
            }:
                cash += self._spread_pnl(
                    position,
                    entry_spread,
                    long_spread,
                    short_spread,
                    pair.volume,
                    pair,
                )
                position = 0
                trade_count += 2

            equity = cash
            if position != 0:
                equity += self._spread_pnl(
                    position,
                    entry_spread,
                    long_spread,
                    short_spread,
                    pair.volume,
                    pair,
                )
            high_watermark = max(high_watermark, equity)
            if high_watermark > 0:
                max_drawdown = min(max_drawdown, equity / high_watermark - 1.0)
            equity_by_day[near.trading_day] = equity

        if position != 0 and common:
            near = near_by_time[common[-1]]
            far = far_by_time[common[-1]]
            long_spread, short_spread = executable_spreads(near, far)
            cash += self._spread_pnl(
                position,
                entry_spread,
                long_spread,
                short_spread,
                pair.volume,
                pair,
            )
            trade_count += 2
            equity_by_day[near.trading_day] = cash

        values = list(equity_by_day.values())
        returns: list[float] = []
        previous = self.initial_capital
        for equity in values:
            if previous > 0:
                returns.append(equity / previous - 1.0)
            previous = equity
        sharpe = 0.0
        if len(returns) >= 2:
            mean_return = sum(returns) / len(returns)
            variance = sum((value - mean_return) ** 2 for value in returns) / (len(returns) - 1)
            if variance > 0:
                sharpe = mean_return / sqrt(variance) * sqrt(252.0)

        return {
            "total_return": cash / self.initial_capital - 1.0,
            "max_drawdown": max_drawdown,
            "sharpe": sharpe,
            "trade_count": trade_count,
            "final_equity": cash,
        }

    def _spread_pnl(
        self,
        position: int,
        entry_spread: float,
        long_spread: float,
        short_spread: float,
        volume: int,
        pair: PairConfig,
    ) -> float:
        multiplier = min(
            self.specs[pair.near_symbol].multiplier,
            self.specs[pair.far_symbol].multiplier,
        )
        if position > 0:
            points = short_spread - entry_spread
        else:
            points = entry_spread - long_spread
        return points * multiplier * volume

    @staticmethod
    def _slice_days(ticks: list[Tick], days: list[str]) -> list[Tick]:
        allowed = set(days)
        return [tick for tick in ticks if tick.trading_day in allowed]

    @staticmethod
    def _parameter_row(pair: PairConfig) -> dict:
        return {
            "lookback": pair.lookback,
            "entry_z": pair.entry_z,
            "exit_z": pair.exit_z,
            "stop_z": pair.stop_z,
        }

    @staticmethod
    def _validate_config(config: ResearchConfig) -> None:
        if any(
            value <= 0
            for value in (
                config.train_days,
                config.validation_days,
                config.oos_days,
                config.step_days,
            )
        ):
            raise ValueError("walk-forward day windows must be positive")
        if not config.cost_stress_multipliers or any(
            value <= 0 for value in config.cost_stress_multipliers
        ):
            raise ValueError("cost stress multipliers must be positive")


class AcceptanceGate:
    """只有跨窗口和成本压力都不脆弱的候选才允许晋级。"""

    def __init__(
        self,
        min_positive_oos_ratio: float = 0.6,
        max_oos_drawdown: float = 0.15,
        min_stress_return: float = -0.02,
    ) -> None:
        self.min_positive_oos_ratio = min_positive_oos_ratio
        self.max_oos_drawdown = max_oos_drawdown
        self.min_stress_return = min_stress_return

    def evaluate(self, result: ResearchResult) -> AcceptanceDecision:
        reasons: list[str] = []
        if not result.folds:
            reasons.append("no walk-forward folds")
        else:
            positive_ratio = sum(
                1 for fold in result.folds if fold.oos_metrics.get("total_return", 0.0) > 0
            ) / len(result.folds)
            if positive_ratio < self.min_positive_oos_ratio:
                reasons.append("positive OOS ratio is too low")
            worst_drawdown = max(
                (abs(fold.oos_metrics.get("max_drawdown", 0.0)) for fold in result.folds),
                default=0.0,
            )
            if worst_drawdown > self.max_oos_drawdown:
                reasons.append("OOS drawdown is too high")

        if any(
            metrics.get("total_return", 0.0) < self.min_stress_return
            for metrics in result.stress_results.values()
        ):
            reasons.append("cost stress is too fragile")
        return AcceptanceDecision(
            accepted=not reasons,
            reasons=tuple(reasons),
        )
