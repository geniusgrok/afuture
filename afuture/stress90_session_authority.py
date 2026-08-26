"""Complete current-session ownership proof shared by lifecycle and live startup."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep

from .broker.ctp_order_journal import CtpOrderSubmissionJournal
from .broker.ctp_session_query import (
    CtpSessionActivityEvidence,
    CtpSessionActivityEvidenceStore,
    CtpSessionQueryIntegrityError,
    validate_ctp_session_activity_ownership,
)
from .models import Trade


@dataclass(frozen=True)
class Stress90SessionOwnershipProof:
    evidence: CtpSessionActivityEvidence
    ownership_digest: str
    local_session_trades: tuple[Trade, ...]


class Stress90SessionOrdersActive(RuntimeError):
    def __init__(self, order_ids: tuple[str, ...]) -> None:
        super().__init__("owned CTP session orders remain active: " + ",".join(order_ids))
        self.order_ids = order_ids


def establish_stress90_session_ownership(
    broker,
    *,
    runtime_dir: str | Path,
    timeout_seconds: float,
    install_startup_capability: bool = False,
    recover_crash_window: bool = False,
) -> Stress90SessionOwnershipProof:
    """Query, exact-join and persist one complete session generation."""

    runtime = Path(runtime_dir)
    checkpoint = getattr(broker, "checkpoint_order_submission_journal", None)
    if callable(checkpoint):
        try:
            checkpoint()
        except Exception as exc:
            raise RuntimeError("session ownership journal checkpoint failed") from exc
    refresh = getattr(broker, "refresh_session_activity", None)
    if not callable(refresh):
        raise RuntimeError("lifecycle Broker cannot prove complete session activity")
    try:
        evidence = refresh(timeout_seconds=float(timeout_seconds))
    except Exception as exc:
        raise RuntimeError("lifecycle Broker complete session activity is unavailable") from exc
    if not isinstance(evidence, CtpSessionActivityEvidence):
        raise RuntimeError("lifecycle Broker complete session activity is invalid")

    identity_getter = getattr(
        broker,
        "get_session_activity_account_identity_digest",
        broker.get_account_identity_digest,
    )
    try:
        expected_account = identity_getter()
        current_day = broker.get_trading_day()
    except Exception as exc:
        raise RuntimeError("lifecycle Broker session identity is unavailable") from exc
    if evidence.account_identity_digest != expected_account:
        raise RuntimeError("lifecycle complete session account identity mismatch")
    if evidence.trading_day != current_day:
        raise RuntimeError("lifecycle complete session trading day mismatch")

    journal = CtpOrderSubmissionJournal(runtime / "stress90_ctp_orders.json")
    try:
        if recover_crash_window:
            recover = getattr(broker, "recover_stress90_session_activity", None)
            if not callable(recover):
                raise RuntimeError("Broker cannot recover complete session activity")
            active_order_ids = tuple(recover(evidence))
            if active_order_ids:
                raise Stress90SessionOrdersActive(active_order_ids)
        entries = journal.load_lifecycle_entries(
            account_identity_digest=evidence.account_identity_digest,
            trading_day=evidence.trading_day,
        )
        ownership_digest = validate_ctp_session_activity_ownership(evidence, entries)
    except Stress90SessionOrdersActive:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise RuntimeError("lifecycle complete session ownership is invalid") from exc

    get_local_trades = getattr(broker, "get_session_trades", None)
    owns_order = getattr(broker, "owns_order", None)
    if not callable(get_local_trades) or not callable(owns_order):
        raise RuntimeError("lifecycle Broker cannot prove local session trade ownership")
    try:
        local_trades = get_local_trades()
    except Exception as exc:
        raise RuntimeError("lifecycle Broker local session evidence is unavailable") from exc
    if not isinstance(local_trades, list):
        raise RuntimeError("lifecycle Broker local session evidence is invalid")
    for trade in local_trades:
        if not isinstance(trade, Trade):
            raise RuntimeError("lifecycle Broker local session evidence is invalid")
        try:
            trade.validate()
            owned = owns_order(trade.order_id)
        except Exception as exc:
            raise RuntimeError("lifecycle Broker local ownership evidence is invalid") from exc
        if owned is not True:
            raise RuntimeError(
                f"unknown session trade blocks lifecycle operation: {trade.trade_id}"
            )

    try:
        CtpSessionActivityEvidenceStore(
            runtime / "stress90_ctp_session_evidence.json"
        ).save(evidence)
    except (OSError, CtpSessionQueryIntegrityError) as exc:
        raise RuntimeError("lifecycle complete session evidence persistence failed") from exc
    require_current = getattr(broker, "require_session_activity_evidence_current", None)
    if not callable(require_current):
        raise RuntimeError("Broker cannot prove that complete session evidence remains current")
    try:
        require_current(evidence)
    except Exception as exc:
        raise RuntimeError("complete session evidence changed after persistence") from exc
    if install_startup_capability:
        installer = getattr(broker, "install_stress90_session_startup_capability", None)
        if not callable(installer):
            raise RuntimeError("Stress-90 Broker startup session capability is unavailable")
        try:
            installer(evidence=evidence, ownership_digest=ownership_digest)
        except Exception as exc:
            raise RuntimeError("Stress-90 Broker startup session capability failed") from exc
    return Stress90SessionOwnershipProof(
        evidence=evidence,
        ownership_digest=ownership_digest,
        local_session_trades=tuple(local_trades),
    )


class Stress90StartupSessionAuthority:
    """Establish one process-local query capability on every Stress-90 startup."""

    def __init__(self, runtime_dir: str | Path) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.last_proof: Stress90SessionOwnershipProof | None = None

    def verify(self, *, broker, lease, timeout_seconds: float) -> Stress90SessionOwnershipProof:
        account_identity = broker.get_account_identity_digest()
        authorizes = getattr(lease, "authorizes_technical_activation", None)
        if not callable(authorizes) or not authorizes(account_identity, self.runtime_dir):
            raise RuntimeError("exact account lease is not held for Stress-90 startup")
        timeout = max(0.1, float(timeout_seconds))
        deadline = monotonic() + timeout
        while True:
            try:
                proof = establish_stress90_session_ownership(
                    broker,
                    runtime_dir=self.runtime_dir,
                    timeout_seconds=max(0.1, deadline - monotonic()),
                    install_startup_capability=True,
                    recover_crash_window=True,
                )
                break
            except Stress90SessionOrdersActive as exc:
                if monotonic() >= deadline:
                    raise RuntimeError(
                        "owned CTP startup orders did not become terminal: "
                        + ",".join(exc.order_ids)
                    ) from exc
                sleep(min(0.1, max(0.0, deadline - monotonic())))
        self.last_proof = proof
        return proof
