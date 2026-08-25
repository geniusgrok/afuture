# Solo Operations Hardening Implementation Plan

> **归档说明：** 本文是已完成阶段的实施记录，不描述当前系统；当前事实见 [`documentation-index.md`](../../../documentation-index.md)。

Status: execution record. Implement against `origin/main` merge `34fd0210b914b847ed10ecadead53b62015a32af`.

## Task 1: Verified previous state

1. Add failing `StateStore` tests for exact verified `.prev` retention, explicit `load_previous`, no automatic fallback, and corrupt-current refusal.
2. Refactor verified decoding to accept an explicit path and add atomic byte replacement.
3. Implement `.prev` creation before current replacement.
4. Run the state-focused tests and static checks for `afuture/state.py`.

## Task 2: Bounded JSONL evidence

1. Add failing tests for size-triggered rotation, backup retention, and valid line preservation.
2. Implement the focused rotating JSONL writer with 20 MiB/14-backup defaults and injectable limits for tests.
3. Delegate `AuditJournal` and `FileAlertSink` writes without changing event schemas.
4. Run journal/alert/rotation tests and affected static checks.

## Task 3: Local status and fail-closed doctor

1. Add failing report tests covering valid/missing/corrupt state, previous-state visibility, disk/path facts, broker reconciliation, kill switch, active orders, metadata coverage, and directional activity.
2. Implement JSON-compatible operational report builders in `afuture.operations`.
3. Add local-only `afuture status --config ...` and structured doctor output/exit codes.
4. Keep live confirmation and fresh-snapshot requirements unchanged; assert `orders_sent=0`.
5. Run operations and CLI safety tests plus CLI smoke checks.

## Task 4: Reproducible installation and documentation governance

1. Add exact core/dev constraints derived from the supported Python 3.10 environment and use them in CI/install commands.
2. Archive superseded research evidence under `docs/archive/evidence/` and Superpowers implementation records under `docs/archive/development/`.
3. Update all links, documentation classifications, operational runbooks, and README.
4. Separate the current engineering baseline (PR #26 / `34fd0210`) from the frozen research checkpoint (PR #25 / `482455d`).
5. Run documentation link/path/command checks.

## Task 5: Final candidate and integration

1. Review the complete diff for economic behavior, public CLI compatibility, temporary files, debug code, and stale documentation.
2. Run the complete engineering validation once: unit/integration tests, lint, format, mypy, compile/import, CLI/config smoke, and documentation consistency.
3. Request a final code review, address substantive findings with targeted reruns, and use verification-before-completion.
4. Commit meaningful checkpoints, push the branch, merge to `main`, and verify the remote main SHA and CI.
