# Industrial Refactoring Design

Status: approved and implemented through Checkpoint D on 2026-08-25; final Checkpoint E remains governed by the implementation plan.

## 1. Purpose

This change hardens `afuture` as a long-lived research and trading codebase without changing validated strategy economics. The governing order is correctness, clarity, maintainability, testability, architecture, performance, then style.

The refactor uses a boundary-hardening approach: keep the existing domain model and validated strategy paths, strengthen invariants where data or state crosses a boundary, and extract responsibilities only where doing so removes a demonstrated coupling or duplication. It is not a repository-wide rewrite or a migration to a framework-shaped architecture.

## 2. Immutable baseline and behavioral contract

The branch starts from remote `main` at:

- current baseline: `482455dc57bc6a134f45232e290b4a49c3f7073d`, PR #25, validated causal Stress-90 leadership freeze;
- inherited checkpoint: `b4207abb50aca1e39d5ebba3affc04765857251a`, PR #24, validated Stress-80 drawdown-reserve candidate.

Both checkpoints are protected. The work must not restore older parameters, strategies, execution assumptions, risk limits, or research splits.

The final candidate will reproduce or explain differences from these authoritative results:

| Matrix | Annualized | Max drawdown | Gross peak | Margin rejects | HALT | Net alpha / turnover |
| --- | ---: | ---: | ---: | ---: | --- | ---: |
| Stress-80 Base full_recent | 128.119261% | — | — | — | — | — |
| Stress-80 Stress full_recent | 80.067891% | 29.727688% | 1.683773x | 0 | false | 30.990722 bps |
| Stress-80 train | 10.177351% | — | — | — | — | — |
| Stress-80 validation | 511.519900% | — | — | — | — | — |
| Stress-80 OOS | 44.102450% | — | — | — | — | — |
| Stress-90 Base full_recent | 156.881655% | 15.708467% | 1.983123x | — | — | — |
| Stress-90 Stress full_recent | 112.100053% | 14.567214% | 1.670510x | 0 | false | 78.274862 bps |
| Stress-90 train | 28.891985% | — | — | — | — | — |
| Stress-90 validation | 512.267292% | — | — | — | — | — |
| Stress-90 OOS | 102.808956% | — | — | — | — | — |

The Stress-90 candidate remains research-only unless the existing code and evidence explicitly say otherwise. This refactor must not activate it in the live runtime.

## 3. Current system model

### 3.1 Calendar-spread runtime

1. Market data enters through a `Broker` implementation (`CTPBroker`, `SimBroker`, or shadow/replay paths).
2. `TradingEngine` receives normalized ticks, order updates, trades, account snapshots, position snapshots, connection state, and broker errors.
3. Data-quality gates validate freshness, continuity, and tradability before strategy evaluation.
4. Strategy code forms pair-level intentions; risk and execution code turn permitted intentions into two-leg order actions.
5. Broker events are the source of truth for fills. `PositionBook` applies trades; engine state tracks paired execution, rejects, hedge recovery, and shutdown behavior.
6. Fees, realized and unrealized PnL, margin, gross/net exposure, and quality attribution flow into state, journal, reconciliation, health, alerts, and reports.
7. `StateStore` persists restart state through a checksummed envelope and atomic replacement.

### 3.2 Directional research and execution-aligned runtime

1. Historical bars, contract metadata, and configured date windows enter through the data/research loaders.
2. Causal feature and candidate builders create daily signals using information available at or before each decision timestamp.
3. Portfolio construction, integer sizing, execution assumptions, risk governors, drawdown reserves, concentration freezes, gross/margin constraints, and HALT/reject state produce daily target positions and fills.
4. Accounting updates cash, positions, PnL, equity, exposure, turnover, costs, margin rejects, and risk state.
5. Acceptance evaluators isolate train, validation, OOS, prior, and full-recent windows and produce evidence matrices.
6. Stress-80 and Stress-90 modules extend the frozen evaluators; they are not implicit replacements for the live runtime.

### 3.3 Dependency direction

The target direction is:

`models/config -> pure domain calculations -> strategy/risk/execution -> runtime orchestration -> broker, persistence, CLI and reporting adapters`.

Research evaluators may compose production calculations, but production runtime code must not depend on acceptance reports or research-only candidates. CLI code may select and invoke public runtime factories, but it must not duplicate construction or mutate internal runtime state.

## 4. Findings that drive the design

The audit found no import cycle in the current Python package graph. The main risks are concentrated in oversized orchestration/evaluator modules and weak validation at state and broker boundaries, not in a need for a wholesale directory rewrite.

