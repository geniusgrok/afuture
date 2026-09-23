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
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .alerts import AlertManager, AlertSink, FileAlertSink, WebhookAlertSink
from .config import load_config
from .data import read_ticks
from .logging_utils import configure_logging
from .metadata import validate_contract_metadata
from .models import (
    AccountSnapshot,
    ContractInfo,
    ContractPosition,
    Order,
    PairConfig,
    RuntimeMode,
    Tick,
    Trade,
)
from .quality import ExecutionQualityRecorder
from .research import AcceptanceGate, ResearchConfig, WalkForwardRunner
from .sample_store import MarketSampleStore
from .scanner import SpreadScanner
from .state import RuntimeState, StateStore

if TYPE_CHECKING:
    from .runtime_lease import AccountExclusiveRuntimeLease
    from .stress90_session_authority import Stress90SessionOwnershipProof

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
    doctor.add_argument(
        "--issue-stress90-permit",
        action="store_true",
        help="在所有内部 P0 通过后签发一次性 Stress-90 技术启动许可",
    )
    doctor.add_argument(
        "--shadow-account",
        action="store_true",
        help="检查并为隔离的持久化 Stress-90 Shadow 账户签发技术许可",
    )

    status = sub.add_parser("status", help="只读检查本地运行状态、证据文件和磁盘空间")
    status.add_argument("--config", required=True)

    capacity = sub.add_parser(
        "stress90-capacity-report", help="零报单诊断 Stress-90 账户容量与整数代表性"
    )
    capacity.add_argument("--config", required=True)
    capacity.add_argument("--confirm-live", action="store_true")
    capacity.add_argument("--output", default="")
    capacity.add_argument("--startup-timeout", type=float, default=60.0)
    capacity.add_argument("--snapshot-wait", type=float, default=12.0)

    stress90_bootstrap = sub.add_parser(
        "stress90-bootstrap",
        help="从五个固定输入重放并创建不可变 Stress-90 seed/state",
    )
    stress90_bootstrap.add_argument("--config", required=True)
    stress90_bootstrap.add_argument("--runtime-dir", required=True)
    stress90_bootstrap.add_argument("--through", required=True, help="最终 target day（YYYYMMDD）")

    stress90_registry_init = sub.add_parser(
        "stress90-registry-init",
        help="一次性初始化固定的机器级账户/runtime lineage registry",
    )
    stress90_registry_init.add_argument("--config", required=True)
    stress90_registry_init.add_argument("--confirm-initialize", action="store_true")
    stress90_registry_init.add_argument("--operator-reason", required=True)

    stress90_activate = sub.add_parser(
        "stress90-activate",
        help="在停机、空仓、无活动委托并完成对账后显式绑定 Stress-90 identity",
    )
    stress90_activate.add_argument("--config", required=True)
    stress90_activate.add_argument("--confirm-live", action="store_true")
    stress90_activate.add_argument("--confirm-activation", action="store_true")
    stress90_activate.add_argument(
        "--confirm-rebase",
        action="store_true",
        help="also required when returning an exact migrated execution_aligned state",
    )
    stress90_activate.add_argument("--operator-reason", required=True)
    stress90_activate.add_argument(
        "--operation-id",
        required=True,
        help="operator-generated 64-hex lifecycle nonce; reuse only for exact retry",
    )
    stress90_activate.add_argument("--runtime-dir", default="")
    stress90_activate.add_argument("--startup-timeout", type=float, default=60.0)
    stress90_activate.add_argument("--snapshot-wait", type=float, default=12.0)
    stress90_activate.add_argument(
        "--shadow-account",
        action="store_true",
        help="绑定隔离的持久化 Shadow 模拟账户，而不绑定真实 CTP 资金账户",
    )

    stress90_prepare = sub.add_parser(
        "stress90-prepare-decision",
        help="在 HALTED、零报单权限下逐日补齐并持久化当前 CTP 日候选决策",
    )
    stress90_prepare.add_argument("--config", required=True)
    stress90_prepare.add_argument("--confirm-live", action="store_true")
    stress90_prepare.add_argument("--runtime-dir", default="")
    stress90_prepare.add_argument("--startup-timeout", type=float, default=60.0)
    stress90_prepare.add_argument("--snapshot-wait", type=float, default=12.0)
    stress90_prepare.add_argument(
        "--shadow-account",
        action="store_true",
        help="为隔离的持久化 Stress-90 Shadow 账户准备同源候选",
    )

    stress90_oi_collect = sub.add_parser(
        "stress90-oi-collect",
        help="以 HALTED、零报单 sidecar 从 CTP raw Tick 收集完整 Price×OI 证据",
    )
    stress90_oi_collect.add_argument("--config", required=True)
    stress90_oi_collect.add_argument("--confirm-live", action="store_true")
    stress90_oi_collect.add_argument("--runtime-dir", default="")
    stress90_oi_collect.add_argument("--startup-timeout", type=float, default=60.0)
    stress90_oi_collect.add_argument("--snapshot-wait", type=float, default=12.0)
    stress90_oi_collect.add_argument("--checkpoint-interval", type=float, default=0.1)
    stress90_oi_collect.add_argument(
        "--once", action="store_true", help="只执行一个批次用于安全检查"
    )
    stress90_oi_collect.add_argument(
        "--shadow-account",
        action="store_true",
        help="写入隔离 Shadow runtime；raw Tick 仍来自同一权威 CTP 链",
    )

    stress90_rebase = sub.add_parser(
        "stress90-account-rebase",
        help="在完整生命周期安全门后显式重置 Stress-90 live account soft path",
    )
    stress90_rebase.add_argument("--config", required=True)
    stress90_rebase.add_argument("--confirm-live", action="store_true")
    stress90_rebase.add_argument("--confirm-rebase", action="store_true")
    stress90_rebase.add_argument("--operator-reason", required=True)
    stress90_rebase.add_argument(
        "--operation-id",
        required=True,
        help="operator-generated 64-hex lifecycle nonce; use a new value for each rebase",
    )
    stress90_rebase.add_argument("--runtime-dir", default="")
    stress90_rebase.add_argument("--startup-timeout", type=float, default=60.0)
    stress90_rebase.add_argument("--snapshot-wait", type=float, default=12.0)
    stress90_rebase.add_argument("--shadow-account", action="store_true")

    stress90_settlement = sub.add_parser(
        "stress90-settlement-roll-forward",
        help="在 HALTED、零报单下原子推进一个已验证 CTP 结算日",
    )
    stress90_settlement.add_argument("--config", required=True)
    stress90_settlement.add_argument("--confirm-live", action="store_true")
    stress90_settlement.add_argument("--confirm-roll-forward", action="store_true")
    stress90_settlement.add_argument("--operator-reason", required=True)
    stress90_settlement.add_argument(
        "--operation-id",
        required=True,
        help="operator-generated 64-hex lifecycle nonce; reuse only for exact retry",
    )
    stress90_settlement.add_argument("--runtime-dir", default="")
    stress90_settlement.add_argument("--startup-timeout", type=float, default=60.0)
    stress90_settlement.add_argument("--snapshot-wait", type=float, default=12.0)
    stress90_settlement.add_argument("--shadow-account", action="store_true")

    stress90_operator_roll_forward = sub.add_parser(
        "stress90-operator-roll-forward",
        help="在 operator-managed 模式下显式确认独占账户并安全推进一个 CTP 交易日",
    )
    stress90_operator_roll_forward.add_argument("--config", required=True)
    stress90_operator_roll_forward.add_argument("--confirm-live", action="store_true")
    stress90_operator_roll_forward.add_argument(
        "--confirm-operator-continuity", action="store_true"
    )
    stress90_operator_roll_forward.add_argument("--operation-id", required=True)
    stress90_operator_roll_forward.add_argument("--operator-reason", required=True)
    stress90_operator_roll_forward.add_argument("--startup-timeout", type=float, default=60.0)
    stress90_operator_roll_forward.add_argument("--snapshot-wait", type=float, default=12.0)

    stress90_crash_fill_recovery = sub.add_parser(
        "stress90-crash-fill-recover",
        help="在 HALTED、零报单下持久化 authorized Stress-90 crash fills",
    )
    stress90_crash_fill_recovery.add_argument("--config", required=True)
    stress90_crash_fill_recovery.add_argument("--confirm-live", action="store_true")
    stress90_crash_fill_recovery.add_argument("--confirm-recovery", action="store_true")
    stress90_crash_fill_recovery.add_argument("--operator-reason", required=True)
    stress90_crash_fill_recovery.add_argument("--operation-id", required=True)
    stress90_crash_fill_recovery.add_argument("--runtime-dir", default="")
    stress90_crash_fill_recovery.add_argument("--startup-timeout", type=float, default=60.0)
    stress90_crash_fill_recovery.add_argument("--snapshot-wait", type=float, default=12.0)

    stress90_order_epoch = sub.add_parser(
        "stress90-order-journal-rollover",
        help="在完整停机安全门后封存已满的 CTP order journal epoch",
    )
    stress90_order_epoch.add_argument("--config", required=True)
    stress90_order_epoch.add_argument("--confirm-live", action="store_true")
    stress90_order_epoch.add_argument("--confirm-rollover", action="store_true")
    stress90_order_epoch.add_argument("--operator-reason", required=True)
    stress90_order_epoch.add_argument(
        "--operation-id",
        required=True,
        help="operator-generated 64-hex epoch identity; reuse only for exact retry",
    )
    stress90_order_epoch.add_argument("--runtime-dir", default="")
    stress90_order_epoch.add_argument("--startup-timeout", type=float, default=60.0)
    stress90_order_epoch.add_argument("--snapshot-wait", type=float, default=12.0)

    policy_migrate = sub.add_parser(
        "directional-policy-migrate",
        help="在停机、空仓、无活动委托并完成对账后显式迁移 Directional policy",
    )
    policy_migrate.add_argument("--config", required=True)
    policy_migrate.add_argument("--to", required=True, choices=("execution_aligned",))
    policy_migrate.add_argument("--confirm-live", action="store_true")
    policy_migrate.add_argument("--confirm-migration", action="store_true")
    policy_migrate.add_argument("--operator-reason", required=True)
    policy_migrate.add_argument(
        "--operation-id",
        required=True,
        help="operator-generated 64-hex lifecycle nonce; reuse only for exact retry",
    )
    policy_migrate.add_argument("--runtime-dir", default="")
    policy_migrate.add_argument("--startup-timeout", type=float, default=60.0)
    policy_migrate.add_argument("--snapshot-wait", type=float, default=12.0)
    policy_migrate.add_argument("--shadow-account", action="store_true")

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
    ohlc_refresh.add_argument(
        "--trading-day-evidence",
        default="",
        help="doctor/activation 写入的 Broker-derived CTP trading-day evidence",
    )

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


def wait_for_stress90_startup_orders(
    engine,
    broker,
    timeout_seconds: float,
    *,
    poll_interval: float = 0.1,
) -> None:
    """Drain exact-owned current-intent events without cancelling or strategy resubmission."""

    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    while True:
        active = broker.get_active_orders()
        if not active:
            return
        owns_order = getattr(broker, "owns_order", None)
        if not callable(owns_order) or any(
            not isinstance(order, Order) or not order.active or not owns_order(order.order_id)
            for order in active
        ):
            raise RuntimeError("unknown active order found during Stress-90 startup")
        engine.run_once()
        if bool(getattr(engine, "halted", False)):
            reason = str(getattr(getattr(engine, "state", None), "kill_reason", "") or "")
            raise RuntimeError(reason or "Stress-90 startup order replay halted")
        if not broker.get_active_orders():
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("owned Stress-90 startup orders did not settle before timeout")
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
    *,
    expected_sequence: int | None = None,
    expected_checksum: str | None = None,
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
    store.save_positions(
        state,
        positions,
        expected_sequence=expected_sequence,
        expected_checksum=expected_checksum,
    )


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
            "state recovery contains ambiguous unqualified trade identities; "
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
    if config.directional.enabled and config.directional.policy == "stress90":
        raise RuntimeError(
            "recover-state is not available for Stress-90; use the durable order "
            "journal/execution-intent restart path, or an explicit HALTED lifecycle rebase"
        )
    _require_production_confirmation(config, args)
    if not args.confirm_adopt_state or os.getenv("AFUTURE_RECOVERY_ACK") != _RECOVERY_ACK:
        raise RuntimeError(
            "state recovery requires --confirm-adopt-state and "
            "AFUTURE_RECOVERY_ACK=I_VERIFIED_CTP_POSITIONS"
        )

    store = StateStore(config.state_path)
    broker = CtpBroker(config.ctp)
    from .runtime_lease import AccountExclusiveRuntimeLease
    from .stress90_lifecycle_transaction import (
        require_no_pending_stress90_lifecycle_transaction,
    )

    runtime_dir = Path(config.state_path).parent
    lease = AccountExclusiveRuntimeLease(
        runtime_dir,
        broker.get_account_identity_digest(),
        role="recover-state",
    )
    lease.acquire()
    try:
        require_no_pending_stress90_lifecycle_transaction(runtime_dir)
        state_record = store.load_required_record()
        state = state_record.state
        if not state.kill_switch:
            raise RuntimeError("state recovery is allowed only while the kill switch is active")
        _seed_state_aware_ctp_broker(broker, state, reject_ambiguous=True)
        broker.start()
    except BaseException:
        try:
            stop_broker = getattr(broker, "stop", None)
            if callable(stop_broker):
                stop_broker()
        finally:
            lease.release()
        raise
    try:
        _wait_until_ready(broker, args.startup_timeout)
        wait_for_fresh_snapshot(broker, args.snapshot_wait)
        recovery_pairs = config.pairs + _auto_pairs_from_state(state)
        _validate_live_metadata(config, broker, recovery_pairs)
        active_orders = broker.get_active_orders()
        if active_orders:
            stress90_recovery = config.directional.policy == "stress90"
            if stress90_recovery:
                raise RuntimeError(
                    "Stress-90 state recovery is blocked by active orders; no automatic "
                    "cancellation or state adoption is permitted"
                )
            for order in active_orders:
                broker.cancel_order(order.order_id)
            state.kill_switch = True
            state.reconciled = False
            state.metadata_verified = False
            state.kill_reason = (
                "active orders found during state recovery; cancelled; "
                "rerun recovery after verification"
            )
            store.save(
                state,
                expected_sequence=state_record.sequence,
                expected_checksum=state_record.checksum,
            )
            raise RuntimeError(
                "active orders existed during recovery and were cancelled; state was not adopted"
            )

        account = broker.get_account()
        positions = broker.get_positions()
        if config.directional.policy == "stress90":
            _require_lifecycle_session_trade_ownership(
                broker,
                runtime_dir=Path(config.state_path).parent,
                timeout_seconds=max(0.1, float(args.snapshot_wait)),
            )
        validate_recovery_positions(recovery_pairs, positions)
        adopt_recovery_state(
            store,
            state,
            account,
            positions,
            expected_sequence=state_record.sequence,
            expected_checksum=state_record.checksum,
        )
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
        lease.release()


def _build_alert_manager(config) -> AlertManager:
    sinks: list[AlertSink] = [FileAlertSink(config.alert_path)]
    if config.alert_webhook:
        sinks.append(
            WebhookAlertSink(
                config.alert_webhook,
                spool_path=Path(config.alert_path).with_suffix(".outbox.sqlite3"),
            )
        )
    return AlertManager(sinks)


def _runtime_path(config, name: str) -> Path:
    return Path(config.state_path).parent / name


def _quality_recorder(config, *, shadow: bool = False) -> ExecutionQualityRecorder:
    name = "shadow_execution_quality.jsonl" if shadow else "execution_quality.jsonl"
    return ExecutionQualityRecorder(_runtime_path(config, name))


def _checkpoint_ctp_trading_day(
    config,
    broker,
    trading_day: str,
    *,
    required: bool = True,
) -> None:
    try:
        account = broker.get_account()
        account.validate()
    except (AttributeError, RuntimeError, ValueError) as exc:
        raise RuntimeError(
            "CTP trading-day checkpoint requires a valid fresh account snapshot"
        ) from exc
    try:
        normalized_day = datetime.strptime(trading_day, "%Y%m%d").strftime("%Y%m%d")
    except (TypeError, ValueError) as exc:
        raise RuntimeError("CTP trading-day checkpoint day is invalid") from exc
    if normalized_day != trading_day or account.trading_day != trading_day:
        raise RuntimeError("CTP trading-day checkpoint account/CTP trading day mismatch")

    prior_days: set[str] = set()
    generic_path = Path(config.state_path)
    if generic_path.exists():
        try:
            generic_record = StateStore(generic_path).load_required_record()
        except (OSError, ValueError) as exc:
            raise RuntimeError("CTP trading-day checkpoint generic state is untrusted") from exc
        prior_days.update(
            day
            for day in (
                generic_record.state.trading_day,
                generic_record.state.last_account_trading_day,
            )
            if day
        )
    if config.directional.enabled and config.directional.policy == "stress90":
        from .directional_stress90_state import (
            Stress90PolicyStateStore,
            Stress90StateIntegrityError,
        )

        policy_path = generic_path.with_name("stress90_policy_state.json")
        if policy_path.exists():
            try:
                policy = Stress90PolicyStateStore(policy_path).load_required()
            except (OSError, Stress90StateIntegrityError) as exc:
                raise RuntimeError(
                    "CTP trading-day checkpoint Stress-90 policy state is untrusted"
                ) from exc
            prior_days.update(
                day
                for day in (
                    policy.last_completed_target_day,
                    policy.last_completed_account_day,
                    policy.live_inception_day,
                )
                if day
            )
    if any(trading_day < prior for prior in prior_days):
        raise RuntimeError("CTP trading-day checkpoint authoritative day moved backward")

    stress90_enabled = config.directional.enabled and config.directional.policy == "stress90"
    if not stress90_enabled:
        return
    from .account_runtime_registry import AccountRuntimeRegistry
    from .directional_stress90_state import Stress90PolicyStateStore
    from .trading_day_evidence import TradingDayEvidenceStore

    runtime_dir = Path(config.state_path).resolve(strict=False).parent
    policy_path = runtime_dir / "stress90_policy_state.json"
    registry_path = _stress90_account_registry_path(config)
    if not required and (not policy_path.exists() or not registry_path.exists()):
        return
    evidence_store = TradingDayEvidenceStore(
        Path(config.state_path).with_name("ctp_trading_day_evidence.json")
    )
    policy_state = Stress90PolicyStateStore(policy_path).load_required()
    evidence_store.save_observation(
        trading_day=trading_day,
        account_identity_digest=broker.get_account_identity_digest(),
        runtime_dir=runtime_dir,
        policy_state=policy_state,
        registry=AccountRuntimeRegistry(registry_path),
    )


def _shadow_runtime_paths(config) -> dict[str, object]:
    """Keep Stress-90 candidate/account/intent state isolated from the live account."""

    runtime_dir = Path(config.state_path).parent
    if config.directional.policy == "stress90":
        shadow_dir = runtime_dir / "shadow"
        return {
            "state": shadow_dir / "state.json",
            "broker_state": shadow_dir / "shadow_broker_state.json",
            "journal": shadow_dir / "audit.jsonl",
            "persistent": True,
        }
    return {
        "state": runtime_dir / "shadow_state.json",
        "broker_state": None,
        "journal": runtime_dir / "shadow_audit.jsonl",
        "persistent": False,
    }


def _build_persistent_shadow_broker(
    config,
    live_broker,
    state_path: Path | None,
):
    """Use one canonical Shadow account identity and execution-mechanics configuration."""

    from .broker.shadow import ShadowBroker

    broker = ShadowBroker(
        live_broker,
        config.initial_capital,
        slippage_ticks=config.slippage_ticks,
        latency_ticks=max(1, config.latency_ticks),
        market_impact_ticks=max(1, config.market_impact_ticks),
        state_path=state_path,
    )
    broker.update_specs(config.contracts)
    return broker


def _shadow_operational_config(config, paths: dict[str, object]):
    """Point local operational evidence at the exact persistent Shadow runtime."""

    state_path = paths.get("state")
    journal_path = paths.get("journal")
    if not isinstance(state_path, Path) or not isinstance(journal_path, Path):
        raise RuntimeError("Shadow operational paths are invalid")
    values = {
        "state_path": str(state_path),
        "journal_path": str(journal_path),
        "log_path": str(state_path.with_name("afuture.log")),
        "report_path": str(state_path.with_name("report.json")),
        "alert_path": str(state_path.with_name("alerts.jsonl")),
    }
    return replace(config, **values)


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


def _initialize_live_engine_after_snapshot(
    engine,
    lease,
    *,
    session_timeout_seconds: float = 10.0,
) -> None:
    """Consume Stress-90 technical authority before any initialization state save."""

    requires_permit = bool(getattr(engine, "requires_technical_activation_permit", False))
    daily_circuit_day = str(
        getattr(getattr(engine, "state", None), "directional_daily_circuit_day", "") or ""
    )
    if requires_permit:
        if lease is None:
            raise RuntimeError("Stress-90 startup session verification requires the account lease")
        verify_session = getattr(engine, "verify_stress90_startup_session", None)
        if not callable(verify_session) or not verify_session(
            lease,
            timeout_seconds=max(0.1, float(session_timeout_seconds)),
        ):
            raise RuntimeError("Stress-90 complete startup session evidence was not accepted")
    if bool(getattr(engine, "halted", False)) and requires_permit and not daily_circuit_day:
        if lease is None:
            raise RuntimeError("Stress-90 technical activation requires the account lease")
        if not engine.activate_halted_runtime_from_permit(lease):
            raise RuntimeError("Stress-90 technical activation permit was not accepted")
    engine.initialize_after_ready()


def _run_live(
    config, args, logger, *, live_runtime_lease: AccountExclusiveRuntimeLease | None = None
) -> int:
    """Hold the account lease before constructing any state-writing engine."""
    from .broker.ctp import CtpBroker
    from .runtime_lease import AccountExclusiveRuntimeLease

    broker = CtpBroker(config.ctp)
    account_digest = broker.get_account_identity_digest()
    runtime = Path(config.state_path).parent
    if live_runtime_lease is not None:
        if not isinstance(live_runtime_lease, AccountExclusiveRuntimeLease) or not (
            live_runtime_lease.authorizes_technical_activation(account_digest, runtime)
        ):
            raise RuntimeError("live startup requires the held matching account/runtime lease")
        return _run_live_leased(config, args, logger, broker, live_runtime_lease)
    with AccountExclusiveRuntimeLease(runtime, account_digest, role="live") as lease:
        return _run_live_leased(config, args, logger, broker, lease)


def _run_live_leased(config, args, logger, broker, lease) -> int:
    """The caller owns the lease through construction, execution and shutdown."""
    from .journal import AuditJournal
    from .report import write_account_report

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
    try:
        engine.start()
        try:
            _wait_until_ready(broker, args.startup_timeout)
            wait_for_fresh_snapshot(broker, args.snapshot_wait)
        except RuntimeError as exc:
            engine.emergency_stop(str(exc))
            raise

        _checkpoint_ctp_trading_day(config, broker, broker.get_trading_day())

        active_orders = broker.get_active_orders()
        if active_orders:
            if config.directional.enabled and config.directional.policy == "stress90":
                if engine.halted and engine.requires_technical_activation_permit:
                    raise RuntimeError("Stress-90 technical activation requires no active orders")
                wait_for_stress90_startup_orders(
                    engine,
                    broker,
                    timeout_seconds=args.halt_drain,
                )
            else:
                for order in active_orders:
                    broker.cancel_order(order.order_id)
                engine.emergency_stop("active orders found during startup reconciliation")
                raise RuntimeError("active orders existed at startup and were cancelled")

        _initialize_live_engine_after_snapshot(
            engine,
            lease,
            session_timeout_seconds=max(
                float(args.snapshot_wait),
                float(config.metadata_timeout_seconds),
            ),
        )
        if not engine.state.metadata_verified:
            raise RuntimeError(
                f"live contract metadata verification failed: {engine.state.kill_reason}"
            )

        if engine.halted:
            if engine.requires_technical_activation_permit:
                raise RuntimeError(
                    "Stress-90 runtime remains HALTED; a fresh technical Doctor permit "
                    "or next-day daily-circuit recovery is required"
                )
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
    from .journal import AuditJournal

    if config.mode != "live" or config.ctp is None:
        raise ValueError("shadow requires system.mode=live")
    _require_production_confirmation(config, args)
    shadow_paths = _shadow_runtime_paths(config)
    shadow_broker_state = shadow_paths["broker_state"]
    if shadow_broker_state is not None and not isinstance(shadow_broker_state, Path):
        raise RuntimeError("shadow Broker state path is invalid")
    live = CtpBroker(config.ctp)
    broker = _build_persistent_shadow_broker(config, live, shadow_broker_state)
    quality = _quality_recorder(config, shadow=True)
    shadow_state = shadow_paths["state"]
    if not isinstance(shadow_state, Path):  # pragma: no cover - internal contract
        raise RuntimeError("shadow state path is invalid")
    # Ephemeral Shadow sessions start with empty local state. Stress-90 state is
    # account-path evidence and must survive restart exactly like live state.
    if shadow_paths["persistent"] is not True and shadow_state.exists():
        shadow_state.unlink()
    shadow_journal = shadow_paths["journal"]
    if not isinstance(shadow_journal, Path):  # pragma: no cover - internal contract
        raise RuntimeError("shadow journal path is invalid")
    lease = None
    if config.directional.enabled and config.directional.account_exclusive:
        from .runtime_lease import AccountExclusiveRuntimeLease

        lease = AccountExclusiveRuntimeLease(
            shadow_state.parent,
            broker.get_account_identity_digest(),
            role="shadow",
        )
        lease.acquire()
    engine = _build_cli_engine(
        config,
        broker,
        StateStore(shadow_state),
        journal=AuditJournal(shadow_journal),
        alert_manager=_build_alert_manager(config),
        auto_manager=_auto_manager(config, evidence=quality, shadow=True),
        quality_recorder=quality,
    )
    deadline = time.monotonic() + args.duration_seconds if args.duration_seconds > 0 else None
    try:
        engine.start()
        _wait_until_ready(broker, args.startup_timeout)
        wait_for_fresh_snapshot(broker, args.snapshot_wait)
        active_orders = broker.get_active_orders()
        if active_orders and engine.halted and engine.requires_technical_activation_permit:
            raise RuntimeError("Stress-90 technical activation requires no active orders")
        _initialize_live_engine_after_snapshot(
            engine,
            lease,
            session_timeout_seconds=max(
                float(args.snapshot_wait),
                float(config.metadata_timeout_seconds),
            ),
        )
        if engine.halted:
            if engine.requires_technical_activation_permit:
                raise RuntimeError(
                    "Stress-90 Shadow remains HALTED; issue a fresh technical Doctor permit"
                )
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
        if lease is not None:
            lease.release()
    return 0


