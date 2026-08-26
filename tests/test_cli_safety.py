from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from afuture.broker.ctp import CtpBroker, CtpCredentials
from afuture.cli import (
    _collect_doctor_quotes,
    _recover_state,
    _shadow_runtime_paths,
    adopt_recovery_state,
    drain_after_halt,
    validate_recovery_positions,
    wait_for_fresh_snapshot,
)
from afuture.models import (
    AccountSnapshot,
    BrokerEvent,
    ContractInfo,
    ContractPosition,
    PairConfig,
    Tick,
)
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


def test_live_stress90_lifecycle_runtime_dir_cannot_fork_account_lineage(
    tmp_path: Path,
) -> None:
    from afuture.cli import _stress90_lifecycle_paths

    canonical = tmp_path / "runtime"
    config = SimpleNamespace(
        state_path=str(canonical / "canonical-state.json"),
        journal_path=str(canonical / "canonical-audit.jsonl"),
    )

    paths = _stress90_lifecycle_paths(config, str(canonical), shadow_account=False)
    assert paths["runtime"] == canonical.resolve()
    assert paths["state"] == Path(config.state_path).resolve()
    assert paths["journal"] == Path(config.journal_path).resolve()
    with pytest.raises(RuntimeError, match="canonical runtime"):
        _stress90_lifecycle_paths(
            config,
            str(tmp_path / "fresh-lineage-bypass"),
            shadow_account=False,
        )

    shadow = _stress90_lifecycle_paths(
        config,
        str(canonical / "shadow"),
        shadow_account=True,
    )
    assert shadow["runtime"] == (canonical / "shadow").resolve()
    with pytest.raises(RuntimeError, match="Shadow.*runtime"):
        _stress90_lifecycle_paths(config, "", shadow_account=True)


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
policy = "execution_aligned"
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
    from afuture.auto import AutoConfig
    from afuture.cli import _run_doctor
    from afuture.directional import DirectionalConfig
    from afuture.models import ContractInfo, ContractSpec, FeeSpec
    from afuture.risk import RiskConfig

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


def test_stress90_doctor_explicitly_issues_zero_order_technical_permit_under_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.auto import AutoConfig
    from afuture.cli import _run_doctor
    from afuture.directional import DirectionalConfig
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.operations import OperationalReport
    from afuture.risk import RiskConfig
    from afuture.stress90_activation_permit import (
        STRESS90_ACTIVATION_PERMIT_ACK,
        STRESS90_DOCTOR_INTERNAL_P0_CHECKS,
    )

    calls: list[dict[str, object]] = []

    class FakeBroker:
        def __init__(self, credentials):
            self.credentials = credentials

        def seed_trade_identities(self, identities):
            assert list(identities) == []

        def configure_order_submission_journal(self, path, **identity):
            del path, identity

        def start(self):
            return None

        def stop(self):
            return None

        def is_ready(self):
            return True

        def snapshot_marker(self):
            return (0, 0)

        def snapshot_ready(self, marker):
            return marker == (0, 0)

        def get_account_identity_digest(self):
            return "a" * 64

        def get_account(self):
            return AccountSnapshot(500_000, 500_000, 500_000, 0, 0, 0, "20260825")

        def get_contract_catalog(self):
            return [ContractInfo("m2609", "DCE", "M", "2026-12-31")]

        def get_trading_day(self):
            return "20260825"

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

        def get_session_activity_account_identity_digest(self):
            return "a" * 64

        def get_session_trades(self):
            return []

        def owns_order(self, _order_id):
            return False

        def get_live_contract_specs(self, symbols, timeout_seconds):
            del symbols, timeout_seconds
            return {}

        def send_order(self, request):
            raise AssertionError("doctor must never submit an order")

    report = OperationalReport()
    for name in STRESS90_DOCTOR_INTERNAL_P0_CHECKS:
        report.add(name, True, "verified")
    report.add("stress90_runtime_permission", False, "HALTED")
    report.facts["broker"] = {"orders_sent": 0}
    report.facts["stress90"] = {
        "stress90_ready": True,
        "external_blocker_reasons": ["multi_day_shadow"],
        "capital_activation_eligible": False,
        "live_eligibility": False,
    }

    def issue(**kwargs):
        calls.append(kwargs)
        assert kwargs["strong_confirmation"] == STRESS90_ACTIVATION_PERMIT_ACK
        assert kwargs["active_orders"] == []
        return SimpleNamespace(permit=SimpleNamespace(status="issued"))

    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", FakeBroker)
    monkeypatch.setattr("afuture.operations.build_doctor_report", lambda *a, **k: report)
    monkeypatch.setattr(
        "afuture.stress90_activation_permit.issue_stress90_doctor_permit",
        issue,
    )
    monkeypatch.setenv("AFUTURE_LIVE_ACK", "I_UNDERSTAND_FUTURES_RISK")
    monkeypatch.setenv(
        "AFUTURE_STRESS90_ACTIVATION_PERMIT_ACK",
        STRESS90_ACTIVATION_PERMIT_ACK,
    )
    config = SimpleNamespace(
        mode="live",
        ctp=SimpleNamespace(environment="production"),
        contracts={},
        auto=AutoConfig(),
        directional=DirectionalConfig(
            enabled=True,
            policy="stress90",
            products=FROZEN_PRODUCTS,
            exchanges=("DCE",),
            account_exclusive=True,
        ),
        risk=RiskConfig(),
        metadata_timeout_seconds=1.0,
        state_path=str(tmp_path / "state.json"),
        log_path=str(tmp_path / "afuture.log"),
        report_path=str(tmp_path / "report.json"),
        journal_path=str(tmp_path / "audit.jsonl"),
        alert_path=str(tmp_path / "alerts.jsonl"),
    )
    args = SimpleNamespace(
        confirm_live=True,
        startup_timeout=0.1,
        snapshot_wait=0.1,
        metadata_limit=0,
        issue_stress90_permit=True,
        shadow_account=False,
    )

    assert _run_doctor(config, args) == 0
    assert len(calls) == 1
    assert calls[0]["runtime_dir"] == tmp_path