Confirmed correctness defects:

1. `PositionBook.apply_trade` validates aggregate closable volume but not the requested SHFE/INE close bucket. `CLOSE_TODAY` can therefore drive today's volume negative while yesterday's volume remains positive.
2. `StateStore.save` silently replaces a corrupt existing state and restarts its sequence at one when no valid in-memory sequence is available. That contradicts the fail-closed persistence contract and hides state loss.

High-risk boundary defect to cover and fix:

3. CTP conversion paths interpret unknown direction or offset values as sell/close fallbacks. Unknown exchange values must not be converted into economically meaningful events.

Additional audit targets include duplicate or non-monotonic research dates, non-finite prices and portfolio values, reject/HALT state transitions, event idempotency, and unit consistency across contracts, notional, margin, percentage, and decimal values.

Documentation currently mixes three different truths: live production mechanics, the Stress-80 research checkpoint, and the later Stress-90 research checkpoint. Some evidence files also describe CI as pending although the promotion workflow completed successfully.

## 5. Proposed changes

### 5.1 Position and accounting invariants

- Validate `CLOSE_TODAY` against today's bucket and `CLOSE_YESTERDAY` against yesterday's bucket before mutation.
- Keep generic `CLOSE` behavior based on total closable volume and the existing exchange-specific depletion order.
- Reject invalid volume, price, direction, and offset combinations before changing position state.
- Assert the post-trade invariant that every position bucket is a non-negative integer.
- Add regression tests for both long and short positions, both bucket-specific offsets, exact-boundary closes, insufficient-volume closes, reversals, and no-mutation-on-error.

These changes only reject states that are already invalid under the exchange offset semantics. Valid fills retain their current accounting behavior.

### 5.2 Restart-state integrity

- Introduce a specific state-integrity exception for checksum, schema, and sequence failures.
- Validate an existing state before deriving the next sequence. A corrupt or unsupported state must remain untouched and must block save.
- Preserve atomic temp-file replacement; add durability steps only where portable and testable.
- Define the single-writer assumption explicitly and detect sequence regression or unexpected replacement where feasible.
- Keep restart compatibility for valid envelopes supported by the current schema contract.
- Test corruption, truncated JSON, checksum mismatch, sequence increments, atomic replacement, and preservation of the original file on failure.

### 5.3 CTP fail-closed conversion

- Replace implicit `else -> SELL/CLOSE` mappings with explicit protocol-value conversion helpers.
- Unknown direction, offset, order type, or status values produce a contextual broker error and no false trade/order event.
- Preserve supported CTP variants and current public event types.
- Test every supported value and representative unknown values without requiring a live CTP session.

### 5.4 Research data and acceptance integrity

- Centralize validation for causal daily inputs used by the production evaluators: unique and monotonic trading dates, expected index alignment, finite required values, positive prices where economically required, and valid split boundaries.
- Reject duplicate dates rather than resolving them implicitly.
- Ensure joins and shifts cannot introduce future observations into a decision row.
- Keep train, validation, OOS, prior, and full-recent definitions unchanged.
- Add targeted tests that prove invalid inputs fail closed and that validation does not alter valid frozen evidence.

### 5.5 Bounded responsibility extraction

Extraction is permitted only after characterization tests protect current behavior.

- `TradingEngine`: extract pair-execution quality lifecycle calculations if the resulting component owns a coherent state transition and removes duplicated accounting. Broker event order, public methods, and engine behavior remain stable.
- Directional acceptance: extract pure data preparation, window validation, and result-accounting helpers where they are reused. Existing evaluator entry points remain compatibility facades.
- CLI: retain parsing and dispatch; move construction only where it duplicates `runtime_factory`. Do not introduce a service/factory hierarchy.
- Configuration: keep typed dataclasses and existing configuration files; centralize validation messages or conversions only where semantics are duplicated.

If an extraction increases indirection or cannot be verified with focused tests, it will be omitted and recorded as deliberately unchanged.

### 5.6 Types, errors, and observability

- Replace ambiguous tuples or `dict[str, Any]` at stable core boundaries only when an existing domain model does not already cover the concept.
- Correct concrete typing defects in core position, state, risk, execution, directional optimizer, and runtime paths.
- Define a practical MyPy gate for the governed package surface and expand it only after current errors are classified.
- Eliminate silent catches that can hide trading, accounting, persistence, or research-integrity failures. Boundary catches must attach actionable context and preserve the original cause.
- Keep operational best-effort behavior only for explicitly non-critical telemetry, with bounded logging and no repeated stack spam.
- Add Ruff lint and format checks with a deliberately small rule set. Repository-wide formatting, if required, is isolated in one mechanical commit so semantic review remains possible.

