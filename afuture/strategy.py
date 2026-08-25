"""商品期货跨期价差均值回归策略。"""

from __future__ import annotations

from collections import deque
from datetime import datetime, time
from math import exp, inf, log, sqrt
from zoneinfo import ZoneInfo

from .economics import executable_spreads
from .models import PairConfig, SignalAction, SpreadSignal, Tick


_CHINA_TZ = ZoneInfo("Asia/Shanghai")


class CalendarSpreadStrategy:
    """只产生统计交易意图，不直接访问柜台或决定最终能否开仓。"""

    def __init__(self, pair: PairConfig) -> None:
        if pair.lookback < 2:
            raise ValueError("lookback must be at least 2")
        if not 0 <= pair.exit_z < pair.entry_z < pair.stop_z:
            raise ValueError("z-score thresholds must satisfy exit < entry < stop")
        if pair.signal_transform not in {"spread", "log_ratio"}:
            raise ValueError("signal_transform must be spread or log_ratio")
        if pair.confirm_entry:
            if pair.confirmation_retrace_z <= 0:
                raise ValueError("confirmation_retrace_z must be positive")
            if not 0 < pair.min_confirmed_entry_z < pair.entry_z:
                raise ValueError("min_confirmed_entry_z must be between zero and entry_z")
        if pair.entry_trend_window < 2:
            raise ValueError("entry_trend_window must be at least 2")
        if pair.max_entry_z_slope <= 0:
            raise ValueError("max_entry_z_slope must be positive")
        if not 0 <= pair.min_stationarity_score <= 1:
            raise ValueError("min_stationarity_score must be between zero and one")
        if pair.max_half_life <= 0:
            raise ValueError("max_half_life must be positive")
        if pair.daily_sample_window:
            self._parse_window(pair.daily_sample_window)

        self.pair = pair
        # _history 始终保存策略信号空间；raw history 单独用于人民币风险定仓。
        self._history: deque[float] = deque(maxlen=pair.lookback)
        self._raw_history: deque[float] = deque(maxlen=pair.lookback)
        self._z_history_window = max(pair.lookback, pair.entry_trend_window * 2)
        self._z_history: deque[float] = deque(maxlen=self._z_history_window)
        self._position = 0
        self._entry_mean = 0.0
        self._entry_std = 0.0
        self._holding_samples = 0
        self._last_sample_ts: datetime | None = None
        self._last_sample_trading_day = ""
        self._armed_direction = 0
        self._armed_extreme = 0.0

    @property
    def position(self) -> int:
        """1 表示多价差，-1 表示空价差，0 表示无仓。"""
        return self._position

    @property
    def spread_std(self) -> float:
        mean = self._raw_mean()
        return self._raw_std(mean)

    def set_position(self, position: int) -> None:
        if position not in {-1, 0, 1}:
            raise ValueError("spread position must be -1, 0 or 1")
        if self._position == 0 and position != 0 and self._raw_history:
            self._entry_mean = self._raw_mean()
            self._entry_std = max(self._raw_std(self._entry_mean), 1e-9)
            self._holding_samples = 0
        if position == 0:
            self._holding_samples = 0
        self._position = position

    def snapshot_state(self) -> dict:
        """导出策略状态，保证重启后不丢失滚动窗口和确认状态。"""
        return {
            "history": list(self._history),
            "raw_history": list(self._raw_history),
            "z_history": list(self._z_history),
            "position": self._position,
            "entry_mean": self._entry_mean,
            "entry_std": self._entry_std,
            "holding_samples": self._holding_samples,
            "last_sample_ts": self._last_sample_ts.isoformat() if self._last_sample_ts else "",
            "last_sample_trading_day": self._last_sample_trading_day,
            "armed_direction": self._armed_direction,
            "armed_extreme": self._armed_extreme,
        }

    def restore_state(self, state: dict) -> None:
        self._history.clear()
        for value in state.get("history", [])[-self.pair.lookback:]:
            self._history.append(float(value))
        self._raw_history.clear()
        raw_values = state.get("raw_history")
        if raw_values is None and self.pair.signal_transform == "spread":
            raw_values = state.get("history", [])
        for value in (raw_values or [])[-self.pair.lookback:]:
            self._raw_history.append(float(value))
        self._z_history.clear()
        for value in state.get("z_history", [])[-self._z_history_window:]:
            self._z_history.append(float(value))

        self._position = int(state.get("position", 0))
        if self._position not in {-1, 0, 1}:
            raise ValueError("invalid persisted spread position")
        self._entry_mean = float(state.get("entry_mean", 0.0))
        self._entry_std = float(state.get("entry_std", 0.0))
        self._holding_samples = int(state.get("holding_samples", 0))
        raw_ts = str(state.get("last_sample_ts", ""))
        if raw_ts:
            self._last_sample_ts = datetime.fromisoformat(raw_ts)
        else:
            self._last_sample_ts = None
        self._last_sample_trading_day = str(
            state.get("last_sample_trading_day", "")
        )
        self._armed_direction = int(state.get("armed_direction", 0))
        if self._armed_direction not in {-1, 0, 1}:
            raise ValueError("invalid persisted confirmation direction")
        self._armed_extreme = float(state.get("armed_extreme", 0.0))

    def restore_after_rejected_signal(self, previous_state: dict) -> None:
        """执行未接受信号时恢复真实持仓语义，同时保留本次市场观测。

        ``on_quotes`` 会先更新内部目标仓位再交给风控/执行。若随后被拒绝，必须恢复
        信号前的真实仓位和入场锚点，但不能丢掉已经消费的本次样本、日频采样标记或
        确认状态，否则同一交易日可能重复产生同一信号。
        """
        observed = self.snapshot_state()
        previous_position = int(previous_state.get("position", 0))
        previous_holding = int(previous_state.get("holding_samples", 0))

        self.restore_state(previous_state)
        self._history.clear()
        for value in observed.get("history", [])[-self.pair.lookback:]:
            self._history.append(float(value))
        self._raw_history.clear()
        for value in observed.get("raw_history", [])[-self.pair.lookback:]:
            self._raw_history.append(float(value))
        self._z_history.clear()
        for value in observed.get("z_history", [])[-self._z_history_window:]:
            self._z_history.append(float(value))
        raw_ts = str(observed.get("last_sample_ts", ""))
        if raw_ts:
            self._last_sample_ts = datetime.fromisoformat(raw_ts)
        else:
            self._last_sample_ts = None
        self._last_sample_trading_day = str(
            observed.get("last_sample_trading_day", "")
        )
        self._armed_direction = int(observed.get("armed_direction", 0))
        self._armed_extreme = float(observed.get("armed_extreme", 0.0))
        if previous_position != 0:
            self._holding_samples = previous_holding + 1

    def on_quotes(self, near: Tick, far: Tick) -> SpreadSignal:
        """按配置采样，在统一统计空间生成跨期交易信号。"""
        near.validate()
        far.validate()
        timestamp = max(near.timestamp, far.timestamp)
        raw_spread = near.mid_price - far.mid_price

        if not self._sample_allowed(near, far, timestamp):
            return self._hold(timestamp, raw_spread)

        signal_value = self._signal_value(near.mid_price, far.mid_price)
        if len(self._history) < self.pair.lookback:
            self._append_sample(signal_value, raw_spread, timestamp, near.trading_day)
            return self._hold(timestamp, raw_spread)

        signal_mean = self._mean()
        signal_std = self._std(signal_mean)
        raw_mean = self._raw_mean()
        raw_std = self._raw_std(raw_mean)
        reference_raw = self._reference_raw_mean(signal_mean, far.mid_price, raw_mean)

        long_exec, short_exec = executable_spreads(near, far)
        long_signal = self._executable_signal(near.ask_price, far.bid_price)
        short_signal = self._executable_signal(near.bid_price, far.ask_price)
        long_z = self._z(long_signal, signal_mean, signal_std)
        short_z = self._z(short_signal, signal_mean, signal_std)
        mid_z = self._z(signal_value, signal_mean, signal_std)
        half_life, stationarity = self._mean_reversion_stats()
        slope = self._entry_slope(mid_z)

        action = SignalAction.HOLD
        chosen_spread = raw_spread
        chosen_z = mid_z
        reason = ""

        if self._position == 0:
            action, reason = self._entry_action(
                mid_z,
                long_z,
                short_z,
                slope=slope,
                half_life=half_life,
                stationarity=stationarity,
            )
            if action is SignalAction.SHORT_SPREAD:
                chosen_spread = short_exec
                chosen_z = mid_z if self.pair.confirm_entry else short_z
                self._position = -1
            elif action is SignalAction.LONG_SPREAD:
                chosen_spread = long_exec
                chosen_z = mid_z if self.pair.confirm_entry else long_z
                self._position = 1
            if action is not SignalAction.HOLD:
                self._entry_mean = reference_raw
                self._entry_std = max(raw_std, 1e-9)
                self._holding_samples = 0
        else:
            self._holding_samples += 1
            liquidation_spread = short_exec if self._position > 0 else long_exec
            chosen_spread = liquidation_spread
            if self.pair.signal_transform == "log_ratio":
                # 平多价差使用 near.bid/far.ask；平空价差使用 near.ask/far.bid。
                # 退出和止损必须基于真实可平仓方向，而不是乐观的 mid ratio。
                liquidation_z = short_z if self._position > 0 else long_z
                chosen_z = liquidation_z
                reverted = (
                    self._position > 0 and liquidation_z >= -self.pair.exit_z
                ) or (
                    self._position < 0 and liquidation_z <= self.pair.exit_z
                )
                stop_reached = (
                    self._position > 0 and liquidation_z <= -self.pair.stop_z
                ) or (
                    self._position < 0 and liquidation_z >= self.pair.stop_z
                )
                if reverted:
                    action = SignalAction.EXIT
                    reason = "executable relative value reverted"
                elif stop_reached:
                    action = SignalAction.EMERGENCY_EXIT
                    reason = "executable relative-value stop reached"
                elif (
                    self.pair.max_holding_samples > 0
                    and self._holding_samples >= self.pair.max_holding_samples
                ):
                    action = SignalAction.EMERGENCY_EXIT
                    reason = "maximum holding period reached"
            else:
                liquidation_z = (
                    liquidation_spread - self._entry_mean
                ) / max(self._entry_std, 1e-9)
                chosen_z = liquidation_z
                current_std = max(raw_std, 1e-9)
                mean_shift_z = max(
                    abs(raw_mean - self._entry_mean),
                    abs(raw_spread - self._entry_mean),
                ) / max(self._entry_std, 1e-9)
                vol_ratio = current_std / max(self._entry_std, 1e-9)
                reverted = (
                    self._position > 0 and liquidation_z >= -self.pair.exit_z
                ) or (
                    self._position < 0 and liquidation_z <= self.pair.exit_z
                )
                structural_break = (
                    mean_shift_z >= self.pair.structural_mean_shift_z
                    or vol_ratio >= self.pair.structural_vol_ratio
                )
                stop_reached = (
                    self._position > 0 and liquidation_z <= -self.pair.stop_z
                ) or (
                    self._position < 0 and liquidation_z >= self.pair.stop_z
                )
                if structural_break:
                    action = SignalAction.EMERGENCY_EXIT
                    reason = "structural break detected"
                elif reverted:
                    action = SignalAction.EXIT
                    reason = "executable spread reverted to entry anchor"
                elif stop_reached:
                    action = SignalAction.EMERGENCY_EXIT
                    reason = "entry anchored executable stop reached"
                elif (
                    self.pair.max_holding_samples > 0
                    and self._holding_samples >= self.pair.max_holding_samples
                ):
                    action = SignalAction.EMERGENCY_EXIT
                    reason = "maximum holding period reached"

            if action in {SignalAction.EXIT, SignalAction.EMERGENCY_EXIT}:
                self._position = 0
                self._holding_samples = 0
                self._armed_direction = 0
                self._armed_extreme = 0.0

        self._z_history.append(mid_z)
        self._append_sample(signal_value, raw_spread, timestamp, near.trading_day)
        return SpreadSignal(
            self.pair.pair_id,
            action,
            chosen_z,
            timestamp,
            chosen_spread,
            reference_raw,
            raw_std,
            reason,
        )

    def _entry_action(
        self,
        mid_z: float,
        long_z: float,
        short_z: float,
        *,
        slope: float,
        half_life: float,
        stationarity: float,
    ) -> tuple[SignalAction, str]:
        if (
            stationarity < self.pair.min_stationarity_score
            or half_life > self.pair.max_half_life
        ):
            self._armed_direction = 0
            self._armed_extreme = 0.0
            return SignalAction.HOLD, ""

        if not self.pair.confirm_entry:
            if short_z >= self.pair.entry_z:
                return SignalAction.SHORT_SPREAD, ""
            if long_z <= -self.pair.entry_z:
                return SignalAction.LONG_SPREAD, ""
            return SignalAction.HOLD, ""

        if self._armed_direction == 0:
            if mid_z >= self.pair.entry_z:
                self._armed_direction = -1
                self._armed_extreme = mid_z
            elif mid_z <= -self.pair.entry_z:
                self._armed_direction = 1
                self._armed_extreme = mid_z
            return SignalAction.HOLD, ""

        if self._armed_direction < 0:
            self._armed_extreme = max(self._armed_extreme, mid_z)
            confirmed = (
                mid_z <= self._armed_extreme - self.pair.confirmation_retrace_z
                and mid_z >= self.pair.min_confirmed_entry_z
            )
            disarmed = mid_z < self.pair.min_confirmed_entry_z
            action = SignalAction.SHORT_SPREAD
        else:
            self._armed_extreme = min(self._armed_extreme, mid_z)
            confirmed = (
                mid_z >= self._armed_extreme + self.pair.confirmation_retrace_z
                and mid_z <= -self.pair.min_confirmed_entry_z
            )
            disarmed = mid_z > -self.pair.min_confirmed_entry_z
            action = SignalAction.LONG_SPREAD

        if confirmed and abs(slope) <= self.pair.max_entry_z_slope:
            self._armed_direction = 0
            self._armed_extreme = 0.0
            return action, "confirmed relative-value reversion"
        if disarmed:
            self._armed_direction = 0
            self._armed_extreme = 0.0
        return SignalAction.HOLD, ""

    def _sample_allowed(self, near: Tick, far: Tick, timestamp) -> bool:
        if self.pair.daily_sample_window:
            if near.trading_day != far.trading_day:
                return False
            if near.trading_day == self._last_sample_trading_day:
                return False
            current = timestamp.astimezone(_CHINA_TZ).timetz().replace(tzinfo=None)
            start, end = self._parse_window(self.pair.daily_sample_window)
            return start <= current <= end
        if self._last_sample_ts is not None and self.pair.sample_seconds > 0:
            return (
                timestamp - self._last_sample_ts
            ).total_seconds() >= self.pair.sample_seconds
        return True

    def _append_sample(
        self,
        signal_value: float,
        raw_spread: float,
        timestamp,
        trading_day: str,
    ) -> None:
        self._history.append(signal_value)
        self._raw_history.append(raw_spread)
        self._last_sample_ts = timestamp
        self._last_sample_trading_day = str(trading_day or "")

    def _hold(self, timestamp, raw_spread: float) -> SpreadSignal:
        return SpreadSignal(
            self.pair.pair_id,
            SignalAction.HOLD,
            0.0,
            timestamp,
            raw_spread,
            self._raw_mean(),
            self.spread_std,
        )

    def _signal_value(self, near_price: float, far_price: float) -> float:
        if self.pair.signal_transform == "log_ratio":
            return log(near_price / far_price)
        return near_price - far_price

    def _executable_signal(self, near_price: float, far_price: float) -> float:
        return self._signal_value(near_price, far_price)

    def _reference_raw_mean(
        self,
        signal_mean: float,
        far_mid: float,
        raw_mean: float,
    ) -> float:
        if self.pair.signal_transform == "log_ratio":
            return far_mid * (exp(signal_mean) - 1.0)
        return raw_mean

    def _entry_slope(self, current_z: float) -> float:
        window = self.pair.entry_trend_window
        values = list(self._z_history)[-(window - 1):] + [current_z]
        if len(values) < window:
            return 0.0
        x_mean = (window - 1) / 2.0
        y_mean = sum(values) / window
        denominator = sum((index - x_mean) ** 2 for index in range(window))
        if denominator <= 0:
            return 0.0
        return sum(
            (index - x_mean) * (value - y_mean)
            for index, value in enumerate(values)
        ) / denominator

    def _mean_reversion_stats(self) -> tuple[float, float]:
        values = list(self._history)
        if len(values) < 4:
            return 999.0, 0.0
        levels = values[:-1]
        changes = [
            values[index + 1] - values[index]
            for index in range(len(values) - 1)
        ]
        level_mean = sum(levels) / len(levels)
        change_mean = sum(changes) / len(changes)
        denominator = sum((value - level_mean) ** 2 for value in levels)
        if denominator <= 1e-12:
            return 999.0, 0.0
        beta = sum(
            (level - level_mean) * (change - change_mean)
            for level, change in zip(levels, changes)
        ) / denominator
        if beta >= 0:
            return 999.0, 0.0
        return max(0.1, -log(2.0) / beta), min(
            1.0, max(0.0, -beta)
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
            raise ValueError(
                "daily sample window must be an intraday increasing window"
            )
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

    def _mean(self) -> float:
        return (
            sum(self._history) / len(self._history)
            if self._history
            else 0.0
        )

    def _std(self, mean: float) -> float:
        if not self._history:
            return 0.0
        variance = sum(
            (value - mean) ** 2 for value in self._history
        ) / len(self._history)
        return sqrt(variance)

    def _raw_mean(self) -> float:
        return (
            sum(self._raw_history) / len(self._raw_history)
            if self._raw_history
            else 0.0
        )

    def _raw_std(self, mean: float) -> float:
        if not self._raw_history:
            return 0.0
        variance = sum(
            (value - mean) ** 2 for value in self._raw_history
        ) / len(self._raw_history)
        return sqrt(variance)
