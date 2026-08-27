from __future__ import annotations

import subprocess
from pathlib import Path


def read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    if text.count(old) != 1:
        raise RuntimeError(f"{path}: expected one patch anchor, found {text.count(old)}")
    write(path, text.replace(old, new, 1))


def run(*args: str) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, check=True)


# 1. Capacity report: validate complete CTP session evidence against the durable
# order journal, not against domain Trade rows.  This remains read-only.
replace_once(
    "afuture/cli.py",
    '''        session_valid = False
        session_detail = "read-only complete session ownership evidence is unavailable"
        try:
            refresh = broker.refresh_session_activity
            evidence = refresh(timeout_seconds=max(0.1, float(args.snapshot_wait)))
            from .broker.ctp_session_query import validate_ctp_session_activity_ownership

            local_session_trades = tuple(broker.get_session_trades())
            validate_ctp_session_activity_ownership(evidence, local_session_trades)
            session_valid = True
            session_detail = "read-only complete CTP session activity ownership verified"
        except Exception as exc:
            session_detail = f"read-only session ownership failed: {exc}"
''',
    '''        session_valid = False
        session_detail = "read-only complete session ownership evidence is unavailable"
        try:
            refresh = broker.refresh_session_activity
            evidence = refresh(timeout_seconds=max(0.1, float(args.snapshot_wait)))
            from .broker.ctp_order_journal import CtpOrderSubmissionJournal
            from .broker.ctp_session_query import validate_ctp_session_activity_ownership

            account_identity = broker.get_account_identity_digest()
            if (
                not isinstance(account_identity, str)
                or evidence.account_identity_digest != account_identity
                or evidence.trading_day != trading_day
            ):
                raise RuntimeError("capacity report CTP session identity mismatch")
            journal_entries = tuple(
                CtpOrderSubmissionJournal(
                    Path(config.state_path).parent / "stress90_ctp_orders.json"
                ).load_all_entries()
            )
            validate_ctp_session_activity_ownership(evidence, journal_entries)
            require_current = getattr(broker, "require_session_activity_evidence_current", None)
            if not callable(require_current):
                raise RuntimeError("capacity report cannot revalidate CTP session evidence")
            require_current(evidence)
            session_valid = True
            session_detail = "read-only complete CTP session activity ownership verified"
        except Exception as exc:
            session_detail = f"read-only session ownership failed: {exc}"
''',
)

# 2. Risk-overlay lifecycle: narrow Broker identity before it becomes a durable
# lifecycle argument or lease identity.
replace_once(
    "afuture/cli.py",
    '''    account_identity = broker.get_account_identity_digest()
    lease = AccountExclusiveRuntimeLease(
        paths["runtime"], account_identity, role="stress90-risk-overlay-reactivation"
    )
''',
    '''    raw_account_identity = broker.get_account_identity_digest()
    if (
        not isinstance(raw_account_identity, str)
        or re.fullmatch(r"[0-9a-f]{64}", raw_account_identity) is None
    ):
        raise RuntimeError("risk-overlay reactivation Broker account identity is invalid")
    account_identity = raw_account_identity
    lease = AccountExclusiveRuntimeLease(
        paths["runtime"], account_identity, role="stress90-risk-overlay-reactivation"
    )
''',
)

# 3. Preserve the factory's pre-activation construction contract and account/runtime
# registry error ordering.  Once generic state exists, overlay mismatch still blocks
# construction before any order-capable runtime is returned.
replace_once(
    "afuture/runtime_factory.py",
    '''            runtime_dir = Path(state_store.path).parent.resolve(strict=False)
            from .directional_policy_activation import require_directional_policy_identity
            from .directional_stress90_policy import STRESS90_POLICY
            from .directional_stress90_state import Stress90SeedStore
            from .stress90_risk_overlay import stress90_risk_overlay_digest

            generic = state_store.load_required_record()
            seed = Stress90SeedStore(runtime_dir / "stress90_bootstrap_seed.json").load_required()
            require_directional_policy_identity(
                generic.state,
                policy_id=STRESS90_POLICY.policy_id,
                policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
                products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
                bootstrap_seed_digest=seed.seed_digest,
                risk_overlay_digest=stress90_risk_overlay_digest(config.directional, config.risk),
            )
            _require_stress90_account_runtime_binding(
                config,
                broker,
                runtime_dir,
            )
''',
    '''            runtime_dir = Path(state_store.path).parent.resolve(strict=False)
            _require_stress90_account_runtime_binding(
                config,
                broker,
                runtime_dir,
            )
            generic = state_store.load_record()
            if generic is not None:
                from .directional_policy_activation import require_directional_policy_identity
                from .directional_stress90_policy import STRESS90_POLICY
                from .directional_stress90_state import Stress90SeedStore
                from .stress90_risk_overlay import stress90_risk_overlay_digest

                seed = Stress90SeedStore(
                    runtime_dir / "stress90_bootstrap_seed.json"
                ).load_required()
                require_directional_policy_identity(
                    generic.state,
                    policy_id=STRESS90_POLICY.policy_id,
                    policy_definition_digest=STRESS90_POLICY.policy_definition_digest,
                    products_manifest_digest=STRESS90_POLICY.products_manifest_digest,
                    bootstrap_seed_digest=seed.seed_digest,
                    risk_overlay_digest=stress90_risk_overlay_digest(
                        config.directional, config.risk
                    ),
                )
''',
)

