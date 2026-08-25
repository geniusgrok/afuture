"""命令行入口。

实盘相关命令默认采用 fail-closed：柜台、完整账户/持仓快照、元数据和持仓对账
任一安全门未通过，都不会进入正常交易循环。Shadow 连接真实 CTP，但订单永远只进入本地模拟柜台。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

from .alerts import AlertManager, AlertSink, FileAlertSink, WebhookAlertSink
from .config import load_config
from .data import read_ticks
from .logging_utils import configure_logging
from .metadata import validate_contract_metadata
from .models import AccountSnapshot, ContractInfo, ContractPosition, PairConfig, RuntimeMode, Tick
from .quality import ExecutionQualityRecorder
from .research import AcceptanceGate, ResearchConfig, WalkForwardRunner
from .sample_store import MarketSampleStore
from .scanner import SpreadScanner
from .state import RuntimeState, StateStore

_LIVE_ACK = "I_UNDERSTAND_FUTURES_RISK"
_RECOVERY_ACK = "I_VERIFIED_CTP_POSITIONS"


def build_parser() -> argparse.ArgumentParser:
    """创建 CLI，并把研究、观察和真实交易入口明确分离。"""
    parser = argparse.ArgumentParser(prog="afuture", description="国内期货套利与方向组合交易系统")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="校验配置，不连接柜台")
    validate.add_argument("--config", required=True)

    replay = sub.add_parser("replay", help="历史 Tick 回放")
    replay.add_argument("--config", required=True)
    replay.add_argument("--data", required=True)

    scan = sub.add_parser("scan", help="扫描跨期套利研究候选")
    scan.add_argument("--config", required=True)
    scan.add_argument("--data", required=True)

    accept = sub.add_parser("accept", help="执行单 pair Walk-forward/OOS/Stress 晋级验收")
    accept.add_argument("--config", required=True)
    accept.add_argument("--data", required=True)
    accept.add_argument("--pair", required=True)
    accept.add_argument("--train-days", type=int, default=60)
    accept.add_argument("--validation-days", type=int, default=20)
    accept.add_argument("--oos-days", type=int, default=20)
    accept.add_argument("--step-days", type=int, default=20)
    accept.add_argument(
        "--stress-multipliers",
        default="1.0,1.5,2.0",
        help="用逗号分隔的交易成本压力倍数",
    )

    accept_auto = sub.add_parser(
        "accept-auto", help="对最终 Auto Portfolio 执行 Walk-forward/OOS/鲁棒性验收"
    )
    accept_auto.add_argument("--config", required=True)
    accept_auto.add_argument("--data", required=True)
    accept_auto.add_argument("--train-days", type=int, default=120)
    accept_auto.add_argument("--validation-days", type=int, default=40)
    accept_auto.add_argument("--oos-days", type=int, default=40)
    accept_auto.add_argument("--step-days", type=int, default=40)
    accept_auto.add_argument("--stress-multipliers", default="1.0,1.5,2.0")
    accept_auto.add_argument("--output", default="")

    data_check = sub.add_parser("data-check", help="检查 Auto 研究数据覆盖、断档和合约生命周期")
    data_check.add_argument("--config", required=True)
    data_check.add_argument("--data", required=True)
    data_check.add_argument("--max-gap-seconds", type=float, default=300.0)
    data_check.add_argument("--output", default="")

    quality = sub.add_parser("quality-report", help="汇总真实/Shadow 执行质量证据")
    quality.add_argument("--config", required=True)
    quality.add_argument("--output", default="")
    quality.add_argument("--shadow", action="store_true", help="汇总 Shadow 而非真实交易证据")

    live = sub.add_parser("live", help="连接 CTP 柜台并真实交易")
    live.add_argument("--config", required=True)
    live.add_argument("--confirm-live", action="store_true")
    live.add_argument("--startup-timeout", type=float, default=60.0)
    live.add_argument("--snapshot-wait", type=float, default=12.0)
    live.add_argument("--halt-drain", type=float, default=3.0)

    shadow = sub.add_parser("shadow", help="连接真实 CTP 行情但所有订单只做本地模拟")
    shadow.add_argument("--config", required=True)
    shadow.add_argument("--confirm-live", action="store_true")
    shadow.add_argument("--startup-timeout", type=float, default=60.0)
    shadow.add_argument("--snapshot-wait", type=float, default=12.0)
    shadow.add_argument("--duration-seconds", type=float, default=0.0)

    doctor = sub.add_parser("doctor", help="无报单检查 CTP 登录、快照、合约目录和元数据")
    doctor.add_argument("--config", required=True)
    doctor.add_argument("--confirm-live", action="store_true")
    doctor.add_argument("--startup-timeout", type=float, default=60.0)
    doctor.add_argument("--snapshot-wait", type=float, default=12.0)
    doctor.add_argument("--metadata-limit", type=int, default=4)

    status = sub.add_parser("status", help="只读检查本地运行状态、证据文件和磁盘空间")
    status.add_argument("--config", required=True)

    stress90_bootstrap = sub.add_parser(
        "stress90-bootstrap",
        help="从五个固定输入重放并创建不可变 Stress-90 seed/state",
    )
    stress90_bootstrap.add_argument("--config", required=True)
    stress90_bootstrap.add_argument("--runtime-dir", required=True)
    stress90_bootstrap.add_argument("--through", required=True, help="最终 target day（YYYYMMDD）")

    stress90_activate = sub.add_parser(
        "stress90-activate",
        help="在停机、空仓、无活动委托并完成对账后显式绑定 Stress-90 identity",
    )
    stress90_activate.add_argument("--config", required=True)
    stress90_activate.add_argument("--confirm-live", action="store_true")
    stress90_activate.add_argument("--confirm-activation", action="store_true")
    stress90_activate.add_argument("--operator-reason", required=True)
    stress90_activate.add_argument("--runtime-dir", default="")
    stress90_activate.add_argument("--startup-timeout", type=float, default=60.0)
    stress90_activate.add_argument("--snapshot-wait", type=float, default=12.0)

    stress90_rebase = sub.add_parser(
        "stress90-account-rebase",
        help="在完整生命周期安全门后显式重置 Stress-90 live account soft path",
    )
    stress90_rebase.add_argument("--config", required=True)
    stress90_rebase.add_argument("--confirm-live", action="store_true")
    stress90_rebase.add_argument("--confirm-rebase", action="store_true")
    stress90_rebase.add_argument("--operator-reason", required=True)
    stress90_rebase.add_argument("--runtime-dir", default="")
    stress90_rebase.add_argument("--startup-timeout", type=float, default=60.0)
    stress90_rebase.add_argument("--snapshot-wait", type=float, default=12.0)

    stress90_compare = sub.add_parser(
        "stress90-oi-compare",
        help="离线比较 CTP raw OI evidence 与批准的 vendor 60m evidence",
    )
    stress90_compare.add_argument("--config", required=True)
    stress90_compare.add_argument("--trading-day", required=True)
    stress90_compare.add_argument("--vendor", required=True)
    stress90_compare.add_argument("--runtime-dir", default="")
    stress90_compare.add_argument("--output", default="")
    stress90_compare.add_argument("--absolute-tolerance", type=float, default=1e-9)

    ohlc_refresh = sub.add_parser(
        "directional-ohlc-refresh",
        help="在无订单权限的进程中更新已完成日 Directional OHLC cache",
    )
    ohlc_refresh.add_argument("--config", required=True)
    ohlc_refresh.add_argument(
        "--current-trading-day",
        required=True,
        help="从运行中 CTP 会话取得的当前 trading day（YYYYMMDD）",
    )
    ohlc_refresh.add_argument("--cache", default="")

    recover = sub.add_parser("recover-state", help="人工核验后重建本地期望持仓")
    recover.add_argument("--config", required=True)
    recover.add_argument("--confirm-live", action="store_true")
    recover.add_argument("--confirm-adopt-state", action="store_true")
    recover.add_argument("--startup-timeout", type=float, default=60.0)
    recover.add_argument("--snapshot-wait", type=float, default=12.0)
    return parser


def wait_for_fresh_snapshot(
    broker,
    timeout_seconds: float,
    *,
    poll_interval: float = 0.1,
) -> None:
    """等待 marker 之后的新账户事件和完整持仓快照同时到达。"""
    marker = broker.snapshot_marker()
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    while not broker.snapshot_ready(marker) and time.monotonic() < deadline:
        time.sleep(max(poll_interval, 0.0001))
    if not broker.snapshot_ready(marker):
        raise RuntimeError("fresh CTP account/position snapshot did not arrive before timeout")


def _collect_doctor_quotes(
    broker,
    contracts: dict[str, ContractInfo],
    *,
    trading_day: str,
    timeout_seconds: float,
    poll_interval: float = 0.05,
) -> dict[str, Tick]:
    """Collect a bounded current-session quote snapshot without any order capability."""

    pending = set(contracts)
    quotes: dict[str, Tick] = {}
    for symbol in sorted(contracts):
        contract = contracts[symbol]
        if contract.symbol != symbol or not contract.exchange:
            raise RuntimeError(f"doctor contract identity is invalid: {symbol}")
        broker.subscribe(symbol, contract.exchange)
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while pending and time.monotonic() <= deadline:
        events = broker.poll_events()
        for event in events:
            if event.event_type in {"trade", "order"}:
                raise RuntimeError(
                    f"doctor observed concurrent {event.event_type} event; account is not stable"
                )
            if event.event_type in {"broker_error", "account_error"}:
                raise RuntimeError(f"doctor broker event failed: {event.payload}")
            if event.event_type != "tick":
                continue
            tick = event.payload
            if not isinstance(tick, Tick) or tick.symbol not in contracts:
                continue
            contract = contracts[tick.symbol]
            if tick.exchange.upper() != contract.exchange.upper():
                raise RuntimeError(f"doctor quote identity mismatch: {tick.symbol}")
            if tick.trading_day != trading_day:
                continue
            try:
                tick.validate()
            except ValueError as exc:
                raise RuntimeError(f"doctor quote is invalid: {tick.symbol}: {exc}") from exc
            quotes[tick.symbol] = tick
            pending.discard(tick.symbol)
        if pending and not events:
            time.sleep(max(0.0001, float(poll_interval)))
    return quotes


def drain_after_halt(
    engine,
    broker,
    timeout_seconds: float,
    *,
    poll_interval: float = 0.1,
) -> bool:
    """停机后继续处理撤单/成交回报，尽量在断开前清空活动委托。"""
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    while True:
        active_orders = broker.get_active_orders()
        if not active_orders:
            return True
        for order in active_orders:
            broker.cancel_order(order.order_id)
        if time.monotonic() >= deadline:
            return False
        engine.run_once()
        time.sleep(max(poll_interval, 0.0001))


def validate_recovery_positions(pairs: list[PairConfig], positions: list[ContractPosition]) -> None:
    """恢复只接受配置内、双腿等量反向且不超过风险上限的套利持仓。"""
    allowed_identities = {
        (symbol, pair.exchange) for pair in pairs for symbol in (pair.near_symbol, pair.far_symbol)
    }
    by_identity: dict[tuple[str, str], ContractPosition] = {}
    duplicates: list[tuple[str, str]] = []
    for position in positions:
        identity = (position.symbol, position.exchange)
        if identity in by_identity:
            duplicates.append(identity)
        else:
            by_identity[identity] = position
    if duplicates:
        detail = ", ".join(f"{symbol}.{exchange}" for symbol, exchange in sorted(duplicates))
        raise RuntimeError(f"broker positions contain duplicate identities: {detail}")

    unknown = sorted(
        f"{position.symbol}.{position.exchange}"
        for position in positions
        if not position.empty and (position.symbol, position.exchange) not in allowed_identities
    )
    if unknown:
        raise RuntimeError(f"broker positions are not configured for afuture: {', '.join(unknown)}")

    for pair in pairs:
        near_identity = (pair.near_symbol, pair.exchange)
        far_identity = (pair.far_symbol, pair.exchange)
        near = by_identity.get(near_identity, ContractPosition(*near_identity))
        far = by_identity.get(far_identity, ContractPosition(*far_identity))
        if near.empty and far.empty:
            continue

        long_volume = near.long_total if near.short_total == 0 else 0
        long_spread = long_volume > 0 and long_volume == far.short_total and far.long_total == 0
        short_volume = near.short_total if near.long_total == 0 else 0
        short_spread = short_volume > 0 and short_volume == far.long_total and far.short_total == 0
        if not (long_spread or short_spread):
            raise RuntimeError(f"pair {pair.pair_id} is not a configured balanced spread")
        volume = long_volume if long_spread else short_volume
        if volume > pair.volume:
            raise RuntimeError(f"pair {pair.pair_id} position exceeds configured risk cap")


def adopt_recovery_state(
    store: StateStore,
    state,
    account: AccountSnapshot,
    positions: list[ContractPosition],
) -> None:
    """人工恢复只重建期望仓位，仍保持停机并要求下一会话重新验元数据和对账。"""
    old_day = str(state.trading_day or "")
    new_day = str(account.trading_day or "")
    if new_day and new_day != old_day:
        state.day_start_equity = account.equity
    elif state.day_start_equity <= 0:
        state.day_start_equity = account.equity
    if new_day:
        state.trading_day = new_day
    state.equity_high_watermark = max(float(state.equity_high_watermark or 0.0), account.equity)
    state.kill_switch = True
    state.kill_reason = (
        "operator adopted verified CTP positions; restart live for independent reconciliation"
    )
    state.runtime_mode = RuntimeMode.HALTED.value
    state.reconciled = False
    state.metadata_verified = False
    store.save_positions(state, positions)


def _require_production_confirmation(config, args) -> None:
    if config.ctp is None or config.ctp.environment != "production":
        return
    if not args.confirm_live or os.getenv("AFUTURE_LIVE_ACK") != _LIVE_ACK:
        raise RuntimeError(
            "production CTP access requires --confirm-live and "
            "AFUTURE_LIVE_ACK=I_UNDERSTAND_FUTURES_RISK"
        )


def _wait_until_ready(broker, timeout_seconds: float) -> None:
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    while not broker.is_ready() and time.monotonic() < deadline:
        time.sleep(0.2)
    if not broker.is_ready():
        raise RuntimeError("CTP did not become ready before timeout")


def _validate_live_metadata(config, broker, extra_pairs=None) -> None:
    """人工状态恢复同样要求柜台元数据存在；静态参数继续执行保守一致性校验。"""
    if not config.require_live_metadata:
        return
    if config.contracts:
        live_specs = broker.get_live_contract_specs(
            list(config.contracts), config.metadata_timeout_seconds
        )
        decision = validate_contract_metadata(config.contracts, live_specs)
        if not decision.allowed:
            raise RuntimeError(decision.reason)

    extra_symbols = {
        symbol
        for pair in (extra_pairs or [])
        for symbol in (pair.near_symbol, pair.far_symbol)
        if symbol not in config.contracts
    }
    if extra_symbols:
        rows = broker.get_live_contract_specs(
            sorted(extra_symbols), config.metadata_timeout_seconds
        )
        missing = extra_symbols - set(rows)
        if missing:
            raise RuntimeError(
                f"live metadata missing for recovered auto contracts: {sorted(missing)}"
            )


def _auto_pairs_from_state(state) -> list[PairConfig]:
    """把持久化动态组合恢复成 PairConfig，供人工恢复安全门复核。"""
    result = []
    for raw in state.auto_pairs.values():
        row = dict(raw)
        if "session_windows" in row:
            row["session_windows"] = tuple(row["session_windows"])
        result.append(PairConfig(**row))
    return result


def _seed_state_aware_ctp_broker(broker, state, *, reject_ambiguous: bool) -> None:
    """Seed only provable composite fill identities before a direct CTP startup."""
    qualified: list[str] = []
    ambiguous: list[str] = []
    for identity in state.recent_trade_ids if state is not None else []:
        parts = identity.split(":", 2)
        if len(parts) != 3:
            ambiguous.append(identity)
            continue
        trading_day, exchange, trade_id = parts
        try:
            parsed_day = datetime.strptime(trading_day, "%Y%m%d")
        except ValueError:
            ambiguous.append(identity)
            continue
        if (
            parsed_day.strftime("%Y%m%d") != trading_day
            or not re.fullmatch(r"[A-Z][A-Z0-9]*", exchange)
            or not trade_id
        ):
            ambiguous.append(identity)
            continue
        qualified.append(identity)

    if reject_ambiguous and ambiguous:
        raise RuntimeError(
            "state recovery contains ambiguous legacy trade identities; "
            "broker reconciliation is required before adoption"
        )
    broker.seed_trade_identities(qualified)


def _recover_state(config, args, logger) -> int:
    """强确认后把人工核验的柜台持仓重新锚定为期望状态，但不解除停机。"""
    from .broker.ctp import CtpBroker
    from .journal import AuditJournal
    from .report import write_account_report

    if config.mode != "live" or config.ctp is None:
        raise ValueError("recover-state requires system.mode=live")
    _require_production_confirmation(config, args)
    if not args.confirm_adopt_state or os.getenv("AFUTURE_RECOVERY_ACK") != _RECOVERY_ACK:
        raise RuntimeError(
            "state recovery requires --confirm-adopt-state and "
            "AFUTURE_RECOVERY_ACK=I_VERIFIED_CTP_POSITIONS"
        )

    store = StateStore(config.state_path)
    state = store.load()
    if not state.kill_switch:
        raise RuntimeError("state recovery is allowed only while the kill switch is active")

    broker = CtpBroker(config.ctp)
    _seed_state_aware_ctp_broker(broker, state, reject_ambiguous=True)
    broker.start()
    try:
        _wait_until_ready(broker, args.startup_timeout)
        wait_for_fresh_snapshot(broker, args.snapshot_wait)
        recovery_pairs = config.pairs + _auto_pairs_from_state(state)
        _validate_live_metadata(config, broker, recovery_pairs)
        active_orders = broker.get_active_orders()
        if active_orders:
            for order in active_orders:
                broker.cancel_order(order.order_id)
            state.kill_switch = True
            state.reconciled = False
            state.metadata_verified = False
            state.kill_reason = (
                "active orders found during state recovery; cancelled; "
                "rerun recovery after verification"
            )
            store.save(state)
            raise RuntimeError(
                "active orders existed during recovery and were cancelled; state was not adopted"
            )

        account = broker.get_account()
        positions = broker.get_positions()
        validate_recovery_positions(recovery_pairs, positions)
        adopt_recovery_state(store, state, account, positions)
        AuditJournal(config.journal_path).record(
            "manual_state_adoption",
            {
                "trading_day": account.trading_day,
                "positions": positions,
                "kill_switch_remains_active": True,
            },
        )
        write_account_report(config.report_path, account, positions)
        logger.warning(
            "已重建本地期望持仓，但停机开关仍保持；必须重新执行 live 完成元数据校验和第二次独立对账"
        )
        return 0
    finally:
        broker.stop()


def _build_alert_manager(config) -> AlertManager:
    sinks: list[AlertSink] = [FileAlertSink(config.alert_path)]
    if config.alert_webhook:
        sinks.append(WebhookAlertSink(config.alert_webhook))
    return AlertManager(sinks)


def _runtime_path(config, name: str) -> Path:
    return Path(config.state_path).parent / name


def _quality_recorder(config, *, shadow: bool = False) -> ExecutionQualityRecorder:
    name = "shadow_execution_quality.jsonl" if shadow else "execution_quality.jsonl"
    return ExecutionQualityRecorder(_runtime_path(config, name))


def _shadow_runtime_paths(config) -> dict[str, object]:
    """Keep Stress-90 candidate/account/intent state isolated from the live account."""

    runtime_dir = Path(config.state_path).parent
    if config.directional.policy == "stress90":
        shadow_dir = runtime_dir / "shadow"
        return {
            "state": shadow_dir / "state.json",
            "journal": shadow_dir / "audit.jsonl",
            "persistent": True,
        }
    return {
        "state": runtime_dir / "shadow_state.json",
        "journal": runtime_dir / "shadow_audit.jsonl",
        "persistent": False,
    }


def _auto_manager(config, *, evidence=None, shadow: bool = False):
    from .auto import AutoPairManager

    if not config.auto.enabled:
        return None
    max_samples = max(config.auto.lookback * 4, config.auto.lookback + 8)
    sample_dir = "shadow_market_samples" if shadow else "market_samples"
    return AutoPairManager(
        config.auto,
        sample_store=MarketSampleStore(_runtime_path(config, sample_dir), max_samples=max_samples),
        evidence_recorder=evidence,
    )


def _build_cli_engine(
    config,
    broker,
    state_store: StateStore,
    *,
    journal=None,
    alert_manager=None,
    auto_manager=None,
    quality_recorder=None,
):
    """Create the account-exclusive runtime selected by validated configuration."""
    from .runtime_factory import build_runtime_engine

    return build_runtime_engine(
        config,
        broker,
        state_store,
        journal=journal,
        alert_manager=alert_manager,
        auto_manager=auto_manager,
        quality_recorder=quality_recorder,
    )


def _run_live(config, args, logger) -> int:
    """完成柜台、快照、元数据、活动订单和持仓安全门后才进入实时循环。"""
    from .broker.ctp import CtpBroker
    from .journal import AuditJournal
    from .report import write_account_report

    broker = CtpBroker(config.ctp)
    quality = _quality_recorder(config)
    engine = _build_cli_engine(
        config,
        broker,
        StateStore(config.state_path),
        journal=AuditJournal(config.journal_path),
        alert_manager=_build_alert_manager(config),
        auto_manager=_auto_manager(config, evidence=quality),
        quality_recorder=quality,
    )
    engine.start()
    try:
        try:
            _wait_until_ready(broker, args.startup_timeout)
            wait_for_fresh_snapshot(broker, args.snapshot_wait)
        except RuntimeError as exc:
            engine.emergency_stop(str(exc))
            raise

        engine.initialize_after_ready()
        if not engine.state.metadata_verified:
            raise RuntimeError(
                f"live contract metadata verification failed: {engine.state.kill_reason}"
            )

        active_orders = broker.get_active_orders()
        if active_orders:
            for order in active_orders:
                broker.cancel_order(order.order_id)
            engine.emergency_stop("active orders found during startup reconciliation")
            raise RuntimeError("active orders existed at startup and were cancelled")

        if engine.halted:
            if not engine.clear_kill_switch_after_reconcile():
                raise RuntimeError("kill switch remains active because reconciliation did not pass")
        elif not engine.reconcile_startup():
            raise RuntimeError("startup reconciliation failed")

        if config.directional.enabled:
            logger.info(
                "CTP 已就绪，方向组合已启用：品种=%d，gross 上限=%.2fx",
                len(config.directional.products),
                config.directional.max_gross_leverage,
            )
        elif config.auto.enabled:
            logger.info(
                "CTP 已就绪，自动发现已启用：品种=%s，最多激活组合=%d",
                ",".join(config.auto.products),
                config.auto.max_active_pairs,
            )
        logger.info("CTP 已就绪，元数据与持仓对账通过，交易循环启动")
        while True:
            engine.run_once()
            if engine.halted:
                raise RuntimeError(f"trading halted: {engine.state.kill_reason}")
            time.sleep(0.1)
    except KeyboardInterrupt:
        logger.info("收到人工停止请求")
    finally:
        if engine.halted and broker.is_ready():
            try:
                if not drain_after_halt(engine, broker, args.halt_drain):
                    logger.error("停机后仍有活动委托；已保留停机开关，下一次启动会再次检查")
            except Exception as exc:
                logger.error("停机撤单收尾失败：%s", exc)
        try:
            write_account_report(config.report_path, broker.get_account(), broker.get_positions())
        except Exception as exc:
            logger.error("关闭前账户报告写入失败：%s", exc)
        engine.stop()
    return 0


def _run_shadow(config, args, logger) -> int:
    """使用真实 CTP 行情/元数据，但所有订单只进入本地保守 SimBroker。"""
    from .broker.ctp import CtpBroker
    from .broker.shadow import ShadowBroker
    from .journal import AuditJournal

    if config.mode != "live" or config.ctp is None:
        raise ValueError("shadow requires system.mode=live")
    _require_production_confirmation(config, args)
    live = CtpBroker(config.ctp)
    broker = ShadowBroker(
        live,
        config.initial_capital,
        slippage_ticks=config.slippage_ticks,
        latency_ticks=max(1, config.latency_ticks),
        market_impact_ticks=max(1, config.market_impact_ticks),
    )
    broker.update_specs(config.contracts)
    quality = _quality_recorder(config, shadow=True)
    shadow_paths = _shadow_runtime_paths(config)
    shadow_state = shadow_paths["state"]
    if not isinstance(shadow_state, Path):  # pragma: no cover - internal contract
        raise RuntimeError("shadow state path is invalid")
    # Legacy Shadow sessions retain their existing empty-account behavior. Stress-90
    # state is account-path evidence and must survive restart exactly like live state.
    if shadow_paths["persistent"] is not True and shadow_state.exists():
        shadow_state.unlink()
    shadow_journal = shadow_paths["journal"]
    if not isinstance(shadow_journal, Path):  # pragma: no cover - internal contract
        raise RuntimeError("shadow journal path is invalid")
    engine = _build_cli_engine(
        config,
        broker,
        StateStore(shadow_state),
        journal=AuditJournal(shadow_journal),
        alert_manager=_build_alert_manager(config),
        auto_manager=_auto_manager(config, evidence=quality, shadow=True),
        quality_recorder=quality,
    )
    engine.start()
    deadline = time.monotonic() + args.duration_seconds if args.duration_seconds > 0 else None
    try:
        _wait_until_ready(broker, args.startup_timeout)
        wait_for_fresh_snapshot(broker, args.snapshot_wait)
        engine.initialize_after_ready()
        if engine.halted:
            if not engine.clear_kill_switch_after_reconcile():
                raise RuntimeError("shadow startup reconciliation did not pass")
        elif not engine.reconcile_startup():
            raise RuntimeError("shadow startup reconciliation failed")
        logger.info("Shadow 已启动：真实 CTP 行情/元数据，本地模拟订单，绝不调用真实 send_order")
        while deadline is None or time.monotonic() < deadline:
            engine.run_once()
            if engine.halted:
                raise RuntimeError(f"shadow halted: {engine.state.kill_reason}")
            time.sleep(0.1)
    except KeyboardInterrupt:
        logger.info("收到 Shadow 人工停止请求")
    finally:
        engine.stop()
    return 0


def _run_doctor(config, args) -> int:
    """无订单检查 CTP、fresh snapshot、持仓、状态和本地运行条件。"""
    from .auto import AutoPairSelector
    from .broker.ctp import CtpBroker
    from .operations import build_doctor_report

    if config.mode != "live" or config.ctp is None:
        raise ValueError("doctor requires system.mode=live")
    _require_production_confirmation(config, args)
    try:
        state = StateStore(config.state_path).load()
    except (OSError, ValueError):
        state = None
    broker = CtpBroker(config.ctp)
    _seed_state_aware_ctp_broker(broker, state, reject_ambiguous=False)
    broker.start()
    try:
        _wait_until_ready(broker, args.startup_timeout)
        wait_for_fresh_snapshot(broker, args.snapshot_wait)
        account = broker.get_account()
        catalog = broker.get_contract_catalog()
        trading_day = broker.get_trading_day()
        positions = broker.get_positions()
        active_orders = broker.get_active_orders()
        quotes: dict[str, Tick] = {}
        symbols: list[str]
        if config.directional.enabled and config.directional.policy == "stress90":
            from .directional_activity import (
                DirectionalActivityStore,
                select_contracts_from_activity,
                validate_directional_activity_snapshot,
            )

            selected = {}
            try:
                snapshot = DirectionalActivityStore(
                    Path(config.state_path).with_name("directional_activity.json")
                ).load()
                if snapshot is not None:
                    validate_directional_activity_snapshot(snapshot)
                    catalog_by_symbol = {item.symbol: item for item in catalog}
                    preferred = {
                        catalog_by_symbol[position.symbol].product.upper(): position.symbol
                        for position in positions
                        if not position.empty and position.symbol in catalog_by_symbol
                    }
                    selected = select_contracts_from_activity(
                        config.directional,
                        catalog,
                        snapshot,
                        datetime.strptime(trading_day, "%Y%m%d").date(),
                        preferred_symbols=preferred,
                    )
            except (KeyError, TypeError, ValueError):
                # The report independently validates and surfaces the exact evidence error.
                selected = {}
            catalog_by_symbol = {item.symbol: item for item in catalog}
            selected_contracts = {item.symbol: item for item in selected.values()}
            for position in positions:
                if not position.empty and position.symbol in catalog_by_symbol:
                    selected_contracts[position.symbol] = catalog_by_symbol[position.symbol]
            symbols = sorted(selected_contracts)
            if selected_contracts:
                quotes = _collect_doctor_quotes(
                    broker,
                    selected_contracts,
                    trading_day=trading_day,
                    timeout_seconds=args.snapshot_wait,
                )
        else:
            symbols = list(config.contracts)
            if config.auto.enabled and catalog:
                today = datetime.strptime(trading_day, "%Y%m%d").date()
                auto_pairs = AutoPairSelector(config.auto).build_pairs(catalog, today)
                for pair in auto_pairs:
                    symbols.extend([pair.near_symbol, pair.far_symbol])
                    if len(set(symbols)) >= args.metadata_limit:
                        break
            if config.directional.enabled and catalog:
                products = {item.upper() for item in config.directional.products}
                exchanges = {item.upper() for item in config.directional.exchanges}
                for contract in catalog:
                    if (
                        contract.product.upper() in products
                        and contract.exchange.upper() in exchanges
                    ):
                        symbols.append(contract.symbol)
                    if len(set(symbols)) >= args.metadata_limit:
                        break
            symbols = sorted(set(symbols))[: max(0, args.metadata_limit)]
        metadata = (
            broker.get_live_contract_specs(symbols, config.metadata_timeout_seconds)
            if symbols
            else {}
        )
        report = build_doctor_report(
            config,
            broker_ready=broker.is_ready(),
            fresh_snapshot=True,
            trading_day=trading_day,
            account=account,
            positions=positions,
            active_order_count=len(active_orders),
            catalog=catalog,
            requested_symbols=symbols,
            metadata=metadata,
            quotes=quotes,
        )
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0 if report.passed else 2
    finally:
        broker.stop()


def _run_status(config) -> int:
    """Print local operational facts without initializing logging or a broker."""
    from .operations import build_local_status

    report = build_local_status(config)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.passed else 2


def _run_stress90_bootstrap(config, args) -> int:
    """Run the fixed replay without initializing a broker or requiring CTP secrets."""
    if config.directional.policy != "stress90":
        raise ValueError("stress90-bootstrap requires directional.policy=stress90")
    from .directional_stress90_bootstrap import bootstrap_stress90

    result = bootstrap_stress90(
        runtime_dir=args.runtime_dir,
        through_day=args.through,
        write_artifacts=True,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


def _stress90_lifecycle_paths(config, runtime_dir: str) -> dict[str, Path]:
    if runtime_dir:
        root = Path(runtime_dir)
        return {
            "runtime": root,
            "state": root / "state.json",
            "journal": root / "audit.jsonl",
        }
    state = Path(config.state_path)
    return {
        "runtime": state.parent,
        "state": state,
        "journal": Path(config.journal_path),
    }


def _validate_stress90_lifecycle_config(config) -> None:
    from .execution_aligned_policy import FROZEN_PRODUCTS

    if config.mode != "live" or config.ctp is None:
        raise ValueError("Stress-90 lifecycle commands require system.mode=live")
    if (
        not config.directional.enabled
        or config.directional.policy != "stress90"
        or not config.directional.account_exclusive
    ):
        raise ValueError(
            "Stress-90 lifecycle commands require enabled, explicit stress90, "
            "account-exclusive configuration"
        )
    products = tuple(sorted({str(item).upper() for item in config.directional.products}))
    if products != FROZEN_PRODUCTS:
        raise ValueError("Stress-90 lifecycle commands require the frozen 50-product universe")


def _run_stress90_activate(config, args) -> int:
    """Explicitly bind generic runtime state to one immutable bootstrap identity."""

    from .broker.ctp import CtpBroker
    from .directional_policy_activation import (
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
    )
    from .directional_stress90_state import Stress90PolicyStateStore, Stress90SeedStore
    from .journal import AuditJournal
    from .reconcile import compare_positions

    _validate_stress90_lifecycle_config(config)
    _require_production_confirmation(config, args)
    if (
        not args.confirm_activation
        or os.getenv("AFUTURE_STRESS90_ACTIVATION_ACK") != STRESS90_ACTIVATION_CONFIRMATION
    ):
        raise RuntimeError(
            "Stress-90 activation requires --confirm-activation and "
            "AFUTURE_STRESS90_ACTIVATION_ACK=I_CONFIRM_STRESS90_POLICY_ACTIVATION"
        )
    paths = _stress90_lifecycle_paths(config, args.runtime_dir)
    seed = Stress90SeedStore(paths["runtime"] / "stress90_bootstrap_seed.json").load_required()
    policy_state = Stress90PolicyStateStore(
        paths["runtime"] / "stress90_policy_state.json"
    ).load_required()
    if policy_state.bootstrap_seed_digest != seed.seed_digest:
        raise RuntimeError("Stress-90 activation seed/policy-state identity mismatch")

    store = StateStore(paths["state"])
    existed = store.path.exists()
    state = store.load()
    broker = CtpBroker(config.ctp)
    _seed_state_aware_ctp_broker(broker, state if existed else None, reject_ambiguous=True)
    broker.start()
    try:
        _wait_until_ready(broker, args.startup_timeout)
        wait_for_fresh_snapshot(broker, args.snapshot_wait)
        account = broker.get_account()
        account.validate()
        trading_day = broker.get_trading_day()
        if account.trading_day != trading_day:
            raise RuntimeError("Stress-90 activation account/CTP trading day mismatch")
        if trading_day < policy_state.last_completed_target_day:
            raise RuntimeError("Stress-90 activation CTP trading day precedes bootstrap state")
        positions = broker.get_positions()
        active_orders = broker.get_active_orders()
        local_positions = store.positions_from_state(state) if existed else []
        reconciliation = compare_positions(local_positions, positions)
        if not reconciliation.matched:
            raise RuntimeError(
                "Stress-90 activation Broker/local reconciliation failed: " + reconciliation.details
            )
        if existed:
            if state.runtime_mode != RuntimeMode.HALTED.value or not state.kill_switch:
                raise RuntimeError("Stress-90 activation requires an existing HALTED state")
        else:
            state = RuntimeState(
                kill_switch=True,
                kill_reason="fresh Stress-90 commissioning requires activation",
                runtime_mode=RuntimeMode.HALTED.value,
            )
        state = replace(
            state,
            kill_switch=True,
            kill_reason="Stress-90 activated; doctor/Shadow gates remain required",
            runtime_mode=RuntimeMode.HALTED.value,
            reconciled=True,
            metadata_verified=False,
            trading_day=trading_day,
            day_start_equity=float(account.equity),
            equity_high_watermark=max(
                float(state.equity_high_watermark or 0.0), float(account.equity)
            ),
            last_account_equity=float(account.equity),
            last_account_trading_day=trading_day,
        )
        activated = activate_stress90_policy(
            state,
            broker_flat=not any(not position.empty for position in positions),
            local_flat=not any(not position.empty for position in local_positions),
            no_active_orders=not active_orders,
            reconciled=reconciliation.matched,
            bootstrap_seed_digest=seed.seed_digest,
            operator_reason=args.operator_reason,
            strong_confirmation=os.environ["AFUTURE_STRESS90_ACTIVATION_ACK"],
        )
        journal = AuditJournal(paths["journal"])
        journal.record(
            "stress90_policy_activation_prepared",
            {
                "trading_day": trading_day,
                "bootstrap_seed_digest": seed.seed_digest,
                "operator_reason": args.operator_reason,
                "runtime_state_preexisting": existed,
            },
        )
        store.save(activated)
        journal.record(
            "stress90_policy_activation_completed",
            {
                "trading_day": trading_day,
                "bootstrap_seed_digest": seed.seed_digest,
                "kill_switch_remains_active": True,
            },
        )
        print(
            json.dumps(
                {
                    "activated": True,
                    "runtime_dir": str(paths["runtime"]),
                    "trading_day": trading_day,
                    "bootstrap_seed_digest": seed.seed_digest,
                    "runtime_mode": RuntimeMode.HALTED.value,
                    "kill_switch": True,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        broker.stop()


def _run_stress90_account_rebase(config, args) -> int:
    """Reset account-path statistics only after explicit flat-account reconciliation."""

    from .broker.ctp import CtpBroker
    from .directional_policy_activation import require_directional_policy_identity
    from .directional_stress90_policy import STRESS90_POLICY
    from .directional_stress90_state import (
        REBASE_CONFIRMATION,
        Stress90PolicyStateStore,
        Stress90SeedStore,
        rebase_stress90_account,
    )
    from .journal import AuditJournal
    from .reconcile import compare_positions

    _validate_stress90_lifecycle_config(config)
    _require_production_confirmation(config, args)
    if not args.confirm_rebase or os.getenv("AFUTURE_STRESS90_REBASE_ACK") != REBASE_CONFIRMATION:
        raise RuntimeError(
            "Stress-90 account rebase requires --confirm-rebase and "
            "AFUTURE_STRESS90_REBASE_ACK=RESET_STRESS90_ACCOUNT_PATH"
        )
    paths = _stress90_lifecycle_paths(config, args.runtime_dir)
    store = StateStore(paths["state"])
    if not store.path.exists():
        raise RuntimeError("Stress-90 account rebase requires existing runtime state")
    state = store.load()
    if state.runtime_mode != RuntimeMode.HALTED.value or not state.kill_switch:
        raise RuntimeError("Stress-90 account rebase requires HALTED state and kill switch")
    seed = Stress90SeedStore(paths["runtime"] / "stress90_bootstrap_seed.json").load_required()
    require_directional_policy_identity(
        state,
        policy_id=STRESS90_POLICY.policy_id,
        policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
        products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
        bootstrap_seed_digest=seed.seed_digest,
    )
    policy_store = Stress90PolicyStateStore(paths["runtime"] / "stress90_policy_state.json")
    policy_record = policy_store.load_required_record()
    if policy_record.state.bootstrap_seed_digest != seed.seed_digest:
        raise RuntimeError("Stress-90 account rebase seed/policy-state identity mismatch")

    broker = CtpBroker(config.ctp)
    _seed_state_aware_ctp_broker(broker, state, reject_ambiguous=True)
    broker.start()
    try:
        _wait_until_ready(broker, args.startup_timeout)
        wait_for_fresh_snapshot(broker, args.snapshot_wait)
        account = broker.get_account()
        account.validate()
        trading_day = broker.get_trading_day()
        if account.trading_day != trading_day:
            raise RuntimeError("Stress-90 account rebase account/CTP trading day mismatch")
        positions = broker.get_positions()
        local_positions = store.positions_from_state(state)
        active_orders = broker.get_active_orders()
        reconciliation = compare_positions(local_positions, positions)
        if not reconciliation.matched:
            raise RuntimeError(
                "Stress-90 account rebase Broker/local reconciliation failed: "
                + reconciliation.details
            )
        rebased, audit = rebase_stress90_account(
            policy_record.state,
            account_trading_day=trading_day,
            account_equity=account.equity,
            operator_reason=args.operator_reason,
            halted=True,
            broker_flat=not any(not position.empty for position in positions),
            local_flat=not any(not position.empty for position in local_positions),
            no_active_orders=not active_orders,
            reconciled=bool(state.reconciled and reconciliation.matched),
            strong_confirmation=os.environ["AFUTURE_STRESS90_REBASE_ACK"],
        )
        journal = AuditJournal(paths["journal"])
        journal.record(
            "stress90_account_rebase_prepared",
            {
                **asdict(audit),
                "bootstrap_seed_digest": seed.seed_digest,
                "kill_switch_active": True,
            },
        )
        policy_store.save(rebased, expected_sequence=policy_record.sequence)
        generic_rebased = replace(
            state,
            trading_day=trading_day,
            day_start_equity=float(account.equity),
            equity_high_watermark=float(account.equity),
            last_account_equity=float(account.equity),
            last_account_trading_day=trading_day,
            reconciled=True,
            metadata_verified=False,
            kill_switch=True,
            kill_reason="Stress-90 account path rebased; doctor/Shadow gates remain required",
            runtime_mode=RuntimeMode.HALTED.value,
        )
        store.save(generic_rebased)
        journal.record(
            "stress90_account_rebase_completed",
            {
                **asdict(audit),
                "bootstrap_seed_digest": seed.seed_digest,
                "kill_switch_remains_active": True,
            },
        )
        print(
            json.dumps(
                {
                    "rebased": True,
                    "runtime_dir": str(paths["runtime"]),
                    "trading_day": trading_day,
                    "account_equity": account.equity,
                    "operator_reason": args.operator_reason,
                    "runtime_mode": RuntimeMode.HALTED.value,
                    "kill_switch": True,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        broker.stop()


def _run_stress90_oi_compare(config, args) -> int:
    """Compare two completed evidence sources in a process with no Broker/order object."""

    if not config.directional.enabled or config.directional.policy != "stress90":
        raise ValueError("stress90-oi-compare requires directional.policy=stress90")
    from .directional_stress90_oi_comparator import (
        compare_completed_oi_evidence,
        load_vendor_oi_reference,
    )
    from .directional_stress90_oi_runtime import Stress90OiEvidenceStore

    runtime_dir = Path(args.runtime_dir) if args.runtime_dir else Path(config.state_path).parent
    record = Stress90OiEvidenceStore(
        runtime_dir / "stress90_oi_evidence.json"
    ).load_required_record()
    live = next(
        (
            evidence
            for evidence in record.state.completed
            if evidence.trading_day == args.trading_day
        ),
        None,
    )
    if live is None:
        raise RuntimeError(
            f"completed CTP Stress-90 OI evidence is unavailable: {args.trading_day}"
        )
    vendor, vendor_sha256 = load_vendor_oi_reference(args.vendor)
    comparison = compare_completed_oi_evidence(
        live,
        vendor,
        absolute_tolerance=args.absolute_tolerance,
    )
    payload = {
        **comparison.to_dict(),
        "vendor_input_path": str(Path(args.vendor)),
        "vendor_input_sha256": vendor_sha256,
        "orders_sent": 0,
        "activation_blocked": bool(comparison.unexplained_flow_differences),
    }
    output = args.output or runtime_dir / (f"stress90_oi_comparison_{args.trading_day}.json")
    _write_json(payload, output)
    return 0 if comparison.matched else 2


def _run_directional_ohlc_refresh(config, args) -> int:
    """Refresh market evidence in an explicitly order-incapable CLI process."""

    if config.directional.policy != "stress90":
        raise ValueError("directional-ohlc-refresh requires directional.policy=stress90")
    from .directional_ohlc_cache import DirectionalOHLCCacheStore
    from .directional_ohlc_refresh import refresh_directional_ohlc_cache
    from .execution_aligned_runtime import SinaContinuousOHLCProvider

    cache_path = (
        Path(args.cache)
        if args.cache
        else Path(config.state_path).with_name("directional_ohlc_cache.json")
    )
    entry = refresh_directional_ohlc_cache(
        DirectionalOHLCCacheStore(cache_path),
        provider=SinaContinuousOHLCProvider(),
        products=tuple(config.directional.products),
        current_ctp_trading_day=args.current_trading_day,
    )
    print(
        json.dumps(
            {
                "cache": str(cache_path),
                "latest_completed_day": entry.latest_date.strftime("%Y%m%d"),
                "row_count": entry.row_count,
                "content_digest": entry.content_digest,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _research_pairs(config, ticks) -> list[PairConfig]:
    """研究命令在 auto 模式下使用与实盘相同的相邻月份生成规则。"""
    if not config.auto.enabled:
        return list(config.pairs)
    from .auto import AutoPairSelector

    trading_days = [str(tick.trading_day) for tick in ticks if tick.trading_day]
    if not trading_days:
        raise ValueError("auto research requires trading_day in tick data")
    today = datetime.strptime(max(trading_days), "%Y%m%d").date()
    return AutoPairSelector(config.auto).build_pairs(config.contract_catalog, today)


def _parse_stress_multipliers(raw: str) -> tuple[float, ...]:
    values = tuple(float(item.strip()) for item in raw.split(",") if item.strip())
    if not values or any(value <= 0 for value in values):
        raise ValueError("stress multipliers must be positive")
    return values


def _write_json(payload: dict, output: str | Path | None = None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(
        args.config,
        require_ctp_credentials=args.command
        not in {
            "status",
            "stress90-bootstrap",
            "stress90-oi-compare",
            "directional-ohlc-refresh",
        },
    )
    if args.command == "status":
        return _run_status(config)
    if args.command == "stress90-bootstrap":
        return _run_stress90_bootstrap(config, args)
    if args.command == "directional-ohlc-refresh":
        return _run_directional_ohlc_refresh(config, args)
    if args.command == "stress90-oi-compare":
        return _run_stress90_oi_compare(config, args)
    logger = configure_logging(config.log_path)

    if args.command == "validate":
        logger.info("配置校验通过")
        return 0

    if args.command == "replay":
        from .replay import run_replay

        account = run_replay(config, args.data)
        logger.info("回放完成：权益 %.2f，保证金 %.2f", account.equity, account.margin)
        return 0

    if args.command == "scan":
        ticks = read_ticks(args.data)
        scanner = SpreadScanner(
            slippage_ticks=config.auto.slippage_ticks
            if config.auto.enabled
            else config.slippage_ticks,
            max_sync_seconds=config.auto.max_sync_seconds if config.auto.enabled else 2.0,
        )
        rows = []
        for research_pair in _research_pairs(config, ticks):
            candidate = scanner.scan_pair(research_pair, ticks, config.contracts)
            if candidate is not None:
                rows.append(asdict(candidate))
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    if args.command == "accept":
        ticks = read_ticks(args.data)
        accepted_pair = next(
            (item for item in _research_pairs(config, ticks) if item.pair_id == args.pair),
            None,
        )
        if accepted_pair is None:
            raise ValueError(f"unknown pair: {args.pair}")
        research_config = ResearchConfig(
            train_days=args.train_days,
            validation_days=args.validation_days,
            oos_days=args.oos_days,
            step_days=args.step_days,
            cost_stress_multipliers=_parse_stress_multipliers(args.stress_multipliers),
        )
        walk_forward_result = WalkForwardRunner(config.contracts, config.initial_capital).run(
            accepted_pair, ticks, research_config
        )
        acceptance_decision = AcceptanceGate().evaluate(walk_forward_result)
        print(
            json.dumps(
                {
                    "accepted": acceptance_decision.accepted,
                    "reasons": acceptance_decision.reasons,
                    "selected_parameters": walk_forward_result.selected_parameters,
                    "folds": [asdict(fold) for fold in walk_forward_result.folds],
                    "stress_results": walk_forward_result.stress_results,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if acceptance_decision.accepted else 2

    if args.command == "accept-auto":
        from .auto_acceptance import AutoPortfolioAcceptanceGate
        from .auto_research import AutoPortfolioResearchConfig, AutoPortfolioRunner

        ticks = read_ticks(args.data)
        research = AutoPortfolioResearchConfig(
            train_days=args.train_days,
            validation_days=args.validation_days,
            oos_days=args.oos_days,
            step_days=args.step_days,
            cost_stress_multipliers=_parse_stress_multipliers(args.stress_multipliers),
        )
        auto_result = AutoPortfolioRunner(config).run(ticks, research)
        auto_decision = AutoPortfolioAcceptanceGate().evaluate(auto_result)
        payload = {
            "accepted": auto_decision.accepted,
            "reasons": auto_decision.reasons,
            "gate_metrics": auto_decision.metrics,
            "selected_parameters": auto_result.selected_parameters,
            "folds": [asdict(fold) for fold in auto_result.folds],
            "stress_results": auto_result.stress_results,
            "robustness": auto_result.robustness,
        }
        output = args.output or _runtime_path(config, "auto_acceptance.json")
        _write_json(payload, output)
        return 0 if auto_decision.accepted else 2

    if args.command == "data-check":
        from .data_quality import DataQualityAnalyzer

        # 保留源文件顺序，才能发现数据供应链中的真实乱序；研究/回放仍按时间排序。
        ticks = read_ticks(args.data, sort_rows=False)
        quality_result = DataQualityAnalyzer(args.max_gap_seconds).analyze(
            ticks, config.contract_catalog, config.auto
        )
        output = args.output or _runtime_path(config, "data_quality.json")
        _write_json(quality_result.to_dict(), output)
        return 0 if quality_result.passed else 2

    if args.command == "quality-report":
        recorder = _quality_recorder(config, shadow=args.shadow)
        payload = recorder.summary()
        default_name = (
            "shadow_execution_quality_report.json"
            if args.shadow
            else "execution_quality_report.json"
        )
        output = args.output or _runtime_path(config, default_name)
        _write_json(payload, output)
        return 0

    if args.command == "doctor":
        return _run_doctor(config, args)

    if args.command == "stress90-activate":
        return _run_stress90_activate(config, args)

    if args.command == "stress90-account-rebase":
        return _run_stress90_account_rebase(config, args)

    if args.command == "shadow":
        return _run_shadow(config, args, logger)

    if args.command == "recover-state":
        return _recover_state(config, args, logger)

    if config.mode != "live" or config.ctp is None:
        raise ValueError("live command requires system.mode=live")
    _require_production_confirmation(config, args)
    return _run_live(config, args, logger)