def _run_doctor(config, args) -> int:
    """无订单检查 CTP、fresh snapshot、持仓、状态和本地运行条件。"""
    from .auto import AutoPairSelector
    from .broker.ctp import CtpBroker
    from .operations import build_doctor_report

    if config.mode != "live" or config.ctp is None:
        raise ValueError("doctor requires system.mode=live")
    _require_production_confirmation(config, args)
    live_config = config
    issue_permit = bool(getattr(args, "issue_stress90_permit", False))
    shadow_account = bool(getattr(args, "shadow_account", False))
    if shadow_account and (
        not config.directional.enabled or config.directional.policy != "stress90"
    ):
        raise ValueError("--shadow-account requires directional.policy=stress90")
    if issue_permit:
        _validate_stress90_lifecycle_config(config)
    live_broker = CtpBroker(config.ctp)
    if shadow_account:
        shadow_paths = _shadow_runtime_paths(config)
        shadow_state_path = shadow_paths.get("broker_state")
        if not isinstance(shadow_state_path, Path):
            raise RuntimeError("Stress-90 Shadow Broker state path is invalid")
        broker = _build_persistent_shadow_broker(
            config,
            live_broker,
            shadow_state_path,
        )
        config = _shadow_operational_config(config, shadow_paths)
    else:
        broker = live_broker
    try:
        state = StateStore(config.state_path).load()
    except (OSError, ValueError):
        state = None
    lease = None
    stress90_doctor = bool(config.directional.enabled and config.directional.policy == "stress90")
    if stress90_doctor:
        from .runtime_lease import AccountExclusiveRuntimeLease

        lease = AccountExclusiveRuntimeLease(
            Path(config.state_path).parent,
            broker.get_account_identity_digest(),
            role=("stress90-doctor-permit" if issue_permit else "stress90-doctor"),
        )
        lease.acquire()
    try:
        if stress90_doctor:
            _configure_stress90_lifecycle_order_journal(
                broker,
                Path(config.state_path).parent,
            )
        _seed_state_aware_ctp_broker(
            live_broker,
            None if shadow_account else state,
            reject_ambiguous=False,
        )
        broker.start()
        _wait_until_ready(broker, args.startup_timeout)
        wait_for_fresh_snapshot(broker, args.snapshot_wait)
        session_trades: list[Trade] = []
        session_activity_proof: Stress90SessionOwnershipProof | None = None
        mechanical: _LifecycleMechanicalSnapshot | None = None
        try:
            mechanical = _require_lifecycle_mechanical_snapshot(
                broker,
                runtime_dir=Path(config.state_path).parent,
                timeout_seconds=max(0.1, float(args.snapshot_wait)),
            )
            session_activity_proof = mechanical.session_proof
            session_trades = list(session_activity_proof.local_session_trades)
        except RuntimeError as exc:
            session_trade_ownership_valid = False
            session_trade_ownership_detail = str(exc)
        else:
            session_trade_ownership_valid = True
            session_trade_ownership_detail = (
                "complete request-bound CTP activity and "
                f"all {len(session_trades)} local session trades are owned"
            )
        if mechanical is None:
            account = broker.get_account()
            catalog = broker.get_contract_catalog()
            trading_day = broker.get_trading_day()
            positions = broker.get_positions()
            active_orders = broker.get_active_orders()
        else:
            account = mechanical.account
            catalog = list(mechanical.catalog)
            trading_day = mechanical.trading_day
            positions = list(mechanical.positions)
            active_orders = list(mechanical.active_orders)
        if not shadow_account:
            _checkpoint_ctp_trading_day(
                live_config,
                live_broker,
                trading_day,
                required=False,
            )
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
            session_trade_ownership_valid=session_trade_ownership_valid,
            session_trade_ownership_detail=session_trade_ownership_detail,
        )
        if issue_permit:
            from .stress90_activation_permit import issue_stress90_doctor_permit

            if session_activity_proof is None or mechanical is None:
                raise RuntimeError("Stress-90 Doctor permit requires complete CTP session evidence")
            _require_lifecycle_mechanical_snapshot_current(broker, mechanical)
            issue_stress90_doctor_permit(
                report=report,
                runtime_dir=Path(config.state_path).parent,
                state_store=StateStore(config.state_path),
                account_identity_digest=broker.get_account_identity_digest(),
                account_snapshot=account,
                ctp_trading_day=trading_day,
                broker_positions=positions,
                active_orders=active_orders,
                session_trades=session_trades,
                owns_order=broker.owns_order,
                session_activity_proof=session_activity_proof,
                catalog=catalog,
                strong_confirmation=os.getenv(
                    "AFUTURE_STRESS90_ACTIVATION_PERMIT_ACK",
                    "",
                ),
                account_registry_path=_stress90_account_registry_path(config),
                account_continuity_mode=config.directional.account_continuity_mode,
            )
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0 if issue_permit or report.passed else 2
    finally:
        broker.stop()
        if lease is not None:
            lease.release()


def _run_stress90_capacity_report(config, args) -> int:
    """Build a fresh CTP-backed Stress-90 capacity report with no write capability."""

    from .broker.ctp import CtpBroker
    from .directional_activity import (
        DirectionalActivityStore,
        select_contracts_from_activity,
        validate_directional_activity_snapshot,
    )
    from .operations import build_doctor_report
    from .stress90_capacity import build_stress90_capacity_payload

    _validate_stress90_lifecycle_config(config)
    _require_production_confirmation(config, args)
    broker = CtpBroker(config.ctp)
    broker.start()
    try:
        _wait_until_ready(broker, args.startup_timeout)
        wait_for_fresh_snapshot(broker, args.snapshot_wait)
        account = broker.get_account()
        account.validate()
        trading_day = broker.get_trading_day()
        if account.trading_day != trading_day:
            raise RuntimeError("capacity report account/CTP trading day mismatch")
        catalog = broker.get_contract_catalog()
        positions = broker.get_positions()
        active_orders = broker.get_active_orders()
        snapshot = DirectionalActivityStore(
            Path(config.state_path).with_name("directional_activity.json")
        ).load()
        if snapshot is None:
            raise RuntimeError("capacity report requires completed directional activity")
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
        selected_symbols = {item.symbol for item in selected.values()}
        selected_symbols.update(position.symbol for position in positions if not position.empty)
        symbols = sorted(selected_symbols)
        quotes = _collect_doctor_quotes(
            broker,
            {
                symbol: catalog_by_symbol[symbol]
                for symbol in symbols
                if symbol in catalog_by_symbol
            },
            trading_day=trading_day,
            timeout_seconds=args.snapshot_wait,
        )
        metadata = broker.get_live_contract_specs(symbols, config.metadata_timeout_seconds)
        session_valid = False
        session_detail = "read-only complete session ownership evidence is unavailable"
        try:
            refresh = broker.refresh_session_activity
            evidence = refresh(timeout_seconds=max(0.1, float(args.snapshot_wait)))
            from .broker.ctp_order_journal import CtpOrderSubmissionJournal
            from .broker.ctp_session_query import validate_ctp_session_activity_ownership

            account_identity = broker.get_account_identity_digest()
            if (
                not isinstance(account_identity, str)
                or evidence.account_identity_digest != account_identity
                or evidence.trading_day != trading_day
            ):
                raise RuntimeError("capacity report CTP session identity mismatch")
            journal_entries = tuple(
                CtpOrderSubmissionJournal(
                    Path(config.state_path).parent / "stress90_ctp_orders.json"
                ).load_all_entries()
            )
            validate_ctp_session_activity_ownership(evidence, journal_entries)
            require_current = getattr(broker, "require_session_activity_evidence_current", None)
            if not callable(require_current):
                raise RuntimeError("capacity report cannot revalidate CTP session evidence")
            require_current(evidence)
            session_valid = True
            session_detail = "read-only complete CTP session activity ownership verified"
        except Exception as exc:
            session_detail = f"read-only session ownership failed: {exc}"
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
            session_trade_ownership_valid=session_valid,
            session_trade_ownership_detail=session_detail,
        )
        payload = build_stress90_capacity_payload(
            report,
            account_identity_digest=broker.get_account_identity_digest(),
            ctp_trading_day=trading_day,
        )
        if args.output:
            _write_json(payload, args.output)
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["hard_safety_passed"] else 2
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


