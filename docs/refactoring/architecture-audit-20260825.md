# Architecture and Correctness Audit — 2026-08-25

## Scope and baseline

This audit covers remote `main` at `482455dc57bc6a134f45232e290b4a49c3f7073d` and inherits the Stress-80 checkpoint `b4207abb50aca1e39d5ebba3affc04765857251a`. The working branch was clean before the documentation-only design and plan commits. The inherited full test suite passes locally: **323 tests in 32.62 seconds** on the project virtual environment.

Repository inventory, excluding the virtual environment and caches:

| Item | Count |
| --- | ---: |
| Python package modules | 73 |
| Internal module dependency edges | 148 |
| Import cycles | 0 |
| Test modules | 70 |
| Repository Markdown files before this audit | 43 |

The largest modules are review signals, not automatic split targets:

| Module | Lines | Assessment |
| --- | ---: | --- |
| `afuture/directional_acceptance.py` | 1,183 | Cohesive deterministic acceptance/accounting core, but input preparation and data validation should be pure boundaries |
| `afuture/engine.py` | 1,077 | Broad runtime orchestrator with broker events, health, auto-universe, quality and persistence coordination; high coupling, but event order makes wholesale extraction risky |
| `afuture/cli.py` | 768 | Parser and command dispatch plus some construction glue; runtime construction already has a separate factory |
| `afuture/directional_runtime.py` | 727 | Directional orchestration plus execution-quality lifecycle; cohesive state transitions should remain together unless characterization proves a safe extraction |
| `afuture/auto.py` | 708 | Auto-universe lifecycle, selection and contract/spec acquisition; large but domain-cohesive |
| `afuture/broker/ctp.py` | 582 | Infrastructure adapter with protocol conversion and live metadata queries; boundary validation is incomplete |

No import cycle was found. The package does not need a directory-wide Clean Architecture migration. The high-value work is to make boundary contracts explicit and keep research dependencies from flowing into production runtime code.

## Resolution status

The findings below preserve the original reproductions. Checkpoint B fixed both P0s and both P1 correctness defects with regressions; Checkpoint C closed the static/type/alert observability findings; Checkpoint D established Markdown authority and consistency checks. No finding was closed by weakening a risk, data or test invariant.

## Current end-to-end behavior

### Calendar-spread path

1. `CtpBroker`, `SimBroker`, shadow, or replay adapters produce normalized `Tick`, `Order`, `Trade`, account, position-snapshot, connection and error events.
2. `TradingEngine` owns runtime ordering. It performs quote/data-quality and health checks, asks `CalendarSpreadStrategy` for an intention, and passes new-risk decisions through `RiskManager` and portfolio risk checks.
3. `PairExecutor` converts an allowed pair intention into leg orders. It owns leg sequencing, partial-fill recovery and cancellation policy; it does not declare fills itself.
4. Broker trade events are the fill truth. The engine and broker mirror apply trades to `PositionBook`; fees and execution quality are attributed from the accepted event.
5. Broker account snapshots are authoritative for equity/available/margin in live operation. Local position/accounting models support simulation, reconciliation and expected-state diagnostics.
6. Reject, connection, health and imbalance transitions can move the runtime to reduce-only or HALT. New risk is not permitted in those modes.
7. `StateStore`, journal, alerts, health monitor, reconciliation and reports expose or persist the resulting state.

### Directional execution and research path

1. Causal OHLC history and completed-day activity enter through directional providers. Contract metadata selects listed, liquid and sufficiently distant contracts.
2. Frozen policy modules create target product weights from information completed before the decision date.
3. `DirectionalPortfolioManager` maps weights to integer lots, submits reductions before openings, applies gross/margin/account gates, and records directional quality cycles.
4. `DirectionalProductionAcceptance` runs the deterministic account proxy: target lots, contract selection, reduction/opening order, fills, costs, cash, equity, PnL, margin, gross exposure, rejects, HALT and audit rows.
5. Frozen evaluators slice train, validation, OOS, prior and full-recent windows and apply stress assumptions. Stress-80 and Stress-90 are research checkpoints layered on the evaluator, not live-runtime activation.

## Dependency and ownership findings

The observed direction is broadly sound:

`models/config -> calculations and policies -> strategy/risk/execution -> runtime orchestration -> broker/persistence/CLI/reporting`.

Positive findings:

- No package import cycle or module-load-order dependency was detected.
- Core event and business records use enums/dataclasses instead of raw dictionaries at most stable boundaries.
- Strategy intentions are separate from execution and broker fill truth.
- Directional research-only candidates are not wired into the live runtime by PR #25.
- Expensive research workflows are manual milestone gates; ordinary CI remains deterministic.

Material architecture risks:

- `TradingEngine` coordinates too many collaborators and contains execution-quality accounting, but its responsibilities are coupled by event order. A speculative extraction could cause duplicate fill processing or persistence order changes. This audit chooses characterization and targeted cleanup instead of a large engine rewrite.
- Directional acceptance combines validated input preparation with deterministic account simulation. Pure input validation can be extracted without relocating the economic loop.
- CTP adapter code treats protocol defaults as business defaults. Infrastructure ambiguity currently crosses into domain events.
- Persistence decoding and sequence advancement are duplicated across `load` and `save`, allowing their integrity behavior to diverge.
- CLI construction overlaps with `runtime_factory` in places. Only proven duplicate construction should move; command parsing and dispatch stay in the CLI.

## Severity-ranked correctness inventory

### P0 — Position close-bucket corruption — fixed

`PositionBook.apply_trade` checks total long/short volume, then `_consume_long` or `_consume_short` subtracts the requested bucket without checking it. For SHFE/INE offsets, one bucket cannot borrow volume from another.

