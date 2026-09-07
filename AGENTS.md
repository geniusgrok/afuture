# Repository working agreement

## Scope and authority

Follow the current task's scope and acceptance criteria within platform permissions. Preserve project-specific business, security, data, economic, CI and release contracts; read applicable nested `AGENTS.md` before editing that directory. Resume with the original task contract and still-valid authorization; surface material conflicts rather than silently relaxing them. Historical plans are context, not new authority. Analysis-only and approval-before-edit requests remain read-only until authorized.

## Context and methods

- Resolve current branches, SHAs, PRs and checks from GitHub; inspect local changes when a worktree exists, otherwise mark local worktree state as not applicable. Continue matching work instead of creating a replacement PR or redoing verified work.
- After applicable `AGENTS.md`, read `PROJECT_STATE.md` as the low-token routing index before loading broader project history. For matching continuation work, follow it to the active PR and load only the current state and affected files needed for the next decision; for new or unrelated work, use it only to avoid stale continuation assumptions. Mutable branch, SHA, CI, artifact and acceptance facts must still be resolved from GitHub. Read the project brief only when stable architecture, commands or boundaries are needed, then relevant README sections, matching PR/diff, and directly affected code, tests, configuration and workflows. Expand only to resolve uncertainty or impact. Keep exact constraints and evidence identities intact; do not preload every skill, document or log.
- Use skills as task-specific methods, not additional task authorities. For an already authorized, bounded task, do not add repeated design approvals, execution-mode questions or ceremonial announcements. Honor explicit user-selected methods and modes, platform-required skill use and genuine project approval gates. Missing optional skills are not blockers when available tools suffice.
- Prefer the smallest sufficient change and existing dependencies. Plan only material decisions, dependencies and acceptance; do not duplicate implementation code in plans. Batch related edits. Parallelize independent work only; keep one writer per shared file, runtime or evidence identity.

## Authorization and recovery

- Continue safe, clearly authorized work without asking for another "continue". Resolve factual ambiguities by reading; ask only for a material decision that cannot be resolved safely. Do not infer permission for spending, real trading, credential/permission changes, irreversible operations or unrelated external writes.
- Resume matching active task branches. For new work, use a feature branch from a verified default-branch SHA and deliver through a PR; do not push directly to the default branch. Merge only with explicit authorization, preserving required reviews/checks, branch protection and conflict checks; verify the default branch after merging.
- Save the first coherent result and meaningful later milestones as verified commits; push authorized checkpoints and verify remote SHA. No empty bootstrap commit, temporary bootstrap file or new Issue is required for routine work; honor any explicit task-specific pre-edit initialization requirement. Record current state and necessary recovery evidence locations in the matching PR using [.github/pull_request_template.md](.github/pull_request_template.md), not permanent instructions.
- Without explicit authorization, do not `reset`, `clean`, `rebase`, force-push, rewrite history, delete branches/worktrees, discard unknown work or overwrite unrelated changes. Never commit secrets or claim an unverified push succeeded.

## Verification and completion

- Diagnose failures at the smallest failing test, module or shard. Batch related fixes before expanding coverage; do not run the full suite after every edit. Run the complete applicable acceptance set on the stable final candidate, expanding earlier only when risk or the contract requires it.
- Reuse evidence only while its covered behavior, inputs, dependencies, configuration and environment remain equivalent. New messages, agents or handoffs alone do not invalidate it. A new SHA still needs applicable exact-HEAD checks; never report an old CI result as the new HEAD's status.
- Behavior-neutral documentation changes need relevant link/command/format checks, not unrelated backtests. Instruction changes also need trigger, authorization and completion-boundary review. Do not weaken business gates, fixtures or thresholds to make checks pass.
- Review the complete task diff once; repeat focused review for material fixes or risk. Finish when acceptance and required checks pass with no known material correctness, security, data-integrity or economic issue. Do not add marginal optimization afterward.
- A checkpoint, PR, partial test pass or prepared handoff is not completion. Continue independent targets past a blocker. If no safe authorized action remains, preserve recoverable progress and report the exact blocked/unverified items separately from completed work. Do not promise background completion.

## afuture entry points and safety

Read [.github/CHATGPT_PROJECT_BRIEF.md](.github/CHATGPT_PROJECT_BRIEF.md) for the architecture and current environment. The implementation is in `afuture/`; use `pyproject.toml`, `constraints/core-dev.txt`, `tests/` and `.github/workflows/ci.yml` for engineering validation. Current safety contracts are indexed by [docs/documentation-index.md](docs/documentation-index.md), with startup and live procedures in [docs/live-trading.md](docs/live-trading.md), [docs/production-checklist.md](docs/production-checklist.md) and [docs/stress90-live-runbook.md](docs/stress90-live-runbook.md).

Broker/CTP events own external account and order facts; PositionBook advances only from fills, RiskManager owns hard limits, and StateStore holds verified restart evidence. Do not promote sidecars or reports into a second account/risk owner. Preserve reduction-first execution, D-to-D+1 causality, fail-closed schema/sequence/checksum/identity checks and Shadow/live separation. Bootstrap, doctor, research, Shadow and test-counter results do not authorize real-money trading; do not automatically adopt damaged state or `.prev`.
