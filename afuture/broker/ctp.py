"""基于 VeighNa ``vnpy_ctp`` 的 CTP 柜台适配器。

个人期货账户通常通过期货公司的 CTP 交易/行情前置接入交易所。
模块在运行实盘命令时才导入二进制依赖，研究和测试环境不需要安装 CTP。
"""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from math import isfinite
from threading import Event, Lock, RLock
from time import monotonic, sleep
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

from ..models import (
    AccountSnapshot,
    BrokerEvent,
    ContractInfo,
    ContractPosition,
    ContractSpec,
    FeeSpec,
    Offset,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Tick,
    Trade,
)
from ..position import PositionBook
from .base import Broker, RawMarketEvidenceError
from .ctp_catalog import (
    CtpContractCatalogAccumulator,
    CtpContractCatalogIntegrityError,
    CtpContractCatalogSnapshot,
)
from .ctp_order_journal import (
    CTP_ORDER_RUNTIME_MAX_FILL_IDENTITIES,
    CtpOrderFillEvidence,
    CtpOrderSubmissionContext,
    CtpOrderSubmissionEntry,
    CtpOrderSubmissionJournal,
    CtpOrderSubmissionJournalRecord,
    ctp_order_fill_evidence,
)
from .ctp_session_query import (
    CtpSessionActivityEvidence,
    CtpSessionOrder,
    CtpSessionQueryAccumulator,
    CtpSessionQueryIntegrityError,
    CtpSessionTrade,
    build_ctp_session_activity_evidence,
    plan_ctp_session_journal_recovery,
)
from .ctp_settlement_query import CtpSettlementDocument, CtpSettlementQuery
from .ctp_snapshot_query import CtpSnapshotQuery, CtpSnapshotQueryError

_CHINA = ZoneInfo("Asia/Shanghai")


class _RateWaiter(TypedDict):
    kind: str
    event: Event
    rows: list[dict[str, Any]]
    error: str


@dataclass(frozen=True)
class CtpCredentials:
    """CTP 连接参数。敏感字段从环境变量注入。"""

    user_id: str
    password: str
    broker_id: str
    td_address: str
    md_address: str
    app_id: str
    auth_code: str
    environment: str = "test"
    account_id: str = ""
    currency_id: str = ""
    investor_id: str = ""
    invest_unit_id: str = ""

    def __post_init__(self) -> None:
        identity_values = (
            self.account_id,
            self.currency_id,
            self.investor_id,
            self.invest_unit_id,
        )
        if any(not isinstance(value, str) or value.strip() != value for value in identity_values):
            raise ValueError("CTP expected account identity values must be trimmed strings")
        if bool(self.account_id) != bool(self.currency_id):
            raise ValueError("CTP expected AccountID and CurrencyID must be configured together")
        if (self.investor_id or self.invest_unit_id) and not self.account_id:
            raise ValueError("CTP investor identity requires expected AccountID and CurrencyID")


@dataclass(frozen=True)
class _CtpAccountEvidence:
    broker_id: str
    account_id: str
    currency_id: str
    investor_id: str
    invest_unit_id: str
    trading_day: str
    deposit: float
    withdrawal: float
    pre_balance: float
    settlement_id: int
    generation: int


@dataclass(frozen=True)
class _Stress90SessionStartupCapability:
    account_identity_digest: str
    trading_day: str
    critical_generation: int
    query_ingress_generation: int
    evidence_digest: str
    ownership_digest: str
    consumed: bool = False


class _CriticalSubmissionBoundaryError(RuntimeError):
    """A critical callback invalidated the final pre-send boundary."""


def build_ctp_setting(credentials: CtpCredentials) -> dict[str, str]:
    """构造当前 ``CtpGateway`` 使用的中文配置键。"""
    return {
        "用户名": credentials.user_id,
        "密码": credentials.password,
        "经纪商代码": credentials.broker_id,
        "交易服务器": credentials.td_address,
        "行情服务器": credentials.md_address,
        "产品名称": credentials.app_id,
        "授权编码": credentials.auth_code,
        "柜台环境": "测试" if credentials.environment.lower() == "test" else "实盘",
    }