def test_stress90_shadow_doctor_uses_canonical_account_without_live_day_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.auto import AutoConfig
    from afuture.cli import _run_doctor
    from afuture.directional import DirectionalConfig
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.operations import OperationalReport
    from afuture.risk import RiskConfig
    from afuture.stress90_activation_permit import (
        STRESS90_ACTIVATION_PERMIT_ACK,
        STRESS90_DOCTOR_INTERNAL_P0_CHECKS,
    )

    issued: list[dict[str, object]] = []
    report_configs: list[object] = []

    class FakeLive:
        def __init__(self, credentials):
            self.credentials = credentials

        def get_account_identity_digest(self):
            return "b" * 64

        def seed_trade_identities(self, identities):
            assert list(identities) == []

    class FakeShadow:
        def __init__(self, live, initial_capital, **kwargs):
            del live
            assert initial_capital == 500_000
            assert kwargs == {
                "slippage_ticks": 2,
                "latency_ticks": 3,
                "market_impact_ticks": 4,
                "state_path": tmp_path / "shadow" / "shadow_broker_state.json",
            }

        def update_specs(self, specs):
            assert specs == {}

        def configure_order_submission_journal(self, path, **identity):
            del path, identity

        def start(self):
            return None

        def stop(self):
            return None

        def is_ready(self):
            return True

        def snapshot_marker(self):
            return (0, 0)

        def snapshot_ready(self, marker):
            return marker == (0, 0)

        def get_account_identity_digest(self):
            return "d" * 64

        def get_account(self):
            return AccountSnapshot(500_000, 500_000, 500_000, 0, 0, 0, "20260825")

        def get_contract_catalog(self):
            return [ContractInfo("M2612", "DCE", "M", "2026-12-15")]

        def get_trading_day(self):
            return "20260825"

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
                account_identity_digest="b" * 64,
                trading_day="20260825",
                order_request_id=11,
                trade_request_id=12,
                orders=(),
                trades=(),
                critical_generation=0,
            )

        def require_session_activity_evidence_current(self, _evidence):
            return None

        def get_session_activity_account_identity_digest(self):
            return "b" * 64

        def get_session_trades(self):
            return []

        def owns_order(self, _order_id):
            return False

        def get_live_contract_specs(self, symbols, timeout_seconds):
            del symbols, timeout_seconds
            return {}

    report = OperationalReport()
    for name in STRESS90_DOCTOR_INTERNAL_P0_CHECKS:
        report.add(name, True, "verified")
    report.facts["broker"] = {"orders_sent": 0}
    report.facts["stress90"] = {
        "stress90_ready": True,
        "external_blocker_reasons": ["multi_day_shadow"],
        "capital_activation_eligible": False,
        "live_eligibility": False,
    }

    def build_report(config, **kwargs):
        del kwargs
        report_configs.append(config)
        return report

    def issue(**kwargs):
        issued.append(kwargs)
        return SimpleNamespace(permit=SimpleNamespace(status="issued"))

    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", FakeLive)
    monkeypatch.setattr("afuture.broker.shadow.ShadowBroker", FakeShadow)
    monkeypatch.setattr("afuture.operations.build_doctor_report", build_report)
    monkeypatch.setattr(
        "afuture.stress90_activation_permit.issue_stress90_doctor_permit",
        issue,
    )
    monkeypatch.setattr(
        "afuture.cli._checkpoint_ctp_trading_day",
        lambda *args: (_ for _ in ()).throw(AssertionError("Shadow must not checkpoint live day")),
    )
    monkeypatch.setenv("AFUTURE_LIVE_ACK", "I_UNDERSTAND_FUTURES_RISK")
    monkeypatch.setenv(
        "AFUTURE_STRESS90_ACTIVATION_PERMIT_ACK",
        STRESS90_ACTIVATION_PERMIT_ACK,
    )
    config = SimpleNamespace(
        mode="live",
        ctp=SimpleNamespace(environment="production"),
        initial_capital=500_000,
        slippage_ticks=2,
        latency_ticks=3,
        market_impact_ticks=4,
        contracts={},
        auto=AutoConfig(),
        directional=DirectionalConfig(
            enabled=True,
            policy="stress90",
            products=FROZEN_PRODUCTS,
            exchanges=("DCE",),
            account_exclusive=True,
        ),
        risk=RiskConfig(),
        metadata_timeout_seconds=1.0,
        state_path=str(tmp_path / "state.json"),
        log_path=str(tmp_path / "afuture.log"),
        report_path=str(tmp_path / "report.json"),
        journal_path=str(tmp_path / "audit.jsonl"),
        alert_path=str(tmp_path / "alerts.jsonl"),
    )
    args = SimpleNamespace(
        confirm_live=True,
        startup_timeout=0.1,
        snapshot_wait=0.1,
        metadata_limit=0,
        issue_stress90_permit=True,
        shadow_account=True,
    )

    assert _run_doctor(config, args) == 0
    assert report_configs[0].state_path == str(tmp_path / "shadow" / "state.json")
    assert issued[0]["runtime_dir"] == tmp_path / "shadow"
    assert issued[0]["account_identity_digest"] == "d" * 64