# 4. Direct regression tests that construct an intent must bind the manager's
# actual overlay; the production runtime already does so.
path = "tests/test_directional_stress90_fail_closed.py"
text = read(path)
old = '''        account_epoch=_ACCOUNT_EPOCH,
        current_lots='''
new = '''        account_epoch=_ACCOUNT_EPOCH,
        risk_overlay_digest=manager.risk_overlay_digest,
        current_lots='''
count = text.count(old)
if count < 1:
    raise RuntimeError("fail-closed intent overlay anchors are missing")
write(path, text.replace(old, new))

# 5. README command index uses literal command lines as the documentation authority.
replace_once(
    "README.md",
    '''afuture stress90-prepare-decision --help
afuture stress90-registry-init --help
''',
    '''afuture stress90-prepare-decision --help
afuture stress90-capacity-report --help
afuture stress90-registry-init --help
''',
)

# 6. Capacity cost facts expose both cash-per-lot fees and bps, plus an unambiguous
# spread_bps alias.
replace_once(
    "afuture/operations.py",
    '''    open_fee = fee_bps(spec.fee.open_fixed, spec.fee.open_rate)
    close_fee = fee_bps(spec.fee.close_fixed, spec.fee.close_rate)
    close_today_fee = fee_bps(
        spec.fee.close_today_fixed,
        spec.fee.close_today_rate,
    )
''',
    '''    open_fee_per_lot = float(spec.fee.open_fixed) + float(spec.fee.open_rate) * notional
    close_fee_per_lot = float(spec.fee.close_fixed) + float(spec.fee.close_rate) * notional
    close_today_fee_per_lot = (
        float(spec.fee.close_today_fixed) + float(spec.fee.close_today_rate) * notional
    )
    open_fee = fee_bps(spec.fee.open_fixed, spec.fee.open_rate)
    close_fee = fee_bps(spec.fee.close_fixed, spec.fee.close_rate)
    close_today_fee = fee_bps(
        spec.fee.close_today_fixed,
        spec.fee.close_today_rate,
    )
''',
)
replace_once(
    "afuture/operations.py",
    '''        "mid_price": mid,
        "open_fee_bps": open_fee,
        "close_yesterday_fee_bps": close_fee,
        "close_today_fee_bps": close_today_fee,
        "one_tick_bps": one_tick,
        "bid_ask_bps": bid_ask,
''',
    '''        "mid_price": mid,
        "open_fee_per_lot": open_fee_per_lot,
        "close_yesterday_fee_per_lot": close_fee_per_lot,
        "close_today_fee_per_lot": close_today_fee_per_lot,
        "open_fee_bps": open_fee,
        "close_yesterday_fee_bps": close_fee,
        "close_today_fee_bps": close_today_fee,
        "one_tick_bps": one_tick,
        "bid_ask_bps": bid_ask,
        "spread_bps": bid_ask,
''',
)

