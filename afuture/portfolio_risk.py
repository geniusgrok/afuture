"""组合级滚动相关性和风险组集中度控制。"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite, sqrt

from .models import RiskDecision


@dataclass(frozen=True)
class _CorrelationEvidence:
    known: bool
    correlation: float | None
    reason: str = ""

    @classmethod
    def unknown(cls, reason: str) -> _CorrelationEvidence:
        return cls(False, None, reason)

    @classmethod
    def measured(cls, correlation: float) -> _CorrelationEvidence:
        return cls(True, correlation)


class PortfolioRiskAnalyzer:
    """从价差序列自行计算相关性，不依赖外部手工输入。

    生产路径优先使用带时间戳的固定时间桶，只在双方共同存在的时间桶上计算
    价差变化相关性，避免不同品种/不同 Tick 频率按“第 N 个样本”错误对齐。
    无时间戳 ``update(pair_id, value)`` 用于按统一采样顺序生成的离线序列。
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
        if not isfinite(numeric):
            raise ValueError("portfolio risk value must be finite")

        bucket: int | None = None
        if timestamp is not None:
            if timestamp.tzinfo is None:
                raise ValueError("portfolio risk timestamp must be timezone-aware")
            bucket = int(timestamp.astimezone(timezone.utc).timestamp()) // self.bucket_seconds

        self._series[pair_id].append(numeric)
        if bucket is None:
            return
        timed = self._timed_series[pair_id]
        # 同一时间桶内保留最后一个可见值；这等价于低频重采样的 last observation。
        timed[bucket] = numeric
        if len(timed) > self.window:
            for stale in sorted(timed)[: len(timed) - self.window]:
                timed.pop(stale, None)

    def correlation(self, left: str, right: str) -> float | None:
        """按价差一阶变化计算滚动相关系数。

        任一侧存在带时间戳观测时，只使用共同时间桶；证据不足时返回 ``None``，
        绝不退回序号对齐制造伪相关。两侧都没有时间戳时才使用按观测顺序对齐的数据。
        """
        return self._correlation_evidence(left, right).correlation

    def _correlation_evidence(self, left: str, right: str) -> _CorrelationEvidence:
        left_timed = self._timed_series.get(left, {})
        right_timed = self._timed_series.get(right, {})
        if left_timed or right_timed:
            common = sorted(set(left_timed) & set(right_timed))[-self.window :]
            if len(common) < self.min_samples:
                return _CorrelationEvidence.unknown(
                    "insufficient authoritative common timestamp buckets"
                )
            left_values = [left_timed[key] for key in common]
            right_values = [right_timed[key] for key in common]
            return self._correlation_evidence_from_values(left_values, right_values)

        left_values = list(self._series[left])
        right_values = list(self._series[right])
        sample_count = min(len(left_values), len(right_values))
        if sample_count < self.min_samples:
            return _CorrelationEvidence.unknown("insufficient untimestamped samples")
        return self._correlation_evidence_from_values(
            left_values[-sample_count:],
            right_values[-sample_count:],
        )

    @staticmethod
    def _correlation_evidence_from_values(
        left_values: list[float], right_values: list[float]
    ) -> _CorrelationEvidence:
        sample_count = min(len(left_values), len(right_values))
        if sample_count < 3:
            return _CorrelationEvidence.unknown("insufficient first-difference observations")
        left_values = left_values[-sample_count:]
        right_values = right_values[-sample_count:]
        if not all(isfinite(value) for value in left_values + right_values):
            return _CorrelationEvidence.unknown("non-finite correlation observation")
        left_changes = [left_values[i] - left_values[i - 1] for i in range(1, sample_count)]
        right_changes = [right_values[i] - right_values[i - 1] for i in range(1, sample_count)]
        if len(left_changes) < 2:
            return _CorrelationEvidence.unknown("insufficient first-difference observations")
        if not all(isfinite(value) for value in left_changes + right_changes):
            return _CorrelationEvidence.unknown("non-finite first-difference observation")

        left_mean = sum(left_changes) / len(left_changes)
        right_mean = sum(right_changes) / len(right_changes)
        left_var = sum((value - left_mean) ** 2 for value in left_changes)
        right_var = sum((value - right_mean) ** 2 for value in right_changes)
        if not isfinite(left_var) or not isfinite(right_var):
            return _CorrelationEvidence.unknown("non-finite change variance")
        if left_var <= 1e-12 or right_var <= 1e-12:
            return _CorrelationEvidence.unknown("zero or near-zero change variance")

        covariance = sum(
            (left_value - left_mean) * (right_value - right_mean)
            for left_value, right_value in zip(left_changes, right_changes, strict=True)
        )
        denominator = sqrt(left_var * right_var)
        if not isfinite(covariance) or not isfinite(denominator) or denominator <= 0:
            return _CorrelationEvidence.unknown("non-finite correlation calculation")
        correlation = covariance / denominator
        if not isfinite(correlation):
            return _CorrelationEvidence.unknown("non-finite correlation result")
        return _CorrelationEvidence.measured(correlation)

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
            evidence = self._correlation_evidence(pair_id, other_pair_id)
            if not evidence.known:
                return RiskDecision(
                    False,
                    f"unknown correlation evidence versus {other_pair_id}: {evidence.reason}",
                )
            correlation = evidence.correlation
            if correlation is not None and abs(correlation) >= self.max_correlation:
                return RiskDecision(
                    False,
                    f"pair correlation limit reached versus {other_pair_id}",
                )
        return RiskDecision(True)
