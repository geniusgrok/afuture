# Solo Operations Hardening Report — 2026-08-25

## Executive summary

This follow-up implements every remaining P1/P2 recommendation from the industrial review while preserving the single-user architecture. It adds observable startup gates, bounded local evidence, explicit last-known-good state evidence, reproducible direct dependency constraints, and a clearer authority/archive split. It does not add a database, service, dashboard, dependency-injection layer, or strategy abstraction.

No unresolved P0 item existed at the start of this follow-up. The work changes no alpha, portfolio construction, sizing, leverage, risk threshold, commission, slippage, execution timing, stress scenario, or train/validation/OOS definition.

## Operational safety

- `afuture status` is a local-only, read-only command. It reports current and previous state integrity, Kill Switch/runtime facts, persisted position/account markers, evidence-file sizes, writable runtime ancestors, and disk space without initializing logging or CTP.
- `afuture doctor` retains live confirmation and fresh account/complete-position snapshot requirements, then evaluates named account, active-order, catalog, metadata, persisted gate, position-reconciliation, Kill Switch, runtime-mode, filesystem, disk, and Directional activity checks. Any failed gate returns 2; it never calls `send_order`.
- Fresh flat deployments may have no state. A non-flat broker without trusted expected state fails reconciliation. Directional deployments require a completed activity snapshot before preflight can pass.

## State and evidence lifecycle

- Before replacing a verified current state, `StateStore` atomically preserves its exact bytes as `<state>.prev`. Corrupt current state still blocks saving, and normal `load` never falls back to `.prev`.
- Audit and alert JSONL now rotate before an append would exceed 20 MiB and retain 14 numbered backups. Records remain complete UTF-8 JSON lines. Audit failures propagate; alert sink failures retain the existing isolated warning behavior.

## Dependencies and documentation

- `constraints/core-dev.txt` pins the direct core/dev versions used by Python 3.10–3.13 CI. `constraints/live.txt` pins the direct CTP/AKShare layer while explicitly requiring target-workstation native-wheel validation.
- CI and manual research workflows consume those constraints instead of resolving open-ended direct versions.
- Superseded research evidence lives under `docs/archive/evidence/`; historical implementation plans/specifications live under `docs/archive/development/`. `docs/documentation-index.md` remains the single authority map.
- README now separates the inherited engineering baseline (PR #26 / `34fd0210`) from the frozen research checkpoint (PR #25 / `482455d`).

## Behavioral compatibility

All production trading economics are unchanged. The only new exit-code behavior is intentional operational fail-closed behavior in `status` and `doctor`. `.prev` and rotation add local filesystem writes only where state/audit/alert writes already occurred; they do not feed strategy, risk, execution, accounting, or research calculations.

## Verification

Final engineering validation results are recorded here after the candidate is frozen.

## Known limitations

- Constraints pin direct dependencies, not every platform-specific transitive wheel or artifact hash. This is deliberate for a Python 3.10–3.13, multi-OS repository; the native CTP environment still requires workstation validation.
- JSONL rotation is process-local. The supported deployment is one afuture process per account/runtime directory.
- `.prev` is one generation and is never an automated recovery mechanism. Broker truth plus the explicit recovery workflow remains authoritative.
