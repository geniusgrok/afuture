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
    ) -> dict[str, tuple[int, int, int, int]]:
        return {
            position.symbol: (
                position.long_today,
                position.long_yesterday,
                position.short_today,
                position.short_yesterday,
            )
            for position in items
            if not position.empty
        }

    left = normalized(local)
    right = normalized(remote)
    if left == right:
        return ReconcileResult(True)

    diffs = []
    for symbol in sorted(set(left) | set(right)):
        if left.get(symbol) != right.get(symbol):
            diffs.append(f"{symbol}: local={left.get(symbol)}, remote={right.get(symbol)}")
    return ReconcileResult(False, "; ".join(diffs))
