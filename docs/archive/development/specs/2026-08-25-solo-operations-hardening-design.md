# Solo Operations Hardening Design

Status: approved implementation record. This document records an engineering change, not the current trading policy.

## Context

The industrial refactor established clear trading, risk, persistence, and research boundaries. The remaining high-value work for a single-user deployment is operational: make startup failures diagnosable, keep local evidence bounded, make state recovery inspectable, and make installation and documentation less ambiguous. No item requires a service, database, dashboard, or new strategy abstraction.

## Scope and invariants

- `afuture status` is local and read-only. It must not import or start the CTP adapter, mutate state, or send orders.
- `afuture doctor` may connect to CTP but must never send an order. It becomes a fail-closed preflight report with named checks and exit code `2` on a failed safety check.
- A missing state file is safe only when the broker is flat. An existing state must pass checksum validation and its expected positions must match the fresh broker snapshot.
- A persisted kill switch, non-running runtime mode, active broker order, invalid metadata, missing required directional activity evidence, or an unusable runtime filesystem fails preflight.
- State saves retain the last verified envelope as `<state>.prev`. The application never falls back to it automatically; it is inspection/recovery evidence only.
- Audit and alert JSONL files rotate before exceeding a bounded size. Rotation preserves whole UTF-8 JSON lines and a fixed number of backups.
- Dependency constraints make the core/dev CI environment reproducible. Native live dependencies remain an explicit platform-specific installation layer because a cross-platform transitive lock would be misleading.
- Documentation archival changes navigation only. Historical evidence remains available and clearly non-authoritative.
- Strategy alpha, sizing, execution assumptions, commission, slippage, risk thresholds, stress scenarios, and research partitions are unchanged.

## Design

`afuture.operations` owns small operational reports and filesystem checks. It returns plain JSON-compatible dictionaries so the CLI remains glue rather than a new framework. Status inspects `StateStore`, its explicit previous snapshot, evidence paths, and available disk space. Doctor combines those local facts with a fresh broker account/position snapshot, active orders, contract catalog, and sampled live metadata.

`afuture.jsonl` provides one focused `RotatingJsonlWriter`. `AuditJournal` and `FileAlertSink` delegate their final serialized row to it; event schemas and callers do not change. Defaults are constants rather than new configuration knobs: 20 MiB per file and 14 backups.

`StateStore.save` verifies the current envelope before any replacement. When current state exists, its exact verified bytes are atomically written to `.prev`, then the new sequence is atomically installed. Corrupt current state remains incident evidence and still blocks saving. `load_previous` is explicit and never called by `load`.

## Failure behavior

- Status prints a complete report even when current or previous state is corrupt, then returns `2` only when current operational state is invalid.
- Doctor prints named failed checks and returns `2`; unexpected connection/runtime exceptions retain the existing CLI error path.
- JSONL write or rotation failures propagate to `AuditJournal`. Alert delivery failures retain `AlertManager`'s existing isolation and warning behavior.
- Backup creation failure aborts a state save before replacing current state.

## Validation

Behavior is introduced through failing unit tests for state backup, explicit no-fallback behavior, rotation, local status, and doctor safety gates. Milestone checks cover the affected CLI/state/alerts/journal modules. The final candidate runs formatting, lint, type checking, compile/import validation, documentation checks, CLI smoke tests, and the full unit/integration suite once. Economic L4 matrices are not rerun because no economic or research behavior changes.
