# Stress-90 live productionization handoff — 2026-08-26

## Safety status

This branch is a production-safety checkpoint, not a completed live release. Keep the pull
request in draft, do not merge it to `main`, and do not authorize real-money trading until
all blockers and final acceptance gates below are closed.

Current conclusion: **Stress-90 live wiring is incomplete and real-money activation is not
authorized.**

## Authoritative Git/GitHub snapshot

- Repository: `ychenracing/afuture`
- Base branch: `main`
- Base SHA observed before and after the work: `da8de59304963c7b1d6737a63e8dadd6eaecd860`
- Feature branch: `codex/stress90-live-productionization`
- Draft PR: <https://github.com/ychenracing/afuture/pull/29>
- Remote feature SHA: `8bb667dd2deb3f31da4b104eeb634dd57fddeb71`
- Remote feature tree: `b7889126ea3e64182143bce21d745016064d23bc`
- Local checkpoint SHA: `8cdbbe7c316c2a9041f55cff271460095829da86`
- Local checkpoint tree: `b7889126ea3e64182143bce21d745016064d23bc`

The local and remote commit SHAs differ because the two final local commits were recreated
through the authenticated GitHub Git-data API after ordinary HTTPS `git push` could not read
credentials. Their final trees are byte-identical. Never force-push either history. A new
session should continue from the remote PR head, or create a separate local continuation
worktree from that remote head and push fast-forward commits to the existing PR branch.

Local milestone history before API recreation:

1. `f552d72` — unify Stress-90 candidate core
2. `3eef47f` — persist Stress-90 decisions exactly once
3. `f557aa5` — capture authoritative Stress-90 OI evidence
4. `c6ac7c9` — wire Stress-90 production runtime
5. `83b7f5f` — add Stress-90 commissioning controls
6. `cc0c8bd` — persist shadow broker mechanics
7. `c0ebe52` — checkpoint production lifecycle safety
8. `8cdbbe7` — harden durable lifecycle boundaries

## Fresh verification evidence

The final local safety-checkpoint verification was:

```text
354 passed in 6.11s
python -m ruff check afuture tests          passed
python -m ruff format --check afuture tests 204 files already formatted
git diff --check                            passed
```

This was an affected L3 suite, not the required final L4.

GitHub Actions run 1749 is available at
<https://github.com/ychenracing/afuture/actions/runs/32970605833>. Its current result is
failure:

- Python 3.10 full suite: `1310 passed, 24 failed, 1 skipped`.
- Python 3.13 was cancelled after the matrix failure.
- Quality: Ruff lint and format passed; MyPy failed with 51 errors in five source files.
- All later smoke/config/build steps were skipped after the failures.

## Work present on the branch

The branch contains the following implementation, but it must still be treated as
unreleased until full parity and CI closure:

- A shared immutable Stress-90 policy definition and production-safe candidate primitives.
- Incremental candidate state and prepared-decision persistence with sequence/checksum/CAS
  semantics.
- Stress-90 bootstrap and fixed-historical 60-minute bridge code.
- Runtime-factory selection for explicit `execution_aligned` and `stress90` modes.
- A Stress-90 manager using freeze-only soft risk response instead of the legacy 0.25 target
  scaling wrapper.
- CTP raw tick evidence observation before manager tick coalescing, with bounded in-memory
  aggregation and batch checkpoint hooks.
- OI/session evidence persistence, session ownership recovery, order submission journaling,
  account runtime registry, activation permit, lifecycle coordinator, and machine/runtime
  exclusive leases.
- Commissioning/status/doctor/config/documentation scaffolding and a Draft live runbook.

## Closed adversarial findings

The latest review and regression tests closed these safety failures:

- A lifecycle decision is committed while Broker critical ingress is fenced.
- Activation, reactivation, rebase, and migration cannot overwrite unpersisted authorized
  crash-fill identities, including net-zero fill sequences.
- CTP raw evidence cannot claim a non-adjacent civil-day transition merely because one market
  socket remained connected.
- A market disconnect/reconnect or process restart cannot invent a completed OI transition.
- Settlement transaction retries bind immutable settlement evidence while allowing volatile
  intraday marks to change.
- CTP session evidence accepts only the exact duplicate `current == .prev` crash layout; it
  never falls back automatically.
- Missing OI `current` with surviving `.prev` evidence fails closed instead of resetting to
  sequence zero.
- Generic state, Stress-90 policy state, and OI evidence rename operations fsync their parent
  directories.
- Runtime/account leases canonicalize real paths, preventing a symlink plus lock-file unlink
  from bypassing the unlink-proof kernel lease with another account identity.
- The production Stress-90 manager refuses implicit next-day account-path advancement from
  the new day's `PreBalance`; an explicit settlement roll-forward path is required.

## Authoritative research-input blocker

None of these five fixed inputs exists in the workspace:

- `broad_daily_universe.csv`
- `return_target_specific_contracts.csv`
- `execution_aligned_weights.csv`
- `prior_two_year_broad_60m.csv`
- `two_year_broad_60m.csv`

