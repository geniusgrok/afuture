"""Canonical zero-order pre-session preparation for one local Stress-90 runtime."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from typing import Any

from .durable_json import DurableJsonError, atomic_replace_regular, canonical_json_bytes

SESSION_PREFLIGHT_KIND = "afuture.session-preflight"
SESSION_PREFLIGHT_SCHEMA_VERSION = 1
SESSION_PREFLIGHT_BLOCKED_EXIT_CODE = 2
SESSION_PREFLIGHT_CALL_ERROR_EXIT_CODE = 3


class PreflightBlocked(RuntimeError):
    def __init__(self, check_name: str, detail: str, next_action: str) -> None:
        super().__init__(detail)
        self.check_name = check_name
        self.detail = detail
        self.next_action = next_action


class PreflightCallError(RuntimeError):
    """Invalid invocation or local call failure distinct from a safety block."""


class ReadOnlyBroker:
    """Capability fence: all Broker reads delegate, order writes are structurally denied."""

    def __init__(self, source: object) -> None:
        self.source = source

    def send_order(self, _request: object) -> str:
        raise RuntimeError("read-only Broker cannot send orders")

    def cancel_order(self, _order_id: str) -> None:
        raise RuntimeError("read-only Broker cannot cancel orders")

    def __getattr__(self, name: str) -> Any:
        return getattr(self.source, name)


def _jsonable(value: object) -> object:
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _write_payload(path: Path, payload: Mapping[str, object]) -> None:
    try:
        atomic_replace_regular(
            path,
            canonical_json_bytes(payload) + b"\n",
            label="session preflight output",
        )
    except DurableJsonError as exc:
        raise PreflightCallError(str(exc)) from exc


def _check_from_report(report: Any, name: str) -> dict[str, object] | None:
    checks = getattr(report, "checks", ())
    for check in checks:
        if str(getattr(check, "name", "")) == name:
            return {
                "name": name,
                "passed": bool(getattr(check, "passed", False)),
                "detail": str(getattr(check, "detail", "")),
            }
    return None


class SessionPreflightRunner:
    """Execute the fixed preparation sequence once, never entering a resident loop."""

    def __init__(
        self,
        *,
        config: Any,
        config_path: str | Path,
        output_path: str | Path,
        confirm_live: bool,
        refresh_ohlc: bool,
        shadow_account: bool,
        backend: Any,
    ) -> None:
        self.config = config
        self.config_path = Path(config_path)
        self.output_path = Path(output_path)
        self.confirm_live = bool(confirm_live)
        self.refresh_ohlc = bool(refresh_ohlc)
        self.shadow_account = bool(shadow_account)
        self.backend = backend

    def _base_payload(self) -> dict[str, object]:
        directional = getattr(self.config, "directional", None)
        return {
            "kind": SESSION_PREFLIGHT_KIND,
            "schema_version": SESSION_PREFLIGHT_SCHEMA_VERSION,
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "passed": False,
            "mode": "shadow" if self.shadow_account else "live",
            "policy": str(getattr(directional, "policy", "")),
            "account_continuity_mode": str(
                getattr(directional, "account_continuity_mode", "strict")
            ),
            "orders_sent": 0,
            "cancels_sent": 0,
            "entered_running": False,
            "allowed_next_actions": [],
        }

    def _blocked_payload(
        self,
        payload: dict[str, object],
        *,
        check_name: str,
        detail: str,
        actions: list[str],
    ) -> tuple[int, dict[str, object]]:
        payload.update(
            passed=False,
            failed_check=check_name,
            failure_detail=detail,
            allowed_next_actions=actions,
            orders_sent=0,
            cancels_sent=0,
            entered_running=False,
        )
        _write_payload(self.output_path, payload)
        return SESSION_PREFLIGHT_BLOCKED_EXIT_CODE, payload

    def run(self) -> tuple[int, dict[str, object]]:
        payload = self._base_payload()
        broker: object | None = None
        try:
            payload["config_validation"] = _jsonable(self.backend.validate_config())
            deployment = self.backend.verify_deployment()
            payload["deployment"] = _jsonable(deployment)
            deployment_dict = _jsonable(deployment)
            if isinstance(deployment_dict, Mapping):
                expected = deployment_dict.get("expected")
                if isinstance(expected, Mapping):
                    payload["deployment_identity"] = {
                        "deployment_role": expected.get("deployment_role"),
                        "source_commit": expected.get("source_commit"),
                        "production_source_tree_digest": expected.get(
                            "production_source_tree_digest"
                        ),
                    }
                    payload["bundle_seed_policy_identity"] = {
                        "bundle_digest": expected.get("bundle_digest"),
                        "bundle_archive_sha256": expected.get("bundle_archive_sha256"),
                        "seed_digest": expected.get("seed_digest"),
                        "policy_definition_digest": expected.get("policy_definition_digest"),
                        "products_manifest_digest": expected.get("products_manifest_digest"),
                    }
            local_status = self.backend.local_status()
            payload["local_status"] = _jsonable(local_status)
            payload["runtime_integrity"] = _jsonable(self.backend.runtime_integrity())
            broker = self.backend.open_broker()
            if not isinstance(broker, ReadOnlyBroker):
                broker = ReadOnlyBroker(broker)
            trading_day = str(self.backend.trading_day(broker))
            payload["ctp_trading_day"] = trading_day
            account_freshness = self.backend.fresh_account(broker, trading_day)
            payload["account_freshness"] = _jsonable(account_freshness)
            positions = self.backend.positions(broker)
            payload["position_freshness"] = {
                "fresh": True,
                "position_count": len(positions),
            }
            active_orders = self.backend.active_orders(broker)
            payload["order_freshness"] = {
                "fresh": True,
                "active_order_count": len(active_orders),
            }
            catalog = self.backend.catalog(broker)
            payload["catalog"] = {"fresh": True, "contract_count": len(catalog)}
            market = self.backend.market_facts(
                broker,
                trading_day,
                positions,
                catalog,
            )
            payload["market_facts"] = _jsonable(market)
            if self.refresh_ohlc:
                payload["ohlc_refresh"] = _jsonable(self.backend.refresh_ohlc(trading_day))
            alignment = self.backend.alignment(trading_day)
            payload["alignment"] = _jsonable(alignment)
            continuity = self.backend.continuity(trading_day)
            payload["continuity"] = _jsonable(continuity)
            doctor = self.backend.doctor_p0(
                broker,
                trading_day,
                positions,
                catalog,
                market,
            )
            payload["doctor_p0"] = _jsonable(doctor)
            capacity = self.backend.capacity(doctor, trading_day)
            capacity_json = _jsonable(capacity)
            payload["capacity"] = capacity_json
            if isinstance(capacity_json, Mapping):
                payload["raw_lots"] = capacity_json.get("raw_lots", {})
                payload["scaled_lots"] = capacity_json.get("scaled_lots", {})
                payload["final_lots"] = capacity_json.get("final_lots", {})
                payload["risk_overlay"] = {
                    "digest": capacity_json.get("risk_overlay_digest"),
                    "configured_live_risk_scale": capacity_json.get(
                        "configured_live_risk_scale"
                    ),
                    **(
                        dict(capacity_json.get("risk_overlay", {}))
                        if isinstance(capacity_json.get("risk_overlay"), Mapping)
                        else {}
                    ),
                }
                raw_warnings = capacity_json.get("warnings")
                warnings = list(raw_warnings) if isinstance(raw_warnings, list) else []
                representation = capacity_json.get("portfolio_representation_warning")
                if isinstance(representation, str) and representation:
                    warnings.append(representation)
                payload["capacity_warnings"] = warnings
            permit = self.backend.permit_status()
            payload["permit"] = _jsonable(permit)
        except PreflightBlocked as exc:
            return self._blocked_payload(
                payload,
                check_name=exc.check_name,
                detail=exc.detail,
                actions=[exc.next_action],
            )
        finally:
            if broker is not None:
                self.backend.close_broker(broker)

        continuity_json = payload.get("continuity")
        if isinstance(continuity_json, Mapping):
            if continuity_json.get("rebase_required") is True:
                return self._blocked_payload(
                    payload,
                    check_name="account_rebase_required",
                    detail="fresh account evidence requires explicit account rebase",
                    actions=[
                        f"afuture stress90-account-rebase --config {self.config_path} "
                        "--confirm-live --confirm-rebase --operator-reason <reason> "
                        "--operation-id <64-hex>"
                    ],
                )
            if continuity_json.get("roll_forward_required") is True:
                return self._blocked_payload(
                    payload,
                    check_name="operator_continuity_required",
                    detail="operator-managed continuity requires explicit roll-forward",
                    actions=[
                        f"afuture stress90-operator-roll-forward --config {self.config_path} "
                        "--confirm-live --confirm-operator-continuity "
                        "--operator-reason <reason> --operation-id <64-hex>"
                    ],
                )

        alignment_json = payload.get("alignment")
        if isinstance(alignment_json, Mapping) and alignment_json.get("passed") is False:
            action = str(
                alignment_json.get(
                    "next_action",
                    f"afuture doctor --config {self.config_path} --confirm-live",
                )
            )
            return self._blocked_payload(
                payload,
                check_name="market_alignment",
                detail=str(alignment_json.get("detail", "market evidence is not aligned")),
                actions=[action],
            )

        doctor_json = payload.get("doctor_p0")
        if isinstance(doctor_json, Mapping) and doctor_json.get("passed") is False:
            return self._blocked_payload(
                payload,
                check_name="doctor_p0",
                detail="one or more internal P0 checks failed",
                actions=[f"afuture doctor --config {self.config_path} --confirm-live"],
            )
        capacity_json = payload.get("capacity")
        if isinstance(capacity_json, Mapping) and capacity_json.get("hard_safety_passed") is False:
            return self._blocked_payload(
                payload,
                check_name="capacity_hard_safety",
                detail="Stress-90 capacity hard safety checks failed",
                actions=[
                    f"afuture stress90-capacity-report --config {self.config_path} --confirm-live"
                ],
            )

        payload.update(
            passed=True,
            failed_check="",
            failure_detail="",
            allowed_next_actions=[
                f"afuture doctor --config {self.config_path} --confirm-live",
                f"afuture stress90-capacity-report --config {self.config_path} --confirm-live",
                "issue a fresh technical activation permit only after manual review",
                f"afuture live --config {self.config_path} --confirm-live",
            ],
            orders_sent=0,
            cancels_sent=0,
            entered_running=False,
        )
        _write_payload(self.output_path, payload)
        return 0, payload


class ProductionPreflightBackend:
    """Read-only CTP-backed implementation that reuses existing status/Doctor/capacity logic."""

    def __init__(
        self,
        *,
        config: Any,
        config_path: str | Path,
        confirm_live: bool,
        shadow_account: bool,
        startup_timeout: float = 60.0,
        snapshot_wait: float = 12.0,
    ) -> None:
        self.base_config = config
        self.config_path = Path(config_path)
        self.confirm_live = bool(confirm_live)
        self.shadow_account = bool(shadow_account)
        self.startup_timeout = float(startup_timeout)
        self.snapshot_wait = float(snapshot_wait)
        self.config = config
        self.live_broker: Any | None = None
        self.broker: Any | None = None
        self.local_report: Any | None = None
        self.account: Any | None = None
        self.positions_snapshot: list[Any] = []
        self.active_orders_snapshot: list[Any] = []
        self.catalog_snapshot: list[Any] = []
        self.metadata: dict[str, Any] = {}
        self.quotes: dict[str, Any] = {}
        self.requested_symbols: list[str] = []
        self.doctor_report: Any | None = None

    @property
    def runtime_dir(self) -> Path:
        return Path(str(self.config.state_path)).resolve(strict=False).parent

    def validate_config(self) -> dict[str, object]:
        from . import cli

        cli._validate_stress90_lifecycle_config(self.base_config)
        cli._require_production_confirmation(
            self.base_config,
            SimpleNamespace(confirm_live=self.confirm_live),
        )
        if self.shadow_account:
            paths = cli._shadow_runtime_paths(self.base_config)
            self.config = cli._shadow_operational_config(self.base_config, paths)
        return {
            "valid": True,
            "mode": str(getattr(self.base_config, "mode", "")),
            "policy": str(getattr(self.base_config.directional, "policy", "")),
            "shadow_account": self.shadow_account,
        }

    def verify_deployment(self):
        from .deployment_identity import require_matching_deployment

        registry = Path(str(self.base_config.account_registry_path))
        return require_matching_deployment(
            config=self.base_config,
            config_path=self.config_path,
            runtime_dir=self.runtime_dir,
            account_registry_path=registry,
            expected_role="shadow" if self.shadow_account else "live",
        )

    def local_status(self):
        from .operations import build_local_status

        self.local_report = build_local_status(self.config)
        return self.local_report

    def runtime_integrity(self) -> dict[str, object]:
        if self.local_report is None:
            raise PreflightCallError("local status was not collected")
        hard_names = {
            "state_integrity",
            "runtime_paths_writable",
            "disk_space",
            "directional_ohlc_cache_integrity",
            "stress90_seed_integrity",
            "stress90_policy_state_integrity",
            "stress90_seed_state_identity",
            "stress90_risk_overlay_identity",
            "stress90_oi_evidence_integrity",
            "stress90_execution_intent_integrity",
            "stress90_ctp_order_journal_integrity",
            "stress90_lifecycle_transaction",
            "stress90_account_runtime_registry",
        }
        failed = [
            str(getattr(check, "name", ""))
            for check in getattr(self.local_report, "checks", ())
            if str(getattr(check, "name", "")) in hard_names
            and not bool(getattr(check, "passed", False))
        ]
        if failed:
            raise PreflightBlocked(
                "runtime_integrity",
                "local runtime/artifact integrity failed: " + ",".join(sorted(failed)),
                f"afuture status --config {self.config_path}",
            )
        return {"passed": True, "checked": sorted(hard_names)}

    def open_broker(self) -> ReadOnlyBroker:
        from . import cli
        from .broker.ctp import CtpBroker
        from .state import StateStore

        ctp = getattr(self.base_config, "ctp", None)
        if ctp is None:
            raise PreflightCallError("prepare-session requires CTP configuration")
        live = CtpBroker(ctp)
        self.live_broker = live
        broker: Any = live
        if self.shadow_account:
            paths = cli._shadow_runtime_paths(self.base_config)
            shadow_state = paths.get("broker_state")
            if not isinstance(shadow_state, Path):
                raise PreflightCallError("Shadow Broker state path is invalid")
            broker = cli._build_persistent_shadow_broker(
                self.base_config,
                live,
                shadow_state,
            )
        state: Any | None
        try:
            state = StateStore(str(self.config.state_path)).load()
        except (OSError, ValueError):
            state = None
        cli._configure_stress90_lifecycle_order_journal(broker, self.runtime_dir)
        cli._seed_state_aware_ctp_broker(
            live,
            None if self.shadow_account else state,
            reject_ambiguous=False,
        )
        self.broker = broker
        return ReadOnlyBroker(broker)

    def trading_day(self, broker: ReadOnlyBroker) -> str:
        from . import cli

        broker.start()
        cli._wait_until_ready(broker, self.startup_timeout)
        day = str(broker.get_trading_day())
        if len(day) != 8 or not day.isdigit():
            raise PreflightBlocked(
                "ctp_trading_day",
                "authoritative CTP trading day is invalid",
                f"afuture doctor --config {self.config_path} --confirm-live",
            )
        return day

    def fresh_account(self, broker: ReadOnlyBroker, trading_day: str) -> dict[str, object]:
        from . import cli

        cli.wait_for_fresh_snapshot(broker, self.snapshot_wait)
        account = broker.get_account()
        account.validate()
        if str(account.trading_day) != trading_day:
            raise PreflightBlocked(
                "account_freshness",
                "fresh account trading day differs from CTP trading day",
                f"afuture doctor --config {self.config_path} --confirm-live",
            )
        self.account = account
        source = self.live_broker
        last = float(getattr(source, "_last_account_monotonic", 0.0) or 0.0)
        age = max(0.0, monotonic() - last) if last > 0 else None
        return {"fresh": True, "trading_day": trading_day, "age_seconds": age}

    def positions(self, broker: ReadOnlyBroker) -> list[Any]:
        self.positions_snapshot = list(broker.get_positions())
        return list(self.positions_snapshot)

    def active_orders(self, broker: ReadOnlyBroker) -> list[Any]:
        self.active_orders_snapshot = list(broker.get_active_orders())
        return list(self.active_orders_snapshot)

    def catalog(self, broker: ReadOnlyBroker) -> list[Any]:
        rows = list(broker.get_contract_catalog())
        if not rows:
            raise PreflightBlocked(
                "contract_catalog",
                "CTP contract catalog is empty",
                f"afuture doctor --config {self.config_path} --confirm-live",
            )
        self.catalog_snapshot = rows
        return list(rows)

    def market_facts(
        self,
        broker: ReadOnlyBroker,
        trading_day: str,
        positions: list[Any],
        catalog: list[Any],
    ) -> dict[str, object]:
        from . import cli
        from .directional_activity import (
            DirectionalActivityStore,
            select_contracts_from_activity,
            validate_directional_activity_snapshot,
        )

        snapshot = DirectionalActivityStore(
            self.runtime_dir / "directional_activity.json"
        ).load()
        if snapshot is None:
            raise PreflightBlocked(
                "directional_activity",
                "completed directional activity is missing",
                f"afuture stress90-oi-collect --config {self.config_path} --confirm-live --once",
            )
        validate_directional_activity_snapshot(snapshot)
        catalog_by_symbol = {str(item.symbol): item for item in catalog}
        preferred = {
            str(catalog_by_symbol[position.symbol].product).upper(): str(position.symbol)
            for position in positions
            if not bool(getattr(position, "empty", False)) and position.symbol in catalog_by_symbol
        }
        selected = select_contracts_from_activity(
            self.config.directional,
            catalog,
            snapshot,
            datetime.strptime(trading_day, "%Y%m%d").date(),
            preferred_symbols=preferred,
        )
        symbols = {str(item.symbol) for item in selected.values()}
        symbols.update(
            str(position.symbol)
            for position in positions
            if not bool(getattr(position, "empty", False))
        )
        self.requested_symbols = sorted(symbols)
        selected_catalog = {
            symbol: catalog_by_symbol[symbol]
            for symbol in self.requested_symbols
            if symbol in catalog_by_symbol
        }
        self.quotes = dict(
            cli._collect_doctor_quotes(
                broker,
                selected_catalog,
                trading_day=trading_day,
                timeout_seconds=self.snapshot_wait,
            )
        )
        self.metadata = dict(
            broker.get_live_contract_specs(
                self.requested_symbols,
                float(getattr(self.config, "metadata_timeout_seconds", 10.0)),
            )
        )
        quote_ages: list[float] = []
        now = datetime.now(timezone.utc)
        for tick in self.quotes.values():
            timestamp = getattr(tick, "timestamp", None)
            if isinstance(timestamp, datetime) and timestamp.tzinfo is not None:
                quote_ages.append(
                    max(
                        0.0,
                        (now - timestamp.astimezone(timezone.utc)).total_seconds(),
                    )
                )
        return {
            "metadata_fresh": set(self.metadata) == set(self.requested_symbols),
            "quotes_fresh": set(self.quotes) == set(self.requested_symbols),
            "margin_fresh": all(
                hasattr(spec, "margin_rate_long") and hasattr(spec, "margin_rate_short")
                for spec in self.metadata.values()
            ),
            "commission_fresh": all(hasattr(spec, "fee") for spec in self.metadata.values()),
            "requested_symbols": list(self.requested_symbols),
            "max_required_quote_age_seconds": max(quote_ages, default=0.0),
        }

    def refresh_ohlc(self, trading_day: str) -> dict[str, object]:
        from .directional_ohlc_cache import DirectionalOHLCCacheStore
        from .directional_ohlc_refresh import refresh_directional_ohlc_cache
        from .execution_aligned_runtime import SinaContinuousOHLCProvider

        entry = refresh_directional_ohlc_cache(
            DirectionalOHLCCacheStore(self.runtime_dir / "directional_ohlc_cache.json"),
            provider_factory=SinaContinuousOHLCProvider,
            products=tuple(self.config.directional.products),
            current_ctp_trading_day=trading_day,
            authoritative_ctp_trading_day=trading_day,
        )
        return {
            "refreshed": True,
            "latest_date": entry.latest_date.isoformat(),
            "content_digest": entry.content_digest,
        }

    def alignment(self, trading_day: str) -> dict[str, object]:
        try:
            from .directional_activity import (
                DirectionalActivityStore,
                validate_directional_activity_snapshot,
            )
            from .directional_ohlc_cache import DirectionalOHLCCacheStore
            from .directional_ohlc_refresh import load_stress90_completed_ohlc
            from .directional_stress90_oi_runtime import Stress90OiEvidenceStore
            from .directional_stress90_state import Stress90PolicyStateStore

            activity = DirectionalActivityStore(
                self.runtime_dir / "directional_activity.json"
            ).load()
            if activity is None:
                raise RuntimeError("directional activity is missing")
            validate_directional_activity_snapshot(activity)
            policy = Stress90PolicyStateStore(
                self.runtime_dir / "stress90_policy_state.json"
            ).load_required()
            ohlc = load_stress90_completed_ohlc(
                DirectionalOHLCCacheStore(self.runtime_dir / "directional_ohlc_cache.json"),
                products=tuple(self.config.directional.products),
                current_ctp_trading_day=trading_day,
                authoritative_ctp_trading_day=trading_day,
                required_completed_day=str(policy.last_completed_target_day),
            )
            oi = Stress90OiEvidenceStore(
                self.runtime_dir / "stress90_oi_evidence.json"
            ).load_required_record()
            latest_oi = oi.state.completed[-1].trading_day if oi.state.completed else ""
            if str(policy.last_completed_target_day) > trading_day:
                raise RuntimeError("policy target day is ahead of authoritative CTP day")
            return {
                "passed": True,
                "activity": "aligned",
                "activity_trading_day": str(activity.trading_day),
                "ohlc": "aligned",
                "ohlc_latest_date": ohlc.latest_date.isoformat(),
                "oi": "aligned",
                "oi_latest_completed_day": latest_oi,
                "target_day": "aligned",
                "policy_last_completed_target_day": str(policy.last_completed_target_day),
            }
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return {
                "passed": False,
                "detail": str(exc),
                "next_action": f"afuture doctor --config {self.config_path} --confirm-live",
            }

    def continuity(self, _trading_day: str) -> dict[str, object]:
        from .operations import _stress90_account_continuity_status
        from .state import StateStore

        try:
            state = StateStore(str(self.config.state_path)).load()
        except (OSError, ValueError):
            state = None
        facts, roll, rebase, needs_permit, passed, detail = _stress90_account_continuity_status(
            self.config,
            state,
        )
        return {
            "mode": str(getattr(self.config.directional, "account_continuity_mode", "strict")),
            "roll_forward_required": bool(roll),
            "rebase_required": bool(rebase),
            "new_permit_required": bool(needs_permit),
            "passed": bool(passed),
            "detail": detail,
            "evidence": facts,
        }

    def doctor_p0(
        self,
        broker: ReadOnlyBroker,
        trading_day: str,
        positions: list[Any],
        catalog: list[Any],
        _market: object,
    ) -> dict[str, object]:
        from . import cli
        from .operations import build_doctor_report
        from .stress90_activation_permit import STRESS90_DOCTOR_INTERNAL_P0_CHECKS

        session_valid = False
        session_detail = "complete CTP session ownership evidence is unavailable"
        try:
            mechanical = cli._require_lifecycle_mechanical_snapshot(
                broker,
                runtime_dir=self.runtime_dir,
                timeout_seconds=max(0.1, self.snapshot_wait),
            )
            proof = mechanical.session_proof
            session_valid = proof is not None
            session_detail = "complete request-bound CTP session ownership verified"
        except RuntimeError as exc:
            session_detail = str(exc)
        if self.account is None:
            raise PreflightCallError("fresh account was not collected")
        report = build_doctor_report(
            self.config,
            broker_ready=bool(broker.is_ready()),
            fresh_snapshot=True,
            trading_day=trading_day,
            account=self.account,
            positions=positions,
            active_order_count=len(self.active_orders_snapshot),
            catalog=catalog,
            requested_symbols=list(self.requested_symbols),
            metadata=self.metadata,
            quotes=self.quotes,
            session_trade_ownership_valid=session_valid,
            session_trade_ownership_detail=session_detail,
        )
        self.doctor_report = report
        p0 = [
            check
            for check in report.checks
            if str(check.name) in STRESS90_DOCTOR_INTERNAL_P0_CHECKS
        ]
        return {
            "passed": bool(p0) and all(check.passed for check in p0),
            "checks": [asdict(check) for check in p0],
            "reconcile": _check_from_report(report, "position_reconciliation"),
        }

    def capacity(self, _doctor: object, trading_day: str) -> dict[str, object]:
        from .stress90_capacity import build_stress90_capacity_payload

        if self.doctor_report is None or self.broker is None:
            raise PreflightCallError("Doctor report is unavailable for capacity projection")
        account_identity = str(self.broker.get_account_identity_digest())
        return build_stress90_capacity_payload(
            self.doctor_report,
            account_identity_digest=account_identity,
            ctp_trading_day=trading_day,
        )

    def permit_status(self) -> dict[str, object]:
        from .stress90_activation_permit import Stress90ActivationPermitStore

        path = self.runtime_dir / "stress90_activation_permit.json"
        try:
            record = Stress90ActivationPermitStore(path).load_record()
        except (OSError, RuntimeError) as exc:
            return {"status": "invalid", "issued": False, "detail": str(exc)}
        if record is None:
            return {"status": "missing", "issued": False}
        return {
            "status": record.permit.status,
            "issued": record.permit.status == "issued",
            "sequence": record.sequence,
            "checksum": record.checksum,
        }

    def close_broker(self, broker: ReadOnlyBroker) -> None:
        try:
            broker.stop()
        except Exception:
            pass