def test_stress90_doctor_releases_account_lease_when_broker_start_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afuture.auto import AutoConfig
    from afuture.cli import _run_doctor
    from afuture.directional import DirectionalConfig
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS
    from afuture.risk import RiskConfig
    from afuture.runtime_lease import AccountExclusiveRuntimeLease

    class FailingBroker:
        def __init__(self, credentials):
            self.credentials = credentials

        def get_account_identity_digest(self):
            return "a" * 64

        def seed_trade_identities(self, identities):
            assert list(identities) == []

        def configure_order_submission_journal(self, path, **identity):
            del path, identity

        def start(self):
            raise RuntimeError("injected broker start failure")

        def stop(self):
            return None

    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", FailingBroker)
    config = SimpleNamespace(
        mode="live",
        ctp=SimpleNamespace(environment="test"),
        contracts={},
        auto=AutoConfig(),
        directional=DirectionalConfig(
            enabled=True,
            policy="stress90",
            products=FROZEN_PRODUCTS,
            account_exclusive=True,
        ),
        risk=RiskConfig(),
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
        metadata_limit=0,
        issue_stress90_permit=True,
        shadow_account=False,
    )

    with pytest.raises(RuntimeError, match="injected broker start failure"):
        _run_doctor(config, args)

    lease = AccountExclusiveRuntimeLease(tmp_path, "a" * 64, role="post-failure-proof")
    lease.acquire()
    lease.release()


