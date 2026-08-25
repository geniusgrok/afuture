"""组合级滚动相关性和风险组集中度控制。"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
from math import sqrt

from .models import RiskDecision


class PortfolioRiskAnalyzer:
    """从价差序列自行计算相关性，不依赖外部手工输入。

    生产路径优先使用带时间戳的固定时间桶，只在双方共同存在的时间桶上计算
    价差变化相关性，避免不同品种/不同 Tick 频率按“第 N 个样本”错误对齐。
    旧的无时间戳 ``update(pair_id, value)`` 仍保留用于兼容纯单元测试和离线调用。
    """

    def __init__(
        self,
        window: int = 60,
        max_correlation: float = 0.8,
        min_samples: int = 20,
        max_group_open_pairs: int = 1,
        bucket_seconds: int = 60,
    ) -> None:
        self.window = max(3, window)
        self.max_correlation = max_correlation
        self.min_samples = max(3, min_samples)
        self.max_group_open_pairs = max(1, max_group_open_pairs)
        if bucket_seconds <= 0:
            raise ValueError("bucket_seconds must be positive")
        self.bucket_seconds = int(bucket_seconds)
        self._series: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=self.window))
        self._timed_series: dict[str, dict[int, float]] = defaultdict(dict)

    def update(
        self,
        pair_id: str,
        value: float,
        timestamp: datetime | None = None,
    ) -> None:
        """追加一个价差观测；有时间戳时同时维护固定时间桶序列。"""
        numeric = float(value)
        self._series[pair_id].append(numeric)
        if timestamp is None:
            return
        if timestamp.tzinfo is None:
            raise ValueError("portfolio risk timestamp must be timezone-aware")
        bucket = int(timestamp.astimezone(timezone.utc).timestamp()) // self.bucket_seconds
        timed = self._timed_series[pair_id]
        # 同一时间桶内保留最后一个可见值；这等价于低频重采样的 last observation。
        timed[bucket] = numeric
        if len(timed) > self.window:
            for stale in sorted(timed)[: len(timed) - self.window]:
                timed.pop(stale, None)

    def correlation(self, left: str, right: str) -> float:
        """按价差一阶变化计算滚动相关系数。

        如果双方都有带时间戳观测，则只使用共同时间桶；共同样本不足时返回 0，
        绝不退回序号对齐制造伪相关。只有完全没有时间戳数据的旧调用才使用兼容路径。
        """
        left_timed = self._timed_series.get(left, {})
        right_timed = self._timed_series.get(right, {})
        if left_timed or right_timed:
            common = sorted(set(left_timed) & set(right_timed))[-self.window :]
            if len(common) < self.min_samples:
                return 0.0
            left_values = [left_timed[key] for key in common]
            right_values = [right_timed[key] for key in common]
            return self._correlation_from_values(left_values, right_values)

        left_values = list(self._series[left])
        right_values = list(self._series[right])
        sample_count = min(len(left_values), len(right_values))
        if sample_count < self.min_samples:
            return 0.0
        return self._correlation_from_values(
            left_values[-sample_count:],
            right_values[-sample_count:],
        )

    @staticmethod
    def _correlation_from_values(left_values: list[float], right_values: list[float]) -> float:
        sample_count = min(len(left_values), len(right_values))
        if sample_count < 3:
            return 0.0
        left_values = left_values[-sample_count:]
        right_values = right_values[-sample_count:]
        left_changes = [left_values[i] - left_values[i - 1] for i in range(1, sample_count)]
        right_changes = [right_values[i] - right_values[i - 1] for i in range(1, sample_count)]
        if len(left_changes) < 2:
            return 0.0

        left_mean = sum(left_changes) / len(left_changes)
        right_mean = sum(right_changes) / len(right_changes)
        left_var = sum((value - left_mean) ** 2 for value in left_changes)
        right_var = sum((value - right_mean) ** 2 for value in right_changes)
        if left_var <= 0 or right_var <= 0:
            return 0.0

        covariance = sum(
            (left_value - left_mean) * (right_value - right_mean)
            for left_value, right_value in zip(left_changes, right_changes, strict=True)
        )
        return covariance / sqrt(left_var * right_var)

    def allow_open(
        self,
        pair_id: str,
        *,
        risk_group: str,
        open_pairs: dict[str, str],
    ) -> RiskDecision:
        """限制同风险组和高相关套利组合的同时暴露。"""
        if risk_group:
            group_count = sum(1 for group in open_pairs.values() if group == risk_group)
            if group_count >= self.max_group_open_pairs:
                return RiskDecision(False, "risk-group concentration limit reached")

        for other_pair_id in open_pairs:
            if other_pair_id == pair_id:
                continue
            if abs(self.correlation(pair_id, other_pair_id)) >= self.max_correlation:
                return RiskDecision(
                    False,
                    f"pair correlation limit reached versus {other_pair_id}",
                )
        return RiskDecision(True)
