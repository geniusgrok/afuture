# Current-Only Breaking Cleanup Design

## Goal

Converge afuture on one current code path, configuration schema, durable runtime
schema, registry schema, and production CLI without changing current trading
economics or weakening fail-closed production safety.

afuture is a single-user, single-economic-account system. It does not provide
backward compatibility for old afuture code, APIs, CLI names, configuration
fields, runtime state, registry schemas, migration artifacts, or historical
replay adapters. Non-current durable formats fail closed. Operators archive an
old runtime and perform an explicit current bootstrap and account reconciliation;
the program does not migrate or guess.

## Current durable contracts

- `StateStore` accepts only schema 3 with the exact four-field envelope and the
  complete exact current `RuntimeState` payload. Schema-less, older, missing-field,
  and unknown-field state is rejected without rewriting evidence.
- The account/runtime registry accepts only schema 3 and requires its current
  lineage marker and authenticated nonce-ledger anchor. Registry schemas 1/2,
  markerless layouts, migration manifests, legacy tombstones, and nonce-migration
  receipts are rejected or absent.
- Stress-90 activation permits accept only schema 6, execution intents only
  schema 7, lifecycle transactions only their current schema/layout, and
  trading-day evidence only its current schema. Current Stress-90 identity
  includes the risk-overlay digest; missing overlay identity is not inferred.
- Current-schema `.prev` files remain immutable incident/predecessor evidence.
  They are never automatic fallback or migration inputs.

The controlled `directional-policy-migrate` operation remains a current lifecycle
transaction, not a file-format migration. It switches a current, verified,
HALTED/flat/reconciled Stress-90 runtime to the current execution-aligned policy
while preserving retirement provenance. Its old-marker acceptance is removed.

## Canonical modules and CLI

- `afuture.runtime_backup` owns current backup/restore directly; the `_impl`
  compatibility facade split is removed without changing backup schema or bytes.
- Current sizing callers use `adaptive_margin_sizing_share` directly; the empty-
  history wrapper is removed while its exact 0.30 result remains tested.
- `afuture.command_router:main` remains the sole installed/public entry point.
  Router-owned production preflight, deployment gates, process fencing, and
  heartbeat setup remain in order; fallback command parsing is an internal CLI
  dispatcher, not a public `legacy_main` path.
- The old registry-to-nonce migration command is removed. Current registry
  initialization directly creates the current empty authenticated nonce ledger.

## Configuration ownership

`load_config()` is the only TOML reader. `AppConfig` owns heartbeat path and
interval; heartbeat/watchdog receive explicit settings derived from the loaded
object, without module-global active configuration or extension-key bypasses.

Unknown and removed keys fail closed. Directional mode requires an explicit
current policy. The compatibility-retained stationarity key becomes the current
`min_mean_reversion_score` name across TOML and Python configuration objects;
the old key is rejected. Numeric defaults and validation remain identical, so
pair/auto selection economics do not change for equivalent current configs.

Business and safety defaults that are not historical adapters remain. In
particular, execution-aligned mode's account-identity requirements remain
distinct from Stress-90's machine-bound identity contract.

## Historical research boundary

The fixed-archive compatibility adapter and compatibility-only replay tests are
removed. Retained Stress-80/90 research evaluators read SHA-verified inputs and
call the shared current candidate core directly. The canonical Stress-90 policy,
five-input bootstrap, incremental live path, matrix manifest validation, and
historical provenance receipt remain.

## Safety and economic invariants

The cleanup preserves Broker/CTP fill truth, exactly-once duplicate suppression,
the CTP order journal, crash-fill recovery, process/runtime leases, file/kernel
locks, CAS/sequence/parent chains, checksums/digests, atomic durable writes,
account/deployment/runtime identity, nonce replay protection, kill switch,
HALTED/REDUCE_ONLY, Shadow's no-live-order boundary, reconciliation, backup and
restore, Stress-90 fail-closed behavior, current bootstrap, and current risk and
execution gates.

The exact Stress-90 policy-definition digest remains
`97435aae770aea87e8313b17ea5af730a4c540f14b95a561c8da5f90e2db5cf6`; the
products-manifest digest remains
`2cd078bcc6f876f4f0f288a60b98143741bb06463921345bf771568fbcfeb403`.
Production product sets, parameters, costs, weights, reduction-first behavior,
order economics, CTP mappings, fill accounting, and Shadow/live wiring do not
change.

## P2 decisions

Python 3.10 remains an explicit supported baseline in packaging, typing/lint
targets, constraints, CI, README, and project policy, so the `tomli` fallback and
3.10/3.13 CI matrix remain. Windows remains a documented and tested core/replay
CLI environment, so its minimal portability smoke remains; live Stress-90 stays
POSIX fail-closed.

The registry's multi-binding representation is not simplified in this change.
It currently carries account-switch recovery, Shadow/live isolation, retired
identity evidence, and lifecycle-rebase invariants. Collapsing it would cut
through current lifecycle safety for little compatibility-debt reduction. This
is an explicit follow-up decision, not an unexamined compatibility promise.

## Verification

Use test-first focused regressions, then subsystem suites at the end of each
checkpoint. Run the full suite once after all phases, followed by pip check,
Ruff lint/format, mypy, compileall, current config validation, production
mechanics, economic-digest checks, independent review, and required PR checks.
Do not run expensive historical research workflows because policy economics are
not changed.
