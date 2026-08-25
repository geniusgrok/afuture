# Industrial Refactoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden `afuture` correctness, boundaries, maintainability, tests, and documentation while preserving the validated Stress-80 and Stress-90 economic behavior.

**Architecture:** Preserve the existing calendar-spread and directional runtime facades. Strengthen position, persistence, broker, and causal-data boundaries with explicit validation and typed failures; extract only pure validation/codec responsibilities whose behavior is protected by tests. Complete the work in five recoverable checkpoints with impact-driven verification and one final expensive evidence run.

**Tech Stack:** Python 3.10–3.13, dataclasses, pandas, NumPy, pytest, Ruff, MyPy, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-25-industrial-refactoring-design.md`

## Global Constraints

- Base SHA is `482455dc57bc6a134f45232e290b4a49c3f7073d`; `b4207abb50aca1e39d5ebba3affc04765857251a` and every valid later `main` commit are immutable inherited work.
- Do not activate the Stress-90 candidate in the live runtime.
- Do not change alpha, portfolio construction, position sizing, leverage, gross target, risk thresholds, drawdown policy, execution assumptions, commission, slippage, stress scenarios, or train/validation/OOS definitions except for an evidenced correctness fix.
- Every bug fix starts with a failing regression test and records whether valid economic behavior changes.
- Small changes receive touched tests and static checks; module milestones receive subsystem tests; the complete suite and evidence matrices run only on the final candidate.
- Existing CLI commands, configuration keys, event models, report fields, and evaluator entry points remain compatible.
- Unknown broker protocol values and corrupt state/data fail closed; invalid inputs may now raise explicit errors.
- Do not introduce repository/service/factory/interface layers without a demonstrated reduction in coupling.

## File map

| File | Responsibility after the change |
| --- | --- |
| `afuture/position.py` | Position mutation, SHFE/INE close-bucket validation, and non-negative bucket invariants |
| `afuture/state.py` | Runtime-state model, validated envelope decoding, monotonic sequence derivation, and atomic persistence |
| `afuture/broker/ctp.py` | CTP adapter and explicit conversion of supported protocol values |
| `afuture/directional_data_validation.py` | Pure validation of indexed causal research inputs and split boundaries |
| `afuture/directional_acceptance.py` | Acceptance preparation/simulation using validated data, with existing entry points preserved |
| `afuture/execution_aligned_runtime.py` | Execution-aligned history loading without implicit duplicate-date resolution |
| `pyproject.toml` | Development dependencies and bounded Ruff/MyPy policy |
| `.github/workflows/ci.yml` | Fast deterministic lint, format, type, compile, test, CLI, and replay gates |
| `README.md` and `docs/*.md` | Current authority, evidence/history classification, commands, invariants, and runbooks |
| `docs/refactoring/industrial-refactoring-report-20260825.md` | Final audit findings, fixes, validation evidence, compatibility, and remaining limitations |

---

### Task 1: Freeze baseline and architecture inventory — Checkpoint A

**Files:**
- Create: `docs/refactoring/architecture-audit-20260825.md`
- Modify: `docs/superpowers/plans/2026-08-25-industrial-refactoring.md`

**Interfaces:**
- Consumes: repository at baseline `482455dc57bc6a134f45232e290b4a49c3f7073d`.
- Produces: severity-ranked inventory, dependency graph facts, current data/decision flow, Markdown classification, and commands used by later tasks.

- [x] **Step 1: Record repository facts**

Run and copy stable findings—not raw verbose output—into the audit:

```bash
git status --short --branch
git log --oneline --decorate -5
find afuture -name '*.py' -type f | sort
find tests -name 'test_*.py' -type f | sort
find . -name '*.md' -not -path './.git/*' | sort
```

- [x] **Step 2: Record dependency and size diagnostics**

Use an AST-based import graph to confirm cycles and `wc -l` only as review signals. The document must explicitly identify `TradingEngine`, directional acceptance/runtime, CLI, persistence, broker adapters, and research-only modules, and must distinguish a large cohesive module from a God module.

- [x] **Step 3: Record the severity inventory**

Include these confirmed items with evidence and affected invariants:

```text
P0/P1  PositionBook permits a requested close bucket to become negative.
P1     StateStore can overwrite a corrupt state and restart sequence at one.
P1     CTP unknown direction/offset/type/status can become a valid sell/close/limit/rejected event.
P1     Duplicate research dates are implicitly resolved in execution-aligned loading.
P2     README/architecture/checklist mix live mechanics with later research checkpoints.
```

Add other findings only when a concrete code path, reproduction, or inconsistent contract is present.

- [x] **Step 4: Run the pre-change fast baseline**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_legacy_contracts.py tests/test_hardening.py tests/test_ctp_compat.py
.venv/bin/python -m compileall -q afuture
```

Expected: current baseline tests pass; the separate minimal reproductions in the audit demonstrate the defects.

- [x] **Step 5: Commit and publish Checkpoint A**

```bash
git add docs/refactoring/architecture-audit-20260825.md docs/superpowers/plans/2026-08-25-industrial-refactoring.md
git commit -m "docs: record industrial architecture audit"
```

Publish through the authorized GitHub branch and align the local branch without discarding work.

---

### Task 2: Enforce position close-bucket invariants

**Files:**
- Modify: `afuture/position.py`
- Create: `tests/test_position_invariants.py`

**Interfaces:**
- Consumes: `PositionBook.apply_trade(trade: Trade) -> float`.
- Produces: unchanged public signature; invalid bucket-specific closes raise `ValueError` before mutation.

- [x] **Step 1: Add failing long and short bucket tests**

Create helpers that build SHFE positions and trades, then assert both error and no mutation:

```python
@pytest.mark.parametrize(
    ("side", "offset", "message"),
    [
        (OrderSide.SELL, Offset.CLOSE_TODAY, "today long"),
        (OrderSide.SELL, Offset.CLOSE_YESTERDAY, "yesterday long"),
        (OrderSide.BUY, Offset.CLOSE_TODAY, "today short"),
        (OrderSide.BUY, Offset.CLOSE_YESTERDAY, "yesterday short"),
    ],
)
def test_bucket_specific_close_rejects_insufficient_bucket_without_mutation(side, offset, message):
    position = ContractPosition("cu2609", "SHFE", long_today=0, long_yesterday=2,
                                short_today=0, short_yesterday=2,
                                long_price=70000, short_price=70100)
    book = PositionBook([position])
    before = book.all()
    with pytest.raises(ValueError, match=message):
        book.apply_trade(make_trade(side=side, offset=offset, volume=1))
    assert book.all() == before
```

Add exact-boundary success cases and generic `CLOSE` depletion tests for long and short.

- [x] **Step 2: Verify the regression fails for the right reason**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_position_invariants.py
```

Expected: bucket-specific cases fail because a bucket becomes negative or no exception is raised; existing valid cases pass.

- [x] **Step 3: Implement pre-mutation validation**

Add a private validation method and call it before realized PnL or mutation:

```python
@staticmethod
def _validate_close_volume(position: ContractPosition, trade: Trade) -> None:
    if trade.side is OrderSide.SELL:
        total, today, yesterday, label = (
            position.long_total, position.long_today, position.long_yesterday, "long"
        )
    else:
        total, today, yesterday, label = (
            position.short_total, position.short_today, position.short_yesterday, "short"
        )
    available = {
        Offset.CLOSE: total,
        Offset.CLOSE_TODAY: today,
        Offset.CLOSE_YESTERDAY: yesterday,
    }[trade.offset]
    bucket = {
        Offset.CLOSE: "total",
        Offset.CLOSE_TODAY: "today",
        Offset.CLOSE_YESTERDAY: "yesterday",
    }[trade.offset]
    if trade.volume > available:
        raise ValueError(f"close volume exceeds {bucket} {label} position")
```

Reject non-finite or non-positive trade prices before opening or closing. Add a post-mutation assertion/helper that checks all four buckets are non-negative without changing valid paths.

- [x] **Step 4: Run affected accounting and execution tests**

```bash
.venv/bin/python -m pytest -q tests/test_position_invariants.py tests/test_legacy_contracts.py tests/test_integration.py tests/test_sim_event_causality.py
.venv/bin/python -m compileall -q afuture/position.py
```

Expected: all pass.

- [x] **Step 5: Commit the position fix**

```bash
git add afuture/position.py tests/test_position_invariants.py
git commit -m "fix: enforce position close bucket invariants"
```

---

### Task 3: Make runtime-state persistence fail closed

**Files:**
- Modify: `afuture/state.py`
- Create: `tests/test_state_integrity.py`
- Modify: `tests/test_hardening.py`

**Interfaces:**
- Produces: `StateIntegrityError(ValueError)` and unchanged `StateStore.load/save` signatures.
- Preserves: valid versioned envelopes and legacy unversioned `RuntimeState` JSON.

- [x] **Step 1: Add failing corruption and sequence tests**

```python
def test_save_refuses_to_replace_corrupt_existing_state(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text('{"schema_version": 2, "sequence": 7, "state": ', encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(StateIntegrityError, match="invalid state JSON"):
        StateStore(path).save(RuntimeState())
    assert path.read_bytes() == original

def test_save_increments_only_a_verified_sequence(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    store.save(RuntimeState(trading_day="20260825"))
    store.save(RuntimeState(trading_day="20260826"))
    assert json.loads(store.path.read_text(encoding="utf-8"))["sequence"] == 2
```

Also cover checksum mismatch, newer schema, non-positive/non-integer sequence, missing envelope fields, valid legacy migration, and original-file preservation.

- [x] **Step 2: Verify failures**

```bash
.venv/bin/python -m pytest -q tests/test_state_integrity.py tests/test_hardening.py::test_state_has_checksum_sequence_and_legacy_migration
```

Expected: corruption-on-save test fails because current code silently overwrites.

- [x] **Step 3: Implement one verified decoder**

Define:

```python
class StateIntegrityError(ValueError):
    """Persisted runtime state cannot be trusted or safely advanced."""

@dataclass(frozen=True)
class _DecodedState:
    state: RuntimeState
    sequence: int
    legacy: bool
```

Add `_read_verified() -> _DecodedState` that wraps JSON/type/key errors with `StateIntegrityError`, rejects newer schemas and invalid sequences, verifies the checksum before constructing `RuntimeState`, and returns `sequence=0, legacy=True` for a valid legacy document. `load()` returns its state. `save()` calls it when the target exists and derives `sequence + 1`; it never catches integrity errors.

- [x] **Step 4: Preserve atomic replacement and clean temporary files**

Write and flush the complete envelope to the same directory, then replace the target. If serialization or replace fails, unlink only the explicitly created temporary path in `finally`; never delete or truncate the target.

- [x] **Step 5: Run restart/persistence subsystem tests**

```bash
.venv/bin/python -m pytest -q tests/test_state_integrity.py tests/test_hardening.py tests/test_directional_restart.py tests/test_strategy_rejection_state.py
.venv/bin/python -m compileall -q afuture/state.py
```

Expected: all pass.

- [x] **Step 6: Commit the persistence fix**

```bash
git add afuture/state.py tests/test_state_integrity.py tests/test_hardening.py
git commit -m "fix: fail closed on corrupt runtime state"
```

---

### Task 4: Reject unknown CTP protocol values

**Files:**
- Modify: `afuture/broker/ctp.py`
- Modify: `tests/test_ctp_compat.py`

**Interfaces:**
- Produces private helpers `_direction_to_side`, `_offset_to_model`, `_type_to_model`, and `_status_to_model`, each returning the corresponding domain enum or raising `ValueError`.
- Preserves `BrokerEvent` shapes and supported VeighNa/CTP values.

- [x] **Step 1: Add conversion characterization and rejection tests**

Use `SimpleNamespace(name=...)` inputs and assert every supported mapping. Add:

```python
@pytest.mark.parametrize("field,value", [
    ("direction", "UNKNOWN"),
    ("offset", "FORCECLOSE"),
    ("type", "MARKET"),
    ("status", "MYSTERY"),
])
def test_ctp_order_conversion_rejects_unknown_protocol_value(field, value):
    broker = fake_runtime_broker()
    raw = valid_raw_order()
    setattr(raw, field, SimpleNamespace(name=value))
    with pytest.raises(ValueError, match=field):
        broker._convert_order(raw)
```

For `_on_trade`, assert one `broker_error` event, no `trade` event, and no mirror-position mutation for an unknown direction or offset.

- [x] **Step 2: Verify the tests fail**

```bash
.venv/bin/python -m pytest -q tests/test_ctp_compat.py
```

Expected: unknown direction/offset/type/status are currently defaulted instead of rejected.

- [x] **Step 3: Implement explicit converters**

Normalize enum names with a helper that rejects missing values. Use exhaustive maps:

```python
_DIRECTION_BY_NAME = {"LONG": OrderSide.BUY, "SHORT": OrderSide.SELL}
_OFFSET_BY_NAME = {
    "OPEN": Offset.OPEN,
    "CLOSE": Offset.CLOSE,
    "CLOSETODAY": Offset.CLOSE_TODAY,
    "CLOSEYESTERDAY": Offset.CLOSE_YESTERDAY,
}
_TYPE_BY_NAME = {"LIMIT": OrderType.LIMIT, "FAK": OrderType.FAK, "FOK": OrderType.FOK}
```

Map statuses explicitly from the runtime enum object. Do not use `dict.get(..., economic_default)`.

- [x] **Step 4: Make event handlers fail closed**

`_on_order` and `_on_trade` catch conversion `ValueError`, enqueue one contextual `broker_error`, and return before emitting an order/trade or mutating the position mirror. Position-mirror accounting failures still emit the existing error and trade event because the upstream trade itself is valid exchange truth.

- [x] **Step 5: Run broker and event-causality tests**

```bash
.venv/bin/python -m pytest -q tests/test_ctp_compat.py tests/test_sim_event_causality.py tests/test_integration.py tests/test_strategy_rejection_state.py
.venv/bin/python -m compileall -q afuture/broker/ctp.py
```

Expected: all pass.

- [x] **Step 6: Commit the broker fix**

```bash
git add afuture/broker/ctp.py tests/test_ctp_compat.py
git commit -m "fix: reject unknown CTP protocol values"
```

---

### Task 5: Validate causal directional data — Checkpoint B

**Files:**
- Create: `afuture/directional_data_validation.py`
- Create: `tests/test_directional_data_validation.py`
- Modify: `afuture/execution_aligned_runtime.py`
- Modify: `afuture/directional_acceptance.py`
- Modify: `tests/test_execution_aligned_runtime.py`
- Modify: `tests/test_directional_acceptance.py`

**Interfaces:**
- Produces `validate_daily_index(frame: pd.DataFrame, *, name: str) -> None`.
- Produces `validate_finite_columns(frame: pd.DataFrame, columns: Collection[str], *, name: str, positive: Collection[str] = ()) -> None`.
- Produces `validate_unique_keys(frame: pd.DataFrame, columns: Collection[str], *, name: str) -> None`.
- Does not add a generic non-overlap window validator: the frozen `full_recent` reporting window intentionally overlaps the disjoint train/validation/OOS slices, and their definitions remain owned by the evaluators.

- [x] **Step 1: Add failing pure validation tests**

```python
def test_daily_index_rejects_duplicate_and_non_monotonic_dates():
    duplicate = pd.DataFrame({"close": [1.0, 2.0]}, index=pd.to_datetime(["2026-01-02", "2026-01-02"]))
    with pytest.raises(ValueError, match="duplicate"):
        validate_daily_index(duplicate, name="signals")

def test_required_values_reject_nan_inf_and_non_positive_prices():
    frame = pd.DataFrame({"close": [1.0, np.inf, 0.0]})
    with pytest.raises(ValueError, match="finite"):
        validate_finite_columns(frame, ["close"], name="signals", positive=["close"])
```

Cover timezone-normalized dates, missing columns, empty data, duplicate business keys, non-finite values, and non-positive prices.

- [x] **Step 2: Verify pure tests fail because the module does not exist**

```bash
.venv/bin/python -m pytest -q tests/test_directional_data_validation.py
```

- [x] **Step 3: Implement pure validators**

Validators report the dataset name, offending field, and first offending date/value. They do not sort, deduplicate, forward-fill, or clip; callers decide how missing optional observations are represented.

- [x] **Step 4: Replace implicit duplicate resolution**

In `SinaContinuousOHLCProvider._load_one`, parse dates, reject invalid/duplicate dates, sort only after proving uniqueness, and preserve the existing return columns. Validate the joined index before returning `ExecutionAlignedSignalHistory`.

- [x] **Step 5: Validate acceptance inputs at the public boundary**

Call the pure validators before `prepare_contracts` and before simulation window slicing. Required price/notional columns must be finite and economically positive; signal/return columns may be zero but not non-finite. Preserve every valid-row calculation and existing evaluator entry point.

- [x] **Step 6: Run causal and acceptance subsystem tests**

```bash
.venv/bin/python -m pytest -q \
  tests/test_directional_data_validation.py \
  tests/test_execution_aligned_runtime.py \
  tests/test_directional_acceptance.py \
  tests/test_directional_acceptance_simulation.py \
  tests/test_causality_closure.py \
  tests/test_portfolio_time_alignment.py
```

Expected: all pass with no frozen valid-input result change.

- [x] **Step 7: Commit and publish Checkpoint B**

```bash
git add afuture/directional_data_validation.py afuture/execution_aligned_runtime.py \
  afuture/directional_acceptance.py tests/test_directional_data_validation.py \
  tests/test_execution_aligned_runtime.py tests/test_directional_acceptance.py
git commit -m "fix: validate causal directional inputs"
```

Publish the accumulated position, state, CTP, and data-integrity commits to the authorized branch.

---

### Task 6: Govern types, errors, and static checks — Checkpoint C

**Files:**
- Modify: `pyproject.toml`
- Modify: `.github/workflows/ci.yml`
- Modify: core files reported by MyPy/Ruff where the issue changes clarity or correctness
- Modify: corresponding focused tests

**Interfaces:**
- Produces reproducible commands `ruff check .`, `ruff format --check .`, and `mypy afuture`.
- Preserves runtime public APIs and Python 3.10 compatibility.

- [x] **Step 1: Add pinned tool ranges and policy**

Add to `project.optional-dependencies.dev`:

```toml
"mypy>=1.17,<2",
"ruff>=0.12,<1",
```

Configure Ruff for Python 3.10, 100-character lines, and high-signal rules `E4`, `E7`, `E9`, `F`, `I`, `B`, `UP`; explicitly ignore only rules with a recorded compatibility reason. Configure MyPy with `python_version = "3.10"`, `check_untyped_defs = true`, `warn_unused_ignores = true`, and pragmatic third-party import handling.

- [x] **Step 2: Capture static failures once**

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy afuture
```

Classify findings into correctness/type-contract fixes, mechanical formatting/import ordering, dynamic CTP boundary annotations, and low-value strictness. Do not weaken a rule merely to hide a real defect.

- [x] **Step 3: Fix semantic type and exception findings by cluster**

Fix concrete tuple inference in the directional integer optimizer; Optional narrowing in runtime/strategy/auto paths; alert sink protocol typing; CLI variable reuse; and broad/silent exception handling that can hide trading, state, or evidence errors. For dynamic VeighNa objects, isolate `Any` at the adapter boundary rather than spreading it into domain code.

After each cluster run its focused tests plus:

```bash
.venv/bin/ruff check <touched-files>
.venv/bin/mypy <touched-files>
```

- [x] **Step 4: Apply mechanical formatting as an isolated change**

Run:

```bash
.venv/bin/ruff check . --fix
.venv/bin/ruff format .
git diff --check
```

Review that this commit contains only formatting/import-order changes, then commit it separately:

```bash
git add afuture tests tools
git commit -m "style: apply repository Python formatting"
```

- [x] **Step 5: Add fast CI gates**

Before the full test step in `.github/workflows/ci.yml`, add:

```yaml
- run: ruff check .
- run: ruff format --check .
- run: mypy afuture
- run: python -m compileall -q afuture
```

Keep expensive research workflows manual.

- [x] **Step 6: Run the governed static surface and affected subsystem tests**

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy afuture
.venv/bin/python -m compileall -q afuture
.venv/bin/python -m pytest -q tests/test_cli_runtime_factory.py tests/test_cli_safety.py tests/test_auto_hardening.py tests/test_directional_integer_optimizer.py
```

Expected: all pass.

- [x] **Step 7: Commit and publish Checkpoint C**

```bash
git add pyproject.toml .github/workflows/ci.yml afuture tests
git commit -m "chore: enforce Python quality gates"
```

Publish the checkpoint.

---

### Task 7: Reconcile tests, comments, and Markdown authority — Checkpoint D

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/data-and-backtest.md`
- Modify: `docs/production-checklist.md`
- Modify: `docs/stress90-final-evidence.md`
- Modify/archive/delete: other `*.md` files only after classification evidence
- Create: `docs/documentation-index.md`
- Modify: comments/docstrings in touched Python modules

**Interfaces:**
- Produces a documentation entry point that separates current runtime authority, research checkpoints, decision/evidence records, and historical material.
- Preserves evidence numbers and does not imply that Stress-90 is live.

- [x] **Step 1: Classify every Markdown file**

In `docs/documentation-index.md`, list each Markdown path exactly once under:

```text
Current authority
Validated research/evidence record
Historical or superseded record
Development specification/plan
```

Delete only redundant/generated files with no unique evidence. Mark retained historical files at their top rather than rewriting history.

- [x] **Step 2: Rewrite the README as the authoritative entry point**

It must contain: purpose and production-readiness boundary, current baseline SHA/PR, calendar and directional architecture, install, configuration, validation, replay, research/backtest commands, test/static commands, risk invariants, module map, and documentation links. State explicitly that PR #25 Stress-90 is a validated research checkpoint and not live activation.

- [x] **Step 3: Correct architecture/data/runbook claims**

Replace old statements that Stress-80 remains unmet or that older 109.0636%/28.9559% mechanics are the latest research result. Preserve those values only in a clearly historical context. Update CI status in `docs/stress90-final-evidence.md` with the successful promotion workflow evidence.

- [x] **Step 4: Align comments with enforced invariants**

Document why close buckets cannot borrow from each other, why corrupt state blocks saving, why unknown CTP values do not get defaults, and why duplicate research dates fail. Remove comments that merely restate syntax or describe pre-fix behavior.

- [x] **Step 5: Add documentation consistency checks**

Add a small test/tool that verifies local Markdown links and backticked repository paths exist, documented CLI subcommands appear in `afuture --help`, and the current SHA/result status blocks occur only in authoritative/evidence contexts. It must exclude external URLs from offline validation.

- [x] **Step 6: Run documentation and public-contract tests**

```bash
.venv/bin/python -m pytest -q tests/test_naming.py tests/test_config_hardening.py tests/test_cli_runtime_factory.py tests/test_candidate_evidence.py tests/test_evidence_closure.py
.venv/bin/python -m afuture --help
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Expected: all pass.

- [x] **Step 7: Commit and publish Checkpoint D**

```bash
git add README.md docs afuture tests tools pyproject.toml
git commit -m "docs: establish authoritative project documentation"
```

Publish the checkpoint.

---

### Task 8: Final adversarial review and candidate validation — Checkpoint E

**Files:**
- Create: `docs/refactoring/industrial-refactoring-report-20260825.md`
- Modify: files exposed by substantive final-review defects only, with regression tests first

**Interfaces:**
- Produces the final validated candidate and complete audit report.

- [x] **Step 1: Perform a bounded adversarial review**

Review the final diff and current paths for P0/P1 issues in accounting, fill timing, realized/unrealized PnL, margin, gross/net exposure, reject/partial-fill/cancel, HALT, restart, duplicate events, causal shifts, split isolation, timestamp/session boundaries, and non-finite values. Record each finding as fixed with evidence, not applicable with reason, or a genuine limitation. Do not start P2/P3 refactoring.

- [x] **Step 2: Obtain and verify frozen evidence inputs**

Download the fixed PR #24/#25 GitHub workflow artifacts, record artifact IDs and digests, and place only reproducible runtime inputs under ignored `runtime/` paths. Do not commit large generated datasets.

- [x] **Step 3: Run the final static, package, CLI, and full test gates once**

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy afuture
.venv/bin/python -m compileall -q afuture
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/afuture validate --config config/afuture.example.toml
.venv/bin/afuture replay --config config/afuture.example.toml --data examples/sample_ticks.csv
.venv/bin/python -m afuture --help
```

Expected: every command succeeds and the complete pytest count is at least the inherited 323 tests plus new regressions.

- [x] **Step 4: Run the complete frozen economic evidence once**

Run the repository's fixed Stress-80 and Stress-90 evaluators against their verified artifacts. Compare annualized return, drawdown, gross peak, rejects, HALT, costs, turnover, and net-alpha efficiency with the design-spec table. Any material difference blocks acceptance until attributed to environment, numerical tolerance, or an explicitly documented bug fix.

- [x] **Step 5: Write the final report**

Include only evidenced content under:

```text
Executive Summary
Architecture Changes
Correctness Fixes
Risk / Trading Fixes
Code Quality
Testing
Documentation
Behavioral Compatibility
Backtest / Stress Comparison
Known Limitations
```

For every behavior-changing bug fix, give the failing scenario, invariant, regression test, corrected behavior, and matrix impact.

- [x] **Step 6: Review repository hygiene**

```bash
git diff origin/main...HEAD --check
git status --short
rg -n "breakpoint\(|pdb\.set_trace|print\(" afuture tests tools
rg -n "except Exception:\s*(pass)?$" afuture
```

Classify legitimate CLI output and contextual boundary catches; remove debug artifacts and temporary parameters. Ensure only the final report is uncommitted.

- [x] **Step 7: Apply the verification-before-completion gate**

Re-run only a failed command or its affected subsystem after a fix. Repeat the complete validation only if the fix changes shared behavior or invalidates earlier evidence.

- [x] **Step 8: Commit and publish Checkpoint E**

```bash
git add docs/refactoring/industrial-refactoring-report-20260825.md
git commit -m "docs: publish industrial refactoring validation report"
```

Publish the final candidate, verify the remote branch SHA and CI, request code review, then open a pull request into the then-current `main`. Do not merge until required checks succeed and the branch is confirmed not to drop later `main` work.
