"""实际执行与候选质量证据。

把模型预期、Auto 候选判断和实际成交分开记录，避免只看账户收益而不知道
selector、手续费、滑点或裸腿修复中的哪一层吞掉 Edge。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from statistics import median


class ExecutionQualityRecorder:
    """以 JSONL 保存套利与方向组合执行证据，并提供轻量汇总。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def record_round_trip(
        self,
        *,
        pair_id: str,
        expected_net_edge: float,
        realized_net_edge: float,
        expected_spread: float,
        entry_spread: float,
        exit_spread: float,
        commission: float,
        leg_latency_ms: float,
        partial_fill: bool,
        rollback: bool,
        reduce_only: bool,
        extra: dict | None = None,
    ) -> None:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "round_trip",
            "pair_id": pair_id,
            "expected_net_edge": float(expected_net_edge),
            "realized_net_edge": float(realized_net_edge),
            "expected_spread": float(expected_spread),
            "entry_spread": float(entry_spread),
            "exit_spread": float(exit_spread),
            "slippage": abs(float(entry_spread) - float(expected_spread)),
            "commission": float(commission),
            "leg_latency_ms": float(leg_latency_ms),
            "partial_fill": bool(partial_fill),
            "rollback": bool(rollback),
            "reduce_only": bool(reduce_only),
        }
        if extra:
            payload["extra"] = dict(extra)
        self._append(payload)

    def record_candidate(self, **fields) -> None:
        self._append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "candidate",
                **fields,
            }
        )

    def record_decision(self, **fields) -> None:
        self._append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "decision",
                **fields,
            }
        )

    def record_directional_rebalance(
        self,
        *,
        cycle_id: str,
        signal_day: str,
        activity_day: str,
        target_gross: float,
        target_lots: dict[str, int],
        reductions: dict[str, int],
        openings: dict[str, int],
        planned_turnover_notional: float,
        reason: str = "",
    ) -> None:
        self._append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "directional_rebalance",
                "cycle_id": str(cycle_id),
                "signal_day": str(signal_day),
                "activity_day": str(activity_day),
                "target_gross": float(target_gross),
                "target_lots": {str(k): int(v) for k, v in target_lots.items()},
                "reductions": {str(k): int(v) for k, v in reductions.items()},
                "openings": {str(k): int(v) for k, v in openings.items()},
                "planned_turnover_notional": float(planned_turnover_notional),
                "reason": str(reason),
            }
        )

    def record_directional_fill(
        self,
        *,
        cycle_id: str,
        order_id: str,
        product: str,
        symbol: str,
        side: str,
        offset: str,
        expected_price: float,
        fill_price: float,
        volume: int,
        multiplier: float,
        slippage_bps: float,
        commission: float,
        commission_source: str,
        fill_notional: float,
    ) -> None:
        self._append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "directional_fill",
                "cycle_id": str(cycle_id),
                "order_id": str(order_id),
                "product": str(product),
                "symbol": str(symbol),
                "side": str(side),
                "offset": str(offset),
                "expected_price": float(expected_price),
                "fill_price": float(fill_price),
                "volume": int(volume),
                "multiplier": float(multiplier),
                "slippage_bps": float(slippage_bps),
                "commission": float(commission),
                "commission_source": str(commission_source),
                "fill_notional": float(fill_notional),
            }
        )

    def record_directional_cycle(
        self,
        *,
        cycle_id: str,
        target_tracking_error: float,
        completion_latency_ms: float,
        partial_count: int,
        rejected_count: int,
        realized_turnover_notional: float,
    ) -> None:
        self._append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "directional_cycle",
                "cycle_id": str(cycle_id),
                "target_tracking_error": float(target_tracking_error),
                "completion_latency_ms": float(completion_latency_ms),
                "partial_count": int(partial_count),
                "rejected_count": int(rejected_count),
                "realized_turnover_notional": float(realized_turnover_notional),
            }
        )

    def summary(self) -> dict:
        all_rows = self._read()
        rows = [row for row in all_rows if row.get("event") == "round_trip"]
        slippage = sorted(float(row.get("slippage", 0.0)) for row in rows)
        realized = [float(row.get("realized_net_edge", 0.0)) for row in rows]
        commissions = [float(row.get("commission", 0.0)) for row in rows]
        latencies = [float(row.get("leg_latency_ms", 0.0)) for row in rows]
        candidates = [row for row in all_rows if row.get("event") == "candidate"]
        decisions = [row for row in all_rows if row.get("event") == "decision"]

        d_rebalances = [row for row in all_rows if row.get("event") == "directional_rebalance"]
        d_fills = [row for row in all_rows if row.get("event") == "directional_fill"]
        d_cycles = [row for row in all_rows if row.get("event") == "directional_cycle"]
        d_slippage = sorted(float(row.get("slippage_bps", 0.0)) for row in d_fills)
        d_tracking = [float(row.get("target_tracking_error", 0.0)) for row in d_cycles]
        d_latency = [float(row.get("completion_latency_ms", 0.0)) for row in d_cycles]

        return {
            "candidate_events": len(candidates),
            "decision_events": len(decisions),
            "round_trips": len(rows),
            "median_slippage": median(slippage) if slippage else 0.0,
            "p95_slippage": self._percentile(slippage, 0.95),
            "realized_edge_total": sum(realized),
            "commission_total": sum(commissions),
            "median_leg_latency_ms": median(latencies) if latencies else 0.0,
            "partial_fill_count": sum(bool(row.get("partial_fill")) for row in rows),
            "rollback_count": sum(bool(row.get("rollback")) for row in rows),
            "reduce_only_count": sum(bool(row.get("reduce_only")) for row in rows),
            "directional": {
                "rebalance_events": len(d_rebalances),
                "fill_count": len(d_fills),
                "cycles": len(d_cycles),
                "turnover_notional": sum(
                    float(row.get("realized_turnover_notional", 0.0)) for row in d_cycles
                ),
                "commission_total": sum(float(row.get("commission", 0.0)) for row in d_fills),
                "median_slippage_bps": median(d_slippage) if d_slippage else 0.0,
                "p95_slippage_bps": self._percentile(d_slippage, 0.95),
                "median_tracking_error": median(d_tracking) if d_tracking else 0.0,
                "median_completion_latency_ms": median(d_latency) if d_latency else 0.0,
                "partial_count": sum(int(row.get("partial_count", 0)) for row in d_cycles),
                "rejected_count": sum(int(row.get("rejected_count", 0)) for row in d_cycles),
            },
        }

    def _append(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        result: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return result

    @staticmethod
    def _percentile(values: list[float], ratio: float) -> float:
        if not values:
            return 0.0
        index = max(0, min(len(values) - 1, int(round((len(values) - 1) * ratio))))
        return float(values[index])
