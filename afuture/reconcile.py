"""本地期望持仓与柜台完整持仓快照对账。"""

from dataclasses import dataclass

from .models import ContractPosition


@dataclass(frozen=True)
class ReconcileResult:
    """持仓对账结果。"""

    matched: bool
    details: str = ""


def compare_positions(
    local: list[ContractPosition],
    remote: list[ContractPosition],
) -> ReconcileResult:
    """逐合约比较今昨、多空数量；均价差异不影响能否继续发单。"""

    def normalized(
        items: list[ContractPosition],
    ) -> tuple[dict[tuple[str, str], tuple[int, int, int, int]], list[tuple[str, str]]]:
        result: dict[tuple[str, str], tuple[int, int, int, int]] = {}
        duplicates: list[tuple[str, str]] = []
        for position in items:
            if position.empty:
                continue
            key = (position.symbol, position.exchange)
            if key in result:
                duplicates.append(key)
                continue
            result[key] = (
                position.long_today,
                position.long_yesterday,
                position.short_today,
                position.short_yesterday,
            )
        return result, duplicates

    left, left_duplicates = normalized(local)
    right, right_duplicates = normalized(remote)
    if left_duplicates or right_duplicates:
        return ReconcileResult(
            False,
            f"duplicate local={left_duplicates}, remote={right_duplicates}",
        )
    if left == right:
        return ReconcileResult(True)

    diffs = []
    for symbol, exchange in sorted(set(left) | set(right)):
        key = (symbol, exchange)
        if left.get(key) != right.get(key):
            diffs.append(f"{symbol}.{exchange}: local={left.get(key)}, remote={right.get(key)}")
    return ReconcileResult(False, "; ".join(diffs))
