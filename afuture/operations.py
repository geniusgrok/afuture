"""Read-only operational inspection and fail-closed live preflight checks."""

from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from math import isfinite
from pathlib import Path

from .directional_activity import DirectionalActivityStore
from .metadata import validate_contract_metadata
from .models import AccountSnapshot, ContractPosition, ContractSpec, RuntimeMode
from .reconcile import compare_positions
from .state import RuntimeState, StateIntegrityError, StateStore

MIN_OPERATIONAL_DISK_FREE_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True)
class OperationalCheck:
    """One named safety claim with enough detail for operator diagnosis."""

    name: str
    passed: bool
    detail: str


@dataclass
class OperationalReport:
    """JSON-compatible operational facts and checks."""

    checks: list[OperationalCheck] = field(default_factory=list)
    facts: dict[str, object] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.checks)

    def add(self, name: str, passed: bool, detail: str) -> None:
        self.checks.append(OperationalCheck(name, passed, detail))

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "checks": [asdict(item) for item in self.checks],
            "facts": self.facts,
            "warnings": list(self.warnings),
        }


def _state_facts(state: RuntimeState) -> dict[str, object]:
    return {
        "valid": True,
        "runtime_mode": state.runtime_mode,
        "kill_switch": state.kill_switch,
        "kill_reason": state.kill_reason,
        "reconciled": state.reconciled,
        "metadata_verified": state.metadata_verified,
        "trading_day": state.trading_day,
        "day_start_equity": state.day_start_equity,
        "equity_high_watermark": state.equity_high_watermark,
        "position_count": len(state.positions),
        "last_order_id": state.last_order_id,
        "last_trade_id": state.last_trade_id,
        "recent_trade_id_count": len(state.recent_trade_ids),
        "last_account_equity": state.last_account_equity,
        "last_account_trading_day": state.last_account_trading_day,
    }


def _existing_ancestor(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _path_facts(path: Path) -> dict[str, object]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "size_bytes": path.stat().st_size if path.is_file() else 0,
    }


def build_local_status(
    config,
    *,
    min_free_bytes: int = MIN_OPERATIONAL_DISK_FREE_BYTES,
) -> OperationalReport:
    """Inspect local state and evidence without creating files or directories."""
    report = OperationalReport()
    store = StateStore(config.state_path)
    state_present = store.path.exists()
    state: RuntimeState | None = None
    if state_present:
        try:
            state = store.load()
        except (OSError, StateIntegrityError) as exc:
            report.add("state_integrity", False, str(exc))
            report.facts["state"] = {
                "present": True,
                "valid": False,
                "error": str(exc),
            }
        else:
            report.add("state_integrity", True, "current state envelope verified")
            report.facts["state"] = {"present": True, **_state_facts(state)}
    else:
        report.add("state_integrity", True, "no current state file")
        report.facts["state"] = {"present": False, "valid": None}

    previous: dict[str, object] = {
        "path": str(store.previous_path),
        "present": store.previous_path.exists(),
        "valid": None,
    }
    if store.previous_path.exists():
        try:
            previous_state = store.load_previous()
        except (OSError, StateIntegrityError) as exc:
            previous["valid"] = False
            previous["error"] = str(exc)
            report.warnings.append(f"previous state evidence is invalid: {exc}")
        else:
            if previous_state is not None:
                previous.update(_state_facts(previous_state))
    report.facts["previous_state"] = previous

    paths = {
        "state": Path(config.state_path),
        "log": Path(config.log_path),
        "report": Path(config.report_path),
        "audit": Path(config.journal_path),
        "alert": Path(config.alert_path),
    }
    report.facts["paths"] = {name: _path_facts(path) for name, path in paths.items()}
    ancestors = {_existing_ancestor(path.parent) for path in paths.values()}
    unwritable = sorted(str(path) for path in ancestors if not os.access(path, os.W_OK))
    report.add(
        "runtime_paths_writable",
        not unwritable,
        "runtime path ancestors are writable"
        if not unwritable
        else "unwritable path ancestors: " + ", ".join(unwritable),
    )
    free_by_path = {str(path): shutil.disk_usage(path).free for path in sorted(ancestors)}
    minimum_free = min(free_by_path.values()) if free_by_path else 0
    report.facts["disk"] = {
        "minimum_free_bytes": minimum_free,
        "required_free_bytes": min_free_bytes,
        "free_bytes_by_path": free_by_path,
    }
    report.add(
        "disk_space",
        minimum_free >= min_free_bytes,
        f"minimum free bytes {minimum_free}; required {min_free_bytes}",
    )
    return report


def _live_specs_valid(specs: dict[str, ContractSpec], symbols: list[str]) -> tuple[bool, str]:
    missing = sorted(set(symbols).difference(specs))
    if missing:
        return False, "missing live metadata: " + ", ".join(missing)
    for symbol in symbols:
        spec = specs[symbol]
        values = (spec.multiplier, spec.price_tick, spec.margin_rate_long, spec.margin_rate_short)
        if spec.symbol != symbol or not spec.exchange:
            return False, f"invalid live contract identity: {symbol}"
        if any(not isfinite(value) or value <= 0 for value in values):
            return False, f"invalid positive live contract field: {symbol}"
        if any(
            not isfinite(getattr(spec.fee, name)) or getattr(spec.fee, name) < 0
            for name in spec.fee.__dataclass_fields__
        ):
            return False, f"invalid live contract fee: {symbol}"
    return True, f"verified live metadata for {len(symbols)} sampled contracts"


