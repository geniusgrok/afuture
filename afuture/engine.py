"""实时和历史回放共用的套利交易引擎。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from time import monotonic
from zoneinfo import ZoneInfo

from .alerts import AlertManager
from .auto import AutoPairManager
from .economics import estimate_net_edge
from .execution import PairExecutor
from .fees import calculate_commission
from .health.monitor import HealthMonitor
from .metadata import validate_contract_metadata
from .models import (
    ContractPosition,
    ContractSpec,
    Offset,
    Order,
    PairConfig,
    RiskDecision,
    RuntimeMode,
    SignalAction,
    Tick,
    Trade,
)
from .portfolio_risk import PortfolioRiskAnalyzer
from .position import PositionBook
from .quality import ExecutionQualityRecorder
from .reconcile import compare_positions
from .risk import RiskManager
from .state import MAX_RECENT_TRADE_IDS, RuntimeState, StateStore
from .strategy import CalendarSpreadStrategy

_CHINA_TZ = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger(__name__)


class TradingEngine:
    """把行情、策略、风险、执行、对账和持久化串成单一生产链路。"""

    def __init__(
        self,
        broker,
        pairs: list[PairConfig],
        specs: dict[str, ContractSpec],
        risk_manager: RiskManager,
        state_store: StateStore,
        *,
        auto_flatten_imbalance: bool = True,
        aggressive_ticks: int = 1,
        slippage_ticks: int = 1,
        legging_timeout_seconds: float = 2.0,
        journal=None,
        health_monitor: HealthMonitor | None = None,
        portfolio_risk: PortfolioRiskAnalyzer | None = None,
        alert_manager: AlertManager | None = None,
        auto_manager: AutoPairManager | None = None,
        quality_recorder: ExecutionQualityRecorder | None = None,
        require_live_metadata: bool = False,
        metadata_timeout_seconds: float = 10.0,
        historical_mode: bool = False,
        health_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.broker = broker
        self.pairs = {pair.pair_id: pair for pair in pairs}
        self.specs = dict(specs)
        self._static_pair_ids = set(self.pairs)
        self._static_spec_symbols = set(self.specs)
        self._auto_pair_ids: set[str] = set()
        self._retiring_auto_pairs: set[str] = set()
        self.risk_manager = risk_manager
        self.state_store = state_store
        self.strategies = {pair.pair_id: CalendarSpreadStrategy(pair) for pair in pairs}
        self.executor = PairExecutor(
            broker,
            risk_manager,
            self.specs,
            aggressive_ticks=aggressive_ticks,
            slippage_ticks=slippage_ticks,
        )

        self.quotes: dict[str, Tick] = {}
        self.state = RuntimeState()
        self._recent_trade_id_set: set[str] = set()
        self.halted = False
        self.auto_flatten_imbalance = auto_flatten_imbalance
        self.legging_timeout_seconds = max(0.0, legging_timeout_seconds)
        self.slippage_ticks = max(0, slippage_ticks)
        self._imbalance_since: dict[str, float] = {}
        self._initialized = False
        self._metadata_verified_session = False
        self._metadata_trading_day = ""
        self._health_ready_since = 0.0

        self.journal = journal
        self.health_monitor = health_monitor or HealthMonitor(
            risk_manager.config.max_quote_age_seconds
        )
        self.portfolio_risk = portfolio_risk or PortfolioRiskAnalyzer()
        self.alerts = alert_manager or AlertManager()
        self.auto_manager = auto_manager
        self.quality = quality_recorder
        self._quality_pending: dict[str, dict] = {}
        self.require_live_metadata = require_live_metadata
        self.metadata_timeout_seconds = metadata_timeout_seconds
        self.historical_mode = historical_mode
        self.health_clock = health_clock or (lambda: datetime.now(timezone.utc))

    def start(self) -> None:
        """加载持久化状态、启动柜台并订阅所有套利腿。"""
        self.state = self.state_store.load()
        self._recent_trade_id_set = set(self.state.recent_trade_ids)
        self.risk_manager.restore_high_watermark(self.state.equity_high_watermark)
        self.halted = self.state.kill_switch

        for pair_id, strategy in self.strategies.items():
            saved = self.state.strategy_states.get(pair_id)
            if saved:
                strategy.restore_state(saved)

        self.broker.start()
        for pair in self.pairs.values():
            self.broker.subscribe(pair.near_symbol, pair.exchange)
            self.broker.subscribe(pair.far_symbol, pair.exchange)

        if self.broker.is_ready():
            self.initialize_after_ready()

    def initialize_after_ready(self) -> None:
        """柜台就绪后建立账户风险基线并执行实盘元数据安全门。"""
        if not self.broker.is_ready():
            raise RuntimeError("broker is not ready")

        if self.auto_manager is not None and not self.auto_manager.initialized:
            try:
                self._initialize_auto_universe()
            except Exception as exc:
                self._initialized = True
                self.emergency_stop(f"auto discovery initialization failed: {exc}")
                return

        account = self.broker.get_account()
        try:
            account.validate()
        except (TypeError, ValueError) as exc:
            self._initialized = True
            self.emergency_stop(f"invalid account snapshot: {exc}")
            return
        self._advance_trading_day(account)
        self._health_ready_since = monotonic()
        self.risk_manager.set_day_start_equity(
            self.state.day_start_equity or account.equity,
            self.state.trading_day or account.trading_day,
        )
        self.risk_manager.restore_high_watermark(self.state.equity_high_watermark)

        if self.require_live_metadata:
            metadata_decision = self._validate_live_metadata()
            if not metadata_decision.allowed:
                self.state.metadata_verified = False
                self._metadata_verified_session = False
                self.emergency_stop(metadata_decision.reason)
                self._initialized = True
                return
            self.state.metadata_verified = True
            self._metadata_verified_session = True
        else:
            self.state.metadata_verified = True
            self._metadata_verified_session = True

        self._metadata_trading_day = str(account.trading_day or "")
        decision = self.risk_manager.check_account(account)
        self.state.equity_high_watermark = self.risk_manager.high_watermark
        self._initialized = True
        if not decision.allowed:
            self.emergency_stop(decision.reason)
            return
        self._persist()

    def _validate_live_metadata(self) -> RiskDecision:
        """从柜台刷新静态合约参数；任何查询异常都按失败关闭处理。"""
        try:
            if not self._static_spec_symbols:
                return RiskDecision(True)
            configured = {symbol: self.specs[symbol] for symbol in self._static_spec_symbols}
            live_specs = self.broker.get_live_contract_specs(
                sorted(self._static_spec_symbols),
                self.metadata_timeout_seconds,
            )
            return validate_contract_metadata(configured, live_specs)
        except Exception as exc:
            return RiskDecision(False, f"live metadata query failed: {exc}")

    def stop(self) -> None:
        """保存期望状态、关闭 Auto 后台 worker，再关闭柜台。"""
        self._persist()
        if self.auto_manager is not None:
            self.auto_manager.close()
        self.broker.stop()

    def reconcile_startup(self) -> bool:
        """本地期望持仓必须与柜台完整快照一致才允许继续。"""
        remote = self.broker.get_positions()
        local = self.state_store.positions_from_state(self.state)
        result = compare_positions(local, remote)
        self.state.reconciled = result.matched
        if not result.matched:
            self.emergency_stop(f"position reconciliation failed: {result.details}")
            return False

        self.state.positions = [asdict(position) for position in remote]
        self._sync_strategy_positions(remote)
        self._persist()
        return True

    def clear_kill_switch_after_reconcile(self) -> bool:
        """只有元数据、持仓和账户风险全部通过后才能解除停机。"""
        if not self.broker.is_ready():
            return False
        if not self._initialized:
            self.initialize_after_ready()
        if not self._metadata_verified_session or not self.state.metadata_verified:
            return False
        if not self.reconcile_startup():
            return False
        if not self.state_store.can_clear_kill_switch(self.state):
            return False
        if not self.risk_manager.check_account(self.broker.get_account()).allowed:
            return False

        self.state.kill_switch = False
        self.state.kill_reason = ""
        self.state.runtime_mode = RuntimeMode.RUNNING.value
        self.state.reduce_reason = ""
        self.halted = False
        self._persist()
        return True

    def on_tick(self, tick: Tick) -> None:
        """处理单条行情并在所有前置门通过后生成交易动作。"""
        try:
            tick.validate()
            self.quotes[tick.symbol] = tick
            if self.auto_manager is not None:
                self.auto_manager.observe(tick)
            if self.halted or self.state.runtime_mode != RuntimeMode.RUNNING.value:
                return

            self._sync_strategy_positions()
            account = self.broker.get_account()
            decision = self.risk_manager.check_account(account)
            if not decision.allowed:
                self.emergency_stop(decision.reason)
                return

            self._refresh_auto_pairs(tick.timestamp)
            for pair_id, pair in list(self.pairs.items()):
                if tick.symbol not in {pair.near_symbol, pair.far_symbol}:
                    continue
                if self._pair_has_active_orders(pair_id):
                    continue

                near = self.quotes.get(pair.near_symbol)
                far = self.quotes.get(pair.far_symbol)
                if near is None or far is None:
                    continue

                quote_time = max(near.timestamp, far.timestamp)
                quote_decision = self.risk_manager.check_quotes([near, far], quote_time)
                if not quote_decision.allowed:
                    continue

                spread = near.mid_price - far.mid_price
                self.portfolio_risk.update(pair_id, spread, quote_time)
                # 一腿已经成交、另一腿未完成时，只允许余额审计/修复路径接管；
                # 不能让策略把“提交中的目标仓位”误当成真实双腿仓位后再生成新信号。
                if not self.executor.pair_is_balanced(pair):
                    continue
                strategy = self.strategies[pair_id]
                pre_signal_state = strategy.snapshot_state()
                signal = strategy.on_quotes(near, far)
                if signal.action is SignalAction.HOLD:
                    continue

                opening = signal.action in {
                    SignalAction.LONG_SPREAD,
                    SignalAction.SHORT_SPREAD,
                }
                if opening and not self._pair_open_eligible(pair_id):
                    strategy.restore_after_rejected_signal(pre_signal_state)
                    self._record(
                        "risk_reject",
                        {"pair": pair_id, "reason": "auto pair is managed but not open-eligible"},
                    )
                    continue
                if opening:
                    portfolio_decision = self.portfolio_risk.allow_open(
                        pair_id,
                        risk_group=pair.risk_group,
                        open_pairs=self._open_pair_groups(),
                    )
                    if not portfolio_decision.allowed:
                        strategy.restore_after_rejected_signal(pre_signal_state)
                        self._record(
                            "risk_reject",
                            {
                                "pair": pair_id,
                                "reason": portfolio_decision.reason,
                            },
                        )
                        continue

                self._record("signal", signal)
                result = self.executor.execute_signal(
                    pair,
                    signal,
                    near,
                    far,
                    open_pair_count=self._open_pair_count(),
                    spread_std=strategy.spread_std,
                    rate_limit_time=(
                        signal.timestamp.timestamp() if self.historical_mode else None
                    ),
                )
                self._record_quality_decision(pair, signal, near, far, result)
                if not result.accepted:
                    strategy.restore_after_rejected_signal(pre_signal_state)
                self._persist()

                if signal.action is SignalAction.EMERGENCY_EXIT and not result.accepted:
                    self.enter_reduce_only(f"emergency exit failed: {result.reason}")
        except Exception as exc:
            self.emergency_stop(f"engine exception: {exc}")

    def run_once(self) -> None:
        """处理一轮异步柜台事件，并执行健康/裸腿审计。"""
        if not self.broker.is_ready():
            if self._initialized and not self.halted:
                self.emergency_stop("broker session disconnected")
            return
        if not self._initialized:
            self.initialize_after_ready()

        health_error = self.broker.health_error()
        if health_error and not self.halted:
            self.emergency_stop(health_error)
            return

        for event in self.broker.poll_events():
            if event.event_type == "tick":
                self.on_tick(event.payload)
                continue
            if event.event_type == "trade":
                self._handle_trade_event(event.payload)
                continue
            if event.event_type == "order":
                self._handle_order_event(event.payload)
                continue
            if event.event_type == "position_snapshot":
                self._reconcile_runtime_snapshot(event.payload)
                continue
            if event.event_type == "account_error":
                self.emergency_stop(f"invalid account snapshot: {event.payload}")
                continue
            if event.event_type == "broker_error":
                self.emergency_stop(f"broker error: {event.payload}")
                continue
            if event.event_type == "account":
                self._handle_account_event(event.payload)

        if not self.halted and self.state.runtime_mode == RuntimeMode.RUNNING.value:
            health_reason = self._market_health_reason()
            if health_reason:
                self.emergency_stop(health_reason)
                return

        self._audit_pair_balance()
        if self.state.runtime_mode == RuntimeMode.REDUCE_ONLY.value:
            self._reduce_only_cycle()
        self._cleanup_retired_auto_pairs()

    def _handle_trade_event(self, trade) -> bool:
        if not isinstance(trade, Trade):
            self._record("trade", trade)
            self.emergency_stop("unrecognized trade detected; possible external account activity")
            return False
        try:
            trade.validate()
            trading_day = self._synchronize_trading_day_for_trade()
            identity = self._trade_identity(trade, trading_day)
        except (AttributeError, TypeError, ValueError) as exc:
            self.emergency_stop(f"invalid trade event: {exc}")
            return False
        legacy_identity = self._legacy_trade_identity(trade, trading_day)
        if identity in self._recent_trade_id_set or legacy_identity in self._recent_trade_id_set:
            return False
        if not self.broker.owns_order(trade.order_id):
            self._record("trade", trade)
            self.emergency_stop("unrecognized trade detected; possible external account activity")
            return False

        self._recent_trade_id_set.add(identity)
        self.state.recent_trade_ids.append(identity)
        if len(self.state.recent_trade_ids) > MAX_RECENT_TRADE_IDS:
            expired = self.state.recent_trade_ids.pop(0)
            self._recent_trade_id_set.discard(expired)
        self.state.last_trade_id = trade.trade_id
        try:
            self._apply_expected_trade(trade)
        except Exception as exc:
            self.emergency_stop(f"expected position update failed: {exc}")
            return False
        self._record("trade", trade)
        self._capture_quality_trade(trade)
        self._finalize_quality_if_flat(trade)
        self._audit_pair_balance()
        self._cleanup_retired_auto_pairs()
        return True

    def _synchronize_trading_day_for_trade(self) -> str:
        """Roll persisted state before applying the first fill reported for a new broker day."""
        account = self.broker.get_account()
        account.validate()
        get_trading_day = getattr(self.broker, "get_trading_day", None)
        reported_day = get_trading_day() if callable(get_trading_day) else account.trading_day
        trading_day = str(reported_day or account.trading_day or "")
        if not trading_day:
            raise ValueError("broker trading day is unavailable")
        if str(account.trading_day or "") != trading_day:
            account = replace(account, trading_day=trading_day)
        self._advance_trading_day(account)
        return trading_day

    @staticmethod
    def _trade_identity(trade: Trade, trading_day: str) -> str:
        return f"{trading_day}:{trade.exchange}:{trade.trade_id}"

    @staticmethod
    def _legacy_trade_identity(trade: Trade, trading_day: str) -> str:
        return f"{trading_day}:{trade.trade_id}"

    def _handle_order_event(self, order) -> None:
        self._record("order", order)
        if isinstance(order, Order):
            self.state.last_order_id = order.order_id
            if order.active and not self.broker.owns_order(order.order_id):
                self.emergency_stop(
                    "unrecognized active order detected; possible external account activity"
                )
                return
        self._persist()
        self._audit_pair_balance()

    def _handle_account_event(self, account) -> None:
        try:
            account.validate()
        except (AttributeError, TypeError, ValueError) as exc:
            self.emergency_stop(f"invalid account snapshot: {exc}")
            return
        previous_day = self._metadata_trading_day
        self._advance_trading_day(account)
        current_day = str(account.trading_day or "")
        if (
            self.require_live_metadata
            and current_day
            and previous_day
            and current_day != previous_day
        ):
            metadata_decision = self._validate_live_metadata()
            if not metadata_decision.allowed:
                self.state.metadata_verified = False
                self._metadata_verified_session = False
                self.emergency_stop(metadata_decision.reason)
                return
            self.state.metadata_verified = True
            self._metadata_verified_session = True
        if current_day:
            self._metadata_trading_day = current_day

        decision = self.risk_manager.check_account(account)
        self.state.equity_high_watermark = self.risk_manager.high_watermark
        self._persist()
        if not decision.allowed:
            self.emergency_stop(decision.reason)

    def _market_health_reason(self) -> str:
        """按运行模式检查行情健康：回放按事件门控，实盘使用墙钟时效。"""
        if not self.pairs:
            return ""

        reference = self._health_reference_time()
        if reference is None:
            return ""

        session_reference = reference.astimezone(_CHINA_TZ)
        active_pairs = [
            pair
            for pair in self.pairs.values()
            if not pair.session_windows
            or self.risk_manager.is_pair_session_active(pair, session_reference)
        ]
        if not active_pairs:
            return ""

        required = {
            symbol for pair in active_pairs for symbol in (pair.near_symbol, pair.far_symbol)
        }
        quotes_ready = required.issubset(self.quotes)
        if not quotes_ready and self._quote_initialization_grace_active():
            return ""

        # 历史事件流可能是分钟/日频抽样：某一腿的新样本先到时，另一腿下一条
        # 已存在于未来事件队列但尚未发布。此时不能把“回放采样间隔”误当成实时
        # 柜台 stale quote 并永久停机；每次真正交易前仍由 check_quotes 严格检查
        # quote age / cross-leg skew。实盘则继续用墙钟执行全局 stale 停机。
        max_quote_age: float
        if self.historical_mode:
            max_quote_age = 0.0
        else:
            observed_quote_age = self._max_quote_age(required, reference, quotes_ready)
            if observed_quote_age is None:
                return "market quote timestamp is in the future"
            max_quote_age = observed_quote_age

        try:
            self.broker.get_account()
            account_ready = True
        except Exception:
            account_ready = False
        try:
            self.broker.get_positions()
            position_ready = True
        except Exception:
            position_ready = False

        return self.health_monitor.evaluate(
            connected=self.broker.is_ready(),
            account_ready=account_ready,
            position_ready=position_ready,
            quotes_ready=quotes_ready,
            max_quote_age=max_quote_age,
        )

    def _health_reference_time(self) -> datetime | None:
        if self.historical_mode:
            if not self.quotes:
                return None
            return max(tick.timestamp for tick in self.quotes.values())

        reference = self.health_clock()
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        return reference

    def _quote_initialization_grace_active(self) -> bool:
        if self.historical_mode:
            return True
        grace = self.risk_manager.config.max_quote_age_seconds
        return bool(self._health_ready_since and monotonic() - self._health_ready_since <= grace)

    def _max_quote_age(
        self,
        required: set[str],
        reference: datetime,
        quotes_ready: bool,
    ) -> float | None:
        if not quotes_ready:
            return 0.0
        if self.historical_mode:
            oldest = min(self.quotes[symbol].timestamp for symbol in required)
            return max(0.0, (reference - oldest).total_seconds())

        ref_utc = reference.astimezone(timezone.utc)
        ages = [
            (ref_utc - self.quotes[symbol].timestamp.astimezone(timezone.utc)).total_seconds()
            for symbol in required
        ]
        if any(age < -2.0 for age in ages):
            return None
        return max(0.0, max(ages, default=0.0))

    def enter_reduce_only(self, reason: str) -> None:
        """进入只减仓模式，取消新增风险委托并持续修复裸腿。"""
        if self.state.runtime_mode == RuntimeMode.HALTED.value:
            return
        self.state.runtime_mode = RuntimeMode.REDUCE_ONLY.value
        self.state.reduce_reason = reason
        self.state.kill_switch = True
        self.state.kill_reason = reason
        self.halted = False
        for pending in self._quality_pending.values():
            pending["reduce_only"] = True
        for order in self.broker.get_active_orders():
            self.broker.cancel_order(order.order_id)
        self.alerts.critical("进入 REDUCE_ONLY", {"reason": reason})
        self._persist()

    def emergency_stop(self, reason: str) -> None:
        """取消活动委托并持久化停机；异常远端状态不会覆盖本地真相。"""
        self._record("emergency_stop", {"reason": reason})
        try:
            for order in self.broker.get_active_orders():
                self.broker.cancel_order(order.order_id)
        finally:
            self.halted = True
            self.state.kill_switch = True
            self.state.kill_reason = reason
            self.state.reconciled = False
            self.state.runtime_mode = RuntimeMode.HALTED.value
            self.alerts.critical("交易系统停机", {"reason": reason})
            self._persist()

    def _reduce_only_cycle(self) -> None:
        any_risk = False
        for pair in self.pairs.values():
            if self.executor.pair_is_balanced(pair):
                continue
            any_risk = True
            near = self.quotes.get(pair.near_symbol)
            far = self.quotes.get(pair.far_symbol)
            if self.auto_flatten_imbalance and near is not None and far is not None:
                try:
                    self.executor.flatten_imbalance(pair, near, far)
                except Exception as exc:
                    self.state.reduce_reason = f"{self.state.reduce_reason}; repair failed: {exc}"

        all_balanced = all(self.executor.pair_is_balanced(pair) for pair in self.pairs.values())
        if not any_risk or all_balanced:
            self.halted = True
            self.state.runtime_mode = RuntimeMode.HALTED.value
            self.state.kill_switch = True
            self.state.kill_reason = (
                self.state.reduce_reason or "reduce-only recovery completed; manual review required"
            )
            self._persist()

    def _audit_pair_balance(self) -> None:
        if self.halted or self.state.runtime_mode == RuntimeMode.REDUCE_ONLY.value:
            return
        now = self._imbalance_clock()
        for pair in self.pairs.values():
            if self.executor.pair_is_balanced(pair):
                self._imbalance_since.pop(pair.pair_id, None)
                continue
            first_seen = self._imbalance_since.setdefault(pair.pair_id, now)
            if now - first_seen >= self.legging_timeout_seconds:
                self.enter_reduce_only(f"pair imbalance detected: {pair.pair_id}")
                return

    def _imbalance_clock(self) -> float:
        """实盘按进程单调时钟；历史回放按最新可见市场事件时间。"""
        if self.historical_mode and self.quotes:
            return max(tick.timestamp for tick in self.quotes.values()).timestamp()
        return monotonic()

    def _apply_expected_trade(self, trade: Trade) -> None:
        book = PositionBook(self.state_store.positions_from_state(self.state))
        book.apply_trade(trade)
        self.state.positions = [asdict(position) for position in book.all()]
        self._sync_strategy_positions(book.all())
        self._persist()

    def _reconcile_runtime_snapshot(self, remote) -> None:
        if self.halted:
            return
        if not isinstance(remote, list) or any(
            not isinstance(position, ContractPosition) for position in remote
        ):
            self.emergency_stop("invalid position snapshot received from broker")
            return

        result = compare_positions(self.state_store.positions_from_state(self.state), remote)
        if not result.matched:
            self.emergency_stop(f"runtime position drift detected: {result.details}")
            return

        self.state.positions = [asdict(position) for position in remote]
        self.state.reconciled = True
        self._sync_strategy_positions(remote)
        self._persist()

    def _pair_has_active_orders(self, pair_id: str) -> bool:
        return any(
            order.request.reference.startswith(pair_id) for order in self.broker.get_active_orders()
        )

    def _pair_open_eligible(self, pair_id: str) -> bool:
        """管理权与开仓权分离：retiring Auto pair 可退出但不能重新开仓。"""
        return pair_id not in self._retiring_auto_pairs

    def _open_pair_count(self) -> int:
        return len(self._open_pair_groups())

    def _open_pair_groups(self) -> dict[str, str]:
        positions = {
            (position.symbol, position.exchange): position
            for position in self.broker.get_positions()
        }
        result: dict[str, str] = {}
        for pair_id, pair in self.pairs.items():
            near = positions.get((pair.near_symbol, pair.exchange))
            far = positions.get((pair.far_symbol, pair.exchange))
            if near and far and not near.empty and not far.empty:
                result[pair_id] = pair.risk_group
        return result

    def _sync_strategy_positions(self, positions: list[ContractPosition] | None = None) -> None:
        if positions is None:
            positions = self.state_store.positions_from_state(self.state)
        book = PositionBook(positions)

        for pair_id, pair in self.pairs.items():
            near = book.get(pair.near_symbol, pair.exchange)
            far = book.get(pair.far_symbol, pair.exchange)
            if near.empty and far.empty:
                position = 0
            elif (
                near.long_total == far.short_total > 0
                and near.short_total == 0
                and far.long_total == 0
            ):
                position = 1
            elif (
                near.short_total == far.long_total > 0
                and near.long_total == 0
                and far.short_total == 0
            ):
                position = -1
            else:
                continue
            self.strategies[pair_id].set_position(position)

    def _advance_trading_day(self, account) -> None:
        new_day = str(account.trading_day or "")
        old_day = str(self.state.trading_day or "")
        if new_day and old_day and new_day != old_day:
            book = PositionBook(self.state_store.positions_from_state(self.state))
            book.roll_trading_day()
            self.state.positions = [asdict(position) for position in book.all()]
            self._sync_strategy_positions(book.all())
            # A trade callback may be the first authoritative event for the new day.
            # Preserve identities already assigned to that day while dropping prior-day fills.
            prefix = f"{new_day}:"
            self.state.recent_trade_ids = [
                identity for identity in self.state.recent_trade_ids if identity.startswith(prefix)
            ]
            self._recent_trade_id_set = set(self.state.recent_trade_ids)

        if self.state.day_start_equity <= 0 or (new_day and new_day != old_day):
            self.state.day_start_equity = account.equity
        if new_day:
            self.state.trading_day = new_day

    def _initialize_auto_universe(self) -> None:
        """在 CTP 合约查询完成后初始化自动候选，并恢复持久化动态组合。"""
        assert self.auto_manager is not None
        today = self._trading_date()
        restored = self.auto_manager.bootstrap(self.broker, today, self.state.auto_pairs)
        for pair, pair_specs in restored:
            self._register_auto_pair(pair, pair_specs, seed_state=None, persist=False)

    def _trading_date(self) -> date:
        raw = self.broker.get_trading_day()
        if not isinstance(raw, str):
            raise ValueError("broker trading day must be a valid YYYYMMDD date")
        try:
            parsed = datetime.strptime(raw, "%Y%m%d")
        except ValueError as exc:
            raise ValueError("broker trading day must be a valid YYYYMMDD date") from exc
        if parsed.strftime("%Y%m%d") != raw:
            raise ValueError("broker trading day must be a valid YYYYMMDD date")
        return parsed.date()

    def _refresh_auto_pairs(self, now: datetime) -> None:
        if self.auto_manager is None or not self.auto_manager.initialized:
            return
        protected = self._open_auto_pair_ids()
        try:
            self.auto_manager.refresh_if_needed(
                self.broker,
                self._trading_date(),
                retained_pairs=[
                    self.pairs[pair_id] for pair_id in sorted(protected) if pair_id in self.pairs
                ],
            )
            selected = self.auto_manager.select(self.broker, now=now, protected_pair_ids=protected)
        except Exception as exc:
            # 当日目录或扫描不可确认时，旧的无持仓 Auto pair 不再拥有开仓权。
            # 已有持仓 pair 仍保留管理/退出权限，等待下一次成功刷新恢复候选资格。
            self._retiring_auto_pairs.update(self._auto_pair_ids - protected)
            self._record("auto_scan_error", {"reason": str(exc)})
            return
        if selected is None:
            return

        selected_ids = {pair.pair_id for pair in selected}
        eligible_ids = set(self.auto_manager.last_eligible_ids)
        for pair_id in protected:
            if pair_id not in eligible_ids:
                self._retiring_auto_pairs.add(pair_id)
        for pair in selected:
            if pair.pair_id in eligible_ids:
                self._retiring_auto_pairs.discard(pair.pair_id)
            elif pair.pair_id in protected:
                self._retiring_auto_pairs.add(pair.pair_id)
            if pair.pair_id in self.pairs:
                try:
                    self.specs.update(self.auto_manager.pair_specs(pair))
                except KeyError:
                    pass
                continue
            if pair.pair_id not in eligible_ids:
                continue
            self._register_auto_pair(
                pair,
                self.auto_manager.pair_specs(pair),
                seed_state=self.auto_manager.strategy_seed(pair),
            )

        for pair_id in list(self._auto_pair_ids):
            if pair_id in selected_ids or pair_id in protected:
                continue
            self._unregister_auto_pair(pair_id)

    def _register_auto_pair(
        self,
        pair: PairConfig,
        pair_specs: dict[str, ContractSpec],
        *,
        seed_state: dict | None,
        persist: bool = True,
    ) -> None:
        self.specs.update(pair_specs)
        self.pairs[pair.pair_id] = pair
        strategy = CalendarSpreadStrategy(pair)
        saved = self.state.strategy_states.get(pair.pair_id)
        if saved:
            strategy.restore_state(saved)
        elif seed_state:
            strategy.restore_state(seed_state)
        self.strategies[pair.pair_id] = strategy
        self._auto_pair_ids.add(pair.pair_id)
        self.broker.subscribe(pair.near_symbol, pair.exchange)
        self.broker.subscribe(pair.far_symbol, pair.exchange)
        if persist:
            self._persist()

    def _unregister_auto_pair(self, pair_id: str) -> None:
        pair = self.pairs.get(pair_id)
        if pair is None or self._pair_has_position(pair):
            return
        if self._pair_has_active_orders(pair_id):
            return
        self.pairs.pop(pair_id, None)
        self.strategies.pop(pair_id, None)
        self._auto_pair_ids.discard(pair_id)
        self._retiring_auto_pairs.discard(pair_id)
        self.state.strategy_states.pop(pair_id, None)
        self.state.auto_pairs.pop(pair_id, None)
        self._persist()

    def _pair_has_position(self, pair: PairConfig) -> bool:
        positions = {
            (position.symbol, position.exchange): position
            for position in self.broker.get_positions()
        }
        near = positions.get((pair.near_symbol, pair.exchange))
        far = positions.get((pair.far_symbol, pair.exchange))
        return bool((near is not None and not near.empty) or (far is not None and not far.empty))

    def _open_auto_pair_ids(self) -> set[str]:
        return {
            pair_id
            for pair_id in self._auto_pair_ids
            if pair_id in self.pairs and self._pair_has_position(self.pairs[pair_id])
        }

    def _cleanup_retired_auto_pairs(self) -> None:
        for pair_id in list(self._retiring_auto_pairs):
            self._unregister_auto_pair(pair_id)

    def _record_quality_decision(self, pair, signal, near, far, result) -> None:
        if self.quality is None:
            return
        expected_edge = 0.0
        expected_spread = float(signal.spread)
        if (
            signal.action in {SignalAction.LONG_SPREAD, SignalAction.SHORT_SPREAD}
            and result.volume > 0
        ):
            try:
                edge = estimate_net_edge(
                    signal.action,
                    reference_mean=signal.reference_mean,
                    near=near,
                    far=far,
                    specs=self.specs,
                    volume=result.volume,
                    slippage_ticks=self.slippage_ticks,
                    legging_buffer=pair.legging_buffer,
                )
                expected_edge = float(edge.net_edge)
                expected_spread = float(edge.executable_spread)
            except Exception as exc:
                logger.warning(
                    "execution-quality estimate failed for pair=%s: %s",
                    pair.pair_id,
                    exc,
                )
        self.quality.record_decision(
            pair_id=pair.pair_id,
            action=signal.action.value,
            zscore=float(signal.zscore),
            accepted=bool(result.accepted),
            reject_reason=result.reason,
            volume=int(result.volume),
            expected_net_edge=expected_edge,
            expected_spread=expected_spread,
        )
        if result.accepted and signal.action in {
            SignalAction.LONG_SPREAD,
            SignalAction.SHORT_SPREAD,
        }:
            self._quality_pending[pair.pair_id] = {
                "pair": pair,
                "action": signal.action,
                "expected_net_edge": expected_edge,
                "expected_spread": expected_spread,
                "volume": result.volume,
                "trades": [],
                "reduce_only": False,
            }

    def _quality_commission(self, trade: Trade) -> tuple[float, str]:
        """优先使用 Broker 回报；CTP 无单笔手续费时用已验证费率表估算。"""
        if float(trade.commission) > 0:
            return float(trade.commission), "broker_trade"
        spec = self.specs.get(trade.symbol)
        if spec is None:
            return 0.0, "unavailable"
        return (
            float(calculate_commission(spec, trade.offset, trade.price, trade.volume)),
            "verified_fee_schedule",
        )

    def _capture_quality_trade(self, trade: Trade) -> None:
        if self.quality is None:
            return
        order = self.broker.get_order(trade.order_id)
        if order is None:
            return
        pair_id = str(order.request.reference).split(":", 1)[0]
        pending = self._quality_pending.get(pair_id)
        if pending is None:
            return
        commission, commission_source = self._quality_commission(trade)
        pending["trades"].append(
            {
                "symbol": trade.symbol,
                "offset": trade.offset,
                "side": trade.side,
                "volume": trade.volume,
                "price": trade.price,
                "commission": commission,
                "commission_source": commission_source,
                "timestamp": trade.timestamp,
                "reference": order.request.reference,
            }
        )
        if ":rollback" in order.request.reference or ":repair" in order.request.reference:
            pending["rollback"] = True

    def _finalize_quality_if_flat(self, trade: Trade) -> None:
        if self.quality is None:
            return
        order = self.broker.get_order(trade.order_id)
        if order is None:
            return
        pair_id = str(order.request.reference).split(":", 1)[0]
        pending = self._quality_pending.get(pair_id)
        pair = self.pairs.get(pair_id)
        if pending is None or pair is None or self._pair_has_position(pair):
            return
        rows = pending.get("trades", [])
        opens = [row for row in rows if row["offset"] is Offset.OPEN]
        closes = [row for row in rows if row["offset"] is not Offset.OPEN]
        if not opens or not closes:
            return

        def weighted(symbol: str, items: list[dict]) -> float:
            selected = [row for row in items if row["symbol"] == symbol]
            volume = sum(int(row["volume"]) for row in selected)
            return (
                sum(float(row["price"]) * int(row["volume"]) for row in selected) / volume
                if volume > 0
                else 0.0
            )

        entry_spread = weighted(pair.near_symbol, opens) - weighted(pair.far_symbol, opens)
        exit_spread = weighted(pair.near_symbol, closes) - weighted(pair.far_symbol, closes)
        multiplier = min(
            self.specs[pair.near_symbol].multiplier,
            self.specs[pair.far_symbol].multiplier,
        )
        volume = max(1, int(pending.get("volume", 1)))
        if pending["action"] is SignalAction.LONG_SPREAD:
            gross = (exit_spread - entry_spread) * multiplier * volume
        else:
            gross = (entry_spread - exit_spread) * multiplier * volume
        commission = sum(float(row.get("commission", 0.0)) for row in rows)
        commission_sources = sorted({str(row.get("commission_source", "unknown")) for row in rows})
        open_times = sorted(row["timestamp"] for row in opens)
        leg_latency_ms = 0.0
        if len(open_times) >= 2:
            leg_latency_ms = (open_times[1] - open_times[0]).total_seconds() * 1000.0
        open_by_symbol = {
            symbol: sum(int(row["volume"]) for row in opens if row["symbol"] == symbol)
            for symbol in (pair.near_symbol, pair.far_symbol)
        }
        partial = (
            len(set(open_by_symbol.values())) > 1
            or min(open_by_symbol.values(), default=0) < volume
        )
        self.quality.record_round_trip(
            pair_id=pair_id,
            expected_net_edge=float(pending.get("expected_net_edge", 0.0)),
            realized_net_edge=gross - commission,
            expected_spread=float(pending.get("expected_spread", entry_spread)),
            entry_spread=entry_spread,
            exit_spread=exit_spread,
            commission=commission,
            leg_latency_ms=leg_latency_ms,
            partial_fill=partial,
            rollback=bool(pending.get("rollback", False)),
            reduce_only=bool(pending.get("reduce_only", False)),
            extra={"commission_sources": commission_sources},
        )
        self._quality_pending.pop(pair_id, None)

    def _record(self, event_type: str, payload: object) -> None:
        if self.journal is not None:
            self.journal.record(event_type, payload)

    def _persist(self) -> None:
        self.state.strategy_states = {
            pair_id: strategy.snapshot_state() for pair_id, strategy in self.strategies.items()
        }
        if self.auto_manager is not None:
            self.state.auto_pairs = {
                pair_id: asdict(self.pairs[pair_id])
                for pair_id in sorted(self._auto_pair_ids)
                if pair_id in self.pairs
            }
        try:
            account = self.broker.get_account()
            account.validate()
            self._advance_trading_day(account)
            self.risk_manager.check_account(account)
            self.state.equity_high_watermark = self.risk_manager.high_watermark
        except Exception as exc:
            logger.warning("account refresh during state persistence failed: %s", exc)
        self.state_store.save(self.state)
