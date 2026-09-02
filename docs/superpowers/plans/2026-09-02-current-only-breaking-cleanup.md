# Current-Only Breaking Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete afuture's historical compatibility paths while preserving every
current production safety boundary and all production economic behavior.

**Architecture:** Make existing decoders exact-current and fail closed, delete
format migrations and thin facades, route retained commands through one public
CLI owner, and make `AppConfig` the only TOML authority. Prefer deletion and
direct calls over new abstractions.

**Tech Stack:** Python 3.10/3.13, standard library, pytest, Ruff, mypy, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-02-current-only-breaking-cleanup-design.md`

## Global Constraints

- Preserve Broker/CTP truth, exactly-once suppression, order journal, crash-fill
  recovery, locks/leases, CAS/sequence/checksum/lineage, atomic writes, identity
  binding, nonce protection, kill switch, HALTED/REDUCE_ONLY, Shadow isolation,
  reconciliation, backup/restore, Stress-90 fail-closed, and current `.prev` evidence.
- Preserve exact policy/product digests, products, thresholds, weights, margin
  results, reduction-first ordering, order economics, fill accounting, bootstrap,
  CTP mapping, and Shadow/live wiring.
- Old formats are rejected and left byte-for-byte untouched. Do not add a
  replacement migration, compatibility adapter, version router, feature flag,
  framework, dependency, or speculative abstraction.
- Use focused tests during implementation, subsystem suites at checkpoints, and
  one full suite after all implementation tasks.
- Do not reset, clean, rebase, force-push, rewrite shared history, delete unknown
  work, push `main`, or merge the PR.

---

### Task 1: Exact-current runtime state and evidence layouts

**Files:**
- Modify: `afuture/state.py`
- Modify: `afuture/stress90_activation_permit.py`
- Modify: `afuture/directional_stress90_execution.py`
- Modify: `afuture/directional_policy_activation.py`
- Modify: `afuture/stress90_lifecycle_transaction.py`
- Modify: `afuture/trading_day_evidence.py`
- Modify: affected state, permit, execution, lifecycle, activation, and evidence tests

- [x] Add RED tests for schema-less/old state, old permit/intent schemas,
  missing/unknown payload fields, no-overlay markers, markerless lifecycle layout,
  and schema-2 trading-day lifecycle binding.
- [x] Require exact current schema, envelope, payload, and lineage layout; remove
  `RuntimeStateRecord.legacy` and all generic-state legacy branches.
- [x] Require current risk-overlay identity everywhere a full Stress-90 marker is
  validated; keep current controlled policy switching and all `.prev` semantics.
- [x] Prove rejected artifacts are not rewritten and run focused state/lifecycle suites.

### Task 2: Current-only account registry and nonce ledger

**Files:**
- Modify: `afuture/account_runtime_registry.py`
- Modify: `afuture/account_runtime_nonce_ledger.py`
- Modify: `afuture/cli.py`
- Modify: backup allowlist in the canonical backup implementation
- Modify: registry, nonce, CLI, and backup tests

- [x] Add RED tests that registry schemas 1/2 and a missing current lineage marker
  fail closed without mutation.
- [x] Delete legacy binding decoding, synthesized revisions/digests/operation kind,
  old checksum construction, and marker auto-upgrade.
- [x] Delete nonce-ledger migration implementation, confirmation constant,
  `migration.json`, legacy tombstones/receipts/fault injections, and the
  `stress90-registry-nonce-migrate` CLI route.
- [x] Preserve current initialization, empty authenticated ledger, locks, CAS,
  sequences, parent checksum, nonce anchor, retired identities, and pending/ready
  current commit recovery; run focused registry/nonce/backup suites.
- [x] Create and push Checkpoint A, verify the exact remote head, and update the PR.

### Task 3: Remove fixed-archive historical compatibility replay

**Files:**
- Delete: the historical fixed-archive compatibility adapter and its dedicated tests
- Modify: `tools/evaluate_directional_stress80_final.py`
- Modify: `tools/evaluate_directional_stress90_final.py`
- Modify: affected Stress-90 final/incremental/policy tests and active docs/workflow wording

- [x] Add/retain tests proving the runtime imports no offline evaluator and the
  current policy-definition digest is exact.
- [x] Route retained evaluator mains through SHA-verified current input loading and
  the shared current candidate core; retain manifest/size validation.
- [x] Delete only the obsolete compatibility adapter and compatibility-only fixed
  replay tests; retain canonical bootstrap, batch/incremental parity, policy, and
  historical provenance receipt.
- [x] Run focused evaluator/bootstrap/policy/workflow suites and confirm candidate
  and policy digests are unchanged.

### Task 4: Collapse backup and sizing facades

**Files:**
- Replace: `afuture/runtime_backup.py` with the current implementation
- Delete: the runtime-backup implementation facade module
- Modify: `afuture/directional.py`
- Modify: backup/restore and sizing tests

- [x] Move the implementation without changing archive schema, member ordering,
  manifest digesting, semantic verification, restore publication, HALTED outcome,
  or quarantine behavior.
- [x] Remove `margin_sizing_share`; call `adaptive_margin_sizing_share` directly
  with empty completed returns in its regression test.
- [x] Prove no-history/calm/stressed sizing values and current backup/restore safety.

### Task 5: One canonical CLI and current configuration schema

**Files:**
- Modify: `afuture/command_router.py`
- Modify: `afuture/cli.py`
- Modify: `afuture/config.py`
- Modify: `afuture/config_validation.py`
- Modify: `afuture/heartbeat_config.py`
- Modify: `afuture/models.py`, `afuture/auto.py`, and current config consumers/examples
- Modify: CLI/config/heartbeat/workflow-contract tests

- [x] Add RED parser tests proving removed migration/old config names are absent and
  retained current operational commands remain.
- [x] Keep `afuture.command_router:main` as the sole public entry point, delete
  `_delegate`/`legacy_main` semantics, and call a neutrally named internal dispatcher
  without changing preflight, deployment, fence, or heartbeat order.
- [x] Rename the compatibility stationarity field/key to
  `min_mean_reversion_score`, reject the old key, and require explicit directional
  policy whenever enabled while preserving equivalent current numeric behavior.
- [x] Put heartbeat path/interval in `AppConfig`; remove independent TOML parsing,
  `_ACTIVE_SETTINGS`, activation functions, and extension-key bypasses. Preserve
  relative-path and Shadow runtime behavior through explicit settings.
- [x] Run focused CLI portability/safety/runtime-factory, config, heartbeat,
  workflow-contract, and documentation consistency suites.

### Task 6: Current-only policy and operational documentation

**Files:**
- Modify: `README.md`
- Modify: `.github/CHATGPT_PROJECT_BRIEF.md`
- Modify: `docs/architecture.md`
- Modify: `docs/configuration.md`
- Modify: Stress-90 runbook, productionization, checklist, index, and related active docs
- Modify: `.github/workflows/ci.yml`

- [x] State the permanent current-only policy, exact fail-closed behavior, operator
  archive/bootstrap/reconciliation procedure, and safety mechanisms that are not debt.
- [x] Remove obsolete migration/compatibility commands and wording; label retained
  historical receipts as provenance only.
- [x] Keep Python 3.10, `tomli`, 3.10/3.13 CI, and Windows core/replay smoke because
  current repository contracts explicitly require them; rename misleading smoke/test wording.
- [x] Record the multi-binding registry representation as a deferred P2 decision
  because it currently enforces account-switch, retired-identity, and lifecycle safety.
- [ ] Create and push Checkpoint B, verify the exact remote head, and update the PR.

### Task 7: Final verification and PR closure

**Files:**
- External: one PR from `codex/current-only-breaking-cleanup` to `main`

- [ ] Compare policy/product digests and protected economic module hashes against
  the recorded baseline; run exact sizing, reduction-first, order economics, CTP,
  fill-accounting, bootstrap, and Shadow/live wiring tests.
- [ ] Run pip check, Ruff lint, Ruff format check, mypy, compileall, all current
  configuration validations, production mechanics, and one full pytest suite.
- [ ] Dispatch one independent base-to-head code review; fix all Critical/Important
  findings and perform one scoped re-review when needed.
- [ ] Create and push Checkpoint C, verify local/remote head and tree, update the PR
  body with exact files/results/UNKNOWNs, and wait for all required checks.
- [ ] Mark the PR ready only when checks pass and no Critical/Important review remains.
  Do not merge.