Therefore this branch has not reproduced:

- candidate SHA `8e38dbf6441b561dd1728df08665b94b15cc3358823257505c2fcb9d63f09f28`;
- the Stress-90 seven-window matrix, including `112.100053%` full-recent annualized;
- the legacy Stress-80 `80.067891%` result.

Do not substitute data, regenerate research inputs, or claim exact historical parity without
the fixed files and their documented digests.

## Open P0 blockers

1. `stress90-settlement-roll-forward` still deliberately raises a fail-closed “lifecycle
   wiring incomplete” error. Complete it as an exactly-once, zero-order, HALTED lifecycle
   transaction or keep runtime rollover unavailable.
2. Prior-day final account funding closure is not authoritative. A D+1 `PreBalance` cannot
   prove that no deposit/withdrawal occurred after the last D account snapshot. Do not record
   that amount as strategy return without an account/day/request-bound final settlement or
   funding witness. Deposits/withdrawals must route to explicit rebase.
3. Authorized crash-fill adoption has no independent durable HALTED recovery checkpoint/CLI.
   The current lifecycle commands safely refuse to proceed when adoption differs from the
   persisted state, but operators cannot yet persist that recovery evidence.
4. Non-adjacent authoritative session continuity is unavailable. Weekend/holiday gaps remain
   fail-closed until an official, immutable CTP/session ledger proves every intermediate
   target transition.
5. Full CI is red. Do not weaken production gates or add permissive FakeBroker fallbacks to
   make tests pass.

## Open Important blockers

- Account-runtime registry lifecycle operation history does not consume the Stress-90 to
  execution-aligned migration nonce.
- Reactivation resets the policy adaptive-return path but still preserves generic
  `recent_daily_returns`; the two soft paths can diverge.
- A deleted registry `current` and `.prev` may be reinitialized using a surviving lock file;
  the incident lineage must fail closed.
- OI evidence validates missing-current evidence but still needs complete current/previous
  chain and interprocess CAS review.
- Trading-day evidence account epoch binding must match the exact registry epoch.
- Explicit old Stress-90 state schema migration and sim/replay startup capabilities need a
  final policy decision and tests.
- The actual CTP Python binding must prove the raw `TradingDay`/`ActionDay` fields and callback
  semantics on the target ABI.
- Branch protection is still disabled on `main`.

## CI failures to reproduce before new feature work

The 24 Python 3.10 failures currently cluster as follows:

- One live-dependency isolation failure in `test_ctp_order_journal.py`.
- Directional daily-recovery, freeze-new-risk, and OHLC-cache API regressions.
- Activation/rebase/migration/bootstrap FakeBroker fixtures missing new durable journal,
  complete-session, or lifecycle-fence capabilities.
- One bootstrap transition test now lacks authoritative CTP rollover evidence.
- Three documentation-consistency failures: four undocumented CLI commands and four
  undocumented Tick CSV fields.
- Eight lifecycle adversarial rebase tests whose FakeBroker lacks the required critical
  ingress commit fence.

MyPy currently reports 51 errors in:

- `afuture/broker/ctp_session_query.py`
- `afuture/directional_stress90_oi_runtime.py`
- `afuture/stress90_lifecycle_transaction.py`
- `afuture/directional_stress90_bootstrap.py`
- `afuture/cli.py`

Use the CI logs as the exact error authority. Fix production typing and test fixtures; do not
silence errors globally.

## Recommended continuation order

1. Reproduce the 24 CI failures and 51 MyPy errors locally; fix by TDD in small coherent
   batches. Run only affected tests after each batch.
2. Close the registry nonce, reactivation-return, and registry missing-lineage findings.
3. Design and implement the durable HALTED crash-fill recovery checkpoint.
4. Define an authoritative prior-day settlement/funding witness. If the CTP API cannot
   provide it, document this as an external activation blocker and keep rollover fail-closed.
5. Complete the explicit settlement roll-forward lifecycle only after the witness contract is
   defensible.
6. Add authoritative non-adjacent session continuity without guessed calendars.
7. Run Directional/CTP/state/CLI/restart L3, then request a new adversarial review.
8. Only after the branch is stable, run the single final L4, all config/CLI/build smokes, and
   the historical matrices if and only if the fixed inputs are available.
9. Keep PR #29 draft until review has no Critical/Important findings and required CI checks
   pass. Configure branch protection before merging.

## External gates that code cannot self-certify

Even after code completion, leave these unchecked until evidence exists on the target
machine: CTP ABI, multi-day Shadow, test counter, actual fees/margins, FAK partial fills,
disconnect/reconnect, tiny real-money trading, and risk-scale expansion.

`112.100053%` is historical research evidence, not a future-return promise. The 96-template
pool has observed selection bias, and new live data is the actual forward evidence. CTP/live
and vendor 60-minute differences must be explained through Shadow comparison.
