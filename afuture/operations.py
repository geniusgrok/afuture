"""Read-only operational inspection and fail-closed live preflight checks."""

from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from math import isfinite
from pathlib import Path

from .directional_activity import (
    DirectionalActivityStore,
    select_contracts_from_activity,
    validate_directional_activity_snapshot,
)
from .metadata import validate_contract_metadata
from .models import AccountSnapshot, ContractInfo, ContractPosition, ContractSpec, RuntimeMode
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
        "symlink": path.is_symlink(),
        "size_bytes": path.stat().st_size if path.is_file() else 0,
    }


def _symlink_components(path: Path) -> list[Path]:
    return [candidate for candidate in (path, *path.parents) if candidate.is_symlink()]


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
    symlink_paths = sorted(
        {str(candidate) for path in paths.values() for candidate in _symlink_components(path)}
    )
    invalid_targets = sorted(
        str(path)
        for path in paths.values()
        if not path.is_symlink() and path.exists() and not path.is_file()
    )
    invalid_ancestors = sorted(str(path) for path in ancestors if not path.is_dir())
    inaccessible_ancestors = sorted(
        str(path) for path in ancestors if path.is_dir() and not os.access(path, os.W_OK | os.X_OK)
    )
    write_targets = (paths["log"], paths["report"], paths["audit"], paths["alert"])
    unwritable_targets = sorted(
        str(path) for path in write_targets if path.is_file() and not os.access(path, os.W_OK)
    )
    path_errors = [
        *(f"symlink is not allowed in runtime path: {path}" for path in symlink_paths),
        *(f"target is not a file: {path}" for path in invalid_targets),
        *(f"ancestor is not a directory: {path}" for path in invalid_ancestors),
        *(f"ancestor is not writable/searchable: {path}" for path in inaccessible_ancestors),
        *(f"write target is not writable: {path}" for path in unwritable_targets),
    ]
    report.add(
        "runtime_paths_writable",
        not path_errors,
        "runtime path ancestors are writable" if not path_errors else "; ".join(path_errors),
    )
    disk_roots = {path if path.is_dir() else path.parent for path in ancestors}
    free_by_path = {str(path): shutil.disk_usage(path).free for path in sorted(disk_roots)}
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
    catalog: list[ContractInfo],
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
        account_valid = False
        report.add("account_snapshot_valid", False, str(exc))
    else:
        account_valid = True
        report.add("account_snapshot_valid", True, "account values are finite and valid")
    try:
        parsed_trading_day = datetime.strptime(trading_day, "%Y%m%d").strftime("%Y%m%d")
    except ValueError:
        parsed_trading_day = ""
    trading_day_consistent = bool(
        len(trading_day) == 8
        and parsed_trading_day == trading_day
        and account.trading_day == trading_day
    )
    report.add(
        "trading_day_consistent",
        trading_day_consistent,
        f"broker={trading_day!r}, account={account.trading_day!r}",
    )
    report.add(
        "no_active_orders",
        active_order_count == 0,
        f"active order count: {active_order_count}",
    )
    report.add(
        "contract_catalog_available",
        bool(catalog),
        f"contract catalog count: {len(catalog)}",
    )

    requested = sorted(set(requested_symbols))
    live_valid, live_detail = _live_specs_valid(metadata, requested)
    if not requested:
        live_valid, live_detail = False, "no contract metadata was sampled"
    report.add("live_metadata_complete", live_valid, live_detail)
    configured = {
        symbol: config.contracts[symbol] for symbol in requested if symbol in config.contracts
    }
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

    risk_failures: list[str] = []
    if not account_valid:
        risk_failures.append("invalid account snapshot")
    elif account.equity > 0:
        margin_ratio = account.margin / account.equity
        available_ratio = account.available / account.equity
        if margin_ratio > config.risk.max_margin_ratio:
            risk_failures.append(
                f"margin ratio {margin_ratio:.6f} > {config.risk.max_margin_ratio:.6f}"
            )
        if available_ratio < config.risk.min_available_ratio:
            risk_failures.append(
                f"available ratio {available_ratio:.6f} < {config.risk.min_available_ratio:.6f}"
            )
        if state is not None:
            if state.trading_day == account.trading_day and state.day_start_equity > 0:
                daily_loss = (
                    max(0.0, state.day_start_equity - account.equity) / state.day_start_equity
                )
                if daily_loss >= config.risk.max_daily_loss_ratio:
                    risk_failures.append(
                        f"daily loss {daily_loss:.6f} >= {config.risk.max_daily_loss_ratio:.6f}"
                    )
            if state.equity_high_watermark > 0:
                drawdown = (
                    max(0.0, state.equity_high_watermark - account.equity)
                    / state.equity_high_watermark
                )
                if drawdown >= config.risk.max_total_drawdown_ratio:
                    risk_failures.append(
                        f"drawdown {drawdown:.6f} >= {config.risk.max_total_drawdown_ratio:.6f}"
                    )
    report.add(
        "account_risk_limits",
        not risk_failures,
        "account margin, available, daily-loss and drawdown limits pass"
        if not risk_failures
        else "; ".join(risk_failures),
    )

    activity_detail = "directional strategy is disabled"
    activity_ready = True
    if config.directional.enabled:
        activity_path = Path(config.state_path).with_name("directional_activity.json")
        try:
            snapshot = DirectionalActivityStore(activity_path).load()
            if snapshot is not None:
                validate_directional_activity_snapshot(snapshot)
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
                    selected = (
                        select_contracts_from_activity(
                            config.directional,
                            catalog,
                            snapshot,
                            current_day,
                        )
                        if activity_day < current_day
                        else {}
                    )
                    configured_products = {
                        product.upper() for product in config.directional.products
                    }
                    selected_products = {product.upper() for product in selected}
                    missing_products = sorted(configured_products - selected_products)
                    activity_ready = bool(configured_products) and not missing_products
                    activity_detail = (
                        f"completed activity day: {snapshot.trading_day}; "
                        f"eligible products: {len(selected_products)}"
                        if activity_ready
                        else "activity/catalog coverage missing eligible products: "
                        + ", ".join(missing_products or sorted(configured_products))
                    )
    report.add("directional_activity_ready", activity_ready, activity_detail)

    broker_margin_ratio = account.margin / account.equity if account.equity > 0 else None
    report.facts["broker"] = {
        "ready": broker_ready,
        "fresh_snapshot": fresh_snapshot,
        "trading_day": trading_day,
        "account_equity": account.equity,
        "account_available": account.available,
        "account_margin": account.margin,
        "margin_ratio": broker_margin_ratio,
        "position_count": len([item for item in positions if not item.empty]),
        "active_order_count": active_order_count,
        "contract_catalog_count": len(catalog),
        "metadata_symbols": sorted(metadata),
        "orders_sent": 0,
    }
    return report