def build_doctor_report(
    config,
    *,
    broker_ready: bool,
    fresh_snapshot: bool,
    trading_day: str,
    account: AccountSnapshot,
    positions: list[ContractPosition],
    active_order_count: int,
    catalog_count: int,
    requested_symbols: list[str],
    metadata: dict[str, ContractSpec],
    min_free_bytes: int = MIN_OPERATIONAL_DISK_FREE_BYTES,
) -> OperationalReport:
    """Combine local evidence with an already-fresh CTP snapshot; never place orders."""
    report = build_local_status(config, min_free_bytes=min_free_bytes)
    report.add("broker_ready", broker_ready, "CTP session ready" if broker_ready else "not ready")
    report.add(
        "fresh_snapshot",
        fresh_snapshot,
        "fresh account and complete position snapshot received"
        if fresh_snapshot
        else "fresh snapshot not proven",
    )
    try:
        account.validate()
    except ValueError as exc:
        report.add("account_snapshot_valid", False, str(exc))
    else:
        report.add("account_snapshot_valid", True, "account values are finite and valid")
    report.add(
        "no_active_orders",
        active_order_count == 0,
        f"active order count: {active_order_count}",
    )
    report.add(
        "contract_catalog_available",
        catalog_count > 0,
        f"contract catalog count: {catalog_count}",
    )

    requested = sorted(set(requested_symbols))
    live_valid, live_detail = _live_specs_valid(metadata, requested)
    if not requested:
        live_valid, live_detail = False, "no contract metadata was sampled"
    report.add("live_metadata_complete", live_valid, live_detail)
    configured = {symbol: config.contracts[symbol] for symbol in requested if symbol in config.contracts}
    decision = validate_contract_metadata(configured, metadata) if configured else None
    report.add(
        "configured_metadata_conservative",
        decision is None or decision.allowed,
        "no sampled static contract requires comparison"
        if decision is None
        else decision.reason or "configured metadata does not understate live values",
    )

    store = StateStore(config.state_path)
    state: RuntimeState | None = None
    state_error = ""
    if store.path.exists():
        try:
            state = store.load()
        except (OSError, StateIntegrityError) as exc:
            state_error = str(exc)
    if state is None and store.path.exists():
        report.add("kill_switch_clear", False, f"current state is not trusted: {state_error}")
        report.add("runtime_mode_running", False, f"current state is not trusted: {state_error}")
        report.add("persisted_safety_gates", False, f"current state is not trusted: {state_error}")
        report.add("position_reconciliation", False, "cannot reconcile an untrusted state")
    elif state is None:
        flat = not positions
        report.add("kill_switch_clear", True, "no persisted kill switch")
        report.add("runtime_mode_running", True, "no persisted runtime mode")
        report.add(
            "persisted_safety_gates",
            flat,
            "fresh flat deployment has no persisted gates"
            if flat
            else "broker is not flat but no trusted local state exists",
        )
        report.add(
            "position_reconciliation",
            flat,
            "fresh deployment and broker are flat"
            if flat
            else "broker positions exist without trusted local expected positions",
        )
    else:
        report.add(
            "kill_switch_clear",
            not state.kill_switch,
            state.kill_reason or ("clear" if not state.kill_switch else "kill switch is active"),
        )
        report.add(
            "runtime_mode_running",
            state.runtime_mode == RuntimeMode.RUNNING.value,
            f"runtime mode: {state.runtime_mode}",
        )
        gates_ready = state.reconciled and state.metadata_verified
        report.add(
            "persisted_safety_gates",
            gates_ready,
            f"reconciled={state.reconciled}, metadata_verified={state.metadata_verified}",
        )
        reconciliation = compare_positions(store.positions_from_state(state), positions)
        report.add(
            "position_reconciliation",
            reconciliation.matched,
            reconciliation.details or "local expected positions match broker snapshot",
        )

    activity_detail = "directional strategy is disabled"
    activity_ready = True
    if config.directional.enabled:
        activity_path = Path(config.state_path).with_name("directional_activity.json")
        try:
            snapshot = DirectionalActivityStore(activity_path).load()
        except (OSError, KeyError, TypeError, ValueError) as exc:
            activity_ready = False
            activity_detail = f"invalid directional activity evidence: {exc}"
        else:
            if snapshot is None:
                activity_ready = False
                activity_detail = "no completed directional activity; observe one full trading day"
            else:
                try:
                    activity_day = datetime.strptime(snapshot.trading_day, "%Y%m%d").date()
                    current_day = datetime.strptime(trading_day, "%Y%m%d").date()
                except ValueError as exc:
                    activity_ready = False
                    activity_detail = f"invalid directional activity trading day: {exc}"
                else:
                    contracts_valid = all(
                        symbol == item.symbol
                        and bool(item.exchange)
                        and bool(item.product)
                        and item.trading_day == snapshot.trading_day
                        and isfinite(item.volume)
                        and item.volume >= 0
                        and isfinite(item.open_interest)
                        and item.open_interest >= 0
                        and item.timestamp.tzinfo is not None
                        for symbol, item in snapshot.contracts.items()
                    )
                    activity_ready = bool(
                        snapshot.contracts and contracts_valid and activity_day < current_day
                    )
                    activity_detail = (
                        f"completed activity day: {snapshot.trading_day}"
                        if activity_ready
                        else "activity must be internally valid and precede the broker trading day"
                    )
    report.add("directional_activity_ready", activity_ready, activity_detail)

    margin_ratio = account.margin / account.equity if account.equity > 0 else None
    report.facts["broker"] = {
        "ready": broker_ready,
        "fresh_snapshot": fresh_snapshot,
        "trading_day": trading_day,
        "account_equity": account.equity,
        "account_available": account.available,
        "account_margin": account.margin,
        "margin_ratio": margin_ratio,
        "position_count": len([item for item in positions if not item.empty]),
        "active_order_count": active_order_count,
        "contract_catalog_count": catalog_count,
        "metadata_symbols": sorted(metadata),
        "orders_sent": 0,
    }
    return report
