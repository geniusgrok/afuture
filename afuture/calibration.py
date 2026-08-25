"""参数稳定区域校准。

目标不是寻找历史最高分的孤立参数，而是优先选择周围参数也能维持相近表现的稳定区域。
"""

from __future__ import annotations


class ParameterCalibrator:
    """按邻域风险调整分选择稳定参数。"""

    _EXTENDED_RESEARCH_KEYS = (
        "min_net_edge",
        "min_stationarity_score",
        "max_half_life",
        "min_persistence_score",
        "max_volatility_percentile",
        "max_trend_shift_z",
        "min_carry_reversal_z",
        "carry_reversal_weight",
        "risk_budget_ratio",
        "max_pair_volume",
    )

    def __init__(self, neighbor_radius: float = 0.20, min_neighbors: int = 2) -> None:
        if neighbor_radius <= 0:
            raise ValueError("neighbor_radius must be positive")
        if min_neighbors <= 0:
            raise ValueError("min_neighbors must be positive")
        self.neighbor_radius = neighbor_radius
        self.min_neighbors = min_neighbors

    def select_best(
        self,
        results: list[dict],
        *,
        parameter_keys: tuple[str, ...] | None = None,
        grid_adjacency: bool = False,
    ) -> dict | None:
        """返回稳定区域中心；多个候选均孤立时拒绝选出历史尖峰。

        默认保留原有相对距离邻域语义。显式启用 ``grid_adjacency`` 时，只有在一个
        参数轴上相邻的点才属于同一局部区域。对于 Auto Portfolio 的 Quality、
        Regime/Carry、Risk 阶段，只要扩展研究维度实际发生变化，也自动使用网格邻接
        并要求至少两个周围参数点，避免这些维度被旧的四参数邻域规则静默忽略。
        """
        if not results:
            return None
        if len(results) == 1:
            return results[0]

        auto_grid = self._extended_grid_varies(results)
        if grid_adjacency or auto_grid:
            required_neighbors = (
                self.min_neighbors if grid_adjacency else max(self.min_neighbors, 3)
            )
            return self._select_best_grid(
                results,
                parameter_keys,
                required_neighbors=required_neighbors,
            )

        stable_candidates: list[tuple[float, float, dict]] = []
        for row in results:
            neighbors = [other for other in results if self._neighbor(row, other)]
            if len(neighbors) < self.min_neighbors:
                continue
            neighborhood_score = sum(self._risk_adjusted(item) for item in neighbors) / len(
                neighbors
            )
            own_score = self._risk_adjusted(row)
            stable_candidates.append((neighborhood_score, own_score, row))

        if not stable_candidates:
            return None
        return max(stable_candidates, key=lambda item: (item[0], item[1]))[2]

    def _select_best_grid(
        self,
        results: list[dict],
        parameter_keys: tuple[str, ...] | None,
        *,
        required_neighbors: int,
    ) -> dict | None:
        keys = self._grid_keys(results, parameter_keys)
        if not keys:
            return None
        axes = {key: sorted({float(row[key]) for row in results}) for key in keys}
        stable_candidates: list[tuple[float, int, float, dict]] = []
        for row in results:
            neighbors = [other for other in results if self._grid_neighbor(row, other, keys, axes)]
            if len(neighbors) < required_neighbors:
                continue
            support = [other for other in neighbors if other is not row]
            if not support:
                continue
            own_score = self._risk_adjusted(row)
            support_score = sum(self._risk_adjusted(item) for item in support) / len(support)
            # 中心和邻域必须同时成立；单点尖峰不能用自身高分抬高稳定分。
            stability_score = min(own_score, support_score)
            stable_candidates.append((stability_score, len(support), support_score, row))
        if not stable_candidates:
            return None
        return max(
            stable_candidates,
            key=lambda item: (item[0], item[1], item[2]),
        )[3]

    @classmethod
    def _extended_grid_varies(cls, results: list[dict]) -> bool:
        for key in cls._EXTENDED_RESEARCH_KEYS:
            if not all(key in row for row in results):
                continue
            if len({float(row[key]) for row in results}) > 1:
                return True
        return False

    @classmethod
    def _grid_keys(
        cls,
        results: list[dict],
        parameter_keys: tuple[str, ...] | None,
    ) -> tuple[str, ...]:
        requested = parameter_keys or (
            "lookback",
            "entry_z",
            "exit_z",
            "stop_z",
            *cls._EXTENDED_RESEARCH_KEYS,
        )
        return tuple(key for key in requested if all(key in row for row in results))

    @staticmethod
    def _grid_neighbor(
        left: dict,
        right: dict,
        keys: tuple[str, ...],
        axes: dict[str, list[float]],
    ) -> bool:
        changed: list[str] = []
        for key in keys:
            left_value = float(left[key])
            right_value = float(right[key])
            if left_value == right_value:
                continue
            changed.append(key)
            if len(changed) > 1:
                return False
        if not changed:
            return True
        key = changed[0]
        axis = axes[key]
        left_index = axis.index(float(left[key]))
        right_index = axis.index(float(right[key]))
        return abs(left_index - right_index) == 1

    def _neighbor(self, left: dict, right: dict) -> bool:
        keys = [
            key
            for key in ("lookback", "entry_z", "exit_z", "stop_z")
            if key in left and key in right
        ]
        if not keys:
            return left is right
        for key in keys:
            left_value = float(left[key])
            right_value = float(right[key])
            scale = max(abs(left_value), abs(right_value), 1e-9)
            if abs(left_value - right_value) / scale > self.neighbor_radius:
                return False
        return True

    @staticmethod
    def _risk_adjusted(row: dict) -> float:
        drawdown = abs(float(row.get("max_drawdown", 0.0)))
        score = float(row.get("score", row.get("sharpe", 0.0)))
        return score - drawdown
