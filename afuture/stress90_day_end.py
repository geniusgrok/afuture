"""Normal day-end coordination under the existing account lease and lifecycle WAL.

A provider is an in-process, read-only capability installed by the Broker adapter.
There is deliberately no TOML/file/CLI switch accepting user-certified settlement.
The real CTP adapter has no authenticated provider yet and remains disabled.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from hashlib import sha256
from math import isclose, isfinite
from time import monotonic, sleep

from .models import (
    ContractPosition,
    Offset,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    RuntimeMode,
    Trade,
)
from .position import PositionBook
from .reconcile import compare_positions
from .state import RuntimeState

DAY_END_KEY = "stress90_day_end"
STAGE_KEY = "stress90_live_permission"
_SHA = re.compile(r"[0-9a-f]{64}")


def _valid_day(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.strptime(value, "%Y%m%d").strftime("%Y%m%d") == value
    except ValueError:
        return False


def finish_stress90_day_end(engine, lease, *, timeout_seconds: float) -> None:
    """Bounded CLI entry to the same event drain and coordinator as the resident loop."""
    engine.bind_day_end_lease(lease)
    coordinator = engine.day_end_coordinator
    if coordinator is None or coordinator.provider is None:
        raise RuntimeError("authenticated final settlement provider is not configured")
    coordinator._require_stage(engine)
    engine.initialize_after_ready()
    deadline = monotonic() + timeout_seconds
    while engine.day_end_paused:
        engine.run_once(allow_strategy=False)
        if engine.halted:
            raise RuntimeError(engine.state.kill_reason)
        if monotonic() >= deadline and engine.day_end_paused:
            raise RuntimeError(coordinator.last_error or "final settlement is not ready")
        if engine.day_end_paused:
            sleep(0.01)


def evidence_digest(value) -> str:
    def encode(item):
        if isinstance(item, datetime):
            return item.isoformat()
        raise TypeError(f"unsupported settlement value: {type(item).__name__}")

    return sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=encode
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Stress90FinalSettlement:
    """Normalized facts from one complete, authenticated, final query generation.

    Completeness/finality comes from the controlled provider and its pinned source
    reference, not a boolean supplied in this payload. Monetary conservation,
    ownership and local event consumption are checked again by the coordinator.
    """

    account_identity_digest: str
    account_epoch: str
    currency: str
    source_day: str
    target_day: str
    version: int
    source_reference: str
    source_digest: str
    query_generation: str
    start_equity: float
    end_equity: float
    realized_pnl: float
    unrealized_pnl_change: float
    fees: float
    deposit: float
    withdrawal: float
    orders: tuple[Order, ...]
    trades: tuple[Trade, ...]
    positions: tuple[ContractPosition, ...]

    @property
    def digest(self) -> str:
        return evidence_digest(asdict(self))


def normal_day_end_pause(state: RuntimeState) -> bool:
    marker = state.strategy_states.get(DAY_END_KEY)
    return bool(
        isinstance(marker, dict)
        and set(marker)
        == {
            "source_day",
            "target_day",
            "day_start_equity",
            "high_watermark",
            "settlement_digest",
            "phase",
        }
        and marker["phase"] == "paused"
        and all(_valid_day(marker[key]) for key in ("source_day", "target_day"))
        and all(
            not isinstance(marker[key], bool)
            and isinstance(marker[key], (float, int))
            and isfinite(marker[key])
            and marker[key] > 0
            for key in ("day_start_equity", "high_watermark")
        )
        and isinstance(marker["settlement_digest"], str)
        and (
            not marker["settlement_digest"]
            or _SHA.fullmatch(marker["settlement_digest"]) is not None
        )
        and marker["source_day"] < marker["target_day"]
        and state.trading_day in {marker["source_day"], marker["target_day"]}
        and state.runtime_mode == RuntimeMode.RUNNING.value
        and not state.kill_switch
        and not state.directional_daily_circuit_day
    )


def continuation_stage(evidence, permit_id: str, runtime_dir, valid_until: str) -> dict:
    expires = datetime.fromisoformat(valid_until)
    if expires.tzinfo is None or expires.strftime("%Y%m%d") <= evidence.ctp_trading_day:
        raise RuntimeError("continuation authorization expiry must be aware and after activation")
    return {
        "permit_id": permit_id,
        "account_identity_digest": evidence.account_identity_digest,
        "account_epoch": evidence.policy_account_epoch,
        "runtime": str(runtime_dir.resolve()),
        "risk_overlay_digest": evidence.risk_overlay_digest,
        "valid_until": expires.isoformat(),
        "first_day": evidence.ctp_trading_day,
        "last_settlement": {},
    }


def require_continuation_stage(
    *,
    runtime_dir,
    state_store,
    policy_store,
    permit_store,
    account_identity_digest,
    account_registry_path,
    lease,
    clock,
):
    from .account_runtime_registry import AccountRuntimeRegistry

    state = state_store.load_required_record().state
    stage = state.strategy_states.get(STAGE_KEY)
    if not isinstance(stage, dict) or set(stage) != {
        "permit_id",
        "account_identity_digest",
        "account_epoch",
        "runtime",
        "risk_overlay_digest",
        "valid_until",
        "first_day",
        "last_settlement",
    }:
        raise RuntimeError("normal day-end has no initial continuation authorization")
    account = account_identity_digest
    policy = policy_store.load_required()
    marker = state.strategy_states["directional_policy_identity"]
    if (
        stage["account_identity_digest"] != account
        or stage["account_epoch"] != policy.live_account_epoch
        or stage["runtime"] != str(runtime_dir.resolve())
        or stage["risk_overlay_digest"] != marker.get("risk_overlay_digest")
    ):
        raise RuntimeError("normal day-end continuation identity changed")
    authorizes = getattr(lease, "authorizes_technical_activation", None)
    if not callable(authorizes) or not authorizes(account, runtime_dir):
        raise RuntimeError("normal day-end requires the matching account lease")
    AccountRuntimeRegistry(account_registry_path).require_binding_evidence(
        account, runtime_dir, policy.live_account_epoch
    )
    permit = permit_store.load_required_record().permit
    if (
        permit.status != "consumed"
        or permit.permit_id != stage["permit_id"]
        or stage["first_day"] != permit.evidence.ctp_trading_day
        or stage["account_identity_digest"] != permit.evidence.account_identity_digest
        or stage["account_epoch"] != permit.evidence.policy_account_epoch
        or stage["risk_overlay_digest"] != permit.evidence.risk_overlay_digest
    ):
        raise RuntimeError("initial continuation authorization was revoked")
    now = clock()
    expiry = datetime.fromisoformat(stage["valid_until"])
    if now.tzinfo is None or expiry.tzinfo is None or now >= expiry:
        raise RuntimeError("initial continuation authorization expired")
    if state.kill_switch or state.runtime_mode != RuntimeMode.RUNNING.value:
        raise RuntimeError("manual or hard-risk stop blocks normal continuation")
    return stage


class Stress90DayEndCoordinator:
    """Small persistent technical pause; RiskManager still owns all risk decisions."""

    def __init__(
        self, runtime_dir, *, account_registry_path, provider=None, require_day_evidence=False
    ):
        from .stress90_lifecycle_transaction import Stress90LifecycleTransactionStore

        self.runtime_dir = runtime_dir
        self.account_registry_path = account_registry_path
        self.provider = provider
        self.require_day_evidence = require_day_evidence
        self.lease = None
        self.transactions = Stress90LifecycleTransactionStore(
            runtime_dir / "stress90_lifecycle_transaction.json"
        )
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="afuture-day-end")
        self._query: Future | None = None
        self._query_days: tuple[str, str] | None = None
        self.last_error = ""

    def close(self):
        self._worker.shutdown(wait=False, cancel_futures=True)

    def bind_lease(self, lease):
        self.lease = lease

    def _require_stage(self, engine):
        return require_continuation_stage(
            runtime_dir=self.runtime_dir,
            state_store=engine.state_store,
            policy_store=engine.directional_manager.policy_state_store,
            permit_store=engine.technical_activation_authority.permit_store,
            account_identity_digest=engine.broker.get_account_identity_digest(),
            account_registry_path=self.account_registry_path,
            lease=self.lease,
            clock=engine.health_clock,
        )

    def require_order(self, engine, request):
        if request.offset is not Offset.OPEN:
            # Protection remains subject to the existing position/quantity/quote gates.
            if self.transactions.load() is not None and (
                self.transactions.load_required().status == "prepared"
            ):
                raise RuntimeError("day-end commit blocks all order writes")
            return
        if STAGE_KEY in engine.state.strategy_states:
            self._require_stage(engine)
        if DAY_END_KEY in engine.state.strategy_states:
            raise RuntimeError("normal day-end technical pause blocks opening")

    def pause(self, engine, target_day: str):
        if self.provider is None:
            raise RuntimeError("authenticated final settlement provider is not configured")
        self._require_stage(engine)
        current = engine.state_store.load_required_record()
        if current.state != engine.state:
            raise RuntimeError("runtime changed before technical pause")
        old = current.state.strategy_states.get(DAY_END_KEY)
        if old is not None:
            if not normal_day_end_pause(current.state) or old["target_day"] != target_day:
                raise RuntimeError("illegal day-end session jump")
            return
        from .directional_stress90_policy import STRESS90_POLICY
        from .runtime_calendar import RuntimeTradingCalendar

        calendar = RuntimeTradingCalendar.load()
        exchanges = {calendar.products[p].exchange for p in STRESS90_POLICY.products}
        if any(
            calendar.next_trading_day(current.state.trading_day, x) != target_day for x in exchanges
        ):
            raise RuntimeError("illegal day-end session jump")
        marker = {
            "phase": "paused",
            "source_day": current.state.trading_day,
            "target_day": target_day,
            "day_start_equity": current.state.day_start_equity,
            "high_watermark": current.state.equity_high_watermark,
            "settlement_digest": "",
        }
        paused = replace(
            current.state, strategy_states={**current.state.strategy_states, DAY_END_KEY: marker}
        )
        record = engine.state_store.save(
            paused, expected_sequence=current.sequence, expected_checksum=current.checksum
        )
        engine.state = record.state
        engine._record("stress90_day_end_pause", marker)

    def trade_day(self, engine, trade):
        if self.provider is None:
            return None
        day = self.provider.trade_trading_day(trade)
        marker = engine.state.strategy_states.get(DAY_END_KEY)
        if marker is not None and day == marker["source_day"]:
            if engine.state.trading_day == day:
                return day
        identity = engine._trade_identity(trade, day)
        if day != engine.state.trading_day and identity not in engine._recent_trade_id_set:
            raise RuntimeError("late unprocessed fill after settlement commit")
        return day

    def require_order_event(self, engine, order):
        if self.provider is None:
            return
        day = self.provider.order_trading_day(order)
        if day != engine.state.trading_day and order.active:
            raise RuntimeError("late nonterminal source order after settlement")

    def _validate_facts(self, engine, facts, marker, *, check_local=True):
        if not isinstance(facts, Stress90FinalSettlement):
            raise RuntimeError("final settlement query payload is invalid")
        stage = self._require_stage(engine)
        if (
            facts.account_identity_digest != stage["account_identity_digest"]
            or facts.account_epoch != stage["account_epoch"]
            or facts.currency != "CNY"
            or facts.source_day != marker["source_day"]
            or facts.target_day != marker["target_day"]
            or isinstance(facts.version, bool)
            or not isinstance(facts.version, int)
            or facts.version < 1
            or not facts.source_reference
            or not facts.query_generation
            or _SHA.fullmatch(facts.source_digest) is None
        ):
            raise RuntimeError("final settlement identity/version/source mismatch")
        self.provider.require_current(facts)
        amounts = (
            facts.start_equity,
            facts.end_equity,
            facts.realized_pnl,
            facts.unrealized_pnl_change,
            facts.fees,
            facts.deposit,
            facts.withdrawal,
        )
        if (
            any(
                isinstance(v, bool) or not isinstance(v, (float, int)) or not isfinite(v)
                for v in amounts
            )
            or facts.start_equity <= 0
            or facts.end_equity <= 0
        ):
            raise RuntimeError("final settlement amounts are invalid")
        if facts.fees < 0 or facts.deposit != 0 or facts.withdrawal != 0:
            raise RuntimeError("external funding requires explicit account rebase")
        conserved = (
            facts.start_equity
            + facts.realized_pnl
            + facts.unrealized_pnl_change
            - facts.fees
            + facts.deposit
            - facts.withdrawal
        )
        if not isclose(conserved, facts.end_equity, abs_tol=1e-8, rel_tol=1e-12):
            raise RuntimeError("final settlement funding conservation failed")
        if not isclose(facts.start_equity, marker["day_start_equity"], abs_tol=1e-8, rel_tol=1e-12):
            raise RuntimeError("final settlement source baseline changed")
        account = engine.broker.get_account()
        account.validate()
        if (
            account.trading_day != facts.target_day
            or account.settlement_id != facts.version
            or not account.settlement_verified
            or not account.cash_flow_verified
            or account.deposit != 0
            or account.withdrawal != 0
            or not isclose(
                float(account.previous_settlement_equity or 0),
                facts.end_equity,
                rel_tol=1e-12,
                abs_tol=1e-8,
            )
        ):
            raise RuntimeError("final settlement and Broker account disagree")
        orders = {}
        for order in facts.orders:
            if (
                not isinstance(order, Order)
                or not isinstance(order.request, OrderRequest)
                or not isinstance(order.request.side, OrderSide)
                or not isinstance(order.request.offset, Offset)
                or not isinstance(order.request.order_type, OrderType)
                or not isinstance(order.status, OrderStatus)
                or order.order_id in orders
                or order.active
                or not engine.broker.owns_order(order.order_id)
                or isinstance(order.traded, bool)
                or not isinstance(order.traded, int)
                or isinstance(order.request.volume, bool)
                or not isinstance(order.request.volume, int)
                or order.request.volume <= 0
                or not isfinite(order.request.price)
                or order.request.price <= 0
                or not 0 <= order.traded <= order.request.volume
                or (order.status is OrderStatus.FILLED and order.traded != order.request.volume)
            ):
                raise RuntimeError("unknown, duplicate or nonterminal settlement order")
            orders[order.order_id] = order
        traded: dict[str, int] = defaultdict(int)
        identities = set()
        for trade in facts.trades:
            trade.validate()
            identity = engine._trade_identity(trade, facts.source_day)
            fill_order = orders.get(trade.order_id)
            if identity in identities or fill_order is None:
                raise RuntimeError("duplicate or unknown settlement fill")
            if (trade.symbol, trade.exchange, trade.side, trade.offset) != (
                fill_order.request.symbol,
                fill_order.request.exchange,
                fill_order.request.side,
                fill_order.request.offset,
            ):
                raise RuntimeError("settlement order/fill binding mismatch")
            identities.add(identity)
            traded[trade.order_id] += trade.volume
        if any(traded[key] != order.traded for key, order in orders.items()):
            raise RuntimeError("settlement order remaining quantity is unproven")
        if not isclose(
            sum(t.commission for t in facts.trades), facts.fees, rel_tol=1e-12, abs_tol=1e-8
        ):
            raise RuntimeError("settlement fees and fill evidence disagree")
        if check_local:
            local_ids = {
                i for i in engine.state.recent_trade_ids if i.startswith(facts.source_day + ":")
            }
            if local_ids != identities:
                raise RuntimeError("final settlement fills have not all been consumed")
            local = engine.state_store.positions_from_state(engine.state)
            if not compare_positions(local, list(facts.positions)).matched:
                raise RuntimeError("final settlement source positions do not reconcile")
        book = PositionBook(list(facts.positions))
        book.roll_trading_day()
        if not compare_positions(book.all(), engine.broker.get_positions()).matched:
            raise RuntimeError("final settlement target positions do not reconcile")
        if engine.broker.get_active_orders():
            raise RuntimeError("day-end has active Broker orders")
        return account

    def _check_risk_before_roll(self, engine, facts, marker):
        engine.risk_manager.set_day_start_equity(marker["day_start_equity"], marker["source_day"])
        engine.risk_manager.restore_high_watermark(marker["high_watermark"])
        for account in (
            replace(
                engine.broker.get_account(),
                equity=facts.end_equity,
                trading_day=marker["source_day"],
            ),
            replace(engine.broker.get_account(), trading_day=marker["source_day"]),
        ):
            decision = engine.risk_manager.check_account(account)
            if not decision.allowed:
                engine.emergency_stop(decision.reason)
                raise RuntimeError("day-end blocked by risk: " + decision.reason)

    def poll(self, engine):
        """Called by the resident loop after events; never waits on provider I/O."""
        stage = engine.state.strategy_states.get(STAGE_KEY)
        if stage is not None:
            self._require_stage(engine)
            last = stage["last_settlement"]
            if last and self.provider is not None:
                self.provider.require_reference(last["source_reference"], last["source_digest"])
        marker = engine.state.strategy_states.get(DAY_END_KEY)
        if marker is None:
            return
        if not normal_day_end_pause(engine.state):
            raise RuntimeError("manual or hard-risk stop blocks normal continuation")
        if self.provider is None:
            raise RuntimeError("authenticated final settlement provider is not configured")
        days = (marker["source_day"], marker["target_day"])
        if self._query is None or self._query_days != days:
            self._query_days = days
            self._query = self._worker.submit(self._collect, engine, days)
            return
        if not self._query.done():
            return
        facts = self._query.result()
        if facts is None:
            self._query = None
            self.last_error = "final settlement is not complete yet"
            return
        self._commit_and_ready(engine, facts, marker)

    def _collect(self, engine, days):
        facts = self.provider.read_final_settlement(*days)
        # Existing catalog/OI maintenance performs any blocking Broker query in
        # this worker, never in the raw tick callback or strategy send path.
        engine.directional_manager._maintain_market_subscriptions_once()
        engine.directional_manager.oi_evidence.arm_verified_session_rollover(*days)
        return facts

    def _commit_and_ready(self, engine, facts, marker):
        from .stress90_lifecycle_transaction import (
            apply_stress90_lifecycle_transaction,
            build_stress90_settlement_roll_forward_targets,
        )

        existing = self.transactions.load()
        already_rolled = engine.state.trading_day == marker["target_day"]
        account = self._validate_facts(engine, facts, marker, check_local=not already_rolled)
        self._check_risk_before_roll(engine, facts, marker)
        if marker["settlement_digest"] and marker["settlement_digest"] != facts.digest:
            raise RuntimeError("settlement correction invalidated prepared day-end")
        from .stress90_operator_continuity import (
            load_stress90_operator_account_day_continuity_evidence,
        )

        def require_market():
            market = load_stress90_operator_account_day_continuity_evidence(
                engine.directional_manager._ohlc_cache,
                engine.directional_manager.oi_evidence_store,
                completed_account_day=marker["source_day"],
                current_ctp_trading_day=marker["target_day"],
            )
            # Target-day raw packets may advance the store sequence while the
            # exact completed source market inputs must remain unchanged.
            return evidence_digest(
                {
                    "facts": facts.digest,
                    "market": {
                        key: getattr(market, key)
                        for key in (
                            "completed_account_day",
                            "current_ctp_trading_day",
                            "ohlc_content_digest",
                            "completed_oi_evidence_digest",
                            "observed_transition_digest",
                        )
                    },
                }
            )

        continuity_digest = require_market()
        if (
            existing is not None
            and existing.status == "prepared"
            and existing.account_day_continuity_digest != continuity_digest
        ):
            raise RuntimeError("prepared day-end market evidence changed")
        if not already_rolled and not (existing is not None and existing.status == "prepared"):
            marker = {**marker, "settlement_digest": facts.digest}
            current = engine.state_store.load_required_record()
            paused = replace(
                current.state,
                strategy_states={**current.state.strategy_states, DAY_END_KEY: marker},
            )
            source = engine.state_store.save(
                paused, expected_sequence=current.sequence, expected_checksum=current.checksum
            )
            engine.state = source.state
            policy = engine.directional_manager.policy_state_store.load_required_record()
            targets = build_stress90_settlement_roll_forward_targets(
                source.state, policy.state, account
            )
            with engine.broker.lifecycle_state_commit_fence():
                self._validate_facts(engine, facts, marker)
                existing = self.transactions.begin(
                    operation="settlement_roll_forward",
                    generic_source=source,
                    policy_source=policy,
                    generic_target=targets.generic_target,
                    policy_target=targets.policy_target,
                    trading_day=marker["target_day"],
                    account_identity_digest=facts.account_identity_digest,
                    account_snapshot=account,
                    operation_nonce=facts.digest,
                    operator_reason="normal day-end technical pause",
                    account_day_continuity_digest=continuity_digest,
                )
        if existing is None or existing.operation != "settlement_roll_forward":
            raise RuntimeError("normal day-end lifecycle transaction is missing")
        if existing.status == "prepared":

            def precommit():
                self.provider.require_current(facts)
                self._require_stage(engine)
                if require_market() != continuity_digest:
                    raise RuntimeError("prepared day-end market evidence changed")

            try:
                with engine.broker.lifecycle_state_commit_fence():
                    precommit()
                    apply_stress90_lifecycle_transaction(
                        self.transactions,
                        generic_store=engine.state_store,
                        policy_store=engine.directional_manager.policy_state_store,
                        precommit_check=precommit,
                    )
            finally:
                # Even an interrupted generic write must never be overwritten
                # by stale source-day memory during an error or orderly stop.
                engine.state = engine.state_store.load_required_record().state
                engine._recent_trade_id_set = set(engine.state.recent_trade_ids)
                engine._sync_strategy_positions(
                    engine.state_store.positions_from_state(engine.state)
                )
        engine.state = engine.state_store.load_required_record().state
        engine._recent_trade_id_set = set(engine.state.recent_trade_ids)
        engine._sync_strategy_positions(engine.state_store.positions_from_state(engine.state))
        engine.risk_manager.set_day_start_equity(
            engine.state.day_start_equity, engine.state.trading_day
        )
        engine.risk_manager.restore_high_watermark(engine.state.equity_high_watermark)
        self._finish_readiness(engine, facts)

    def _finish_readiness(self, engine, facts):
        from .directional_stress90_runtime import Stress90DataUnavailableError
        from .stress90_session_authority import establish_stress90_session_ownership

        manager = engine.directional_manager
        if not manager._initialized:
            manager.bootstrap(engine._reference_now())
        snapshot = manager.activity_tracker.completed_snapshot
        if snapshot is None or snapshot.trading_day != facts.source_day:
            self.last_error = "completed directional activity is not ready"
            return
        try:
            manager._prepare_decision_for_current_day(
                facts.target_day, required_activity_day=facts.source_day
            )
        except Stress90DataUnavailableError as exc:
            self.last_error = str(exc)
            return
        proof = establish_stress90_session_ownership(
            engine.broker,
            runtime_dir=self.runtime_dir,
            timeout_seconds=engine.metadata_timeout_seconds,
            install_startup_capability=True,
        )
        engine.technical_activation_authority.startup_session_authority.last_proof = proof
        if engine.require_live_metadata:
            decision = engine._validate_live_metadata()
            if not decision.allowed:
                raise RuntimeError(decision.reason)
        decision = engine.risk_manager.check_account(engine.broker.get_account())
        if not decision.allowed:
            engine.emergency_stop(decision.reason)
            raise RuntimeError(decision.reason)
        from .account_runtime_registry import AccountRuntimeRegistry
        from .trading_day_evidence import TradingDayEvidenceStore

        day_evidence = TradingDayEvidenceStore(self.runtime_dir / "ctp_trading_day_evidence.json")
        if self.require_day_evidence or day_evidence.path.exists():
            day_evidence.save_observation(
                trading_day=facts.target_day,
                account_identity_digest=facts.account_identity_digest,
                runtime_dir=self.runtime_dir,
                policy_state=manager.policy_state_store.load_required(),
                registry=AccountRuntimeRegistry(self.account_registry_path),
            )
        with engine.broker.lifecycle_state_commit_fence():
            self.provider.require_current(facts)
            self._require_stage(engine)
            current = engine.state_store.load_required_record()
            if current.state != engine.state or not normal_day_end_pause(current.state):
                raise RuntimeError("runtime changed before day-end technical readiness")
            stage = {
                **current.state.strategy_states[STAGE_KEY],
                "last_settlement": {
                    "source_reference": facts.source_reference,
                    "source_digest": facts.source_digest,
                    "source_day": facts.source_day,
                    "target_day": facts.target_day,
                    "version": facts.version,
                    "facts_digest": facts.digest,
                },
            }
            markers = {**current.state.strategy_states, STAGE_KEY: stage}
            del markers[DAY_END_KEY]
            ready = replace(current.state, strategy_states=markers, metadata_verified=True)
            engine.state = engine.state_store.save(
                ready,
                expected_sequence=current.sequence,
                expected_checksum=current.checksum,
            ).state
        engine._metadata_verified_session = True
        engine._metadata_trading_day = facts.target_day
        engine._directional_initialized = True
        self._query = None
        self.last_error = ""
        engine._record("stress90_day_end_ready", stage["last_settlement"])
