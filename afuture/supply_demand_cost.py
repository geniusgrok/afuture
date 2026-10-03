"""Research-only causal episode cost qualification and shared target netting.

Neither function owns an account or enables live trading. Paper episode prices
use the disclosed common daily execution proxy, not certified counter quotes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from statistics import median


@dataclass(frozen=True)
class SupplyEpisode:
    product: str
    direction: int
    entered_at: datetime
    completed_at: datetime
    known_at: datetime
    entry_notional: float
    gross_pnl: float
    turnover_notional: float
    episode_id: str

    def __post_init__(self) -> None:
        if (
            not self.product.isalpha()
            or self.product != self.product.upper()
            or type(self.direction) is not int
            or self.direction not in (-1, 1)
            or self.entered_at.tzinfo is None
            or self.entered_at.utcoffset() is None
            or self.completed_at.tzinfo is None
            or self.completed_at.utcoffset() is None
            or self.completed_at <= self.entered_at
            or self.known_at.tzinfo is None
            or self.known_at.utcoffset() is None
            or self.known_at < self.completed_at
            or not all(
                isfinite(v) for v in (self.entry_notional, self.gross_pnl, self.turnover_notional)
            )
            or self.entry_notional <= 0
            or self.turnover_notional < self.entry_notional
            or not self.episode_id
        ):
            raise ValueError("invalid completed supply episode")


def episode_cost_gate(
    *,
    targets: Mapping[str, float],
    prior_approved: Mapping[str, float],
    episodes: Sequence[SupplyEpisode],
    decision_at: datetime,
) -> tuple[dict[str, float], list[dict]]:
    """Freeze one estimator: >=4 prior same-product/signed episodes; Stress cost.

    Median(gross minus all entry/exit/roll fees), in entry-notional basis points,
    must be positive. Unfinished episodes never occur in this input; future exits
    are filtered. Reductions always pass; an unqualified reversal closes to zero.
    Four samples are a research readiness floor, not statistical certification.
    """
    if decision_at.tzinfo is None or decision_at.utcoffset() is None:
        raise ValueError("decision time requires an explicit timezone")
    if len({e.episode_id for e in episodes}) != len(episodes):
        raise ValueError("duplicate supply episode identity")
    ends: dict[str, datetime] = {}
    for episode in sorted(episodes, key=lambda e: e.entered_at):
        if episode.product in ends and episode.entered_at < ends[episode.product]:
            raise ValueError("overlapping supply episodes")
        ends[episode.product] = episode.completed_at
    output, audit = {}, []
    for product, target in targets.items():
        current = float(prior_approved.get(product, 0.0))
        target = float(target)
        if not isfinite(target) or not isfinite(current):
            raise ValueError("supply weights must be finite")
        reversal = current * target < 0
        increase = target != 0 and (current == 0 or reversal or abs(target) > abs(current))
        applied = target
        if increase:
            samples = [
                e
                for e in episodes
                if e.product == product
                and e.direction == (1 if target > 0 else -1)
                and e.known_at <= decision_at
            ]
            benefits = [
                (10000 * e.gross_pnl - 15 * e.turnover_notional) / e.entry_notional for e in samples
            ]
            estimate = median(benefits) if len(samples) >= 4 else None
            if estimate is None or estimate <= 0:
                applied = 0.0 if current == 0 or reversal else current
            audit.append(
                dict(
                    product=product,
                    completed_samples=len(samples),
                    stress_net_bps_median=estimate,
                    approved=applied == target,
                )
            )
        output[product] = applied
    return output, audit


def net_supply_with_baseline(
    baseline: Mapping[str, float], supply: Mapping[str, float]
) -> dict[str, float]:
    """One uniform spare-gross rule, followed by the existing account's limits.

    Both mechanisms use the shared concrete-contract selector. Opposite requests
    cancel before integer rounding. Preserve remaining baseline requests; scale
    all incremental requests equally to the remaining 2x gross envelope. This is
    a target budget, not a claim about spare cash or unchanged baseline equity.
    """
    if set(baseline) != set(supply):
        raise ValueError("baseline and supply product support must match")
    if any(not isfinite(float(v)) for row in (baseline, supply) for v in row.values()):
        raise ValueError("shared targets must be finite")
    if sum(abs(v) for v in baseline.values()) > 2 + 1e-12:
        raise ValueError("baseline exceeds original gross envelope")
    remaining, incremental = {}, {}
    for product, base in baseline.items():
        added = float(supply[product])
        cancel = min(abs(base), abs(added)) if base * added < 0 else 0.0
        remaining[product] = base - (cancel if base > 0 else -cancel)
        incremental[product] = added - (cancel if added > 0 else -cancel)
    spare = max(0.0, 2 - sum(abs(v) for v in remaining.values()))
    requested = sum(abs(v) for v in incremental.values())
    scale = min(1.0, spare / requested) if requested else 0.0
    return {p: remaining[p] + scale * incremental[p] for p in baseline}
