from pathlib import Path

import pytest

from afuture.cli import (
    adopt_recovery_state,
    drain_after_halt,
    validate_recovery_positions,
    wait_for_fresh_snapshot,
)
from afuture.models import AccountSnapshot, ContractPosition, PairConfig
from afuture.state import RuntimeState, StateStore


def test_status_is_local_read_only_and_does_not_create_log(tmp_path: Path, capsys) -> None:
    from afuture.cli import main

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

    assert main(["status", "--config", str(config_path)]) == 0
    assert '"passed": true' in capsys.readouterr().out
    assert not (tmp_path / "afuture.log").exists()


def test_status_does_not_require_live_ctp_credentials(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.cli import main

    for name in ("AFUTURE_CTP_USER", "AFUTURE_CTP_PASSWORD", "AFUTURE_CTP_BROKER"):
        monkeypatch.delenv(name, raising=False)
    config_path = tmp_path / "live-status.toml"
    config_path.write_text(
        """
[system]
mode = "live"
initial_capital = 500000

[ctp]
environment = "test"
td_address = "tcp://trade.example"
md_address = "tcp://market.example"

[directional]
enabled = true
products = ["M"]

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

    assert main(["status", "--config", str(config_path)]) == 0
    assert '"passed": true' in capsys.readouterr().out


def test_doctor_preflight_never_calls_send_order(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from afuture.auto import AutoConfig
    from afuture.cli import _run_doctor
    from afuture.directional import DirectionalConfig
    from afuture.models import ContractInfo, ContractSpec, FeeSpec
    from afuture.risk import RiskConfig

    spec = ContractSpec("m2609", "DCE", 10, 1, 0.12, 0.12, FeeSpec(open_fixed=1))

    class FakeBroker:
        def __init__(self, credentials):
            self.credentials = credentials

        def start(self):
            return None

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

    assert _run_doctor(config, args) == 0
    assert '"orders_sent": 0' in capsys.readouterr().out


def test_wait_for_fresh_snapshot_requires_both_generations_to_advance():
    class FakeBroker:
        def __init__(self):
            self.calls = 0

        def snapshot_marker(self):
            return (2, 3)

        def snapshot_ready(self, marker):
            assert marker == (2, 3)
            self.calls += 1
            return self.calls >= 2

    broker = FakeBroker()
    wait_for_fresh_snapshot(broker, timeout_seconds=0.2, poll_interval=0.001)
    assert broker.calls >= 2


def test_wait_for_fresh_snapshot_times_out_without_complete_snapshot():
    class FakeBroker:
        def snapshot_marker(self):
            return (0, 0)

        def snapshot_ready(self, marker):
            return False

    with pytest.raises(RuntimeError, match="fresh CTP account/position snapshot"):
        wait_for_fresh_snapshot(FakeBroker(), timeout_seconds=0.001, poll_interval=0.0001)


def test_recovery_accepts_balanced_dynamic_volume_not_only_pair_cap():
    pair = PairConfig("m_pair", "m2609", "m2701", "DCE", 5)
    positions = [
        ContractPosition("m2609", "DCE", long_yesterday=2),
        ContractPosition("m2701", "DCE", short_yesterday=2),
    ]

    validate_recovery_positions([pair], positions)


def test_recovery_rejects_volume_above_pair_cap_and_unbalanced_positions():
    pair = PairConfig("m_pair", "m2609", "m2701", "DCE", 3)
    with pytest.raises(RuntimeError, match="configured risk cap"):
        validate_recovery_positions(
            [pair],
            [
                ContractPosition("m2609", "DCE", long_today=4),
                ContractPosition("m2701", "DCE", short_today=4),
            ],
        )
    with pytest.raises(RuntimeError, match="balanced spread"):
        validate_recovery_positions([pair], [ContractPosition("m2609", "DCE", long_today=1)])


def test_recovery_rejects_unknown_contract():
    pair = PairConfig("m_pair", "m2609", "m2701", "DCE", 3)
    with pytest.raises(RuntimeError, match="not configured"):
        validate_recovery_positions([pair], [ContractPosition("rb2610", "SHFE", long_today=1)])


def test_adopt_recovery_state_keeps_kill_switch_and_requires_fresh_metadata(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    state = RuntimeState(
        kill_switch=True,
        kill_reason="position reconciliation failed",
        trading_day="20260819",
        day_start_equity=500000,
        equity_high_watermark=520000,
        metadata_verified=True,
    )
    account = AccountSnapshot(500000, 500000, 400000, 100000, 0, 0, "20260820")
    positions = [ContractPosition("m2609", "DCE", long_yesterday=1, long_price=3000.0)]

    adopt_recovery_state(store, state, account, positions)

    saved = store.load()
    assert saved.kill_switch
    assert not saved.reconciled
    assert not saved.metadata_verified
    assert saved.trading_day == "20260820"
    assert saved.day_start_equity == 500000
    assert saved.equity_high_watermark == 520000
    assert store.positions_from_state(saved)[0].long_yesterday == 1


def test_drain_after_halt_waits_until_active_orders_are_gone():
    class FakeBroker:
        def __init__(self):
            self.active = [type("Order", (), {"order_id": "o1"})()]
            self.cancelled = []

        def get_active_orders(self):
            return list(self.active)

        def cancel_order(self, order_id):
            self.cancelled.append(order_id)

    class FakeEngine:
        def __init__(self, broker):
            self.broker = broker
            self.calls = 0

        def run_once(self):
            self.calls += 1
            self.broker.active.clear()

    broker = FakeBroker()
    engine = FakeEngine(broker)

    assert drain_after_halt(engine, broker, timeout_seconds=0.1, poll_interval=0.001)
    assert broker.cancelled == ["o1"]
    assert engine.calls >= 1