### 5.7 Documentation authority model

All Markdown files will be classified as current authority, decision/evidence record, experiment record, or historical/archived material.

- `README.md` becomes the new-engineer entry point and accurately distinguishes the live/runtime path from current research checkpoints.
- `docs/architecture.md` documents module boundaries, the two execution paths, dependency direction, state ownership, and invariants.
- Data/backtest, configuration, risk, live, and runbook documents must use the implemented names, defaults, units, commands, and behavior.
- Stress-80 and Stress-90 evidence remains immutable historical evidence except for clear status metadata and cross-links. It must not be rewritten to imply live activation.
- Stale claims, including completed CI described as pending and old research targets described as current behavior, will be corrected or explicitly marked historical.
- A final refactoring report will record defects, architecture decisions, tests, behavior compatibility, evidence reproduction, and genuine limitations.

## 6. Public API and compatibility

No intentional breaking public API change is planned. Existing imports, CLI commands, configuration keys, event models, report fields, and evaluator entry points remain available unless the audit proves one is unreferenced and misleading. Any contemplated removal requires repository and history evidence; uncertain interfaces remain.

Strict validation can cause previously accepted invalid inputs to raise an explicit error. That is an intentional correctness change, not an economic-policy change, and will be documented with regression evidence.

## 7. Verification strategy

### Level 1: each focused change

- write a failing regression or characterization test first;
- run the directly affected tests;
- run import/compile, lint, format, and type checks for touched modules.

### Level 2: module milestones

- position/accounting/execution/risk subsystem tests;
- broker/state/restart integration tests;
- directional causality, acceptance, stress gate, and portfolio tests;
- CLI/config/runtime-factory tests;
- Markdown path, link, and command checks.

### Level 3: final candidate

Run once after behavior-changing work has stopped:

1. full unit and integration suite;
2. Ruff lint and formatting check;
3. MyPy governed-surface check;
4. compile/import and packaging validation;
5. CLI smoke and replay/config validation;
6. full backtest and Stress-80/Stress-90 acceptance matrices from fixed inputs;
7. baseline comparison with tolerances and explicit investigation of material differences;
8. code/comment/configuration/document consistency review;
9. final diff, debug-artifact, and clean-status review.

A full validation is repeated only after a later behavioral change, a substantive fix found by the full run, or a change to shared infrastructure that invalidates prior evidence.

## 8. Checkpoints and change control

- Checkpoint A: architecture audit, characterization tests, and bounded structural work.
- Checkpoint B: correctness fixes for position, persistence, broker conversion, data, and state transitions.
- Checkpoint C: typing, error handling, observability, duplication, and targeted readability work.
- Checkpoint D: test governance, comments, Markdown authority cleanup, and consistency checks.
- Checkpoint E: adversarial review, final validation, evidence comparison, and final report.

Each checkpoint is a coherent commit and is pushed to `refactor/industrial-quality-20260825`. The final integration path is a reviewed pull request into the then-current `main`; if `main` advances, the branch is updated without discarding either side's valid work.

## 9. Risks and controls

| Risk | Control |
| --- | --- |
| Validated economic behavior changes during extraction | Characterization tests and frozen matrix comparison; keep compatibility facades |
| Fail-closed validation rejects valid legacy data | Enumerate supported protocol/data variants and test them before tightening |
| Large formatting diff obscures semantics | Isolate mechanical formatting or omit it when it adds insufficient value |
| Full evidence depends on unavailable artifacts or environment | Use fixed PR artifacts, verify digests, and report any irreducible environment gap |
| Documentation overstates production readiness | Separate live runtime, research checkpoint, historical evidence, and deployment prerequisites |
| Refactor expands without measurable value | Stop at the bounded checkpoints and reject abstractions that add indirection |

## 10. Completion criteria

The work stops when:

- no known P0 defect or unresolved P1 correctness defect remains;
- position, accounting, execution, margin, risk, reject, HALT, restart, and causal-split invariants have regression coverage;
- dependencies remain acyclic and core module ownership is explainable;
- all final validation gates pass, or an external limitation is evidenced precisely;
- Stress-80 and Stress-90 outputs are reproduced within explainable numerical tolerance, with every material difference investigated;
- code, configuration, tests, comments, documentation, and observed behavior describe the same system;
- the worktree contains no temporary/debug files or hidden test relaxations.

P2 cleanup is performed only when it materially reduces future maintenance cost. P3 style work is not a reason to extend the project.