def test_stress90_doctor_quote_collection_is_bounded_current_day_and_order_incapable():
    contracts = {
        "a2609": ContractInfo("a2609", "DCE", "A", "2026-09-01"),
        "m2609": ContractInfo("m2609", "DCE", "M", "2026-09-01"),
    }

    class FakeBroker:
        def __init__(self):
            self.subscriptions = []
            self.polls = 0

        def subscribe(self, symbol, exchange):
            self.subscriptions.append((symbol, exchange))

        def poll_events(self):
            self.polls += 1
            if self.polls == 1:
                return [
                    BrokerEvent(
                        "tick",
                        Tick(
                            "a2609",
                            "DCE",
                            datetime(2026, 8, 24, tzinfo=timezone.utc),
                            99,
                            101,
                            100,
                            10,
                            11,
                            "20260824",
                        ),
                    ),
                    BrokerEvent(
                        "tick",
                        Tick(
                            "m2609",
                            "DCE",
                            datetime(2026, 8, 25, tzinfo=timezone.utc),
                            2999,
                            3001,
                            3000,
                            12,
                            13,
                            "20260825",
                        ),
                    ),
                ]
            return [
                BrokerEvent(
                    "tick",
                    Tick(
                        "a2609",
                        "DCE",
                        datetime(2026, 8, 25, tzinfo=timezone.utc),
                        3999,
                        4001,
                        4000,
                        14,
                        15,
                        "20260825",
                    ),
                )
            ]

        def send_order(self, request):
            raise AssertionError("doctor must never submit an order")

    broker = FakeBroker()
    quotes = _collect_doctor_quotes(
        broker,
        contracts,
        trading_day="20260825",
        timeout_seconds=0.05,
        poll_interval=0.0001,
    )

    assert broker.subscriptions == [("a2609", "DCE"), ("m2609", "DCE")]
    assert set(quotes) == {"a2609", "m2609"}
    assert quotes["a2609"].trading_day == "20260825"


def test_stress90_doctor_quote_collection_fails_on_concurrent_trade_event():
    contract = ContractInfo("m2609", "DCE", "M", "2026-09-01")

    class FakeBroker:
        def subscribe(self, symbol, exchange):
            return None

        def poll_events(self):
            return [BrokerEvent("trade", object())]

    with pytest.raises(RuntimeError, match="trade event"):
        _collect_doctor_quotes(
            FakeBroker(),
            {"m2609": contract},
            trading_day="20260825",
            timeout_seconds=0.01,
            poll_interval=0.0001,
        )


def test_stress90_doctor_uses_activity_selected_contracts_not_sampling_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from afuture.auto import AutoConfig
    from afuture.cli import _run_doctor
    from afuture.directional import DirectionalConfig
    from afuture.directional_activity import (
        ContractActivity,
        DirectionalActivitySnapshot,
        DirectionalActivityStore,
    )
    from afuture.models import ContractSpec
    from afuture.risk import RiskConfig

    catalog = [ContractInfo("m2609", "DCE", "M", "2026-12-31")]
    spec = ContractSpec("m2609", "DCE", 10, 1, 0.12, 0.12)
    subscribed: list[tuple[str, str]] = []

    class FakeBroker:
        def __init__(self, credentials):
            self.credentials = credentials

        def seed_trade_identities(self, identities):
            return None

        def configure_order_submission_journal(self, path, **identity):
            del path, identity

        def get_account_identity_digest(self):
            return "a" * 64

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
            return AccountSnapshot(500_000, 500_000, 500_000, 0, 0, 0, "20260825")

        def get_contract_catalog(self):
            return catalog

        def get_trading_day(self):
            return "20260825"

        def get_live_contract_specs(self, symbols, timeout_seconds):
            assert symbols == ["m2609"]
            return {"m2609": spec}

        def get_positions(self):
            return []

        def get_active_orders(self):
            return []

        def subscribe(self, symbol, exchange):
            subscribed.append((symbol, exchange))

        def poll_events(self):
            return [
                BrokerEvent(
                    "tick",
                    Tick(
                        "m2609",
                        "DCE",
                        datetime(2026, 8, 25, tzinfo=timezone.utc),
                        2999,
                        3001,
                        3000,
                        10,
                        10,
                        "20260825",
                    ),
                )
            ]

        def send_order(self, request):
            raise AssertionError("doctor must never submit an order")

    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", FakeBroker)
    state_path = tmp_path / "state.json"
    DirectionalActivityStore(tmp_path / "directional_activity.json").save(
        DirectionalActivitySnapshot(
            "20260824",
            {
                "m2609": ContractActivity(
                    "m2609",
                    "DCE",
                    "M",
                    "20260824",
                    10_000,
                    20_000,
                    datetime(2026, 8, 24, tzinfo=timezone.utc),
                )
            },
        )
    )
    config = SimpleNamespace(
        mode="live",
        ctp=SimpleNamespace(environment="test"),
        contracts={},
        auto=AutoConfig(),
        directional=DirectionalConfig(
            enabled=True,
            policy="stress90",
            products=("M",),
            exchanges=("DCE",),
        ),
        risk=RiskConfig(max_margin_ratio=0.35, min_available_ratio=0.25),
        metadata_timeout_seconds=1.0,
        state_path=str(state_path),
        log_path=str(tmp_path / "afuture.log"),
        report_path=str(tmp_path / "report.json"),
        journal_path=str(tmp_path / "audit.jsonl"),
        alert_path=str(tmp_path / "alerts.jsonl"),
    )
    args = SimpleNamespace(
        confirm_live=False,
        startup_timeout=0.1,
        snapshot_wait=0.05,
        metadata_limit=0,
    )

    assert _run_doctor(config, args) == 2
    assert subscribed == [("m2609", "DCE")]