# 7. Local status cannot invent live quote/spec mechanics.  Surface locally provable
# raw/scaled gross and persisted plan stages, and explicitly label live-only metrics.
replace_once(
    "afuture/operations.py",
    '''            target_gross=(
                sum(abs(float(value)) for value in prepared.survivor_weights.values())
                if prepared is not None
                else sum(abs(float(value)) for value in policy_state.last_survivor_weights.values())
            ),
        )
''',
    '''            target_gross=(
                sum(abs(float(value)) for value in prepared.survivor_weights.values())
                if prepared is not None
                else sum(abs(float(value)) for value in policy_state.last_survivor_weights.values())
            ),
        )
        raw_target_gross = float(facts["target_gross"])
        facts["raw_target_gross"] = raw_target_gross
        facts["scaled_target_gross"] = raw_target_gross * float(config.directional.live_risk_scale)
        facts["raw_product_weights"] = (
            dict(prepared.survivor_weights)
            if prepared is not None
            else dict(policy_state.last_survivor_weights)
        )
        facts["scaled_product_weights"] = {
            product: float(weight) * float(config.directional.live_risk_scale)
            for product, weight in facts["raw_product_weights"].items()
        }
''',
)
replace_once(
    "afuture/operations.py",
    '''        if intent_record is not None:
            facts["target_lots"] = (
                {}
                if intent_record.retired
                else dict(intent_record.intent.initial_margin_fitted_lots)
            )
''',
    '''        if intent_record is not None:
            facts["target_lots"] = (
                {}
                if intent_record.retired
                else dict(intent_record.intent.initial_margin_fitted_lots)
            )
            facts["raw_integer_lots"] = None
            facts["scaled_integer_lots"] = None
            facts["margin_fitted_lots"] = (
                {} if intent_record.retired else dict(intent_record.intent.initial_margin_fitted_lots)
            )
            facts["final_lots"] = (
                {} if intent_record.retired else dict(intent_record.intent.freeze_authorized_lots)
            )
            facts["live_plan_metrics_available"] = False
            facts["live_plan_metrics_reason"] = (
                "fresh CTP quotes/specs are required; run doctor or stress90-capacity-report"
            )
            facts["integer_tracking_error"] = None
            facts["live_cost_compatibility"] = None
            facts["estimated_margin_ratio"] = None
            facts["estimated_available_ratio"] = None
''',
)

# 8. Final CI includes every explicitly requested stable-candidate gate not already
# present in the repository workflow.
ci = read(".github/workflows/ci.yml")
needle = '''      - run: python -m pip install -e ".[dev]" -c constraints/core-dev.txt
      - name: Lint
'''
if ci.count(needle) != 1:
    raise RuntimeError("quality install anchor mismatch")
ci = ci.replace(
    needle,
    '''      - run: python -m pip install -e ".[dev]" -c constraints/core-dev.txt
      - run: python -m pip check
      - name: Lint
''',
    1,
)
needle = '''      - name: Validate directional live example
        env:
          AFUTURE_CTP_USER: test-user
          AFUTURE_CTP_PASSWORD: test-password
          AFUTURE_CTP_BROKER: test-broker
        run: afuture validate --config config/afuture.directional-live.example.toml

      - name: Directional production smoke
'''
if ci.count(needle) != 1:
    raise RuntimeError("directional example CI anchor mismatch")
ci = ci.replace(
    needle,
    '''      - name: Validate directional live example
        env:
          AFUTURE_CTP_USER: test-user
          AFUTURE_CTP_PASSWORD: test-password
          AFUTURE_CTP_BROKER: test-broker
        run: afuture validate --config config/afuture.directional-live.example.toml
      - name: Validate Stress-90 commissioning example
        env:
          AFUTURE_CTP_USER: test-user
          AFUTURE_CTP_PASSWORD: test-password
          AFUTURE_CTP_BROKER: test-broker
          AFUTURE_CTP_ACCOUNT_ID: test-account
          AFUTURE_CTP_CURRENCY_ID: CNY
        run: afuture validate --config config/afuture.directional-stress90-live.example.toml

      - name: Directional production smoke
''',
    1,
)
needle = '''      - run: python tools/test_directional_production_mechanics.py
      - run: python -m afuture --help
'''
if ci.count(needle) != 1:
    raise RuntimeError("final mechanics CI anchor mismatch")
ci = ci.replace(
    needle,
    '''      - name: Shadow zero-write smoke
        run: python -m pytest -q tests/test_cli_safety.py -k shadow
      - name: Stress-90 production mechanics
        run: >-
          python -m pytest -q
          tests/test_stress90_live_risk_capacity.py
          tests/test_directional_stress90_planner.py
          tests/test_directional_stress90_fail_closed.py
      - run: python tools/test_directional_production_mechanics.py
      - run: python -m afuture --help
''',
    1,
)
write(".github/workflows/ci.yml", ci)

# 9. Focused verification only.  The PR workflow will perform the single final full
# stable-candidate run after this checkpoint is pushed.
run("python", "-m", "ruff", "format", "afuture/cli.py", "afuture/runtime_factory.py", "afuture/operations.py", "tests/test_directional_stress90_fail_closed.py")
run("python", "-m", "ruff", "check", "afuture/cli.py", "afuture/runtime_factory.py", "afuture/operations.py", "tests/test_directional_stress90_fail_closed.py")
run("python", "-m", "mypy", "afuture")
run(
    "python",
    "-m",
    "pytest",
    "-q",
    "tests/test_cli_runtime_factory.py",
    "tests/test_directional_stress90_fail_closed.py",
    "tests/test_documentation_consistency.py",
    "tests/test_stress90_live_risk_capacity.py",
)
run("python", "-m", "compileall", "-q", "afuture")
print("focused live-risk-capacity fix verification passed", flush=True)
