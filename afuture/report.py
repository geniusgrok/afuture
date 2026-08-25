"""运行结果和研究绩效指标。"""

import json
from math import sqrt
from pathlib import Path


def calculate_performance(
    equity_curve,
    *,
    initial_capital: float,
    trade_count: int,
) -> dict[str, float | int]:
    """按交易日末权益计算收益、回撤和日频夏普。"""
    if initial_capital <= 0:
        raise ValueError("initial_capital must be positive")

    daily: dict[str, float] = {}
    for trading_day, equity in equity_curve:
        daily[trading_day] = float(equity)

    values = list(daily.values())
    final_equity = values[-1] if values else initial_capital

    high_watermark = initial_capital
    max_drawdown = 0.0
    for equity in values:
        high_watermark = max(high_watermark, equity)
        if high_watermark > 0:
            max_drawdown = min(
                max_drawdown,
                equity / high_watermark - 1.0,
            )

    returns: list[float] = []
    previous = initial_capital
    for equity in values:
        if previous > 0:
            returns.append(equity / previous - 1.0)
        previous = equity

    sharpe = 0.0
    if len(returns) >= 2:
        mean_return = sum(returns) / len(returns)
        variance = sum((value - mean_return) ** 2 for value in returns) / (len(returns) - 1)
        if variance > 0:
            sharpe = mean_return / sqrt(variance) * sqrt(252.0)

    annualized_return = 0.0
    if values and final_equity > 0:
        annualized_return = (final_equity / initial_capital) ** (252.0 / len(values)) - 1.0

    return {
        "initial_capital": initial_capital,
        "final_equity": final_equity,
        "total_return": final_equity / initial_capital - 1.0,
        "annualized_return": annualized_return,
        "max_drawdown": max_drawdown,
        "sharpe": sharpe,
        "trading_days": len(values),
        "trade_count": trade_count,
    }


def write_account_report(
    path: str | Path,
    account,
    positions,
    metrics: dict | None = None,
) -> None:
    """将账户、持仓和可选绩效指标写为便于人工核验的 JSON。"""
    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "account": account.__dict__,
        "positions": [position.__dict__ for position in positions],
    }
    if metrics is not None:
        payload["performance"] = metrics
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
