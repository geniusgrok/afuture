"""最终 Auto Portfolio 的 Walk-forward/OOS/鲁棒性研究。

与单 pair 轻量研究不同，本模块直接复用 TradingEngine + AutoPairManager + RiskManager +
PairExecutor + SimBroker，验证最终生产组合的自动发现、资金竞争、动态手数和生命周期。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import timedelta
from itertools import groupby
from math import isfinite
from pathlib import Path
from tempfile import TemporaryDirectory

from .auto import AutoPairManager
from .broker.sim import SimBroker
from .calibration import ParameterCalibrator
from .engine import TradingEngine
from .models import ContractInfo, ContractSpec, FeeSpec, Tick
from .report import calculate_performance
from .risk import RiskManager
from .state import StateStore


@dataclass(frozen=True)
class AutoPortfolioResearchConfig:
    train_days: int = 120
    validation_days: int = 40
    oos_days: int = 40
    step_days: int = 40
    parameter_grid: tuple[dict, ...] = ()
    cost_stress_multipliers: tuple[float, ...] = (1.0, 1.5, 2.0)
    depth_haircuts: tuple[float, ...] = (1.0, 0.5)
    extra_latency_ticks: tuple[int, ...] = (0, 1, 2)
    extra_impact_ticks: tuple[int, ...] = (0, 1)
    data_gap_rates: tuple[float, ...] = (0.02, 0.05)
    quote_skew_seconds: tuple[float, ...] = (0.5, 1.0, 2.0)
    activity_missing_rates: tuple[float, ...] = (0.02, 0.05)


@dataclass(frozen=True)
class AutoPortfolioFold:
    parameters: dict
    train_metrics: dict
    validation_metrics: dict
    oos_metrics: dict
    oos_days: tuple[str, ...]


@dataclass(frozen=True)
class AutoPortfolioResearchResult:
    folds: list[AutoPortfolioFold]
    selected_parameters: dict
    stress_results: dict[float, dict]
    robustness: dict[str, object]


class AutoPortfolioRunner:
    """严格按时间顺序选择全局参数，并用完整 Auto 交易链执行 OOS。"""

    def __init__(self, base_config) -> None:
        if not base_config.auto.enabled:
            raise ValueError("accept-auto requires auto.enabled=true")
        self.base = base_config

    def parameter_grid(
        self,
        research: AutoPortfolioResearchConfig,
    ) -> list[dict]:
        """返回预先限定的小型全局参数邻域，不做高维全空间搜索。

        风险参数和仓位参数不参与收益优化；这里只对少量信号/候选参数做单变量邻域扰动，
        让 Calibrator 能选择稳定区域，同时避免商品逐个调参造成自由度爆炸。
        """
        if research.parameter_grid:
            return [dict(row) for row in research.parameter_grid]
        base = self._parameter_row(self.base.auto)
        rows = [dict(base)]

        def add(**updates) -> None:
            row = dict(base)
            row.update(updates)
            if not 0 <= row["exit_z"] < row["entry_z"] < self.base.auto.stop_z:
                return
            key = tuple(sorted(row.items()))
            if key not in {tuple(sorted(existing.items())) for existing in rows}:
                rows.append(row)

        add(lookback=max(2, int(round(base["lookback"] * 0.8))))
        add(lookback=max(2, int(round(base["lookback"] * 1.2))))
        add(entry_z=max(base["exit_z"] + 0.05, base["entry_z"] * 0.9))
        add(entry_z=min(self.base.auto.stop_z - 0.05, base["entry_z"] * 1.1))
        add(exit_z=max(0.0, base["exit_z"] * 0.9))
        add(exit_z=min(base["entry_z"] - 0.05, base["exit_z"] * 1.1))
        if base["min_net_edge"] > 0:
            add(min_net_edge=base["min_net_edge"] * 0.8)
            add(min_net_edge=base["min_net_edge"] * 1.2)
        if base["min_mean_reversion_score"] > 0:
            add(min_mean_reversion_score=max(0.0, base["min_mean_reversion_score"] * 0.8))
            add(min_mean_reversion_score=min(1.0, base["min_mean_reversion_score"] * 1.2))
        add(max_half_life=max(0.1, base["max_half_life"] * 0.8))
        add(max_half_life=base["max_half_life"] * 1.2)
        return rows

    def run(
        self,
        ticks: list[Tick],
        research: AutoPortfolioResearchConfig,
    ) -> AutoPortfolioResearchResult:
        self._validate(research)
        days = sorted({row.trading_day for row in ticks})
        total = research.train_days + research.validation_days + research.oos_days
        grid = self.parameter_grid(research)
        folds: list[AutoPortfolioFold] = []
        selected_rows: list[dict] = []

        for start in range(0, max(0, len(days) - total + 1), research.step_days):
            train_end = start + research.train_days
            validation_end = train_end + research.validation_days
            oos_end = validation_end + research.oos_days
            if oos_end > len(days):
                break
            train_days = days[start:train_end]
            validation_days = days[train_end:validation_end]
            oos_days = days[validation_end:oos_end]
            train_ticks = self._slice(ticks, train_days)
            validation_ticks = self._slice(ticks, validation_days)
            oos_ticks = self._slice(ticks, oos_days)

            candidates: list[dict] = []
            for parameters in grid:
                candidate = self._config_with_parameters(parameters)
                train_metrics = self._run_portfolio(candidate, train_ticks)
                validation_metrics = self._run_portfolio(candidate, validation_ticks)
                candidates.append(
                    {
                        **parameters,
                        "score": 0.35 * self._score(train_metrics)
                        + 0.65 * self._score(validation_metrics),
                        "max_drawdown": max(
                            abs(float(train_metrics.get("max_drawdown", 0.0))),
                            abs(float(validation_metrics.get("max_drawdown", 0.0))),
                        ),
                        "_train": train_metrics,
                        "_validation": validation_metrics,
                    }
                )

            selected = ParameterCalibrator().select_best(candidates)
            if selected is None:
                continue
            parameters = {
                key: selected[key]
                for key in (
                    "lookback",
                    "entry_z",
                    "exit_z",
                    "min_net_edge",
                    "min_mean_reversion_score",
                    "max_half_life",
                )
                if key in selected
            }
            oos_metrics = self._run_portfolio(self._config_with_parameters(parameters), oos_ticks)
            folds.append(
                AutoPortfolioFold(
                    parameters=parameters,
                    train_metrics=selected["_train"],
                    validation_metrics=selected["_validation"],
                    oos_metrics=oos_metrics,
                    oos_days=tuple(oos_days),
                )
            )
            selected_rows.append(parameters)

        selected_parameters = (
            selected_rows[-1] if selected_rows else self._parameter_row(self.base.auto)
        )
        final_config = self._config_with_parameters(selected_parameters)
        stress_results = {
            float(multiplier): self._run_portfolio(
                self._cost_stress(final_config, float(multiplier)), ticks
            )
            for multiplier in research.cost_stress_multipliers
        }
        robustness = self._robustness(final_config, ticks, folds, research)
        return AutoPortfolioResearchResult(
            folds=folds,
            selected_parameters=selected_parameters,
            stress_results=stress_results,
            robustness=robustness,
        )

    def _robustness(self, config, ticks, folds, research) -> dict[str, object]:
        products = sorted({item.product.lower() for item in config.contract_catalog})
        leave_one: dict[str, dict] = {}
        single: dict[str, dict] = {}
        for product in products:
            leave_one[product] = self._run_portfolio(
                self._with_products(
                    config, tuple(item for item in config.auto.products if item.lower() != product)
                ),
                ticks,
            )
            single[product] = self._run_portfolio(self._with_products(config, (product,)), ticks)

        remove_best = self._remove_best_period(config, ticks, folds)
        depth = {
            str(value): self._run_portfolio(config, self._haircut_depth(ticks, value))
            for value in research.depth_haircuts
        }
        latency = {
            str(value): self._run_portfolio(
                replace(config, latency_ticks=config.latency_ticks + value), ticks
            )
            for value in research.extra_latency_ticks
        }
        impact = {
            str(value): self._run_portfolio(
                replace(config, market_impact_ticks=config.market_impact_ticks + value), ticks
            )
            for value in research.extra_impact_ticks
        }
        data_gap = {
            self._ratio_key(value): self._run_portfolio(config, self._drop_data(ticks, value))
            for value in research.data_gap_rates
        }
        quote_skew = {
            str(value): self._run_portfolio(
                config, self._skew_far_legs(ticks, config.contract_catalog, value)
            )
            for value in research.quote_skew_seconds
        }
        activity_missing = {
            self._ratio_key(value): self._run_portfolio(config, self._remove_activity(ticks, value))
            for value in research.activity_missing_rates
        }
        return {
            "leave_one_product_out": leave_one,
            "single_product": single,
            "remove_best_period": remove_best,
            "depth_haircut": depth,
            "latency": latency,
            "market_impact": impact,
            "data_gap": data_gap,
            "quote_skew": quote_skew,
            "activity_missing": activity_missing,
        }

    def _remove_best_period(self, config, ticks, folds) -> dict:
        if not folds:
            return self._run_portfolio(config, ticks)
        best = max(folds, key=lambda fold: float(fold.oos_metrics.get("total_return", 0.0)))
        excluded = set(best.oos_days)
        return self._run_portfolio(
            config, [row for row in ticks if row.trading_day not in excluded]
        )

    def _run_portfolio(self, config, ticks: list[Tick]) -> dict:
        if not ticks:
            return self._empty_metrics(config.initial_capital)
        with TemporaryDirectory(prefix="afuture-auto-research-") as temp:
            root = Path(temp)
            broker = SimBroker(
                config.initial_capital,
                config.contracts,
                slippage_ticks=config.slippage_ticks,
                conservative=True,
                latency_ticks=config.latency_ticks,
                market_impact_ticks=config.market_impact_ticks,
                contract_catalog=config.contract_catalog,
            )
            broker._trading_day = ticks[0].trading_day
            engine = TradingEngine(
                broker,
                config.pairs,
                config.contracts,
                RiskManager(config.risk),
                StateStore(root / "state.json"),
                auto_flatten_imbalance=config.auto_flatten_imbalance,
                aggressive_ticks=config.aggressive_ticks,
                slippage_ticks=config.slippage_ticks,
                legging_timeout_seconds=config.legging_timeout_seconds,
                auto_manager=AutoPairManager(config.auto),
                require_live_metadata=False,
                historical_mode=True,
            )
            equity_curve: list[tuple[str, float]] = []
            engine.start()
            try:
                for _, group in groupby(
                    sorted(ticks, key=lambda row: row.timestamp), key=lambda row: row.timestamp
                ):
                    batch = list(group)
                    for row in batch:
                        broker.publish_tick(row)
                    engine.run_once()
                    equity_curve.append((batch[-1].trading_day, broker.get_account().equity))
                trades = broker.get_trades()
                metrics = calculate_performance(
                    equity_curve,
                    initial_capital=config.initial_capital,
                    trade_count=len(trades),
                )
                metrics["commission_total"] = sum(float(row.commission) for row in trades)
                metrics["final_position_count"] = len(broker.get_positions())
                return metrics
            finally:
                engine.stop()

    def _config_with_parameters(self, parameters: dict):
        allowed = {
            key: value
            for key, value in parameters.items()
            if key in self.base.auto.__dataclass_fields__
        }
        return replace(self.base, auto=replace(self.base.auto, **allowed))

    @staticmethod
    def _parameter_row(auto) -> dict:
        return {
            "lookback": auto.lookback,
            "entry_z": auto.entry_z,
            "exit_z": auto.exit_z,
            "min_net_edge": auto.min_net_edge,
            "min_mean_reversion_score": auto.min_mean_reversion_score,
            "max_half_life": auto.max_half_life,
        }

    def _cost_stress(self, config, multiplier: float):
        specs = {
            symbol: self._scale_spec(row, multiplier) for symbol, row in config.contracts.items()
        }
        auto = replace(config.auto, legging_buffer=config.auto.legging_buffer * multiplier)
        slippage = max(config.slippage_ticks, int(round(config.slippage_ticks * multiplier)))
        return replace(config, contracts=specs, auto=auto, slippage_ticks=slippage)

    @staticmethod
    def _scale_spec(row: ContractSpec, multiplier: float) -> ContractSpec:
        fee = row.fee
        scaled_fee = FeeSpec(
            open_fixed=fee.open_fixed * multiplier,
            open_rate=fee.open_rate * multiplier,
            close_fixed=fee.close_fixed * multiplier,
            close_rate=fee.close_rate * multiplier,
            close_today_fixed=fee.close_today_fixed * multiplier,
            close_today_rate=fee.close_today_rate * multiplier,
        )
        return replace(row, fee=scaled_fee)

    @staticmethod
    def _with_products(config, products: tuple[str, ...]):
        if not products:
            products = ("__none__",)
        return replace(config, auto=replace(config.auto, products=products))

    @staticmethod
    def _haircut_depth(ticks: list[Tick], ratio: float) -> list[Tick]:
        ratio = max(0.0, float(ratio))
        return [
            replace(
                row,
                bid_volume=max(1.0, row.bid_volume * ratio),
                ask_volume=max(1.0, row.ask_volume * ratio),
            )
            for row in ticks
        ]

    @staticmethod
    def _drop_data(ticks: list[Tick], ratio: float) -> list[Tick]:
        """确定性删除少量 Tick，避免随机种子让 CI 不可复现。"""
        if ratio <= 0:
            return list(ticks)
        every = max(2, int(round(1.0 / ratio)))
        counters: dict[str, int] = defaultdict(int)
        result: list[Tick] = []
        for row in sorted(ticks, key=lambda item: (item.timestamp, item.symbol)):
            counters[row.symbol] += 1
            if counters[row.symbol] % every == 0:
                continue
            result.append(row)
        return result

    @staticmethod
    def _skew_far_legs(
        ticks: list[Tick], catalog: list[ContractInfo], seconds: float
    ) -> list[Tick]:
        """把每个品种较远月份向后移动，验证异步双腿行情不会制造虚假 Alpha。"""
        grouped: dict[tuple[str, str], list[ContractInfo]] = defaultdict(list)
        for item in catalog:
            grouped[(item.product.lower(), item.exchange.upper())].append(item)
        far_symbols: set[str] = set()
        for rows in grouped.values():
            ordered = sorted(rows, key=lambda item: (item.expiry, item.symbol))
            far_symbols.update(item.symbol for item in ordered[1:])
        delta = timedelta(seconds=max(0.0, float(seconds)))
        return [
            replace(row, timestamp=row.timestamp + delta) if row.symbol in far_symbols else row
            for row in ticks
        ]

    @staticmethod
    def _remove_activity(ticks: list[Tick], ratio: float) -> list[Tick]:
        """确定性制造少量 volume/OI 缺失，验证 selector 会安全拒绝而不是猜测。"""
        if ratio <= 0:
            return list(ticks)
        every = max(2, int(round(1.0 / ratio)))
        result: list[Tick] = []
        for index, row in enumerate(
            sorted(ticks, key=lambda item: (item.timestamp, item.symbol)), start=1
        ):
            if index % every == 0:
                result.append(replace(row, volume=0.0, open_interest=0.0))
            else:
                result.append(row)
        return result

    @staticmethod
    def _slice(ticks: list[Tick], days: list[str]) -> list[Tick]:
        allowed = set(days)
        return [row for row in ticks if row.trading_day in allowed]

    @staticmethod
    def _score(metrics: dict) -> float:
        total_return = float(metrics.get("total_return", 0.0))
        drawdown = abs(float(metrics.get("max_drawdown", 0.0)))
        sharpe = float(metrics.get("sharpe", 0.0))
        if not isfinite(sharpe):
            sharpe = 0.0
        calmar = total_return / max(drawdown, 1e-6) if total_return else 0.0
        return 0.6 * calmar + 0.4 * sharpe

    @staticmethod
    def _empty_metrics(initial_capital: float) -> dict:
        return {
            "initial_capital": initial_capital,
            "final_equity": initial_capital,
            "total_return": 0.0,
            "annualized_return": 0.0,
            "max_drawdown": 0.0,
            "sharpe": 0.0,
            "trading_days": 0,
            "trade_count": 0,
            "commission_total": 0.0,
            "final_position_count": 0,
        }

    @staticmethod
    def _ratio_key(value: float) -> str:
        return f"{float(value):.4g}"

    @staticmethod
    def _validate(config: AutoPortfolioResearchConfig) -> None:
        if any(
            value <= 0
            for value in (
                config.train_days,
                config.validation_days,
                config.oos_days,
                config.step_days,
            )
        ):
            raise ValueError("auto walk-forward windows must be positive")
        if not config.cost_stress_multipliers or any(
            value <= 0 for value in config.cost_stress_multipliers
        ):
            raise ValueError("cost stress multipliers must be positive")
        if any(not 0 < value <= 1 for value in config.depth_haircuts):
            raise ValueError("depth haircuts must be within (0, 1]")
        if any(value < 0 for value in config.extra_latency_ticks + config.extra_impact_ticks):
            raise ValueError("latency/impact stress cannot be negative")
        if any(
            not 0 < value < 1 for value in config.data_gap_rates + config.activity_missing_rates
        ):
            raise ValueError("data gap/activity missing rates must be within (0, 1)")
        if any(value < 0 for value in config.quote_skew_seconds):
            raise ValueError("quote skew stress cannot be negative")
