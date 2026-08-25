"""实盘合约元数据校验。

本地配置可以比柜台更保守，但不得低估保证金、手续费，
也不得错误填写合约乘数和最小变动价位。
"""

from math import isfinite

from .models import ContractSpec, RiskDecision


def _finite_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and isfinite(value)


def validate_contract_metadata(
    configured: dict[str, ContractSpec],
    live: dict[str, ContractSpec],
    *,
    tolerance: float = 1e-9,
) -> RiskDecision:
    """确认生产配置不会低估柜台真实交易成本和资金占用。"""
    if not _finite_number(tolerance) or tolerance < 0:
        return RiskDecision(False, "invalid metadata tolerance")
    for symbol, local in configured.items():
        remote = live.get(symbol)
        if remote is None:
            return RiskDecision(False, f"missing live contract metadata: {symbol}")
        for source, spec in (("configured", local), ("live", remote)):
            if spec.symbol != symbol or not spec.exchange:
                return RiskDecision(False, f"invalid {source} contract identity: {symbol}")
            positives = {
                "multiplier": spec.multiplier,
                "price_tick": spec.price_tick,
                "margin_rate_long": spec.margin_rate_long,
                "margin_rate_short": spec.margin_rate_short,
            }
            for name, value in positives.items():
                if not _finite_number(value) or value <= 0:
                    return RiskDecision(
                        False,
                        f"invalid {source} contract {name}: {symbol}",
                    )
            for name in spec.fee.__dataclass_fields__:
                value = getattr(spec.fee, name)
                if not _finite_number(value) or value < 0:
                    return RiskDecision(
                        False,
                        f"invalid {source} contract fee: {symbol}/{name}",
                    )
        if local.exchange != remote.exchange:
            return RiskDecision(False, f"exchange mismatch: {symbol}")
        if abs(local.multiplier - remote.multiplier) > tolerance:
            return RiskDecision(False, f"multiplier mismatch: {symbol}")
        if abs(local.price_tick - remote.price_tick) > tolerance:
            return RiskDecision(False, f"price tick mismatch: {symbol}")

        if (
            local.margin_rate_long + tolerance < remote.margin_rate_long
            or local.margin_rate_short + tolerance < remote.margin_rate_short
        ):
            return RiskDecision(
                False,
                f"configured margin understates live margin: {symbol}",
            )

        for name in local.fee.__dataclass_fields__:
            if getattr(local.fee, name) + tolerance < getattr(remote.fee, name):
                return RiskDecision(
                    False,
                    f"configured fee understates live fee: {symbol}/{name}",
                )
    return RiskDecision(True)