def _run_stress90_registry_init(config, args) -> int:
    """Provision the fixed machine anchor without constructing a Broker or order path."""

    from .account_runtime_registry import (
        ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,
        PRODUCTION_ACCOUNT_RUNTIME_REGISTRY_PATH,
        AccountRuntimeRegistry,
    )
    from .journal import AuditJournal

    if (
        config.mode != "live"
        or not config.directional.enabled
        or config.directional.policy != "stress90"
        or not config.directional.account_exclusive
    ):
        raise ValueError(
            "stress90-registry-init requires live account-exclusive Stress-90 configuration"
        )
    if Path(config.account_registry_path) != PRODUCTION_ACCOUNT_RUNTIME_REGISTRY_PATH:
        raise ValueError("stress90-registry-init requires the fixed machine registry path")
    reason = str(args.operator_reason)
    if not reason.strip() or reason != reason.strip() or len(reason) > 1_000:
        raise ValueError("stress90-registry-init operator reason is invalid")
    if (
        not args.confirm_initialize
        or os.getenv("AFUTURE_ACCOUNT_RUNTIME_REGISTRY_ACK")
        != ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION
    ):
        raise RuntimeError(
            "registry initialization requires --confirm-initialize and "
            "AFUTURE_ACCOUNT_RUNTIME_REGISTRY_ACK="
            + ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION
        )
    record = AccountRuntimeRegistry(PRODUCTION_ACCOUNT_RUNTIME_REGISTRY_PATH).initialize(
        strong_confirmation=os.environ["AFUTURE_ACCOUNT_RUNTIME_REGISTRY_ACK"]
    )
    AuditJournal(config.journal_path).record(
        "stress90_machine_account_registry_initialized",
        {
            "registry_path": str(PRODUCTION_ACCOUNT_RUNTIME_REGISTRY_PATH),
            "registry_sequence": record.sequence,
            "registry_checksum": record.checksum,
            "operator_reason": reason,
            "orders_sent": 0,
        },
    )
    print(
        json.dumps(
            {
                "initialized": True,
                "registry_path": str(PRODUCTION_ACCOUNT_RUNTIME_REGISTRY_PATH),
                "sequence": record.sequence,
                "checksum": record.checksum,
                "bindings": len(record.bindings),
                "orders_sent": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _stress90_lifecycle_paths(
    config,
    runtime_dir: str,
    *,
    shadow_account: bool = False,
) -> dict[str, Path]:
    state = Path(config.state_path).resolve(strict=False)
    canonical_root = state.parent
    requested = Path(runtime_dir).resolve(strict=False) if runtime_dir else None
    if shadow_account:
        shadow_root = (canonical_root / "shadow").resolve(strict=False)
        if requested is None or requested != shadow_root:
            raise RuntimeError("Shadow lifecycle requires the canonical runtime/shadow directory")
        return {
            "runtime": shadow_root,
            "state": shadow_root / "state.json",
            "journal": shadow_root / "audit.jsonl",
        }
    if requested is not None and requested != canonical_root:
        raise RuntimeError(
            "live Stress-90 lifecycle runtime-dir must equal the configured canonical runtime"
        )
    return {
        "runtime": canonical_root,
        "state": state,
        "journal": Path(config.journal_path).resolve(strict=False),
    }


def _validate_stress90_lifecycle_config(config) -> None:
    from .account_runtime_registry import PRODUCTION_ACCOUNT_RUNTIME_REGISTRY_PATH
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
    registry_path = _stress90_account_registry_path(config)
    if not registry_path.is_absolute():
        raise ValueError("Stress-90 lifecycle commands require an absolute account_registry_path")
    if registry_path != PRODUCTION_ACCOUNT_RUNTIME_REGISTRY_PATH:
        raise ValueError("Stress-90 lifecycle commands require the fixed machine registry path")


def _stress90_account_registry_path(config) -> Path:
    return Path(config.account_registry_path)


@contextmanager
def _stress90_lifecycle_broker_fence(broker):
    fence = getattr(broker, "lifecycle_state_commit_fence", None)
    if not callable(fence):
        raise RuntimeError("lifecycle Broker lacks the critical-ingress commit fence")
    with fence():
        yield


def _commit_stress90_lifecycle_under_broker_fence(
    broker,
    *,
    transaction_store,
    generic_store,
    policy_store,
    prepare_transaction,
    apply_registry_transition,
    apply_evidence_transition,
    precommit_check,
):
    """Linearize every irreversible lifecycle participant under Broker ingress."""

    from .stress90_lifecycle_transaction import (
        apply_stress90_lifecycle_transaction,
    )

    with _stress90_lifecycle_broker_fence(broker):
        precommit_check()
        transaction = prepare_transaction()
        apply_registry_transition(transaction)
        apply_evidence_transition(transaction)

        def precommit_with_recovery_consumption() -> None:
            precommit_check()
            generic_target = getattr(transaction, "generic_target", None)
            if not isinstance(generic_target, RuntimeState):
                return
            marker = generic_target.strategy_states.get("stress90_crash_fill_recovery")
            if not isinstance(marker, dict) or set(marker) != {
                "schema_version",
                "transaction_id",
                "consumer_operation_nonce",
                "receipt_digest",
            }:
                return
            from .stress90_crash_fill_recovery import (
                Stress90CrashFillRecoveryStore,
                consume_stress90_crash_fill_recovery,
            )

            consume_stress90_crash_fill_recovery(
                Stress90CrashFillRecoveryStore(
                    generic_store.path.parent / "stress90_crash_fill_recovery.json"
                ),
                generic_store.load_required_record(),
                consumer_operation_nonce=transaction.operation_nonce,
            )

        return apply_stress90_lifecycle_transaction(
            transaction_store,
            generic_store=generic_store,
            policy_store=policy_store,
            precommit_check=precommit_with_recovery_consumption,
        )


def _apply_stress90_account_runtime_registry_transition(
    config,
    *,
    runtime_dir: Path,
    lifecycle_transaction,
) -> None:
    """Bind the HALTED lifecycle target to one durable machine-level lineage."""

    from .account_runtime_registry import AccountRuntimeRegistry

    registry = AccountRuntimeRegistry(_stress90_account_registry_path(config))
    source_account = lifecycle_transaction.source_account_identity_digest
    target_account = lifecycle_transaction.account_identity_digest
    source_epoch = lifecycle_transaction.source_account_epoch
    target_epoch = lifecycle_transaction.policy_target.live_account_epoch
    operation = lifecycle_transaction.operation
    if operation == "risk_overlay_reactivation":
        registry.require_binding(
            target_account, runtime_dir, lifecycle_transaction.policy_target.live_account_epoch
        )
        return
    if operation == "activation":
        if source_epoch or not target_epoch:
            raise RuntimeError("Stress-90 activation registry lineage is invalid")
        registry.bind_new(
            target_account,
            runtime_dir,
            target_epoch,
            lifecycle_transaction.operation_nonce,
        )
        return
    if operation in {"reactivation", "account_rebase"}:
        if not source_epoch or not target_epoch:
            raise RuntimeError("Stress-90 lifecycle registry epoch is missing")
        if source_account == target_account:
            registry.advance_epoch(
                target_account,
                runtime_dir,
                source_epoch,
                target_epoch,
                lifecycle_transaction.operation_nonce,
            )
            return
        registry.switch_account_binding(
            source_account_identity_digest=source_account,
            target_account_identity_digest=target_account,
            runtime_dir=runtime_dir,
            source_epoch=source_epoch,
            target_epoch=target_epoch,
            operation_id=lifecycle_transaction.operation_nonce,
        )
        return
    if operation == "stress90_to_execution_aligned":
        if not source_epoch:
            raise RuntimeError("Stress-90 migration registry epoch is missing")
        registry.acknowledge_binding_operation(
            source_account,
            runtime_dir,
            source_epoch,
            lifecycle_transaction.operation_nonce,
        )
        return
    raise RuntimeError("unsupported Stress-90 account registry lifecycle operation")


def _apply_stress90_trading_day_evidence_transition(
    config,
    *,
    runtime_dir: Path,
    lifecycle_transaction,
) -> None:
    """Commit the exact post-registry account receipt before lifecycle state targets."""

    from .account_runtime_registry import AccountRuntimeRegistry
    from .trading_day_evidence import TradingDayEvidenceStore

    account = lifecycle_transaction.account_identity_digest
    epoch = lifecycle_transaction.policy_target.live_account_epoch
    if lifecycle_transaction.operation == "risk_overlay_reactivation":
        # No account/runtime lineage changed; existing TDE remains authoritative.
        return
    if not account or not epoch:
        raise RuntimeError("Stress-90 lifecycle evidence target identity is missing")
    registry = AccountRuntimeRegistry(_stress90_account_registry_path(config))
    receipt = registry.require_binding_evidence(account, runtime_dir, epoch)
    TradingDayEvidenceStore(runtime_dir / "ctp_trading_day_evidence.json").bind_for_lifecycle(
        transaction=lifecycle_transaction,
        runtime_dir=runtime_dir,
        binding_evidence=receipt,
    )


def _require_stress90_account_runtime_registry_binding(
    config,
    *,
    runtime_dir: Path,
    broker,
    policy_state,
) -> None:
    from .account_runtime_registry import AccountRuntimeRegistry

    account = policy_state.live_account_identity_digest
    epoch = policy_state.live_account_epoch
    if account is None or epoch is None:
        raise RuntimeError("Stress-90 account runtime registry identity is missing")
    broker_account = broker.get_account_identity_digest()
    if broker_account != account:
        raise RuntimeError("Stress-90 Broker/policy account identity mismatch")
    AccountRuntimeRegistry(_stress90_account_registry_path(config)).require_binding(
        account,
        runtime_dir.resolve(strict=False),
        epoch,
    )


def _stress90_evidence_collection_catalog(
    directional_config,
    catalog: list[ContractInfo],
    trading_day: str,
) -> tuple[ContractInfo, ...]:
    """Filter the exact 50-product futures catalog for raw evidence subscriptions."""

    from .directional_sessions import PRODUCT_SESSION_MANIFEST

    try:
        target_date = datetime.strptime(trading_day, "%Y%m%d").date()
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Stress-90 evidence trading day must be YYYYMMDD") from exc
    if target_date.strftime("%Y%m%d") != trading_day:
        raise RuntimeError("Stress-90 evidence trading day must be YYYYMMDD")
    products = {str(item).upper() for item in directional_config.products}
    exchanges = {str(item).upper() for item in directional_config.exchanges}
    allowed: list[ContractInfo] = []
    seen: set[str] = set()
    for contract in catalog:
        if not isinstance(contract, ContractInfo):
            raise RuntimeError("Stress-90 evidence catalog row is invalid")
        product = str(contract.product).upper()
        if product not in products:
            continue
        exchange = str(contract.exchange).upper()
        if exchange not in exchanges:
            continue
        expected_exchange = PRODUCT_SESSION_MANIFEST[product].exchange
        if exchange != expected_exchange:
            raise RuntimeError(f"Stress-90 evidence catalog exchange mismatch: {contract.symbol}")
        symbol = str(contract.symbol).upper()
        match = re.fullmatch(r"([A-Z]{1,4})\d{3,4}", symbol)
        if match is None or match.group(1) != product:
            # Options, combinations and other non-futures instruments are not subscribed.
            continue
        try:
            expiry = datetime.fromisoformat(str(contract.expiry)).date()
            listing = (
                datetime.fromisoformat(str(contract.listing)).date()
                if str(contract.listing)
                else None
            )
        except ValueError as exc:
            raise RuntimeError(
                f"Stress-90 evidence catalog lifecycle is invalid: {contract.symbol}"
            ) from exc
        if expiry < target_date or (listing is not None and listing > target_date):
            continue
        if symbol in seen:
            raise RuntimeError("Stress-90 evidence catalog contains duplicate symbols")
        seen.add(symbol)
        allowed.append(contract)
    covered = {str(item.product).upper() for item in allowed}
    missing = sorted(products - covered)
    if missing:
        raise RuntimeError(f"Stress-90 evidence catalog missing products: {missing}")
    return tuple(sorted(allowed, key=lambda item: (item.symbol.upper(), item.exchange.upper())))


def _require_stress90_order_journal_full_audit(runtime_dir: Path) -> None:
    """Fail closed on any current or sealed CTP order-journal corruption."""

    from .broker.ctp_order_journal import CtpOrderSubmissionJournal

    audit = CtpOrderSubmissionJournal(runtime_dir / "stress90_ctp_orders.json").audit_epochs()
    record = audit.current_record
    entries = () if record is None else record.all_entries
    if any(entry.status not in {"terminal", "aborted_before_send"} for entry in entries):
        from .broker.ctp_order_journal import CtpOrderJournalIntegrityError

        raise CtpOrderJournalIntegrityError(
            "Stress-90 lifecycle requires no unresolved durable CTP orders"
        )


def _require_stress90_order_journal_recovery_structural_audit(
    runtime_dir: Path,
    *,
    account_identity_digest: str,
    trading_day: str,
) -> None:
    """Validate recoverable current-session rows before the Broker query terminalizes them."""

    from .broker.ctp_order_journal import (
        CtpOrderJournalIntegrityError,
        CtpOrderSubmissionJournal,
    )

    audit = CtpOrderSubmissionJournal(runtime_dir / "stress90_ctp_orders.json").audit_epochs()
    record = audit.current_record
    entries = () if record is None else record.all_entries
    if any(
        entry.account_identity_digest != account_identity_digest
        or entry.target_trading_day != trading_day
        for entry in entries
    ):
        raise CtpOrderJournalIntegrityError(
            "Stress-90 recovery current order journal has foreign account/day rows"
        )


def _require_lifecycle_resume_safety(
    state: RuntimeState,
    *,
    broker_positions,
    local_positions,
    active_orders,
    reconciliation_matched: bool,
    operation: str,
) -> None:
    """Re-run every mutable safety gate before rolling a prepared operation forward."""

    if state.runtime_mode != RuntimeMode.HALTED.value or not state.kill_switch:
        raise RuntimeError(f"{operation} resume requires HALTED state and kill switch")
    if not reconciliation_matched:
        raise RuntimeError(f"{operation} resume requires fresh Broker/local reconciliation")
    if any(not position.empty for position in broker_positions):
        raise RuntimeError(f"{operation} resume requires a flat Broker account")
    if any(not position.empty for position in local_positions):
        raise RuntimeError(f"{operation} resume requires flat local positions")
    if active_orders:
        raise RuntimeError(f"{operation} resume is blocked by active orders")


def _require_lifecycle_trading_day_not_backward(
    trading_day: str,
    *,
    state: RuntimeState,
    policy_state,
    operation: str,
) -> None:
    prior_days = {
        day
        for day in (
            state.trading_day,
            state.last_account_trading_day,
            policy_state.last_completed_target_day,
            policy_state.last_completed_account_day,
            policy_state.live_inception_day,
        )
        if day
    }
    if any(trading_day < prior for prior in prior_days):
        raise RuntimeError(f"{operation} authoritative trading day moved backward")


def _record_lifecycle_completion_once(
    journal,
    event_type: str,
    transaction_id: str,
    payload: dict[str, object],
) -> bool:
    """Append a transaction completion at most once while the runtime lease is held."""

    journal_path = Path(journal.path)
    candidates = [
        journal_path,
        *[journal_path.with_name(f"{journal_path.name}.{i}") for i in range(1, 15)],
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(
                            "lifecycle audit evidence contains invalid JSON"
                        ) from exc
                    if (
                        isinstance(row, dict)
                        and row.get("event_type") == event_type
                        and isinstance(row.get("payload"), dict)
                        and row["payload"].get("transaction_id") == transaction_id
                    ):
                        return False
        except UnicodeDecodeError as exc:
            raise RuntimeError("lifecycle audit evidence is not valid UTF-8") from exc
    journal.record(event_type, {**payload, "transaction_id": transaction_id})
    return True


def _require_lifecycle_operator_reason(transaction, operator_reason: str) -> None:
    if transaction.operator_reason != operator_reason.strip():
        raise RuntimeError(
            "Stress-90 lifecycle operator reason changed or mismatched prepared evidence"
        )


def _require_lifecycle_operation_nonce(args) -> str:
    nonce = str(getattr(args, "operation_id", "") or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", nonce) is None:
        raise RuntimeError("Stress-90 lifecycle operation-id must be an explicit 64-hex nonce")
    return nonce


def _require_verified_lifecycle_account_snapshot(
    account: AccountSnapshot,
    *,
    operation: str,
) -> None:
    try:
        account.validate()
    except ValueError as exc:
        raise RuntimeError(f"{operation} account/settlement evidence is unverified") from exc
    if not account.cash_flow_verified:
        raise RuntimeError(f"{operation} account cash-flow evidence is unverified")
    if (
        not account.settlement_verified
        or account.previous_settlement_equity is None
        or account.settlement_id is None
    ):
        raise RuntimeError(f"{operation} settlement evidence is unverified")


def _require_lifecycle_session_trade_ownership(
    broker,
    *,
    runtime_dir: Path,
    timeout_seconds: float,
) -> Stress90SessionOwnershipProof:
    """Persist complete request-bound CTP truth and reject every unknown identity."""

    from .stress90_session_authority import (
        establish_stress90_session_ownership,
    )

    return establish_stress90_session_ownership(
        broker,
        runtime_dir=runtime_dir,
        timeout_seconds=timeout_seconds,
        recover_crash_window=callable(getattr(broker, "recover_stress90_session_activity", None)),
    )


def _configure_stress90_lifecycle_order_journal(broker, runtime_dir: Path) -> None:
    """Install durable order ownership before any lifecycle CTP callback can arrive."""

    from .directional_stress90_policy import STRESS90_POLICY

    configure = getattr(broker, "configure_order_submission_journal", None)
    if not callable(configure):
        raise RuntimeError("lifecycle Broker lacks the durable Stress-90 order journal")
    configure(
        runtime_dir / "stress90_ctp_orders.json",
        policy_id=STRESS90_POLICY.policy_id,
        policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
        products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
    )


def _restart_account_switch_with_target_order_epoch(
    broker,
    live_broker,
    *,
    runtime_dir: Path,
    state,
    shadow_account: bool,
    startup_timeout: float,
    snapshot_wait: float,
) -> _LifecycleMechanicalSnapshot:
    """Restart only after the durable journal epoch has been rebound to the target."""

    broker.stop()
    _configure_stress90_lifecycle_order_journal(broker, runtime_dir)
    cleared_source_history = replace(
        state,
        last_order_id="",
        last_trade_id="",
        recent_trade_ids=[],
    )
    _seed_state_aware_ctp_broker(
        live_broker,
        None if shadow_account else cleared_source_history,
        reject_ambiguous=not shadow_account,
    )
    broker.start()
    _wait_until_ready(broker, startup_timeout)
    wait_for_fresh_snapshot(broker, snapshot_wait)
    return _require_lifecycle_mechanical_snapshot(
        broker,
        runtime_dir=runtime_dir,
        timeout_seconds=max(0.1, float(snapshot_wait)),
    )


def _require_account_switch_target_epoch_current(
    broker,
    live_broker,
    *,
    runtime_dir: Path,
    state,
    local_positions: list[ContractPosition],
    prior_snapshot: _LifecycleMechanicalSnapshot,
    lifecycle_transaction,
    shadow_account: bool,
    startup_timeout: float,
    snapshot_wait: float,
) -> _LifecycleMechanicalSnapshot:
    """Prove the target journal/account again after the epoch seal and restart."""

    from .reconcile import compare_positions
    from .stress90_lifecycle_transaction import (
        require_matching_stress90_lifecycle_account_evidence,
    )

    mechanical = _restart_account_switch_with_target_order_epoch(
        broker,
        live_broker,
        runtime_dir=runtime_dir,
        state=state,
        shadow_account=shadow_account,
        startup_timeout=startup_timeout,
        snapshot_wait=snapshot_wait,
    )
    _require_verified_lifecycle_account_snapshot(
        mechanical.account,
        operation="Stress-90 account switch target-epoch recovery",
    )
    identity = broker.get_account_identity_digest()
    if (
        mechanical.trading_day != lifecycle_transaction.trading_day
        or mechanical.account.trading_day != mechanical.trading_day
        or identity != lifecycle_transaction.account_identity_digest
        or mechanical.catalog != prior_snapshot.catalog
    ):
        raise RuntimeError("Stress-90 account switch target-epoch evidence changed")
    require_matching_stress90_lifecycle_account_evidence(
        lifecycle_transaction,
        mechanical.account,
        account_identity_digest=identity,
    )
    reconciliation = compare_positions(local_positions, list(mechanical.positions))
    _require_lifecycle_resume_safety(
        state,
        broker_positions=list(mechanical.positions),
        local_positions=local_positions,
        active_orders=list(mechanical.active_orders),
        reconciliation_matched=reconciliation.matched,
        operation="Stress-90 account switch target-epoch recovery",
    )
    return mechanical


@dataclass(frozen=True)
class _LifecycleMechanicalSnapshot:
    session_proof: Stress90SessionOwnershipProof
    account: AccountSnapshot
    trading_day: str
    positions: tuple[ContractPosition, ...]
    active_orders: tuple[Order, ...]
    catalog: tuple[ContractInfo, ...]


def _require_empty_session_before_account_epoch_cleanup(
    broker,
    *,
    runtime_dir: Path,
    timeout_seconds: float,
) -> _LifecycleMechanicalSnapshot:
    """Prove a switched-to account has no session activity before resuming cleanup.

    A crashed account-switch can leave the old journal sources durably sealed but not
    yet removed. Normal ownership validation intentionally rejects that intermediate
    layout, so an exact prepared retry first proves the new account session is empty,
    completes the idempotent cleanup, and then runs normal ownership validation again.
    """

    from .broker.ctp_session_query import (
        CtpSessionActivityEvidence,
        CtpSessionActivityEvidenceStore,
        validate_ctp_session_activity_ownership,
    )
    from .stress90_session_authority import Stress90SessionOwnershipProof

    refresh = getattr(broker, "refresh_session_activity", None)
    require_current = getattr(broker, "require_session_activity_evidence_current", None)
    if not callable(refresh) or not callable(require_current):
        raise RuntimeError("account epoch cleanup requires complete session evidence")
    evidence = refresh(timeout_seconds=float(timeout_seconds))
    if not isinstance(evidence, CtpSessionActivityEvidence):
        raise RuntimeError("account epoch cleanup session evidence is invalid")
    identity_getter = getattr(
        broker,
        "get_session_activity_account_identity_digest",
        broker.get_account_identity_digest,
    )
    if (
        evidence.account_identity_digest != identity_getter()
        or evidence.trading_day != broker.get_trading_day()
    ):
        raise RuntimeError("account epoch cleanup session identity mismatch")
    if evidence.orders or evidence.trades:
        raise RuntimeError("account epoch cleanup requires an empty target-account session")
    local_trades_getter = getattr(broker, "get_session_trades", None)
    if not callable(local_trades_getter):
        raise RuntimeError("account epoch cleanup requires local session evidence")
    local_trades = local_trades_getter()
    if not isinstance(local_trades, list) or local_trades:
        raise RuntimeError("account epoch cleanup requires an empty local target session")
    ownership_digest = validate_ctp_session_activity_ownership(evidence, ())
    CtpSessionActivityEvidenceStore(runtime_dir / "stress90_ctp_session_evidence.json").save(
        evidence
    )
    require_current(evidence)
    account = broker.get_account()
    trading_day = broker.get_trading_day()
    positions = tuple(broker.get_positions())
    active_orders = tuple(broker.get_active_orders())
    catalog_getter = getattr(broker, "get_contract_catalog", None)
    catalog = tuple(catalog_getter()) if callable(catalog_getter) else ()
    require_current(evidence)
    if (
        identity_getter() != evidence.account_identity_digest
        or trading_day != evidence.trading_day
        or broker.get_trading_day() != trading_day
        or broker.get_account() != account
        or tuple(broker.get_positions()) != positions
        or tuple(broker.get_active_orders()) != active_orders
        or (tuple(catalog_getter()) if callable(catalog_getter) else ()) != catalog
    ):
        raise RuntimeError("account epoch cleanup mechanical evidence changed")
    return _LifecycleMechanicalSnapshot(
        session_proof=Stress90SessionOwnershipProof(
            evidence=evidence,
            ownership_digest=ownership_digest,
            local_session_trades=(),
        ),
        account=account,
        trading_day=trading_day,
        positions=positions,
        active_orders=active_orders,
        catalog=catalog,
    )


def _require_lifecycle_mechanical_snapshot(
    broker,
    *,
    runtime_dir: Path,
    timeout_seconds: float,
) -> _LifecycleMechanicalSnapshot:
    """Query complete session truth first, then recapture every mechanical snapshot."""

    proof = _require_lifecycle_session_trade_ownership(
        broker,
        runtime_dir=runtime_dir,
        timeout_seconds=timeout_seconds,
    )
    account = broker.get_account()
    trading_day = broker.get_trading_day()
    positions = tuple(broker.get_positions())
    active_orders = tuple(broker.get_active_orders())
    catalog_getter = getattr(broker, "get_contract_catalog", None)
    catalog = tuple(catalog_getter()) if callable(catalog_getter) else ()
    require_current = getattr(broker, "require_session_activity_evidence_current", None)
    if not callable(require_current):
        raise RuntimeError("lifecycle Broker cannot keep session evidence current")
    require_current(proof.evidence)
    return _LifecycleMechanicalSnapshot(
        session_proof=proof,
        account=account,
        trading_day=trading_day,
        positions=positions,
        active_orders=active_orders,
        catalog=catalog,
    )


def _require_lifecycle_mechanical_snapshot_current(
    broker,
    snapshot: _LifecycleMechanicalSnapshot,
) -> None:
    require_current = getattr(broker, "require_session_activity_evidence_current", None)
    if not callable(require_current):
        raise RuntimeError("lifecycle Broker cannot keep session evidence current")
    require_current(snapshot.session_proof.evidence)
    account = broker.get_account()
    positions = tuple(broker.get_positions())
    active_orders = tuple(broker.get_active_orders())
    catalog_getter = getattr(broker, "get_contract_catalog", None)
    catalog = tuple(catalog_getter()) if callable(catalog_getter) else ()
    require_current(snapshot.session_proof.evidence)
    identity_getter = getattr(
        broker,
        "get_session_activity_account_identity_digest",
        broker.get_account_identity_digest,
    )
    if (
        broker.get_trading_day() != snapshot.trading_day
        or identity_getter() != snapshot.session_proof.evidence.account_identity_digest
        or account != snapshot.account
        or positions != snapshot.positions
        or active_orders != snapshot.active_orders
        or catalog != snapshot.catalog
    ):
        raise RuntimeError("lifecycle mechanical snapshot identity is no longer current")


def _build_stress90_lifecycle_manager(config, broker, *, runtime_dir: Path):
    """Construct the production Stress-90 manager for read-only lifecycle primitives."""

    from .directional_stress90_runtime import Stress90DirectionalPortfolioManager
    from .risk import RiskManager

    return Stress90DirectionalPortfolioManager(
        config.directional,
        broker,
        RiskManager(config.risk),
        policy_state_path=runtime_dir / "stress90_policy_state.json",
        seed_path=runtime_dir / "stress90_bootstrap_seed.json",
        oi_evidence_path=runtime_dir / "stress90_oi_evidence.json",
        execution_intent_path=runtime_dir / "stress90_execution_intent.json",
        static_specs=config.contracts,
    )


def _require_stress90_account_day_continuity(
    *,
    runtime_dir: Path,
    completed_account_day: str,
    current_ctp_trading_day: str,
):
    """Return an exact OHLC/OI session capability for one lifecycle commit."""

    from .directional_ohlc_cache import DirectionalOHLCCacheStore
    from .directional_stress90_oi_runtime import Stress90OiEvidenceStore
    from .directional_stress90_runtime import (
        load_stress90_account_day_continuity_evidence,
    )

    return load_stress90_account_day_continuity_evidence(
        DirectionalOHLCCacheStore(runtime_dir / "directional_ohlc_cache.json"),
        Stress90OiEvidenceStore(runtime_dir / "stress90_oi_evidence.json"),
        completed_account_day=completed_account_day,
        current_ctp_trading_day=current_ctp_trading_day,
    )


def _require_stress90_lifecycle_precommit_current(
    broker,
    mechanical: _LifecycleMechanicalSnapshot,
    *,
    runtime_dir: Path,
    lifecycle_transaction,
) -> None:
    """Revalidate Broker mechanics and any transaction-bound account-day chain."""

    _require_lifecycle_mechanical_snapshot_current(broker, mechanical)
    continuity_digest = lifecycle_transaction.account_day_continuity_digest
    continuity_source_day = lifecycle_transaction.account_day_continuity_source_day
    if continuity_digest or continuity_source_day:
        if not continuity_digest or not continuity_source_day:
            raise RuntimeError("Stress-90 lifecycle continuity identity is incomplete")
        continuity = _require_stress90_account_day_continuity(
            runtime_dir=runtime_dir,
            completed_account_day=continuity_source_day,
            current_ctp_trading_day=lifecycle_transaction.trading_day,
        )
        if continuity.continuity_digest != continuity_digest:
            raise RuntimeError("Stress-90 account-day continuity evidence changed")
    _require_lifecycle_mechanical_snapshot_current(broker, mechanical)


def _adopt_stress90_lifecycle_crash_fills(
    config,
    broker,
    *,
    runtime_dir: Path,
    state: RuntimeState,
    broker_positions: list[ContractPosition],
) -> RuntimeState:
    """Apply exact durable session fills to an in-memory lifecycle target once."""

    from .state import MAX_RECENT_TRADE_IDS

    manager = _build_stress90_lifecycle_manager(
        config,
        broker,
        runtime_dir=runtime_dir,
    )
    try:
        allowed, adopted_ids, detail = manager.reconcile_authorized_crash_fills(
            [ContractPosition(**item) for item in state.positions],
            broker_positions,
            tuple(state.recent_trade_ids),
        )
    finally:
        manager.close()
    if not allowed:
        raise RuntimeError("Stress-90 lifecycle crash-fill adoption failed: " + detail)
    recent = list(state.recent_trade_ids)
    known = set(recent)
    for identity in adopted_ids:
        if identity not in known:
            known.add(identity)
            recent.append(identity)
    if len(recent) > MAX_RECENT_TRADE_IDS:
        recent = recent[-MAX_RECENT_TRADE_IDS:]
    return replace(
        state,
        positions=[asdict(position) for position in broker_positions],
        recent_trade_ids=recent,
    )


def _require_no_unpersisted_lifecycle_crash_fill_adoption(
    persisted_state: RuntimeState,
    adopted_state: RuntimeState,
    *,
    pending_lifecycle,
    persisted_record=None,
    runtime_dir: Path | None = None,
    consumer_operation_nonce: str | None = None,
) -> RuntimeState:
    """Never let a lifecycle target erase recovered Broker-owned fill identity."""

    if adopted_state != persisted_state:
        pending = bool(
            pending_lifecycle is not None and getattr(pending_lifecycle, "status", "") == "prepared"
        )
        detail = "prepared lifecycle coordinator" if pending else "lifecycle preparation"
        raise RuntimeError(
            "Stress-90 authorized crash fills require a durable HALTED recovery "
            f"checkpoint before {detail}; lifecycle commit is blocked"
        )
    marker = persisted_state.strategy_states.get("stress90_crash_fill_recovery")
    if marker is None:
        return persisted_state
    if (
        persisted_record is None
        or getattr(persisted_record, "state", None) != persisted_state
        or runtime_dir is None
    ):
        raise RuntimeError("Stress-90 lifecycle lacks the exact crash-fill recovery proof")
    from .stress90_crash_fill_recovery import (
        Stress90CrashFillRecoveryStore,
        build_stress90_crash_fill_recovery_consumed_state,
        consume_stress90_crash_fill_recovery,
        require_committed_stress90_crash_fill_recovery,
    )

    recovery_store = Stress90CrashFillRecoveryStore(
        runtime_dir / "stress90_crash_fill_recovery.json"
    )
    try:
        require_committed_stress90_crash_fill_recovery(
            recovery_store,
            persisted_record,
        )
    except Exception as exc:
        pending = bool(
            pending_lifecycle is not None
            and getattr(pending_lifecycle, "status", "") == "prepared"
            and consumer_operation_nonce is not None
            and getattr(pending_lifecycle, "operation_nonce", None) == consumer_operation_nonce
        )
        if not pending:
            raise RuntimeError(
                "Stress-90 lifecycle crash-fill recovery proof is not exact and committed"
            ) from exc
        assert consumer_operation_nonce is not None
        try:
            consume_stress90_crash_fill_recovery(
                recovery_store,
                persisted_record,
                consumer_operation_nonce=consumer_operation_nonce,
            )
            require_committed_stress90_crash_fill_recovery(
                recovery_store,
                persisted_record,
            )
        except Exception as retry_exc:
            raise RuntimeError(
                "Stress-90 lifecycle crash-fill recovery proof is not exact and committed"
            ) from retry_exc
    if consumer_operation_nonce is None:
        return persisted_state
    return build_stress90_crash_fill_recovery_consumed_state(
        recovery_store,
        persisted_record,
        consumer_operation_nonce=consumer_operation_nonce,
    )


def _run_stress90_crash_fill_recovery(config, args) -> int:
    """Persist exact authorized current-session fills while remaining HALTED."""

    from .account_runtime_nonce_ledger import AccountRuntimeNonceLedger
    from .account_runtime_registry import AccountRuntimeRegistry
    from .broker.ctp import CtpBroker
    from .broker.ctp_session_query import CtpSessionActivityEvidenceStore
    from .directional_policy_activation import require_directional_policy_identity
    from .directional_stress90_policy import STRESS90_POLICY
    from .directional_stress90_state import Stress90PolicyStateStore, Stress90SeedStore
    from .journal import AuditJournal
    from .reconcile import compare_positions
    from .runtime_lease import AccountExclusiveRuntimeLease
    from .state import StateIntegrityError
    from .stress90_crash_fill_recovery import (
        STRESS90_CRASH_FILL_RECOVERY_CONFIRMATION,
        Stress90CrashFillRecoveryAuthority,
        Stress90CrashFillRecoveryError,
        Stress90CrashFillRecoveryStore,
        apply_stress90_crash_fill_recovery,
        build_stress90_crash_fill_recovery_checkpoint,
        build_stress90_crash_fill_recovery_request_digest,
        stress90_crash_fill_positions_digest,
    )
    from .stress90_lifecycle_transaction import Stress90LifecycleTransactionStore
    from .trading_day_evidence import (
        TradingDayEvidenceStore,
        require_authoritative_trading_day_evidence,
    )

    _validate_stress90_lifecycle_config(config)
    _require_production_confirmation(config, args)
    operation_nonce = str(args.operation_id).strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", operation_nonce) is None:
        raise RuntimeError("Stress-90 crash-fill recovery operation-id must be 64-hex")
    operator_reason = str(args.operator_reason).strip()
    if not operator_reason:
        raise RuntimeError("Stress-90 crash-fill recovery operator reason is required")
    if (
        not args.confirm_recovery
        or os.getenv("AFUTURE_STRESS90_CRASH_FILL_RECOVERY_ACK")
        != STRESS90_CRASH_FILL_RECOVERY_CONFIRMATION
    ):
        raise RuntimeError(
            "Stress-90 crash-fill recovery requires strong confirmation "
            "--confirm-recovery and AFUTURE_STRESS90_CRASH_FILL_RECOVERY_ACK="
            + STRESS90_CRASH_FILL_RECOVERY_CONFIRMATION
        )

    paths = _stress90_lifecycle_paths(config, args.runtime_dir, shadow_account=False)
    runtime_dir = paths["runtime"]
    state_store = StateStore(paths["state"])
    policy_store = Stress90PolicyStateStore(runtime_dir / "stress90_policy_state.json")
    seed_store = Stress90SeedStore(runtime_dir / "stress90_bootstrap_seed.json")
    registry = AccountRuntimeRegistry(_stress90_account_registry_path(config))
    trading_day_store = TradingDayEvidenceStore(runtime_dir / "ctp_trading_day_evidence.json")
    session_store = CtpSessionActivityEvidenceStore(
        runtime_dir / "stress90_ctp_session_evidence.json"
    )
    lifecycle_store = Stress90LifecycleTransactionStore(
        runtime_dir / "stress90_lifecycle_transaction.json"
    )
    recovery_store = Stress90CrashFillRecoveryStore(
        runtime_dir / "stress90_crash_fill_recovery.json"
    )
    broker = CtpBroker(config.ctp)
    account_identity = broker.get_account_identity_digest()
    lease = AccountExclusiveRuntimeLease(
        runtime_dir,
        account_identity,
        role="stress90-crash-fill-recover",
    )

    def authority_snapshot():
        policy_record = policy_store.load_required_record()
        policy = policy_record.state
        if (
            policy.live_account_identity_digest != account_identity
            or policy.live_account_epoch is None
        ):
            raise RuntimeError("Stress-90 crash-fill recovery policy account lineage mismatch")
        receipt = registry.require_binding_evidence(
            account_identity,
            runtime_dir,
            policy.live_account_epoch,
        )
        evidence = trading_day_store.load_required()
        lifecycle = lifecycle_store.load()
        if lifecycle is not None and lifecycle.status == "prepared":
            raise RuntimeError(
                "Stress-90 crash-fill recovery is blocked by a prepared lifecycle transaction"
            )
        authoritative = True
        try:
            require_authoritative_trading_day_evidence(
                evidence,
                policy_state=policy,
                registry=registry,
                runtime_dir=runtime_dir,
                lifecycle_transaction=lifecycle,
            )
        except Exception:
            authoritative = False
        binding = receipt.binding
        registry_nonce_receipt = AccountRuntimeNonceLedger.for_registry(
            registry.path
        ).require_receipt(
            receipt.registry_nonce_root,
            binding.last_operation_id,
        )
        recovery_source = (
            not authoritative
            and evidence.phase == "bound"
            and evidence.account_identity_digest == account_identity
            and evidence.account_epoch == policy.live_account_epoch
            and evidence.canonical_runtime == binding.canonical_runtime
            and evidence.runtime_identity_digest == binding.runtime_identity_digest
            and binding.last_operation_id == operation_nonce
            and binding.operation_kinds[-1] == "stress90_crash_fill_recovery"
            and receipt.binding_revision == evidence.account_binding_revision + 1
            and len(binding.operation_history) >= 2
            and binding.operation_history[-2] == evidence.account_binding_last_operation_id
        )
        if not authoritative and not recovery_source:
            raise RuntimeError(
                "Stress-90 crash-fill recovery trading-day evidence authority mismatch"
            )
        semantic_evidence = evidence
        if (
            authoritative
            and evidence.rebind_transaction_id == operation_nonce
            and evidence.account_binding_last_operation_id == operation_nonce
        ):
            if not trading_day_store.previous_path.exists():
                raise RuntimeError(
                    "Stress-90 crash-fill recovery source trading-day evidence is missing"
                )
            semantic_evidence = TradingDayEvidenceStore(
                trading_day_store.previous_path
            ).load_required()
        authority = Stress90CrashFillRecoveryAuthority(
            account_identity_digest=account_identity,
            account_epoch=policy.live_account_epoch,
            canonical_runtime=receipt.binding.canonical_runtime,
            runtime_identity_digest=receipt.binding.runtime_identity_digest,
            account_binding_payload_digest=receipt.binding_payload_digest,
            account_binding_revision=receipt.binding_revision,
            account_binding_last_operation_id=receipt.binding.last_operation_id,
            account_binding_last_operation_receipt_digest=(
                receipt.binding.last_operation_receipt_digest
            ),
            account_binding_receipt_digest=receipt.binding_receipt_digest,
            registry_sequence=receipt.registry_sequence,
            registry_checksum=receipt.registry_checksum,
            registry_nonce_root=receipt.registry_nonce_root,
            registry_nonce_count=receipt.registry_nonce_count,
            registry_nonce_receipt_checksum=registry_nonce_receipt.checksum,
            policy_state_sequence=policy_record.sequence,
            policy_state_checksum=policy_record.checksum,
        )
        return policy_record, authority, evidence, semantic_evidence

    def account_authority_identity(current):
        return (
            current.account_identity_digest,
            current.account_epoch,
            current.canonical_runtime,
            current.runtime_identity_digest,
            current.account_binding_payload_digest,
            current.account_binding_revision,
            current.account_binding_last_operation_id,
            current.account_binding_last_operation_receipt_digest,
            current.account_binding_receipt_digest,
            current.registry_nonce_receipt_checksum,
            current.policy_state_sequence,
            current.policy_state_checksum,
        )

    lease.acquire()
    try:
        if not lease.authorizes_technical_activation(account_identity, runtime_dir):
            raise RuntimeError("exact account/runtime lease is not held for crash-fill recovery")
        source_record = state_store.load_required_record()
        source_state = source_record.state
        if source_state.runtime_mode != RuntimeMode.HALTED.value:
            raise RuntimeError("Stress-90 crash-fill recovery requires HALTED state")
        if not source_state.kill_switch:
            raise RuntimeError("Stress-90 crash-fill recovery requires the kill switch")
        seed = seed_store.load_required()
        (
            policy_record,
            authority,
            trading_day_evidence,
            semantic_trading_day_evidence,
        ) = authority_snapshot()
        _require_stress90_order_journal_recovery_structural_audit(
            runtime_dir,
            account_identity_digest=account_identity,
            trading_day=trading_day_evidence.trading_day,
        )
        require_directional_policy_identity(
            source_state,
            policy_id=STRESS90_POLICY.policy_id,
            policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
            products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
            bootstrap_seed_digest=seed.seed_digest,
            account_identity_digest=account_identity,
        )
        _configure_stress90_lifecycle_order_journal(broker, runtime_dir)
        _seed_state_aware_ctp_broker(broker, source_state, reject_ambiguous=True)
        broker.start()
    except BaseException:
        try:
            broker.stop()
        finally:
            lease.release()
        raise

    try:
        _wait_until_ready(broker, args.startup_timeout)
        wait_for_fresh_snapshot(broker, args.snapshot_wait)
        mechanical = _require_lifecycle_mechanical_snapshot(
            broker,
            runtime_dir=runtime_dir,
            timeout_seconds=max(0.1, float(args.snapshot_wait)),
        )
        if mechanical.active_orders:
            raise RuntimeError(
                "Stress-90 crash-fill recovery is blocked by active orders; "
                "no cancellation is permitted"
            )
        if (
            mechanical.trading_day != trading_day_evidence.trading_day
            or mechanical.session_proof.evidence.account_identity_digest != account_identity
            or mechanical.session_proof.evidence.trading_day != mechanical.trading_day
        ):
            raise RuntimeError("Stress-90 crash-fill recovery session identity mismatch")
        broker_positions = list(mechanical.positions)
        persisted_state = source_state
        adopted_state = _adopt_stress90_lifecycle_crash_fills(
            config,
            broker,
            runtime_dir=runtime_dir,
            state=persisted_state,
            broker_positions=broker_positions,
        )
        local_after_adoption = state_store.positions_from_state(adopted_state)
        reconciliation = compare_positions(local_after_adoption, broker_positions)
        if not reconciliation.matched:
            raise RuntimeError(
                "Stress-90 crash-fill recovery Broker/local reconciliation failed: "
                + reconciliation.details
            )
        before_ids = set(persisted_state.recent_trade_ids)
        adopted_ids = tuple(
            item for item in adopted_state.recent_trade_ids if item not in before_ids
        )
        session_fill_ids = {item.fill_key for item in mechanical.session_proof.evidence.trades}
        existing_recovery = recovery_store.load_record()
        checkpoint = None
        target_state = None
        session_record = None
        source_positions_digest = ""
        target_positions_digest = ""
        request_digest = ""

        if existing_recovery is not None and (
            existing_recovery.checkpoint.operation_nonce == operation_nonce
        ):
            checkpoint = existing_recovery.checkpoint
            stable_authority = account_authority_identity(checkpoint.authority)
            current_authority = account_authority_identity(authority)
            evidence = mechanical.session_proof.evidence
            if (
                checkpoint.operator_reason != operator_reason
                or stable_authority != current_authority
                or checkpoint.trading_day != mechanical.trading_day
                or checkpoint.trading_day_evidence != trading_day_evidence
                or checkpoint.session_evidence.account_identity_digest
                != evidence.account_identity_digest
                or checkpoint.session_evidence.orders_digest != evidence.orders_digest
                or checkpoint.session_evidence.trades_digest != evidence.trades_digest
                or checkpoint.session_ownership_digest != mechanical.session_proof.ownership_digest
                or checkpoint.target_positions_digest
                != stress90_crash_fill_positions_digest(broker_positions)
                or not set(checkpoint.adopted_fill_ids).issubset(session_fill_ids)
            ):
                raise RuntimeError(
                    "Stress-90 crash-fill recovery nonce was reused with changed evidence"
                )
        else:
            if not adopted_ids:
                raise RuntimeError("no unpersisted authorized Stress-90 crash fills were found")
            if not set(adopted_ids).issubset(session_fill_ids):
                raise RuntimeError(
                    "adopted crash-fill identities are not bound to complete session evidence"
                )
            target_state = replace(
                adopted_state,
                kill_switch=True,
                kill_reason=(
                    "Stress-90 crash fills recovered; lifecycle/Doctor gates remain required"
                ),
                runtime_mode=RuntimeMode.HALTED.value,
                reconciled=True,
                metadata_verified=False,
            )
            session_record = session_store.load_required_record()
            if session_record.evidence != mechanical.session_proof.evidence:
                raise RuntimeError("persisted complete session recovery evidence changed")
            source_positions_digest = stress90_crash_fill_positions_digest(
                state_store.positions_from_state(persisted_state)
            )
            target_positions_digest = stress90_crash_fill_positions_digest(broker_positions)
            request_digest = build_stress90_crash_fill_recovery_request_digest(
                operation_nonce=operation_nonce,
                operator_reason=operator_reason,
                account_identity_digest=authority.account_identity_digest,
                account_epoch=authority.account_epoch,
                canonical_runtime=authority.canonical_runtime,
                trading_day_evidence=semantic_trading_day_evidence,
                session_evidence=mechanical.session_proof.evidence,
                session_ownership_digest=mechanical.session_proof.ownership_digest,
                generic_source=source_record,
                generic_target=target_state,
                source_positions_digest=source_positions_digest,
                target_positions_digest=target_positions_digest,
                adopted_fill_ids=adopted_ids,
            )

        with _stress90_lifecycle_broker_fence(broker):
            _require_lifecycle_mechanical_snapshot_current(broker, mechanical)
            _require_stress90_order_journal_full_audit(runtime_dir)
            if not lease.authorizes_technical_activation(account_identity, runtime_dir):
                raise RuntimeError(
                    "exact account/runtime lease changed before crash-fill recovery commit"
                )
            (
                current_policy,
                current_authority,
                current_trading_day,
                current_semantic_trading_day,
            ) = authority_snapshot()
            if (
                current_policy != policy_record
                or current_trading_day != trading_day_evidence
                or current_semantic_trading_day != semantic_trading_day_evidence
                or account_authority_identity(current_authority)
                != account_authority_identity(authority)
            ):
                raise RuntimeError("Stress-90 crash-fill recovery authority changed before commit")
            broker.require_session_activity_evidence_current(mechanical.session_proof.evidence)
            semantic_request_digest = (
                checkpoint.request_digest if checkpoint is not None else request_digest
            )
            registry.acknowledge_stress90_recovery_operation(
                account_identity,
                runtime_dir,
                policy_record.state.live_account_epoch,
                operation_nonce,
                semantic_request_digest,
            )
            post_receipt = registry.require_binding_evidence(
                account_identity,
                runtime_dir,
                policy_record.state.live_account_epoch,
            )
            post_trading_day = trading_day_store.roll_forward_stress90_recovery_binding(
                source_evidence=semantic_trading_day_evidence,
                binding_evidence=post_receipt,
                operation_nonce=operation_nonce,
            )
            post_authority = Stress90CrashFillRecoveryAuthority(
                account_identity_digest=account_identity,
                account_epoch=str(policy_record.state.live_account_epoch),
                canonical_runtime=post_receipt.binding.canonical_runtime,
                runtime_identity_digest=post_receipt.binding.runtime_identity_digest,
                account_binding_payload_digest=post_receipt.binding_payload_digest,
                account_binding_revision=post_receipt.binding_revision,
                account_binding_last_operation_id=post_receipt.binding.last_operation_id,
                account_binding_last_operation_receipt_digest=(
                    post_receipt.binding.last_operation_receipt_digest
                ),
                account_binding_receipt_digest=post_receipt.binding_receipt_digest,
                registry_sequence=post_trading_day.registry_sequence,
                registry_checksum=post_trading_day.registry_checksum,
                registry_nonce_root=post_receipt.registry_nonce_root,
                registry_nonce_count=post_receipt.registry_nonce_count,
                registry_nonce_receipt_checksum=(
                    AccountRuntimeNonceLedger.for_registry(registry.path)
                    .require_receipt(
                        post_receipt.registry_nonce_root,
                        operation_nonce,
                    )
                    .checksum
                ),
                policy_state_sequence=policy_record.sequence,
                policy_state_checksum=policy_record.checksum,
            )
            if checkpoint is None:
                assert target_state is not None and session_record is not None
                checkpoint = build_stress90_crash_fill_recovery_checkpoint(
                    operation_nonce=operation_nonce,
                    request_digest=semantic_request_digest,
                    operator_reason=operator_reason,
                    authority=post_authority,
                    trading_day=mechanical.trading_day,
                    semantic_trading_day_evidence=semantic_trading_day_evidence,
                    trading_day_evidence=post_trading_day,
                    session_evidence=mechanical.session_proof.evidence,
                    session_evidence_sequence=session_record.sequence,
                    session_evidence_checksum=session_record.checksum,
                    session_ownership_digest=mechanical.session_proof.ownership_digest,
                    generic_source=source_record,
                    generic_target=target_state,
                    source_positions_digest=source_positions_digest,
                    target_positions_digest=target_positions_digest,
                    adopted_fill_ids=adopted_ids,
                )
            elif (
                account_authority_identity(checkpoint.authority)
                != account_authority_identity(post_authority)
                or checkpoint.trading_day_evidence != post_trading_day
            ):
                raise RuntimeError("Stress-90 crash-fill recovery post-ack authority changed")
            if checkpoint.status == "prepared":
                recovery_store.begin(checkpoint)
            completed = apply_stress90_crash_fill_recovery(recovery_store, state_store)
            _require_lifecycle_mechanical_snapshot_current(broker, mechanical)

        _record_lifecycle_completion_once(
            AuditJournal(paths["journal"]),
            "stress90_crash_fill_recovery",
            completed.checkpoint.transaction_id,
            {
                "operation_nonce": completed.checkpoint.operation_nonce,
                "trading_day": completed.checkpoint.trading_day,
                "adopted_fill_ids": list(completed.checkpoint.adopted_fill_ids),
                "runtime_mode": RuntimeMode.HALTED.value,
                "kill_switch": True,
                "orders_sent": 0,
            },
        )
        print(
            json.dumps(
                {
                    "recovered": True,
                    "exact_retry": existing_recovery is not None,
                    "transaction_id": completed.checkpoint.transaction_id,
                    "trading_day": completed.checkpoint.trading_day,
                    "adopted_fill_ids": list(completed.checkpoint.adopted_fill_ids),
                    "runtime_mode": RuntimeMode.HALTED.value,
                    "kill_switch": True,
                    "orders_sent": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (StateIntegrityError, Stress90CrashFillRecoveryError) as exc:
        raise RuntimeError(str(exc)) from exc
    finally:
        broker.stop()
        lease.release()


def _run_stress90_prepare_decision(config, args) -> int:
    """Prepare candidate state under a HALTED account lease and a no-order broker fence."""

    from .broker.ctp import CtpBroker
    from .directional_activity import (
        DirectionalActivityStore,
        validate_directional_activity_snapshot,
    )
    from .directional_policy_activation import require_directional_policy_identity
    from .directional_stress90_policy import STRESS90_POLICY
    from .directional_stress90_runtime import prepare_halted_stress90_candidate
    from .directional_stress90_state import Stress90PolicyStateStore, Stress90SeedStore
    from .journal import AuditJournal
    from .reconcile import compare_positions
    from .runtime_lease import AccountExclusiveRuntimeLease
    from .stress90_lifecycle_transaction import (
        require_no_pending_stress90_lifecycle_transaction,
    )

    _validate_stress90_lifecycle_config(config)
    _require_production_confirmation(config, args)
    shadow_account = bool(getattr(args, "shadow_account", False))
    paths = _stress90_lifecycle_paths(
        config,
        args.runtime_dir,
        shadow_account=shadow_account,
    )
    store = StateStore(paths["state"])
    live_broker = CtpBroker(config.ctp)
    broker = (
        _build_persistent_shadow_broker(
            config,
            live_broker,
            paths["runtime"] / "shadow_broker_state.json",
        )
        if shadow_account
        else live_broker
    )
    lease = AccountExclusiveRuntimeLease(
        paths["runtime"],
        broker.get_account_identity_digest(),
        role="stress90-prepare-decision",
    )
    lease.acquire()
    try:
        require_no_pending_stress90_lifecycle_transaction(paths["runtime"])
        _require_stress90_order_journal_full_audit(paths["runtime"])
        generic_record = store.load_required_record()
        state = generic_record.state
        if (
            state.runtime_mode != RuntimeMode.HALTED.value
            or not state.kill_switch
            or not state.reconciled
        ):
            raise RuntimeError(
                "Stress-90 decision preparation requires HALTED, kill-switched, reconciled state"
            )
        seed = Stress90SeedStore(paths["runtime"] / "stress90_bootstrap_seed.json").load_required()
        policy_store = Stress90PolicyStateStore(paths["runtime"] / "stress90_policy_state.json")
        policy_record = policy_store.load_required_record()
        if policy_record.state.bootstrap_seed_digest != seed.seed_digest:
            raise RuntimeError("Stress-90 decision preparation seed/state identity mismatch")
        _require_stress90_account_runtime_registry_binding(
            config,
            runtime_dir=paths["runtime"],
            broker=broker,
            policy_state=policy_record.state,
        )
        _configure_stress90_lifecycle_order_journal(broker, paths["runtime"])
        _seed_state_aware_ctp_broker(
            live_broker,
            None if shadow_account else state,
            reject_ambiguous=not shadow_account,
        )
        broker.start()
    except BaseException:
        try:
            broker.stop()
        finally:
            lease.release()
        raise
    try:
        _wait_until_ready(broker, args.startup_timeout)
        wait_for_fresh_snapshot(broker, args.snapshot_wait)
        mechanical = _require_lifecycle_mechanical_snapshot(
            broker,
            runtime_dir=paths["runtime"],
            timeout_seconds=max(0.1, float(args.snapshot_wait)),
        )
        account = mechanical.account
        trading_day = mechanical.trading_day
        positions = list(mechanical.positions)
        active_orders = list(mechanical.active_orders)
        _require_verified_lifecycle_account_snapshot(
            account,
            operation="Stress-90 decision preparation",
        )
        if account.trading_day != trading_day:
            raise RuntimeError("Stress-90 decision preparation account/CTP day mismatch")
        _require_lifecycle_trading_day_not_backward(
            trading_day,
            state=state,
            policy_state=policy_record.state,
            operation="Stress-90 decision preparation",
        )
        if active_orders:
            raise RuntimeError("Stress-90 decision preparation requires no active orders")
        local_positions = store.positions_from_state(state)
        reconciliation = compare_positions(local_positions, positions)
        if not reconciliation.matched:
            raise RuntimeError(
                "Stress-90 decision preparation Broker/local reconciliation failed: "
                + reconciliation.details
            )
        account_identity = broker.get_account_identity_digest()
        require_directional_policy_identity(
            state,
            policy_id=STRESS90_POLICY.policy_id,
            policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
            products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
            bootstrap_seed_digest=seed.seed_digest,
            account_identity_digest=account_identity,
        )
        if policy_record.state.live_account_identity_digest != account_identity:
            raise RuntimeError("Stress-90 decision preparation policy/account identity mismatch")

        already_prepared = bool(
            policy_record.state.prepared_decision is not None
            and policy_record.state.prepared_decision.target_trading_day == trading_day
            and policy_record.state.last_completed_target_day == trading_day
        )
        required_activity_day = policy_record.state.last_completed_input_day
        if not already_prepared:
            snapshot = DirectionalActivityStore(
                paths["runtime"] / "directional_activity.json"
            ).load()
            if snapshot is None:
                raise RuntimeError(
                    "Stress-90 decision preparation requires completed directional activity"
                )
            validate_directional_activity_snapshot(snapshot)
            if snapshot.trading_day >= trading_day:
                raise RuntimeError(
                    "Stress-90 decision preparation activity must be strictly prior to target"
                )
            required_activity_day = snapshot.trading_day
        _require_lifecycle_mechanical_snapshot_current(broker, mechanical)
        if not shadow_account:
            _checkpoint_ctp_trading_day(config, live_broker, trading_day)
        prepared = prepare_halted_stress90_candidate(
            directional_config=config.directional,
            risk_config=config.risk,
            runtime_dir=paths["runtime"],
            current_ctp_trading_day=trading_day,
            required_activity_day=required_activity_day,
            static_specs=config.contracts,
        )
        AuditJournal(paths["journal"]).record(
            "stress90_candidate_prepared_without_order_authority",
            {
                "target_trading_day": prepared.target_trading_day,
                "daily_decision_digest": prepared.daily_decision_digest,
                "account_identity_digest": account_identity,
                "orders_sent": 0,
            },
        )
        print(
            json.dumps(
                {
                    "prepared": True,
                    "target_trading_day": prepared.target_trading_day,
                    "daily_decision_digest": prepared.daily_decision_digest,
                    "orders_sent": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        broker.stop()
        lease.release()


def _run_stress90_oi_collect(config, args) -> int:
    """Collect raw CTP OI plus completed activity with an order-capability fence."""

    from .broker.ctp import CtpBroker
    from .directional_activity import DirectionalActivityStore, DirectionalActivityTracker
    from .directional_ohlc_cache import DirectionalOHLCCacheStore
    from .directional_policy_activation import require_directional_policy_identity
    from .directional_stress90_oi_runtime import (
        Stress90EvidenceOnlyBroker,
        Stress90OiEvidenceAggregator,
        Stress90OiEvidenceStore,
        arm_stress90_raw_evidence_collection,
    )
    from .directional_stress90_policy import STRESS90_POLICY
    from .directional_stress90_runtime import stress90_target_transitions
    from .directional_stress90_state import Stress90PolicyStateStore, Stress90SeedStore
    from .journal import AuditJournal
    from .reconcile import compare_positions
    from .runtime_lease import AccountExclusiveRuntimeLease
    from .stress90_lifecycle_transaction import (
        require_no_pending_stress90_lifecycle_transaction,
    )

    _validate_stress90_lifecycle_config(config)
    _require_production_confirmation(config, args)
    interval = float(args.checkpoint_interval)
    if interval != interval or interval <= 0.0 or interval > 60.0:
        raise ValueError("Stress-90 evidence checkpoint interval must be in (0, 60]")
    shadow_account = bool(getattr(args, "shadow_account", False))
    paths = _stress90_lifecycle_paths(
        config,
        args.runtime_dir,
        shadow_account=shadow_account,
    )
    state_store = StateStore(paths["state"])
    live_broker = CtpBroker(config.ctp)
    source_broker = (
        _build_persistent_shadow_broker(
            config,
            live_broker,
            paths["runtime"] / "shadow_broker_state.json",
        )
        if shadow_account
        else live_broker
    )
    lease = AccountExclusiveRuntimeLease(
        paths["runtime"],
        source_broker.get_account_identity_digest(),
        role="stress90-oi-collect",
    )
    lease.acquire()
    broker: Stress90EvidenceOnlyBroker | None = None
    started = False
    aggregator: Stress90OiEvidenceAggregator | None = None
    activity_tracker: DirectionalActivityTracker | None = None
    try:
        # Every local authority file is first loaded only after the account lease.
        _configure_stress90_lifecycle_order_journal(source_broker, paths["runtime"])
        broker = Stress90EvidenceOnlyBroker(source_broker)
        state_store._require_fresh_or_current()
        seed = Stress90SeedStore(paths["runtime"] / "stress90_bootstrap_seed.json").load_required()
        policy_record = Stress90PolicyStateStore(
            paths["runtime"] / "stress90_policy_state.json"
        ).load_required_record()
        if policy_record.state.bootstrap_seed_digest != seed.seed_digest:
            raise RuntimeError("Stress-90 evidence collection seed/state identity mismatch")
        generic_record = state_store.load_record()
        if generic_record is None:
            if policy_record.state.live_account_identity_digest is not None:
                raise RuntimeError(
                    "bound Stress-90 policy cannot collect evidence without generic runtime state"
                )
        else:
            generic_state = generic_record.state
            if (
                generic_state.runtime_mode != RuntimeMode.HALTED.value
                or not generic_state.kill_switch
                or not generic_state.reconciled
            ):
                raise RuntimeError(
                    "Stress-90 evidence collection requires HALTED, kill-switched, reconciled state"
                )
            if policy_record.state.live_account_identity_digest is None:
                raise RuntimeError(
                    "unbound Stress-90 policy cannot reuse an existing generic runtime state"
                )
            _require_stress90_account_runtime_registry_binding(
                config,
                runtime_dir=paths["runtime"],
                broker=source_broker,
                policy_state=policy_record.state,
            )
        require_no_pending_stress90_lifecycle_transaction(paths["runtime"])
        _require_stress90_order_journal_full_audit(paths["runtime"])
        if generic_record is not None:
            _seed_state_aware_ctp_broker(
                live_broker,
                None if shadow_account else generic_record.state,
                reject_ambiguous=not shadow_account,
            )
        started = True
        broker.start()
        _wait_until_ready(broker, args.startup_timeout)
        wait_for_fresh_snapshot(broker, args.snapshot_wait)
        mechanical = _require_lifecycle_mechanical_snapshot(
            broker,
            runtime_dir=paths["runtime"],
            timeout_seconds=max(0.1, float(args.snapshot_wait)),
        )
        session_activity = mechanical.session_proof
        account = mechanical.account
        trading_day = mechanical.trading_day
        positions = list(mechanical.positions)
        active_orders = list(mechanical.active_orders)
        _require_verified_lifecycle_account_snapshot(
            account,
            operation="Stress-90 evidence collection",
        )
        if account.trading_day != trading_day:
            raise RuntimeError("Stress-90 evidence collection account/CTP day mismatch")
        if trading_day < policy_record.state.last_completed_target_day:
            raise RuntimeError("Stress-90 evidence collection trading day moved backward")
        if active_orders:
            raise RuntimeError("Stress-90 evidence collection requires no active orders")
        if generic_record is None:
            if (
                session_activity.evidence.orders
                or session_activity.evidence.trades
                or session_activity.local_session_trades
            ):
                raise RuntimeError(
                    "pre-activation Stress-90 evidence collection requires no session activity"
                )
            if any(not position.empty for position in positions):
                raise RuntimeError(
                    "pre-activation Stress-90 evidence collection requires a flat Broker account"
                )
        else:
            local_positions = state_store.positions_from_state(generic_record.state)
            reconciliation = compare_positions(local_positions, positions)
            if not reconciliation.matched:
                raise RuntimeError(
                    "Stress-90 evidence collection Broker/local reconciliation failed: "
                    + reconciliation.details
                )
            account_identity = source_broker.get_account_identity_digest()
            require_directional_policy_identity(
                generic_record.state,
                policy_id=STRESS90_POLICY.policy_id,
                policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
                products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
                bootstrap_seed_digest=seed.seed_digest,
                account_identity_digest=account_identity,
            )
            if policy_record.state.live_account_identity_digest != account_identity:
                raise RuntimeError("Stress-90 evidence collection policy/account mismatch")

        # A fresh/restarted collector may begin only at the next observed session.  A
        # persisted in-progress day proves continuity across a process restart.
        oi_store = Stress90OiEvidenceStore(paths["runtime"] / "stress90_oi_evidence.json")
        oi_record = oi_store.load_required_record()
        anchor = (
            oi_record.state.in_progress.trading_day
            if oi_record.state.in_progress is not None
            else oi_record.state.completed[-1].trading_day
        )
        if trading_day > anchor:
            ohlc = DirectionalOHLCCacheStore(paths["runtime"] / "directional_ohlc_cache.json").load(
                STRESS90_POLICY.products
            )
            if ohlc is None:
                raise RuntimeError("Stress-90 evidence collection OHLC continuity is missing")
            transitions = stress90_target_transitions(
                last_completed_target_day=anchor,
                current_ctp_trading_day=trading_day,
                completed_close_index=ohlc.close.index,
            )
            if transitions != ((anchor, trading_day),):
                raise RuntimeError(
                    "Stress-90 evidence collection refuses to skip target trading days"
                )
        if trading_day < anchor:
            raise RuntimeError("Stress-90 evidence collection state moved backward")

        try:
            live_broker.refresh_contract_catalog(
                timeout_seconds=max(0.001, float(config.metadata_timeout_seconds))
            )
        except Exception:
            if not live_broker.contract_catalog_verified_for_day(trading_day):
                raise
        catalog = live_broker.get_contract_catalog()
        subscriptions = _stress90_evidence_collection_catalog(
            config.directional,
            catalog,
            trading_day,
        )
        aggregator = Stress90OiEvidenceAggregator(store=oi_store)
        activity_tracker = DirectionalActivityTracker(
            DirectionalActivityStore(paths["runtime"] / "directional_activity.json")
        )
        _require_lifecycle_mechanical_snapshot_current(broker, mechanical)
        arm_stress90_raw_evidence_collection(
            broker,
            aggregator,
            trading_day=trading_day,
            catalog=catalog,
            subscription_catalog=subscriptions,
        )
        aggregator.checkpoint()
        catalog_by_symbol = {item.symbol: item for item in subscriptions}
        subscribed = {(item.symbol, item.exchange) for item in subscriptions}
        if not shadow_account:
            _checkpoint_ctp_trading_day(config, live_broker, trading_day)
        audit = AuditJournal(paths["journal"])
        audit.record(
            "stress90_raw_evidence_collection_started",
            {
                "trading_day": trading_day,
                "subscription_count": len(subscriptions),
                "orders_sent": 0,
            },
        )

        batches = 0
        while True:
            events = broker.poll_events()
            for event in events:
                if event.event_type in {"order", "trade"}:
                    raise RuntimeError(
                        f"Stress-90 evidence collection observed concurrent {event.event_type}"
                    )
                if event.event_type in {"broker_error", "account_error"}:
                    raise RuntimeError(
                        f"Stress-90 evidence collection Broker event failed: {event.payload}"
                    )
                if event.event_type != "tick":
                    continue
                tick = event.payload
                if not isinstance(tick, Tick):
                    raise RuntimeError("Stress-90 evidence collection Tick is invalid")
                contract = catalog_by_symbol.get(tick.symbol)
                if contract is not None:
                    activity_tracker.observe(tick, contract)
            # Persistence occurs only after the bounded Broker batch, never in callback.
            aggregator.checkpoint()
            activity_tracker.checkpoint()
            batches += 1
            current_day = broker.get_trading_day()
            if current_day < trading_day:
                raise RuntimeError("Stress-90 evidence collection trading day moved backward")
            if current_day != trading_day:
                fresh_account = broker.get_account()
                _require_verified_lifecycle_account_snapshot(
                    fresh_account,
                    operation="Stress-90 evidence collection rollover",
                )
                if fresh_account.trading_day != current_day:
                    raise RuntimeError(
                        "Stress-90 evidence collection rollover account/CTP day mismatch"
                    )
                snapshot = live_broker.refresh_contract_catalog(
                    timeout_seconds=max(0.001, float(config.metadata_timeout_seconds))
                )
                if snapshot.trading_day != current_day:
                    raise RuntimeError(
                        "Stress-90 evidence collection rollover catalog day mismatch"
                    )
                catalog = live_broker.get_contract_catalog()
                next_subscriptions = _stress90_evidence_collection_catalog(
                    config.directional,
                    catalog,
                    current_day,
                )
                aggregator.refresh_contract_catalog(current_day, catalog)
                new_contracts = tuple(
                    item
                    for item in next_subscriptions
                    if (item.symbol, item.exchange) not in subscribed
                )
                for contract in new_contracts:
                    broker.subscribe(contract.symbol, contract.exchange)
                supported_new = tuple(
                    item.symbol
                    for item in new_contracts
                    if item.product.upper() in STRESS90_POLICY.oi_products
                )
                if supported_new:
                    aggregator.note_contract_subscriptions(current_day, supported_new)
                subscribed.update((item.symbol, item.exchange) for item in new_contracts)
                catalog_by_symbol = {item.symbol: item for item in next_subscriptions}
                trading_day = current_day
                aggregator.checkpoint()
            error = broker.health_error()
            if error:
                raise RuntimeError(f"Stress-90 evidence collection Broker unhealthy: {error}")
            if args.once:
                break
            if not events:
                time.sleep(interval)

        audit.record(
            "stress90_raw_evidence_collection_stopped",
            {
                "trading_day": trading_day,
                "batches": batches,
                "orders_sent": 0,
            },
        )
        print(
            json.dumps(
                {
                    "collecting": False,
                    "trading_day": trading_day,
                    "batches": batches,
                    "orders_sent": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        if started:
            assert broker is not None
            try:
                broker.set_raw_tick_observer(None)
                if aggregator is not None:
                    aggregator.checkpoint()
                if activity_tracker is not None:
                    activity_tracker.checkpoint()
            finally:
                broker.stop()
        lease.release()


def _stress90_risk_overlay_reactivation_required(config, args) -> bool:
    from .stress90_risk_overlay import stress90_risk_overlay_digest

    paths = _stress90_lifecycle_paths(
        config,
        args.runtime_dir,
        shadow_account=bool(getattr(args, "shadow_account", False)),
    )
    record = StateStore(paths["state"]).load_record()
    if record is None:
        return False
    marker = record.state.strategy_states.get("directional_policy_identity")
    return bool(
        isinstance(marker, dict)
        and marker.get("policy_id") == "stress90"
        and marker.get("risk_overlay_digest")
        != stress90_risk_overlay_digest(config.directional, config.risk)
    )


def _run_stress90_risk_overlay_reactivation(config, args) -> int:
    from .broker.ctp import CtpBroker
    from .directional_policy_activation import rebind_stress90_risk_overlay_identity
    from .directional_stress90_execution import Stress90ExecutionIntentStore
    from .directional_stress90_state import Stress90PolicyStateStore, Stress90SeedStore
    from .journal import AuditJournal
    from .reconcile import compare_positions
    from .runtime_lease import AccountExclusiveRuntimeLease
    from .stress90_activation_permit import Stress90ActivationPermitStore
    from .stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
        require_matching_stress90_lifecycle_account_evidence,
    )
    from .stress90_risk_overlay import stress90_risk_overlay_digest

    operation_nonce = _require_lifecycle_operation_nonce(args)
    shadow_account = bool(getattr(args, "shadow_account", False))
    paths = _stress90_lifecycle_paths(
        config,
        args.runtime_dir,
        shadow_account=shadow_account,
    )
    store = StateStore(paths["state"])
    policy_store = Stress90PolicyStateStore(paths["runtime"] / "stress90_policy_state.json")
    seed_store = Stress90SeedStore(paths["runtime"] / "stress90_bootstrap_seed.json")
    lifecycle_store = Stress90LifecycleTransactionStore(
        paths["runtime"] / "stress90_lifecycle_transaction.json"
    )
    live_broker = CtpBroker(config.ctp)
    broker = (
        _build_persistent_shadow_broker(
            config,
            live_broker,
            paths["runtime"] / "shadow_broker_state.json",
        )
        if shadow_account
        else live_broker
    )
    raw_account_identity = broker.get_account_identity_digest()
    if (
        not isinstance(raw_account_identity, str)
        or re.fullmatch(r"[0-9a-f]{64}", raw_account_identity) is None
    ):
        raise RuntimeError("risk-overlay reactivation Broker account identity is invalid")
    account_identity = raw_account_identity
    lease = AccountExclusiveRuntimeLease(
        paths["runtime"], account_identity, role="stress90-risk-overlay-reactivation"
    )
    lease.acquire()
    try:
        state_record = store.load_required_record()
        state = state_record.state
        policy_record = policy_store.load_required_record()
        seed = seed_store.load_required()
        if policy_record.state.bootstrap_seed_digest != seed.seed_digest:
            raise RuntimeError("risk-overlay reactivation seed/policy identity mismatch")
        if (
            policy_record.state.live_account_identity_digest != account_identity
            or policy_record.state.live_account_epoch is None
        ):
            raise RuntimeError("risk-overlay reactivation account lineage mismatch")
        current_digest = stress90_risk_overlay_digest(config.directional, config.risk)
        marker = state.strategy_states.get("directional_policy_identity")
        if not isinstance(marker, dict) or marker.get("policy_id") != "stress90":
            raise RuntimeError("risk-overlay reactivation requires activated Stress-90 identity")
        if marker.get("risk_overlay_digest") == current_digest:
            raise RuntimeError("Stress-90 risk overlay is already bound to current configuration")
        _require_stress90_account_runtime_registry_binding(
            config,
            runtime_dir=paths["runtime"],
            broker=broker,
            policy_state=policy_record.state,
        )
        _require_stress90_order_journal_full_audit(paths["runtime"])
        _configure_stress90_lifecycle_order_journal(broker, paths["runtime"])
        _seed_state_aware_ctp_broker(
            live_broker,
            None if shadow_account else state,
            reject_ambiguous=not shadow_account,
        )
        broker.start()
    except BaseException:
        try:
            broker.stop()
        finally:
            lease.release()
        raise
    try:
        _wait_until_ready(broker, args.startup_timeout)
        wait_for_fresh_snapshot(broker, args.snapshot_wait)
        mechanical = _require_lifecycle_mechanical_snapshot(
            broker,
            runtime_dir=paths["runtime"],
            timeout_seconds=max(0.1, float(args.snapshot_wait)),
        )
        account = mechanical.account
        trading_day = mechanical.trading_day
        positions = list(mechanical.positions)
        active_orders = list(mechanical.active_orders)
        _require_verified_lifecycle_account_snapshot(
            account, operation="Stress-90 risk-overlay reactivation"
        )
        local_positions = store.positions_from_state(state)
        reconciliation = compare_positions(local_positions, positions)
        _require_lifecycle_resume_safety(
            state,
            broker_positions=positions,
            local_positions=local_positions,
            active_orders=active_orders,
            reconciliation_matched=reconciliation.matched,
            operation="Stress-90 risk-overlay reactivation",
        )
        if (
            trading_day != state.trading_day
            or state.last_account_trading_day != trading_day
            or account.trading_day != trading_day
            or float(account.equity) != float(state.last_account_equity)
            or float(account.deposit) != float(state.last_account_deposit)
            or float(account.withdrawal) != float(state.last_account_withdrawal)
            or account.settlement_id != state.last_account_settlement_id
        ):
            raise RuntimeError("risk-overlay reactivation fresh account snapshot changed")
        intent_record = Stress90ExecutionIntentStore(
            paths["runtime"] / "stress90_execution_intent.json"
        ).load_record()
        if (
            intent_record is not None
            and not intent_record.retired
            and intent_record.intent.target_trading_day >= trading_day
        ):
            raise RuntimeError(
                "risk-overlay reactivation is blocked by a current/future execution intent; converge or retire it under existing semantics first"
            )
        target = rebind_stress90_risk_overlay_identity(
            state,
            risk_overlay_digest=current_digest,
            operator_reason=args.operator_reason,
        )
        existing = lifecycle_store.load()
        if existing is not None and existing.status == "prepared":
            if (
                existing.operation != "risk_overlay_reactivation"
                or existing.operation_nonce != operation_nonce
                or existing.account_identity_digest != account_identity
                or existing.trading_day != trading_day
            ):
                raise Stress90LifecycleTransactionError(
                    "pending lifecycle transaction does not match risk-overlay reactivation"
                )
            require_matching_stress90_lifecycle_account_evidence(
                existing, account, account_identity_digest=account_identity
            )
            _require_lifecycle_operator_reason(existing, args.operator_reason)
            pending = existing
        else:
            pending = None
        _require_lifecycle_mechanical_snapshot_current(broker, mechanical)
        Stress90ActivationPermitStore(
            paths["runtime"] / "stress90_activation_permit.json"
        ).invalidate("Stress-90 risk overlay configuration changed")

        def prepare():
            if pending is not None:
                return pending
            return lifecycle_store.begin(
                operation="risk_overlay_reactivation",
                generic_source=state_record,
                policy_source=policy_record,
                generic_target=target,
                policy_target=policy_record.state,
                trading_day=trading_day,
                account_identity_digest=account_identity,
                account_snapshot=account,
                operation_nonce=operation_nonce,
                operator_reason=args.operator_reason,
            )

        completed = _commit_stress90_lifecycle_under_broker_fence(
            broker,
            transaction_store=lifecycle_store,
            generic_store=store,
            policy_store=policy_store,
            prepare_transaction=prepare,
            apply_registry_transition=lambda transaction: (
                _apply_stress90_account_runtime_registry_transition(
                    config,
                    runtime_dir=paths["runtime"],
                    lifecycle_transaction=transaction,
                )
            ),
            apply_evidence_transition=lambda transaction: (
                _apply_stress90_trading_day_evidence_transition(
                    config,
                    runtime_dir=paths["runtime"],
                    lifecycle_transaction=transaction,
                )
            ),
            precommit_check=lambda: _require_lifecycle_mechanical_snapshot_current(
                broker, mechanical
            ),
        )
        _record_lifecycle_completion_once(
            AuditJournal(paths["journal"]),
            "stress90_risk_overlay_reactivation_completed",
            completed.transaction_id,
            {
                "trading_day": trading_day,
                "operation_nonce": completed.operation_nonce,
                "account_identity_digest": account_identity,
                "risk_overlay_digest": current_digest,
                "kill_switch_remains_active": True,
            },
        )
        print(
            json.dumps(
                {
                    "reactivated": True,
                    "risk_overlay_only": True,
                    "risk_overlay_digest": current_digest,
                    "runtime_mode": RuntimeMode.HALTED.value,
                    "kill_switch": True,
                    "orders_sent": 0,
                    "cancels_sent": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        broker.stop()
        lease.release()


def _run_stress90_activate(config, args) -> int:
    """Explicitly bind generic runtime state to one immutable bootstrap identity."""

    from .broker.ctp import CtpBroker
    from .directional_policy_activation import (
        STRESS90_ACTIVATION_CONFIRMATION,
        activate_stress90_policy,
        reactivate_stress90_policy,
    )
    from .directional_stress90_state import (
        REBASE_CONFIRMATION,
        Stress90PolicyStateStore,
        Stress90SeedStore,
        bind_stress90_account_identity,
        rebase_stress90_account,
    )
    from .journal import AuditJournal
    from .reconcile import compare_positions
    from .stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
        derive_stress90_account_epoch,
        require_matching_stress90_lifecycle_account_evidence,
        stress90_lifecycle_account_transition,
    )

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
    operation_nonce = _require_lifecycle_operation_nonce(args)
    if _stress90_risk_overlay_reactivation_required(config, args):
        return _run_stress90_risk_overlay_reactivation(config, args)
    shadow_account = bool(getattr(args, "shadow_account", False))
    paths = _stress90_lifecycle_paths(
        config,
        args.runtime_dir,
        shadow_account=shadow_account,
    )
    seed_store = Stress90SeedStore(paths["runtime"] / "stress90_bootstrap_seed.json")
    policy_store = Stress90PolicyStateStore(paths["runtime"] / "stress90_policy_state.json")
    store = StateStore(paths["state"])
    lifecycle_store = Stress90LifecycleTransactionStore(
        paths["runtime"] / "stress90_lifecycle_transaction.json"
    )
    # Reject an impossible current/.prev layout before even constructing a CTP
    # object.  This checks only file presence; the authoritative payload is
    # decoded later while holding the account-exclusive lease.
    store._require_fresh_or_current()
    live_broker = CtpBroker(config.ctp)
    if shadow_account:
        expected_shadow = Path(config.state_path).parent / "shadow"
        if Path(paths["runtime"]).resolve() != expected_shadow.resolve():
            raise RuntimeError("Shadow activation requires --runtime-dir runtime/shadow")
        broker = _build_persistent_shadow_broker(
            config,
            live_broker,
            paths["runtime"] / "shadow_broker_state.json",
        )
    else:
        broker = live_broker
    from .runtime_lease import AccountExclusiveRuntimeLease

    lease = AccountExclusiveRuntimeLease(
        paths["runtime"],
        broker.get_account_identity_digest(),
        role="stress90-activate",
    )
    lease.acquire()
    try:
        # All files that authorize this lifecycle mutation are first read while
        # the account-exclusive lease is held; pre-lease observations must not
        # influence a write target.
        _require_stress90_order_journal_full_audit(paths["runtime"])
        seed = seed_store.load_required()
        policy_record = policy_store.load_required_record()
        policy_state = policy_record.state
        if policy_state.bootstrap_seed_digest != seed.seed_digest:
            raise RuntimeError("Stress-90 activation seed/policy-state identity mismatch")
        generic_record = store.load_record()
        existed = generic_record is not None
        state = RuntimeState() if generic_record is None else generic_record.state
        existing_lifecycle = lifecycle_store.load()
        source_marker = state.strategy_states.get("directional_policy_identity")
        coordinator_reactivation = bool(
            existing_lifecycle is not None
            and existing_lifecycle.operation == "reactivation"
            and existing_lifecycle.operation_nonce == operation_nonce
        )
        reactivating = bool(
            coordinator_reactivation
            or (
                isinstance(source_marker, dict)
                and source_marker.get("policy_id") == "execution_aligned"
                and {
                    "migrated_from_policy_id",
                    "migrated_from_policy_definition_digest",
                }.intersection(source_marker)
            )
        )
        lifecycle_operation = "reactivation" if reactivating else "activation"
        if reactivating and (
            not bool(getattr(args, "confirm_rebase", False))
            or os.getenv("AFUTURE_STRESS90_REBASE_ACK") != REBASE_CONFIRMATION
        ):
            raise RuntimeError(
                "Stress-90 reactivation requires --confirm-rebase and "
                "AFUTURE_STRESS90_REBASE_ACK=RESET_STRESS90_ACCOUNT_PATH"
            )
        _configure_stress90_lifecycle_order_journal(broker, paths["runtime"])
        _seed_state_aware_ctp_broker(
            live_broker,
            state if existed else None,
            reject_ambiguous=not shadow_account,
        )
        broker.start()
    except BaseException:
        try:
            broker.stop()
        finally:
            lease.release()
        raise
    try:
        _wait_until_ready(broker, args.startup_timeout)
        wait_for_fresh_snapshot(broker, args.snapshot_wait)
        mechanical = _require_lifecycle_mechanical_snapshot(
            broker,
            runtime_dir=paths["runtime"],
            timeout_seconds=max(0.1, float(args.snapshot_wait)),
        )
        account = mechanical.account
        trading_day = mechanical.trading_day
        positions = list(mechanical.positions)
        active_orders = list(mechanical.active_orders)
        _require_verified_lifecycle_account_snapshot(
            account,
            operation=f"Stress-90 {lifecycle_operation}",
        )
        if not shadow_account and not (
            existing_lifecycle is not None and existing_lifecycle.status == "prepared"
        ):
            _checkpoint_ctp_trading_day(config, live_broker, trading_day)
        if account.trading_day != trading_day:
            raise RuntimeError("Stress-90 activation account/CTP trading day mismatch")
        _require_lifecycle_trading_day_not_backward(
            trading_day,
            state=state,
            policy_state=policy_state,
            operation=f"Stress-90 {lifecycle_operation}",
        )
        account_identity_digest = broker.get_account_identity_digest()
        persisted_state = state
        state = _adopt_stress90_lifecycle_crash_fills(
            config,
            broker,
            runtime_dir=paths["runtime"],
            state=state,
            broker_positions=positions,
        )
        state = _require_no_unpersisted_lifecycle_crash_fill_adoption(
            persisted_state,
            state,
            pending_lifecycle=existing_lifecycle,
            persisted_record=generic_record,
            runtime_dir=paths["runtime"],
            consumer_operation_nonce=operation_nonce,
        )
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
        account_transition = stress90_lifecycle_account_transition(
            operation=lifecycle_operation,
            generic_source=state if existed else None,
            account_snapshot=account,
            account_switched=False,
        )
        state = replace(
            state,
            kill_switch=True,
            kill_reason=(
                "Stress-90 reactivated; doctor/Shadow gates remain required"
                if reactivating
                else "Stress-90 activated; doctor/Shadow gates remain required"
            ),
            runtime_mode=RuntimeMode.HALTED.value,
            reconciled=True,
            metadata_verified=False,
            trading_day=trading_day,
            day_start_equity=account_transition.day_start_equity,
            equity_high_watermark=account_transition.equity_high_watermark,
            last_account_equity=float(account.equity),
            last_account_trading_day=trading_day,
            last_account_deposit=float(account.deposit),
            last_account_withdrawal=float(account.withdrawal),
            last_account_cash_flow_verified=bool(account.cash_flow_verified),
            last_account_settlement_id=(
                int(account.settlement_id) if account.settlement_id is not None else -1
            ),
            recent_daily_returns=([] if reactivating else state.recent_daily_returns),
        )
        from .stress90_risk_overlay import stress90_risk_overlay_digest

        lifecycle_gates = {
            "broker_flat": not any(not position.empty for position in positions),
            "local_flat": not any(not position.empty for position in local_positions),
            "no_active_orders": not active_orders,
            "reconciled": reconciliation.matched,
            "bootstrap_seed_digest": seed.seed_digest,
            "account_identity_digest": account_identity_digest,
            "risk_overlay_digest": stress90_risk_overlay_digest(config.directional, config.risk),
            "operator_reason": args.operator_reason,
        }
        if coordinator_reactivation:
            activated = existing_lifecycle.generic_target  # type: ignore[union-attr]
        elif reactivating:
            activated = reactivate_stress90_policy(
                state,
                **lifecycle_gates,
                activation_confirmation=os.environ["AFUTURE_STRESS90_ACTIVATION_ACK"],
                rebase_confirmation=os.environ["AFUTURE_STRESS90_REBASE_ACK"],
            )
        else:
            activated = activate_stress90_policy(
                state,
                **lifecycle_gates,
                strong_confirmation=os.environ["AFUTURE_STRESS90_ACTIVATION_ACK"],
            )
        journal = AuditJournal(paths["journal"])
        if (
            existing_lifecycle is not None
            and existing_lifecycle.operation == lifecycle_operation
            and existing_lifecycle.operation_nonce == operation_nonce
            and existing_lifecycle.trading_day == trading_day
            and existing_lifecycle.account_identity_digest == account_identity_digest
        ):
            _require_lifecycle_resume_safety(
                state,
                broker_positions=positions,
                local_positions=local_positions,
                active_orders=active_orders,
                reconciliation_matched=reconciliation.matched,
                operation=f"Stress-90 {lifecycle_operation}",
            )
            require_matching_stress90_lifecycle_account_evidence(
                existing_lifecycle,
                account,
                account_identity_digest=account_identity_digest,
            )
            _require_lifecycle_operator_reason(existing_lifecycle, args.operator_reason)
            pending_lifecycle = existing_lifecycle
        elif existing_lifecycle is not None and existing_lifecycle.status == "prepared":
            pending_lifecycle = existing_lifecycle
        else:
            pending_lifecycle = None
        if pending_lifecycle is not None:
            if (
                pending_lifecycle.operation != lifecycle_operation
                or pending_lifecycle.operation_nonce != operation_nonce
                or pending_lifecycle.trading_day != trading_day
                or pending_lifecycle.account_identity_digest != account_identity_digest
            ):
                raise Stress90LifecycleTransactionError(
                    "pending lifecycle transaction does not match this " + lifecycle_operation
                )
            _require_lifecycle_operator_reason(pending_lifecycle, args.operator_reason)
            _require_lifecycle_mechanical_snapshot_current(broker, mechanical)
            from .stress90_activation_permit import Stress90ActivationPermitStore

            Stress90ActivationPermitStore(
                paths["runtime"] / "stress90_activation_permit.json"
            ).invalidate(f"Stress-90 policy {lifecycle_operation} changed bound runtime identity")

            def prepare_pending_activation():
                if reactivating:
                    _retire_stress90_execution_intent_for_reactivation(
                        runtime_dir=paths["runtime"],
                        lifecycle_transaction=pending_lifecycle,
                        broker_flat=not any(not position.empty for position in positions),
                        local_flat=not any(not position.empty for position in local_positions),
                        no_active_orders=not active_orders,
                        reconciled=reconciliation.matched,
                        activation_confirmation=os.environ["AFUTURE_STRESS90_ACTIVATION_ACK"],
                        rebase_confirmation=os.environ["AFUTURE_STRESS90_REBASE_ACK"],
                    )
                return pending_lifecycle

            completed = _commit_stress90_lifecycle_under_broker_fence(
                broker,
                transaction_store=lifecycle_store,
                generic_store=store,
                policy_store=policy_store,
                prepare_transaction=prepare_pending_activation,
                apply_registry_transition=lambda transaction: (
                    _apply_stress90_account_runtime_registry_transition(
                        config,
                        runtime_dir=paths["runtime"],
                        lifecycle_transaction=transaction,
                    )
                ),
                apply_evidence_transition=lambda transaction: (
                    _apply_stress90_trading_day_evidence_transition(
                        config,
                        runtime_dir=paths["runtime"],
                        lifecycle_transaction=transaction,
                    )
                ),
                precommit_check=lambda: _require_lifecycle_mechanical_snapshot_current(
                    broker, mechanical
                ),
            )
            _record_lifecycle_completion_once(
                journal,
                (
                    "stress90_policy_reactivation_completed"
                    if reactivating
                    else "stress90_policy_activation_completed"
                ),
                completed.transaction_id,
                {
                    "trading_day": trading_day,
                    "bootstrap_seed_digest": seed.seed_digest,
                    "operator_reason": completed.operator_reason,
                    "kill_switch_remains_active": True,
                    "resumed_transaction_id": completed.transaction_id,
                },
            )
            print(
                json.dumps(
                    {
                        "activated": not reactivating,
                        "reactivated": reactivating,
                        "resumed": True,
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
        journal.record(
            (
                "stress90_policy_reactivation_prepared"
                if reactivating
                else "stress90_policy_activation_prepared"
            ),
            {
                "trading_day": trading_day,
                "bootstrap_seed_digest": seed.seed_digest,
                "operator_reason": args.operator_reason,
                "runtime_state_preexisting": existed,
            },
        )
        from .stress90_activation_permit import Stress90ActivationPermitStore

        _require_lifecycle_mechanical_snapshot_current(broker, mechanical)
        Stress90ActivationPermitStore(
            paths["runtime"] / "stress90_activation_permit.json"
        ).invalidate(f"Stress-90 policy {lifecycle_operation} changed bound runtime identity")
        policy_record = policy_store.load_required_record()
        account_epoch = derive_stress90_account_epoch(
            operation=lifecycle_operation,
            operation_nonce=operation_nonce,
            policy_source_checksum=policy_record.checksum,
            account_identity_digest=account_identity_digest,
            trading_day=trading_day,
        )
        if reactivating:
            bound_policy, _reactivation_audit = rebase_stress90_account(
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
                account_identity_digest=account_identity_digest,
                account_epoch=account_epoch,
            )
        else:
            bound_policy = bind_stress90_account_identity(
                policy_record.state,
                account_identity_digest,
                account_epoch=account_epoch,
            )
            if bound_policy.live_inception_day is None:
                bound_policy = replace(
                    bound_policy,
                    live_inception_day=trading_day,
                    live_inception_equity=float(account.equity),
                )

        def prepare_fresh_activation():
            prepared = lifecycle_store.begin(
                operation=lifecycle_operation,
                generic_source=generic_record,
                policy_source=policy_record,
                generic_target=activated,
                policy_target=bound_policy,
                trading_day=trading_day,
                account_identity_digest=account_identity_digest,
                account_snapshot=account,
                operation_nonce=operation_nonce,
                operator_reason=args.operator_reason,
            )
            if reactivating:
                _retire_stress90_execution_intent_for_reactivation(
                    runtime_dir=paths["runtime"],
                    lifecycle_transaction=prepared,
                    broker_flat=not any(not position.empty for position in positions),
                    local_flat=not any(not position.empty for position in local_positions),
                    no_active_orders=not active_orders,
                    reconciled=reconciliation.matched,
                    activation_confirmation=os.environ["AFUTURE_STRESS90_ACTIVATION_ACK"],
                    rebase_confirmation=os.environ["AFUTURE_STRESS90_REBASE_ACK"],
                )
            return prepared

        completed = _commit_stress90_lifecycle_under_broker_fence(
            broker,
            transaction_store=lifecycle_store,
            generic_store=store,
            policy_store=policy_store,
            prepare_transaction=prepare_fresh_activation,
            apply_registry_transition=lambda transaction: (
                _apply_stress90_account_runtime_registry_transition(
                    config,
                    runtime_dir=paths["runtime"],
                    lifecycle_transaction=transaction,
                )
            ),
            apply_evidence_transition=lambda transaction: (
                _apply_stress90_trading_day_evidence_transition(
                    config,
                    runtime_dir=paths["runtime"],
                    lifecycle_transaction=transaction,
                )
            ),
            precommit_check=lambda: _require_lifecycle_mechanical_snapshot_current(
                broker, mechanical
            ),
        )
        _record_lifecycle_completion_once(
            journal,
            (
                "stress90_policy_reactivation_completed"
                if reactivating
                else "stress90_policy_activation_completed"
            ),
            completed.transaction_id,
            {
                "trading_day": trading_day,
                "bootstrap_seed_digest": seed.seed_digest,
                "operator_reason": completed.operator_reason,
                "kill_switch_remains_active": True,
            },
        )
        print(
            json.dumps(
                {
                    "activated": not reactivating,
                    "reactivated": reactivating,
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
        lease.release()


def _seal_stress90_account_switch_order_epoch(
    *,
    runtime_dir: Path,
    lifecycle_transaction,
    trading_day: str,
    operator_reason: str,
    broker_flat: bool,
    local_flat: bool,
    no_active_orders: bool,
    reconciled: bool,
) -> None:
    """Bind a recoverable CTP order-journal epoch to the lifecycle transaction."""

    source_identity = lifecycle_transaction.source_account_identity_digest
    target_identity = lifecycle_transaction.account_identity_digest
    if source_identity == target_identity:
        return
    from .broker.ctp_order_journal import (
        CTP_ORDER_JOURNAL_EPOCH_CONFIRMATION,
        CtpOrderSubmissionJournal,
    )

    CtpOrderSubmissionJournal(runtime_dir / "stress90_ctp_orders.json").seal_epoch(
        transaction_id=lifecycle_transaction.transaction_id,
        source_account_identity_digest=source_identity,
        target_account_identity_digest=target_identity,
        trading_day=trading_day,
        operator_reason=operator_reason,
        halted=True,
        broker_flat=broker_flat,
        local_flat=local_flat,
        no_active_orders=no_active_orders,
        reconciled=reconciled,
        strong_confirmation=CTP_ORDER_JOURNAL_EPOCH_CONFIRMATION,
    )


def _retire_stress90_execution_intent_for_account_rebase(
    *,
    runtime_dir: Path,
    lifecycle_transaction,
    broker_flat: bool,
    local_flat: bool,
    no_active_orders: bool,
    reconciled: bool,
    strong_confirmation: str,
) -> None:
    """Fence stale execution authority before either lifecycle state can advance."""

    from .directional_stress90_execution import Stress90ExecutionIntentStore

    if lifecycle_transaction.operation != "account_rebase":
        raise RuntimeError("execution intent retirement requires an account rebase transaction")
    Stress90ExecutionIntentStore(
        runtime_dir / "stress90_execution_intent.json"
    ).retire_for_account_rebase(
        transaction_id=lifecycle_transaction.transaction_id,
        operation_nonce=lifecycle_transaction.operation_nonce,
        policy_source_checksum=lifecycle_transaction.policy_source_checksum,
        source_account_identity_digest=(lifecycle_transaction.source_account_identity_digest),
        source_account_epoch=lifecycle_transaction.source_account_epoch,
        target_account_identity_digest=lifecycle_transaction.account_identity_digest,
        target_account_epoch=(lifecycle_transaction.policy_target.live_account_epoch or ""),
        trading_day=lifecycle_transaction.trading_day,
        halted=True,
        broker_flat=broker_flat,
        local_flat=local_flat,
        no_active_orders=no_active_orders,
        reconciled=reconciled,
        strong_confirmation=strong_confirmation,
    )


def _retire_stress90_execution_intent_for_reactivation(
    *,
    runtime_dir: Path,
    lifecycle_transaction,
    broker_flat: bool,
    local_flat: bool,
    no_active_orders: bool,
    reconciled: bool,
    activation_confirmation: str,
    rebase_confirmation: str,
) -> None:
    """Fence the old Stress-90 account epoch before reactivation state can advance."""

    from .directional_stress90_execution import Stress90ExecutionIntentStore

    if lifecycle_transaction.operation != "reactivation":
        raise RuntimeError("execution intent retirement requires a reactivation transaction")
    Stress90ExecutionIntentStore(
        runtime_dir / "stress90_execution_intent.json"
    ).retire_for_reactivation(
        transaction_id=lifecycle_transaction.transaction_id,
        operation_nonce=lifecycle_transaction.operation_nonce,
        policy_source_checksum=lifecycle_transaction.policy_source_checksum,
        source_account_identity_digest=(lifecycle_transaction.source_account_identity_digest),
        source_account_epoch=lifecycle_transaction.source_account_epoch,
        target_account_identity_digest=lifecycle_transaction.account_identity_digest,
        target_account_epoch=(lifecycle_transaction.policy_target.live_account_epoch or ""),
        trading_day=lifecycle_transaction.trading_day,
        halted=True,
        broker_flat=broker_flat,
        local_flat=local_flat,
        no_active_orders=no_active_orders,
        reconciled=reconciled,
        activation_confirmation=activation_confirmation,
        rebase_confirmation=rebase_confirmation,
    )


def _require_stress90_order_journal_rollover_cutoff(
    *,
    current_trading_day: str,
    session_evidence,
    entries,
) -> None:
    """Never seal identities that the current private CTP topic can replay."""

    if tuple(getattr(session_evidence, "orders", ())) or tuple(
        getattr(session_evidence, "trades", ())
    ):
        raise RuntimeError("Stress-90 order journal rollover requires an empty current CTP session")
    days = tuple(str(getattr(entry, "target_trading_day", "")) for entry in entries)
    if any(not day or day >= current_trading_day for day in days):
        raise RuntimeError(
            "Stress-90 order journal rollover entries must be strictly prior to current CTP day"
        )


def _run_stress90_order_journal_rollover(config, args) -> int:
    """Seal a capacity epoch only while Broker and local truth are flat and reconciled."""

    from .broker.ctp import CtpBroker
    from .broker.ctp_order_journal import (
        CTP_ORDER_JOURNAL_EPOCH_CONFIRMATION,
        CtpOrderSubmissionJournal,
    )
    from .directional_policy_activation import require_directional_policy_identity
    from .directional_stress90_policy import STRESS90_POLICY
    from .directional_stress90_state import Stress90SeedStore
    from .journal import AuditJournal
    from .reconcile import compare_positions
    from .runtime_lease import AccountExclusiveRuntimeLease
    from .stress90_activation_permit import Stress90ActivationPermitStore
    from .stress90_lifecycle_transaction import (
        require_no_pending_stress90_lifecycle_transaction,
    )

    _validate_stress90_lifecycle_config(config)
    _require_production_confirmation(config, args)
    operation_id = str(args.operation_id).strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", operation_id) is None:
        raise RuntimeError("Stress-90 order journal rollover operation id must be 64-hex")
    if (
        not args.confirm_rollover
        or os.getenv("AFUTURE_STRESS90_ORDER_EPOCH_ACK") != CTP_ORDER_JOURNAL_EPOCH_CONFIRMATION
    ):
        raise RuntimeError(
            "Stress-90 order journal rollover requires --confirm-rollover and "
            "AFUTURE_STRESS90_ORDER_EPOCH_ACK=" + CTP_ORDER_JOURNAL_EPOCH_CONFIRMATION
        )
    paths = _stress90_lifecycle_paths(config, args.runtime_dir)
    store = StateStore(paths["state"])
    broker = CtpBroker(config.ctp)
    lease = AccountExclusiveRuntimeLease(
        paths["runtime"],
        broker.get_account_identity_digest(),
        role="stress90-order-journal-rollover",
    )
    lease.acquire()
    try:
        require_no_pending_stress90_lifecycle_transaction(paths["runtime"])
        state_record = store.load_record()
        if state_record is None:
            raise RuntimeError("Stress-90 order journal rollover requires runtime state")
        state = state_record.state
        if state.runtime_mode != RuntimeMode.HALTED.value or not state.kill_switch:
            raise RuntimeError("Stress-90 order journal rollover requires HALTED kill switch")
        seed = Stress90SeedStore(paths["runtime"] / "stress90_bootstrap_seed.json").load_required()
        require_directional_policy_identity(
            state,
            policy_id=STRESS90_POLICY.policy_id,
            policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
            products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
            bootstrap_seed_digest=seed.seed_digest,
            account_identity_digest=broker.get_account_identity_digest(),
        )
        _configure_stress90_lifecycle_order_journal(broker, paths["runtime"])
        _seed_state_aware_ctp_broker(broker, state, reject_ambiguous=True)
        broker.start()
    except BaseException:
        try:
            broker.stop()
        finally:
            lease.release()
        raise
    try:
        _wait_until_ready(broker, args.startup_timeout)
        wait_for_fresh_snapshot(broker, args.snapshot_wait)
        mechanical = _require_lifecycle_mechanical_snapshot(
            broker,
            runtime_dir=paths["runtime"],
            timeout_seconds=max(0.1, float(args.snapshot_wait)),
        )
        account = mechanical.account
        trading_day = mechanical.trading_day
        positions = list(mechanical.positions)
        active_orders = list(mechanical.active_orders)
        _require_verified_lifecycle_account_snapshot(
            account,
            operation="Stress-90 order journal rollover",
        )
        if account.trading_day != trading_day:
            raise RuntimeError("Stress-90 order journal rollover trading day mismatch")
        persisted_state = state
        state = _adopt_stress90_lifecycle_crash_fills(
            config,
            broker,
            runtime_dir=paths["runtime"],
            state=state,
            broker_positions=positions,
        )
        _require_no_unpersisted_lifecycle_crash_fill_adoption(
            persisted_state,
            state,
            pending_lifecycle=None,
            persisted_record=state_record,
            runtime_dir=paths["runtime"],
        )
        local_positions = store.positions_from_state(state)
        reconciliation = compare_positions(local_positions, positions)
        broker_flat = not any(not position.empty for position in positions)
        local_flat = not any(not position.empty for position in local_positions)
        no_active_orders = not active_orders
        if not (
            broker_flat
            and local_flat
            and no_active_orders
            and state.reconciled
            and reconciliation.matched
        ):
            raise RuntimeError(
                "Stress-90 order journal rollover requires flat Broker/local state, "
                "no active orders and completed reconciliation"
            )
        account_identity = broker.get_account_identity_digest()
        _require_lifecycle_mechanical_snapshot_current(broker, mechanical)
        order_journal = CtpOrderSubmissionJournal(paths["runtime"] / "stress90_ctp_orders.json")
        _require_stress90_order_journal_rollover_cutoff(
            current_trading_day=trading_day,
            session_evidence=mechanical.session_proof.evidence,
            entries=order_journal.load_all_entries(),
        )
        _checkpoint_ctp_trading_day(config, broker, trading_day)
        Stress90ActivationPermitStore(
            paths["runtime"] / "stress90_activation_permit.json"
        ).invalidate("Stress-90 CTP order journal epoch changed")
        manifest = order_journal.seal_epoch(
            transaction_id=operation_id,
            source_account_identity_digest=account_identity,
            target_account_identity_digest=account_identity,
            trading_day=trading_day,
            operator_reason=args.operator_reason,
            halted=True,
            broker_flat=broker_flat,
            local_flat=local_flat,
            no_active_orders=no_active_orders,
            reconciled=reconciliation.matched,
            strong_confirmation=CTP_ORDER_JOURNAL_EPOCH_CONFIRMATION,
        )
        order_journal.audit_epochs()
        if state.last_order_id or state.last_trade_id or state.recent_trade_ids:
            state = replace(
                state,
                last_order_id="",
                last_trade_id="",
                recent_trade_ids=[],
            )
            store.save(
                state,
                expected_sequence=state_record.sequence,
                expected_checksum=state_record.checksum,
            )
        AuditJournal(paths["journal"]).record(
            "stress90_ctp_order_journal_epoch_sealed",
            {
                "transaction_id": operation_id,
                "trading_day": trading_day,
                "operator_reason": args.operator_reason,
                "account_identity_digest": account_identity,
                "epoch_manifest_sequence": manifest.sequence,
                "epoch_manifest_digest": manifest.checksum,
                "sealed_epoch_count": len(manifest.sealed_epochs),
                "kill_switch_remains_active": True,
            },
        )
        print(
            json.dumps(
                {
                    "sealed": True,
                    "runtime_dir": str(paths["runtime"]),
                    "transaction_id": operation_id,
                    "trading_day": trading_day,
                    "epoch_manifest_digest": manifest.checksum,
                    "sealed_epoch_count": len(manifest.sealed_epochs),
                    "runtime_mode": RuntimeMode.HALTED.value,
                    "kill_switch": True,
                    "orders_sent": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        broker.stop()
        lease.release()


def _run_stress90_operator_roll_forward(config, args) -> int:
    """Advance one Stress-90 account day under the explicit operator trust model."""

    from hashlib import sha256
    from types import SimpleNamespace

    from .account_runtime_registry import AccountRuntimeRegistry
    from .broker.ctp import CtpBroker
    from .broker.ctp_order_journal import CtpOrderSubmissionJournal
    from .directional_activity import (
        DirectionalActivityStore,
        validate_directional_activity_snapshot,
    )
    from .directional_ohlc_cache import DirectionalOHLCCacheStore
    from .directional_policy_activation import require_directional_policy_identity
    from .directional_stress90_oi_runtime import Stress90OiEvidenceStore
    from .directional_stress90_policy import STRESS90_POLICY
    from .directional_stress90_state import Stress90PolicyStateStore, Stress90SeedStore
    from .journal import AuditJournal
    from .reconcile import compare_positions
    from .runtime_lease import AccountExclusiveRuntimeLease
    from .stress90_activation_permit import Stress90ActivationPermitStore
    from .stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
        apply_stress90_lifecycle_transaction,
        require_matching_stress90_lifecycle_account_evidence,
    )
    from .stress90_operator_continuity import (
        STRESS90_OPERATOR_CONTINUITY_CONFIRMATION,
        Stress90OperatorContinuityError,
        Stress90OperatorContinuityStore,
        build_stress90_operator_roll_forward_plan,
        load_stress90_operator_account_day_continuity_evidence,
    )
    from .trading_day_evidence import (
        TradingDayEvidenceStore,
        require_authoritative_trading_day_evidence,
    )

    _validate_stress90_lifecycle_config(config)
    if config.directional.account_continuity_mode != "operator_managed":
        raise ValueError(
            "stress90-operator-roll-forward requires "
            "directional.account_continuity_mode=operator_managed"
        )
    _require_production_confirmation(config, args)
    operation_nonce = _require_lifecycle_operation_nonce(args)
    reason = str(args.operator_reason)
    if not reason.strip() or reason != reason.strip() or len(reason) > 1_000:
        raise ValueError("operator continuity operator reason is invalid")
    if (
        not args.confirm_operator_continuity
        or os.getenv("AFUTURE_OPERATOR_CONTINUITY_ACK") != STRESS90_OPERATOR_CONTINUITY_CONFIRMATION
    ):
        raise RuntimeError(
            "operator continuity requires --confirm-operator-continuity and "
            "AFUTURE_OPERATOR_CONTINUITY_ACK=" + STRESS90_OPERATOR_CONTINUITY_CONFIRMATION
        )

    configured_state = Path(config.state_path)
    runtime_dir = configured_state.parent.resolve(strict=False)
    if runtime_dir == Path(runtime_dir.anchor):
        raise ValueError("operator continuity runtime path must not be a filesystem root")
    if configured_state.resolve(strict=False).parent != runtime_dir:
        raise ValueError("operator continuity state path is not bound to canonical runtime")

    state_store = StateStore(configured_state.resolve(strict=False))
    state_store._require_fresh_or_current()
    policy_store = Stress90PolicyStateStore(runtime_dir / "stress90_policy_state.json")
    seed_store = Stress90SeedStore(runtime_dir / "stress90_bootstrap_seed.json")
    lifecycle_store = Stress90LifecycleTransactionStore(
        runtime_dir / "stress90_lifecycle_transaction.json"
    )
    registry = AccountRuntimeRegistry(_stress90_account_registry_path(config))
    trading_day_store = TradingDayEvidenceStore(runtime_dir / "ctp_trading_day_evidence.json")
    operator_store = Stress90OperatorContinuityStore(
        runtime_dir / "stress90_operator_continuity.json"
    )
    # Validate surviving operator evidence before Broker construction.  This is read-only.
    preflight_operator_record = operator_store.load_record()
    broker = CtpBroker(config.ctp)
    account_identity = broker.get_account_identity_digest()
    lease = AccountExclusiveRuntimeLease(
        runtime_dir,
        account_identity,
        role="stress90-operator-roll-forward",
    )

    def canonical_positions(positions) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for position in positions:
            position.validate()
            rows.append(asdict(position))
        return sorted(rows, key=lambda row: (str(row["symbol"]), str(row["exchange"])))

    def position_reconciliation_digest(local_positions, broker_positions) -> str:
        payload = {
            "local": canonical_positions(local_positions),
            "broker": canonical_positions(broker_positions),
            "matched": True,
        }
        return sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

    def order_journal_digest() -> str:
        audit = CtpOrderSubmissionJournal(runtime_dir / "stress90_ctp_orders.json").audit_epochs()
        record = audit.current_record
        manifest = audit.manifest
        payload = {
            "current_sequence": 0 if record is None else record.sequence,
            "current_checksum": "" if record is None else record.checksum,
            "archive_head_checksum": (
                "" if record is None else (record.archive_head_checksum or "")
            ),
            "runtime_index_checksum": (
                "" if record is None else (record.runtime_index_checksum or "")
            ),
            "manifest_sequence": 0 if manifest is None else manifest.sequence,
            "manifest_checksum": "" if manifest is None else manifest.checksum,
            "sealed_epoch_count": audit.sealed_epoch_count,
            "sealed_entry_count": audit.sealed_entry_count,
            "sealed_fill_identity_count": audit.sealed_fill_identity_count,
        }
        return sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

    def semantic_source_trading_day_evidence(transaction, current_registry):
        current_tde = trading_day_store.load_required()
        source_day = transaction.account_day_continuity_source_day
        if (
            current_tde.trading_day == source_day
            and current_tde.account_binding_last_operation_id != transaction.operation_nonce
        ):
            return current_tde
        if (
            current_tde.trading_day == transaction.trading_day
            and current_tde.rebind_transaction_id == transaction.transaction_id
            and current_tde.account_binding_last_operation_id == transaction.operation_nonce
        ):
            if not trading_day_store.previous_path.exists():
                raise RuntimeError(
                    "operator continuity prepared retry source TradingDayEvidence is missing"
                )
            return TradingDayEvidenceStore(trading_day_store.previous_path).load_required()
        binding = current_registry.binding
        if (
            current_tde.trading_day == source_day
            and binding.last_operation_id == transaction.operation_nonce
        ):
            return current_tde
        raise RuntimeError(
            "operator continuity prepared retry TradingDayEvidence is not the exact source/target"
        )

    def source_registry_evidence_from_tde(source_tde, current_registry, transaction):
        binding = current_registry.binding
        current_is_source = bool(
            current_registry.binding_receipt_digest == source_tde.account_binding_receipt_digest
            and current_registry.registry_sequence == source_tde.registry_sequence
            and current_registry.registry_checksum == source_tde.registry_checksum
        )
        if current_is_source:
            return current_registry
        if (
            binding.account_identity_digest != source_tde.account_identity_digest
            or binding.account_epoch != source_tde.account_epoch
            or binding.canonical_runtime != source_tde.canonical_runtime
            or binding.runtime_identity_digest != source_tde.runtime_identity_digest
            or binding.last_operation_id != transaction.operation_nonce
            or not binding.operation_kinds
            or binding.operation_kinds[-1] != "operator_managed_continuity"
            or current_registry.binding_revision != source_tde.account_binding_revision + 1
            or len(binding.operation_history) < 2
            or binding.operation_history[-2] != source_tde.account_binding_last_operation_id
        ):
            raise RuntimeError(
                "operator continuity prepared retry registry does not extend exact source"
            )
        source_binding = SimpleNamespace(
            account_identity_digest=source_tde.account_identity_digest,
            account_epoch=source_tde.account_epoch,
            canonical_runtime=source_tde.canonical_runtime,
            runtime_identity_digest=source_tde.runtime_identity_digest,
        )
        return SimpleNamespace(
            binding=source_binding,
            binding_receipt_digest=source_tde.account_binding_receipt_digest,
            registry_sequence=source_tde.registry_sequence,
            registry_checksum=source_tde.registry_checksum,
        )

    lease.acquire()
    started = False
    try:
        state_record = state_store.load_required_record()
        state = state_record.state
        if state.runtime_mode != RuntimeMode.HALTED.value or not state.kill_switch:
            raise RuntimeError(
                "operator continuity requires generic runtime HALTED with kill switch=true"
            )
        seed = seed_store.load_required()
        policy_record = policy_store.load_required_record()
        policy_state = policy_record.state
        require_directional_policy_identity(
            state,
            policy_id=STRESS90_POLICY.policy_id,
            policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
            products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
            bootstrap_seed_digest=seed.seed_digest,
            account_identity_digest=account_identity,
        )
        if (
            policy_state.bootstrap_seed_digest != seed.seed_digest
            or policy_state.live_account_identity_digest != account_identity
            or policy_state.live_account_epoch is None
        ):
            raise RuntimeError("operator continuity Stress-90 policy account lineage mismatch")
        account_epoch = policy_state.live_account_epoch
        current_registry = registry.require_binding_evidence(
            account_identity, runtime_dir, account_epoch
        )
        existing_lifecycle = lifecycle_store.load()
        if existing_lifecycle is not None and existing_lifecycle.status == "prepared":
            if (
                existing_lifecycle.operation != "settlement_roll_forward"
                or existing_lifecycle.operation_nonce != operation_nonce
            ):
                raise RuntimeError(
                    "operator continuity is blocked by a different prepared lifecycle transaction"
                )
            _require_lifecycle_operator_reason(existing_lifecycle, reason)
        exact_committed_retry = bool(
            existing_lifecycle is not None
            and existing_lifecycle.status == "committed"
            and existing_lifecycle.operation == "settlement_roll_forward"
            and existing_lifecycle.operation_nonce == operation_nonce
        )
        operator_record = operator_store.load_record()
        if operator_record is None and preflight_operator_record is not None:
            raise RuntimeError("operator continuity evidence changed after preflight")
        prepared_registry_advance = bool(
            existing_lifecycle is not None
            and existing_lifecycle.status == "prepared"
            and current_registry.binding.last_operation_id == operation_nonce
            and current_registry.binding.operation_kinds
            and current_registry.binding.operation_kinds[-1] == "operator_managed_continuity"
        )
        if (
            operator_record is None
            and "operator_managed_continuity" in current_registry.binding.operation_kinds
            and not prepared_registry_advance
        ):
            raise RuntimeError(
                "operator continuity artifact is missing but registry proves prior operator continuity"
            )
        _require_stress90_order_journal_full_audit(runtime_dir)
        _configure_stress90_lifecycle_order_journal(broker, runtime_dir)
        _seed_state_aware_ctp_broker(broker, state, reject_ambiguous=True)
        broker.start()
        started = True

        _wait_until_ready(broker, args.startup_timeout)
        wait_for_fresh_snapshot(broker, args.snapshot_wait)
        mechanical = _require_lifecycle_mechanical_snapshot(
            broker,
            runtime_dir=runtime_dir,
            timeout_seconds=max(0.1, float(args.snapshot_wait)),
        )
        account = mechanical.account
        target_day = mechanical.trading_day
        broker_positions = list(mechanical.positions)
        active_orders = list(mechanical.active_orders)
        _require_verified_lifecycle_account_snapshot(
            account, operation="Stress-90 operator-managed roll-forward"
        )
        if account.trading_day != target_day:
            raise RuntimeError("operator continuity account/CTP trading day mismatch")
        if account_identity != broker.get_account_identity_digest():
            raise RuntimeError("operator continuity account identity changed after startup")
        if active_orders:
            raise RuntimeError(
                "operator continuity requires zero active orders; no cancellation is permitted"
            )
        if float(account.deposit) != 0.0 or float(account.withdrawal) != 0.0:
            raise RuntimeError(
                "operator continuity observed Deposit/Withdraw; use stress90-account-rebase"
            )
        local_positions = state_store.positions_from_state(state)
        reconciliation = compare_positions(local_positions, broker_positions)
        if not reconciliation.matched:
            raise RuntimeError(
                "operator continuity Broker/local position reconciliation failed: "
                + reconciliation.details
            )
        reconciliation_digest = position_reconciliation_digest(local_positions, broker_positions)
        journal_digest = order_journal_digest()
        session_digest = mechanical.session_proof.ownership_digest

        if exact_committed_retry:
            assert existing_lifecycle is not None
            require_matching_stress90_lifecycle_account_evidence(
                existing_lifecycle,
                account,
                account_identity_digest=account_identity,
            )
            _require_lifecycle_operator_reason(existing_lifecycle, reason)
            receipt = operator_store.require_current_binding(
                account_identity_digest=account_identity,
                account_epoch=account_epoch,
                canonical_runtime=runtime_dir,
                target_ctp_trading_day=target_day,
            )
            if (
                receipt.evidence.operation_id != operation_nonce
                or receipt.request_digest != existing_lifecycle.account_day_continuity_digest
                or receipt.evidence.session_ownership_digest != session_digest
                or receipt.evidence.ctp_order_journal_digest != journal_digest
                or receipt.evidence.position_reconciliation_digest != reconciliation_digest
            ):
                raise RuntimeError("operator continuity exact committed retry evidence changed")
            current_registry = registry.require_binding_evidence(
                account_identity, runtime_dir, account_epoch
            )
            current_tde = trading_day_store.load_required()
            from .stress90_operator_continuity import (
                require_stress90_operator_continuity_authority,
            )

            require_stress90_operator_continuity_authority(
                receipt,
                registry_evidence=current_registry,
                trading_day_evidence=current_tde,
            )
            apply_stress90_lifecycle_transaction(
                lifecycle_store,
                generic_store=state_store,
                policy_store=policy_store,
            )
            final_state = state_store.load_required_record().state
            if (
                final_state.runtime_mode != RuntimeMode.HALTED.value
                or not final_state.kill_switch
                or final_state.metadata_verified
            ):
                raise RuntimeError("operator continuity exact retry lost HALTED fail-closed state")
            print(
                json.dumps(
                    {
                        "operation": "stress90-operator-roll-forward",
                        "status": "committed",
                        "transaction_id": existing_lifecycle.transaction_id,
                        "operator_continuity_sequence": receipt.sequence,
                        "operator_continuity_checksum": receipt.checksum,
                        "source_ctp_trading_day": receipt.evidence.source_ctp_trading_day,
                        "target_ctp_trading_day": receipt.evidence.target_ctp_trading_day,
                        "runtime_mode": final_state.runtime_mode,
                        "kill_switch": True,
                        "metadata_verified": False,
                        "requires_doctor": True,
                        "requires_new_permit": True,
                        "external_activation_gates_completed": False,
                        "orders_sent": 0,
                        "cancels_sent": 0,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        receipt_for_operation = (
            operator_record
            if operator_record is not None
            and operator_record.evidence.operation_id == operation_nonce
            else None
        )
        plan = None
        if receipt_for_operation is None:
            if (
                existing_lifecycle is not None
                and existing_lifecycle.status == "prepared"
                and (
                    state_record.sequence != existing_lifecycle.generic_source_sequence
                    or state_record.checksum != existing_lifecycle.generic_source_checksum
                    or policy_record.sequence != existing_lifecycle.policy_source_sequence
                    or policy_record.checksum != existing_lifecycle.policy_source_checksum
                )
            ):
                raise RuntimeError(
                    "operator continuity prepared transaction advanced state before receipt"
                )
            transaction_for_source = existing_lifecycle
            if transaction_for_source is None or transaction_for_source.status != "prepared":
                current_tde = trading_day_store.load_required()
                require_authoritative_trading_day_evidence(
                    current_tde,
                    policy_state=policy_state,
                    registry=registry,
                    runtime_dir=runtime_dir,
                    lifecycle_transaction=existing_lifecycle,
                )
                source_tde = current_tde
                source_registry = current_registry
                source_day = state.trading_day
            else:
                source_tde = semantic_source_trading_day_evidence(
                    transaction_for_source, current_registry
                )
                source_registry = source_registry_evidence_from_tde(
                    source_tde, current_registry, transaction_for_source
                )
                source_day = transaction_for_source.account_day_continuity_source_day
            activity = DirectionalActivityStore(runtime_dir / "directional_activity.json").load()
            if activity is None:
                raise RuntimeError("operator continuity completed activity evidence is missing")
            validate_directional_activity_snapshot(activity)
            if activity.trading_day != source_day:
                raise RuntimeError(
                    "operator continuity completed activity day does not match source day"
                )
            market = load_stress90_operator_account_day_continuity_evidence(
                DirectionalOHLCCacheStore(runtime_dir / "directional_ohlc_cache.json"),
                Stress90OiEvidenceStore(runtime_dir / "stress90_oi_evidence.json"),
                completed_account_day=source_day,
                current_ctp_trading_day=target_day,
            )
            plan = build_stress90_operator_roll_forward_plan(
                operation_id=operation_nonce,
                operator_reason=reason,
                generic_source=state_record,
                policy_source=policy_record,
                source_trading_day_evidence=source_tde,
                source_registry_evidence=source_registry,
                runtime_dir=runtime_dir,
                account_identity_digest=account_identity,
                account_snapshot=account,
                active_order_count=len(active_orders),
                positions_reconciled=reconciliation.matched,
                position_reconciliation_digest=reconciliation_digest,
                session_ownership_digest=session_digest,
                ctp_order_journal_digest=journal_digest,
                activity_latest_completed_day=activity.trading_day,
                market_continuity=market,
            )
            request_digest = plan.request_digest
            continuity_evidence = plan.evidence
        else:
            request_digest = receipt_for_operation.request_digest
            continuity_evidence = receipt_for_operation.evidence
            if (
                continuity_evidence.operator_reason != reason
                or continuity_evidence.target_ctp_trading_day != target_day
                or continuity_evidence.account_identity_digest != account_identity
                or continuity_evidence.account_epoch != account_epoch
                or continuity_evidence.session_ownership_digest != session_digest
                or continuity_evidence.ctp_order_journal_digest != journal_digest
                or continuity_evidence.position_reconciliation_digest != reconciliation_digest
            ):
                raise RuntimeError("operator continuity prepared retry receipt evidence changed")

        if existing_lifecycle is not None and existing_lifecycle.status == "prepared":
            transaction = existing_lifecycle
            require_matching_stress90_lifecycle_account_evidence(
                transaction,
                account,
                account_identity_digest=account_identity,
            )
            if (
                transaction.trading_day != target_day
                or transaction.account_identity_digest != account_identity
                or transaction.source_account_epoch != account_epoch
                or transaction.policy_target.live_account_epoch != account_epoch
                or transaction.account_day_continuity_digest != request_digest
            ):
                raise Stress90LifecycleTransactionError(
                    "prepared lifecycle transaction does not match operator continuity request"
                )
            if plan is not None and (
                transaction.generic_target != plan.targets.generic_target
                or transaction.policy_target != plan.targets.policy_target
            ):
                raise Stress90LifecycleTransactionError(
                    "prepared lifecycle targets changed for operator continuity retry"
                )
        else:
            if plan is None:
                raise RuntimeError(
                    "operator continuity receipt exists without a matching prepared transaction"
                )

            def begin_transaction():
                return lifecycle_store.begin(
                    operation="settlement_roll_forward",
                    generic_source=state_record,
                    policy_source=policy_record,
                    generic_target=plan.targets.generic_target,
                    policy_target=plan.targets.policy_target,
                    trading_day=target_day,
                    account_identity_digest=account_identity,
                    account_snapshot=account,
                    operation_nonce=operation_nonce,
                    operator_reason=reason,
                    account_day_continuity_digest=request_digest,
                )

            transaction = None

        permit_store = Stress90ActivationPermitStore(
            runtime_dir / "stress90_activation_permit.json"
        )
        permit_store.invalidate(
            "operator-managed account continuity advanced; fresh Doctor permit required"
        )

        def prepare_transaction():
            if transaction is not None:
                return transaction
            return begin_transaction()

        def apply_registry_transition(lifecycle_transaction):
            registry.acknowledge_operator_continuity_operation(
                account_identity,
                runtime_dir,
                account_epoch,
                lifecycle_transaction.operation_nonce,
                request_digest,
            )

        def apply_evidence_transition(lifecycle_transaction):
            _apply_stress90_trading_day_evidence_transition(
                config,
                runtime_dir=runtime_dir,
                lifecycle_transaction=lifecycle_transaction,
            )
            target_registry = registry.require_binding_evidence(
                account_identity, runtime_dir, account_epoch
            )
            target_tde = trading_day_store.load_required()
            operator_store.save(
                continuity_evidence,
                target_registry_receipt_digest=(target_registry.binding_receipt_digest),
                target_trading_day_evidence_sequence=target_tde.sequence,
                target_trading_day_evidence_checksum=target_tde.checksum,
            )

        def precommit_check():
            if not lease.authorizes_technical_activation(account_identity, runtime_dir):
                raise RuntimeError(
                    "exact account/runtime lease is not held for operator continuity"
                )
            _require_lifecycle_mechanical_snapshot_current(broker, mechanical)
            _require_stress90_order_journal_full_audit(runtime_dir)
            if broker.get_trading_day() != target_day:
                raise RuntimeError("operator continuity CTP trading day changed before commit")
            if broker.get_account_identity_digest() != account_identity:
                raise RuntimeError("operator continuity account identity changed before commit")

        completed = _commit_stress90_lifecycle_under_broker_fence(
            broker,
            transaction_store=lifecycle_store,
            generic_store=state_store,
            policy_store=policy_store,
            prepare_transaction=prepare_transaction,
            apply_registry_transition=apply_registry_transition,
            apply_evidence_transition=apply_evidence_transition,
            precommit_check=precommit_check,
        )
        receipt = operator_store.require_current_binding(
            account_identity_digest=account_identity,
            account_epoch=account_epoch,
            canonical_runtime=runtime_dir,
            target_ctp_trading_day=target_day,
        )
        target_registry = registry.require_binding_evidence(
            account_identity, runtime_dir, account_epoch
        )
        target_tde = trading_day_store.load_required()
        from .stress90_operator_continuity import (
            require_stress90_operator_continuity_authority,
        )

        if receipt.request_digest != completed.account_day_continuity_digest:
            raise RuntimeError(
                "operator continuity committed lifecycle/receipt request identity mismatch"
            )
        require_stress90_operator_continuity_authority(
            receipt,
            registry_evidence=target_registry,
            trading_day_evidence=target_tde,
        )
        final_record = state_store.load_required_record()
        final_state = final_record.state
        final_policy = policy_store.load_required_record().state
        if (
            final_state.runtime_mode != RuntimeMode.HALTED.value
            or final_state.kill_switch is not True
            or final_state.metadata_verified is not False
            or final_state.trading_day != target_day
            or final_policy.last_completed_account_day != receipt.evidence.source_ctp_trading_day
        ):
            raise RuntimeError(
                "operator continuity committed state did not preserve fail-closed invariants"
            )
        AuditJournal(config.journal_path).record(
            "stress90_operator_continuity_rolled_forward",
            {
                "transaction_id": completed.transaction_id,
                "operation_nonce": completed.operation_nonce,
                "operator_reason": completed.operator_reason,
                "source_ctp_trading_day": receipt.evidence.source_ctp_trading_day,
                "target_ctp_trading_day": receipt.evidence.target_ctp_trading_day,
                "natural_day_gap": receipt.evidence.natural_day_gap,
                "operator_continuity_sequence": receipt.sequence,
                "operator_continuity_checksum": receipt.checksum,
                "operator_continuity_request_digest": receipt.request_digest,
                "evidence_authority": "operator_trust",
                "authoritative_broker_or_exchange_evidence": False,
                "orders_sent": 0,
                "cancels_sent": 0,
            },
        )
        print(
            json.dumps(
                {
                    "operation": "stress90-operator-roll-forward",
                    "status": "committed",
                    "transaction_id": completed.transaction_id,
                    "operator_continuity_sequence": receipt.sequence,
                    "operator_continuity_checksum": receipt.checksum,
                    "source_ctp_trading_day": receipt.evidence.source_ctp_trading_day,
                    "target_ctp_trading_day": receipt.evidence.target_ctp_trading_day,
                    "natural_day_gap": receipt.evidence.natural_day_gap,
                    "evidence_authority": "operator_trust",
                    "authoritative_broker_or_exchange_evidence": False,
                    "runtime_mode": final_state.runtime_mode,
                    "kill_switch": final_state.kill_switch,
                    "metadata_verified": final_state.metadata_verified,
                    "requires_doctor": True,
                    "requires_new_permit": True,
                    "external_activation_gates_completed": False,
                    "orders_sent": 0,
                    "cancels_sent": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except Stress90OperatorContinuityError as exc:
        raise RuntimeError(f"operator continuity evidence is invalid: {exc}") from exc
    finally:
        try:
            if started:
                broker.stop()
        finally:
            lease.release()


def _run_stress90_settlement_roll_forward(config, args) -> int:
    """Fail closed until authoritative prior-day funding closure is available."""

    _validate_stress90_lifecycle_config(config)
    _require_production_confirmation(config, args)
    confirmation = "ROLL_FORWARD_STRESS90_SETTLEMENT"
    if (
        not args.confirm_roll_forward
        or os.getenv("AFUTURE_STRESS90_SETTLEMENT_ACK") != confirmation
    ):
        raise RuntimeError(
            "Stress-90 settlement roll-forward requires --confirm-roll-forward and "
            "AFUTURE_STRESS90_SETTLEMENT_ACK=ROLL_FORWARD_STRESS90_SETTLEMENT"
        )
    _require_lifecycle_operation_nonce(args)
    raise RuntimeError(
        "Stress-90 settlement roll-forward is fail-closed: authoritative prior-day final "
        "funding/settlement witness is unavailable; D+1 PreBalance/current Deposit/Withdraw, "
        "CTP TransferSerial alone, opaque SettlementInfo content, and operator assertion are "
        "insufficient"
    )


def _run_stress90_account_rebase(config, args) -> int:
    """Reset account-path statistics only after explicit flat-account reconciliation."""

    from .broker.ctp import CtpBroker
    from .directional_policy_activation import (
        rebind_stress90_runtime_account_identity,
        require_directional_policy_identity,
    )
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
    operation_nonce = _require_lifecycle_operation_nonce(args)
    shadow_account = bool(getattr(args, "shadow_account", False))
    paths = _stress90_lifecycle_paths(
        config,
        args.runtime_dir,
        shadow_account=shadow_account,
    )
    store = StateStore(paths["state"])
    seed_store = Stress90SeedStore(paths["runtime"] / "stress90_bootstrap_seed.json")
    policy_store = Stress90PolicyStateStore(paths["runtime"] / "stress90_policy_state.json")
    from .stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionStore,
        derive_stress90_account_epoch,
        stress90_lifecycle_account_transition,
    )

    lifecycle_store = Stress90LifecycleTransactionStore(
        paths["runtime"] / "stress90_lifecycle_transaction.json"
    )

    live_broker = CtpBroker(config.ctp)
    if shadow_account:
        expected_shadow = Path(config.state_path).parent / "shadow"
        if Path(paths["runtime"]).resolve() != expected_shadow.resolve():
            raise RuntimeError("Shadow rebase requires --runtime-dir runtime/shadow")
        broker = _build_persistent_shadow_broker(
            config,
            live_broker,
            paths["runtime"] / "shadow_broker_state.json",
        )
    else:
        broker = live_broker
    from .runtime_lease import AccountExclusiveRuntimeLease

    lease = AccountExclusiveRuntimeLease(
        paths["runtime"],
        broker.get_account_identity_digest(),
        role="stress90-account-rebase",
    )
    lease.acquire()
    try:
        state_record = store.load_required_record()
        state = state_record.state
        if state.runtime_mode != RuntimeMode.HALTED.value or not state.kill_switch:
            raise RuntimeError("Stress-90 account rebase requires HALTED state and kill switch")
        seed = seed_store.load_required()
        require_directional_policy_identity(
            state,
            policy_id=STRESS90_POLICY.policy_id,
            policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
            products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
            bootstrap_seed_digest=seed.seed_digest,
        )
        policy_record = policy_store.load_required_record()
        if policy_record.state.bootstrap_seed_digest != seed.seed_digest:
            raise RuntimeError("Stress-90 account rebase seed/policy-state identity mismatch")
        preexisting_lifecycle = lifecycle_store.load()
        exact_prepared_rebase_retry = bool(
            preexisting_lifecycle is not None
            and preexisting_lifecycle.status == "prepared"
            and preexisting_lifecycle.operation == "account_rebase"
            and preexisting_lifecycle.operation_nonce == operation_nonce
        )
        if not exact_prepared_rebase_retry:
            _require_stress90_order_journal_full_audit(paths["runtime"])
        source_account_identity = policy_record.state.live_account_identity_digest
        if (
            preexisting_lifecycle is not None
            and preexisting_lifecycle.status == "prepared"
            and preexisting_lifecycle.operation == "account_rebase"
        ):
            source_account_identity = preexisting_lifecycle.source_account_identity_digest
        if source_account_identity is None:
            raise RuntimeError("Stress-90 account rebase source account identity is missing")
        preflight_account_identity = broker.get_account_identity_digest()
        account_switched = preflight_account_identity != source_account_identity
        broker_seed_state = (
            replace(
                state,
                last_order_id="",
                last_trade_id="",
                recent_trade_ids=[],
            )
            if account_switched
            else state
        )
        if not account_switched:
            _configure_stress90_lifecycle_order_journal(broker, paths["runtime"])
        _seed_state_aware_ctp_broker(
            live_broker,
            broker_seed_state,
            reject_ambiguous=not shadow_account,
        )
        broker.start()
    except BaseException:
        try:
            broker.stop()
        finally:
            lease.release()
        raise
    try:
        _wait_until_ready(broker, args.startup_timeout)
        wait_for_fresh_snapshot(broker, args.snapshot_wait)
        if account_switched:
            mechanical = _require_empty_session_before_account_epoch_cleanup(
                broker,
                runtime_dir=paths["runtime"],
                timeout_seconds=max(0.1, float(args.snapshot_wait)),
            )
            cleanup_account = mechanical.account
            cleanup_trading_day = mechanical.trading_day
            cleanup_positions = mechanical.positions
            cleanup_active_orders = mechanical.active_orders
            _require_verified_lifecycle_account_snapshot(
                cleanup_account,
                operation="Stress-90 account switch empty-session proof",
            )
            if exact_prepared_rebase_retry and not shadow_account:
                cleanup_local_positions = store.positions_from_state(state)
                cleanup_reconciliation = compare_positions(
                    cleanup_local_positions,
                    list(cleanup_positions),
                )
                _require_lifecycle_resume_safety(
                    state,
                    broker_positions=list(cleanup_positions),
                    local_positions=cleanup_local_positions,
                    active_orders=list(cleanup_active_orders),
                    reconciliation_matched=cleanup_reconciliation.matched,
                    operation="Stress-90 account rebase cleanup recovery",
                )
                assert preexisting_lifecycle is not None
                _seal_stress90_account_switch_order_epoch(
                    runtime_dir=paths["runtime"],
                    lifecycle_transaction=preexisting_lifecycle,
                    trading_day=cleanup_trading_day,
                    operator_reason=preexisting_lifecycle.operator_reason,
                    broker_flat=not any(not position.empty for position in cleanup_positions),
                    local_flat=not any(not position.empty for position in cleanup_local_positions),
                    no_active_orders=not cleanup_active_orders,
                    reconciled=cleanup_reconciliation.matched,
                )
        else:
            mechanical = _require_lifecycle_mechanical_snapshot(
                broker,
                runtime_dir=paths["runtime"],
                timeout_seconds=max(0.1, float(args.snapshot_wait)),
            )
        account = mechanical.account
        trading_day = mechanical.trading_day
        positions = list(mechanical.positions)
        active_orders = list(mechanical.active_orders)
        _require_verified_lifecycle_account_snapshot(
            account,
            operation="Stress-90 account rebase",
        )
        if account.trading_day != trading_day:
            raise RuntimeError("Stress-90 account rebase account/CTP trading day mismatch")
        _require_lifecycle_trading_day_not_backward(
            trading_day,
            state=state,
            policy_state=policy_record.state,
            operation="Stress-90 account rebase",
        )
        if not account_switched:
            persisted_state = state
            state = _adopt_stress90_lifecycle_crash_fills(
                config,
                broker,
                runtime_dir=paths["runtime"],
                state=state,
                broker_positions=positions,
            )
            state = _require_no_unpersisted_lifecycle_crash_fill_adoption(
                persisted_state,
                state,
                pending_lifecycle=preexisting_lifecycle,
                persisted_record=state_record,
                runtime_dir=paths["runtime"],
                consumer_operation_nonce=operation_nonce,
            )
        local_positions = store.positions_from_state(state)
        account_identity_digest = broker.get_account_identity_digest()
        if account_identity_digest != preflight_account_identity:
            raise RuntimeError("Stress-90 account identity changed during rebase")
        account_switched = account_identity_digest != source_account_identity
        reconciliation = compare_positions(local_positions, positions)
        if not reconciliation.matched:
            raise RuntimeError(
                "Stress-90 account rebase Broker/local reconciliation failed: "
                + reconciliation.details
            )
        from .stress90_lifecycle_transaction import (
            Stress90LifecycleTransactionError,
            require_matching_stress90_lifecycle_account_evidence,
        )

        existing_lifecycle = lifecycle_store.load()
        if (
            existing_lifecycle is not None
            and existing_lifecycle.operation == "account_rebase"
            and existing_lifecycle.operation_nonce == operation_nonce
            and existing_lifecycle.trading_day == trading_day
            and existing_lifecycle.account_identity_digest == account_identity_digest
        ):
            _require_lifecycle_resume_safety(
                state,
                broker_positions=positions,
                local_positions=local_positions,
                active_orders=active_orders,
                reconciliation_matched=reconciliation.matched,
                operation="Stress-90 account rebase",
            )
            require_matching_stress90_lifecycle_account_evidence(
                existing_lifecycle,
                account,
                account_identity_digest=account_identity_digest,
            )
            _require_lifecycle_operator_reason(existing_lifecycle, args.operator_reason)
            pending_lifecycle = existing_lifecycle
        elif existing_lifecycle is not None and existing_lifecycle.status == "prepared":
            pending_lifecycle = existing_lifecycle
        else:
            pending_lifecycle = None
        if pending_lifecycle is not None:
            if (
                pending_lifecycle.operation != "account_rebase"
                or pending_lifecycle.operation_nonce != operation_nonce
                or pending_lifecycle.trading_day != trading_day
                or pending_lifecycle.account_identity_digest != account_identity_digest
            ):
                raise Stress90LifecycleTransactionError(
                    "pending lifecycle transaction does not match this account rebase"
                )
            _require_lifecycle_operator_reason(pending_lifecycle, args.operator_reason)
            _require_stress90_lifecycle_precommit_current(
                broker,
                mechanical,
                runtime_dir=paths["runtime"],
                lifecycle_transaction=pending_lifecycle,
            )
            from .stress90_activation_permit import Stress90ActivationPermitStore

            def prepare_pending_rebase():
                _retire_stress90_execution_intent_for_account_rebase(
                    runtime_dir=paths["runtime"],
                    lifecycle_transaction=pending_lifecycle,
                    broker_flat=not any(not position.empty for position in positions),
                    local_flat=not any(not position.empty for position in local_positions),
                    no_active_orders=not active_orders,
                    reconciled=reconciliation.matched,
                    strong_confirmation=os.environ["AFUTURE_STRESS90_REBASE_ACK"],
                )
                if not shadow_account:
                    _seal_stress90_account_switch_order_epoch(
                        runtime_dir=paths["runtime"],
                        lifecycle_transaction=pending_lifecycle,
                        trading_day=trading_day,
                        operator_reason=pending_lifecycle.operator_reason,
                        broker_flat=not any(not position.empty for position in positions),
                        local_flat=not any(not position.empty for position in local_positions),
                        no_active_orders=not active_orders,
                        reconciled=reconciliation.matched,
                    )
                _require_stress90_order_journal_full_audit(paths["runtime"])
                return pending_lifecycle

            if account_switched:
                with _stress90_lifecycle_broker_fence(broker):
                    _require_stress90_lifecycle_precommit_current(
                        broker,
                        mechanical,
                        runtime_dir=paths["runtime"],
                        lifecycle_transaction=pending_lifecycle,
                    )
                    prepare_pending_rebase()
                mechanical = _require_account_switch_target_epoch_current(
                    broker,
                    live_broker,
                    runtime_dir=paths["runtime"],
                    state=state,
                    local_positions=local_positions,
                    prior_snapshot=mechanical,
                    lifecycle_transaction=pending_lifecycle,
                    shadow_account=shadow_account,
                    startup_timeout=float(args.startup_timeout),
                    snapshot_wait=float(args.snapshot_wait),
                )
                account = mechanical.account
                trading_day = mechanical.trading_day
                positions = list(mechanical.positions)
                active_orders = list(mechanical.active_orders)
                reconciliation = compare_positions(local_positions, positions)
            Stress90ActivationPermitStore(
                paths["runtime"] / "stress90_activation_permit.json"
            ).invalidate("Stress-90 account rebase changed bound account-path evidence")
            completed = _commit_stress90_lifecycle_under_broker_fence(
                broker,
                transaction_store=lifecycle_store,
                generic_store=store,
                policy_store=policy_store,
                prepare_transaction=(
                    (lambda: pending_lifecycle) if account_switched else prepare_pending_rebase
                ),
                apply_registry_transition=lambda transaction: (
                    _apply_stress90_account_runtime_registry_transition(
                        config,
                        runtime_dir=paths["runtime"],
                        lifecycle_transaction=transaction,
                    )
                ),
                apply_evidence_transition=lambda transaction: (
                    _apply_stress90_trading_day_evidence_transition(
                        config,
                        runtime_dir=paths["runtime"],
                        lifecycle_transaction=transaction,
                    )
                ),
                precommit_check=lambda: _require_stress90_lifecycle_precommit_current(
                    broker,
                    mechanical,
                    runtime_dir=paths["runtime"],
                    lifecycle_transaction=pending_lifecycle,
                ),
            )
            journal = AuditJournal(paths["journal"])
            _record_lifecycle_completion_once(
                journal,
                "stress90_account_rebase_completed",
                completed.transaction_id,
                {
                    "trading_day": trading_day,
                    "operator_reason": completed.operator_reason,
                    "operation_nonce": completed.operation_nonce,
                    "source_account_identity_digest": (completed.source_account_identity_digest),
                    "account_identity_digest": completed.account_identity_digest,
                    "verified_deposit_delta": completed.verified_deposit_delta,
                    "verified_withdrawal_delta": completed.verified_withdrawal_delta,
                    "bootstrap_seed_digest": seed.seed_digest,
                    "kill_switch_remains_active": True,
                    "resumed_transaction_id": completed.transaction_id,
                },
            )
            print(
                json.dumps(
                    {
                        "rebased": True,
                        "resumed": True,
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
        account_day_continuity = None
        if not account_switched and state.trading_day != trading_day:
            account_day_continuity = _require_stress90_account_day_continuity(
                runtime_dir=paths["runtime"],
                completed_account_day=state.trading_day,
                current_ctp_trading_day=trading_day,
            )
        account_transition = stress90_lifecycle_account_transition(
            operation="account_rebase",
            generic_source=state,
            account_snapshot=account,
            account_switched=account_switched,
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
            account_identity_digest=account_identity_digest,
            account_epoch=derive_stress90_account_epoch(
                operation="account_rebase",
                operation_nonce=operation_nonce,
                policy_source_checksum=policy_record.checksum,
                account_identity_digest=account_identity_digest,
                trading_day=trading_day,
            ),
        )
        deposit_delta = account_transition.verified_deposit_delta
        withdrawal_delta = account_transition.verified_withdrawal_delta
        if not account_switched:
            if deposit_delta == 0.0 and withdrawal_delta == 0.0:
                raise RuntimeError(
                    "same-account rebase cash flow change (deposit or withdrawal) is required"
                )
        rebased_day_start = account_transition.day_start_equity
        rebased_high_watermark = account_transition.equity_high_watermark
        journal = AuditJournal(paths["journal"])
        journal.record(
            "stress90_account_rebase_prepared",
            {
                **asdict(audit),
                "operation_nonce": operation_nonce,
                "source_account_identity_digest": source_account_identity,
                "account_identity_digest": account_identity_digest,
                "verified_deposit_delta": deposit_delta if not account_switched else 0.0,
                "verified_withdrawal_delta": (withdrawal_delta if not account_switched else 0.0),
                "bootstrap_seed_digest": seed.seed_digest,
                "kill_switch_active": True,
            },
        )
        from .stress90_activation_permit import Stress90ActivationPermitStore

        _require_lifecycle_mechanical_snapshot_current(broker, mechanical)
        Stress90ActivationPermitStore(
            paths["runtime"] / "stress90_activation_permit.json"
        ).invalidate("Stress-90 account rebase changed bound account-path evidence")
        generic_rebased = replace(
            state,
            trading_day=trading_day,
            day_start_equity=rebased_day_start,
            equity_high_watermark=rebased_high_watermark,
            last_account_equity=float(account.equity),
            last_account_trading_day=trading_day,
            last_account_deposit=float(account.deposit),
            last_account_withdrawal=float(account.withdrawal),
            last_account_cash_flow_verified=bool(account.cash_flow_verified),
            last_account_settlement_id=(
                int(account.settlement_id) if account.settlement_id is not None else -1
            ),
            reconciled=True,
            metadata_verified=False,
            kill_switch=True,
            kill_reason="Stress-90 account path rebased; doctor/Shadow gates remain required",
            runtime_mode=RuntimeMode.HALTED.value,
            directional_daily_circuit_day="",
            last_order_id="" if account_switched else state.last_order_id,
            last_trade_id="" if account_switched else state.last_trade_id,
            recent_trade_ids=[] if account_switched else list(state.recent_trade_ids),
            recent_daily_returns=[],
        )
        generic_rebased = rebind_stress90_runtime_account_identity(
            generic_rebased,
            account_identity_digest=account_identity_digest,
        )

        def precheck_fresh_rebase() -> None:
            _require_lifecycle_mechanical_snapshot_current(broker, mechanical)
            if account_day_continuity is not None:
                current_continuity = _require_stress90_account_day_continuity(
                    runtime_dir=paths["runtime"],
                    completed_account_day=account_day_continuity.completed_account_day,
                    current_ctp_trading_day=(account_day_continuity.current_ctp_trading_day),
                )
                if current_continuity.continuity_digest != account_day_continuity.continuity_digest:
                    raise RuntimeError("Stress-90 account-day continuity evidence changed")
            _require_lifecycle_mechanical_snapshot_current(broker, mechanical)

        def prepare_fresh_rebase():
            prepared = lifecycle_store.begin(
                operation="account_rebase",
                generic_source=state_record,
                policy_source=policy_record,
                generic_target=generic_rebased,
                policy_target=rebased,
                trading_day=trading_day,
                account_identity_digest=account_identity_digest,
                account_snapshot=account,
                operation_nonce=operation_nonce,
                operator_reason=args.operator_reason,
                account_day_continuity_digest=(
                    ""
                    if account_day_continuity is None
                    else account_day_continuity.continuity_digest
                ),
            )
            _retire_stress90_execution_intent_for_account_rebase(
                runtime_dir=paths["runtime"],
                lifecycle_transaction=prepared,
                broker_flat=not any(not position.empty for position in positions),
                local_flat=not any(not position.empty for position in local_positions),
                no_active_orders=not active_orders,
                reconciled=reconciliation.matched,
                strong_confirmation=os.environ["AFUTURE_STRESS90_REBASE_ACK"],
            )
            if not shadow_account:
                _seal_stress90_account_switch_order_epoch(
                    runtime_dir=paths["runtime"],
                    lifecycle_transaction=prepared,
                    trading_day=trading_day,
                    operator_reason=prepared.operator_reason,
                    broker_flat=not any(not position.empty for position in positions),
                    local_flat=not any(not position.empty for position in local_positions),
                    no_active_orders=not active_orders,
                    reconciled=reconciliation.matched,
                )
            _require_stress90_order_journal_full_audit(paths["runtime"])
            return prepared

        prepared_lifecycle = None
        if account_switched:
            with _stress90_lifecycle_broker_fence(broker):
                precheck_fresh_rebase()
                prepared_lifecycle = prepare_fresh_rebase()
            mechanical = _require_account_switch_target_epoch_current(
                broker,
                live_broker,
                runtime_dir=paths["runtime"],
                state=state,
                local_positions=local_positions,
                prior_snapshot=mechanical,
                lifecycle_transaction=prepared_lifecycle,
                shadow_account=shadow_account,
                startup_timeout=float(args.startup_timeout),
                snapshot_wait=float(args.snapshot_wait),
            )
            account = mechanical.account
            trading_day = mechanical.trading_day
            positions = list(mechanical.positions)
            active_orders = list(mechanical.active_orders)
            reconciliation = compare_positions(local_positions, positions)
        completed = _commit_stress90_lifecycle_under_broker_fence(
            broker,
            transaction_store=lifecycle_store,
            generic_store=store,
            policy_store=policy_store,
            prepare_transaction=(
                (lambda: prepared_lifecycle) if account_switched else prepare_fresh_rebase
            ),
            apply_registry_transition=lambda transaction: (
                _apply_stress90_account_runtime_registry_transition(
                    config,
                    runtime_dir=paths["runtime"],
                    lifecycle_transaction=transaction,
                )
            ),
            apply_evidence_transition=lambda transaction: (
                _apply_stress90_trading_day_evidence_transition(
                    config,
                    runtime_dir=paths["runtime"],
                    lifecycle_transaction=transaction,
                )
            ),
            precommit_check=(
                (
                    lambda: _require_stress90_lifecycle_precommit_current(
                        broker,
                        mechanical,
                        runtime_dir=paths["runtime"],
                        lifecycle_transaction=prepared_lifecycle,
                    )
                )
                if account_switched
                else precheck_fresh_rebase
            ),
        )
        _record_lifecycle_completion_once(
            journal,
            "stress90_account_rebase_completed",
            completed.transaction_id,
            {
                **{**asdict(audit), "operator_reason": completed.operator_reason},
                "operation_nonce": completed.operation_nonce,
                "source_account_identity_digest": (completed.source_account_identity_digest),
                "account_identity_digest": completed.account_identity_digest,
                "verified_deposit_delta": completed.verified_deposit_delta,
                "verified_withdrawal_delta": completed.verified_withdrawal_delta,
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
        lease.release()


def _run_directional_policy_migrate(config, args) -> int:
    """Explicitly retire Stress-90 identity without granting execution permission."""

    from .account_runtime_registry import PRODUCTION_ACCOUNT_RUNTIME_REGISTRY_PATH
    from .broker.ctp import CtpBroker
    from .directional_policy_activation import (
        DIRECTIONAL_POLICY_MIGRATION_CONFIRMATION,
        migrate_stress90_to_execution_aligned,
        require_directional_policy_identity,
    )
    from .directional_stress90_policy import STRESS90_POLICY
    from .directional_stress90_state import Stress90PolicyStateStore, Stress90SeedStore
    from .execution_aligned_policy import FROZEN_PRODUCTS
    from .journal import AuditJournal
    from .reconcile import compare_positions
    from .stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
        require_matching_stress90_lifecycle_account_evidence,
    )

    if config.mode != "live" or config.ctp is None:
        raise ValueError("directional policy migration requires system.mode=live")
    if Path(config.account_registry_path) != PRODUCTION_ACCOUNT_RUNTIME_REGISTRY_PATH:
        raise ValueError("directional policy migration requires the fixed machine registry path")
    products = tuple(sorted({str(item).upper() for item in config.directional.products}))
    if (
        not config.directional.enabled
        or config.directional.policy != args.to
        or args.to != "execution_aligned"
        or not config.directional.account_exclusive
        or products != FROZEN_PRODUCTS
    ):
        raise ValueError(
            "directional policy migration requires explicit execution_aligned target, "
            "account exclusivity and the frozen 50-product universe"
        )
    if not config.ctp.account_id or not config.ctp.currency_id:
        raise ValueError(
            "directional policy migration requires the exact existing CTP account identity"
        )
    _require_production_confirmation(config, args)
    if (
        not args.confirm_migration
        or os.getenv("AFUTURE_DIRECTIONAL_POLICY_MIGRATION_ACK")
        != DIRECTIONAL_POLICY_MIGRATION_CONFIRMATION
    ):
        raise RuntimeError(
            "directional policy migration requires --confirm-migration and "
            "AFUTURE_DIRECTIONAL_POLICY_MIGRATION_ACK="
            "I_CONFIRM_DIRECTIONAL_POLICY_MIGRATION"
        )
    operation_nonce = _require_lifecycle_operation_nonce(args)

    shadow_account = bool(getattr(args, "shadow_account", False))
    paths = _stress90_lifecycle_paths(
        config,
        args.runtime_dir,
        shadow_account=shadow_account,
    )
    state_store = StateStore(paths["state"])
    seed_store = Stress90SeedStore(paths["runtime"] / "stress90_bootstrap_seed.json")
    policy_store = Stress90PolicyStateStore(paths["runtime"] / "stress90_policy_state.json")
    lifecycle_store = Stress90LifecycleTransactionStore(
        paths["runtime"] / "stress90_lifecycle_transaction.json"
    )

    live_broker = CtpBroker(config.ctp)
    if shadow_account:
        expected_shadow = Path(config.state_path).parent / "shadow"
        if Path(paths["runtime"]).resolve() != expected_shadow.resolve():
            raise RuntimeError("Shadow policy migration requires --runtime-dir runtime/shadow")
        broker = _build_persistent_shadow_broker(
            config,
            live_broker,
            paths["runtime"] / "shadow_broker_state.json",
        )
    else:
        broker = live_broker
    from .runtime_lease import AccountExclusiveRuntimeLease

    lease = AccountExclusiveRuntimeLease(
        paths["runtime"],
        broker.get_account_identity_digest(),
        role="directional-policy-migrate",
    )
    lease.acquire()
    try:
        _require_stress90_order_journal_full_audit(paths["runtime"])
        state_record = state_store.load_required_record()
        state = state_record.state
        if state.runtime_mode != RuntimeMode.HALTED.value or not state.kill_switch:
            raise RuntimeError("directional policy migration requires HALTED state and kill switch")
        seed = seed_store.load_required()
        policy_record = policy_store.load_required_record()
        if (
            policy_record.state.bootstrap_seed_digest != seed.seed_digest
            or policy_record.state.policy_definition_digest
            != STRESS90_POLICY.policy_definition_digest
        ):
            raise RuntimeError("directional policy migration Stress-90 state/seed mismatch")
        pending_lifecycle = lifecycle_store.load()
        exact_migration_retry = bool(
            pending_lifecycle is not None
            and pending_lifecycle.operation == "stress90_to_execution_aligned"
            and pending_lifecycle.operation_nonce == operation_nonce
        )
        if not exact_migration_retry:
            require_directional_policy_identity(
                state,
                policy_id=STRESS90_POLICY.policy_id,
                policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
                products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
                bootstrap_seed_digest=seed.seed_digest,
            )
        _configure_stress90_lifecycle_order_journal(broker, paths["runtime"])
        _seed_state_aware_ctp_broker(live_broker, state, reject_ambiguous=not shadow_account)
        broker.start()
    except BaseException:
        try:
            broker.stop()
        finally:
            lease.release()
        raise
    try:
        _wait_until_ready(broker, args.startup_timeout)
        wait_for_fresh_snapshot(broker, args.snapshot_wait)
        mechanical = _require_lifecycle_mechanical_snapshot(
            broker,
            runtime_dir=paths["runtime"],
            timeout_seconds=max(0.1, float(args.snapshot_wait)),
        )
        account = mechanical.account
        trading_day = mechanical.trading_day
        positions = list(mechanical.positions)
        active_orders = list(mechanical.active_orders)
        _require_verified_lifecycle_account_snapshot(
            account,
            operation="directional policy migration",
        )
        if not shadow_account and not (
            pending_lifecycle is not None and pending_lifecycle.status == "prepared"
        ):
            _checkpoint_ctp_trading_day(config, live_broker, trading_day)
        if account.trading_day != trading_day:
            raise RuntimeError("directional policy migration account/CTP day mismatch")
        _require_lifecycle_trading_day_not_backward(
            trading_day,
            state=state,
            policy_state=policy_record.state,
            operation="directional policy migration",
        )
        persisted_state = state
        state = _adopt_stress90_lifecycle_crash_fills(
            config,
            broker,
            runtime_dir=paths["runtime"],
            state=state,
            broker_positions=positions,
        )
        state = _require_no_unpersisted_lifecycle_crash_fill_adoption(
            persisted_state,
            state,
            pending_lifecycle=pending_lifecycle,
            persisted_record=state_record,
            runtime_dir=paths["runtime"],
            consumer_operation_nonce=operation_nonce,
        )
        local_positions = state_store.positions_from_state(state)
        account_identity_digest = broker.get_account_identity_digest()
        if policy_record.state.live_account_identity_digest != account_identity_digest:
            raise RuntimeError("directional policy migration policy/account identity mismatch")
        reconciliation = compare_positions(local_positions, positions)
        if not reconciliation.matched:
            raise RuntimeError(
                "directional policy migration Broker/local reconciliation failed: "
                + reconciliation.details
            )

        journal = AuditJournal(paths["journal"])
        from .stress90_activation_permit import Stress90ActivationPermitStore

        _require_lifecycle_mechanical_snapshot_current(broker, mechanical)
        Stress90ActivationPermitStore(
            paths["runtime"] / "stress90_activation_permit.json"
        ).invalidate("directional policy identity migration invalidated Stress-90 permit")
        if (
            pending_lifecycle is not None
            and pending_lifecycle.operation == "stress90_to_execution_aligned"
            and pending_lifecycle.operation_nonce == operation_nonce
            and pending_lifecycle.trading_day == trading_day
            and pending_lifecycle.account_identity_digest == account_identity_digest
        ):
            _require_lifecycle_resume_safety(
                state,
                broker_positions=positions,
                local_positions=local_positions,
                active_orders=active_orders,
                reconciliation_matched=reconciliation.matched,
                operation="directional policy migration",
            )
            require_matching_stress90_lifecycle_account_evidence(
                pending_lifecycle,
                account,
                account_identity_digest=account_identity_digest,
            )
            _require_lifecycle_operator_reason(pending_lifecycle, args.operator_reason)
            completed = _commit_stress90_lifecycle_under_broker_fence(
                broker,
                transaction_store=lifecycle_store,
                generic_store=state_store,
                policy_store=policy_store,
                prepare_transaction=lambda: pending_lifecycle,
                apply_registry_transition=lambda transaction: (
                    _apply_stress90_account_runtime_registry_transition(
                        config,
                        runtime_dir=paths["runtime"],
                        lifecycle_transaction=transaction,
                    )
                ),
                apply_evidence_transition=lambda transaction: (
                    _apply_stress90_trading_day_evidence_transition(
                        config,
                        runtime_dir=paths["runtime"],
                        lifecycle_transaction=transaction,
                    )
                ),
                precommit_check=lambda: _require_lifecycle_mechanical_snapshot_current(
                    broker, mechanical
                ),
            )
            resumed = True
        elif pending_lifecycle is not None and pending_lifecycle.status == "prepared":
            raise Stress90LifecycleTransactionError(
                "pending lifecycle transaction does not match this policy migration"
            )
        else:
            migrated = migrate_stress90_to_execution_aligned(
                state,
                broker_flat=not any(not position.empty for position in positions),
                local_flat=not any(not position.empty for position in local_positions),
                no_active_orders=not active_orders,
                reconciled=bool(state.reconciled and reconciliation.matched),
                account_identity_digest=account_identity_digest,
                operator_reason=args.operator_reason,
                strong_confirmation=os.environ["AFUTURE_DIRECTIONAL_POLICY_MIGRATION_ACK"],
            )
            journal.record(
                "directional_policy_migration_prepared",
                {
                    "from": STRESS90_POLICY.policy_id,
                    "to": "execution_aligned",
                    "trading_day": trading_day,
                    "operator_reason": args.operator_reason,
                },
            )
            completed = _commit_stress90_lifecycle_under_broker_fence(
                broker,
                transaction_store=lifecycle_store,
                generic_store=state_store,
                policy_store=policy_store,
                prepare_transaction=lambda: lifecycle_store.begin(
                    operation="stress90_to_execution_aligned",
                    generic_source=state_record,
                    policy_source=policy_record,
                    generic_target=migrated,
                    policy_target=policy_record.state,
                    trading_day=trading_day,
                    account_identity_digest=account_identity_digest,
                    account_snapshot=account,
                    operation_nonce=operation_nonce,
                    operator_reason=args.operator_reason,
                ),
                apply_registry_transition=lambda transaction: (
                    _apply_stress90_account_runtime_registry_transition(
                        config,
                        runtime_dir=paths["runtime"],
                        lifecycle_transaction=transaction,
                    )
                ),
                apply_evidence_transition=lambda transaction: (
                    _apply_stress90_trading_day_evidence_transition(
                        config,
                        runtime_dir=paths["runtime"],
                        lifecycle_transaction=transaction,
                    )
                ),
                precommit_check=lambda: _require_lifecycle_mechanical_snapshot_current(
                    broker, mechanical
                ),
            )
            resumed = False
        _record_lifecycle_completion_once(
            journal,
            "directional_policy_migration_completed",
            completed.transaction_id,
            {
                "from": STRESS90_POLICY.policy_id,
                "to": "execution_aligned",
                "trading_day": trading_day,
                "operator_reason": completed.operator_reason,
                "resumed": resumed,
                "kill_switch_remains_active": True,
            },
        )
        print(
            json.dumps(
                {
                    "migrated": True,
                    "from": STRESS90_POLICY.policy_id,
                    "to": "execution_aligned",
                    "resumed": resumed,
                    "runtime_dir": str(paths["runtime"]),
                    "trading_day": trading_day,
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
        lease.release()


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

    from .account_runtime_registry import AccountRuntimeRegistry
    from .directional_ohlc_cache import DirectionalOHLCCacheStore
    from .directional_ohlc_refresh import refresh_directional_ohlc_cache
    from .directional_stress90_state import Stress90PolicyStateStore
    from .durable_file_creation import canonical_file_path
    from .execution_aligned_runtime import SinaContinuousOHLCProvider
    from .runtime_lease import AccountExclusiveRuntimeLease
    from .stress90_lifecycle_transaction import Stress90LifecycleTransactionStore
    from .trading_day_evidence import (
        TradingDayEvidenceStore,
        require_authoritative_trading_day_evidence,
    )

    runtime_dir = Path(config.state_path).resolve(strict=False).parent
    expected_cache = runtime_dir / "directional_ohlc_cache.json"
    expected_evidence = runtime_dir / "ctp_trading_day_evidence.json"
    requested_cache = Path(args.cache) if args.cache else expected_cache
    requested_evidence = (
        Path(args.trading_day_evidence) if args.trading_day_evidence else expected_evidence
    )
    cache_path = canonical_file_path(requested_cache)
    evidence_path = canonical_file_path(requested_evidence)
    if cache_path != expected_cache or cache_path.name != "directional_ohlc_cache.json":
        raise RuntimeError(
            "directional-ohlc-refresh cache must use the fixed evidence runtime path"
        )
    if evidence_path != expected_evidence or evidence_path.name != "ctp_trading_day_evidence.json":
        raise RuntimeError(
            "directional-ohlc-refresh trading-day evidence must use the fixed runtime path"
        )
    policy_store = Stress90PolicyStateStore(runtime_dir / "stress90_policy_state.json")
    evidence_store = TradingDayEvidenceStore(evidence_path)
    preliminary_policy = policy_store.load_required()
    preliminary_evidence = evidence_store.load_required()
    lease_account = (
        preliminary_policy.live_account_identity_digest
        or preliminary_evidence.account_identity_digest
    )
    entry = None
    authoritative_evidence = None
    for attempt in range(2):
        lease = AccountExclusiveRuntimeLease(
            runtime_dir,
            lease_account,
            role="directional-ohlc-refresh",
        )
        lease.acquire()
        try:
            policy_state = policy_store.load_required()
            evidence = evidence_store.load_required()
            authoritative_account = (
                policy_state.live_account_identity_digest or evidence.account_identity_digest
            )
            if not lease.authorizes_technical_activation(
                authoritative_account,
                runtime_dir,
            ):
                if attempt == 1:
                    raise RuntimeError(
                        "directional-ohlc-refresh authoritative account changed during "
                        "lease acquisition"
                    )
                lease_account = authoritative_account
                continue
            registry = AccountRuntimeRegistry(_stress90_account_registry_path(config))
            lifecycle = Stress90LifecycleTransactionStore(
                runtime_dir / "stress90_lifecycle_transaction.json"
            ).load()
            require_authoritative_trading_day_evidence(
                evidence,
                policy_state=policy_state,
                registry=registry,
                runtime_dir=runtime_dir,
                lifecycle_transaction=lifecycle,
            )
            entry = refresh_directional_ohlc_cache(
                DirectionalOHLCCacheStore(cache_path),
                provider_factory=SinaContinuousOHLCProvider,
                products=tuple(config.directional.products),
                current_ctp_trading_day=args.current_trading_day,
                authoritative_ctp_trading_day=evidence.trading_day,
            )
            authoritative_evidence = evidence
            break
        finally:
            lease.release()
    if entry is None or authoritative_evidence is None:
        raise RuntimeError("directional-ohlc-refresh authority could not be acquired")
    evidence = authoritative_evidence
    print(
        json.dumps(
            {
                "cache": str(cache_path),
                "latest_completed_day": entry.latest_date.strftime("%Y%m%d"),
                "row_count": entry.row_count,
                "content_digest": entry.content_digest,
                "ctp_trading_day_evidence": str(evidence_path),
                "ctp_trading_day_evidence_checksum": evidence.checksum,
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


def run_command(
    argv: list[str] | None = None,
    *,
    live_runtime_lease: AccountExclusiveRuntimeLease | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if live_runtime_lease is not None and args.command != "live":
        raise ValueError("a borrowed live lease is only valid for the live command")
    config = load_config(
        args.config,
        require_ctp_credentials=args.command
        not in {
            "status",
            "stress90-bootstrap",
            "stress90-registry-init",
            "stress90-oi-compare",
            "directional-ohlc-refresh",
        },
    )
    if args.command == "status":
        return _run_status(config)
    if args.command == "stress90-bootstrap":
        return _run_stress90_bootstrap(config, args)
    if args.command == "stress90-registry-init":
        return _run_stress90_registry_init(config, args)
    if args.command == "directional-ohlc-refresh":
        return _run_directional_ohlc_refresh(config, args)
    if args.command == "stress90-oi-compare":
        return _run_stress90_oi_compare(config, args)
    if args.command == "directional-policy-migrate":
        return _run_directional_policy_migrate(config, args)
    if args.command == "stress90-oi-collect":
        return _run_stress90_oi_collect(config, args)
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

    if args.command == "stress90-capacity-report":
        return _run_stress90_capacity_report(config, args)

    if args.command == "stress90-activate":
        return _run_stress90_activate(config, args)

    if args.command == "stress90-prepare-decision":
        return _run_stress90_prepare_decision(config, args)

    if args.command == "stress90-account-rebase":
        return _run_stress90_account_rebase(config, args)

    if args.command == "stress90-crash-fill-recover":
        return _run_stress90_crash_fill_recovery(config, args)

    if args.command == "stress90-settlement-roll-forward":
        return _run_stress90_settlement_roll_forward(config, args)

    if args.command == "stress90-operator-roll-forward":
        return _run_stress90_operator_roll_forward(config, args)

    if args.command == "stress90-order-journal-rollover":
        return _run_stress90_order_journal_rollover(config, args)

    if args.command == "shadow":
        return _run_shadow(config, args, logger)

    if args.command == "recover-state":
        return _recover_state(config, args, logger)

    if args.command != "live":
        raise RuntimeError(f"unhandled command cannot enter live runtime: {args.command}")
    if config.mode != "live" or config.ctp is None:
        raise ValueError("live command requires system.mode=live")
    _require_production_confirmation(config, args)
    return _run_live(config, args, logger, live_runtime_lease=live_runtime_lease)
