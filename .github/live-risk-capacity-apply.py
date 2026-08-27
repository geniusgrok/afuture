from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def p(path: str) -> Path:
    return ROOT / path


def read(path: str) -> str:
    return p(path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = p(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected exactly one anchor, got {count}: {old[:100]!r}")
    write(path, text.replace(old, new, 1))


def replace_all(path: str, old: str, new: str, *, minimum: int = 1) -> None:
    text = read(path)
    count = text.count(old)
    if count < minimum:
        raise RuntimeError(f"{path}: expected >= {minimum} anchors, got {count}: {old[:100]!r}")
    write(path, text.replace(old, new))


def append_once(path: str, marker: str, content: str) -> None:
    text = read(path)
    if marker in text:
        return
    write(path, text.rstrip() + "\n\n" + content.strip() + "\n")


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=check,
    )


# ---------------------------------------------------------------------------
# TDD RED: install the acceptance-facing tests first and prove main does not
# already implement the requested surface.
# ---------------------------------------------------------------------------
TESTS = r'''
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from afuture.directional import DirectionalConfig
from afuture.models import AccountSnapshot, ContractSpec, FeeSpec, Tick
from afuture.risk import RiskConfig

_CHINA = ZoneInfo("Asia/Shanghai")


def _account(equity: float = 100_000.0) -> AccountSnapshot:
    return AccountSnapshot(
        balance=equity,
        equity=equity,
        available=equity,
        margin=0.0,
        realized_pnl=0.0,
        unrealized_pnl=0.0,
        trading_day="20260827",
    )


def _tick(symbol: str = "A2612", price: float = 1_000.0) -> Tick:
    return Tick(
        symbol=symbol,
        exchange="DCE",
        timestamp=datetime(2026, 8, 27, 21, 1, tzinfo=_CHINA),
        bid_price=price - 0.5,
        ask_price=price + 0.5,
        last_price=price,
        bid_volume=1000,
        ask_volume=1000,
        trading_day="20260827",
        volume=10000,
        open_interest=20000,
    )


def _spec() -> ContractSpec:
    return ContractSpec(
        "A2612",
        "DCE",
        10.0,
        1.0,
        0.10,
        0.12,
        FeeSpec(open_fixed=2.0, close_fixed=2.0, close_today_fixed=3.0),
    )


def test_live_risk_scale_default_and_validation():
    assert DirectionalConfig().live_risk_scale == 1.0
    for value in (0.0, -0.1, float("nan"), float("inf"), 1.000001):
        with pytest.raises(ValueError):
            DirectionalConfig(live_risk_scale=value).validate()
    DirectionalConfig(live_risk_scale=0.05).validate()


def test_scale_helper_is_monotone_and_direction_preserving():
    from afuture.stress90_risk_overlay import scale_stress90_product_weights

    raw = {"A": 0.8, "M": -0.5, "I": 0.0}
    scaled = scale_stress90_product_weights(raw, 0.05)
    assert scaled == {"A": pytest.approx(0.04), "M": pytest.approx(-0.025), "I": 0.0}
    for product, value in raw.items():
        assert abs(scaled[product]) <= abs(value)
        if value:
            assert (scaled[product] > 0) == (value > 0)


def test_stress90_scale_is_before_integer_and_margin_fit_and_can_zero_targets():
    from afuture.directional_stress90_planner import build_stress90_rebalance_stages

    common = dict(
        account=_account(),
        product_weights={"A": 1.0},
        product_ticks={"A": _tick()},
        specs={"A2612": _spec()},
        current_lots={},
        symbol_products={"A2612": "A"},
        max_contract_volume=35,
        max_gross_leverage=2.0,
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
        max_daily_loss_ratio=0.05,
        margin_estimate_buffer=1.25,
        completed_returns=(),
        drawdown_reserve_freeze=False,
        concentration_freeze=False,
    )
    baseline = build_stress90_rebalance_stages(**common)
    scaled = build_stress90_rebalance_stages(**common, live_risk_scale=0.10)
    tiny = build_stress90_rebalance_stages(**common, live_risk_scale=0.001)
    assert baseline.raw_integer_lots == baseline.scaled_integer_lots
    assert baseline.margin_fitted_lots == {"A2612": 10}
    assert scaled.raw_integer_lots == {"A2612": 10}
    assert scaled.scaled_integer_lots == {"A2612": 1}
    assert scaled.margin_fitted_lots == {"A2612": 1}
    assert tiny.scaled_integer_lots == {}
    assert tiny.final_frozen_lots == {}


def test_scale_never_suppresses_reduction_or_reversal_close():
    from afuture.directional_stress90_planner import build_stress90_rebalance_stages

    stages = build_stress90_rebalance_stages(
        account=_account(),
        product_weights={"A": -1.0},
        product_ticks={"A": _tick()},
        specs={"A2612": _spec()},
        current_lots={"A2612": 5},
        symbol_products={"A2612": "A"},
        max_contract_volume=35,
        max_gross_leverage=2.0,
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
        max_daily_loss_ratio=0.05,
        margin_estimate_buffer=1.25,
        completed_returns=(),
        drawdown_reserve_freeze=False,
        concentration_freeze=False,
        live_risk_scale=0.001,
    )
    assert stages.reductions == {"A2612": -5}
    assert stages.openings == {}


def test_risk_overlay_digest_is_stable_sensitive_and_separate_from_policy_digest():
    from afuture.directional_stress90_policy import STRESS90_POLICY
    from afuture.stress90_risk_overlay import stress90_risk_overlay_digest

    directional = DirectionalConfig(enabled=True, policy="stress90", products=("A",))
    risk = RiskConfig()
    first = stress90_risk_overlay_digest(directional, risk)
    assert first == stress90_risk_overlay_digest(directional, risk)
    assert first != stress90_risk_overlay_digest(replace(directional, live_risk_scale=0.5), risk)
    assert first != stress90_risk_overlay_digest(
        directional, replace(risk, max_orders_per_minute=risk.max_orders_per_minute + 1)
    )
    assert first != STRESS90_POLICY.policy_definition_digest


def test_policy_identity_requires_matching_risk_overlay_without_changing_policy_digest():
    from afuture.directional_policy_activation import (
        POLICY_IDENTITY_STATE_KEY,
        require_directional_policy_identity,
    )
    from afuture.directional_stress90_policy import STRESS90_POLICY
    from afuture.state import RuntimeState

    marker = {
        "policy_id": STRESS90_POLICY.policy_id,
        "policy_definition_digest": STRESS90_POLICY.policy_definition_digest,
        "products_manifest_digest": STRESS90_POLICY.products_manifest_digest,
        "bootstrap_seed_digest": "a" * 64,
        "account_identity_digest": "b" * 64,
        "risk_overlay_digest": "c" * 64,
        "operator_reason": "test",
    }
    state = RuntimeState(strategy_states={POLICY_IDENTITY_STATE_KEY: marker})
    require_directional_policy_identity(
        state,
        policy_id=STRESS90_POLICY.policy_id,
        policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
        products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
        bootstrap_seed_digest="a" * 64,
        account_identity_digest="b" * 64,
        risk_overlay_digest="c" * 64,
    )
    with pytest.raises(RuntimeError, match="risk overlay"):
        require_directional_policy_identity(
            state,
            policy_id=STRESS90_POLICY.policy_id,
            policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
            products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
            bootstrap_seed_digest="a" * 64,
            account_identity_digest="b" * 64,
            risk_overlay_digest="d" * 64,
        )


def test_execution_intent_binds_risk_overlay_and_same_day_cannot_be_reinterpreted(tmp_path):
    from afuture.directional_stress90_execution import (
        Stress90ExecutionIntentIntegrityError,
        Stress90ExecutionIntentStore,
        prepare_stress90_execution_intent,
    )

    store = Stress90ExecutionIntentStore(tmp_path / "intent.json")
    kwargs = dict(
        target_trading_day="20260827",
        daily_decision_digest="1" * 64,
        account_identity_digest="2" * 64,
        account_epoch="3" * 64,
        current_lots={},
        margin_fitted_lots={"A2612": 1},
        freeze_authorized_lots={"A2612": 1},
        symbol_products={"A2612": "A"},
        lot_notionals={"A2612": 10_000.0},
    )
    intent = prepare_stress90_execution_intent(
        store, risk_overlay_digest="4" * 64, **kwargs
    )
    assert intent.risk_overlay_digest == "4" * 64
    with pytest.raises(Stress90ExecutionIntentIntegrityError, match="risk overlay"):
        prepare_stress90_execution_intent(store, risk_overlay_digest="5" * 64, **kwargs)


def test_cost_preview_can_require_real_commission_evidence():
    from afuture.operations import estimate_stress90_contract_cost

    with pytest.raises(ValueError, match="commission evidence"):
        estimate_stress90_contract_cost(
            _tick(),
            replace(_spec(), fee=FeeSpec()),
            require_commission_evidence=True,
        )
    row = estimate_stress90_contract_cost(
        _tick(), _spec(), require_commission_evidence=True
    )
    assert row["historical_hurdle_bps"] == 15.0
    assert row["minimum_reasonable_one_way_bps"] > 0.0


def test_capacity_payload_is_read_only_projection_of_doctor_facts():
    from afuture.operations import OperationalReport
    from afuture.stress90_capacity import build_stress90_capacity_payload

    report = OperationalReport()
    report.add("stress90_risk_overlay_identity", True, "ok")
    report.facts["stress90"] = {
        "risk_overlay_digest": "a" * 64,
        "configured_live_risk_scale": 0.05,
        "raw_product_weights": {"A": 1.0},
        "scaled_product_weights": {"A": 0.05},
        "raw_target_gross": 1.0,
        "scaled_target_gross": 0.05,
        "selected_contracts": {"A": "A2612"},
        "contract_capacity": {"A": {"symbol": "A2612"}},
        "integer_target_stages": {
            "raw_integer_lots": {"A2612": 10},
            "scaled_integer_lots": {},
            "margin_fitted_lots": {},
            "drawdown_frozen_lots": {},
            "hhi_frozen_lots": {},
            "final_frozen_lots": {},
            "reductions": {},
            "openings": {},
        },
        "nonzero_product_count": 0,
        "largest_product_absolute_gross_share": 0.0,
        "product_hhi": 0.0,
        "integer_tracking_error": 0.05,
        "estimated_margin_ratio": 0.0,
        "estimated_available_ratio": 1.0,
        "contract_cap_hits": [],
        "clipped_products": {"integer": ["A"]},
        "live_costs": {"A2612": {"historical_15bp_compatible": True}},
        "risk_manager_preview": {"allowed": True, "reason": ""},
        "execution_chain_commissioning_only": True,
        "portfolio_representation_warning": "scaled target rounds to zero",
    }
    payload = build_stress90_capacity_payload(
        report,
        account_identity_digest="b" * 64,
        ctp_trading_day="20260827",
    )
    assert payload["orders_sent"] == 0
    assert payload["cancels_sent"] == 0
    assert payload["risk_overlay_digest"] == "a" * 64
    assert payload["raw_lots"] == {"A2612": 10}
    assert payload["scaled_lots"] == {}


def test_capacity_command_is_present_and_has_required_arguments():
    from afuture.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "stress90-capacity-report",
            "--config",
            "x.toml",
            "--confirm-live",
            "--output",
            "out.json",
        ]
    )
    assert args.command == "stress90-capacity-report"
    assert args.confirm_live is True
    assert args.output == "out.json"
'''

write("tests/test_stress90_live_risk_capacity.py", textwrap.dedent(TESTS).lstrip())
red = run("python", "-m", "pytest", "-q", "tests/test_stress90_live_risk_capacity.py", check=False)
if red.returncode == 0:
    raise RuntimeError("TDD red phase unexpectedly passed before implementation")
print("TDD RED confirmed:\n" + red.stdout[-4000:])


# ---------------------------------------------------------------------------
# Core risk-overlay identity and scale primitives.
# ---------------------------------------------------------------------------
RISK_OVERLAY = r'''
"""Canonical Stress-90 production risk overlay identity and monotone scaling."""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from math import isfinite

_KIND = "afuture.stress90.risk-overlay"
_SCHEMA_VERSION = 1


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _finite_scale(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("directional.live_risk_scale must be finite and in (0, 1]")
    scale = float(value)
    if not isfinite(scale) or not 0.0 < scale <= 1.0:
        raise ValueError("directional.live_risk_scale must be finite and in (0, 1]")
    return scale


def scale_stress90_product_weights(
    weights: Mapping[str, float], live_risk_scale: float
) -> dict[str, float]:
    """Scale production targets before integer sizing without creating or flipping risk."""

    scale = _finite_scale(live_risk_scale)
    result: dict[str, float] = {}
    for raw_product, raw_value in weights.items():
        product = str(raw_product).upper()
        value = float(raw_value)
        if not product or not isfinite(value):
            raise ValueError("Stress-90 product weights must be finite")
        scaled = value * scale
        if abs(scaled) > abs(value) + 1e-15:
            raise RuntimeError("Stress-90 live scale increased absolute target risk")
        if value and scaled and (scaled > 0.0) != (value > 0.0):
            raise RuntimeError("Stress-90 live scale changed target direction")
        result[product] = scaled
    return result


def stress90_risk_overlay_payload(directional, risk) -> dict[str, object]:
    """Return the complete canonical configuration envelope that can change live risk."""

    scale = _finite_scale(getattr(directional, "live_risk_scale", 1.0))
    directional_fields = (
        "max_gross_leverage",
        "max_contract_volume",
        "min_days_to_expiry",
        "min_volume",
        "min_open_interest",
        "rebalance_window",
        "signal_max_age_hours",
        "account_exclusive",
        "account_continuity_mode",
    )
    risk_fields = tuple(getattr(risk, "__dataclass_fields__", {}).keys())
    if not risk_fields:
        risk_fields = (
            "max_margin_ratio",
            "min_available_ratio",
            "max_daily_loss_ratio",
            "max_total_drawdown_ratio",
            "max_contract_volume",
            "margin_estimate_buffer",
            "max_orders_per_minute",
            "min_depth_multiple",
            "max_bid_ask_ticks",
            "limit_distance_ticks",
            "max_quote_age_seconds",
            "max_leg_skew_seconds",
            "expiry_blackout_days",
            "open_cooldown_minutes",
            "close_blackout_minutes",
        )
    directional_payload = {name: getattr(directional, name) for name in directional_fields}
    directional_payload["live_risk_scale"] = scale
    risk_payload = {name: getattr(risk, name) for name in risk_fields}
    payload = {
        "kind": _KIND,
        "schema_version": _SCHEMA_VERSION,
        "directional": directional_payload,
        "risk": risk_payload,
    }
    # Canonical encoding is also the final type/finite-value guard.
    try:
        _canonical(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("Stress-90 risk overlay is not canonical finite JSON") from exc
    return payload


def stress90_risk_overlay_digest(directional, risk) -> str:
    return sha256(_canonical(stress90_risk_overlay_payload(directional, risk))).hexdigest()
'''
write("afuture/stress90_risk_overlay.py", textwrap.dedent(RISK_OVERLAY).lstrip())

CAPACITY = r'''
"""Read-only projection of the shared Stress-90 Doctor mechanics into capacity diagnostics."""

from __future__ import annotations

from collections.abc import Mapping


def build_stress90_capacity_payload(
    report,
    *,
    account_identity_digest: str,
    ctp_trading_day: str,
) -> dict[str, object]:
    facts = getattr(report, "facts", None)
    stress = facts.get("stress90") if isinstance(facts, Mapping) else None
    if not isinstance(stress, Mapping):
        raise RuntimeError("Stress-90 capacity report requires Doctor Stress-90 facts")
    stages = stress.get("integer_target_stages")
    if not isinstance(stages, Mapping):
        raise RuntimeError("Stress-90 capacity report requires shared integer planner facts")
    costs = stress.get("live_costs")
    if not isinstance(costs, Mapping):
        raise RuntimeError("Stress-90 capacity report requires live cost evidence")
    risk_preview = stress.get("risk_manager_preview")
    if not isinstance(risk_preview, Mapping):
        raise RuntimeError("Stress-90 capacity report requires RiskManager preview")
    failures = [
        str(item.name)
        for item in getattr(report, "checks", ())
        if not bool(getattr(item, "passed", False))
        and str(getattr(item, "name", "")).startswith("stress90_")
        and str(getattr(item, "name", "")) != "stress90_runtime_permission"
    ]
    return {
        "orders_sent": 0,
        "cancels_sent": 0,
        "account_identity_digest": str(account_identity_digest),
        "ctp_trading_day": str(ctp_trading_day),
        "risk_overlay_digest": stress.get("risk_overlay_digest"),
        "configured_live_risk_scale": stress.get("configured_live_risk_scale"),
        "raw_product_weights": stress.get("raw_product_weights", {}),
        "scaled_product_weights": stress.get("scaled_product_weights", {}),
        "raw_target_gross": stress.get("raw_target_gross", 0.0),
        "scaled_target_gross": stress.get("scaled_target_gross", 0.0),
        "selected_contracts": stress.get("selected_contracts", {}),
        "contract_capacity": stress.get("contract_capacity", {}),
        "live_costs": dict(costs),
        "raw_lots": stages.get("raw_integer_lots", {}),
        "scaled_lots": stages.get("scaled_integer_lots", {}),
        "margin_fitted_lots": stages.get("margin_fitted_lots", {}),
        "drawdown_frozen_lots": stages.get("drawdown_frozen_lots", {}),
        "hhi_frozen_lots": stages.get("hhi_frozen_lots", {}),
        "final_lots": stages.get("final_frozen_lots", {}),
        "reductions": stages.get("reductions", {}),
        "openings": stages.get("openings", {}),
        "nonzero_product_count": stress.get("nonzero_product_count", 0),
        "largest_product_absolute_gross_share": stress.get(
            "largest_product_absolute_gross_share", 0.0
        ),
        "product_hhi": stress.get("product_hhi", 0.0),
        "integer_tracking_error": stress.get("integer_tracking_error", 0.0),
        "estimated_margin_ratio": stress.get("estimated_margin_ratio", 0.0),
        "estimated_available_ratio": stress.get("estimated_available_ratio", 0.0),
        "contract_cap_hits": stress.get("contract_cap_hits", []),
        "clipped_products": stress.get("clipped_products", {}),
        "risk_manager_preview": dict(risk_preview),
        "execution_chain_commissioning_only": bool(
            stress.get("execution_chain_commissioning_only", False)
        ),
        "portfolio_representation_warning": stress.get(
            "portfolio_representation_warning", ""
        ),
        "hard_safety_failures": failures,
        "hard_safety_passed": not failures and bool(risk_preview.get("allowed", False)),
    }
'''
write("afuture/stress90_capacity.py", textwrap.dedent(CAPACITY).lstrip())

# DirectionalConfig: default-preserving scale with unconditional finite/range validation.
replace_once(
    "afuture/directional.py",
    "    max_gross_leverage: float = MAX_GROSS_LEVERAGE\n    min_days_to_expiry: int = 20\n",
    "    max_gross_leverage: float = MAX_GROSS_LEVERAGE\n    live_risk_scale: float = 1.0\n    min_days_to_expiry: int = 20\n",
)
replace_once(
    "afuture/directional.py",
    '            "max_gross_leverage",\n            "min_volume",\n',
    '            "max_gross_leverage",\n            "live_risk_scale",\n            "min_volume",\n',
)
replace_once(
    "afuture/directional.py",
    "        if not self.enabled:\n            return\n",
    "        if not 0.0 < self.live_risk_scale <= 1.0:\n"
    "            raise ValueError(\"directional.live_risk_scale must be finite and in (0, 1]\")\n"
    "        if not self.enabled:\n            return\n",
)

# Stress-90 lot pipeline: raw candidate remains untouched; scale is before integer/margin sizing.
replace_once(
    "afuture/directional_stress90_planner.py",
    "from .directional_stress90_policy import STRESS90_POLICY\n",
    "from .directional_stress90_policy import STRESS90_POLICY\n"
    "from .stress90_risk_overlay import scale_stress90_product_weights\n",
)
replace_once(
    "afuture/directional_stress90_planner.py",
    "class Stress90LotStages:\n    raw_integer_lots: dict[str, int]\n    margin_fitted_lots: dict[str, int]\n",
    "class Stress90LotStages:\n    raw_integer_lots: dict[str, int]\n    scaled_integer_lots: dict[str, int]\n    margin_fitted_lots: dict[str, int]\n",
)
replace_once(
    "afuture/directional_stress90_planner.py",
    "    product_weights: Mapping[str, float],\n    product_ticks: Mapping[str, Tick],\n",
    "    product_weights: Mapping[str, float],\n    product_ticks: Mapping[str, Tick],\n    live_risk_scale: float = 1.0,\n",
)
replace_once(
    "afuture/directional_stress90_planner.py",
    '    """Apply 1x integer sizing, margin fitting, reserve freeze, then HHI freeze."""\n',
    '    """Scale live product targets, then apply integer sizing, margin fit and freezes."""\n',
)
replace_once(
    "afuture/directional_stress90_planner.py",
    "        raise ValueError(\"Stress-90 contract cap must be a positive integer at most 35\")\n\n    raw = build_target_lots(\n",
    "        raise ValueError(\"Stress-90 contract cap must be a positive integer at most 35\")\n\n"
    "    scaled_weights = scale_stress90_product_weights(product_weights, live_risk_scale)\n"
    "    raw = build_target_lots(\n",
)
replace_once(
    "afuture/directional_stress90_planner.py",
    "    if persisted_margin_fitted_lots is not None:\n",
    "    scaled = build_target_lots(\n"
    "        account,\n"
    "        scaled_weights,\n"
    "        product_ticks,\n"
    "        specs,\n"
    "        max_contract_volume=max_contract_volume,\n"
    "    )\n"
    "    if persisted_margin_fitted_lots is not None:\n",
)
replace_once(
    "afuture/directional_stress90_planner.py",
    "            product_weights,\n            product_ticks,\n            specs,\n            max_contract_volume=max_contract_volume,\n            max_margin_ratio=max_margin_ratio,\n",
    "            scaled_weights,\n            product_ticks,\n            specs,\n            max_contract_volume=max_contract_volume,\n            max_margin_ratio=max_margin_ratio,\n",
)
replace_once(
    "afuture/directional_stress90_planner.py",
    "    return Stress90LotStages(\n        raw_integer_lots=_lots(raw),\n        margin_fitted_lots=margin_target,\n",
    "    return Stress90LotStages(\n        raw_integer_lots=_lots(raw),\n        scaled_integer_lots=_lots(scaled),\n        margin_fitted_lots=margin_target,\n",
)

# Policy identity marker: preserve policy/candidate digests while binding the separate risk overlay.
replace_once(
    "afuture/directional_policy_activation.py",
    '_STRESS90_MARKER_FIELDS = {\n    "policy_id",\n',
    '_LEGACY_STRESS90_MARKER_FIELDS = {\n'
    '    "policy_id",\n'
    '    "policy_definition_digest",\n'
    '    "products_manifest_digest",\n'
    '    "bootstrap_seed_digest",\n'
    '    "account_identity_digest",\n'
    '    "operator_reason",\n'
    '}\n'
    '_STRESS90_MARKER_FIELDS = {\n'
    '    *_LEGACY_STRESS90_MARKER_FIELDS,\n'
    '    "risk_overlay_digest",\n',
)
# remove duplicated original fields left after the replacement's first entry.
replace_once(
    "afuture/directional_policy_activation.py",
    '    "policy_definition_digest",\n    "products_manifest_digest",\n    "bootstrap_seed_digest",\n    "account_identity_digest",\n    "operator_reason",\n}\n_EXECUTION_ALIGNED_MARKER_FIELDS',
    '}\n_EXECUTION_ALIGNED_MARKER_FIELDS',
)
replace_once(
    "afuture/directional_policy_activation.py",
    "    bootstrap_seed_digest: str,\n    account_identity_digest: str,\n    operator_reason: str,\n",
    "    bootstrap_seed_digest: str,\n    account_identity_digest: str,\n    risk_overlay_digest: str,\n    operator_reason: str,\n",
)
replace_once(
    "afuture/directional_policy_activation.py",
    "    if _SHA256.fullmatch(account_identity_digest or \"\") is None:\n        raise RuntimeError(\"Stress-90 activation account identity is invalid\")\n",
    "    if _SHA256.fullmatch(account_identity_digest or \"\") is None:\n"
    "        raise RuntimeError(\"Stress-90 activation account identity is invalid\")\n"
    "    if _SHA256.fullmatch(risk_overlay_digest or \"\") is None:\n"
    "        raise RuntimeError(\"Stress-90 activation risk overlay identity is invalid\")\n",
)
replace_all(
    "afuture/directional_policy_activation.py",
    "set(existing_marker) != _STRESS90_MARKER_FIELDS",
    "set(existing_marker) not in (_STRESS90_MARKER_FIELDS, _LEGACY_STRESS90_MARKER_FIELDS)",
)
replace_once(
    "afuture/directional_policy_activation.py",
    "                account_identity_digest=account_identity_digest,\n            )\n",
    "                account_identity_digest=account_identity_digest,\n"
    "                risk_overlay_digest=(\n"
    "                    str(existing_marker.get(\"risk_overlay_digest\"))\n"
    "                    if \"risk_overlay_digest\" in existing_marker\n"
    "                    else None\n"
    "                ),\n"
    "            )\n",
)
replace_once(
    "afuture/directional_policy_activation.py",
    '        "account_identity_digest": account_identity_digest,\n        "operator_reason": operator_reason.strip(),\n',
    '        "account_identity_digest": account_identity_digest,\n'
    '        "risk_overlay_digest": risk_overlay_digest,\n'
    '        "operator_reason": operator_reason.strip(),\n',
)
replace_once(
    "afuture/directional_policy_activation.py",
    "    bootstrap_seed_digest: str | None = None,\n    account_identity_digest: str | None = None,\n) -> None:\n",
    "    bootstrap_seed_digest: str | None = None,\n"
    "    account_identity_digest: str | None = None,\n"
    "    risk_overlay_digest: str | None = None,\n"
    ") -> None:\n",
)
replace_once(
    "afuture/directional_policy_activation.py",
    "    if account_identity_digest is not None:\n",
    "    if risk_overlay_digest is not None:\n"
    "        if _SHA256.fullmatch(risk_overlay_digest or \"\") is None:\n"
    "            raise RuntimeError(\"directional risk overlay identity is invalid\")\n"
    "        if marker.get(\"risk_overlay_digest\") != risk_overlay_digest:\n"
    "            raise RuntimeError(\n"
    "                \"directional risk overlay identity mismatch; explicit HALTED activation/reactivation is required\"\n"
    "            )\n"
    "    if account_identity_digest is not None:\n",
)
replace_all(
    "afuture/directional_policy_activation.py",
    "set(marker) != _STRESS90_MARKER_FIELDS",
    "set(marker) not in (_STRESS90_MARKER_FIELDS, _LEGACY_STRESS90_MARKER_FIELDS)",
)
# New same-account, same-epoch marker-only rebind helper.
insert_anchor = "\n\ndef rebind_stress90_runtime_account_identity(\n"
text = read("afuture/directional_policy_activation.py")
if insert_anchor not in text:
    raise RuntimeError("activation rebind insertion anchor missing")
helper = r'''


def rebind_stress90_risk_overlay_identity(
    state: RuntimeState,
    *,
    risk_overlay_digest: str,
    operator_reason: str,
) -> RuntimeState:
    """Rebind only the production risk overlay after the existing HALTED lifecycle gates."""

    if state.runtime_mode != RuntimeMode.HALTED.value or not state.kill_switch or not state.reconciled:
        raise RuntimeError("Stress-90 risk overlay reactivation requires HALTED reconciled state")
    if _SHA256.fullmatch(risk_overlay_digest or "") is None:
        raise RuntimeError("Stress-90 risk overlay reactivation digest is invalid")
    if not isinstance(operator_reason, str) or not operator_reason.strip():
        raise RuntimeError("Stress-90 risk overlay reactivation operator reason is required")
    marker = state.strategy_states.get(POLICY_IDENTITY_STATE_KEY)
    if (
        not isinstance(marker, dict)
        or set(marker) not in (_STRESS90_MARKER_FIELDS, _LEGACY_STRESS90_MARKER_FIELDS)
        or marker.get("policy_id") != STRESS90_POLICY.policy_id
        or marker.get("policy_definition_digest") != STRESS90_POLICY.policy_definition_digest
        or marker.get("products_manifest_digest") != STRESS90_POLICY.products_manifest_digest
        or _SHA256.fullmatch(str(marker.get("bootstrap_seed_digest", ""))) is None
        or _SHA256.fullmatch(str(marker.get("account_identity_digest", ""))) is None
    ):
        raise RuntimeError("Stress-90 risk overlay source identity is invalid")
    strategy_states = {key: dict(value) for key, value in state.strategy_states.items()}
    strategy_states[POLICY_IDENTITY_STATE_KEY] = {
        **marker,
        "risk_overlay_digest": risk_overlay_digest,
        "operator_reason": operator_reason.strip(),
    }
    return replace(
        state,
        strategy_states=strategy_states,
        kill_switch=True,
        kill_reason="Stress-90 risk overlay rebound; fresh Doctor permit remains required",
        runtime_mode=RuntimeMode.HALTED.value,
        reconciled=True,
        metadata_verified=False,
        directional_daily_circuit_day="",
    )
'''
write(
    "afuture/directional_policy_activation.py",
    text.replace(insert_anchor, textwrap.dedent(helper).rstrip() + insert_anchor, 1),
)
# Migration reactivation also binds the current risk overlay.
replace_once(
    "afuture/directional_policy_activation.py",
    "    account_identity_digest: str,\n    operator_reason: str,\n    activation_confirmation: str,\n",
    "    account_identity_digest: str,\n    risk_overlay_digest: str,\n    operator_reason: str,\n    activation_confirmation: str,\n",
)
replace_once(
    "afuture/directional_policy_activation.py",
    "        account_identity_digest=account_identity_digest,\n        operator_reason=operator_reason,\n        strong_confirmation=activation_confirmation,\n",
    "        account_identity_digest=account_identity_digest,\n"
    "        risk_overlay_digest=risk_overlay_digest,\n"
    "        operator_reason=operator_reason,\n"
    "        strong_confirmation=activation_confirmation,\n",
)

# Execution intent: schema 7 writes risk digest; schema 6 remains readable but stale.
replace_once(
    "afuture/directional_stress90_execution.py",
    "_SCHEMA_VERSION = 6\n",
    "_SCHEMA_VERSION = 7\n_LEGACY_SCHEMA_VERSION = 6\n",
)
replace_once(
    "afuture/directional_stress90_execution.py",
    "    products_manifest_digest: str\n    account_identity_digest: str\n",
    "    products_manifest_digest: str\n    risk_overlay_digest: str\n    account_identity_digest: str\n",
)
replace_once(
    "afuture/directional_stress90_execution.py",
    '        "products_manifest_digest": intent.products_manifest_digest,\n        "account_identity_digest": intent.account_identity_digest,\n',
    '        "products_manifest_digest": intent.products_manifest_digest,\n'
    '        "risk_overlay_digest": intent.risk_overlay_digest,\n'
    '        "account_identity_digest": intent.account_identity_digest,\n',
)
# Replace _intent function's field gate with legacy/current handling.
replace_once(
    "afuture/directional_stress90_execution.py",
    "    fields = {\n        \"policy_definition_digest\",\n        \"products_manifest_digest\",\n        \"account_identity_digest\",\n",
    "    legacy_fields = {\n"
    "        \"policy_definition_digest\",\n"
    "        \"products_manifest_digest\",\n"
    "        \"account_identity_digest\",\n",
)
replace_once(
    "afuture/directional_stress90_execution.py",
    "        \"source_digest\",\n    }\n    if not isinstance(raw, Mapping) or set(raw) != fields:\n        raise Stress90ExecutionIntentIntegrityError(\"execution intent fields are invalid\")\n    result = Stress90ExecutionIntent(\n",
    "        \"source_digest\",\n"
    "    }\n"
    "    fields = {*legacy_fields, \"risk_overlay_digest\"}\n"
    "    if not isinstance(raw, Mapping) or set(raw) not in (fields, legacy_fields):\n"
    "        raise Stress90ExecutionIntentIntegrityError(\"execution intent fields are invalid\")\n"
    "    legacy = set(raw) == legacy_fields\n"
    "    result = Stress90ExecutionIntent(\n",
)
replace_once(
    "afuture/directional_stress90_execution.py",
    "        products_manifest_digest=_sha(\n            raw[\"products_manifest_digest\"], name=\"execution products digest\"\n        ),\n        account_identity_digest=_sha(\n",
    "        products_manifest_digest=_sha(\n"
    "            raw[\"products_manifest_digest\"], name=\"execution products digest\"\n"
    "        ),\n"
    "        risk_overlay_digest=(\n"
    "            \"\"\n"
    "            if legacy\n"
    "            else _sha(raw[\"risk_overlay_digest\"], name=\"execution risk overlay digest\")\n"
    "        ),\n"
    "        account_identity_digest=_sha(\n",
)
replace_once(
    "afuture/directional_stress90_execution.py",
    '        "account_epoch": result.account_epoch,\n        "account_identity_digest": result.account_identity_digest,\n        "current_lots": dict(result.initial_current_lots),\n',
    '        "account_epoch": result.account_epoch,\n'
    '        "account_identity_digest": result.account_identity_digest,\n'
    '        **({} if legacy else {"risk_overlay_digest": result.risk_overlay_digest}),\n'
    '        "current_lots": dict(result.initial_current_lots),\n',
)
replace_once(
    "afuture/directional_stress90_execution.py",
    "        if raw[\"kind\"] != _KIND or raw[\"schema_version\"] != _SCHEMA_VERSION:\n            raise Stress90ExecutionIntentIntegrityError(\"execution intent schema is invalid\")\n",
    "        if raw[\"kind\"] != _KIND or raw[\"schema_version\"] not in {\n"
    "            _LEGACY_SCHEMA_VERSION,\n"
    "            _SCHEMA_VERSION,\n"
    "        }:\n"
    "            raise Stress90ExecutionIntentIntegrityError(\"execution intent schema is invalid\")\n",
)
replace_once(
    "afuture/directional_stress90_execution.py",
    "    account_epoch: str,\n    current_lots: Mapping[str, int],\n",
    "    account_epoch: str,\n    risk_overlay_digest: str,\n    current_lots: Mapping[str, int],\n",
)
replace_once(
    "afuture/directional_stress90_execution.py",
    "    epoch = _sha(account_epoch, name=\"execution account epoch\")\n    current = _lots(current_lots, name=\"current lots\")\n",
    "    epoch = _sha(account_epoch, name=\"execution account epoch\")\n"
    "    risk_overlay = _sha(risk_overlay_digest, name=\"execution risk overlay digest\")\n"
    "    current = _lots(current_lots, name=\"current lots\")\n",
)
replace_once(
    "afuture/directional_stress90_execution.py",
    "            if intent.account_identity_digest != account_identity or intent.account_epoch != epoch:\n                raise Stress90ExecutionIntentIntegrityError(\n                    \"same-day execution intent belongs to a stale account lifecycle epoch\"\n                )\n            return intent\n",
    "            if intent.account_identity_digest != account_identity or intent.account_epoch != epoch:\n"
    "                raise Stress90ExecutionIntentIntegrityError(\n"
    "                    \"same-day execution intent belongs to a stale account lifecycle epoch\"\n"
    "                )\n"
    "            if intent.risk_overlay_digest != risk_overlay:\n"
    "                raise Stress90ExecutionIntentIntegrityError(\n"
    "                    \"same-day execution intent risk overlay changed; old intent cannot be reinterpreted\"\n"
    "                )\n"
    "            return intent\n",
)
replace_once(
    "afuture/directional_stress90_execution.py",
    '        "account_identity_digest": account_identity,\n        "current_lots": dict(current),\n',
    '        "account_identity_digest": account_identity,\n'
    '        "risk_overlay_digest": risk_overlay,\n'
    '        "current_lots": dict(current),\n',
)
replace_once(
    "afuture/directional_stress90_execution.py",
    "        products_manifest_digest=STRESS90_POLICY.products_manifest_digest,\n        account_identity_digest=account_identity,\n",
    "        products_manifest_digest=STRESS90_POLICY.products_manifest_digest,\n"
    "        risk_overlay_digest=risk_overlay,\n"
    "        account_identity_digest=account_identity,\n",
)

# Runtime binds current digest, scales before lot generation, and reuses persisted old intent only under same digest.
replace_once(
    "afuture/directional_stress90_runtime.py",
    "from .position import PositionBook\n",
    "from .position import PositionBook\n"
    "from .stress90_risk_overlay import (\n"
    "    scale_stress90_product_weights,\n"
    "    stress90_risk_overlay_digest,\n"
    ")\n",
)
replace_once(
    "afuture/directional_stress90_runtime.py",
    "        _validate_hard_risk_envelope(config, risk_manager.config)\n        self.policy_state_store = Stress90PolicyStateStore(policy_state_path)\n",
    "        _validate_hard_risk_envelope(config, risk_manager.config)\n"
    "        self.risk_overlay_digest = stress90_risk_overlay_digest(config, risk_manager.config)\n"
    "        self.policy_state_store = Stress90PolicyStateStore(policy_state_path)\n",
)
replace_once(
    "afuture/directional_stress90_runtime.py",
    "            product_ticks=product_ticks,\n            specs=specs,\n            incumbent_ticks={\n",
    "            product_ticks=product_ticks,\n"
    "            specs=specs,\n"
    "            live_risk_scale=self.config.live_risk_scale,\n"
    "            incumbent_ticks={\n",
)
replace_once(
    "afuture/directional_stress90_runtime.py",
    "                        and intent_matches_account(intent_record.intent)\n                        and intent_record.intent.target_trading_day == current\n",
    "                        and intent_matches_account(intent_record.intent)\n"
    "                        and intent_record.intent.risk_overlay_digest == self.risk_overlay_digest\n"
    "                        and intent_record.intent.target_trading_day == current\n",
)
replace_once(
    "afuture/directional_stress90_runtime.py",
    "                            or not intent_matches_account(intent_record.intent)\n                            or entry.daily_decision_digest\n",
    "                            or not intent_matches_account(intent_record.intent)\n"
    "                            or intent_record.intent.risk_overlay_digest\n"
    "                            != self.risk_overlay_digest\n"
    "                            or entry.daily_decision_digest\n",
)
replace_once(
    "afuture/directional_stress90_runtime.py",
    "            account_epoch=account_epoch,\n            current_lots=current_lots,\n",
    "            account_epoch=account_epoch,\n"
    "            risk_overlay_digest=self.risk_overlay_digest,\n"
    "            current_lots=current_lots,\n",
)

# Runtime factory fails closed before constructing Stress-90 order authority when config digest differs.
replace_once(
    "afuture/runtime_factory.py",
    "            runtime_dir = Path(state_store.path).parent.resolve(strict=False)\n            _require_stress90_account_runtime_binding(\n",
    "            runtime_dir = Path(state_store.path).parent.resolve(strict=False)\n"
    "            from .directional_policy_activation import require_directional_policy_identity\n"
    "            from .directional_stress90_policy import STRESS90_POLICY\n"
    "            from .directional_stress90_state import Stress90SeedStore\n"
    "            from .stress90_risk_overlay import stress90_risk_overlay_digest\n\n"
    "            generic = state_store.load_required_record()\n"
    "            seed = Stress90SeedStore(runtime_dir / \"stress90_bootstrap_seed.json\").load_required()\n"
    "            require_directional_policy_identity(\n"
    "                generic.state,\n"
    "                policy_id=STRESS90_POLICY.policy_id,\n"
    "                policy_definition_digest=STRESS90_POLICY.policy_definition_digest,\n"
    "                products_manifest_digest=STRESS90_POLICY.products_manifest_digest,\n"
    "                bootstrap_seed_digest=seed.seed_digest,\n"
    "                risk_overlay_digest=stress90_risk_overlay_digest(config.directional, config.risk),\n"
    "            )\n"
    "            _require_stress90_account_runtime_binding(\n",
)

# Activation permit evidence binds the exact risk overlay; old schema-5 permits remain readable but stale.
replace_once(
    "afuture/stress90_activation_permit.py",
    "STRESS90_ACTIVATION_PERMIT_SCHEMA_VERSION = 5\n",
    "STRESS90_ACTIVATION_PERMIT_SCHEMA_VERSION = 6\n"
    "_LEGACY_STRESS90_ACTIVATION_PERMIT_SCHEMA_VERSION = 5\n",
)
replace_once(
    "afuture/stress90_activation_permit.py",
    "        \"stress90_runtime_policy_identity\",\n        \"stress90_oi_evidence_integrity\",\n",
    "        \"stress90_runtime_policy_identity\",\n"
    "        \"stress90_risk_overlay_identity\",\n"
    "        \"stress90_oi_evidence_integrity\",\n",
)
replace_once(
    "afuture/stress90_activation_permit.py",
    "        \"stress90_integer_preview\",\n        \"stress90_first_activation_gates\",\n",
    "        \"stress90_integer_preview\",\n"
    "        \"stress90_risk_manager_preview\",\n"
    "        \"stress90_first_activation_gates\",\n",
)
replace_once(
    "afuture/stress90_activation_permit.py",
    "    products_manifest_digest: str\n    bootstrap_seed_digest: str\n",
    "    products_manifest_digest: str\n    risk_overlay_digest: str = \"\"\n    bootstrap_seed_digest: str = \"\"\n",
)
# dataclass defaults require following fields to have defaults: move risk field after active_order_count instead.
text = read("afuture/stress90_activation_permit.py")
text = text.replace(
    '    products_manifest_digest: str\n    risk_overlay_digest: str = ""\n    bootstrap_seed_digest: str = ""\n',
    '    products_manifest_digest: str\n    bootstrap_seed_digest: str\n',
    1,
)
text = text.replace(
    '    active_order_count: int\n    account_continuity_mode: str = "strict"\n',
    '    active_order_count: int\n    risk_overlay_digest: str = ""\n    account_continuity_mode: str = "strict"\n',
    1,
)
write("afuture/stress90_activation_permit.py", text)
replace_once(
    "afuture/stress90_activation_permit.py",
    "    if evidence.account_continuity_mode not in {\"strict\", \"operator_managed\"}:\n",
    "    if evidence.risk_overlay_digest:\n"
    "        _valid_sha256(evidence.risk_overlay_digest, name=\"risk overlay digest\")\n"
    "    if evidence.account_continuity_mode not in {\"strict\", \"operator_managed\"}:\n",
)
replace_once(
    "afuture/stress90_activation_permit.py",
    "        products_manifest_digest=STRESS90_POLICY.products_manifest_digest,\n        bootstrap_seed_digest=seed.seed_digest,\n",
    "        products_manifest_digest=STRESS90_POLICY.products_manifest_digest,\n"
    "        bootstrap_seed_digest=seed.seed_digest,\n"
    "        risk_overlay_digest=str(\n"
    "            state.strategy_states[\"directional_policy_identity\"].get(\"risk_overlay_digest\", \"\")\n"
    "        ),\n",
)
replace_once(
    "afuture/stress90_activation_permit.py",
    "    expected_evidence_fields = set(Stress90ActivationEvidence.__dataclass_fields__)\n    if not isinstance(evidence_raw, dict) or set(evidence_raw) != expected_evidence_fields:\n        raise Stress90ActivationPermitIntegrityError(\"activation evidence fields are invalid\")\n    try:\n        evidence = Stress90ActivationEvidence(**evidence_raw)\n",
    "    expected_evidence_fields = set(Stress90ActivationEvidence.__dataclass_fields__)\n"
    "    legacy_evidence_fields = expected_evidence_fields - {\"risk_overlay_digest\"}\n"
    "    if not isinstance(evidence_raw, dict) or set(evidence_raw) not in (\n"
    "        expected_evidence_fields,\n"
    "        legacy_evidence_fields,\n"
    "    ):\n"
    "        raise Stress90ActivationPermitIntegrityError(\"activation evidence fields are invalid\")\n"
    "    if set(evidence_raw) == legacy_evidence_fields:\n"
    "        evidence_raw = {**evidence_raw, \"risk_overlay_digest\": \"\"}\n"
    "    try:\n"
    "        evidence = Stress90ActivationEvidence(**evidence_raw)\n",
)
replace_once(
    "afuture/stress90_activation_permit.py",
    "        if raw[\"schema_version\"] != STRESS90_ACTIVATION_PERMIT_SCHEMA_VERSION:\n            raise Stress90ActivationPermitIntegrityError(\"activation permit schema is unsupported\")\n",
    "        if raw[\"schema_version\"] not in {\n"
    "            _LEGACY_STRESS90_ACTIVATION_PERMIT_SCHEMA_VERSION,\n"
    "            STRESS90_ACTIVATION_PERMIT_SCHEMA_VERSION,\n"
    "        }:\n"
    "            raise Stress90ActivationPermitIntegrityError(\"activation permit schema is unsupported\")\n",
)

# Lifecycle coordinator gains one marker-only operation; it remains the sole coordinator.
replace_once(
    "afuture/stress90_lifecycle_transaction.py",
    '    "reactivation",\n    "account_rebase",\n',
    '    "reactivation",\n    "risk_overlay_reactivation",\n    "account_rebase",\n',
)
replace_once(
    "afuture/stress90_lifecycle_transaction.py",
    '_STRESS90_MARKER_FIELDS = {\n    "policy_id",\n',
    '_LEGACY_STRESS90_MARKER_FIELDS = {\n'
    '    "policy_id",\n'
    '    "policy_definition_digest",\n'
    '    "products_manifest_digest",\n'
    '    "bootstrap_seed_digest",\n'
    '    "account_identity_digest",\n'
    '    "operator_reason",\n'
    '}\n'
    '_STRESS90_MARKER_FIELDS = {\n'
    '    *_LEGACY_STRESS90_MARKER_FIELDS,\n'
    '    "risk_overlay_digest",\n',
)
replace_once(
    "afuture/stress90_lifecycle_transaction.py",
    '    "policy_definition_digest",\n    "products_manifest_digest",\n    "bootstrap_seed_digest",\n    "account_identity_digest",\n    "operator_reason",\n}\n_EXECUTION_ALIGNED_MARKER_FIELDS',
    '}\n_EXECUTION_ALIGNED_MARKER_FIELDS',
)
replace_once(
    "afuture/stress90_lifecycle_transaction.py",
    "_MIGRATION_GENERIC_MUTABLE_FIELDS = {\n",
    "_RISK_OVERLAY_REACTIVATION_GENERIC_MUTABLE_FIELDS = {\n"
    "    \"kill_reason\",\n"
    "    \"metadata_verified\",\n"
    "    \"directional_daily_circuit_day\",\n"
    "    \"strategy_states\",\n"
    "}\n"
    "_MIGRATION_GENERIC_MUTABLE_FIELDS = {\n",
)
# Historical committed lifecycle transactions may contain the pre-overlay marker.
replace_all(
    "afuture/stress90_lifecycle_transaction.py",
    "set(marker) != _STRESS90_MARKER_FIELDS",
    "set(marker) not in (_STRESS90_MARKER_FIELDS, _LEGACY_STRESS90_MARKER_FIELDS)",
    minimum=2,
)
# New operation invariants before normal activation/rebase branch.
anchor = "    if transaction.operation in {\"activation\", \"reactivation\", \"account_rebase\"}:\n"
text = read("afuture/stress90_lifecycle_transaction.py")
if text.count(anchor) != 1:
    raise RuntimeError("lifecycle invariant insertion anchor mismatch")
block = r'''
    if transaction.operation == "risk_overlay_reactivation":
        if (
            not transaction.source_account_epoch
            or policy.live_account_epoch != transaction.source_account_epoch
            or transaction.source_account_identity_digest != account_identity
            or policy.live_account_identity_digest != account_identity
            or set(marker) != _STRESS90_MARKER_FIELDS
            or marker.get("policy_id") != STRESS90_POLICY.policy_id
            or marker.get("policy_definition_digest") != STRESS90_POLICY.policy_definition_digest
            or marker.get("products_manifest_digest") != STRESS90_POLICY.products_manifest_digest
            or marker.get("bootstrap_seed_digest") != policy.bootstrap_seed_digest
            or _SHA.fullmatch(str(marker.get("risk_overlay_digest", ""))) is None
            or marker.get("operator_reason") != transaction.operator_reason
            or transaction.verified_deposit_delta != 0.0
            or transaction.verified_withdrawal_delta != 0.0
        ):
            raise Stress90LifecycleTransactionError(
                "risk-overlay reactivation lifecycle identity mismatch"
            )
        evidence = transaction.account_evidence
        if (
            str(evidence["trading_day"]) != transaction.trading_day
            or generic.trading_day != transaction.trading_day
            or generic.last_account_trading_day != transaction.trading_day
            or generic.last_account_equity != _finite_float(evidence["equity"], "account evidence equity")
            or generic.last_account_deposit != _nonnegative_finite(evidence["deposit"], "account evidence deposit")
            or generic.last_account_withdrawal
            != _nonnegative_finite(evidence["withdrawal"], "account evidence withdrawal")
            or generic.last_account_cash_flow_verified
            is not _boolean(evidence["cash_flow_verified"], "account evidence cash-flow verification")
            or generic.last_account_settlement_id
            != _nonnegative_int(evidence["settlement_id"], "account evidence settlement identity")
        ):
            raise Stress90LifecycleTransactionError(
                "risk-overlay reactivation account snapshot no longer matches persisted state"
            )
        return

'''
write("afuture/stress90_lifecycle_transaction.py", text.replace(anchor, textwrap.dedent(block) + anchor, 1))
# Source validation accepts current/legacy Stress-90 marker for overlay-only rebind.
anchor = "    if operation == \"reactivation\":\n"
text = read("afuture/stress90_lifecycle_transaction.py")
if text.count(anchor) < 2:
    raise RuntimeError("reactivation anchors missing")
source_block = r'''
    if operation == "risk_overlay_reactivation":
        if (
            set(marker) not in (_STRESS90_MARKER_FIELDS, _LEGACY_STRESS90_MARKER_FIELDS)
            or marker.get("policy_id") != STRESS90_POLICY.policy_id
            or marker.get("policy_definition_digest") != STRESS90_POLICY.policy_definition_digest
            or marker.get("products_manifest_digest") != STRESS90_POLICY.products_manifest_digest
            or marker.get("bootstrap_seed_digest") != source_policy.bootstrap_seed_digest
            or marker.get("account_identity_digest") != account_identity_digest
            or source_policy.live_account_identity_digest != account_identity_digest
            or not source_policy.live_account_epoch
            or generic.trading_day != trading_day
            or generic.last_account_trading_day != trading_day
        ):
            raise Stress90LifecycleTransactionError(
                "risk-overlay reactivation requires exact same-account Stress-90 lineage"
            )
        return account_identity_digest

'''
# insert before the LAST source-validation occurrence by locating _validate_begin_source_invariants.
pos = text.index("def _validate_begin_source_invariants")
idx = text.index(anchor, pos)
text = text[:idx] + textwrap.dedent(source_block) + text[idx:]
write("afuture/stress90_lifecycle_transaction.py", text)
# Transition validator branch before normal reactivation.
text = read("afuture/stress90_lifecycle_transaction.py")
pos = text.index("def _validate_operation_transition")
idx = text.index(anchor, pos)
transition_block = r'''
    if operation == "risk_overlay_reactivation":
        if account_transition is not None or policy_target != source_policy:
            raise Stress90LifecycleTransactionError(
                "risk-overlay reactivation cannot alter Stress-90 policy/account state"
            )
        _require_only_allowed_delta(
            generic_source.state,
            generic_target,
            allowed_fields=_RISK_OVERLAY_REACTIVATION_GENERIC_MUTABLE_FIELDS,
            label="risk-overlay reactivation generic target",
        )
        _require_other_strategy_states_unchanged(
            generic_source.state,
            generic_target,
            label="risk-overlay reactivation",
            operation_nonce=operation_nonce,
        )
        source_marker = generic_source.state.strategy_states.get(_POLICY_IDENTITY_STATE_KEY)
        target_marker = generic_target.strategy_states.get(_POLICY_IDENTITY_STATE_KEY)
        if not isinstance(source_marker, dict) or not isinstance(target_marker, dict):
            raise Stress90LifecycleTransactionError("risk-overlay marker transition is invalid")
        source_without_overlay = {
            key: value
            for key, value in source_marker.items()
            if key not in {"risk_overlay_digest", "operator_reason"}
        }
        target_without_overlay = {
            key: value
            for key, value in target_marker.items()
            if key not in {"risk_overlay_digest", "operator_reason"}
        }
        if (
            source_without_overlay != target_without_overlay
            or set(target_marker) != _STRESS90_MARKER_FIELDS
            or _SHA.fullmatch(str(target_marker.get("risk_overlay_digest", ""))) is None
            or target_marker.get("risk_overlay_digest") == source_marker.get("risk_overlay_digest")
            or target_marker.get("operator_reason") is None
            or not str(target_marker["operator_reason"]).strip()
            or generic_target.positions != generic_source.state.positions
            or generic_target.recent_daily_returns != generic_source.state.recent_daily_returns
        ):
            raise Stress90LifecycleTransactionError("risk-overlay reactivation changed forbidden state")
        return

'''
text = text[:idx] + textwrap.dedent(transition_block) + text[idx:]
write("afuture/stress90_lifecycle_transaction.py", text)

# Operations: real commission option, risk-overlay status, scaled Doctor preview, capacity diagnostics.
replace_once(
    "afuture/operations.py",
    "    *,\n    hurdle_bps: float = 15.0,\n) -> dict[str, object]:\n",
    "    *,\n    hurdle_bps: float = 15.0,\n    require_commission_evidence: bool = False,\n) -> dict[str, object]:\n",
)
replace_once(
    "afuture/operations.py",
    "    open_fee = fee_bps(spec.fee.open_fixed, spec.fee.open_rate)\n",
    "    fee_values = [float(getattr(spec.fee, name)) for name in spec.fee.__dataclass_fields__]\n"
    "    if require_commission_evidence and not any(value > 0.0 for value in fee_values):\n"
    "        raise ValueError(\"live commission evidence is missing\")\n"
    "    open_fee = fee_bps(spec.fee.open_fixed, spec.fee.open_rate)\n",
)
replace_once(
    "afuture/operations.py",
    "                hurdle_bps=STRESS90_POLICY.cost_hurdle_bps,\n            )\n",
    "                hurdle_bps=STRESS90_POLICY.cost_hurdle_bps,\n"
    "                require_commission_evidence=True,\n"
    "            )\n",
)
# Local status separate risk-overlay check immediately before require policy identity.
replace_once(
    "afuture/operations.py",
    "        require_directional_policy_identity(\n            runtime_state,\n            policy_id=STRESS90_POLICY.policy_id,\n",
    "        from .stress90_risk_overlay import stress90_risk_overlay_digest\n\n"
    "        configured_risk_overlay_digest = stress90_risk_overlay_digest(\n"
    "            config.directional, config.risk\n"
    "        )\n"
    "        marker = runtime_state.strategy_states.get(POLICY_IDENTITY_STATE_KEY)\n"
    "        bound_risk_overlay_digest = (\n"
    "            str(marker.get(\"risk_overlay_digest\", \"\")) if isinstance(marker, dict) else \"\"\n"
    "        )\n"
    "        risk_overlay_matches = bound_risk_overlay_digest == configured_risk_overlay_digest\n"
    "        facts[\"configured_live_risk_scale\"] = float(config.directional.live_risk_scale)\n"
    "        facts[\"risk_overlay_digest\"] = configured_risk_overlay_digest\n"
    "        facts[\"bound_risk_overlay_digest\"] = bound_risk_overlay_digest\n"
    "        facts[\"risk_overlay_matches\"] = risk_overlay_matches\n"
    "        facts[\"risk_overlay_reactivation_required\"] = not risk_overlay_matches\n"
    "        report.add(\n"
    "            \"stress90_risk_overlay_identity\",\n"
    "            risk_overlay_matches,\n"
    "            \"configured risk overlay matches activated runtime identity\"\n"
    "            if risk_overlay_matches\n"
    "            else \"configured risk overlay differs from activated runtime; HALTED reactivation required\",\n"
    "        )\n"
    "        if not risk_overlay_matches:\n"
    "            blockers.append(\"stress90_risk_overlay_identity\")\n"
    "        require_directional_policy_identity(\n"
    "            runtime_state,\n"
    "            policy_id=STRESS90_POLICY.policy_id,\n",
)
replace_once(
    "afuture/operations.py",
    "            bootstrap_seed_digest=seed.seed_digest,\n        )\n",
    "            bootstrap_seed_digest=seed.seed_digest,\n"
    "            risk_overlay_digest=configured_risk_overlay_digest,\n"
    "        )\n",
)
# Doctor planner passes scale.
replace_once(
    "afuture/operations.py",
    "                product_ticks=product_ticks,\n                specs=metadata,\n                incumbent_ticks={\n",
    "                product_ticks=product_ticks,\n"
    "                specs=metadata,\n"
    "                live_risk_scale=config.directional.live_risk_scale,\n"
    "                incumbent_ticks={\n",
)
# Enhance stages facts block and metrics.
replace_once(
    "afuture/operations.py",
    '                    "raw_integer_lots": stages.raw_integer_lots,\n                    "margin_fitted_lots": stages.margin_fitted_lots,\n',
    '                    "raw_integer_lots": stages.raw_integer_lots,\n'
    '                    "scaled_integer_lots": stages.scaled_integer_lots,\n'
    '                    "margin_fitted_lots": stages.margin_fitted_lots,\n',
)
replace_once(
    "afuture/operations.py",
    "            target_weights = realized_weights(stages.final_frozen_lots)\n            actual_weights = realized_weights(current_lots)\n            target_gross = sum(abs(value) for value in target_weights.values())\n            actual_gross = sum(abs(value) for value in actual_weights.values())\n            tracking_error = sum(\n                abs(target_weights[product] - float(prepared.survivor_weights[product]))\n                for product in STRESS90_POLICY.products\n            )\n            stress.update(\n",
    "            from .risk import RiskManager\n"
    "            from .stress90_risk_overlay import (\n"
    "                scale_stress90_product_weights,\n"
    "                stress90_risk_overlay_digest,\n"
    "            )\n"
    "            from .models import Offset, OrderRequest, OrderSide\n\n"
    "            raw_product_weights = {\n"
    "                product: float(prepared.survivor_weights[product])\n"
    "                for product in STRESS90_POLICY.products\n"
    "            }\n"
    "            scaled_product_weights = scale_stress90_product_weights(\n"
    "                raw_product_weights, config.directional.live_risk_scale\n"
    "            )\n"
    "            target_weights = realized_weights(stages.final_frozen_lots)\n"
    "            actual_weights = realized_weights(current_lots)\n"
    "            target_gross = sum(abs(value) for value in target_weights.values())\n"
    "            actual_gross = sum(abs(value) for value in actual_weights.values())\n"
    "            raw_target_gross = sum(abs(value) for value in raw_product_weights.values())\n"
    "            scaled_target_gross = sum(abs(value) for value in scaled_product_weights.values())\n"
    "            tracking_error = sum(\n"
    "                abs(target_weights[product] - scaled_product_weights[product])\n"
    "                for product in STRESS90_POLICY.products\n"
    "            )\n"
    "            nonzero_weights = {\n"
    "                product: value for product, value in target_weights.items() if abs(value) > 1e-15\n"
    "            }\n"
    "            largest_share = (\n"
    "                max(abs(value) for value in nonzero_weights.values()) / target_gross\n"
    "                if target_gross > 0.0\n"
    "                else 0.0\n"
    "            )\n"
    "            product_hhi = (\n"
    "                sum((abs(value) / target_gross) ** 2 for value in nonzero_weights.values())\n"
    "                if target_gross > 0.0\n"
    "                else 0.0\n"
    "            )\n"
    "            per_lot_margin: dict[str, float] = {}\n"
    "            for symbol, volume in stages.final_frozen_lots.items():\n"
    "                tick = ticks_by_symbol[symbol]\n"
    "                spec = metadata[symbol]\n"
    "                rate = spec.margin_rate_long if volume > 0 else spec.margin_rate_short\n"
    "                per_lot_margin[symbol] = (\n"
    "                    float(tick.mid_price)\n"
    "                    * float(spec.multiplier)\n"
    "                    * float(rate)\n"
    "                    * float(config.risk.margin_estimate_buffer)\n"
    "                )\n"
    "            target_margin = sum(\n"
    "                abs(volume) * per_lot_margin[symbol]\n"
    "                for symbol, volume in stages.final_frozen_lots.items()\n"
    "            )\n"
    "            estimated_margin_ratio = target_margin / float(account.equity)\n"
    "            estimated_available_ratio = max(0.0, float(account.equity) - target_margin) / float(\n"
    "                account.equity\n"
    "            )\n"
    "            opening_requests: list[OrderRequest] = []\n"
    "            for symbol, delta in stages.openings.items():\n"
    "                tick = ticks_by_symbol[symbol]\n"
    "                opening_requests.append(\n"
    "                    OrderRequest(\n"
    "                        symbol=symbol,\n"
    "                        exchange=metadata[symbol].exchange,\n"
    "                        side=OrderSide.BUY if delta > 0 else OrderSide.SELL,\n"
    "                        offset=Offset.OPEN,\n"
    "                        volume=abs(int(delta)),\n"
    "                        price=float(tick.ask_price if delta > 0 else tick.bid_price),\n"
    "                        reference=\"stress90-capacity-preview\",\n"
    "                    )\n"
    "                )\n"
    "            risk_preview = RiskManager(config.risk).check_open_orders(\n"
    "                account,\n"
    "                opening_requests,\n"
    "                metadata,\n"
    "                current_contract_volumes={symbol: abs(volume) for symbol, volume in current_lots.items()},\n"
    "            )\n"
    "            report.add(\n"
    "                \"stress90_risk_manager_preview\",\n"
    "                risk_preview.allowed,\n"
    "                \"RiskManager preview accepts the shared opening plan\"\n"
    "                if risk_preview.allowed\n"
    "                else \"RiskManager preview blocks openings: \" + risk_preview.reason,\n"
    "            )\n"
    "            cap = min(config.directional.max_contract_volume, config.risk.max_contract_volume)\n"
    "            contract_cap_hits = sorted(\n"
    "                symbol for symbol, volume in stages.scaled_integer_lots.items() if abs(volume) >= cap\n"
    "            )\n"
    "            scaled_by_product = {\n"
    "                symbol_products[symbol]: abs(volume)\n"
    "                for symbol, volume in stages.scaled_integer_lots.items()\n"
    "            }\n"
    "            margin_by_product = {\n"
    "                symbol_products[symbol]: abs(volume)\n"
    "                for symbol, volume in stages.margin_fitted_lots.items()\n"
    "            }\n"
    "            final_by_product = {\n"
    "                symbol_products[symbol]: abs(volume)\n"
    "                for symbol, volume in stages.final_frozen_lots.items()\n"
    "            }\n"
    "            clipped_products = {\n"
    "                \"integer\": sorted(\n"
    "                    product\n"
    "                    for product, weight in scaled_product_weights.items()\n"
    "                    if abs(weight) > 1e-15 and scaled_by_product.get(product, 0) == 0\n"
    "                ),\n"
    "                \"margin_or_funding\": sorted(\n"
    "                    product\n"
    "                    for product, lots in scaled_by_product.items()\n"
    "                    if margin_by_product.get(product, 0) < lots\n"
    "                ),\n"
    "                \"drawdown\": sorted(\n"
    "                    {symbol_products[symbol] for symbol in set(stages.margin_fitted_lots) | set(stages.drawdown_frozen_lots) if abs(stages.drawdown_frozen_lots.get(symbol, 0)) < abs(stages.margin_fitted_lots.get(symbol, 0))}\n"
    "                ),\n"
    "                \"concentration\": sorted(\n"
    "                    {symbol_products[symbol] for symbol in set(stages.drawdown_frozen_lots) | set(stages.hhi_frozen_lots) if abs(stages.hhi_frozen_lots.get(symbol, 0)) < abs(stages.drawdown_frozen_lots.get(symbol, 0))}\n"
    "                ),\n"
    "                \"cost\": sorted(\n"
    "                    product for product, contract in selected.items()\n"
    "                    if contract.symbol in costs and costs[contract.symbol][\"historical_15bp_compatible\"] is not True\n"
    "                ),\n"
    "                \"final\": sorted(\n"
    "                    product for product, lots in margin_by_product.items() if final_by_product.get(product, 0) < lots\n"
    "                ),\n"
    "            }\n"
    "            contract_capacity: dict[str, dict[str, object]] = {}\n"
    "            for product, contract in selected.items():\n"
    "                tick = quotes.get(contract.symbol)\n"
    "                spec = metadata.get(contract.symbol)\n"
    "                if tick is None or spec is None:\n"
    "                    continue\n"
    "                notional = float(tick.mid_price) * float(spec.multiplier)\n"
    "                contract_capacity[product] = {\n"
    "                    \"symbol\": contract.symbol,\n"
    "                    \"per_lot_notional\": notional,\n"
    "                    \"margin_rate_long\": float(spec.margin_rate_long),\n"
    "                    \"margin_rate_short\": float(spec.margin_rate_short),\n"
    "                    \"buffered_margin_long\": notional * float(spec.margin_rate_long) * float(config.risk.margin_estimate_buffer),\n"
    "                    \"buffered_margin_short\": notional * float(spec.margin_rate_short) * float(config.risk.margin_estimate_buffer),\n"
    "                    **dict(costs.get(contract.symbol, {})),\n"
    "                }\n"
    "            representation_parts: list[str] = []\n"
    "            if clipped_products[\"integer\"]:\n"
    "                representation_parts.append(\"scaled products round to zero lots\")\n"
    "            if target_gross == 0.0 and scaled_target_gross > 0.0:\n"
    "                representation_parts.append(\"non-zero scaled portfolio cannot be represented by one-lot granularity\")\n"
    "            if largest_share > 0.5:\n"
    "                representation_parts.append(\"final portfolio is dominated by one product\")\n"
    "            if tracking_error > 0.25:\n"
    "                representation_parts.append(\"integer target has material L1 tracking error\")\n"
    "            stress.update(\n",
)
replace_once(
    "afuture/operations.py",
    "                target_lots=stages.final_frozen_lots,\n                current_broker_lots=current_lots,\n                target_gross=target_gross,\n",
    "                configured_live_risk_scale=float(config.directional.live_risk_scale),\n"
    "                risk_overlay_digest=stress90_risk_overlay_digest(config.directional, config.risk),\n"
    "                raw_product_weights=raw_product_weights,\n"
    "                scaled_product_weights=scaled_product_weights,\n"
    "                raw_target_gross=raw_target_gross,\n"
    "                scaled_target_gross=scaled_target_gross,\n"
    "                selected_contracts={product: contract.symbol for product, contract in selected.items()},\n"
    "                contract_capacity=contract_capacity,\n"
    "                target_lots=stages.final_frozen_lots,\n"
    "                current_broker_lots=current_lots,\n"
    "                target_gross=target_gross,\n",
)
replace_once(
    "afuture/operations.py",
    "                integer_tracking_error=tracking_error,\n                single_product_actual_concentration=(\n",
    "                integer_tracking_error=tracking_error,\n"
    "                nonzero_product_count=len(nonzero_weights),\n"
    "                largest_product_absolute_gross_share=largest_share,\n"
    "                product_hhi=product_hhi,\n"
    "                estimated_margin_ratio=estimated_margin_ratio,\n"
    "                estimated_available_ratio=estimated_available_ratio,\n"
    "                contract_cap_hits=contract_cap_hits,\n"
    "                clipped_products=clipped_products,\n"
    "                risk_manager_preview={\"allowed\": risk_preview.allowed, \"reason\": risk_preview.reason},\n"
    "                execution_chain_commissioning_only=bool(\n"
    "                    float(config.directional.live_risk_scale) < 1.0\n"
    "                    or cap < STRESS90_POLICY.max_contract_lots\n"
    "                    or representation_parts\n"
    "                ),\n"
    "                portfolio_representation_warning=\"; \".join(representation_parts),\n"
    "                single_product_actual_concentration=(\n",
)

# Capacity CLI parser and implementation. It never checkpoints, saves state, issues permits, sends or cancels.
replace_once(
    "afuture/cli.py",
    "    status = sub.add_parser(\"status\", help=\"只读检查本地运行状态、证据文件和磁盘空间\")\n    status.add_argument(\"--config\", required=True)\n\n",
    "    status = sub.add_parser(\"status\", help=\"只读检查本地运行状态、证据文件和磁盘空间\")\n"
    "    status.add_argument(\"--config\", required=True)\n\n"
    "    capacity = sub.add_parser(\"stress90-capacity-report\", help=\"零报单诊断 Stress-90 账户容量与整数代表性\")\n"
    "    capacity.add_argument(\"--config\", required=True)\n"
    "    capacity.add_argument(\"--confirm-live\", action=\"store_true\")\n"
    "    capacity.add_argument(\"--output\", default=\"\")\n"
    "    capacity.add_argument(\"--startup-timeout\", type=float, default=60.0)\n"
    "    capacity.add_argument(\"--snapshot-wait\", type=float, default=12.0)\n\n",
)
# Insert read-only command before _run_status.
anchor = "\ndef _run_status(config) -> int:\n"
text = read("afuture/cli.py")
if text.count(anchor) != 1:
    raise RuntimeError("capacity CLI insertion anchor missing")
capacity_cli = r'''

def _run_stress90_capacity_report(config, args) -> int:
    """Build a fresh CTP-backed Stress-90 capacity report with no write capability."""

    from .broker.ctp import CtpBroker
    from .directional_activity import (
        DirectionalActivityStore,
        select_contracts_from_activity,
        validate_directional_activity_snapshot,
    )
    from .operations import build_doctor_report
    from .stress90_capacity import build_stress90_capacity_payload

    _validate_stress90_lifecycle_config(config)
    _require_production_confirmation(config, args)
    broker = CtpBroker(config.ctp)
    broker.start()
    try:
        _wait_until_ready(broker, args.startup_timeout)
        wait_for_fresh_snapshot(broker, args.snapshot_wait)
        account = broker.get_account()
        account.validate()
        trading_day = broker.get_trading_day()
        if account.trading_day != trading_day:
            raise RuntimeError("capacity report account/CTP trading day mismatch")
        catalog = broker.get_contract_catalog()
        positions = broker.get_positions()
        active_orders = broker.get_active_orders()
        snapshot = DirectionalActivityStore(
            Path(config.state_path).with_name("directional_activity.json")
        ).load()
        if snapshot is None:
            raise RuntimeError("capacity report requires completed directional activity")
        validate_directional_activity_snapshot(snapshot)
        catalog_by_symbol = {item.symbol: item for item in catalog}
        preferred = {
            catalog_by_symbol[position.symbol].product.upper(): position.symbol
            for position in positions
            if not position.empty and position.symbol in catalog_by_symbol
        }
        selected = select_contracts_from_activity(
            config.directional,
            catalog,
            snapshot,
            datetime.strptime(trading_day, "%Y%m%d").date(),
            preferred_symbols=preferred,
        )
        selected_symbols = {item.symbol for item in selected.values()}
        selected_symbols.update(
            position.symbol for position in positions if not position.empty
        )
        symbols = sorted(selected_symbols)
        quotes = _collect_doctor_quotes(
            broker,
            {symbol: catalog_by_symbol[symbol] for symbol in symbols if symbol in catalog_by_symbol},
            trading_day=trading_day,
            timeout_seconds=args.snapshot_wait,
        )
        metadata = broker.get_live_contract_specs(symbols, config.metadata_timeout_seconds)
        session_valid = False
        session_detail = "read-only complete session ownership evidence is unavailable"
        try:
            refresh = getattr(broker, "refresh_session_activity")
            evidence = refresh(timeout_seconds=max(0.1, float(args.snapshot_wait)))
            from .broker.ctp_session_query import validate_ctp_session_activity_ownership

            local_session_trades = tuple(broker.get_session_trades())
            validate_ctp_session_activity_ownership(evidence, local_session_trades)
            session_valid = True
            session_detail = "read-only complete CTP session activity ownership verified"
        except Exception as exc:
            session_detail = f"read-only session ownership failed: {exc}"
        report = build_doctor_report(
            config,
            broker_ready=broker.is_ready(),
            fresh_snapshot=True,
            trading_day=trading_day,
            account=account,
            positions=positions,
            active_order_count=len(active_orders),
            catalog=catalog,
            requested_symbols=symbols,
            metadata=metadata,
            quotes=quotes,
            session_trade_ownership_valid=session_valid,
            session_trade_ownership_detail=session_detail,
        )
        payload = build_stress90_capacity_payload(
            report,
            account_identity_digest=broker.get_account_identity_digest(),
            ctp_trading_day=trading_day,
        )
        if args.output:
            _write_json(payload, args.output)
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["hard_safety_passed"] else 2
    finally:
        broker.stop()

'''
write("afuture/cli.py", text.replace(anchor, textwrap.dedent(capacity_cli).rstrip() + anchor, 1))
replace_once(
    "afuture/cli.py",
    "    if args.command == \"doctor\":\n        return _run_doctor(config, args)\n\n",
    "    if args.command == \"doctor\":\n"
    "        return _run_doctor(config, args)\n\n"
    "    if args.command == \"stress90-capacity-report\":\n"
    "        return _run_stress90_capacity_report(config, args)\n\n",
)

# Add risk-overlay-only reactivation to the existing stress90-activate CLI/lifecycle coordinator.
# First, teach registry/evidence callbacks that this operation verifies but does not change account lineage.
replace_once(
    "afuture/cli.py",
    "    operation = lifecycle_transaction.operation\n    if operation == \"activation\":\n",
    "    operation = lifecycle_transaction.operation\n"
    "    if operation == \"risk_overlay_reactivation\":\n"
    "        registry.require_binding(\n"
    "            target_account, runtime_dir, lifecycle_transaction.policy_target.live_account_epoch\n"
    "        )\n"
    "        return\n"
    "    if operation == \"activation\":\n",
)
replace_once(
    "afuture/cli.py",
    "    account = lifecycle_transaction.account_identity_digest\n    epoch = lifecycle_transaction.policy_target.live_account_epoch\n",
    "    account = lifecycle_transaction.account_identity_digest\n"
    "    epoch = lifecycle_transaction.policy_target.live_account_epoch\n"
    "    if lifecycle_transaction.operation == \"risk_overlay_reactivation\":\n"
    "        # No account/runtime lineage changed; existing TDE remains authoritative.\n"
    "        return\n",
)
# Insert helper before _run_stress90_activate.
anchor = "\ndef _run_stress90_activate(config, args) -> int:\n"
text = read("afuture/cli.py")
if text.count(anchor) != 1:
    raise RuntimeError("stress90 activate insertion anchor missing")
risk_reactivation_cli = r'''

def _stress90_risk_overlay_reactivation_required(config, args) -> bool:
    from .stress90_risk_overlay import stress90_risk_overlay_digest

    paths = _stress90_lifecycle_paths(
        config,
        args.runtime_dir,
        shadow_account=bool(getattr(args, "shadow_account", False)),
    )
    record = StateStore(paths["state"]).load_record()
    if record is None:
        return False
    marker = record.state.strategy_states.get("directional_policy_identity")
    return bool(
        isinstance(marker, dict)
        and marker.get("policy_id") == "stress90"
        and marker.get("risk_overlay_digest")
        != stress90_risk_overlay_digest(config.directional, config.risk)
    )


def _run_stress90_risk_overlay_reactivation(config, args) -> int:
    from .broker.ctp import CtpBroker
    from .directional_policy_activation import rebind_stress90_risk_overlay_identity
    from .directional_stress90_execution import Stress90ExecutionIntentStore
    from .directional_stress90_state import Stress90PolicyStateStore, Stress90SeedStore
    from .journal import AuditJournal
    from .reconcile import compare_positions
    from .runtime_lease import AccountExclusiveRuntimeLease
    from .stress90_activation_permit import Stress90ActivationPermitStore
    from .stress90_lifecycle_transaction import (
        Stress90LifecycleTransactionError,
        Stress90LifecycleTransactionStore,
        require_matching_stress90_lifecycle_account_evidence,
    )
    from .stress90_risk_overlay import stress90_risk_overlay_digest

    operation_nonce = _require_lifecycle_operation_nonce(args)
    shadow_account = bool(getattr(args, "shadow_account", False))
    paths = _stress90_lifecycle_paths(
        config,
        args.runtime_dir,
        shadow_account=shadow_account,
    )
    store = StateStore(paths["state"])
    policy_store = Stress90PolicyStateStore(paths["runtime"] / "stress90_policy_state.json")
    seed_store = Stress90SeedStore(paths["runtime"] / "stress90_bootstrap_seed.json")
    lifecycle_store = Stress90LifecycleTransactionStore(
        paths["runtime"] / "stress90_lifecycle_transaction.json"
    )
    live_broker = CtpBroker(config.ctp)
    broker = (
        _build_persistent_shadow_broker(
            config,
            live_broker,
            paths["runtime"] / "shadow_broker_state.json",
        )
        if shadow_account
        else live_broker
    )
    account_identity = broker.get_account_identity_digest()
    lease = AccountExclusiveRuntimeLease(
        paths["runtime"], account_identity, role="stress90-risk-overlay-reactivation"
    )
    lease.acquire()
    try:
        state_record = store.load_required_record()
        if state_record.legacy:
            raise RuntimeError("risk-overlay reactivation rejects legacy generic state")
        state = state_record.state
        policy_record = policy_store.load_required_record()
        seed = seed_store.load_required()
        if policy_record.state.bootstrap_seed_digest != seed.seed_digest:
            raise RuntimeError("risk-overlay reactivation seed/policy identity mismatch")
        if (
            policy_record.state.live_account_identity_digest != account_identity
            or policy_record.state.live_account_epoch is None
        ):
            raise RuntimeError("risk-overlay reactivation account lineage mismatch")
        current_digest = stress90_risk_overlay_digest(config.directional, config.risk)
        marker = state.strategy_states.get("directional_policy_identity")
        if not isinstance(marker, dict) or marker.get("policy_id") != "stress90":
            raise RuntimeError("risk-overlay reactivation requires activated Stress-90 identity")
        if marker.get("risk_overlay_digest") == current_digest:
            raise RuntimeError("Stress-90 risk overlay is already bound to current configuration")
        _require_stress90_account_runtime_registry_binding(
            config,
            runtime_dir=paths["runtime"],
            broker=broker,
            policy_state=policy_record.state,
        )
        _require_stress90_order_journal_full_audit(paths["runtime"])
        _configure_stress90_lifecycle_order_journal(broker, paths["runtime"])
        _seed_state_aware_ctp_broker(
            live_broker,
            None if shadow_account else state,
            reject_ambiguous=not shadow_account,
        )
        broker.start()
    except BaseException:
        try:
            broker.stop()
        finally:
            lease.release()
        raise
    try:
        _wait_until_ready(broker, args.startup_timeout)
        wait_for_fresh_snapshot(broker, args.snapshot_wait)
        mechanical = _require_lifecycle_mechanical_snapshot(
            broker,
            runtime_dir=paths["runtime"],
            timeout_seconds=max(0.1, float(args.snapshot_wait)),
        )
        account = mechanical.account
        trading_day = mechanical.trading_day
        positions = list(mechanical.positions)
        active_orders = list(mechanical.active_orders)
        _require_verified_lifecycle_account_snapshot(
            account, operation="Stress-90 risk-overlay reactivation"
        )
        local_positions = store.positions_from_state(state)
        reconciliation = compare_positions(local_positions, positions)
        _require_lifecycle_resume_safety(
            state,
            broker_positions=positions,
            local_positions=local_positions,
            active_orders=active_orders,
            reconciliation_matched=reconciliation.matched,
            operation="Stress-90 risk-overlay reactivation",
        )
        if (
            trading_day != state.trading_day
            or state.last_account_trading_day != trading_day
            or account.trading_day != trading_day
            or float(account.equity) != float(state.last_account_equity)
            or float(account.deposit) != float(state.last_account_deposit)
            or float(account.withdrawal) != float(state.last_account_withdrawal)
            or account.settlement_id != state.last_account_settlement_id
        ):
            raise RuntimeError("risk-overlay reactivation fresh account snapshot changed")
        intent_record = Stress90ExecutionIntentStore(
            paths["runtime"] / "stress90_execution_intent.json"
        ).load_record()
        if (
            intent_record is not None
            and not intent_record.retired
            and intent_record.intent.target_trading_day >= trading_day
        ):
            raise RuntimeError(
                "risk-overlay reactivation is blocked by a current/future execution intent; converge or retire it under existing semantics first"
            )
        target = rebind_stress90_risk_overlay_identity(
            state,
            risk_overlay_digest=current_digest,
            operator_reason=args.operator_reason,
        )
        existing = lifecycle_store.load()
        if existing is not None and existing.status == "prepared":
            if (
                existing.operation != "risk_overlay_reactivation"
                or existing.operation_nonce != operation_nonce
                or existing.account_identity_digest != account_identity
                or existing.trading_day != trading_day
            ):
                raise Stress90LifecycleTransactionError(
                    "pending lifecycle transaction does not match risk-overlay reactivation"
                )
            require_matching_stress90_lifecycle_account_evidence(
                existing, account, account_identity_digest=account_identity
            )
            _require_lifecycle_operator_reason(existing, args.operator_reason)
            pending = existing
        else:
            pending = None
        _require_lifecycle_mechanical_snapshot_current(broker, mechanical)
        Stress90ActivationPermitStore(
            paths["runtime"] / "stress90_activation_permit.json"
        ).invalidate("Stress-90 risk overlay configuration changed")

        def prepare():
            if pending is not None:
                return pending
            return lifecycle_store.begin(
                operation="risk_overlay_reactivation",
                generic_source=state_record,
                policy_source=policy_record,
                generic_target=target,
                policy_target=policy_record.state,
                trading_day=trading_day,
                account_identity_digest=account_identity,
                account_snapshot=account,
                operation_nonce=operation_nonce,
                operator_reason=args.operator_reason,
            )

        completed = _commit_stress90_lifecycle_under_broker_fence(
            broker,
            transaction_store=lifecycle_store,
            generic_store=store,
            policy_store=policy_store,
            prepare_transaction=prepare,
            apply_registry_transition=lambda transaction: (
                _apply_stress90_account_runtime_registry_transition(
                    config,
                    runtime_dir=paths["runtime"],
                    lifecycle_transaction=transaction,
                )
            ),
            apply_evidence_transition=lambda transaction: (
                _apply_stress90_trading_day_evidence_transition(
                    config,
                    runtime_dir=paths["runtime"],
                    lifecycle_transaction=transaction,
                )
            ),
            precommit_check=lambda: _require_lifecycle_mechanical_snapshot_current(
                broker, mechanical
            ),
        )
        _record_lifecycle_completion_once(
            AuditJournal(paths["journal"]),
            "stress90_risk_overlay_reactivation_completed",
            completed.transaction_id,
            {
                "trading_day": trading_day,
                "operation_nonce": completed.operation_nonce,
                "account_identity_digest": account_identity,
                "risk_overlay_digest": current_digest,
                "kill_switch_remains_active": True,
            },
        )
        print(
            json.dumps(
                {
                    "reactivated": True,
                    "risk_overlay_only": True,
                    "risk_overlay_digest": current_digest,
                    "runtime_mode": RuntimeMode.HALTED.value,
                    "kill_switch": True,
                    "orders_sent": 0,
                    "cancels_sent": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        broker.stop()
        lease.release()

'''
write("afuture/cli.py", text.replace(anchor, textwrap.dedent(risk_reactivation_cli).rstrip() + anchor, 1))
# Existing activation gates include current risk digest and delegate mismatched activated Stress-90 markers.
replace_once(
    "afuture/cli.py",
    "    operation_nonce = _require_lifecycle_operation_nonce(args)\n    shadow_account = bool(getattr(args, \"shadow_account\", False))\n",
    "    operation_nonce = _require_lifecycle_operation_nonce(args)\n"
    "    if _stress90_risk_overlay_reactivation_required(config, args):\n"
    "        return _run_stress90_risk_overlay_reactivation(config, args)\n"
    "    shadow_account = bool(getattr(args, \"shadow_account\", False))\n",
)
replace_once(
    "afuture/cli.py",
    "        lifecycle_gates = {\n            \"broker_flat\": not any(not position.empty for position in positions),\n",
    "        from .stress90_risk_overlay import stress90_risk_overlay_digest\n\n"
    "        lifecycle_gates = {\n"
    "            \"broker_flat\": not any(not position.empty for position in positions),\n",
)
replace_once(
    "afuture/cli.py",
    "            \"account_identity_digest\": account_identity_digest,\n            \"operator_reason\": args.operator_reason,\n",
    "            \"account_identity_digest\": account_identity_digest,\n"
    "            \"risk_overlay_digest\": stress90_risk_overlay_digest(\n"
    "                config.directional, config.risk\n"
    "            ),\n"
    "            \"operator_reason\": args.operator_reason,\n",
)

# Lifecycle reactivation function call gets risk digest via lifecycle_gates automatically after signature update.
# Account rebind/migration marker checks accept legacy/current fields.

# Quality: bind risk overlay + intent and expose raw/scaled evidence without changing legacy callers.
replace_once(
    "afuture/quality.py",
    "        stress90_decision_digest: str,\n    ) -> None:\n",
    "        stress90_decision_digest: str,\n"
    "        stress90_risk_overlay_digest: str = \"\",\n"
    "        stress90_execution_intent_digest: str = \"\",\n"
    "        stress90_live_risk_scale: float = 1.0,\n"
    "        stress90_scaled_target: dict[str, float] | None = None,\n"
    "        stress90_scaled_integer_target: dict[str, int] | None = None,\n"
    "        stress90_raw_gross: float = 0.0,\n"
    "        stress90_scaled_gross: float = 0.0,\n"
    "        stress90_raw_turnover: float = 0.0,\n"
    "        stress90_scaled_turnover: float = 0.0,\n"
    "        stress90_scale_tracking_error: float = 0.0,\n"
    "        stress90_scale_zeroed_products: tuple[str, ...] = (),\n"
    "    ) -> None:\n",
)
replace_once(
    "afuture/quality.py",
    '                "stress90_decision_digest": str(stress90_decision_digest),\n',
    '                "stress90_decision_digest": str(stress90_decision_digest),\n'
    '                "stress90_risk_overlay_digest": str(stress90_risk_overlay_digest),\n'
    '                "stress90_execution_intent_digest": str(stress90_execution_intent_digest),\n'
    '                "stress90_live_risk_scale": float(stress90_live_risk_scale),\n'
    '                "stress90_scaled_target": {str(key): float(value) for key, value in (stress90_scaled_target or {}).items()},\n'
    '                "stress90_scaled_integer_target": {str(key): int(value) for key, value in (stress90_scaled_integer_target or {}).items()},\n'
    '                "stress90_raw_gross": float(stress90_raw_gross),\n'
    '                "stress90_scaled_gross": float(stress90_scaled_gross),\n'
    '                "stress90_raw_turnover": float(stress90_raw_turnover),\n'
    '                "stress90_scaled_turnover": float(stress90_scaled_turnover),\n'
    '                "stress90_scale_tracking_error": float(stress90_scale_tracking_error),\n'
    '                "stress90_scale_zeroed_products": [str(item) for item in stress90_scale_zeroed_products],\n',
)
replace_once(
    "afuture/quality.py",
    '                "rejected_count": sum(int(row.get("rejected_count", 0)) for row in d_cycles),\n            },\n',
    '                "rejected_count": sum(int(row.get("rejected_count", 0)) for row in d_cycles),\n'
    '                "risk_overlay_digests": sorted({str(row.get("stress90_risk_overlay_digest")) for row in stress90_decisions if row.get("stress90_risk_overlay_digest")}),\n'
    '                "execution_intent_digests": sorted({str(row.get("stress90_execution_intent_digest")) for row in stress90_decisions if row.get("stress90_execution_intent_digest")}),\n'
    '                "raw_target_gross_total": sum(float(row.get("stress90_raw_gross", 0.0)) for row in stress90_decisions),\n'
    '                "scaled_target_gross_total": sum(float(row.get("stress90_scaled_gross", 0.0)) for row in stress90_decisions),\n'
    '                "raw_turnover_notional": sum(float(row.get("stress90_raw_turnover", 0.0)) for row in stress90_decisions),\n'
    '                "scaled_turnover_notional": sum(float(row.get("stress90_scaled_turnover", 0.0)) for row in stress90_decisions),\n'
    '                "scale_induced_tracking_error_total": sum(float(row.get("stress90_scale_tracking_error", 0.0)) for row in stress90_decisions),\n'
    '                "scale_induced_zeroed_products": sorted({str(product) for row in stress90_decisions for product in row.get("stress90_scale_zeroed_products", [])}),\n'
    '            },\n',
)
# Runtime quality method accepts intent/current lots and logs raw/scaled metrics.
replace_once(
    "afuture/directional_stress90_runtime.py",
    "    def _record_stress90_quality(self, prepared, stages: Stress90LotStages) -> None:\n",
    "    def _record_stress90_quality(\n"
    "        self, prepared, stages: Stress90LotStages, *, current_lots, intent\n"
    "    ) -> None:\n",
)
replace_once(
    "afuture/directional_stress90_runtime.py",
    "        record_decision(\n            target_trading_day=prepared.target_trading_day,\n",
    "        scaled_weights = scale_stress90_product_weights(\n"
    "            prepared.survivor_weights, self.config.live_risk_scale\n"
    "        )\n"
    "        ticks_by_symbol = dict(self._ticks)\n"
    "        lot_notionals = {\n"
    "            symbol: float(ticks_by_symbol[symbol].mid_price) * float(self._specs[symbol].multiplier)\n"
    "            for symbol in set(stages.raw_integer_lots) | set(stages.scaled_integer_lots) | set(current_lots)\n"
    "            if symbol in ticks_by_symbol and symbol in self._specs\n"
    "        }\n"
    "        raw_turnover = sum(\n"
    "            abs(stages.raw_integer_lots.get(symbol, 0) - int(current_lots.get(symbol, 0)))\n"
    "            * lot_notionals.get(symbol, 0.0)\n"
    "            for symbol in set(stages.raw_integer_lots) | set(current_lots)\n"
    "        )\n"
    "        scaled_turnover = sum(\n"
    "            abs(stages.scaled_integer_lots.get(symbol, 0) - int(current_lots.get(symbol, 0)))\n"
    "            * lot_notionals.get(symbol, 0.0)\n"
    "            for symbol in set(stages.scaled_integer_lots) | set(current_lots)\n"
    "        )\n"
    "        zeroed = tuple(\n"
    "            sorted(\n"
    "                product\n"
    "                for product, value in scaled_weights.items()\n"
    "                if abs(value) > 1e-15\n"
    "                and not any(\n"
    "                    symbol in stages.scaled_integer_lots\n"
    "                    and self._catalog_by_symbol_product(symbol) == product\n"
    "                    for symbol in stages.scaled_integer_lots\n"
    "                )\n"
    "            )\n"
    "        )\n"
    "        record_decision(\n"
    "            target_trading_day=prepared.target_trading_day,\n",
)
# The helper above references a method that does not exist; replace zeroed calculation with selected-symbol product inference from intent impossible.
text = read("afuture/directional_stress90_runtime.py")
old = '''        zeroed = tuple(\n            sorted(\n                product\n                for product, value in scaled_weights.items()\n                if abs(value) > 1e-15\n                and not any(\n                    symbol in stages.scaled_integer_lots\n                    and self._catalog_by_symbol_product(symbol) == product\n                    for symbol in stages.scaled_integer_lots\n                )\n            )\n        )\n'''
new = '''        selected_products = {\n            item.product.upper()\n            for item in self._catalog\n            if item.symbol in stages.scaled_integer_lots\n        }\n        zeroed = tuple(\n            sorted(\n                product\n                for product, value in scaled_weights.items()\n                if abs(value) > 1e-15 and product not in selected_products\n            )\n        )\n'''
if old not in text:
    raise RuntimeError("quality zeroed replacement anchor missing")
write("afuture/directional_stress90_runtime.py", text.replace(old, new, 1))
replace_once(
    "afuture/directional_stress90_runtime.py",
    "            stress90_raw_integer_target=stages.raw_integer_lots,\n            stress90_margin_fitted_target=stages.margin_fitted_lots,\n",
    "            stress90_raw_integer_target=stages.raw_integer_lots,\n"
    "            stress90_margin_fitted_target=stages.margin_fitted_lots,\n",
)
replace_once(
    "afuture/directional_stress90_runtime.py",
    "            stress90_decision_digest=prepared.daily_decision_digest,\n        )\n",
    "            stress90_decision_digest=prepared.daily_decision_digest,\n"
    "            stress90_risk_overlay_digest=self.risk_overlay_digest,\n"
    "            stress90_execution_intent_digest=intent.source_digest,\n"
    "            stress90_live_risk_scale=self.config.live_risk_scale,\n"
    "            stress90_scaled_target=scaled_weights,\n"
    "            stress90_scaled_integer_target=stages.scaled_integer_lots,\n"
    "            stress90_raw_gross=sum(abs(float(value)) for value in prepared.survivor_weights.values()),\n"
    "            stress90_scaled_gross=sum(abs(float(value)) for value in scaled_weights.values()),\n"
    "            stress90_raw_turnover=raw_turnover,\n"
    "            stress90_scaled_turnover=scaled_turnover,\n"
    "            stress90_scale_tracking_error=sum(\n"
    "                abs(float(prepared.survivor_weights[product]) - scaled_weights[product])\n"
    "                for product in scaled_weights\n"
    "            ),\n"
    "            stress90_scale_zeroed_products=zeroed,\n"
    "        )\n",
)
replace_once(
    "afuture/directional_stress90_runtime.py",
    "        self._record_stress90_quality(prepared, stages)\n",
    "        self._record_stress90_quality(\n"
    "            prepared, stages, current_lots=current_lots, intent=intent\n"
    "        )\n",
)

# Example commissioning configuration is deliberately stricter than the policy envelope.
replace_once(
    "config/afuture.directional-stress90-live.example.toml",
    "max_gross_leverage = 2.0\n",
    "max_gross_leverage = 2.0\nlive_risk_scale = 0.05\n",
)
replace_once(
    "config/afuture.directional-stress90-live.example.toml",
    "max_contract_volume = 35\n# Stress-90 applies",
    "max_contract_volume = 1\n# Stress-90 applies",
)
replace_once(
    "config/afuture.directional-stress90-live.example.toml",
    "max_margin_ratio = 0.35\nmax_daily_loss_ratio = 0.05\nmax_total_drawdown_ratio = 0.30\n",
    "max_margin_ratio = 0.10\nmax_daily_loss_ratio = 0.01\nmax_total_drawdown_ratio = 0.05\n",
)
replace_once(
    "config/afuture.directional-stress90-live.example.toml",
    "max_contract_volume = 35\nmax_quote_age_seconds",
    "max_contract_volume = 1\nmax_quote_age_seconds",
)
replace_once(
    "config/afuture.directional-stress90-live.example.toml",
    "min_available_ratio = 0.25\n",
    "min_available_ratio = 0.80\n",
)
replace_once(
    "config/afuture.directional-stress90-live.example.toml",
    "max_orders_per_minute = 60\n",
    "max_orders_per_minute = 10\n",
)

# Existing authoritative docs only; no stage/version/handoff artifacts.
doc_sections = {
    "README.md": r'''
### Stress-90 commissioning risk overlay

Stress-90 live/Shadow production can set `directional.live_risk_scale` in `(0, 1]`. The default `1.0` preserves the historical production target; the scale is applied only after the immutable Base/OI/cost/survivor/HHI/drawdown decision and before integer lots and margin fitting. It never scales exits, reductions, hard-risk flattening, crash recovery or cancellation. Production state binds a separate canonical risk-overlay digest; changing any bound risk field fails closed until the existing `stress90-activate` lifecycle is run while HALTED, kill-switched, flat, reconciled and free of active orders.

`afuture stress90-capacity-report --config ... --confirm-live --output ...` is a zero-order, zero-cancel diagnostic that reuses Doctor contract selection, live metadata/cost evidence, the shared Stress-90 lot planner and RiskManager preview. It is capacity evidence, not capital activation approval.
''',
    "docs/configuration.md": r'''
## Stress-90 live risk scale and overlay identity

`directional.live_risk_scale` defaults to `1.0` and must be finite with `0 < value <= 1`. Only the Stress-90 live/Shadow production lot path consumes it; replay, bootstrap, historical candidate generation and `execution_aligned` ignore it. The production risk-overlay digest includes the scale, directional gross/contract/session-entry limits and all `RiskConfig` fields. A digest change is an identity change, not a hot reload: live/status/Doctor fail closed until explicit HALTED activation/reactivation rebinds it.

The checked-in Stress-90 live example is intentionally a personal commissioning starting point (`live_risk_scale=0.05`, one-lot contract caps, 10% margin, 80% available cash, 1% daily loss and 5% total drawdown). These values are not Alpha parameters or permanent policy requirements. Recalibrate them from real one-lot stress loss, live margin/commission and account equity before committing more capital.
''',
    "docs/live-trading.md": r'''
## Stress-90 risk-overlay change control

Never edit Stress-90 live risk fields under a RUNNING process. A configured/bound risk-overlay digest mismatch blocks live startup and invalidates the meaning of an old technical permit. To rebind, stop in `HALTED` with kill switch enabled, require Broker/local flatness, zero active orders and a fresh reconcile, then run the existing `stress90-activate` command with a new operation id and activation confirmation. The lifecycle coordinator changes only the overlay marker for this case; account epoch, strategy returns and historical candidate identity are not reset.

Before first money, run `stress90-capacity-report`. It performs no order or cancel calls and does not checkpoint generic/policy/intent state. Missing real margin or commission evidence is a hard failure. Representation warnings never loosen hard risk limits or automatically increase scale.
''',
    "docs/production-checklist.md": r'''
## Stress-90 commissioning overlay and account capacity

- [ ] `status` and `doctor` show identical configured and bound risk-overlay digests.
- [ ] `directional.live_risk_scale` is an intentional commissioning value; no value above `1` is accepted.
- [ ] Any risk-overlay change was rebound only from `HALTED`, kill-switch=true, Broker/local flat, zero active orders and fresh reconciliation.
- [ ] A prior same-day execution intent was not reinterpreted under a new overlay.
- [ ] `stress90-capacity-report` returned `orders_sent=0` and `cancels_sent=0` using real CTP margin/commission evidence.
- [ ] One-lot notional, buffered long/short margin, one-tick/spread cost and 15bp compatibility were reviewed for every selected non-zero product.
- [ ] Integer zeroing, margin/funding clipping, HHI/drawdown freezes, product HHI, largest product share and tracking error are acceptable for commissioning.
- [ ] The commissioning example was adjusted from measured one-lot stress and actual account equity; no warning was used to relax hard risk.
''',
    "docs/stress90-live-runbook.md": r'''
## Commissioning scale, overlay reactivation and capacity report

The repository live example now starts at `live_risk_scale=0.05` with one-lot caps and a materially tighter account envelope. This is only an execution-chain commissioning baseline. `live_risk_scale=1.0` retains current production sizing semantics; any smaller value multiplies survivor product weights before integer conversion, so a sufficiently small account/scale may legitimately produce an all-zero target.

After changing scale or any bound risk field, do not start live. Keep `HALTED`, kill switch on, Broker/local flat, no active orders, and run a fresh reconcile. Then use `stress90-activate` with the normal strong activation confirmation and a new 64-hex operation id. The same lifecycle coordinator records a risk-overlay-only reactivation and invalidates the prior technical permit. Current/future execution intent blocks the rebind rather than being reinterpreted.

Run the capacity diagnostic before Doctor permit issuance:

```bash
afuture stress90-capacity-report \
  --config /secure/path/afuture.directional-stress90.toml \
  --confirm-live \
  --output /secure/path/stress90-capacity.json
```

The command sends and cancels zero orders. Exit code `2` means hard safety evidence failed. Review raw/scaled weights and gross, raw/scaled/margin/freeze/final lots, real fee/margin/tick/spread cost, 15bp compatibility, margin/cash ratios, clipped products, concentration and tracking error. Portfolio representation warnings are diagnostic only and never expand risk or product scope.
''',
    "docs/troubleshooting.md": r'''
## Stress-90 risk overlay mismatch or capacity failure

If status/Doctor reports `stress90_risk_overlay_identity` failed, do not delete state or edit the bound digest. Stop the runtime, keep it HALTED/kill-switched, finish or retire any current execution intent under its original digest, verify Broker/local flatness and zero active orders, reconcile, then run the explicit `stress90-activate` lifecycle to bind the new overlay. A smaller scale is still an identity change because old orders must retain their original interpretation.

If `stress90-capacity-report` exits `2`, inspect `hard_safety_failures`, `risk_manager_preview`, missing contract capacity/cost evidence and clipped products. Missing margin or all-zero commission evidence is not treated as zero cost. Integer zeroing can be a valid safe target; it is not a reason to enlarge scale automatically.
''',
    "docs/documentation-index.md": r'''
### Stress-90 commissioning diagnostics

Risk-scale configuration and overlay identity are specified in `configuration.md` and `live-trading.md`; operational sequencing and the zero-order `stress90-capacity-report` are in `stress90-live-runbook.md`; production gates are in `production-checklist.md`; mismatch/fail-closed recovery is in `troubleshooting.md`.
''',
}
for path, section in doc_sections.items():
    append_once(path, "Stress-90 commissioning risk overlay" if path == "README.md" else "Stress-90 risk overlay", textwrap.dedent(section))

# ---------------------------------------------------------------------------
# Focused green verification. Full repository gates remain for the PR CI only.
# ---------------------------------------------------------------------------
for command in (
    ("python", "-m", "ruff", "format", "afuture", "tests/test_stress90_live_risk_capacity.py"),
    ("python", "-m", "ruff", "check", "--fix", "afuture", "tests/test_stress90_live_risk_capacity.py"),
):
    result = run(*command, check=False)
    if result.returncode != 0:
        raise RuntimeError("formatter/lint fix failed:\n" + result.stdout[-5000:])

focus = run(
    "python",
    "-m",
    "pytest",
    "-q",
    "tests/test_stress90_live_risk_capacity.py",
    "tests/test_directional_stress90_planner.py",
    "tests/test_directional_policy_activation.py",
    "tests/test_stress90_activation_permit.py",
    "tests/test_operations.py",
    check=False,
)
if focus.returncode != 0:
    raise RuntimeError("focused GREEN tests failed:\n" + focus.stdout[-12000:])
print("Focused GREEN passed:\n" + focus.stdout[-3000:])

lint = run("python", "-m", "ruff", "check", "afuture", "tests/test_stress90_live_risk_capacity.py", check=False)
if lint.returncode != 0:
    raise RuntimeError("focused ruff failed:\n" + lint.stdout[-6000:])
compile_result = run("python", "-m", "compileall", "-q", "afuture", check=False)
if compile_result.returncode != 0:
    raise RuntimeError("compileall failed:\n" + compile_result.stdout[-6000:])

print("live risk capacity implementation runner completed")
