"""历史 Tick 回放入口。"""

from __future__ import annotations

from itertools import groupby
from pathlib import Path

from .auto import AutoPairManager
from .broker.sim import SimBroker
from .data import read_ticks
from .engine import TradingEngine
from .journal import AuditJournal
from .report import calculate_performance, write_account_report
from .risk import RiskManager
from .state import StateStore


def run_replay(config, data_path: str | Path):
    """使用与实盘相同的策略/风控/执行链路执行确定性历史回放。

    同一时间戳的多腿行情会先一起注入模拟柜台，再执行一次事件循环。这样历史数据
    即使按分钟采样，也不会因为“先喂近月、后喂远月”的文件顺序被健康监控误判为
    一条腿已经陈旧。
    """
    state_path = Path(config.state_path)
    if state_path.exists():
        state_path.unlink()

    broker = SimBroker(
        config.initial_capital,
        config.contracts,
        slippage_ticks=config.slippage_ticks,
        conservative=config.conservative_simulation,
        latency_ticks=config.latency_ticks,
        market_impact_ticks=config.market_impact_ticks,
        contract_catalog=config.contract_catalog,
    )
    engine = TradingEngine(
        broker,
        config.pairs,
        config.contracts,
        RiskManager(config.risk),
        StateStore(config.state_path),
        auto_flatten_imbalance=config.auto_flatten_imbalance,
        aggressive_ticks=config.aggressive_ticks,
        slippage_ticks=config.slippage_ticks,
        legging_timeout_seconds=config.legging_timeout_seconds,
        journal=AuditJournal(config.journal_path),
        auto_manager=(AutoPairManager(config.auto) if config.auto.enabled else None),
        require_live_metadata=False,
        historical_mode=True,
    )
    ticks = read_ticks(data_path)
    equity_curve: list[tuple[str, float]] = []
    engine.start()
    try:
        for _, group in groupby(ticks, key=lambda tick: tick.timestamp):
            batch = list(group)
            for tick in batch:
                broker.publish_tick(tick)
            engine.run_once()
            account = broker.get_account()
            # 同一批只记录一次权益，避免一条腿先更新造成无意义的曲线重复点。
            equity_curve.append((batch[-1].trading_day, account.equity))

        account = broker.get_account()
        metrics = calculate_performance(
            equity_curve,
            initial_capital=config.initial_capital,
            trade_count=len(broker.get_trades()),
        )
        write_account_report(
            config.report_path,
            account,
            broker.get_positions(),
            metrics,
        )
        return account
    finally:
        engine.stop()
