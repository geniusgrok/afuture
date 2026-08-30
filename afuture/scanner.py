"""跨期套利研究扫描器。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from math import exp, inf, log, sqrt
from zoneinfo import ZoneInfo

from .economics import estimate_net_edge
from .models import ContractSpec, PairConfig, SignalAction, Tick

_CHINA_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class SpreadCandidate:
    """单个跨期组合的研究候选评分。"""

    pair: str
    zscore: float
    liquidity_score: float
    volume_score: float
    open_interest_score: float
    half_life: float
    stationarity_score: float
    net_edge: float
    score: float


@dataclass(frozen=True)
class SpreadStatistics:
    """不依赖手续费/保证金的纯行情统计预筛结果。"""

    zscore: float
    reference_mean: float
    reference_std: float
    half_life: float
    stationarity_score: float
    signal_mean: float = 0.0
    signal_std: float = 0.0


class SpreadScanner:
    """从 Tick 计算统计偏离、流动性、持仓活跃度和净边际。"""

    def __init__(
        self,
        min_liquidity_score: float = 0.5,
        slippage_ticks: int = 1,
        max_sync_seconds: float = 2.0,
    ) -> None:
        if max_sync_seconds <= 0:
            raise ValueError("max_sync_seconds must be positive")
        self.min_liquidity_score = min_liquidity_score
        self.slippage_ticks = slippage_ticks
        self.max_sync_seconds = max_sync_seconds

    def filter(self, candidates: list[SpreadCandidate]) -> list[SpreadCandidate]:
        """只保留流动性合格且预期净边际为正的候选。"""
        return [
            candidate
            for candidate in candidates
            if candidate.liquidity_score >= self.min_liquidity_score and candidate.net_edge > 0
        ]

    def entry_signal(
        self,
        pair: PairConfig,
        near: Tick,
        far: Tick,
        statistics: SpreadStatistics,
    ) -> tuple[SignalAction, float] | None:
        """非确认模式按当前可成交价判断是否越过开仓阈值。"""
        long_signal = self._signal_value(pair, near.ask_price, far.bid_price)
        short_signal = self._signal_value(pair, near.bid_price, far.ask_price)
        short_z = self._z(short_signal, statistics.signal_mean, statistics.signal_std)
        long_z = self._z(long_signal, statistics.signal_mean, statistics.signal_std)
        if short_z >= pair.entry_z:
            return SignalAction.SHORT_SPREAD, short_z
        if long_z <= -pair.entry_z:
            return SignalAction.LONG_SPREAD, long_z
        return None

    def candidate_entry(
        self,
        pair: PairConfig,
        synchronized: list[tuple[Tick, Tick]],
        statistics: SpreadStatistics,
    ) -> tuple[SignalAction, float] | None:
        """返回最新样本真正可执行的入场方向；确认模式复算历史武装状态。"""
        if not synchronized:
            return None
        near, far = synchronized[-1]
        if not pair.confirm_entry:
            return self.entry_signal(pair, near, far, statistics)

        z_values = self._rolling_mid_z(pair, synchronized)
        if not z_values:
            return None
        current_index, current_z = z_values[-1]
        if current_index != len(synchronized) - 1:
            return None

        slope_values = [value for _, value in z_values[-pair.entry_trend_window :]]
        slope = (
            self._linear_slope(slope_values)
            if len(slope_values) >= pair.entry_trend_window
            else 0.0
        )
        if abs(slope) > pair.max_entry_z_slope:
            return None

        armed = 0
        extreme = 0.0
        confirmed: tuple[SignalAction, float] | None = None
        for index, zscore in z_values:
            if armed == 0:
                if zscore >= pair.entry_z:
                    armed = -1
                    extreme = zscore
                elif zscore <= -pair.entry_z:
                    armed = 1
                    extreme = zscore
                continue
            if armed < 0:
                extreme = max(extreme, zscore)
                reverted = (
                    zscore <= extreme - pair.confirmation_retrace_z
                    and zscore >= pair.min_confirmed_entry_z
                )
                disarmed = zscore < pair.min_confirmed_entry_z
                if reverted:
                    if index == len(synchronized) - 1:
                        confirmed = (SignalAction.SHORT_SPREAD, zscore)
                    armed = 0
                    extreme = 0.0
                elif disarmed:
                    armed = 0
                    extreme = 0.0
            else:
                extreme = min(extreme, zscore)
                reverted = (
                    zscore >= extreme + pair.confirmation_retrace_z
                    and zscore <= -pair.min_confirmed_entry_z
                )
                disarmed = zscore > -pair.min_confirmed_entry_z
                if reverted:
                    if index == len(synchronized) - 1:
                        confirmed = (SignalAction.LONG_SPREAD, zscore)
                    armed = 0
                    extreme = 0.0
                elif disarmed:
                    armed = 0
                    extreme = 0.0
        return confirmed

    def confirmation_seed(
        self,
        pair: PairConfig,
        synchronized: list[tuple[Tick, Tick]],
    ) -> tuple[list[float], int, float]:
        """为刚激活的策略恢复最近 Z 序列和尚未消费的确认武装状态。"""
        z_values = self._rolling_mid_z(pair, synchronized)
        history = [value for _, value in z_values[-max(pair.entry_trend_window, 2) :]]
        armed = 0
        extreme = 0.0
        for _, zscore in z_values:
            if armed == 0:
                if zscore >= pair.entry_z:
                    armed, extreme = -1, zscore
                elif zscore <= -pair.entry_z:
                    armed, extreme = 1, zscore
                continue
            if armed < 0:
                extreme = max(extreme, zscore)
                confirmed = (
                    zscore <= extreme - pair.confirmation_retrace_z
                    and zscore >= pair.min_confirmed_entry_z
                )
                disarmed = zscore < pair.min_confirmed_entry_z
            else:
                extreme = min(extreme, zscore)
                confirmed = (
                    zscore >= extreme + pair.confirmation_retrace_z
                    and zscore <= -pair.min_confirmed_entry_z
                )
                disarmed = zscore > -pair.min_confirmed_entry_z
            # 已经完成过一次确认的 armed 状态不能跨重启重复消费。
            if confirmed or disarmed:
                armed, extreme = 0, 0.0
        return history, armed, extreme

    def scan_pair(
        self,
        pair: PairConfig,
        ticks: list[Tick],
        specs: dict[str, ContractSpec],
    ) -> SpreadCandidate | None:
        """对一个同品种跨期组合生成最新研究候选。"""
        synchronized = self.synchronized_ticks(pair, ticks)
        statistics = self.statistics(pair, ticks, synchronized=synchronized)
        if statistics is None:
            return None
        entry = self.candidate_entry(pair, synchronized, statistics)
        if entry is None:
            return None
        action, zscore = entry
        near, far = synchronized[-1]
        edge = estimate_net_edge(
            action,
            reference_mean=statistics.reference_mean,
            near=near,
            far=far,
            specs=specs,
            volume=pair.volume,
            slippage_ticks=self.slippage_ticks,
            legging_buffer=pair.legging_buffer,
        )

        depth = min(
            near.bid_volume,
            near.ask_volume,
            far.bid_volume,
            far.ask_volume,
        )
        liquidity_score = min(1.0, depth / max(pair.volume * 2.0, 1.0))
        volume_score = 1.0 - exp(-max(min(near.volume, far.volume), 0.0) / 5000.0)
        open_interest_score = 1.0 - exp(
            -max(min(near.open_interest, far.open_interest), 0.0) / 10000.0
        )
        activity_score = 0.25 + 0.375 * volume_score + 0.375 * open_interest_score
        score = (
            abs(zscore) * liquidity_score * activity_score * statistics.stationarity_score
            + max(edge.net_edge, 0.0) / 1000.0
        )
        return SpreadCandidate(
            pair=pair.pair_id,
            zscore=zscore,
            liquidity_score=liquidity_score,
            volume_score=volume_score,
            open_interest_score=open_interest_score,
            half_life=statistics.half_life,
            stationarity_score=statistics.stationarity_score,
            net_edge=edge.net_edge,
            score=score,
        )

    def statistics(
        self,
        pair: PairConfig,
        ticks: list[Tick],
        *,
        synchronized: list[tuple[Tick, Tick]] | None = None,
    ) -> SpreadStatistics | None:
        """只用历史行情计算统计预筛，当前样本不进入参考均值。"""
        synchronized = (
            synchronized if synchronized is not None else self.synchronized_ticks(pair, ticks)
        )
        if len(synchronized) < pair.lookback + 1:
            return None
        raw_values = [near.mid_price - far.mid_price for near, far in synchronized]
        signal_values = [
            self._signal_value(pair, near.mid_price, far.mid_price) for near, far in synchronized
        ]
        signal_history = signal_values[-pair.lookback - 1 : -1]
        raw_history = raw_values[-pair.lookback - 1 : -1]
        if len(signal_history) < pair.lookback:
            return None

        signal_mean = sum(signal_history) / len(signal_history)
        signal_std = self._std(signal_history, signal_mean)
        raw_mean = sum(raw_history) / len(raw_history)
        raw_std = self._std(raw_history, raw_mean)
        zscore = self._z(signal_values[-1], signal_mean, signal_std)
        half_life, stationarity_score = self._mean_reversion_stats(signal_history)
        far_mid = synchronized[-1][1].mid_price
        reference_mean = (
            far_mid * (exp(signal_mean) - 1.0) if pair.signal_transform == "log_ratio" else raw_mean
        )
        return SpreadStatistics(
            zscore=zscore,
            reference_mean=reference_mean,
            reference_std=raw_std,
            half_life=half_life,
            stationarity_score=stationarity_score,
            signal_mean=signal_mean,
            signal_std=signal_std,
        )

    def synchronized_ticks(self, pair: PairConfig, ticks: list[Tick]) -> list[tuple[Tick, Tick]]:
        """按策略采样语义配对历史；实时执行同步由 RiskManager 独立硬门负责。"""
        near_ticks = sorted(
            (tick for tick in ticks if tick.symbol == pair.near_symbol),
            key=lambda item: item.timestamp,
        )
        far_ticks = sorted(
            (tick for tick in ticks if tick.symbol == pair.far_symbol),
            key=lambda item: item.timestamp,
        )
        if not near_ticks or not far_ticks:
            return []

        if pair.daily_sample_window:
            start, end = self._parse_window(pair.daily_sample_window)
            near_by_day = self._daily_ticks_by_trading_day(near_ticks, start, end)
            far_by_day = self._daily_ticks_by_trading_day(far_ticks, start, end)
            common_days = sorted(set(near_by_day) & set(far_by_day))
            return [
                (near_by_day[trading_day], far_by_day[trading_day]) for trading_day in common_days
            ]

        result: list[tuple[Tick, Tick]] = []
        far_index = 0
        last_sample = None
        for near in near_ticks:
            while far_index + 1 < len(far_ticks) and abs(
                (far_ticks[far_index + 1].timestamp - near.timestamp).total_seconds()
            ) <= abs((far_ticks[far_index].timestamp - near.timestamp).total_seconds()):
                far_index += 1
            far = far_ticks[far_index]
            skew = abs((far.timestamp - near.timestamp).total_seconds())
            if skew > self.max_sync_seconds or near.trading_day != far.trading_day:
                continue
            timestamp = max(near.timestamp, far.timestamp)
            if (
                last_sample is not None
                and pair.sample_seconds > 0
                and (timestamp - last_sample).total_seconds() < pair.sample_seconds
            ):
                continue
            result.append((near, far))
            last_sample = timestamp
        return result

    @staticmethod
    def _daily_ticks_by_trading_day(ticks: list[Tick], start: time, end: time) -> dict[str, Tick]:
        """每个交易日保留窗口内最后一笔；统计历史不套用 2 秒执行 skew。"""
        result: dict[str, Tick] = {}
        for tick in ticks:
            current = tick.timestamp.astimezone(_CHINA_TZ).timetz().replace(tzinfo=None)
            if not start <= current <= end:
                continue
            previous = result.get(tick.trading_day)
            if previous is None or tick.timestamp > previous.timestamp:
                result[tick.trading_day] = tick
        return result

    def _rolling_mid_z(
        self,
        pair: PairConfig,
        synchronized: list[tuple[Tick, Tick]],
    ) -> list[tuple[int, float]]:
        values = [
            self._signal_value(pair, near.mid_price, far.mid_price) for near, far in synchronized
        ]
        result: list[tuple[int, float]] = []
        for index in range(pair.lookback, len(values)):
            history = values[index - pair.lookback : index]
            mean = sum(history) / len(history)
            std = self._std(history, mean)
            result.append((index, self._z(values[index], mean, std)))
        return result

    @staticmethod
    def _signal_value(pair: PairConfig, near_price: float, far_price: float) -> float:
        if pair.signal_transform == "log_ratio":
            return log(near_price / far_price)
        return near_price - far_price

    @staticmethod
    def _std(values: list[float], mean: float) -> float:
        if not values:
            return 0.0
        return sqrt(sum((value - mean) ** 2 for value in values) / len(values))

    @staticmethod
    def _linear_slope(values: list[float]) -> float:
        if len(values) < 2:
            return 0.0
        x_mean = (len(values) - 1) / 2.0
        y_mean = sum(values) / len(values)
        denominator = sum((index - x_mean) ** 2 for index in range(len(values)))
        if denominator <= 0:
            return 0.0
        return (
            sum((index - x_mean) * (value - y_mean) for index, value in enumerate(values))
            / denominator
        )

    @staticmethod
    def _parse_window(raw: str) -> tuple[time, time]:
        try:
            left, right = raw.split("-", 1)
            start = time.fromisoformat(left)
            end = time.fromisoformat(right)
        except ValueError as exc:
            raise ValueError(f"invalid daily sample window: {raw}") from exc
        if start >= end:
            raise ValueError("daily sample window must be increasing")
        return start, end

    @staticmethod
    def _z(value: float, mean: float, std: float) -> float:
        delta = value - mean
        if std <= 1e-12:
            if delta > 0:
                return inf
            if delta < 0:
                return -inf
            return 0.0
        return delta / std

    @staticmethod
    def _mean_reversion_stats(values: list[float]) -> tuple[float, float]:
        """用简单 AR(1) 近似计算半衰期和均值回复启发式分数。"""
        if len(values) < 4:
            return 999.0, 0.0
        levels = values[:-1]
        changes = [values[index + 1] - values[index] for index in range(len(values) - 1)]
        level_mean = sum(levels) / len(levels)
        change_mean = sum(changes) / len(changes)
        denominator = sum((value - level_mean) ** 2 for value in levels)
        if denominator <= 1e-12:
            return 999.0, 0.0
        beta = (
            sum(
                (level - level_mean) * (change - change_mean)
                for level, change in zip(levels, changes, strict=True)
            )
            / denominator
        )
        if beta >= 0:
            return 999.0, 0.0
        return max(0.1, -log(2.0) / beta), min(1.0, max(0.0, -beta))
