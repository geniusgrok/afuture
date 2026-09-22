"""Private, test-counter-only capture of the broker's actual settlement format."""

from __future__ import annotations

import json
import math
import os
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

from .broker.ctp import CtpBroker, CtpCredentials
from .config import load_config


def capture_test_settlement(
    *,
    config_path: str | Path,
    trading_day: str,
    output_path: str | Path,
    confirm_test_connection: bool,
    timeout_seconds: float = 30.0,
) -> dict[str, object]:
    """Capture evidence only; no orders, cancellation, state adoption or permit.

    Native login can confirm the counter's settlement as part of its normal
    authentication flow. Thus an explicit, isolated-test connection approval is
    required BEFORE construction or connection, even though this command cannot
    authorize an order. It is intentionally unavailable for a production front.
    """
    if confirm_test_connection is not True:
        raise RuntimeError("settlement capture requires explicit isolated test connection approval")
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("settlement capture timeout must be finite and positive")
    CtpBroker._validate_trading_day(trading_day)
    config = load_config(config_path, require_ctp_credentials=False)
    if config.mode != "live" or config.ctp_environment != "test":
        raise RuntimeError("settlement capture is restricted to live-mode isolated test CTP")
    output = Path(output_path)
    if not output.is_absolute() or output.is_symlink() or output.exists():
        raise RuntimeError("settlement capture output must be a new absolute private file")
    parent = output.parent.resolve(strict=True)
    if parent != output.parent or not parent.is_dir():
        raise RuntimeError("settlement capture output parent must be canonical and exist")
    # Preflight the destination before creating any native connection.  Reserve
    # it with O_EXCL, keep the fd, and never replace another artifact on failure.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    broker: CtpBroker | None = None
    lease = None
    try:
        config = load_config(config_path, require_ctp_credentials=True)
        if not isinstance(config.ctp, CtpCredentials) or config.ctp.environment != "test":
            raise RuntimeError("test CTP credential/environment identity mismatch")
        if not config.ctp.account_id or not config.ctp.currency_id:
            raise RuntimeError("test capture needs explicit expected AccountID and CurrencyID")
        from .cli import _wait_until_ready, wait_for_fresh_snapshot
        from .runtime_lease import AccountExclusiveRuntimeLease

        broker = CtpBroker(config.ctp)
        account_digest = broker.get_account_identity_digest()
        lease = AccountExclusiveRuntimeLease(
            Path(config.state_path).resolve(strict=False).parent,
            account_digest,
            role="settlement-capture-test-only",
        )
        lease.acquire()
        broker.start()
        _wait_until_ready(broker, float(timeout_seconds))
        wait_for_fresh_snapshot(broker, float(timeout_seconds))
        document = broker.capture_settlement_document(
            trading_day, timeout_seconds=float(timeout_seconds)
        )
        envelope = document.to_dict()
        envelope.update(
            account_identity_digest=account_digest,
            ctp_environment="test",
            native_binding_version=version("vnpy_ctp"),
            current_ctp_trading_day=broker.get_trading_day(),
            config_sha256=sha256(Path(config_path).read_bytes()).hexdigest(),
        )
        content = (
            json.dumps(
                envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
            + b"\n"
        )
        view = memoryview(content)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("settlement capture write made no progress")
            view = view[written:]
        os.fsync(fd)
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return {
            "captured": True,
            "output": str(output),
            "sha256": sha256(content).hexdigest(),
            "bytes": len(content),
            "request_id": document.request_id,
            "fragment_count": len(document.fragments),
            "financial_continuity_verified": False,
            "orders_sent": 0,
            "cancels_sent": 0,
        }
    finally:
        try:
            if broker is not None:
                broker.stop()
        finally:
            if lease is not None:
                lease.release()
            os.close(fd)
        # Partial evidence remains available for forensics, never relabeled as a
        # valid capture or silently replaced by a retry. No account state changed.
