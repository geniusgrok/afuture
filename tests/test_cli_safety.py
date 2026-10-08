from pathlib import Path
from types import SimpleNamespace

import pytest

from afuture.directional import DirectionalConfig
from afuture.models import (
    AccountSnapshot,
    ContractInfo,
)
from afuture.risk import RiskConfig
from afuture.state import RuntimeState, StateStore


@pytest.fixture(autouse=True)
def _use_isolated_current_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "afuture.account_runtime_registry.PRODUCTION_ACCOUNT_RUNTIME_REGISTRY_PATH",
        tmp_path / ".account-runtime-registry.json",
    )


def test_status_is_local_read_only_and_does_not_create_log(tmp_path: Path, capsys) -> None:
    from afuture.cli import run_command

    config_path = tmp_path / "status.toml"
    config_path.write_text(
        """
[system]
mode = "replay"
initial_capital = 500000

[paths]
state = "{state}"
log = "{log}"
report = "{report}"
journal = "{journal}"
alert = "{alert}"
""".format(
            state=tmp_path / "state.json",
            log=tmp_path / "afuture.log",
            report=tmp_path / "report.json",
            journal=tmp_path / "audit.jsonl",
            alert=tmp_path / "alerts.jsonl",
        ),
        encoding="utf-8",
    )

    assert run_command(["status", "--config", str(config_path)]) == 0
    assert '"passed": true' in capsys.readouterr().out
    assert not (tmp_path / "afuture.log").exists()


def test_doctor_preflight_never_calls_send_order(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.auto import AutoConfig
    from afuture.cli import _run_doctor
    from afuture.models import ContractSpec, FeeSpec

    spec = ContractSpec("m2609", "DCE", 10, 1, 0.12, 0.12, FeeSpec(open_fixed=1))

    class FakeBroker:
        def __init__(self, credentials):
            self.credentials = credentials
            self.seeded_trade_ids = None

        def start(self):
            assert self.seeded_trade_ids == ["20260825:DCE:DOCTOR-T1"]
            return None

        def get_account_identity_digest(self):
            return "a" * 64

        def seed_trade_identities(self, identities):
            assert self.seeded_trade_ids is None
            self.seeded_trade_ids = list(identities)

        def stop(self):
            return None

        def is_ready(self):
            return True

        def snapshot_marker(self):
            return (0, 0)

        def snapshot_ready(self, marker):
            return True

        def get_account(self):
            return AccountSnapshot(500_000, 500_000, 440_000, 60_000, 0, 0, "20260825")

        def get_contract_catalog(self):
            return [ContractInfo("m2609", "DCE", "M", "2026-12-31")]

        def get_trading_day(self):
            return "20260825"

        def get_live_contract_specs(self, symbols, timeout_seconds):
            assert symbols == ["m2609"]
            return {"m2609": spec}

        def get_positions(self):
            return []

        def get_active_orders(self):
            return []

        def refresh_session_activity(self, *, timeout_seconds):
            from afuture.broker.ctp_session_query import (
                build_ctp_session_activity_evidence,
            )

            assert timeout_seconds == 0.1
            return build_ctp_session_activity_evidence(
                account_identity_digest="a" * 64,
                trading_day="20260825",
                order_request_id=11,
                trade_request_id=12,
                orders=(),
                trades=(),
                critical_generation=0,
            )

        def require_session_activity_evidence_current(self, _evidence):
            return None

        def get_session_trades(self):
            return []

        def owns_order(self, _order_id):
            return False

        def send_order(self, request):
            raise AssertionError("doctor must never submit an order")

    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", FakeBroker)
    config = SimpleNamespace(
        mode="live",
        ctp=SimpleNamespace(environment="test"),
        contracts={"m2609": spec},
        auto=AutoConfig(),
        directional=DirectionalConfig(),
        risk=RiskConfig(max_margin_ratio=0.35, min_available_ratio=0.25),
        metadata_timeout_seconds=1.0,
        state_path=str(tmp_path / "state.json"),
        account_registry_path=str(tmp_path / ".account-runtime-registry.json"),
        log_path=str(tmp_path / "afuture.log"),
        report_path=str(tmp_path / "report.json"),
        journal_path=str(tmp_path / "audit.jsonl"),
        alert_path=str(tmp_path / "alerts.jsonl"),
    )
    args = SimpleNamespace(
        confirm_live=False,
        startup_timeout=0.1,
        snapshot_wait=0.1,
        metadata_limit=1,
    )
    StateStore(config.state_path).save(
        RuntimeState(
            reconciled=True,
            metadata_verified=True,
            recent_trade_ids=["20260825:DCE:DOCTOR-T1"],
        )
    )

    assert _run_doctor(config, args) == 0
    assert '"orders_sent": 0' in capsys.readouterr().out


def test_stress90_startup_unknown_order_halts_without_cancelling():
    from afuture.cli import wait_for_stress90_startup_orders
    from afuture.models import Offset, Order, OrderRequest, OrderSide, OrderStatus

    active = Order(
        "CTP.manual",
        OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 100.0),
        OrderStatus.NOT_TRADED,
    )

    class Broker:
        def get_active_orders(self):
            return [active]

        def owns_order(self, _order_id):
            return False

        def cancel_order(self, _order_id):
            raise AssertionError("unknown order must HALT, not be blanket-cancelled")

    with pytest.raises(RuntimeError, match="unknown active order"):
        wait_for_stress90_startup_orders(
            SimpleNamespace(), Broker(), timeout_seconds=0.1, poll_interval=0.001
        )