def test_stress90_shadow_uses_dedicated_persistent_runtime_directory(tmp_path: Path):
    stress = SimpleNamespace(
        state_path=str(tmp_path / "state.json"),
        directional=SimpleNamespace(policy="stress90"),
    )
    legacy = SimpleNamespace(
        state_path=str(tmp_path / "state.json"),
        directional=SimpleNamespace(policy="execution_aligned"),
    )

    stress_paths = _shadow_runtime_paths(stress)
    legacy_paths = _shadow_runtime_paths(legacy)

    assert stress_paths["state"] == tmp_path / "shadow" / "state.json"
    assert stress_paths["broker_state"] == tmp_path / "shadow" / "shadow_broker_state.json"
    assert stress_paths["journal"] == tmp_path / "shadow" / "audit.jsonl"
    assert stress_paths["persistent"] is True
    assert legacy_paths["state"] == tmp_path / "shadow_state.json"
    assert legacy_paths["broker_state"] is None
    assert legacy_paths["persistent"] is False


def test_run_shadow_passes_durable_broker_state_on_every_stress90_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import logging

    from afuture.cli import _run_shadow

    broker_state_paths: list[Path | None] = []

    class FakeLiveBroker:
        def __init__(self, credentials):
            self.credentials = credentials

    class FakeShadowBroker:
        def __init__(self, live, initial_capital, **kwargs):
            del live, initial_capital
            broker_state_paths.append(kwargs.get("state_path"))

        def update_specs(self, specs):
            del specs

        def is_ready(self):
            return True

        def snapshot_marker(self):
            return (0, 0)

        def snapshot_ready(self, marker):
            return marker == (0, 0)

        def get_active_orders(self):
            return []

    class FakeEngine:
        halted = False
        state = SimpleNamespace(kill_reason="")

        def start(self):
            return None

        def initialize_after_ready(self):
            return None

        def reconcile_startup(self):
            return True

        def run_once(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", FakeLiveBroker)
    monkeypatch.setattr("afuture.broker.shadow.ShadowBroker", FakeShadowBroker)
    monkeypatch.setattr("afuture.cli._build_cli_engine", lambda *args, **kwargs: FakeEngine())
    config = SimpleNamespace(
        mode="live",
        ctp=SimpleNamespace(environment="test"),
        initial_capital=500_000,
        slippage_ticks=1,
        latency_ticks=1,
        market_impact_ticks=1,
        metadata_timeout_seconds=0.0,
        contracts={},
        auto=SimpleNamespace(enabled=False),
        directional=SimpleNamespace(
            enabled=True,
            policy="stress90",
            account_exclusive=False,
        ),
        state_path=str(tmp_path / "state.json"),
        alert_path=str(tmp_path / "alerts.jsonl"),
        alert_webhook="",
    )
    args = SimpleNamespace(
        confirm_live=False,
        duration_seconds=1e-9,
        startup_timeout=0.0,
        snapshot_wait=0.0,
    )

    for _ in range(2):
        assert _run_shadow(config, args, logging.getLogger("test-shadow")) == 0

    assert broker_state_paths == [
        tmp_path / "shadow" / "shadow_broker_state.json",
        tmp_path / "shadow" / "shadow_broker_state.json",
    ]


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


@pytest.mark.parametrize(
    "foreign_first",
    [True, False],
    ids=["foreign-before-configured", "foreign-after-configured"],
)
def test_recovery_rejects_same_symbol_on_unconfigured_exchange_for_either_broker_order(
    foreign_first: bool,
) -> None:
    pair = PairConfig("m_pair", "m2609", "m2701", "DCE", 3)
    configured = [
        ContractPosition("m2609", "DCE", long_today=1),
        ContractPosition("m2701", "DCE", short_today=1),
    ]
    foreign = ContractPosition("m2609", "SHFE", long_today=7)
    positions = [foreign, *configured] if foreign_first else [*configured, foreign]

    with pytest.raises(RuntimeError, match="not configured"):
        validate_recovery_positions([pair], positions)


def test_recovery_rejects_duplicate_symbol_exchange_position_rows() -> None:
    pair = PairConfig("m_pair", "m2609", "m2701", "DCE", 3)
    positions = [
        ContractPosition("m2609", "DCE", long_today=1),
        ContractPosition("m2609", "DCE", long_today=1),
        ContractPosition("m2701", "DCE", short_today=1),
    ]

    with pytest.raises(RuntimeError, match="duplicate"):
        validate_recovery_positions([pair], positions)


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


def test_recover_state_seeds_fresh_ctp_before_inclusive_snapshot_replay_and_adopts_one_lot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trading_day = "20260825"
    pair = PairConfig("m_pair", "m2609", "m2701", "DCE", 3)
    store = StateStore(tmp_path / "state.json")
    store.save(
        RuntimeState(
            kill_switch=True,
            trading_day=trading_day,
            positions=[
                ContractPosition("m2609", "DCE", long_today=1, long_price=3000.0).__dict__,
                ContractPosition("m2701", "DCE", short_today=1, short_price=2990.0).__dict__,
            ],
            recent_trade_ids=[
                f"{trading_day}:DCE:CTP.NEAR-T1",
                f"{trading_day}:DCE:CTP.FAR-T1",
            ],
        )
    )
    broker = CtpBroker(CtpCredentials("user", "secret", "9999", "td", "md", "app", "auth", "test"))
    broker._last_account = AccountSnapshot(
        500_000,
        500_000,
        500_000,
        0,
        0,
        0,
        trading_day,
    )

    def start_with_inclusive_snapshot_and_replays() -> None:
        broker._trading_day = trading_day
        broker._positions = {
            ("m2609", "DCE"): ContractPosition("m2609", "DCE", long_today=1, long_price=3000.0),
            ("m2701", "DCE"): ContractPosition("m2701", "DCE", short_today=1, short_price=2990.0),
        }
        for trade_id, symbol, direction, price in (
            ("CTP.NEAR-T1", "m2609", "LONG", 3000.0),
            ("CTP.FAR-T1", "m2701", "SHORT", 2990.0),
        ):
            broker._on_trade(
                SimpleNamespace(
                    data=SimpleNamespace(
                        vt_tradeid=trade_id,
                        vt_orderid=f"CTP.{symbol}",
                        symbol=symbol,
                        exchange=SimpleNamespace(value="DCE"),
                        direction=SimpleNamespace(name=direction),
                        offset=SimpleNamespace(name="OPEN"),
                        volume=1,
                        price=price,
                        datetime=datetime(2026, 8, 25, 1, tzinfo=timezone.utc),
                    )
                )
            )

    broker.start = start_with_inclusive_snapshot_and_replays
    broker.is_ready = lambda: True
    broker.snapshot_marker = lambda: (0, 0)
    broker.snapshot_ready = lambda _marker: True
    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", lambda _credentials: broker)
    monkeypatch.setenv("AFUTURE_RECOVERY_ACK", "I_VERIFIED_CTP_POSITIONS")
    config = SimpleNamespace(
        mode="live",
        ctp=broker.credentials,
        state_path=str(store.path),
        pairs=[pair],
        require_live_metadata=False,
        contracts={},
        metadata_timeout_seconds=1.0,
        journal_path=str(tmp_path / "audit.jsonl"),
        report_path=str(tmp_path / "report.json"),
    )
    args = SimpleNamespace(
        confirm_live=False,
        confirm_adopt_state=True,
        startup_timeout=0.1,
        snapshot_wait=0.1,
    )

    result = _recover_state(config, args, SimpleNamespace(warning=lambda *_args: None))

    saved_positions = sorted(
        store.positions_from_state(store.load()), key=lambda position: position.symbol
    )
    assert result == 0
    assert [
        (position.symbol, position.long_total, position.short_total) for position in saved_positions
    ] == [
        ("m2609", 1, 0),
        ("m2701", 0, 1),
    ]
    assert broker.delivery_counters()["critical_enqueued"] == 0


def test_recover_state_refuses_ambiguous_legacy_identity_before_ctp_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state.json")
    store.save(
        RuntimeState(
            kill_switch=True,
            trading_day="20260825",
            recent_trade_ids=["20260825:LEGACY-T1"],
        )
    )

    class NeverStartedBroker:
        def get_account_identity_digest(self):
            return "a" * 64

        def seed_trade_identities(self, _identities):
            raise AssertionError("ambiguous legacy identities must not be seeded")

        def start(self):
            raise AssertionError("ambiguous legacy recovery must fail before CTP start")

    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", lambda _credentials: NeverStartedBroker())
    monkeypatch.setenv("AFUTURE_RECOVERY_ACK", "I_VERIFIED_CTP_POSITIONS")
    config = SimpleNamespace(
        mode="live",
        ctp=SimpleNamespace(environment="test"),
        state_path=str(store.path),
    )
    args = SimpleNamespace(
        confirm_live=False,
        confirm_adopt_state=True,
        startup_timeout=0.1,
        snapshot_wait=0.1,
    )

    with pytest.raises(RuntimeError, match="ambiguous legacy trade identities"):
        _recover_state(config, args, SimpleNamespace(warning=lambda *_args: None))


def test_recover_state_rejects_pending_lifecycle_before_reading_mutable_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PendingBarrier(RuntimeError):
        pass

    class Broker:
        started = False

        def get_account_identity_digest(self):
            return "a" * 64

        def start(self):
            type(self).started = True

        def stop(self):
            return None

    def reject_pending(runtime_dir: Path) -> None:
        assert runtime_dir == tmp_path
        raise PendingBarrier("prepared lifecycle blocks recovery")

    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", lambda _credentials: Broker())
    monkeypatch.setattr(
        "afuture.stress90_lifecycle_transaction.require_no_pending_stress90_lifecycle_transaction",
        reject_pending,
    )
    monkeypatch.setenv("AFUTURE_RECOVERY_ACK", "I_VERIFIED_CTP_POSITIONS")
    config = SimpleNamespace(
        mode="live",
        ctp=SimpleNamespace(environment="test"),
        state_path=str(tmp_path / "state.json"),
    )
    args = SimpleNamespace(
        confirm_live=False,
        confirm_adopt_state=True,
        startup_timeout=0.1,
        snapshot_wait=0.1,
    )

    with pytest.raises(PendingBarrier, match="blocks recovery"):
        _recover_state(config, args, SimpleNamespace(warning=lambda *_args: None))
    assert Broker.started is False


def test_recover_state_is_not_an_account_path_authority_for_stress90(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NeverConstructedBroker:
        def __init__(self, _credentials):
            raise AssertionError("Stress-90 legacy recovery must fail before CTP construction")

    monkeypatch.setattr("afuture.broker.ctp.CtpBroker", NeverConstructedBroker)
    monkeypatch.setenv("AFUTURE_RECOVERY_ACK", "I_VERIFIED_CTP_POSITIONS")
    config = SimpleNamespace(
        mode="live",
        ctp=SimpleNamespace(environment="test"),
        state_path=str(tmp_path / "state.json"),
        directional=SimpleNamespace(enabled=True, policy="stress90"),
    )
    args = SimpleNamespace(
        confirm_live=False,
        confirm_adopt_state=True,
        startup_timeout=0.1,
        snapshot_wait=0.1,
    )

    with pytest.raises(RuntimeError, match="not authorized for Stress-90"):
        _recover_state(config, args, SimpleNamespace(warning=lambda *_args: None))


def test_ctp_trading_day_checkpoint_never_persists_an_account_day_mismatch(
    tmp_path: Path,
) -> None:
    from afuture.cli import _checkpoint_ctp_trading_day

    class Broker:
        def get_account(self):
            return AccountSnapshot(500_000, 500_000, 500_000, 0, 0, 0, "20260824")

        def get_account_identity_digest(self):
            return "a" * 64

    config = SimpleNamespace(state_path=str(tmp_path / "state.json"))

    with pytest.raises(RuntimeError, match="account/CTP trading day mismatch"):
        _checkpoint_ctp_trading_day(config, Broker(), "20260825")

    assert not (tmp_path / "ctp_trading_day_evidence.json").exists()


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


def test_stress90_startup_waits_for_owned_orders_without_cancelling():
    from afuture.cli import wait_for_stress90_startup_orders
    from afuture.models import Offset, Order, OrderRequest, OrderSide, OrderStatus

    active = Order(
        "CTP.1_2_3",
        OrderRequest("A2612", "DCE", OrderSide.BUY, Offset.OPEN, 1, 100.0),
        OrderStatus.NOT_TRADED,
    )

    class Broker:
        def __init__(self):
            self.active = [active]

        def get_active_orders(self):
            return list(self.active)

        def owns_order(self, order_id):
            return order_id == active.order_id

        def cancel_order(self, _order_id):
            raise AssertionError("owned Stress-90 startup order must not be cancelled")

    class Engine:
        halted = False
        state = SimpleNamespace(kill_reason="")

        def __init__(self, broker):
            self.broker = broker
            self.calls = 0

        def run_once(self):
            self.calls += 1
            self.broker.active.clear()

    broker = Broker()
    engine = Engine(broker)
    wait_for_stress90_startup_orders(engine, broker, timeout_seconds=0.1, poll_interval=0.001)
    assert engine.calls == 1


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


def test_lifecycle_commit_linearizes_precheck_registry_and_all_state_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import contextmanager

    from afuture import stress90_lifecycle_transaction as lifecycle_module
    from afuture.cli import _commit_stress90_lifecycle_under_broker_fence

    events = []
    transaction = object()

    class Broker:
        @contextmanager
        def lifecycle_state_commit_fence(self):
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

    def fake_apply(*_args, precommit_check, **_kwargs):
        events.append("policy+generic")
        precommit_check()
        events.append("coordinator")
        return "committed"

    monkeypatch.setattr(
        lifecycle_module,
        "apply_stress90_lifecycle_transaction",
        fake_apply,
    )

    result = _commit_stress90_lifecycle_under_broker_fence(
        Broker(),
        transaction_store=object(),
        generic_store=object(),
        policy_store=object(),
        prepare_transaction=lambda: events.append("prepare") or transaction,
        apply_registry_transition=lambda current: events.append(
            "registry" if current is transaction else "wrong"
        ),
        precommit_check=lambda: events.append("precheck"),
    )

    assert result == "committed"
    assert events == [
        "enter",
        "precheck",
        "prepare",
        "registry",
        "policy+generic",
        "precheck",
        "coordinator",
        "exit",
    ]


def test_lifecycle_commit_rejects_broker_without_ingress_fence() -> None:
    from afuture.cli import _commit_stress90_lifecycle_under_broker_fence

    with pytest.raises(RuntimeError, match="commit fence"):
        _commit_stress90_lifecycle_under_broker_fence(
            object(),
            transaction_store=object(),
            generic_store=object(),
            policy_store=object(),
            prepare_transaction=lambda: (_ for _ in ()).throw(
                AssertionError("must not prepare without a Broker fence")
            ),
            apply_registry_transition=lambda _transaction: None,
            precommit_check=lambda: None,
        )


def test_prepared_lifecycle_cannot_erase_recovered_net_zero_fill_identity() -> None:
    from afuture.cli import _require_no_unpersisted_lifecycle_crash_fill_adoption

    persisted = RuntimeState(
        kill_switch=True,
        runtime_mode="HALTED",
        positions=[],
        recent_trade_ids=[],
    )
    adopted = RuntimeState(
        **{
            **vars(persisted),
            # A buy+sell crash window can be net-flat while the durable identity
            # remains essential for duplicate/unknown-trade detection.
            "recent_trade_ids": ["20260825:DCE:T-RECOVERED"],
        }
    )

    with pytest.raises(RuntimeError, match="durable HALTED recovery checkpoint"):
        _require_no_unpersisted_lifecycle_crash_fill_adoption(
            persisted,
            adopted,
            pending_lifecycle=SimpleNamespace(status="prepared"),
        )

    assert persisted.recent_trade_ids == []
    assert adopted.recent_trade_ids == ["20260825:DCE:T-RECOVERED"]
