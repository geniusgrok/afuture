# Solo Operations Hardening Report — 2026-08-25

## Executive summary

This follow-up implements every remaining P1/P2 recommendation from the industrial review while preserving the single-user architecture. It adds observable startup gates, bounded local evidence, explicit last-known-good state evidence, reproducible direct dependency constraints, and a clearer authority/archive split. It does not add a database, service, dashboard, dependency-injection layer, or strategy abstraction.

No unresolved P0 item existed at the start of this follow-up. The work changes no alpha, portfolio construction, sizing, leverage, risk threshold, commission, slippage, execution timing, stress scenario, or train/validation/OOS definition.

## Operational safety

- `afuture status` is a local-only, read-only command. It reports current and previous state integrity, Kill Switch/runtime facts, persisted position/account markers, evidence-file sizes, writable runtime ancestors, and disk space without initializing logging or CTP.
- `afuture doctor` retains live confirmation and fresh account/complete-position snapshot requirements, then evaluates named account/trading-day, margin/available/daily-loss/drawdown, active-order, catalog, metadata, persisted gate, position-reconciliation, Kill Switch, runtime-mode, filesystem, disk, and Directional activity checks. Any failed gate returns 2; it never calls `send_order`.
- Fresh flat deployments may have no state. A non-flat broker without trusted expected state fails reconciliation. Directional deployments require a completed activity snapshot before preflight can pass.

## State and evidence lifecycle

- Before replacing a verified current state, `StateStore` reads once, verifies that exact byte buffer, and atomically preserves it as `<state>.prev`. Invalid JSON/UTF-8 current state still blocks saving, and normal `load` never falls back to `.prev`.
- Audit and alert JSONL now rotate before an append would exceed 20 MiB and retain 14 numbered backups. Records remain complete UTF-8 JSON lines; a single encoded record over the bound is rejected. Audit failures propagate; alert sink failures retain the existing isolated warning behavior.

## Correctness hardening found in adversarial review

- Position reconciliation previously keyed only by symbol and silently overwrote duplicate rows. It now compares `(symbol, exchange)`, rejects duplicates on either side, and rejects duplicate persisted position symbols before state load. Legal single-row positions are unchanged; ambiguous or inconsistent state now fails closed.
- Directional doctor now reuses the production completed-activity selector against the real catalog and requires an exact symbol/product/exchange identity plus eligible coverage for every configured product. Unrelated, expired, wrong-exchange, wrong-product, or below-volume/OI activity can no longer satisfy preflight.
- Runtime path inspection rejects directories used as file targets, symlink components, non-directory or non-searchable ancestors, and unwritable log/report/audit/alert targets. Broker/account trading-day mismatches and already-breached account limits also fail doctor.

## Dependencies and documentation

- `constraints/core-dev.txt` pins the direct core/dev versions used by Python 3.10–3.13 CI. `constraints/live.txt` pins the direct CTP/AKShare layer; AKShare `1.17.99` is resolver-checked for Python 3.10 while native CTP still requires target-workstation validation.
- CI and manual research workflows consume those constraints instead of resolving open-ended direct versions.
- Superseded research evidence lives under `docs/archive/evidence/`; historical implementation plans/specifications live under `docs/archive/development/`. `docs/documentation-index.md` remains the single authority map.
- README now separates the inherited engineering baseline (PR #26 / `34fd0210`) from the frozen research checkpoint (PR #25 / `482455d`).

## Behavioral compatibility

All production trading economics are unchanged for valid state, catalog, activity, and broker inputs. The only selection change rejects internally inconsistent activity identity that previously could be joined to an unrelated catalog row by symbol alone; this is a fail-closed correctness fix, not a strategy change. The new exit-code behavior in `status` and `doctor` is intentional operational fail-closed behavior. `.prev` and rotation add local filesystem writes only where state/audit/alert writes already occurred; they do not feed strategy, risk, execution, accounting, or research calculations.

## Verification

- Full repository suite: **512 passed**.
- Static/packaging gates: Ruff lint and format, Mypy, `compileall`, `pip check`, constrained requirements dry-run, and `git diff --check` passed.
- Documentation consistency: **49 Markdown files** passed the repository authority/link/path/command checker.
- CLI smoke: help, replay/live/directional-live validation, credential-free Directional `status`, fixed-pair replay, and Auto replay passed. Both replay reports closed with four trades, no positions, and zero margin.
- Direct live constraints resolved successfully on the current Python and the AKShare pin was separately resolver-checked for Python 3.10. Native CTP workstation loading remains an operator gate.
- The Stress/acceptance matrices were not rerun: no validated economic parameter, algorithm, execution assumption, or legal-input research path changed. The inherited PR #25 Stress-90 checkpoint therefore remains the comparison baseline; rerunning L4 would not add evidence.

## Known limitations

- Constraints pin direct dependencies, not every platform-specific transitive wheel or artifact hash. This is deliberate for a Python 3.10–3.13, multi-OS repository; the native CTP environment still requires workstation validation.
- JSONL rotation is process-local. The supported deployment is one afuture process per account/runtime directory.
- `.prev` is one generation and is never an automated recovery mechanism. Broker truth plus the explicit recovery workflow remains authoritative.