Minimal reproduction from the inherited baseline:

```text
initial: long_today=0, long_yesterday=2
trade:   SELL 1 CLOSE_TODAY
result:  long_today=-1, long_yesterday=2, long_total=1
```

Impact: the internal position can become impossible while aggregate volume appears plausible. Downstream reconciliation, close planning, exposure and recovery decisions can then use corrupt state. The fix must reject before mutation and preserve the original position.

### P0 — Unknown CTP values become economic events — fixed

`_convert_order` and `_on_trade` use default fallbacks. A synthetic order with unknown protocol values currently converts as:

```text
direction UNKNOWN -> SELL
offset FORCECLOSE -> CLOSE
type MARKET       -> LIMIT
status MYSTERY    -> REJECTED
```

Impact: an unsupported or newly introduced exchange/gateway value can be represented as a different economically meaningful instruction. This is silent semantic corruption at the broker boundary. Supported values must be explicit; unknown values must produce one contextual `broker_error` and no fabricated order/trade event.

### P1 — Corrupt restart state is silently replaced — fixed

`StateStore.save` catches every exception while reading an existing state, resets `sequence` to one, and atomically replaces the target. A file containing invalid JSON is therefore replaced with a valid new envelope:

```text
existing: corrupt JSON
save:     RuntimeState(trading_day="20260825")
result:   sequence=1, corruption_overwritten=true
```

Impact: restart evidence and sequence history disappear precisely when integrity is uncertain. This contradicts the class contract that checksum failures fail closed. One verified decoder must serve both load and save; a bad target must remain unchanged.

### P1 — Duplicate daily observations are resolved implicitly — fixed

`SinaContinuousOHLCProvider._load_one` calls `drop_duplicates("date", keep="last")`. Two rows for the same date are accepted as one, with the last row winning. The reproduction accepted two conflicting closes, `101` and `201`, and retained `201` without an integrity signal.

Impact: provider ordering—not a declared market-data rule—selects the historical truth. This can contaminate features, acceptance windows and evidence. Duplicate and non-monotonic causal dates must fail closed before sorting or joining.

### P1/P2 — Static type and exception boundaries are incomplete — fixed

Diagnostic MyPy found concrete inference and Optional errors in integer optimization, dynamic CTP objects, alert sinks, auto/strategy paths and CLI variables. Ruff found broad exception catches and import/format drift. These diagnostics are not all defects; the governed gate will fix semantic contract errors, isolate dynamic `Any` at adapters, and avoid strictness-only boilerplate.

### P2 — Documentation authority is ambiguous — fixed

`README.md`, `docs/architecture.md`, `docs/data-and-backtest.md` and `docs/production-checklist.md` still present older 109.0636%/28.9559% mechanics or an unmet Stress-80 target as current conclusions. `docs/stress90-final-evidence.md` says permanent CI is pending although PR #25 CI succeeded on Python 3.10 and 3.13.

Impact: a new engineer cannot distinguish the live runtime, the Stress-80 checkpoint and the later Stress-90 research checkpoint. Documentation will be classified and cross-linked rather than rewriting historical evidence as live truth.

## Quantitative correctness review priorities

The following invariants are already represented in code/tests and remain mandatory during changes:

- Features and activity snapshots use only completed observations available before the decision timestamp.
- Each matrix row owns independent account state. Train, validation, OOS and prior use frozen slices; `full_recent` deliberately aggregates overlapping subperiods and is not an independent holdout.
- Reductions execute before openings; rejects do not mutate positions; HALT and reduce-only do not increase risk.
- Broker fill events, not submitted requests, update positions.
- Realized/unrealized PnL, cash, equity, commission, turnover, gross exposure and margin use explicit contract multipliers and units.
- Target and realized gross stay within the frozen hard cap; margin rejection and HALT counts remain part of evidence acceptance.
- A reversal is a close followed by an open, not a net position assignment that bypasses execution/accounting.

The final adversarial review will recheck partial fills, duplicate events, cancellation, recovery, session/trading-day boundaries, NaN/inf/extreme values, zero position, exact margin thresholds and drawdown/HALT thresholds. No issue is reported as fixed without a regression or a deterministic reproduction.

## Markdown classification policy

The repository Markdown set will be classified during Checkpoint D:

- current authority: README, architecture, data/backtest, live/runbook, production checklist and documentation index;
- validated evidence: research and Stress-80/90 result records with immutable measurements;
- historical/superseded evidence: older experiments retained for provenance and marked at the top;
- development records: Superpowers specifications and plans;
- governance: `AGENTS.md`.

`.pytest_cache/README.md` and virtual-environment documentation are generated artifacts and are not repository documentation.

## Bounded architecture decision

The audit rejects a wholesale repackage. It will make these structural changes only:

1. unify state-envelope decoding and sequence validation inside `StateStore`;
2. centralize pure directional daily-index/value/window validation in one dependency-free module;
3. centralize explicit CTP enum conversion inside the adapter;
4. add static gates and correct concrete core typing/error defects;
5. retain current engine/evaluator facades and economic loops.

This is sufficient to remove demonstrated knowledge duplication and boundary ambiguity. Engine quality-lifecycle extraction, a repository/service hierarchy, or a broad CLI rewrite are explicitly out of scope unless later tests expose a correctness requirement that cannot be fixed locally.

## Pre-change verification evidence

```text
python -m pytest -q
323 passed in 32.62s

python -m pytest -q tests/test_legacy_contracts.py tests/test_hardening.py
21 passed

python -m compileall -q afuture
success
```

The design and implementation plan are stored under `docs/archive/development/`. Checkpoint B begins with regression tests for position, persistence, CTP conversion and duplicate causal data before implementation changes.
