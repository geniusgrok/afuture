from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from afuture.session_preflight import (
    PreflightBlocked,
    ReadOnlyBroker,
    SessionPreflightRunner,
)


class FakeBroker:
    def __init__(self) -> None:
        self.orders_sent = 0
        self.cancels_sent = 0

    def send_order(self, _request):
        self.orders_sent += 1
        return "unexpected"

    def cancel_order(self, _order_id):
        self.cancels_sent += 1


class FakeBackend:
    def __init__(self, *, fail: str = "", continuity: bool = False, rebase: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail
        self.continuity_required = continuity
        self.rebase_required = rebase
        self.broker = FakeBroker()

    def _call(self, name: str):
        self.calls.append(name)
        if self.fail == name:
            raise PreflightBlocked(name, f"{name} failed", f"manual-{name}")

    def validate_config(self):
        self._call("validate_config")
        return {"valid": True}

    def verify_deployment(self):
        self._call("verify_deployment")
        return {"passed": True, "deployment_identity_digest": "a" * 64}

    def local_status(self):
        self._call("local_status")
        return {"passed": True, "facts": {"stress90": {}}}

    def runtime_integrity(self):
        self._call("runtime_integrity")
        return {"passed": True}

    def open_broker(self):
        self._call("open_broker")
        return ReadOnlyBroker(self.broker)

    def trading_day(self, _broker):
        self._call("trading_day")
        return "20260828"

    def fresh_account(self, _broker, _trading_day):
        self._call("fresh_account")
        return {"fresh": True, "trading_day": "20260828"}

    def positions(self, _broker):
        self._call("positions")
        return []

    def active_orders(self, _broker):
        self._call("active_orders")
        return []

    def catalog(self, _broker):
        self._call("catalog")
        return [{"symbol": "IF2609", "product": "IF"}]

    def market_facts(self, _broker, _trading_day, _positions, _catalog):
        self._call("market_facts")
        return {
            "metadata_fresh": True,
            "quotes_fresh": True,
            "margin_fresh": True,
            "commission_fresh": True,
        }

    def refresh_ohlc(self, _trading_day):
        self._call("refresh_ohlc")
        return {"refreshed": True}

    def alignment(self, _trading_day):
        self._call("alignment")
        return {
            "activity": "aligned",
            "ohlc": "aligned",
            "oi": "aligned",
            "target_day": "aligned",
        }

    def continuity(self, _trading_day):
        self._call("continuity")
        return {
            "mode": "operator_managed",
            "roll_forward_required": self.continuity_required,
            "rebase_required": self.rebase_required,
        }

    def doctor_p0(self, _broker, _trading_day, _positions, _catalog, _market):
        self._call("doctor_p0")
        return {"passed": True, "checks": []}

    def capacity(self, _doctor, _trading_day):
        self._call("capacity")
        return {
            "hard_safety_passed": True,
            "warnings": ["integer representation is coarse"],
            "raw_lots": {"IF": 2},
            "scaled_lots": {"IF": 1},
            "final_lots": {"IF": 1},
            "risk_overlay": {"scale": 0.5},
        }

    def permit_status(self):
        self._call("permit_status")
        return {"status": "missing", "issued": False}

    def close_broker(self, _broker):
        self._call("close_broker")


EXPECTED_ORDER = [
    "validate_config",
    "verify_deployment",
    "local_status",
    "runtime_integrity",
    "open_broker",
    "trading_day",
    "fresh_account",
    "positions",
    "active_orders",
    "catalog",
    "market_facts",
    "alignment",
    "continuity",
    "doctor_p0",
    "capacity",
    "permit_status",
    "close_broker",
]


def _runner(
    backend: FakeBackend, tmp_path: Path, *, refresh: bool = False
) -> SessionPreflightRunner:
    return SessionPreflightRunner(
        config=SimpleNamespace(
            directional=SimpleNamespace(
                policy="stress90", account_continuity_mode="operator_managed"
            )
        ),
        config_path=tmp_path / "config.toml",
        output_path=tmp_path / "preflight.json",
        confirm_live=True,
        refresh_ohlc=refresh,
        shadow_account=False,
        backend=backend,
    )


def test_prepare_session_normal_path_is_zero_write_and_canonical(tmp_path: Path) -> None:
    backend = FakeBackend()
    code, payload = _runner(backend, tmp_path).run()
    assert code == 0
    assert backend.calls == EXPECTED_ORDER
    assert backend.broker.orders_sent == 0
    assert backend.broker.cancels_sent == 0
    assert payload["orders_sent"] == 0
    assert payload["cancels_sent"] == 0
    assert payload["permit"]["issued"] is False
    assert payload["entered_running"] is False
    assert payload["ctp_trading_day"] == "20260828"
    assert payload["raw_lots"] == {"IF": 2}
    assert payload["scaled_lots"] == {"IF": 1}
    assert payload["final_lots"] == {"IF": 1}
    assert (tmp_path / "preflight.json").exists()


@pytest.mark.parametrize(
    "failure",
    [
        "verify_deployment",
        "local_status",
        "runtime_integrity",
        "market_facts",
        "alignment",
        "doctor_p0",
    ],
)
def test_prepare_session_safety_failures_return_two_and_named_next_action(
    tmp_path: Path,
    failure: str,
) -> None:
    backend = FakeBackend(fail=failure)
    code, payload = _runner(backend, tmp_path).run()
    assert code == 2
    assert payload["passed"] is False
    assert payload["failed_check"] == failure
    assert payload["allowed_next_actions"] == [f"manual-{failure}"]
    assert backend.broker.orders_sent == 0
    assert backend.broker.cancels_sent == 0


def test_read_only_broker_blocks_send_and_cancel() -> None:
    source = FakeBroker()
    broker = ReadOnlyBroker(source)
    with pytest.raises(RuntimeError, match="read-only"):
        broker.send_order(object())
    with pytest.raises(RuntimeError, match="read-only"):
        broker.cancel_order("x")
    assert source.orders_sent == 0
    assert source.cancels_sent == 0
