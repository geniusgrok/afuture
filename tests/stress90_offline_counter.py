"""Isolated persistent counter: only SimBroker, never a real gateway or credentials."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from afuture.broker.ctp_session_query import build_ctp_session_activity_evidence
from afuture.broker.sim import SimBroker
from afuture.directional import DirectionalConfig
from afuture.directional_ohlc_cache import DirectionalOHLCCacheStore
from afuture.directional_policy_activation import (
    STRESS90_ACTIVATION_CONFIRMATION,
    activate_stress90_policy,
)
from afuture.directional_sessions import PRODUCT_SESSION_MANIFEST
from afuture.directional_stress90_policy import STRESS90_POLICY
from afuture.directional_stress90_state import Stress90PolicyStateStore
from afuture.models import (
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
    RuntimeMode,
    Tick,
    Trade,
)
from afuture.risk import RiskConfig
from afuture.runtime_calendar import RuntimeTradingCalendar
from afuture.runtime_factory import build_runtime_engine
from afuture.state import RuntimeState, StateStore
from afuture.stress90_day_end import Stress90FinalSettlement
from afuture.stress90_risk_overlay import stress90_risk_overlay_digest

CHINA = ZoneInfo("Asia/Shanghai")
ACCOUNT = "a" * 64
EPOCH = "b" * 64


def _encode(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _decode_order(raw):
    req = raw["request"]
    request = OrderRequest(
        **{
            **req,
            "side": OrderSide(req["side"]),
            "offset": Offset(req["offset"]),
            "order_type": OrderType(req["order_type"]),
        }
    )
    return Order(**{**raw, "request": request, "status": OrderStatus(raw["status"])})


def _decode_trade(raw):
    return Trade(
        **{
            **raw,
            "side": OrderSide(raw["side"]),
            "offset": Offset(raw["offset"]),
            "timestamp": datetime.fromisoformat(raw["timestamp"]),
        }
    )


class OfflineSettlementProvider:
    def __init__(self, broker):
        self.broker = broker
        self.root = broker.root / "counter_originals"
        self.root.mkdir(exist_ok=True)
        self.missing = False

    def read_final_settlement(self, source, target):
        path = self._current_path(source, target)
        if self.missing or not path.exists():
            return None
        payload = path.read_bytes()
        raw = json.loads(payload)
        return Stress90FinalSettlement(
            account_identity_digest=ACCOUNT,
            account_epoch=EPOCH,
            currency="CNY",
            source_day=source,
            target_day=target,
            version=raw["version"],
            source_reference=path.name,
            source_digest=sha256(payload).hexdigest(),
            query_generation=str(raw["version"]),
            start_equity=raw["start"]["equity"],
            end_equity=raw["end"]["equity"],
            realized_pnl=(raw["end"]["realized_pnl"] - raw["start"]["realized_pnl"] + raw["fees"]),
            unrealized_pnl_change=(raw["end"]["unrealized_pnl"] - raw["start"]["unrealized_pnl"]),
            fees=raw["fees"],
            deposit=raw["end"]["deposit"],
            withdrawal=raw["end"]["withdrawal"],
            orders=tuple(_decode_order(row) for row in raw["orders"]),
            trades=tuple(_decode_trade(row) for row in raw["trades"]),
            positions=tuple(ContractPosition(**row) for row in raw["positions"]),
        )

    def require_reference(self, reference, digest):
        path = self.root / reference
        raw = json.loads(path.read_bytes())
        if (
            sha256(path.read_bytes()).hexdigest() != digest
            or self._current_path(raw["source_day"], raw["target_day"]) != path
        ):
            raise RuntimeError("counter settlement source was corrected")

    def _current_path(self, source, target):
        pointer = self.root / f"current-{source}-{target}.json"
        reference = (
            json.loads(pointer.read_text())["reference"]
            if pointer.exists()
            else f"settlement-{source}-{target}.json"
        )
        return self.root / reference

    def publish_correction(self, source, target, change):
        original = json.loads(self._current_path(source, target).read_bytes())
        change(original)
        reference = f"correction-{source}-{target}.json"
        (self.root / reference).write_text(json.dumps(original, sort_keys=True))
        (self.root / f"current-{source}-{target}.json").write_text(
            json.dumps({"reference": reference})
        )

    def require_current(self, facts):
        self.require_reference(facts.source_reference, facts.source_digest)
        if (
            self.read_final_settlement(facts.source_day, facts.target_day) != facts
            or self.broker.get_trading_day() != facts.target_day
        ):
            raise RuntimeError("counter settlement query generation changed")

    def trade_trading_day(self, trade):
        for item in self.broker.get_trades():
            if item == trade:
                return self.broker.get_trading_day()
        for path in self.root.glob("settlement-*.json"):
            raw = json.loads(path.read_text())
            if any(_decode_trade(row) == trade for row in raw["trades"]):
                return raw["source_day"]
        raise RuntimeError("counter cannot bind unknown fill to its source day")

    def order_trading_day(self, order):
        current = self.broker.get_order(order.order_id)
        if (
            current is not None
            and current.request == order.request
            and order.traded <= current.traded
        ):
            return self.broker.get_trading_day()
        for path in self.root.glob("settlement-*.json"):
            raw = json.loads(path.read_text())
            if any(_decode_order(row) == order for row in raw["orders"]):
                return raw["source_day"]
        raise RuntimeError("counter cannot bind unknown/changed order to its source day")

    def oi_session_verifier(self, source, target, digest):
        # Rebuild source OI from the independent counter's original raw packets.
        # No internal validator patch and no user-supplied verified/final flag.
        from afuture.directional_stress90_oi_runtime import (
            Stress90OiEvidenceAggregator,
            VerifiedSessionContinuity,
        )

        path = self.root / f"ticks-{source}.jsonl"
        raw_bytes = path.read_bytes()
        observer = Stress90OiEvidenceAggregator()
        observer.set_expected_contracts(source, self.broker.get_contract_catalog())
        for line in raw_bytes.splitlines():
            row = json.loads(line)
            tick = Tick(**{**row, "timestamp": datetime.fromisoformat(row["timestamp"])})
            observer.observe_raw_tick(tick, self.broker._contract_catalog_by_symbol[tick.symbol])
        adjacent = (datetime.strptime(source, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
        observer.set_expected_contracts(adjacent, self.broker.get_contract_catalog())
        completed = observer.completed_evidence(source)
        if not completed.complete or completed.evidence_digest != digest:
            raise RuntimeError("counter raw OI archive mismatch")
        return VerifiedSessionContinuity(
            source,
            target,
            digest,
            "isolated-persistent-sim-counter",
            "1",
            sha256(raw_bytes).hexdigest(),
            ("CZCE", "DCE", "INE", "SHFE"),
            source,
            target,
            sha256((source + ":" + target).encode()).hexdigest(),
        )


class OfflineCounter(SimBroker):
    metadata_query_blocks = False

    def __init__(self, root, catalog, specs):
        self.root = root
        super().__init__(
            100_000.0,
            specs,
            conservative=True,
            latency_ticks=1,
            contract_catalog=catalog,
            state_path=root / "sim_broker.json",
            state_identity="isolated-counter-one-account",
        )
        self.stress90_settlement_provider = OfflineSettlementProvider(self)
        self._opening_path = self.stress90_settlement_provider.root / "current-open.json"
        self._session_capability = None
        self.order_journal_identity = None

    def get_account_identity_digest(self):
        return ACCOUNT

    def configure_order_submission_journal(self, path, **identity):
        # SIM submissions already have an authoritative durable ledger. The CTP
        # session verifier below is used only for truly empty target generations.
        self.order_journal_identity = identity

    def require_stress90_session_startup_capability(self):
        pass

    def refresh_session_activity(self, *, timeout_seconds):
        if self.get_orders() or self.get_session_trades():
            raise RuntimeError("nonempty SIM session requires its own settlement query")
        return build_ctp_session_activity_evidence(
            account_identity_digest=ACCOUNT,
            trading_day=self.get_trading_day(),
            order_request_id=1,
            trade_request_id=2,
            orders=(),
            trades=(),
            critical_generation=0,
        )

    def require_session_activity_evidence_current(self, evidence):
        if (
            evidence.trading_day != self.get_trading_day()
            or self.get_orders()
            or self.get_session_trades()
        ):
            raise RuntimeError("counter empty target query changed")

    def install_stress90_session_startup_capability(self, *, evidence, ownership_digest):
        self.require_session_activity_evidence_current(evidence)
        self._session_capability = evidence

    def recover_stress90_session_activity(self, evidence):
        self.require_session_activity_evidence_current(evidence)
        return ()

    def get_trading_day(self):
        return self.get_account().trading_day

    def synchronize_trading_day(self, day):
        source = self._trading_day
        if source and source != day:
            end = self.get_account()
            start = json.loads(self._opening_path.read_text())
            data = {
                "source_day": source,
                "target_day": day,
                "version": self._settlement_id + 1,
                "start": start,
                "end": asdict(end),
                "fees": sum(t.commission for t in self.get_trades()),
                "orders": [asdict(o) for o in self.get_orders()],
                "trades": [asdict(t) for t in self.get_trades()],
                "positions": [asdict(p) for p in self.get_positions()],
            }
            path = self.stress90_settlement_provider.root / f"settlement-{source}-{day}.json"
            if path.exists():
                raise RuntimeError("counter refuses to overwrite a final original")
            path.write_text(json.dumps(data, sort_keys=True, default=_encode))
            next_start = asdict(end)
        else:
            next_start = asdict(self.get_account())
        super().synchronize_trading_day(day)
        if source != day:
            self._opening_path.write_text(json.dumps(next_start, sort_keys=True))
            self._events.append(BrokerEvent("account", self.get_account()))

    def publish_tick(self, tick):
        path = self.stress90_settlement_provider.root / f"ticks-{tick.trading_day}.jsonl"
        with path.open("a") as stream:
            stream.write(json.dumps(asdict(tick), sort_keys=True, default=_encode) + "\n")
        super().publish_tick(tick)


class OfflineMonth:
    def __init__(self, root: Path, *, restore=False, valid_until="2026-10-10T00:00:00+08:00"):
        from test_directional_stress90_runtime import _write_completed_oi, _write_seed_state

        from afuture.account_runtime_registry import (
            ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,
            AccountRuntimeRegistry,
        )
        from afuture.directional_stress90_state import bind_stress90_account_identity

        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.now = datetime(2026, 8, 24, 21, tzinfo=CHINA)
        self.calendar = RuntimeTradingCalendar.load()
        self.catalog = [
            ContractInfo(f"{p}2612", PRODUCT_SESSION_MANIFEST[p].exchange, p, "2026-12-15")
            for p in STRESS90_POLICY.products
        ]
        self.specs = {
            c.symbol: ContractSpec(
                c.symbol,
                c.exchange,
                10.0,
                1.0,
                0.10,
                0.10,
                FeeSpec(open_fixed=2.0, close_fixed=2.0, close_today_fixed=2.0),
            )
            for c in self.catalog
        }
        self.market_prices = {c.symbol: 1000.0 for c in self.catalog}
        self.config = SimpleNamespace(
            mode="replay",
            pairs=[],
            contracts=self.specs,
            risk=RiskConfig(
                max_daily_loss_ratio=0.05,
                max_total_drawdown_ratio=0.30,
                min_available_ratio=0.25,
                margin_estimate_buffer=1.25,
                max_contract_volume=35,
                max_orders_per_minute=200,
            ),
            directional=DirectionalConfig(
                enabled=True,
                policy="stress90",
                products=STRESS90_POLICY.products,
                exchanges=("DCE", "CZCE", "SHFE", "INE"),
                signal_max_age_hours=120.0,
            ),
            account_registry_path=str(root / ".account-runtime-registry.json"),
            auto_flatten_imbalance=True,
            aggressive_ticks=1,
            slippage_ticks=1,
            legging_timeout_seconds=2.0,
            require_live_metadata=True,
            metadata_timeout_seconds=1.0,
        )
        self.broker = OfflineCounter(root, self.catalog, self.specs)
        if restore:
            clock = json.loads((root / "counter-clock.json").read_text())
            self.now = datetime.fromisoformat(clock["timestamp"])
            self.market_prices = clock["prices"]
        if not restore:
            _seed, policy_path = _write_seed_state(root)
            store = Stress90PolicyStateStore(policy_path)
            record = store.load_required_record()
            store.save(
                replace(
                    bind_stress90_account_identity(record.state, ACCOUNT, account_epoch=EPOCH),
                    live_inception_day="20260825",
                    live_inception_equity=100_000.0,
                ),
                expected_sequence=record.sequence,
            )
            registry = AccountRuntimeRegistry(self.config.account_registry_path)
            registry.initialize(
                strong_confirmation=ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION
            )
            registry.bind_new(ACCOUNT, root, EPOCH, "c" * 64)
            _write_completed_oi(root)
            from afuture.directional_stress90_oi_runtime import Stress90OiEvidenceStore

            # Bootstrap input contains completed prior-day evidence only. The
            # persistent counter supplies every packet of the first active day.
            oi_store = Stress90OiEvidenceStore(root / "stress90_oi_evidence.json")
            oi = oi_store.load_required_record()
            oi_store.save_state(replace(oi.state, in_progress=None), expected_sequence=oi.sequence)
            self.save_ohlc("20260824")
            from afuture.directional_activity import (
                ContractActivity,
                DirectionalActivitySnapshot,
                DirectionalActivityStore,
            )

            DirectionalActivityStore(root / "directional_activity.json").save(
                DirectionalActivitySnapshot(
                    "20260824",
                    {
                        c.symbol: ContractActivity(
                            c.symbol,
                            c.exchange,
                            c.product,
                            "20260824",
                            20_000.0,
                            30_000.0,
                            self.now.replace(hour=14, minute=59),
                        )
                        for c in self.catalog
                    },
                )
            )
            self.broker.synchronize_trading_day("20260825")
            seed = store.load_required().bootstrap_seed_digest
            state = activate_stress90_policy(
                RuntimeState(
                    kill_switch=True,
                    kill_reason="initial commissioning",
                    reconciled=True,
                    runtime_mode=RuntimeMode.HALTED.value,
                    trading_day="20260825",
                    day_start_equity=100_000.0,
                    equity_high_watermark=100_000.0,
                    last_account_equity=100_000.0,
                    last_account_trading_day="20260825",
                    last_account_cash_flow_verified=True,
                    last_account_settlement_id=0,
                ),
                broker_flat=True,
                local_flat=True,
                no_active_orders=True,
                reconciled=True,
                bootstrap_seed_digest=seed,
                account_identity_digest=ACCOUNT,
                risk_overlay_digest=stress90_risk_overlay_digest(
                    self.config.directional, self.config.risk
                ),
                operator_reason="isolated first commissioning",
                strong_confirmation=STRESS90_ACTIVATION_CONFIRMATION,
            )
            StateStore(root / "state.json").save(state)
        self.engine = build_runtime_engine(
            self.config,
            self.broker,
            StateStore(root / "state.json"),
            historical_mode=True,
            health_clock=lambda: self.now,
            continuation_until=valid_until,
            runtime_calendar=self.calendar,
        )
        from afuture.runtime_lease import AccountExclusiveRuntimeLease

        self.lease = AccountExclusiveRuntimeLease(root, ACCOUNT, role="isolated-month")
        self.lease.acquire()
        self.engine.bind_day_end_lease(self.lease)
        self.engine.start()
        if not restore:
            self.engine.verify_stress90_startup_session(self.lease, timeout_seconds=1.0)
            self.engine.directional_manager.bootstrap(self.now)
            self.emit("20260825", self.now, volume=1000, hold=30_000)
            self.engine.run_once()  # initial HALTED setup, never a daily state rewrite
            manager = self.engine.directional_manager
            manager._prepare_decision_for_current_day("20260825")
            from afuture.stress90_activation_permit import collect_stress90_activation_evidence

            authority = self.engine.technical_activation_authority
            evidence = collect_stress90_activation_evidence(
                runtime_dir=root,
                state_store=self.engine.state_store,
                account_identity_digest=ACCOUNT,
                account_snapshot=self.broker.get_account(),
                ctp_trading_day="20260825",
                broker_positions=[],
                active_orders=[],
                session_trades=[],
                owns_order=self.broker.owns_order,
                session_activity_proof=authority.startup_session_authority.last_proof,
                catalog=self.catalog,
                account_registry_path=self.config.account_registry_path,
            )
            authority.permit_store.issue(evidence)
            assert self.engine.activate_halted_runtime_from_permit(self.lease)
            self.engine.initialize_after_ready()
        else:
            self.engine.initialize_after_ready()

    def save_ohlc(self, completed):
        index = pd.bdate_range(start="20240102", end="20261010")
        opened = set(self.calendar.open_days["DCE"])
        index = index[
            [d.date() < self.calendar.coverage_start or d.date() in opened for d in index]
        ]
        step = np.arange(len(index))
        prices = pd.DataFrame(
            {
                p: 100 * np.exp(np.cumsum(0.0015 + 0.002 * np.sin(step * 0.17 + i * 0.6)))
                for i, p in enumerate(STRESS90_POLICY.products)
            },
            index=index,
        )
        prices = prices.loc[prices.index <= pd.Timestamp(completed)]
        DirectionalOHLCCacheStore(self.root / "directional_ohlc_cache.json").save(
            STRESS90_POLICY.products, prices * 0.999, prices
        )

    def emit(self, day, timestamp, *, volume, hold, shift=0.0):
        self.now = timestamp
        (self.root / "counter-clock.json").write_text(
            json.dumps({"timestamp": timestamp.isoformat(), "prices": self.market_prices})
        )
        with self.broker.market_batch(trading_day=day):
            for i, c in enumerate(self.catalog):
                price = self.market_prices[c.symbol] + shift + np.sin(i * 0.3) * 0.2
                tick = Tick(
                    c.symbol,
                    c.exchange,
                    timestamp,
                    price - 0.5,
                    price + 0.5,
                    price,
                    1000,
                    1000,
                    day,
                    volume=volume,
                    open_interest=hold,
                    open_price=1000.0,
                )
                self.broker.publish_tick(tick)

    def session_open(self, target):
        date = datetime.strptime(target, "%Y%m%d").replace(tzinfo=CHINA)
        previous = max(d for d in self.calendar.open_days["DCE"] if d < date.date())
        night = datetime.combine(previous, datetime.min.time(), CHINA).replace(hour=21)
        if self.calendar.expected_trading_day("B2612", night, "DCE") == target:
            return night
        return date.replace(hour=9)

    def close(self):
        try:
            self.engine.stop()
        finally:
            self.lease.release()