class CtpBroker(Broker):
    """把 VeighNa CTP 对象转换为 afuture 内部模型。"""

    gateway_name = "CTP"
    _MAX_SEEN_TRADE_KEYS = CTP_ORDER_RUNTIME_MAX_FILL_IDENTITIES
    _MAX_PENDING_ACCOUNT_EVIDENCE = 32

    def __init__(
        self,
        credentials: CtpCredentials,
        *,
        snapshot_stale_seconds: float = 20.0,
        max_events_per_poll: int = 100,
    ) -> None:
        if snapshot_stale_seconds <= 0:
            raise ValueError("snapshot_stale_seconds must be positive")
        if (
            isinstance(max_events_per_poll, bool)
            or not isinstance(max_events_per_poll, int)
            or max_events_per_poll <= 0
        ):
            raise ValueError("max_events_per_poll must be a positive integer")
        self.credentials = credentials
        self.snapshot_stale_seconds = snapshot_stale_seconds
        self.max_events_per_poll = max_events_per_poll
        self._runtime: dict[str, Any] | None = None
        # VeighNa is an optional runtime dependency.  Keep its dynamic objects at
        # the adapter boundary instead of leaking ``Any`` into domain models.
        self._event_engine: Any | None = None
        self._main_engine: Any | None = None
        self._critical_events: deque[BrokerEvent] = deque()
        self._latest_ticks: dict[tuple[str, str], BrokerEvent] = {}
        self._event_lock = Lock()
        # Lock order is position -> event whenever both are needed. Event polling never
        # acquires the position lock, so snapshot/trade state and their events stay ordered.
        self._position_lock = Lock()
        self._critical_enqueued = 0
        self._ticks_received = 0
        self._ticks_coalesced = 0
        self._critical_delivered = 0
        self._ticks_delivered = 0
        self._order_references: dict[str, str] = {}
        self._authorized_order_reference_prefixes: tuple[str, ...] = ()
        self._order_submission_lock = Lock()
        self._order_submission_status_lock = Lock()
        self._critical_barrier_lock = RLock()
        self._critical_upstream_pending = 0
        self._critical_callbacks_inflight = 0
        self._order_trade_upstream_pending = 0
        self._order_trade_callbacks_inflight = 0
        self._critical_acknowledged = 0
        self._critical_submission_active = False
        self._lifecycle_commit_fence_active = False
        self._order_submission_journal: CtpOrderSubmissionJournal | None = None
        self._order_submission_context: CtpOrderSubmissionContext | None = None
        self._candidate_order_authorizations: dict[OrderRequest, int] = {}
        self._order_submission_entries: dict[str, CtpOrderSubmissionEntry] = {}
        self._pending_order_journal_status: dict[str, str] = {}
        self._pending_order_journal_fills: dict[str, dict[str, CtpOrderFillEvidence]] = {}
        self._order_journal_policy_identity: tuple[str, str, str] | None = None
        self._stress90_session_startup_required = False
        self._stress90_session_startup_capability: _Stress90SessionStartupCapability | None = None
        self._last_account: AccountSnapshot | None = None
        self._raw_market_connection_lock = Lock()
        self._raw_market_connection_generation = 0
        self._raw_market_connected = False
        self._positions: dict[tuple[str, str], ContractPosition] = {}
        self._trading_day = ""
        self._account_event_generation = 0
        self._position_snapshot_generation = 0
        self._last_account_monotonic = 0.0
        self._last_position_snapshot_monotonic = 0.0
        self._contract_catalog: dict[str, ContractInfo] = {}
        self._contract_catalog_accumulator = CtpContractCatalogAccumulator()
        self._contract_catalog_refresh_lock = Lock()
        self._contract_catalog_refresh_state_lock = Lock()
        self._contract_catalog_refresh_event: Event | None = None
        self._contract_catalog_refresh_request_id: int | None = None
        self._contract_catalog_refresh_error = ""
        # A response that cannot belong to the registered query generation is a
        # protocol-integrity failure, not a transient query failure.  Keep the
        # first such failure sticky so an older verified cache cannot make the
        # broker appear healthy after a late/duplicate callback.
        self._contract_catalog_sticky_error = ""
        # VeighNa's automatic startup instrument query has no afuture call site at
        # which to register its request id. Exactly one first callback may establish
        # that bootstrap generation. Every later generation must be registered before
        # its first callback, so aborted/completed ids cannot be resurrected.
        self._contract_catalog_bootstrap_registration_open = True
        self._ctp_query_request_lock = Lock()
        self._session_refresh_lock = Lock()
        self._settlement_query = CtpSettlementQuery()
        self._snapshot_query_error = ""
        self._session_query_ingress_lock = RLock()
        self._session_query_state_lock = Lock()
        self._session_query_accumulators = {
            "order": CtpSessionQueryAccumulator("order"),
            "trade": CtpSessionQueryAccumulator("trade"),
        }
        self._session_query_events: dict[str, Event] = {}
        self._session_query_results: dict[str, tuple[CtpSessionOrder | CtpSessionTrade, ...]] = {}
        self._session_query_errors: dict[str, str] = {}
        self._session_query_request_ids: dict[str, int] = {}
        self._session_query_sticky_error = ""
        self._session_query_callbacks_inflight = 0
        self._session_query_ingress_generation = 0
        self._recovered_session_evidence: CtpSessionActivityEvidence | None = None
        self._recovered_session_trades: tuple[Trade, ...] = ()
        self._recovered_session_orders: dict[str, Order] = {}
        self._order_trade_generation_lock = Lock()
        self._order_trade_generation = 0
        self._account_lock = Lock()
        self._account_cash_flow: dict[str, _CtpAccountEvidence] = {}
        self._account_cash_flow_error: dict[str, str] = {}
        self._account_evidence_generation = 0
        self._paired_account_evidence_generation = 0
        self._account_evidence_pair_pending = False
        self._pending_account_evidence: deque[_CtpAccountEvidence] = deque()
        self._last_verified_account_evidence_day = ""
        self._account_identity_verified_state = False
        self._account_identity_verified_day = ""
        self._seen_trade_keys: set[str] = set()
        self._seen_trade_order: deque[str] = deque()
        self._seen_trade_fingerprints: dict[str, CtpOrderFillEvidence] = {}
        self._durable_trade_fingerprints: dict[str, CtpOrderFillEvidence] = {}
        self._external_trade_keys: set[str] = set()
        self._external_trade_order: deque[str] = deque()

    def _mark_order_trade_activity(self) -> None:
        with self._order_trade_generation_lock:
            self._order_trade_generation += 1

    def _current_order_trade_generation(self) -> int:
        with self._order_trade_generation_lock:
            return self._order_trade_generation

    @contextmanager
    def _critical_callback_scope(
        self,
        *,
        consumes_upstream: bool = False,
        order_trade: bool = False,
    ) -> Iterator[None]:
        """Publish callback-in-flight state before any economic conversion starts."""

        with self._critical_barrier_lock:
            if consumes_upstream and self._critical_upstream_pending:
                self._critical_upstream_pending -= 1
            if consumes_upstream and order_trade and self._order_trade_upstream_pending:
                self._order_trade_upstream_pending -= 1
            self._critical_callbacks_inflight += 1
            if order_trade:
                self._order_trade_callbacks_inflight += 1
        try:
            yield
        finally:
            with self._critical_barrier_lock:
                self._critical_callbacks_inflight -= 1
                if order_trade:
                    self._order_trade_callbacks_inflight -= 1

    def _mark_critical_upstream_pending(self, kind: str = "") -> None:
        """Expose a gateway event before VeighNa queues it for handler dispatch."""

        with self._critical_barrier_lock:
            self._critical_upstream_pending += 1
            if kind in {"order", "trade"}:
                self._order_trade_upstream_pending += 1
                self._mark_order_trade_activity()

    def _quiescent_order_trade_generation(self) -> int:
        """Return the ingress generation only at a callback-quiescent boundary."""

        with self._critical_barrier_lock:
            if self._order_trade_upstream_pending or self._order_trade_callbacks_inflight:
                raise RuntimeError("CTP order/trade callbacks are not quiescent")
            return self._current_order_trade_generation()

    def acknowledge_critical_events(self) -> bool:
        """Acknowledge a fully consumed, callback-quiescent critical FIFO boundary."""

        if self._order_submission_journal is None:
            return True
        with self._critical_barrier_lock:
            if self._account_evidence_pair_incomplete():
                return False
            with self._event_lock:
                if (
                    self._critical_upstream_pending
                    or self._critical_callbacks_inflight
                    or self._critical_events
                ):
                    return False
                self._critical_acknowledged = self._critical_enqueued
                return True

    def _require_acknowledged_critical_boundary(self) -> None:
        if self._order_submission_journal is None:
            return
        with self._critical_barrier_lock:
            if self._account_evidence_pair_incomplete():
                raise _CriticalSubmissionBoundaryError(
                    "CTP order submission requires an acknowledged critical event boundary"
                )
            with self._event_lock:
                if (
                    self._critical_callbacks_inflight
                    or self._critical_upstream_pending
                    or self._critical_events
                    or self._critical_acknowledged != self._critical_enqueued
                ):
                    raise _CriticalSubmissionBoundaryError(
                        "CTP order submission requires an acknowledged critical event boundary"
                    )

    @contextmanager
    def _critical_order_submission_scope(self) -> Iterator[None]:
        """Make the final FIFO check atomic with the short official send call.

        Journal fsync deliberately happens before this scope. Callback threads therefore
        never wait for disk IO, but they cannot enter between the second check and the
        actual gateway call. A same-thread callback is treated as an interrupt and raises
        before a test/adapter can continue to the external send boundary.
        """

        if self._order_submission_journal is None:
            yield
            return
        with self._critical_barrier_lock:
            with self._session_query_ingress_lock:
                if self._account_evidence_pair_incomplete():
                    raise _CriticalSubmissionBoundaryError(
                        "CTP order submission requires an acknowledged critical event boundary"
                    )
                with self._event_lock:
                    if (
                        self._critical_callbacks_inflight
                        or self._critical_upstream_pending
                        or self._critical_events
                        or self._critical_acknowledged != self._critical_enqueued
                    ):
                        raise _CriticalSubmissionBoundaryError(
                            "CTP order submission requires an acknowledged critical event boundary"
                        )
                with self._session_query_state_lock:
                    session_query_invalid = bool(
                        self._stress90_session_startup_required
                        and (
                            self._session_query_callbacks_inflight
                            or self._session_query_sticky_error
                        )
                    )
                if session_query_invalid:
                    raise _CriticalSubmissionBoundaryError(
                        "CTP session query changed at the official submission boundary"
                    )
                if self._critical_submission_active:
                    raise RuntimeError("CTP order submission boundary is already active")
                self._critical_submission_active = True
                try:
                    yield
                finally:
                    self._critical_submission_active = False

    @contextmanager
    def lifecycle_state_commit_fence(self) -> Iterator[None]:
        """Linearize a short HALTED lifecycle commit before new critical ingress."""

        with self._critical_barrier_lock:
            with self._session_query_ingress_lock:
                with self._event_lock:
                    if (
                        self._critical_upstream_pending
                        or self._critical_callbacks_inflight
                        or self._critical_events
                        or self._critical_submission_active
                    ):
                        raise RuntimeError(
                            "CTP lifecycle commit requires a quiescent critical boundary"
                        )
                if self._account_evidence_pair_incomplete():
                    raise RuntimeError("CTP lifecycle commit account evidence pair is incomplete")
                with self._session_query_state_lock:
                    if self._session_query_callbacks_inflight or self._session_query_sticky_error:
                        raise RuntimeError("CTP lifecycle commit session evidence is not quiescent")
                if self._lifecycle_commit_fence_active:
                    raise RuntimeError("CTP lifecycle commit fence is already active")
                self._lifecycle_commit_fence_active = True
                try:
                    yield
                finally:
                    self._lifecycle_commit_fence_active = False

    def _account_evidence_pair_incomplete(self) -> bool:
        return bool(self._has_expected_account_identity and self._account_evidence_pair_pending)

    def seed_trade_identities(self, identities: list[str]) -> None:
        """Restore durable, exchange-qualified fill identities before callbacks start."""
        if self._main_engine is not None or self._event_engine is not None:
            raise RuntimeError("CTP trade identities must be seeded before broker start")

        qualified: list[str] = []
        for identity in identities:
            if not isinstance(identity, str):
                continue
            parts = identity.split(":", 2)
            if len(parts) != 3:
                continue
            trading_day, exchange, trade_id = parts
            try:
                parsed_day = datetime.strptime(trading_day, "%Y%m%d")
            except ValueError:
                continue
            if (
                parsed_day.strftime("%Y%m%d") != trading_day
                or not re.fullmatch(r"[A-Z][A-Z0-9]*", exchange)
                or not trade_id
            ):
                continue
            qualified.append(identity)

        if self._order_submission_journal is not None:
            next_external_order = list(self._external_trade_order)
            next_external_keys = set(self._external_trade_keys)
            for identity in qualified:
                if identity not in next_external_keys:
                    next_external_keys.add(identity)
                    next_external_order.append(identity)
            qualified = [*next_external_order, *self._durable_trade_fingerprints]
        else:
            next_external_order = []
            next_external_keys = set()
            qualified.extend(self._seen_trade_order)
        combined = list(dict.fromkeys(qualified))
        if self._order_submission_journal is not None and len(combined) > self._MAX_SEEN_TRADE_KEYS:
            raise RuntimeError(
                "durable CTP fill identities exceed Broker replay capacity; "
                "HALTED journal epoch rollover is required"
            )
        bounded = combined[-self._MAX_SEEN_TRADE_KEYS :]
        if self._order_submission_journal is not None:
            self._external_trade_keys = next_external_keys
            self._external_trade_order = deque(next_external_order)
        self._seen_trade_keys = set(bounded)
        self._seen_trade_order = deque(bounded)
        self._seen_trade_fingerprints = {
            key: evidence
            for key, evidence in {
                **self._seen_trade_fingerprints,
                **self._durable_trade_fingerprints,
            }.items()
            if key in self._seen_trade_keys
        }

    def _decode_order_submission_runtime_record(
        self,
        record: CtpOrderSubmissionJournalRecord | None,
        identities: tuple[str, str, str],
        account_digest: str,
    ) -> tuple[
        dict[str, CtpOrderSubmissionEntry],
        dict[str, CtpOrderFillEvidence],
        list[str],
    ]:
        entries = {} if record is None else {entry.order_id: entry for entry in record.all_entries}
        for entry in entries.values():
            if (
                entry.account_identity_digest != account_digest
                or (
                    entry.policy_id,
                    entry.policy_definition_digest,
                    entry.products_manifest_digest,
                )
                != identities
            ):
                raise RuntimeError("CTP order journal account/policy identity mismatch")
        durable_fill_keys = [fill_key for entry in entries.values() for fill_key in entry.fill_keys]
        durable_fill_evidence = {
            evidence.key: evidence for entry in entries.values() for evidence in entry.fill_evidence
        }
        if len(durable_fill_keys) > self._MAX_SEEN_TRADE_KEYS:
            raise RuntimeError(
                "durable CTP fill identities exceed Broker replay capacity; "
                "HALTED journal epoch rollover is required"
            )
        if len(durable_fill_keys) != len(set(durable_fill_keys)) or set(
            durable_fill_evidence
        ) != set(durable_fill_keys):
            raise RuntimeError("CTP order journal fill fingerprint evidence is incomplete")
        combined = list(dict.fromkeys((*self._external_trade_order, *durable_fill_keys)))
        reserved_remaining = sum(
            entry.request.volume - entry.filled_volume
            for entry in entries.values()
            if entry.status != "aborted_before_send"
        )
        if len(combined) + reserved_remaining > self._MAX_SEEN_TRADE_KEYS:
            raise RuntimeError(
                "durable CTP fill identity reservation exceeds Broker replay capacity; "
                "HALTED journal epoch rollover is required"
            )
        return entries, durable_fill_evidence, combined

    def _install_order_submission_runtime_record(
        self,
        record: CtpOrderSubmissionJournalRecord | None,
        identities: tuple[str, str, str],
        account_digest: str,
    ) -> None:
        entries, durable_fill_evidence, combined = self._decode_order_submission_runtime_record(
            record,
            identities,
            account_digest,
        )
        self._order_submission_entries = entries
        self._order_references = {
            order_id: entry.request.reference for order_id, entry in entries.items()
        }
        self._durable_trade_fingerprints = durable_fill_evidence
        self._seen_trade_keys = set(combined)
        self._seen_trade_order = deque(combined)
        self._seen_trade_fingerprints = {
            key: evidence
            for key, evidence in durable_fill_evidence.items()
            if key in self._seen_trade_keys
        }
        if not set(durable_fill_evidence).issubset(self._seen_trade_keys):
            raise RuntimeError("CTP order journal durable fill identities were truncated")

    def _load_runtime(self) -> dict[str, Any]:
        """延迟加载实盘依赖，并扩展完整持仓快照和费率查询回调。"""
        if self._runtime is not None:
            return self._runtime
        try:
            from vnpy.event import EventEngine
            from vnpy.trader.constant import Direction, Exchange, Status
            from vnpy.trader.constant import Offset as VnOffset
            from vnpy.trader.constant import OrderType as VnOrderType
            from vnpy.trader.engine import MainEngine
            from vnpy.trader.event import EVENT_ACCOUNT, EVENT_ORDER, EVENT_TICK, EVENT_TRADE
            from vnpy.trader.object import OrderRequest as VnOrderRequest
            from vnpy.trader.object import SubscribeRequest
            from vnpy_ctp.gateway.ctp_gateway import CtpGateway, CtpMdApi, CtpTdApi
        except ImportError as exc:
            raise RuntimeError(
                "CTP live dependencies are missing; install with: pip install -e '.[live]'"
            ) from exc

        domain_broker = self

        class TrackedCtpTdApi(CtpTdApi):
            """在官方交易 API 上增加查询完成边界，不改动官方下单逻辑。"""

            def __init__(self, gateway):
                super().__init__(gateway)
                self._afuture_rate_waiters: dict[int, _RateWaiter] = {}
                self._afuture_expected_order_ref: int | None = None
                self._afuture_request_id_lock = RLock()
                self._afuture_snapshot_callback_lock = RLock()

                def snapshot_query(kind: str) -> CtpSnapshotQuery:
                    return CtpSnapshotQuery(
                        kind,
                        broker_id=domain_broker.credentials.broker_id,
                        investor_id=domain_broker.credentials.investor_id
                        or domain_broker.credentials.user_id,
                        account_id=domain_broker.credentials.account_id,
                        currency_id=domain_broker.credentials.currency_id,
                        timeout_seconds=domain_broker.snapshot_stale_seconds,
                    )

                self._afuture_account_query = snapshot_query("account")
                self._afuture_position_query = snapshot_query("position")

            def _afuture_allocate_request_id(self) -> int:
                with self._afuture_request_id_lock:
                    request_id = int(getattr(self, "reqid", 0)) + 1
                    if request_id <= 0:
                        raise RuntimeError("CTP query request id is invalid")
                    self.reqid = request_id
                    return request_id

            def send_order(self, req):
                with self._afuture_request_id_lock:
                    expected = self._afuture_expected_order_ref
                    before = int(self.order_ref)
                    if expected is not None and before + 1 != expected:
                        raise RuntimeError(
                            "reserved CTP OrderRef no longer matches official counter"
                        )
                    result = super().send_order(req)
                    if expected is not None and int(self.order_ref) != expected:
                        raise RuntimeError(
                            "official vnpy_ctp did not consume reserved OrderRef exactly"
                        )
                    return result

            def authenticate(self, *args, **kwargs):
                with self._afuture_request_id_lock:
                    return super().authenticate(*args, **kwargs)

            def login(self, *args, **kwargs):
                with self._afuture_request_id_lock:
                    return super().login(*args, **kwargs)

            def onRspUserLogin(self, *args, **kwargs):
                with self._afuture_request_id_lock:
                    return super().onRspUserLogin(*args, **kwargs)

            def onRspSettlementInfoConfirm(self, *args, **kwargs):
                with self._afuture_request_id_lock:
                    return super().onRspSettlementInfoConfirm(*args, **kwargs)

            def cancel_order(self, *args, **kwargs):
                with self._afuture_request_id_lock:
                    return super().cancel_order(*args, **kwargs)

            def query_account(self, *args, **kwargs):
                with self._afuture_request_id_lock:
                    return super().query_account(*args, **kwargs)

            def query_position(self, *args, **kwargs):
                with self._afuture_request_id_lock:
                    return super().query_position(*args, **kwargs)

            def _afuture_snapshot_failure(self, exc):
                if not domain_broker._snapshot_query_error:
                    domain_broker._snapshot_query_error = f"CTP snapshot query failed: {exc}"
                    domain_broker._enqueue_critical(
                        BrokerEvent("broker_error", domain_broker._snapshot_query_error)
                    )

            def _afuture_query_snapshot(self, guard, request, request_id, native):
                try:
                    if not guard.begin(request_id, self.getTradingDay()):
                        return -2
                    status = native(request, request_id)
                    guard.submitted(request_id, status)
                    return status
                except Exception as exc:
                    self._afuture_snapshot_failure(exc)
                    return -1

            def reqQryTradingAccount(self, request, request_id):
                return self._afuture_query_snapshot(
                    self._afuture_account_query,
                    request,
                    request_id,
                    super().reqQryTradingAccount,
                )

            def reqQryInvestorPosition(self, request, request_id):
                return self._afuture_query_snapshot(
                    self._afuture_position_query,
                    request,
                    request_id,
                    super().reqQryInvestorPosition,
                )

            def onRspQryInvestorPosition(self, data, error, reqid, last):
                with self._afuture_snapshot_callback_lock:
                    try:
                        rows = self._afuture_position_query.observe(
                            data, error, reqid, last, trading_day=self.getTradingDay()
                        )
                        if rows is None:
                            return
                        if self.positions:
                            raise CtpSnapshotQueryError("unowned SDK position scratch remains")
                        # Feed each deduplicated row once and never let the SDK publish
                        # a partial/cross-request snapshot on its own last flag.
                        for row in rows:
                            super().onRspQryInvestorPosition(row, {}, reqid, False)
                        snapshot = list(self.positions.values())
                        expected = {
                            (row["InstrumentID"], row["ExchangeID"])
                            for row in rows
                            if row["Position"] > 0
                        }
                        observed = {(p.symbol, p.exchange.value) for p in snapshot if p.volume > 0}
                        if observed != expected:
                            raise CtpSnapshotQueryError(
                                "SDK dropped or misidentified a held contract"
                            )
                        # The official SDK keys positions without the exchange. Refuse
                        # collisions instead of combining two distinct contract identities.
                        if len({row["InstrumentID"] for row in rows if row["Position"] > 0}) != len(
                            expected
                        ):
                            raise CtpSnapshotQueryError("ambiguous cross-exchange SDK position key")
                        expected_volume: dict[tuple[str, str, str], int] = {}
                        for row in rows:
                            key = (row["InstrumentID"], row["ExchangeID"], row["PosiDirection"])
                            expected_volume[key] = expected_volume.get(key, 0) + row["Position"]
                        observed_volume: dict[tuple[str, str, str], float] = {}
                        for position in snapshot:
                            side = domain_broker._direction_to_side(position.direction)
                            key = (
                                position.symbol,
                                position.exchange.value,
                                "2" if side is OrderSide.BUY else "3",
                            )
                            observed_volume[key] = observed_volume.get(key, 0) + position.volume
                        if {k: v for k, v in expected_volume.items() if v} != {
                            k: v for k, v in observed_volume.items() if v
                        }:
                            raise CtpSnapshotQueryError("SDK changed held quantity or direction")
                        if rows and self.getTradingDay() != rows[0]["TradingDay"]:
                            raise CtpSnapshotQueryError("trading day changed during SDK conversion")
                        snapshot = [p for p in snapshot if p.volume > 0]
                        for position in snapshot:
                            self.gateway.on_position(position)
                        callback = getattr(
                            self.gateway, "_afuture_position_snapshot_callback", None
                        )
                        if callable(callback):
                            callback(snapshot)
                    except Exception as exc:
                        self._afuture_snapshot_failure(exc)
                    finally:
                        # SDK accumulation is disposable parsing scratch, not account truth.
                        self.positions.clear()

            def onRspQryInstrument(self, data, error, reqid, last):
                # Official VeighNa owns ContractData; afuture separately publishes only
                # one complete, request-bound futures generation.
                super().onRspQryInstrument(data, error, reqid, last)
                callback = getattr(self.gateway, "_afuture_contract_catalog_callback", None)
                if callable(callback):
                    callback(
                        None if not data else dict(data),
                        dict(error or {}),
                        int(reqid),
                        bool(last),
                    )

            def onRspQryOrder(self, data, error, reqid, last):
                callback = getattr(self.gateway, "_afuture_session_query_callback", None)
                if callable(callback):
                    callback(
                        "order",
                        None if not data else dict(data),
                        dict(error or {}),
                        int(reqid),
                        bool(last),
                    )

            def onRspQryTrade(self, data, error, reqid, last):
                callback = getattr(self.gateway, "_afuture_session_query_callback", None)
                if callable(callback):
                    callback(
                        "trade",
                        None if not data else dict(data),
                        dict(error or {}),
                        int(reqid),
                        bool(last),
                    )

            def onRspQrySettlementInfo(self, data, error, reqid, last):
                callback = getattr(self.gateway, "_afuture_settlement_query_callback", None)
                if callable(callback):
                    callback(data, error, reqid, last)

            def _capture_rate(self, kind: str, data, error, reqid: int, last: bool) -> None:
                waiter = self._afuture_rate_waiters.get(reqid)
                if waiter is None or waiter.get("kind") != kind:
                    return
                if int((error or {}).get("ErrorID", 0)):
                    waiter["error"] = str(
                        (error or {}).get("ErrorMsg", "CTP metadata query failed")
                    )
                elif data:
                    waiter["rows"].append(dict(data))
                if last:
                    waiter["event"].set()

            def onRspQryInstrumentMarginRate(self, data, error, reqid, last):
                self._capture_rate("margin", data, error, reqid, last)

            def onRspQryInstrumentCommissionRate(self, data, error, reqid, last):
                self._capture_rate("commission", data, error, reqid, last)

            def onRspQryTradingAccount(self, data, error, reqid, last):
                with self._afuture_snapshot_callback_lock:
                    try:
                        rows = self._afuture_account_query.observe(
                            data, error, reqid, last, trading_day=self.getTradingDay()
                        )
                        if rows is None:
                            return
                        row = rows[0]
                        callback = getattr(
                            self.gateway, "_afuture_account_cash_flow_callback", None
                        )
                        if callable(callback) and not callback(dict(row)):
                            return
                        super().onRspQryTradingAccount(row, {}, reqid, True)
                    except Exception as exc:
                        self._afuture_snapshot_failure(exc)

        class TrackedCtpMdApi(CtpMdApi):
            """Preserve raw CTP day identity before VeighNa converts TickData."""

            def onRspUserLogin(self, data, error, reqid, last):
                result = super().onRspUserLogin(data, error, reqid, last)
                if bool(last) and not int((error or {}).get("ErrorID", 0)):
                    callback = getattr(
                        self.gateway,
                        "_afuture_raw_market_connection_callback",
                        None,
                    )
                    if callable(callback):
                        callback(True)
                return result

            def onFrontDisconnected(self, reason):
                result = super().onFrontDisconnected(reason)
                callback = getattr(
                    self.gateway,
                    "_afuture_raw_market_connection_callback",
                    None,
                )
                if callable(callback):
                    callback(False)
                return result

            def onRtnDepthMarketData(self, data):
                identity = {
                    "instrument_id": str((data or {}).get("InstrumentID", "") or ""),
                    "exchange_id": str((data or {}).get("ExchangeID", "") or ""),
                    "trading_day": str((data or {}).get("TradingDay", "") or ""),
                    "action_day": str((data or {}).get("ActionDay", "") or ""),
                }
                self.gateway._afuture_raw_market_identity = identity
                try:
                    return super().onRtnDepthMarketData(data)
                finally:
                    if self.gateway._afuture_raw_market_identity is identity:
                        self.gateway._afuture_raw_market_identity = None

        class TrackedCtpGateway(CtpGateway):
            default_name = "CTP"

            def __init__(self, event_engine, gateway_name):
                super().__init__(event_engine, gateway_name)
                self._afuture_position_snapshot_callback = None
                self._afuture_contract_catalog_callback = None
                self._afuture_account_cash_flow_callback = None
                self._afuture_session_query_callback = None
                self._afuture_settlement_query_callback = None
                self._afuture_critical_ingress_callback = None
                self._afuture_raw_market_connection_callback = None
                self._afuture_raw_market_identity = None
                # super 创建的交易 API 还未连接，直接替换不会遗留会话。
                self.td_api = TrackedCtpTdApi(self)
                self.md_api = TrackedCtpMdApi(self)

            def _mark_critical_ingress(self, kind: str) -> None:
                callback = self._afuture_critical_ingress_callback
                if callable(callback):
                    callback(kind)

            def on_order(self, order) -> None:
                self._mark_critical_ingress("order")
                super().on_order(order)

            def on_trade(self, trade) -> None:
                self._mark_critical_ingress("trade")
                super().on_trade(trade)

            def on_account(self, account) -> None:
                self._mark_critical_ingress("account")
                super().on_account(account)

            def on_tick(self, tick) -> None:
                identity = self._afuture_raw_market_identity
                self._afuture_raw_market_identity = None
                if isinstance(identity, dict):
                    tick._afuture_trading_day = str(identity.get("trading_day", "") or "")
                    tick._afuture_action_day = str(identity.get("action_day", "") or "")
                    tick._afuture_instrument_id = str(identity.get("instrument_id", "") or "")
                    tick._afuture_exchange_id = str(identity.get("exchange_id", "") or "")
                super().on_tick(tick)

        self._runtime = {
            "EventEngine": EventEngine,
            "MainEngine": MainEngine,
            "CtpGateway": TrackedCtpGateway,
            "Direction": Direction,
            "Exchange": Exchange,
            "Offset": VnOffset,
            "OrderType": VnOrderType,
            "Status": Status,
            "OrderRequest": VnOrderRequest,
            "SubscribeRequest": SubscribeRequest,
            "EVENT_ACCOUNT": EVENT_ACCOUNT,
            "EVENT_ORDER": EVENT_ORDER,
            "EVENT_TICK": EVENT_TICK,
            "EVENT_TRADE": EVENT_TRADE,
        }
        return self._runtime

    def start(self) -> None:
        runtime = self._load_runtime()
        self._event_engine = runtime["EventEngine"]()
        self._main_engine = runtime["MainEngine"](self._event_engine)
        self._main_engine.add_gateway(runtime["CtpGateway"])
        gateway = self._main_engine.get_gateway(self.gateway_name)
        if gateway is None:
            raise RuntimeError("CTP gateway could not be created")
        gateway._afuture_position_snapshot_callback = self._handle_position_snapshot
        gateway._afuture_contract_catalog_callback = self._handle_contract_catalog_response
        gateway._afuture_account_cash_flow_callback = self._handle_account_cash_flow
        gateway._afuture_session_query_callback = self._handle_session_query_response
        gateway._afuture_settlement_query_callback = self._settlement_query.observe
        gateway._afuture_critical_ingress_callback = self._mark_critical_upstream_pending
        gateway._afuture_raw_market_connection_callback = self._handle_raw_market_connection_state
        self._event_engine.register(runtime["EVENT_TICK"], self._on_tick)
        self._event_engine.register(runtime["EVENT_ORDER"], self._on_order)
        self._event_engine.register(runtime["EVENT_TRADE"], self._on_trade)
        self._event_engine.register(runtime["EVENT_ACCOUNT"], self._on_account)
        self._main_engine.connect(build_ctp_setting(self.credentials), self.gateway_name)

    def stop(self) -> None:
        self._handle_raw_market_connection_state(False)
        if self._main_engine is not None:
            self._main_engine.close()
        self._main_engine = None
        self._event_engine = None
        self._stress90_session_startup_capability = None

    def set_raw_tick_observer(self, observer) -> None:
        super().set_raw_tick_observer(observer)
        if observer is None:
            return
        with self._raw_market_connection_lock:
            connected = self._raw_market_connected
            generation = self._raw_market_connection_generation
        if connected:
            callback = getattr(observer, "note_raw_market_connection", None)
            if callable(callback):
                callback(connected=True, generation=generation)

    def _handle_raw_market_connection_state(self, connected: bool) -> None:
        """Publish an uninterrupted CTP MD login generation to raw observers."""

        if not isinstance(connected, bool):
            raise TypeError("CTP raw market connection state must be boolean")
        with self._raw_market_connection_lock:
            if connected:
                if self._raw_market_connected:
                    return
                self._raw_market_connection_generation += 1
                self._raw_market_connected = True
            else:
                if not self._raw_market_connected:
                    return
                self._raw_market_connected = False
            generation = self._raw_market_connection_generation
        observer = getattr(self, "_raw_tick_observer", None)
        callback = getattr(observer, "note_raw_market_connection", None)
        if callable(callback):
            try:
                callback(connected=connected, generation=generation)
            except Exception as exc:
                self._enqueue_critical(
                    BrokerEvent(
                        "broker_error",
                        f"CTP raw market connection observer failed: {exc}",
                    )
                )

    def is_ready(self) -> bool:
        if self._main_engine is None:
            return False
        gateway = self._main_engine.get_gateway(self.gateway_name)
        if gateway is None:
            return False
        td_api = getattr(gateway, "td_api", None)
        md_api = getattr(gateway, "md_api", None)
        return bool(
            td_api
            and md_api
            and getattr(td_api, "login_status", False)
            and getattr(td_api, "contract_inited", False)
            and getattr(md_api, "login_status", False)
        )

    def health_error(self, *, now_monotonic: float | None = None) -> str | None:
        if self._snapshot_query_error:
            return self._snapshot_query_error
        if self._settlement_query.integrity_error:
            return self._settlement_query.integrity_error
        with self._contract_catalog_refresh_state_lock:
            catalog_sticky_error = self._contract_catalog_sticky_error
        if catalog_sticky_error:
            return catalog_sticky_error
        if not self.is_ready():
            return None
        now = monotonic() if now_monotonic is None else now_monotonic
        if self._last_account_monotonic <= 0 or self._last_position_snapshot_monotonic <= 0:
            return "CTP account/position snapshot is not initialized"
        if now - self._last_account_monotonic > self.snapshot_stale_seconds:
            return "CTP account snapshot is stale"
        if now - self._last_position_snapshot_monotonic > self.snapshot_stale_seconds:
            return "CTP position snapshot is stale"
        return None

    def snapshot_marker(self) -> tuple[int, int]:
        return self._account_event_generation, self._position_snapshot_generation

    def snapshot_ready(self, marker: tuple[int, int]) -> bool:
        account_generation, position_generation = marker
        return bool(
            self._last_account is not None
            and self._account_event_generation > account_generation
            and self._position_snapshot_generation > position_generation
        )

    def subscribe(self, symbol: str, exchange: str) -> None:
        if self._main_engine is None:
            raise RuntimeError("CTP broker is not started")
        runtime = self._load_runtime()
        request = runtime["SubscribeRequest"](
            symbol=symbol,
            exchange=self._exchange(exchange),
        )
        self._main_engine.subscribe(request, self.gateway_name)

    def get_account_identity_digest(self) -> str:
        if not self._has_expected_account_identity:
            identity = "\0".join(
                (
                    "afuture.ctp-account.v1",
                    self.credentials.broker_id,
                    self.credentials.user_id,
                    self.credentials.environment.lower(),
                )
            )
            return sha256(identity.encode("utf-8")).hexdigest()
        identity = "\0".join(
            (
                "afuture.ctp-economic-account.v1",
                self.credentials.broker_id,
                self.credentials.environment.lower(),
                self.credentials.account_id,
                self.credentials.currency_id,
                self.credentials.investor_id,
                self.credentials.invest_unit_id,
            )
        )
        return sha256(identity.encode("utf-8")).hexdigest()

    @property
    def _has_expected_account_identity(self) -> bool:
        return bool(self.credentials.account_id and self.credentials.currency_id)

    @property
    def account_identity_verified(self) -> bool:
        """Whether the latest AccountData is paired with exact current-day CTP evidence."""

        try:
            authoritative_day = self.get_trading_day()
        except RuntimeError:
            return False
        with self._account_lock:
            return bool(
                self._has_expected_account_identity
                and self._account_identity_verified_state
                and self._account_identity_verified_day
                and self._account_identity_verified_day == authoritative_day
                and self._last_account is not None
                and self._last_account.cash_flow_verified
            )

    def configure_order_submission_journal(
        self,
        path,
        *,
        policy_id: str,
        policy_definition_digest: str,
        products_manifest_digest: str,
    ) -> None:
        """Load exact Stress-90 CTP ownership proof before gateway callbacks can start."""

        if self._main_engine is not None or self._event_engine is not None:
            raise RuntimeError("CTP order journal must be configured before broker start")
        identities = (
            str(policy_id),
            str(policy_definition_digest),
            str(products_manifest_digest),
        )
        if (
            not identities[0]
            or re.fullmatch(r"[0-9a-f]{64}", identities[1]) is None
            or re.fullmatch(r"[0-9a-f]{64}", identities[2]) is None
        ):
            raise ValueError("CTP order journal policy identity is invalid")
        journal = CtpOrderSubmissionJournal(path)
        account_digest = self.get_account_identity_digest()
        record = journal.load_runtime(account_identity_digest=account_digest)
        self._decode_order_submission_runtime_record(record, identities, account_digest)
        self._order_submission_journal = journal
        self._order_journal_policy_identity = identities
        self._install_order_submission_runtime_record(record, identities, account_digest)

    def require_stress90_session_startup_capability(self) -> None:
        """Require a fresh complete-query proof before this process may send."""

        if self._main_engine is not None or self._event_engine is not None:
            raise RuntimeError("Stress-90 startup capability must be required before Broker start")
        if self._order_submission_journal is None:
            raise RuntimeError("Stress-90 startup capability requires the durable order journal")
        self._stress90_session_startup_required = True
        self._stress90_session_startup_capability = None

    def install_stress90_session_startup_capability(
        self,
        *,
        evidence: CtpSessionActivityEvidence,
        ownership_digest: str,
    ) -> None:
        """Install one process-local baseline after exact complete-query ownership."""

        if not self._stress90_session_startup_required:
            raise RuntimeError("Stress-90 startup session capability was not required")
        if re.fullmatch(r"[0-9a-f]{64}", ownership_digest) is None:
            raise RuntimeError("Stress-90 startup session capability identity mismatch")
        self.require_session_activity_evidence_current(evidence)
        self._stress90_session_startup_capability = _Stress90SessionStartupCapability(
            account_identity_digest=evidence.account_identity_digest,
            trading_day=evidence.trading_day,
            critical_generation=evidence.critical_generation,
            query_ingress_generation=evidence.query_ingress_generation,
            evidence_digest=evidence.evidence_digest,
            ownership_digest=ownership_digest,
        )

    def require_stress90_session_startup_capability_current(
        self,
        *,
        consume: bool = False,
    ) -> None:
        """Check the startup baseline; optionally consume it at the first send boundary."""

        if not self._stress90_session_startup_required:
            return
        capability = self._stress90_session_startup_capability
        if capability is None:
            raise RuntimeError("Stress-90 startup session capability is missing")
        if (
            capability.account_identity_digest != self.get_account_identity_digest()
            or capability.trading_day != self.get_trading_day()
        ):
            raise RuntimeError("Stress-90 startup session capability is no longer current")
        if not capability.consumed:
            with self._critical_barrier_lock:
                with self._session_query_ingress_lock:
                    try:
                        generation = self._quiescent_order_trade_generation()
                    except RuntimeError as exc:
                        raise RuntimeError(
                            "Stress-90 startup session capability is no longer current"
                        ) from exc
                    with self._session_query_state_lock:
                        sticky = self._session_query_sticky_error
                        query_generation = self._session_query_ingress_generation
                        callbacks_inflight = self._session_query_callbacks_inflight
                    if (
                        sticky
                        or callbacks_inflight
                        or generation != capability.critical_generation
                        or query_generation != capability.query_ingress_generation
                    ):
                        raise RuntimeError(
                            "Stress-90 startup session capability is no longer current"
                        )
                    if consume:
                        self._stress90_session_startup_capability = replace(
                            capability,
                            consumed=True,
                        )

    def require_session_activity_evidence_current(
        self,
        evidence: CtpSessionActivityEvidence,
    ) -> None:
        """Require an exact query generation to remain current in this process."""

        if (
            not isinstance(evidence, CtpSessionActivityEvidence)
            or evidence.account_identity_digest != self.get_account_identity_digest()
            or evidence.trading_day != self.get_trading_day()
        ):
            raise RuntimeError("CTP session activity evidence identity is no longer current")
        with self._critical_barrier_lock:
            with self._session_query_ingress_lock:
                try:
                    generation = self._quiescent_order_trade_generation()
                except RuntimeError as exc:
                    raise RuntimeError(
                        "CTP session activity evidence is no longer current"
                    ) from exc
                with self._session_query_state_lock:
                    sticky = self._session_query_sticky_error
                    query_generation = self._session_query_ingress_generation
                    callbacks_inflight = self._session_query_callbacks_inflight
                if (
                    sticky
                    or callbacks_inflight
                    or generation != evidence.critical_generation
                    or query_generation != evidence.query_ingress_generation
                ):
                    raise RuntimeError("CTP session activity evidence is no longer current")

    def set_order_submission_context(
        self,
        *,
        target_trading_day: str,
        daily_decision_digest: str,
        execution_intent_digest: str,
        transition,
    ) -> None:
        if self._order_submission_journal is None:
            return
        target = self._validate_trading_day(target_trading_day)
        decision = str(daily_decision_digest)
        intent = str(execution_intent_digest)
        if (
            re.fullmatch(r"[0-9a-f]{64}", decision) is None
            or re.fullmatch(r"[0-9a-f]{64}", intent) is None
            or not isinstance(transition, dict)
        ):
            raise ValueError("CTP order submission context is invalid")
        self._order_submission_context = CtpOrderSubmissionContext(
            target,
            decision,
            intent,
            transition,
        )
        # Exact request capabilities are deliberately shorter lived than the daily
        # candidate identity. Every newly computed order batch must authorize its own
        # immutable requests after all planner and hard-risk checks have completed.
        self._candidate_order_authorizations = {}

    def authorize_candidate_order_requests(self, requests) -> None:
        """Install one consumable exact capability for every planned OPEN request."""

        if self._order_submission_journal is None:
            return
        with self._order_submission_lock:
            # Replacing a batch is all-or-nothing. An invalid re-authorization must
            # revoke any older unconsumed capabilities from the same daily context.
            self._candidate_order_authorizations = {}
            context = self._order_submission_context
            if context is None:
                raise RuntimeError("Stress-90 candidate context is missing")
            canonical = tuple(requests)
            if not canonical:
                raise ValueError("Stress-90 exact candidate request authorization is empty")
            authorized_lots = context.transition.get("freeze_authorized_lots")
            if not isinstance(authorized_lots, dict):
                raise ValueError("Stress-90 candidate lot authorization is invalid")
            expected_reference_prefix = (
                f"directional:stress90:{context.daily_decision_digest[:12]}:"
            )
            counts: dict[OrderRequest, int] = {}
            aggregate_by_symbol: dict[str, int] = {}
            for request in canonical:
                if not isinstance(request, OrderRequest) or request.offset is not Offset.OPEN:
                    raise ValueError("Stress-90 candidate authorization accepts only OPEN requests")
                signed_volume = (
                    int(request.volume) if request.side is OrderSide.BUY else -int(request.volume)
                )
                target_volume = authorized_lots.get(request.symbol)
                if (
                    isinstance(request.volume, bool)
                    or not isinstance(request.volume, int)
                    or request.volume <= 0
                    or isinstance(target_volume, bool)
                    or not isinstance(target_volume, int)
                    or target_volume == 0
                    or (signed_volume > 0) != (target_volume > 0)
                    or abs(signed_volume) > abs(target_volume)
                    or not request.reference.startswith(expected_reference_prefix)
                ):
                    raise ValueError("Stress-90 exact candidate request exceeds persisted intent")
                aggregate = aggregate_by_symbol.get(request.symbol, 0) + signed_volume
                if abs(aggregate) > abs(target_volume):
                    raise ValueError(
                        "Stress-90 candidate request aggregate exceeds persisted intent"
                    )
                aggregate_by_symbol[request.symbol] = aggregate
                counts[request] = counts.get(request, 0) + 1
            self._candidate_order_authorizations = counts

    def _risk_reduction_submission_context(
        self,
        request: OrderRequest,
        current_trading_day: str,
        identities: tuple[str, str, str],
    ) -> CtpOrderSubmissionContext:
        if request.offset is Offset.OPEN:
            raise RuntimeError("Stress-90 candidate context is missing for CTP opening")
        daily_payload = {
            "account_identity_digest": self.get_account_identity_digest(),
            "authorization_kind": "risk_reduction",
            "policy_definition_digest": identities[1],
            "policy_id": identities[0],
            "products_manifest_digest": identities[2],
            "target_trading_day": current_trading_day,
        }
        daily_encoded = json.dumps(
            daily_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        daily_digest = sha256(daily_encoded).hexdigest()
        intent_payload = {
            "daily_decision_digest": daily_digest,
            "request": {
                "symbol": request.symbol,
                "exchange": request.exchange,
                "side": request.side.value,
                "offset": request.offset.value,
                "volume": request.volume,
                "price": request.price,
                "order_type": request.order_type.value,
                "reference": request.reference,
            },
        }
        intent_digest = sha256(
            json.dumps(
                intent_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        return CtpOrderSubmissionContext(
            current_trading_day,
            daily_digest,
            intent_digest,
            {"freeze_authorized_lots": {}, "transitions": []},
        )

    def send_order(self, request: OrderRequest) -> str:
        if not self.is_ready() or self._main_engine is None:
            raise RuntimeError("CTP market/trading session is not ready")
        if self._order_submission_journal is None:
            order_id = self._main_engine.send_order(self._to_vnpy_order(request), self.gateway_name)
            if not order_id:
                raise RuntimeError("CTP order request was not accepted by gateway")
            self._order_references[order_id] = request.reference
            return order_id

        self._require_acknowledged_critical_boundary()
        self.require_stress90_session_startup_capability_current()
        with self._order_submission_lock:
            identities = self._order_journal_policy_identity
            if identities is None:
                raise RuntimeError("Stress-90 CTP order policy identity is missing")
            current_trading_day = self.get_trading_day()
            candidate_context = self._order_submission_context
            candidate_is_current = bool(
                candidate_context is not None
                and candidate_context.target_trading_day == current_trading_day
            )
            if request.offset is Offset.OPEN:
                if not candidate_is_current:
                    if candidate_context is None:
                        raise RuntimeError("Stress-90 candidate context is missing for CTP opening")
                    raise RuntimeError(
                        "Stress-90 candidate context does not match current CTP trading day"
                    )
                if self._candidate_order_authorizations.get(request, 0) <= 0:
                    raise RuntimeError(
                        "Stress-90 exact candidate request is not authorized or was consumed"
                    )
                context = candidate_context
                authorization_kind = "candidate"
            else:
                context = self._risk_reduction_submission_context(
                    request,
                    current_trading_day,
                    identities,
                )
                authorization_kind = "risk_reduction"
            if context is None:  # pragma: no cover - narrowed above
                raise RuntimeError("Stress-90 CTP order submission context is missing")
            gateway = self._main_engine.get_gateway(self.gateway_name)
            td_api = getattr(gateway, "td_api", None) if gateway is not None else None
            if td_api is None:
                raise RuntimeError("CTP trading API is unavailable for exact order reservation")
            try:
                front_id = int(td_api.frontid)
                session_id = int(td_api.sessionid)
                current_order_ref = int(td_api.order_ref)
            except (AttributeError, TypeError, ValueError) as exc:
                raise RuntimeError("CTP replayable order identity is unavailable") from exc
            if min(front_id, session_id) <= 0 or current_order_ref < 0:
                raise RuntimeError("CTP replayable order identity is unavailable")
            order_ref = current_order_ref + 1
            order_id = f"{self.gateway_name}.{front_id}_{session_id}_{order_ref}"
            sequence = len(self._order_submission_entries) + 1
            entry = CtpOrderSubmissionEntry(
                sequence=sequence,
                account_identity_digest=self.get_account_identity_digest(),
                policy_id=identities[0],
                policy_definition_digest=identities[1],
                products_manifest_digest=identities[2],
                target_trading_day=context.target_trading_day,
                daily_decision_digest=context.daily_decision_digest,
                execution_intent_digest=context.execution_intent_digest,
                transition=context.transition,
                front_id=front_id,
                session_id=session_id,
                order_ref=order_ref,
                order_id=order_id,
                request=request,
                status="prepared",
                authorization_kind=authorization_kind,
            )
            prepared = self._order_submission_journal.prepare(
                entry,
                fill_identity_capacity=self._MAX_SEEN_TRADE_KEYS,
                observed_fill_identity_count=len(self._seen_trade_keys),
            )
            self._order_submission_entries[order_id] = prepared
            self._order_references[order_id] = request.reference
            if authorization_kind == "candidate":
                remaining = self._candidate_order_authorizations.get(request, 0)
                if remaining <= 0:  # pragma: no cover - guarded before durable prepare
                    raise RuntimeError("Stress-90 exact candidate request capability vanished")
                if remaining == 1:
                    self._candidate_order_authorizations.pop(request, None)
                else:
                    self._candidate_order_authorizations[request] = remaining - 1
            td_api._afuture_expected_order_ref = order_ref
            try:
                try:
                    with self._critical_order_submission_scope():
                        try:
                            self.require_stress90_session_startup_capability_current(consume=True)
                        except RuntimeError as exc:
                            raise _CriticalSubmissionBoundaryError(
                                "CTP session proof changed before official send"
                            ) from exc
                        returned = self._main_engine.send_order(
                            self._to_vnpy_order(request), self.gateway_name
                        )
                except _CriticalSubmissionBoundaryError:
                    self._order_submission_journal.update_status(order_id, "aborted_before_send")
                    self._order_submission_entries[order_id] = replace(
                        prepared, status="aborted_before_send"
                    )
                    raise
            finally:
                td_api._afuture_expected_order_ref = None
            if returned != order_id:
                raise RuntimeError(
                    "CTP gateway returned an identity different from reserved OrderRef"
                )
            self._order_submission_journal.update_status(order_id, "submitted")
            submitted = replace(prepared, status="submitted")
            self._order_submission_entries[order_id] = submitted
            return order_id

    def owns_order(self, order_id: str) -> bool:
        if self._order_submission_journal is not None:
            return order_id in self._order_submission_entries
        if order_id in self._order_references:
            return True
        order = self.get_order(order_id)
        if order is None:
            return False
        reference = order.request.reference
        if any(
            reference.startswith(prefix) for prefix in self._authorized_order_reference_prefixes
        ):
            self._order_references[order_id] = reference
            return True
        return False

    def get_order_submission_identity(self, order_id: str):
        """Return exact durable CTP proof; callers must still bind it to current intent."""

        return self._order_submission_entries.get(order_id)

    def get_unresolved_order_submission_identities(self):
        """Return nonterminal exact identities; callers wait rather than resubmit ambiguously."""

        return tuple(
            entry
            for entry in self._order_submission_entries.values()
            if entry.status not in {"terminal", "aborted_before_send"}
        )

    def seed_order_reference_prefixes(self, prefixes) -> None:
        if self._order_submission_journal is not None:
            return
        normalized = tuple(sorted(set(str(item) for item in prefixes if str(item))))
        self._authorized_order_reference_prefixes = normalized

    def cancel_order(self, order_id: str) -> None:
        if self._main_engine is None:
            return
        order = self._main_engine.get_order(order_id)
        if order is not None:
            self._main_engine.cancel_order(order.create_cancel_request(), self.gateway_name)

    def get_order(self, order_id: str) -> Order | None:
        if self._main_engine is None:
            return self._recovered_session_orders.get(order_id)
        raw = self._main_engine.get_order(order_id)
        if raw is not None:
            return self._convert_order(raw)
        evidence = self._recovered_session_evidence
        if evidence is not None:
            self.require_session_activity_evidence_current(evidence)
        return self._recovered_session_orders.get(order_id)

    def get_active_orders(self) -> list[Order]:
        if self._main_engine is None:
            return []
        return [self._convert_order(order) for order in self._main_engine.get_all_active_orders()]

    def get_account(self) -> AccountSnapshot:
        if self._main_engine is not None:
            accounts = self._main_engine.get_all_accounts()
            if self._has_expected_account_identity:
                exact = [raw for raw in accounts if self._account_data_matches_expected(raw)]
                if len(exact) != 1:
                    reason = "CTP AccountData did not contain one unique exact expected account"
                    self._invalidate_account_identity(reason)
                    raise RuntimeError(reason)
                if self._last_account is None or not self.account_identity_verified:
                    raise RuntimeError("verified CTP account snapshot is not available")
            elif accounts:
                self._last_account = self._convert_account(accounts[0])
        if self._last_account is None:
            raise RuntimeError("CTP account snapshot is not available")
        return self._last_account

    def get_positions(self) -> list[ContractPosition]:
        with self._position_lock:
            return self._copy_positions_unlocked()

    def _copy_positions_unlocked(self) -> list[ContractPosition]:
        return [replace(position) for position in self._positions.values() if not position.empty]

    def poll_events(self) -> list[BrokerEvent]:
        result: list[BrokerEvent] = []
        with self._event_lock:
            while self._critical_events and len(result) < self.max_events_per_poll:
                result.append(self._critical_events.popleft())
                self._critical_delivered += 1
            while self._latest_ticks and len(result) < self.max_events_per_poll:
                key = next(iter(self._latest_ticks))
                result.append(self._latest_ticks.pop(key))
                self._ticks_delivered += 1
        return result

    def delivery_counters(self) -> dict[str, int]:
        """Return a lock-consistent delivery snapshot for lightweight observability."""
        with self._event_lock:
            return {
                "critical_enqueued": self._critical_enqueued,
                "ticks_received": self._ticks_received,
                "ticks_coalesced": self._ticks_coalesced,
                "critical_delivered": self._critical_delivered,
                "ticks_delivered": self._ticks_delivered,
                "critical_backlog": len(self._critical_events),
                "tick_backlog": len(self._latest_ticks),
            }

    def has_pending_critical_events(self) -> bool:
        with self._critical_barrier_lock:
            if self._account_evidence_pair_incomplete():
                return True
            with self._event_lock:
                return bool(
                    self._critical_upstream_pending
                    or self._critical_callbacks_inflight
                    or self._critical_events
                    or (
                        self._order_submission_journal is not None
                        and self._critical_acknowledged != self._critical_enqueued
                    )
                )

    def _enqueue_critical(self, event: BrokerEvent) -> None:
        with self._critical_barrier_lock:
            with self._event_lock:
                self._critical_events.append(event)
                self._critical_enqueued += 1
            if self._critical_submission_active:
                raise _CriticalSubmissionBoundaryError(
                    "critical event arrived during CTP submission boundary"
                )

    def _enqueue_tick(self, tick: Tick) -> None:
        key = (tick.symbol, tick.exchange)
        with self._event_lock:
            self._ticks_received += 1
            if key in self._latest_ticks:
                self._ticks_coalesced += 1
            self._latest_ticks[key] = BrokerEvent("tick", tick)

    def get_trading_day(self) -> str:
        if self._main_engine is not None:
            gateway = self._main_engine.get_gateway(self.gateway_name)
            if gateway is None:
                raise RuntimeError("CTP trading day gateway is unavailable")
            td_api = getattr(gateway, "td_api", None)
            if td_api is None:
                raise RuntimeError("CTP trading day API is unavailable")
            getter = getattr(td_api, "getTradingDay", None)
            if not callable(getter):
                raise RuntimeError("CTP trading day getter is unavailable")
            try:
                value = getter()
            except Exception as exc:
                raise RuntimeError("CTP trading day query failed") from exc
            if isinstance(value, bytes):
                try:
                    value = value.decode("ascii")
                except UnicodeDecodeError as exc:
                    raise RuntimeError("CTP trading day is invalid") from exc
            self._trading_day = self._validate_trading_day(value)
        return self._validate_trading_day(self._trading_day)

    @staticmethod
    def _validate_trading_day(value: object) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"\d{8}", value):
            raise RuntimeError("CTP trading day is missing or invalid")
        try:
            parsed = datetime.strptime(value, "%Y%m%d")
        except ValueError as exc:
            raise RuntimeError("CTP trading day is missing or invalid") from exc
        if parsed.strftime("%Y%m%d") != value:
            raise RuntimeError("CTP trading day is missing or invalid")
        return value

    def get_contract_catalog(self) -> list[ContractInfo]:
        """返回 CTP 合约查询得到的期货目录，供自动构建相邻月份。"""
        return sorted(
            self._contract_catalog.values(),
            key=lambda item: (item.product.lower(), item.expiry, item.symbol),
        )

    def capture_settlement_document(
        self, trading_day: str, *, timeout_seconds: float = 30.0
    ) -> CtpSettlementDocument:
        """Read a complete decoded statement, never promote it to financial proof."""
        day = self._validate_trading_day(trading_day)
        current_day = self.get_trading_day()
        if day > current_day:
            raise RuntimeError("cannot query a future settlement trading day")
        if not self._has_expected_account_identity or not self.account_identity_verified:
            raise RuntimeError("settlement capture requires verified explicit account identity")
        if self._main_engine is None or not self.is_ready():
            raise RuntimeError("CTP is not ready for settlement capture")
        gateway = self._main_engine.get_gateway(self.gateway_name)
        td_api = getattr(gateway, "td_api", None)
        native_query = getattr(td_api, "reqQrySettlementInfo", None)
        if not callable(native_query):
            raise RuntimeError("native CTP SettlementInfo query is unavailable")
        document = self._settlement_query.query(
            native_query,
            request_id=self._allocate_ctp_query_request_id(td_api),
            broker_id=self.credentials.broker_id,
            investor_id=self.credentials.investor_id or self.credentials.user_id,
            account_id=self.credentials.account_id,
            currency_id=self.credentials.currency_id,
            trading_day=day,
            timeout_seconds=timeout_seconds,
        )
        if self.get_trading_day() != current_day or not self.account_identity_verified:
            raise RuntimeError("CTP session changed during settlement capture")
        self._settlement_query.require_current(document)
        return document

    def _allocate_ctp_query_request_id(self, td_api) -> int:
        """Allocate one request id under the afuture query serialization boundary."""

        allocator = getattr(td_api, "_afuture_allocate_request_id", None)
        if callable(allocator):
            request_id = allocator()
        else:
            with self._ctp_query_request_lock:
                request_id = int(getattr(td_api, "reqid", 0)) + 1
                td_api.reqid = request_id
        if isinstance(request_id, bool) or not isinstance(request_id, int) or request_id <= 0:
            raise RuntimeError("CTP query request id is invalid")
        return request_id

    def _handle_session_query_response(
        self,
        kind: str,
        data,
        error,
        request_id: int,
        last: bool,
    ) -> None:
        # This is the linearization point shared with the official send boundary.
        # A callback that enters first invalidates the proof; a send that acquires
        # the ingress fence first completes before this callback is considered new.
        with self._session_query_ingress_lock:
            with self._session_query_state_lock:
                self._session_query_ingress_generation += 1
                self._session_query_callbacks_inflight += 1
        try:
            self._handle_session_query_response_impl(
                kind,
                data,
                error,
                request_id,
                last,
            )
        finally:
            with self._session_query_state_lock:
                self._session_query_callbacks_inflight -= 1

    def _set_session_query_sticky_error(self, detail: str) -> None:
        with self._session_query_state_lock:
            if not self._session_query_sticky_error:
                self._session_query_sticky_error = detail
            sticky = self._session_query_sticky_error
        self._enqueue_critical(BrokerEvent("broker_error", sticky))

    def _handle_session_query_response_impl(
        self,
        kind: str,
        data,
        error,
        request_id: int,
        last: bool,
    ) -> None:
        """Capture one callback generation without emitting order/trade events."""

        if kind not in self._session_query_accumulators:
            self._set_session_query_sticky_error("unknown CTP session query kind")
            return
        try:
            completed = self._session_query_accumulators[kind].observe(
                int(request_id),
                None if data is None else dict(data),
                None if error is None else dict(error),
                last=bool(last),
            )
        except (CtpSessionQueryIntegrityError, TypeError, ValueError) as exc:
            late = False
            with self._session_query_state_lock:
                expected = self._session_query_request_ids.get(kind)
                event = self._session_query_events.get(kind)
                if expected == int(request_id) and event is not None:
                    if event.is_set():
                        self._session_query_sticky_error = (
                            f"CTP {kind} query received a late response after completion"
                        )
                        late = True
                    else:
                        self._session_query_errors[kind] = str(exc)
                        event.set()
                        return
            if late:
                self._set_session_query_sticky_error(self._session_query_sticky_error)
                return
            self._set_session_query_sticky_error(f"CTP session query callback failed: {exc}")
            return
        if completed is not None:
            late = False
            outside = False
            with self._session_query_state_lock:
                expected = self._session_query_request_ids.get(kind)
                event = self._session_query_events.get(kind)
                if expected != int(request_id) or event is None:
                    outside = True
                elif event.is_set():
                    self._session_query_sticky_error = (
                        f"CTP {kind} query received a late response after completion"
                    )
                    late = True
                else:
                    self._session_query_results[kind] = completed
                    event.set()
            if outside:
                self._set_session_query_sticky_error(
                    "CTP session query completed outside registered request"
                )
                return
            if late:
                self._set_session_query_sticky_error(self._session_query_sticky_error)

    def _query_complete_session_kind(
        self,
        td_api,
        *,
        kind: str,
        trading_day: str,
        deadline: float,
    ) -> tuple[int, tuple[CtpSessionOrder | CtpSessionTrade, ...]]:
        query = getattr(td_api, "reqQryOrder" if kind == "order" else "reqQryTrade", None)
        if not callable(query):
            raise RuntimeError(f"CTP {kind} query API is unavailable")
        request_id = self._allocate_ctp_query_request_id(td_api)
        investor_id = self.credentials.investor_id or self.credentials.user_id
        request = {
            "BrokerID": self.credentials.broker_id,
            "InvestorID": investor_id,
        }
        if self.credentials.invest_unit_id:
            request["InvestUnitID"] = self.credentials.invest_unit_id
        accumulator = self._session_query_accumulators[kind]
        accumulator.begin(
            request_id,
            broker_id=self.credentials.broker_id,
            investor_id=investor_id,
            invest_unit_id=self.credentials.invest_unit_id,
            trading_day=trading_day,
        )
        event = Event()
        with self._session_query_state_lock:
            if kind in self._session_query_events:
                raise RuntimeError(f"CTP {kind} query state is already active")
            self._session_query_events[kind] = event
            self._session_query_request_ids[kind] = request_id
            self._session_query_errors.pop(kind, None)
            self._session_query_results.pop(kind, None)
        try:
            while True:
                status = query(request, request_id)
                if not status:
                    break
                if monotonic() >= deadline:
                    raise RuntimeError(f"CTP {kind} query rate-limit timeout")
                sleep(min(0.2, max(0.0, deadline - monotonic())))
            if not event.wait(max(0.0, deadline - monotonic())):
                raise RuntimeError(f"CTP {kind} query completion timeout")
            while True:
                with self._session_query_state_lock:
                    callbacks_inflight = self._session_query_callbacks_inflight
                if callbacks_inflight == 0:
                    break
                if monotonic() >= deadline:
                    raise RuntimeError(f"CTP {kind} query callback quiescence timeout")
                sleep(min(0.001, max(0.0, deadline - monotonic())))
            with self._session_query_state_lock:
                sticky = self._session_query_sticky_error
                failure = self._session_query_errors.get(kind, "")
                result = self._session_query_results.get(kind)
            if sticky:
                raise RuntimeError(sticky)
            if failure:
                raise RuntimeError(failure)
            if result is None:
                raise RuntimeError(f"CTP {kind} query completion evidence is missing")
            return request_id, result
        except Exception as exc:
            try:
                accumulator.abort(request_id, str(exc))
            except CtpSessionQueryIntegrityError:
                pass
            raise
        finally:
            with self._session_query_state_lock:
                self._session_query_events.pop(kind, None)
                self._session_query_request_ids.pop(kind, None)
                self._session_query_errors.pop(kind, None)
                self._session_query_results.pop(kind, None)

    def refresh_session_activity(
        self,
        *,
        timeout_seconds: float = 10.0,
    ) -> CtpSessionActivityEvidence:
        """Query complete current-session orders/trades under one quiescent generation."""

        timeout = float(timeout_seconds)
        if not isfinite(timeout) or timeout <= 0.0:
            raise ValueError("CTP session query timeout must be positive")
        if self._main_engine is None:
            raise RuntimeError("CTP is not started for session query")
        gateway = self._main_engine.get_gateway(self.gateway_name)
        td_api = getattr(gateway, "td_api", None) if gateway is not None else None
        if td_api is None:
            raise RuntimeError("CTP session query API is unavailable")
        with self._session_refresh_lock:
            trading_day = self.get_trading_day()
            account_digest = self.get_account_identity_digest()
            with self._session_query_state_lock:
                if self._session_query_sticky_error:
                    raise RuntimeError(self._session_query_sticky_error)
            generation = self._quiescent_order_trade_generation()
            deadline = monotonic() + timeout
            trade_request, raw_trades = self._query_complete_session_kind(
                td_api,
                kind="trade",
                trading_day=trading_day,
                deadline=deadline,
            )
            # Query trades first, then orders.  A remotely-created live order in
            # the inter-query window is therefore present in the later order view;
            # the opposite order leaves a mechanically detectable blind window.
            order_request, raw_orders = self._query_complete_session_kind(
                td_api,
                kind="order",
                trading_day=trading_day,
                deadline=deadline,
            )
            orders = tuple(row for row in raw_orders if isinstance(row, CtpSessionOrder))
            trades = tuple(row for row in raw_trades if isinstance(row, CtpSessionTrade))
            if len(orders) != len(raw_orders) or len(trades) != len(raw_trades):
                raise RuntimeError("CTP session query returned mixed row types")
            with self._critical_barrier_lock:
                with self._session_query_ingress_lock:
                    final_generation = self._quiescent_order_trade_generation()
                    with self._session_query_state_lock:
                        sticky = self._session_query_sticky_error
                        callbacks_inflight = self._session_query_callbacks_inflight
                        query_generation = self._session_query_ingress_generation
                    if sticky:
                        raise RuntimeError(sticky)
                    if callbacks_inflight:
                        raise RuntimeError("CTP session query callbacks are not quiescent")
                    if (
                        self.get_trading_day() != trading_day
                        or self.get_account_identity_digest() != account_digest
                        or final_generation != generation
                    ):
                        raise RuntimeError("CTP order/trade activity changed during complete query")
                    return build_ctp_session_activity_evidence(
                        account_identity_digest=account_digest,
                        trading_day=trading_day,
                        order_request_id=order_request,
                        trade_request_id=trade_request,
                        orders=orders,
                        trades=trades,
                        critical_generation=generation,
                        query_ingress_generation=query_generation,
                    )

    def recover_stress90_session_activity(
        self,
        evidence: CtpSessionActivityEvidence,
    ) -> tuple[str, ...]:
        """Roll an exact crash window into the durable journal from Broker query truth."""

        journal = self._order_submission_journal
        identities = self._order_journal_policy_identity
        if journal is None or identities is None:
            raise RuntimeError("Stress-90 session recovery requires the configured journal")
        self.require_session_activity_evidence_current(evidence)
        lifecycle_view = journal.load_lifecycle_view(
            account_identity_digest=evidence.account_identity_digest,
            trading_day=evidence.trading_day,
        )
        plan = plan_ctp_session_journal_recovery(
            evidence,
            lifecycle_view.entries,
            current_order_ids=lifecycle_view.current_order_ids,
        )
        fills = {
            order_id: tuple(
                ctp_order_fill_evidence(evidence.trading_day, trade) for trade in trades
            )
            for order_id, trades in plan.fill_trades
        }
        if plan.status_updates or fills:
            journal.apply_updates(
                statuses=dict(plan.status_updates),
                fills=fills,
            )
        runtime_record = journal.load_runtime(
            account_identity_digest=evidence.account_identity_digest
        )
        runtime_entries = (
            {}
            if runtime_record is None
            else {entry.order_id: entry for entry in runtime_record.all_entries}
        )
        traded_by_order: dict[str, int] = {}
        notional_by_order: dict[str, float] = {}
        for trade in plan.session_trades:
            traded_by_order[trade.order_id] = traded_by_order.get(trade.order_id, 0) + trade.volume
            notional_by_order[trade.order_id] = (
                notional_by_order.get(trade.order_id, 0.0) + trade.price * trade.volume
            )
        recovered_orders: dict[str, Order] = {}
        status_map = {
            "0": OrderStatus.FILLED,
            "1": OrderStatus.PART_TRADED,
            "2": OrderStatus.CANCELLED,
            "3": OrderStatus.NOT_TRADED,
            "4": OrderStatus.CANCELLED,
            "5": OrderStatus.CANCELLED,
            "b": OrderStatus.NOT_TRADED,
            "c": OrderStatus.NOT_TRADED,
        }
        for row in evidence.orders:
            if row.order_id not in lifecycle_view.current_order_ids:
                continue
            entry = runtime_entries.get(row.order_id)
            if entry is None:
                raise RuntimeError("CTP recovered query order disappeared from durable journal")
            traded = traded_by_order.get(row.order_id, 0)
            recovered_orders[row.order_id] = Order(
                order_id=row.order_id,
                request=entry.request,
                status=status_map[row.order_status],
                traded=traded,
                average_price=(0.0 if traded == 0 else notional_by_order[row.order_id] / traded),
            )
        with self._critical_barrier_lock:
            with self._session_query_ingress_lock:
                self.require_session_activity_evidence_current(evidence)
                if self._order_trade_upstream_pending or self._order_trade_callbacks_inflight:
                    raise RuntimeError("CTP session recovery callback boundary changed")
                self._install_order_submission_runtime_record(
                    runtime_record,
                    identities,
                    evidence.account_identity_digest,
                )
                self._recovered_session_evidence = evidence
                self._recovered_session_trades = plan.session_trades
                self._recovered_session_orders = recovered_orders
        return plan.active_order_ids

    def get_contract_catalog_status(self) -> dict[str, object]:
        snapshot = self._contract_catalog_accumulator.latest_verified
        failure = self._contract_catalog_accumulator.last_failure
        with self._contract_catalog_refresh_state_lock:
            sticky_error = self._contract_catalog_sticky_error
        return {
            "trading_day": "" if snapshot is None else snapshot.trading_day,
            "request_id": 0 if snapshot is None else snapshot.request_id,
            "generation": 0 if snapshot is None else snapshot.generation,
            "digest": "" if snapshot is None else snapshot.catalog_digest,
            "contract_count": 0 if snapshot is None else len(snapshot.contracts),
            "last_failure": "" if failure is None else failure.reason,
            "sticky_error": sticky_error,
        }

    def contract_catalog_verified_for_day(self, trading_day: str) -> bool:
        snapshot = self._contract_catalog_accumulator.latest_verified
        with self._contract_catalog_refresh_state_lock:
            sticky_error = self._contract_catalog_sticky_error
        return bool(
            not sticky_error and snapshot is not None and snapshot.trading_day == trading_day
        )

    def _handle_contract_catalog_response(
        self,
        data,
        error,
        request_id: int,
        last: bool,
    ) -> None:
        with self._critical_callback_scope():
            self._handle_contract_catalog_response_impl(data, error, request_id, last)

    def _handle_contract_catalog_response_impl(
        self,
        data,
        error,
        request_id: int,
        last: bool,
    ) -> None:
        """Accumulate one official query generation and atomically publish on ``last``."""

        failure = ""
        active_request_id = self._contract_catalog_accumulator.active_request_id
        with self._contract_catalog_refresh_state_lock:
            bootstrap_registration_open = self._contract_catalog_bootstrap_registration_open
        unregistered_generation = bool(
            (active_request_id is None and not bootstrap_registration_open)
            or (active_request_id is not None and active_request_id != int(request_id))
        )
        try:
            if self._contract_catalog_accumulator.active_request_id is None:
                self._begin_contract_catalog_generation(
                    request_id=int(request_id),
                    trading_day=self.get_trading_day(),
                    bootstrap_ingress=True,
                )
            snapshot = self._contract_catalog_accumulator.observe(
                int(request_id),
                None if data is None else dict(data),
                None if error is None else dict(error),
                last=bool(last),
            )
        except (CtpContractCatalogIntegrityError, TypeError, ValueError) as exc:
            failure = str(exc)
            if unregistered_generation:
                sticky_error = f"CTP contract catalog: {exc}"
                with self._contract_catalog_refresh_state_lock:
                    if not self._contract_catalog_sticky_error:
                        self._contract_catalog_sticky_error = sticky_error
                self._enqueue_critical(BrokerEvent("broker_error", sticky_error))
            try:
                current_day = self.get_trading_day()
            except RuntimeError:
                current_day = ""
            if not unregistered_generation and not self.contract_catalog_verified_for_day(
                current_day
            ):
                self._enqueue_critical(BrokerEvent("broker_error", f"CTP contract catalog: {exc}"))
            snapshot = None
        if snapshot is not None:
            # Never mutate a published dictionary: raw Tick lookup remains O(1) and
            # sees either the complete old generation or the complete new generation.
            self._contract_catalog = {item.symbol: item for item in snapshot.contracts}
        if snapshot is not None or failure:
            with self._contract_catalog_refresh_state_lock:
                if self._contract_catalog_refresh_request_id == int(request_id):
                    self._contract_catalog_refresh_error = failure
                    event = self._contract_catalog_refresh_event
                    if event is not None:
                        event.set()

    def _begin_contract_catalog_generation(
        self,
        *,
        request_id: int,
        trading_day: str,
        bootstrap_ingress: bool = False,
    ) -> None:
        """Register one query generation before any of its rows are accepted."""

        with self._contract_catalog_refresh_state_lock:
            if bootstrap_ingress and not self._contract_catalog_bootstrap_registration_open:
                raise CtpContractCatalogIntegrityError(
                    "catalog response request id is not registered"
                )
            self._contract_catalog_accumulator.begin(
                request_id=request_id,
                trading_day=trading_day,
            )
            self._contract_catalog_bootstrap_registration_open = False

    def refresh_contract_catalog(
        self,
        *,
        timeout_seconds: float = 10.0,
    ) -> CtpContractCatalogSnapshot:
        """Refresh from CTP outside callbacks/order paths, or reuse same-day evidence."""

        timeout = float(timeout_seconds)
        if not isfinite(timeout) or timeout <= 0.0:
            raise ValueError("CTP catalog refresh timeout must be positive")
        if not self.is_ready() or self._main_engine is None:
            raise RuntimeError("CTP is not ready for contract catalog refresh")
        with self._contract_catalog_refresh_lock:
            with self._contract_catalog_refresh_state_lock:
                sticky_error = self._contract_catalog_sticky_error
            if sticky_error:
                raise RuntimeError(sticky_error)
            day = self.get_trading_day()
            verified = self._contract_catalog_accumulator.latest_verified
            if verified is not None and verified.trading_day == day:
                return verified
            gateway = self._main_engine.get_gateway(self.gateway_name)
            td_api = getattr(gateway, "td_api", None) if gateway is not None else None
            query = getattr(td_api, "reqQryInstrument", None)
            if td_api is None or not callable(query):
                raise RuntimeError("CTP instrument query API is unavailable")
            request_id = self._allocate_ctp_query_request_id(td_api)
            event = Event()
            with self._contract_catalog_refresh_state_lock:
                self._contract_catalog_refresh_request_id = request_id
                self._contract_catalog_refresh_event = event
                self._contract_catalog_refresh_error = ""
            self._begin_contract_catalog_generation(
                request_id=request_id,
                trading_day=day,
            )
            deadline = monotonic() + timeout
            try:
                while True:
                    status = query({}, request_id)
                    if not status:
                        break
                    if monotonic() >= deadline:
                        raise RuntimeError("CTP contract catalog query rate-limit timeout")
                    sleep(min(0.2, max(0.0, deadline - monotonic())))
                if not event.wait(max(0.0, deadline - monotonic())):
                    raise RuntimeError("CTP contract catalog query timeout")
                with self._contract_catalog_refresh_state_lock:
                    failure = self._contract_catalog_refresh_error
                if failure:
                    raise RuntimeError(failure)
                snapshot = self._contract_catalog_accumulator.latest_verified
                if (
                    snapshot is None
                    or snapshot.request_id != request_id
                    or snapshot.trading_day != day
                ):
                    raise RuntimeError("CTP contract catalog completion evidence is invalid")
                return snapshot
            except Exception as exc:
                if self._contract_catalog_accumulator.active_request_id == request_id:
                    self._contract_catalog_accumulator.abort(
                        request_id=request_id,
                        reason=str(exc),
                    )
                raise
            finally:
                with self._contract_catalog_refresh_state_lock:
                    if self._contract_catalog_refresh_request_id == request_id:
                        self._contract_catalog_refresh_request_id = None
                        self._contract_catalog_refresh_event = None

    def get_live_contract_specs(
        self, symbols: list[str], timeout_seconds: float = 10.0
    ) -> dict[str, ContractSpec]:
        """从 CTP/VeighNa 获取乘数、tick、保证金和手续费用于启动安全门。

        查询失败、固定金额保证金等无法可靠映射的情况一律报错，由上层 fail-closed。
        """
        if not self.is_ready() or self._main_engine is None:
            raise RuntimeError("CTP is not ready for metadata query")
        gateway = self._main_engine.get_gateway(self.gateway_name)
        td_api = getattr(gateway, "td_api", None) if gateway else None
        if td_api is None:
            raise RuntimeError("CTP trading API is unavailable")
        contract_map = {c.symbol: c for c in self._main_engine.get_all_contracts()}
        deadline = monotonic() + max(timeout_seconds, 0.1)
        result: dict[str, ContractSpec] = {}
        for symbol in symbols:
            contract = contract_map.get(symbol)
            if contract is None:
                raise RuntimeError(f"CTP contract metadata missing: {symbol}")
            margin = self._query_rate(td_api, "margin", symbol, deadline)
            commission = self._query_rate(td_api, "commission", symbol, deadline)
            long_by_volume = float(margin.get("LongMarginRatioByVolume", 0.0) or 0.0)
            short_by_volume = float(margin.get("ShortMarginRatioByVolume", 0.0) or 0.0)
            if long_by_volume > 0 or short_by_volume > 0:
                raise RuntimeError(f"fixed-per-lot margin is unsupported for validation: {symbol}")
            fee = FeeSpec(
                open_fixed=float(commission.get("OpenRatioByVolume", 0.0) or 0.0),
                open_rate=float(commission.get("OpenRatioByMoney", 0.0) or 0.0),
                close_fixed=float(commission.get("CloseRatioByVolume", 0.0) or 0.0),
                close_rate=float(commission.get("CloseRatioByMoney", 0.0) or 0.0),
                close_today_fixed=float(commission.get("CloseTodayRatioByVolume", 0.0) or 0.0),
                close_today_rate=float(commission.get("CloseTodayRatioByMoney", 0.0) or 0.0),
            )
            result[symbol] = ContractSpec(
                symbol=symbol,
                exchange=contract.exchange.value,
                multiplier=float(contract.size),
                price_tick=float(contract.pricetick),
                margin_rate_long=float(margin.get("LongMarginRatioByMoney", 0.0) or 0.0),
                margin_rate_short=float(margin.get("ShortMarginRatioByMoney", 0.0) or 0.0),
                fee=fee,
            )
        return result

    def _query_rate(self, td_api, kind: str, symbol: str, deadline: float) -> dict:
        reqid = self._allocate_ctp_query_request_id(td_api)
        waiter: _RateWaiter = {
            "kind": kind,
            "event": Event(),
            "rows": [],
            "error": "",
        }
        td_api._afuture_rate_waiters[reqid] = waiter
        try:
            while True:
                if monotonic() >= deadline:
                    raise RuntimeError(f"CTP {kind} query timeout: {symbol}")
                if kind == "margin":
                    status = td_api.reqQryInstrumentMarginRate(
                        {"InstrumentID": symbol, "HedgeFlag": "1"}, reqid
                    )
                else:
                    status = td_api.reqQryInstrumentCommissionRate({"InstrumentID": symbol}, reqid)
                if not status:
                    break
                sleep(0.2)
            remaining = max(0.0, deadline - monotonic())
            if not waiter["event"].wait(remaining):
                raise RuntimeError(f"CTP {kind} query timeout: {symbol}")
            if waiter["error"]:
                raise RuntimeError(f"CTP {kind} query failed for {symbol}: {waiter['error']}")
            if not waiter["rows"]:
                raise RuntimeError(f"CTP {kind} query returned no data: {symbol}")
            return waiter["rows"][-1]
        finally:
            td_api._afuture_rate_waiters.pop(reqid, None)

    def _to_vnpy_order(self, request: OrderRequest):
        runtime = self._load_runtime()
        direction = (
            runtime["Direction"].LONG
            if request.side is OrderSide.BUY
            else runtime["Direction"].SHORT
        )
        offset_map = {
            Offset.OPEN: runtime["Offset"].OPEN,
            Offset.CLOSE: runtime["Offset"].CLOSE,
            Offset.CLOSE_TODAY: runtime["Offset"].CLOSETODAY,
            Offset.CLOSE_YESTERDAY: runtime["Offset"].CLOSEYESTERDAY,
        }
        type_map = {
            OrderType.LIMIT: runtime["OrderType"].LIMIT,
            OrderType.FAK: runtime["OrderType"].FAK,
            OrderType.FOK: runtime["OrderType"].FOK,
        }
        return runtime["OrderRequest"](
            symbol=request.symbol,
            exchange=self._exchange(request.exchange),
            direction=direction,
            type=type_map[request.order_type],
            volume=request.volume,
            price=request.price,
            offset=offset_map[request.offset],
            reference=request.reference,
        )

    def _exchange(self, exchange: str):
        runtime = self._load_runtime()
        try:
            return getattr(runtime["Exchange"], exchange)
        except AttributeError as exc:
            raise ValueError(f"unsupported exchange: {exchange}") from exc

    def _on_tick(self, event) -> None:
        with self._critical_callback_scope():
            self._on_tick_impl(event)

    def _on_tick_impl(self, event) -> None:
        try:
            raw = event.data
            authoritative_day = self.get_trading_day()
            source_day_raw = str(getattr(raw, "_afuture_trading_day", "") or "").strip()
            action_day_raw = str(getattr(raw, "_afuture_action_day", "") or "").strip()
            source_symbol = str(getattr(raw, "_afuture_instrument_id", "") or "").strip()
            source_exchange = str(getattr(raw, "_afuture_exchange_id", "") or "").strip()
            source_day = self._validate_trading_day(source_day_raw) if source_day_raw else ""
            action_day = self._validate_trading_day(action_day_raw) if action_day_raw else ""
            if source_day and source_day != authoritative_day:
                raise ValueError("CTP raw Tick source trading day is stale or not authoritative")
            if source_symbol and source_symbol.upper() != str(raw.symbol).upper():
                raise ValueError("CTP raw Tick source instrument identity mismatch")
            if source_exchange and source_exchange.upper() != str(raw.exchange.value).upper():
                raise ValueError("CTP raw Tick source exchange identity mismatch")
            if action_day and raw.datetime.astimezone(_CHINA).strftime("%Y%m%d") != action_day:
                raise ValueError("CTP raw Tick ActionDay/timestamp identity mismatch")
            tick = Tick(
                symbol=raw.symbol,
                exchange=raw.exchange.value,
                timestamp=raw.datetime,
                bid_price=float(raw.bid_price_1),
                ask_price=float(raw.ask_price_1),
                last_price=float(raw.last_price),
                bid_volume=float(raw.bid_volume_1),
                ask_volume=float(raw.ask_volume_1),
                trading_day=authoritative_day,
                limit_up=float(getattr(raw, "limit_up", 0.0) or 0.0),
                limit_down=float(getattr(raw, "limit_down", 0.0) or 0.0),
                volume=float(getattr(raw, "volume", 0.0) or 0.0),
                open_interest=float(getattr(raw, "open_interest", 0.0) or 0.0),
                open_price=float(getattr(raw, "open_price", 0.0) or 0.0),
                source_trading_day=source_day,
                source_action_day=action_day,
                source_trading_day_verified=bool(source_day),
            )
            tick.validate()
        except Exception as exc:
            self._enqueue_critical(
                BrokerEvent("broker_error", f"CTP tick conversion failed: {exc}")
            )
            return
        try:
            self._notify_raw_tick_observer(
                tick,
                self._contract_catalog.get(tick.symbol),
            )
        except RawMarketEvidenceError:
            # The observer already marked the affected OI day incomplete. This valid
            # Tick must still reach matching/manager paths; fatal revisions use the
            # separate RawMarketEvidenceFatalError type and fall through below.
            pass
        except Exception as exc:
            self._enqueue_critical(
                BrokerEvent("broker_error", f"CTP raw Tick observer failed: {exc}")
            )
            return
        self._enqueue_tick(tick)

    def _on_order(self, event) -> None:
        with self._critical_callback_scope(consumes_upstream=True, order_trade=True):
            self._mark_order_trade_activity()
            self._on_order_impl(event)

    def _on_order_impl(self, event) -> None:
        try:
            order = self._convert_order(event.data)
        except Exception as exc:
            self._enqueue_critical(
                BrokerEvent(
                    "broker_error",
                    f"CTP order conversion failed: {exc}",
                )
            )
            return
        durable = self._order_submission_entries.get(order.order_id)
        if self._order_submission_journal is not None and durable is None:
            self._enqueue_critical(
                BrokerEvent(
                    "broker_error",
                    "CTP order callback has no bounded durable Stress-90 identity",
                )
            )
            return
        if durable is not None and (
            durable.status == "aborted_before_send"
            or (durable.status == "terminal" and order.active)
        ):
            self._enqueue_critical(
                BrokerEvent(
                    "broker_error",
                    "CTP callback conflicts with durable final order status",
                )
            )
            return
        if order.order_id in self._order_submission_entries and not order.active:
            # Callback threads never write files. The engine checkpoints after the event batch.
            with self._order_submission_status_lock:
                self._pending_order_journal_status[order.order_id] = "terminal"
        self._enqueue_critical(BrokerEvent("order", order))

    def checkpoint_order_submission_journal(self) -> None:
        journal = self._order_submission_journal
        if journal is None:
            return
        # Serialize every whole-file read/modify/write with reservation. Callback
        # threads only append to the separate in-memory status map and never wait on fsync.
        with self._order_submission_lock:
            with self._order_submission_status_lock:
                pending = dict(self._pending_order_journal_status)
                pending_fills = {
                    order_id: tuple(sorted(fill_evidence.values(), key=lambda item: item.key))
                    for order_id, fill_evidence in self._pending_order_journal_fills.items()
                }
            if pending or pending_fills:
                updated = journal.apply_updates(statuses=pending, fills=pending_fills)
                with self._order_submission_status_lock:
                    for entry in updated:
                        self._order_submission_entries[entry.order_id] = entry
                    for order_id, status in pending.items():
                        if self._pending_order_journal_status.get(order_id) == status:
                            self._pending_order_journal_status.pop(order_id, None)
                    for order_id, persisted_evidence in pending_fills.items():
                        current = self._pending_order_journal_fills.get(order_id)
                        if current is None:
                            raise RuntimeError("CTP pending fill checkpoint changed inconsistently")
                        snapshot_keys = {item.key for item in persisted_evidence}
                        if not snapshot_keys.issubset(current):
                            raise RuntimeError(
                                "CTP pending fill checkpoint lost an in-flight identity"
                            )
                        for evidence in persisted_evidence:
                            if current.get(evidence.key) != evidence:
                                raise RuntimeError(
                                    "CTP pending fill checkpoint fingerprint changed"
                                )
                        remaining = {
                            key: evidence
                            for key, evidence in current.items()
                            if key not in snapshot_keys
                        }
                        if remaining:
                            self._pending_order_journal_fills[order_id] = remaining
                        else:
                            self._pending_order_journal_fills.pop(order_id, None)

            # Terminal entries move to immutable cold segments after each completed
            # Broker batch. Reinstall only the bounded hot/recent snapshot while the
            # callback barrier is quiescent, so a long-running process has the same
            # replay window and fail-closed cold-callback behavior as a restart.
            journal.compact_terminal()
            runtime_record = journal.load_runtime()
            identities = self._order_journal_policy_identity
            if identities is None:
                raise RuntimeError("Stress-90 CTP order policy identity is missing")
            with self._critical_barrier_lock:
                with self._event_lock:
                    callback_quiescent = not (
                        self._critical_callbacks_inflight
                        or self._critical_upstream_pending
                        or self._critical_events
                    )
                if not callback_quiescent:
                    return
                with self._position_lock:
                    with self._order_submission_status_lock:
                        if self._pending_order_journal_status or self._pending_order_journal_fills:
                            return
                        self._install_order_submission_runtime_record(
                            runtime_record,
                            identities,
                            self.get_account_identity_digest(),
                        )

    def _on_trade(self, event) -> None:
        with self._critical_callback_scope(consumes_upstream=True, order_trade=True):
            self._mark_order_trade_activity()
            self._on_trade_impl(event)

    def _on_trade_impl(self, event) -> None:
        try:
            trade = self._convert_trade(event.data)
            trading_day = self.get_trading_day()
        except Exception as exc:
            self._enqueue_critical(
                BrokerEvent(
                    "broker_error",
                    f"CTP trade conversion failed: {exc}",
                )
            )
            return
        try:
            fill_evidence = ctp_order_fill_evidence(trading_day, trade)
        except Exception as exc:
            self._enqueue_critical(
                BrokerEvent("broker_error", f"CTP trade fingerprint failed: {exc}")
            )
            return
        trade_key = fill_evidence.key
        if self._order_submission_journal is not None and (
            self._order_submission_journal.contains_runtime_sealed_fill_identity(
                account_identity_digest=self.get_account_identity_digest(),
                fill_identity=trade_key,
            )
        ):
            self._enqueue_critical(
                BrokerEvent(
                    "broker_error",
                    "CTP trade identity was already recorded in a sealed account epoch",
                )
            )
            return
        if trade_key in self._seen_trade_keys:
            expected = self._seen_trade_fingerprints.get(trade_key)
            if self._order_submission_journal is not None and expected != fill_evidence:
                self._enqueue_critical(
                    BrokerEvent(
                        "broker_error",
                        "duplicate CTP trade identity fingerprint mismatch",
                    )
                )
            return
        submission = self._order_submission_entries.get(trade.order_id)
        if self._order_submission_journal is not None:
            if submission is None:
                self._enqueue_critical(
                    BrokerEvent(
                        "broker_error",
                        "CTP trade has no durable Stress-90 order identity",
                    )
                )
                return
            if submission.status == "aborted_before_send":
                self._enqueue_critical(
                    BrokerEvent(
                        "broker_error",
                        "CTP trade exists for an order aborted before official send",
                    )
                )
                return
            request = submission.request
            if submission.target_trading_day != trading_day or (
                trade.symbol,
                trade.exchange,
                trade.side,
                trade.offset,
            ) != (
                request.symbol,
                request.exchange,
                request.side,
                request.offset,
            ):
                self._enqueue_critical(
                    BrokerEvent(
                        "broker_error",
                        "durable CTP trade request mismatch",
                    )
                )
                return
            price_tolerance = max(1e-10, abs(float(request.price)) * 1e-12)
            worse_than_limit = (
                trade.side is OrderSide.BUY and trade.price > float(request.price) + price_tolerance
            ) or (
                trade.side is OrderSide.SELL
                and trade.price < float(request.price) - price_tolerance
            )
            if worse_than_limit:
                self._enqueue_critical(
                    BrokerEvent("broker_error", "CTP trade violates durable limit price")
                )
                return
        with self._position_lock:
            if trade_key in self._seen_trade_keys:
                expected = self._seen_trade_fingerprints.get(trade_key)
                if self._order_submission_journal is not None and expected != fill_evidence:
                    self._enqueue_critical(
                        BrokerEvent(
                            "broker_error",
                            "duplicate CTP trade identity fingerprint mismatch",
                        )
                    )
                return
            if (
                self._order_submission_journal is not None
                and len(self._seen_trade_keys) >= self._MAX_SEEN_TRADE_KEYS
            ):
                self._enqueue_critical(
                    BrokerEvent(
                        "broker_error",
                        "CTP durable fill replay capacity is exhausted before position mutation",
                    )
                )
                return
            try:
                book = PositionBook(self._copy_positions_unlocked())
                book.apply_trade(trade)
            except Exception as exc:
                self._enqueue_critical(
                    BrokerEvent("broker_error", f"CTP trade mirror update failed: {exc}")
                )
                return
            if submission is not None:
                with self._order_submission_status_lock:
                    durable_submission = self._order_submission_entries.get(trade.order_id)
                    if durable_submission is None:
                        self._enqueue_critical(
                            BrokerEvent(
                                "broker_error",
                                "CTP trade durable identity disappeared concurrently",
                            )
                        )
                        return
                    pending_evidence = dict(
                        self._pending_order_journal_fills.get(trade.order_id, {})
                    )
                    if trade_key in pending_evidence:
                        if pending_evidence[trade_key] != fill_evidence:
                            self._enqueue_critical(
                                BrokerEvent(
                                    "broker_error",
                                    "pending CTP trade identity fingerprint mismatch",
                                )
                            )
                        return
                    pending_volume = sum(item.volume for item in pending_evidence.values())
                    if (
                        durable_submission.filled_volume + pending_volume + trade.volume
                        > durable_submission.request.volume
                    ):
                        self._enqueue_critical(
                            BrokerEvent(
                                "broker_error",
                                "CTP cumulative trade volume exceeds durable order volume",
                            )
                        )
                        return
                    pending_evidence[trade_key] = fill_evidence
                    self._pending_order_journal_fills[trade.order_id] = pending_evidence
            self._positions = {
                (position.symbol, position.exchange): position for position in book.all()
            }
            self._seen_trade_keys.add(trade_key)
            self._seen_trade_order.append(trade_key)
            if self._order_submission_journal is not None:
                self._seen_trade_fingerprints[trade_key] = fill_evidence
            if (
                self._order_submission_journal is None
                and len(self._seen_trade_order) > self._MAX_SEEN_TRADE_KEYS
            ):
                expired = self._seen_trade_order.popleft()
                self._seen_trade_keys.discard(expired)
                self._seen_trade_fingerprints.pop(expired, None)
        self._enqueue_critical(BrokerEvent("trade", trade))

    def _convert_trade(self, raw) -> Trade:
        numeric_volume = float(raw.volume)
        if (
            isinstance(raw.volume, bool)
            or not isfinite(numeric_volume)
            or numeric_volume <= 0
            or not numeric_volume.is_integer()
        ):
            raise ValueError(f"invalid CTP trade volume: {raw.volume!r}")
        price = float(raw.price)
        if not isfinite(price) or price <= 0:
            raise ValueError(f"invalid CTP trade price: {raw.price!r}")
        timestamp = getattr(raw, "datetime", None)
        if (
            not isinstance(timestamp, datetime)
            or timestamp.tzinfo is None
            or timestamp.utcoffset() is None
        ):
            raise ValueError("invalid CTP trade timestamp: timezone-aware datetime required")
        trade = Trade(
            raw.vt_tradeid,
            raw.vt_orderid,
            raw.symbol,
            raw.exchange.value,
            self._direction_to_side(raw.direction),
            self._offset_to_model(raw.offset),
            int(numeric_volume),
            price,
            timestamp,
        )
        trade.validate()
        return trade

    def get_session_trades(self) -> list[Trade]:
        evidence = self._recovered_session_evidence
        if evidence is not None:
            self.require_session_activity_evidence_current(evidence)
        raw_trades = (
            []
            if self._main_engine is None
            else [self._convert_trade(raw) for raw in self._main_engine.get_all_trades()]
        )
        merged: dict[tuple[str, str], Trade] = {
            (trade.exchange, trade.trade_id): trade for trade in self._recovered_session_trades
        }
        for trade in raw_trades:
            key = (trade.exchange, trade.trade_id)
            prior = merged.get(key)
            if prior is not None and prior != trade:
                raise RuntimeError("CTP recovered/live session trade fingerprint mismatch")
            merged[key] = trade
        return sorted(
            merged.values(),
            key=lambda item: (item.timestamp, item.exchange, item.trade_id),
        )

    def _on_account(self, event) -> None:
        with self._critical_callback_scope(consumes_upstream=True):
            self._on_account_impl(event)

    def _on_account_impl(self, event) -> None:
        evidence: _CtpAccountEvidence | None = None
        if self._has_expected_account_identity:
            if not self._account_data_matches_expected(event.data):
                self._invalidate_account_identity("CTP AccountData identity mismatch")
                return
            with self._account_lock:
                if self._pending_account_evidence:
                    evidence = self._pending_account_evidence.popleft()
            if evidence is None:
                self._invalidate_account_identity(
                    "CTP AccountData has no fresh authoritative query evidence"
                )
                return
        try:
            account = self._convert_account(event.data, evidence=evidence)
        except Exception as exc:
            self._enqueue_critical(
                BrokerEvent("account_error", f"CTP account conversion failed: {exc}")
            )
            with self._account_lock:
                self._account_identity_verified_state = False
                self._account_identity_verified_day = ""
            return
        with self._account_lock:
            pair_is_current = bool(
                self._has_expected_account_identity
                and account.cash_flow_verified
                and evidence is not None
                and evidence.generation == self._account_evidence_generation
                and not self._pending_account_evidence
            )
            if account.cash_flow_verified and not pair_is_current:
                account = replace(
                    account,
                    deposit=0.0,
                    withdrawal=0.0,
                    cash_flow_verified=False,
                    previous_settlement_equity=None,
                    settlement_verified=False,
                    settlement_id=None,
                )
            self._last_account = account
            self._account_identity_verified_state = pair_is_current
            self._account_identity_verified_day = (
                account.trading_day if self._account_identity_verified_state else ""
            )
            if pair_is_current and evidence is not None:
                self._paired_account_evidence_generation = evidence.generation
                with self._critical_barrier_lock:
                    self._account_evidence_pair_pending = False
        self._account_event_generation += 1
        self._last_account_monotonic = monotonic()
        self._enqueue_critical(BrokerEvent("account", self._last_account))

    def _handle_account_cash_flow(self, raw: dict[str, Any]) -> bool:
        with self._critical_callback_scope():
            return self._handle_account_cash_flow_impl(raw)

    def _handle_account_cash_flow_impl(self, raw: dict[str, Any]) -> bool:
        day = str(raw.get("TradingDay", "") or "")
        try:
            day = self._validate_trading_day(day)
            authoritative_day = self.get_trading_day()
            if day != authoritative_day:
                raise ValueError("CTP account evidence day is stale or not authoritative")
            broker_id, account_id, currency_id, investor_id, invest_unit_id = (
                self._validate_raw_account_identity(raw)
            )
            deposit = float(raw["Deposit"])
            withdrawal = float(raw["Withdraw"])
            pre_balance = float(raw["PreBalance"])
            settlement_id_raw = raw["SettlementID"]
            if isinstance(settlement_id_raw, bool):
                raise ValueError("invalid CTP settlement id")
            settlement_id = int(settlement_id_raw)
            if float(settlement_id_raw) != settlement_id or settlement_id < 0:
                raise ValueError("invalid CTP settlement id")
            if (
                not isfinite(deposit)
                or not isfinite(withdrawal)
                or not isfinite(pre_balance)
                or min(deposit, withdrawal) < 0
                or pre_balance <= 0
            ):
                raise ValueError("invalid CTP account cash-flow values")
            with self._account_lock:
                if self._last_verified_account_evidence_day and (
                    day < self._last_verified_account_evidence_day
                ):
                    raise ValueError("CTP account evidence trading day moved backward")
                previous = self._account_cash_flow.get(day)
                if previous is not None and (
                    deposit != previous.deposit
                    or withdrawal != previous.withdrawal
                    or pre_balance != previous.pre_balance
                    or settlement_id != previous.settlement_id
                    or (broker_id, account_id, currency_id, investor_id, invest_unit_id)
                    != (
                        previous.broker_id,
                        previous.account_id,
                        previous.currency_id,
                        previous.investor_id,
                        previous.invest_unit_id,
                    )
                ):
                    raise ValueError("CTP same-day account settlement evidence mutated")
                if (
                    self._has_expected_account_identity
                    and len(self._pending_account_evidence) >= self._MAX_PENDING_ACCOUNT_EVIDENCE
                ):
                    raise ValueError("CTP account evidence callback backlog is full")
        except (KeyError, RuntimeError, TypeError, ValueError) as exc:
            error_day = day if re.fullmatch(r"\d{8}", day) else self._trading_day
            with self._account_lock:
                self._account_cash_flow_error.clear()
                if error_day:
                    self._account_cash_flow_error[error_day] = str(exc)
                    self._account_cash_flow.pop(error_day, None)
            self._invalidate_account_identity(f"CTP account evidence rejected: {exc}")
            return False
        with self._account_lock:
            self._account_evidence_generation += 1
            evidence = _CtpAccountEvidence(
                broker_id,
                account_id,
                currency_id,
                investor_id,
                invest_unit_id,
                day,
                deposit,
                withdrawal,
                pre_balance,
                settlement_id,
                self._account_evidence_generation,
            )
            self._account_cash_flow = {day: evidence}
            if self._has_expected_account_identity:
                # Each raw query response precedes exactly one VeighNa AccountData
                # event. Preserve that order: replacing the queue here could pair an
                # older AccountData balance with the newest settlement generation.
                self._pending_account_evidence.append(evidence)
                self._account_identity_verified_state = False
                self._account_identity_verified_day = ""
                with self._critical_barrier_lock:
                    self._account_evidence_pair_pending = True
            self._last_verified_account_evidence_day = day
            self._account_cash_flow_error.clear()
        return True

    def _validate_raw_account_identity(
        self,
        raw: dict[str, Any],
    ) -> tuple[str, str, str, str, str]:
        if not self._has_expected_account_identity:
            return ("", "", "", "", "")
        required = (
            ("BrokerID", self.credentials.broker_id),
            ("AccountID", self.credentials.account_id),
            ("CurrencyID", self.credentials.currency_id),
        )
        for field, expected in required:
            actual = raw.get(field)
            if not isinstance(actual, str) or actual != expected:
                raise ValueError(f"CTP raw account {field} mismatch")
        optional = (
            ("InvestorID", self.credentials.investor_id),
            ("InvestUnitID", self.credentials.invest_unit_id),
        )
        verified_optional: list[str] = []
        for field, expected in optional:
            actual = raw.get(field)
            if expected:
                if not isinstance(actual, str) or actual != expected:
                    raise ValueError(f"CTP raw account {field} mismatch")
                verified_optional.append(actual)
            else:
                verified_optional.append("")
        return (
            self.credentials.broker_id,
            self.credentials.account_id,
            self.credentials.currency_id,
            verified_optional[0],
            verified_optional[1],
        )

    def _account_data_matches_expected(self, raw: Any) -> bool:
        if not self._has_expected_account_identity:
            return True
        if getattr(raw, "accountid", None) != self.credentials.account_id:
            return False
        for attribute in ("currencyid", "currency_id", "currency"):
            actual = getattr(raw, attribute, None)
            if actual not in (None, ""):
                return actual == self.credentials.currency_id
        return True

    def _invalidate_account_identity(self, reason: str) -> None:
        with self._account_lock:
            self._account_identity_verified_state = False
            self._account_identity_verified_day = ""
            self._last_account = None
            self._pending_account_evidence.clear()
        self._enqueue_critical(BrokerEvent("broker_error", reason))

    def _handle_position_snapshot(self, raw_positions: list[Any]) -> None:
        with self._critical_callback_scope():
            self._handle_position_snapshot_impl(raw_positions)

    def _handle_position_snapshot_impl(self, raw_positions: list[Any]) -> None:
        with self._position_lock:
            combined: dict[tuple[str, str], ContractPosition] = {}
            try:
                for raw in raw_positions:
                    volume = self._exact_integer(raw.volume, "position volume", positive=True)
                    yesterday = self._exact_integer(
                        raw.yd_volume,
                        "yesterday position volume",
                        positive=False,
                    )
                    if yesterday > volume:
                        raise ValueError(f"invalid CTP position volume for {raw.symbol}")
                    today = volume - yesterday
                    symbol = str(raw.symbol).strip()
                    exchange = str(raw.exchange.value).strip()
                    if not symbol or not exchange:
                        raise ValueError("CTP position identity is empty")
                    price = float(raw.price)
                    if not isfinite(price) or price <= 0:
                        raise ValueError(f"invalid CTP position price for {symbol}")
                    key = (symbol, exchange)
                    position = combined.setdefault(key, ContractPosition(symbol, exchange))
                    side = self._direction_to_side(raw.direction)
                    if side is OrderSide.BUY:
                        position.long_today += today
                        position.long_yesterday += yesterday
                        position.long_price = price
                    else:
                        position.short_today += today
                        position.short_yesterday += yesterday
                        position.short_price = price
                for position in combined.values():
                    position.validate()
            except Exception as exc:
                self._enqueue_critical(BrokerEvent("broker_error", str(exc)))
                return
            self._positions = {
                key: position for key, position in combined.items() if not position.empty
            }
            self._position_snapshot_generation += 1
            self._last_position_snapshot_monotonic = monotonic()
            self._enqueue_critical(
                BrokerEvent("position_snapshot", self._copy_positions_unlocked())
            )

    def _convert_account(
        self,
        raw,
        *,
        evidence: _CtpAccountEvidence | None = None,
    ) -> AccountSnapshot:
        balance = float(raw.balance)
        available = float(raw.available)
        # VeighNa AccountData 不暴露 CurrMargin，用权益与可用资金差额作为保守代理。
        margin = max(0.0, balance - available)
        trading_day = self.get_trading_day()
        identity_verified = bool(
            self._has_expected_account_identity
            and evidence is not None
            and evidence.trading_day == trading_day
            and self._account_evidence_is_current(evidence)
            and self._account_data_matches_expected(raw)
        )
        verified_evidence = evidence if identity_verified else None
        account = AccountSnapshot(
            balance,
            balance,
            available,
            margin,
            0.0,
            0.0,
            trading_day,
            deposit=0.0 if verified_evidence is None else verified_evidence.deposit,
            withdrawal=0.0 if verified_evidence is None else verified_evidence.withdrawal,
            cash_flow_verified=identity_verified,
            previous_settlement_equity=(
                None if verified_evidence is None else verified_evidence.pre_balance
            ),
            settlement_verified=identity_verified,
            settlement_id=(None if verified_evidence is None else verified_evidence.settlement_id),
        )
        account.validate()
        return account

    def _account_evidence_is_current(self, evidence: _CtpAccountEvidence) -> bool:
        with self._account_lock:
            current = self._account_cash_flow.get(evidence.trading_day)
            if current is None or evidence.trading_day in self._account_cash_flow_error:
                return False
            return (
                evidence.broker_id,
                evidence.account_id,
                evidence.currency_id,
                evidence.investor_id,
                evidence.invest_unit_id,
                evidence.trading_day,
                evidence.deposit,
                evidence.withdrawal,
                evidence.pre_balance,
                evidence.settlement_id,
                evidence.generation,
            ) == (
                current.broker_id,
                current.account_id,
                current.currency_id,
                current.investor_id,
                current.invest_unit_id,
                current.trading_day,
                current.deposit,
                current.withdrawal,
                current.pre_balance,
                current.settlement_id,
                current.generation,
            )

    def _convert_order(self, raw) -> Order:
        volume = self._exact_integer(raw.volume, "order volume", positive=True)
        traded = self._exact_integer(raw.traded, "order traded", positive=False)
        if traded > volume:
            raise ValueError("CTP order traded volume exceeds order volume")
        price = float(raw.price)
        if not isfinite(price) or price <= 0:
            raise ValueError(f"invalid CTP order price: {raw.price!r}")
        durable = self._order_submission_entries.get(raw.vt_orderid)
        reference = (
            durable.request.reference
            if durable is not None
            else getattr(raw, "reference", "") or self._order_references.get(raw.vt_orderid, "")
        )
        request = OrderRequest(
            raw.symbol,
            raw.exchange.value,
            self._direction_to_side(raw.direction),
            self._offset_to_model(raw.offset),
            volume,
            price,
            self._type_to_model(raw.type),
            reference,
        )
        if durable is not None:
            expected = durable.request
            observed_economics = (
                request.symbol,
                request.exchange,
                request.side,
                request.offset,
                request.volume,
                request.price,
                request.order_type,
            )
            expected_economics = (
                expected.symbol,
                expected.exchange,
                expected.side,
                expected.offset,
                expected.volume,
                expected.price,
                expected.order_type,
            )
            if observed_economics != expected_economics:
                raise ValueError(f"durable CTP order request mismatch: {raw.vt_orderid}")
        return Order(
            raw.vt_orderid,
            request,
            self._status_to_model(raw.status),
            traded,
            price,
        )

    @staticmethod
    def _exact_integer(value: Any, field: str, *, positive: bool) -> int:
        if isinstance(value, bool):
            raise ValueError(f"invalid CTP {field}: {value!r}")
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid CTP {field}: {value!r}") from exc
        if not isfinite(numeric) or not numeric.is_integer():
            raise ValueError(f"invalid CTP {field}: {value!r}")
        integer = int(numeric)
        if integer < 0 or (positive and integer <= 0):
            raise ValueError(f"invalid CTP {field}: {value!r}")
        return integer

    @staticmethod
    def _enum_name(value: object, field: str) -> str:
        """Return an explicit protocol enum name; never guess an economic default."""
        name = getattr(value, "name", None)
        if not isinstance(name, str) or not name:
            raise ValueError(f"unsupported CTP {field}: {value!r}")
        return name.upper()

    @classmethod
    def _direction_to_side(cls, value: object) -> OrderSide:
        name = cls._enum_name(value, "direction")
        mapping = {
            "LONG": OrderSide.BUY,
            "SHORT": OrderSide.SELL,
        }
        try:
            return mapping[name]
        except KeyError as exc:
            raise ValueError(f"unsupported CTP direction: {name}") from exc

    @classmethod
    def _offset_to_model(cls, value: object) -> Offset:
        name = cls._enum_name(value, "offset")
        mapping = {
            "OPEN": Offset.OPEN,
            "CLOSE": Offset.CLOSE,
            "CLOSETODAY": Offset.CLOSE_TODAY,
            "CLOSEYESTERDAY": Offset.CLOSE_YESTERDAY,
        }
        try:
            return mapping[name]
        except KeyError as exc:
            raise ValueError(f"unsupported CTP offset: {name}") from exc

    @classmethod
    def _type_to_model(cls, value: object) -> OrderType:
        name = cls._enum_name(value, "order type")
        mapping = {
            "LIMIT": OrderType.LIMIT,
            "FAK": OrderType.FAK,
            "FOK": OrderType.FOK,
        }
        try:
            return mapping[name]
        except KeyError as exc:
            raise ValueError(f"unsupported CTP order type: {name}") from exc

    def _status_to_model(self, value: object) -> OrderStatus:
        runtime = self._load_runtime()
        status_map = {
            runtime["Status"].SUBMITTING: OrderStatus.SUBMITTING,
            runtime["Status"].NOTTRADED: OrderStatus.NOT_TRADED,
            runtime["Status"].PARTTRADED: OrderStatus.PART_TRADED,
            runtime["Status"].ALLTRADED: OrderStatus.FILLED,
            runtime["Status"].CANCELLED: OrderStatus.CANCELLED,
            runtime["Status"].REJECTED: OrderStatus.REJECTED,
        }
        try:
            return status_map[value]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"unsupported CTP status: {value!r}") from exc
