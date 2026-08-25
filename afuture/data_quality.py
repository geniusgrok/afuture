"""历史/研究数据质量检查。

目标不是建设数据平台，而是在研究前明确回答覆盖、断档、盘口、活动度和合约生命周期
是否足够可靠，并识别单一品种垄断样本导致的伪泛化。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

from .auto import AutoConfig, AutoPairSelector
from .models import ContractInfo, Tick


@dataclass(frozen=True)
class DataQualityResult:
    tick_count: int
    contract_count: int
    trading_days: int
    duplicate_count: int
    out_of_order_count: int
    invalid_quote_count: int
    activity_missing_count: int
    activity_missing_ratio: float
    limit_missing_count: int
    gap_count: int
    samples_by_day: dict[str, int]
    coverage_by_product: dict[str, dict]
    max_product_sample_share: float
    daily_auto_candidates: dict[str, int] = field(default_factory=dict)
    hard_failures: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.hard_failures

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["passed"] = self.passed
        return payload


class DataQualityAnalyzer:
    """检查研究数据是否足以支持 Auto Universe，而不只支持一个固定 pair。"""

    def __init__(self, max_gap_seconds: float = 300.0) -> None:
        if max_gap_seconds <= 0:
            raise ValueError("max_gap_seconds must be positive")
        self.max_gap_seconds = max_gap_seconds

    def analyze(
        self,
        ticks: list[Tick],
        catalog: list[ContractInfo] | None = None,
        auto_config: AutoConfig | None = None,
    ) -> DataQualityResult:
        duplicates = 0
        out_of_order = 0
        invalid = 0
        activity_missing = 0
        limit_missing = 0
        seen: set[tuple[str, datetime]] = set()
        last_seen: dict[str, datetime] = {}
        rows_by_symbol: dict[str, list[Tick]] = defaultdict(list)
        symbols_by_day: dict[str, set[str]] = defaultdict(set)
        samples_by_day: dict[str, int] = defaultdict(int)

        for row in ticks:
            key = (row.symbol, row.timestamp)
            if key in seen:
                duplicates += 1
            seen.add(key)
            previous = last_seen.get(row.symbol)
            if previous is not None and row.timestamp < previous:
                out_of_order += 1
            last_seen[row.symbol] = row.timestamp
            try:
                row.validate()
            except ValueError:
                invalid += 1
            if row.volume <= 0 or row.open_interest <= 0:
                activity_missing += 1
            if row.limit_up <= 0 or row.limit_down <= 0:
                limit_missing += 1
            rows_by_symbol[row.symbol].append(row)
            symbols_by_day[row.trading_day].add(row.symbol)
            samples_by_day[row.trading_day] += 1

        gaps = 0
        for rows in rows_by_symbol.values():
            ordered = sorted(rows, key=lambda item: item.timestamp)
            for left, right in zip(ordered, ordered[1:], strict=False):
                if left.trading_day != right.trading_day:
                    continue
                if (right.timestamp - left.timestamp).total_seconds() > self.max_gap_seconds:
                    gaps += 1

        catalog = list(catalog or [])
        product_by_symbol = {item.symbol: item.product.lower() for item in catalog}
        coverage: dict[str, dict] = {}
        symbols_by_product: dict[str, set[str]] = defaultdict(set)
        days_by_product: dict[str, set[str]] = defaultdict(set)
        samples_by_product: dict[str, int] = defaultdict(int)
        for symbol, rows in rows_by_symbol.items():
            product = product_by_symbol.get(symbol)
            if not product:
                product = "".join(ch for ch in symbol if ch.isalpha()).lower() or symbol.lower()
            symbols_by_product[product].add(symbol)
            days_by_product[product].update(row.trading_day for row in rows)
            samples_by_product[product] += len(rows)
        for product in sorted(symbols_by_product):
            coverage[product] = {
                "contracts": len(symbols_by_product[product]),
                "trading_days": len(days_by_product[product]),
                "samples": samples_by_product[product],
                "sample_share": samples_by_product[product] / max(len(ticks), 1),
                "symbols": sorted(symbols_by_product[product]),
            }
        max_product_share = max((row["sample_share"] for row in coverage.values()), default=0.0)

        daily_candidates: dict[str, int] = {}
        if catalog and auto_config is not None and auto_config.enabled:
            selector = AutoPairSelector(auto_config)
            for trading_day in sorted(symbols_by_day):
                try:
                    day = date(int(trading_day[:4]), int(trading_day[4:6]), int(trading_day[6:8]))
                except Exception:
                    continue
                observed = symbols_by_day[trading_day]
                daily_catalog = [item for item in catalog if item.symbol in observed]
                daily_candidates[trading_day] = len(selector.build_pairs(daily_catalog, day))

        failures: list[str] = []
        warnings: list[str] = []
        if not ticks:
            failures.append("dataset is empty")
        if invalid:
            failures.append(f"invalid quotes: {invalid}")
        if out_of_order:
            failures.append(f"out-of-order rows: {out_of_order}")
        if duplicates:
            warnings.append(f"duplicate symbol/timestamp rows: {duplicates}")
        if activity_missing:
            warnings.append(f"rows missing volume/open_interest: {activity_missing}")
        if limit_missing:
            warnings.append(f"rows missing daily price limits: {limit_missing}")
        if gaps:
            warnings.append(f"long intra-day gaps: {gaps}")
        if len(coverage) > 1 and max_product_share >= 0.80:
            warnings.append(f"single product dominates dataset samples: {max_product_share:.1%}")
        zero_candidate_days = [day for day, count in daily_candidates.items() if count <= 0]
        if zero_candidate_days:
            failures.append(
                "auto universe has no observed two-leg candidate on trading days: "
                + ",".join(zero_candidate_days[:10])
            )

        return DataQualityResult(
            tick_count=len(ticks),
            contract_count=len(rows_by_symbol),
            trading_days=len(symbols_by_day),
            duplicate_count=duplicates,
            out_of_order_count=out_of_order,
            invalid_quote_count=invalid,
            activity_missing_count=activity_missing,
            activity_missing_ratio=activity_missing / max(len(ticks), 1),
            limit_missing_count=limit_missing,
            gap_count=gaps,
            samples_by_day=dict(sorted(samples_by_day.items())),
            coverage_by_product=coverage,
            max_product_sample_share=max_product_share,
            daily_auto_candidates=daily_candidates,
            hard_failures=tuple(failures),
            warnings=tuple(warnings),
        )
