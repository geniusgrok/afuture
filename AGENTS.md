# Repository working agreement

## Entry and authority

- Follow the current task's scope and acceptance criteria; read applicable nested `AGENTS.md` before editing that directory. Resume with still-valid authorization and surface material conflicts rather than silently relaxing them.
- For continuation work, read `PROJECT_STATE.md` after this file, resolve mutable branch/PR/SHA/CI/artifact facts from GitHub, then load only the matching PR/diff and affected code, tests, configuration and workflows. Read `.github/CHATGPT_PROJECT_BRIEF.md` only for stable architecture, commands or boundaries. Historical plans are context, not new authority; do not preload unrelated history, skills or logs.
- Skills provide methods, not additional authority or approval gates. Continue already-authorized bounded work unless a platform/safety limit or a material decision not resolvable from repository state blocks it.

## Git, recovery and verification

- Resume the matching active branch/PR. New work uses a feature branch from a verified default-branch SHA and is delivered through a PR; do not push directly to the default branch. Merge only with explicit authorization and required reviews/checks/protection satisfied.
- Save coherent recoverable milestones as commits, push when authorized, verify the remote SHA, and keep mutable recovery state in the matching PR.
- Without explicit authorization, do not reset, clean, rebase, force-push, rewrite history, discard unknown work, or commit secrets.
- Verify the smallest affected scope first and expand by impact. Run the complete applicable acceptance on the stable final candidate or when the active contract requires it earlier. Reuse evidence only while its covered behavior, inputs, dependencies, configuration and environment remain equivalent; new messages/handoffs alone do not invalidate it, while a new SHA still needs applicable exact-HEAD checks.
- Treat a failed check, rejected candidate or invalidated hypothesis as feedback, not task completion. Diagnose it and continue with an evidence-supported alternative or a bounded check that distinguishes plausible causes, within the authorized scope and any task budget. If attempts add no information, reassess other authorized paths rather than repeat them. Finish when acceptance is met; if no safe authorized action remains, preserve progress and report the specific blocker or evidence gap without requiring proof that the goal is impossible. Do not weaken acceptance criteria, suppress failed evidence, or bypass safety, authorization or frozen contracts.
- Behavior-neutral documentation changes need relevant link/command/governance checks, not unrelated stress or economic reruns once neutrality is established. Do not weaken business gates, fixtures or safety checks to make checks pass.

## afuture boundaries

The implementation is in `afuture/`; use `pyproject.toml`, `constraints/core-dev.txt`, `tests/` and `.github/workflows/ci.yml` for engineering validation. Current safety contracts are indexed by `docs/documentation-index.md`; startup/live procedures are in `docs/live-trading.md`, `docs/production-checklist.md` and `docs/stress90-live-runbook.md`.

Broker/CTP events own external account and order facts; PositionBook advances only from fills, RiskManager owns hard limits, and StateStore holds verified restart evidence. Do not create a second account/risk owner from sidecars or reports. Preserve reduction-first execution, D-to-D+1 causality, fail-closed schema/sequence/checksum/identity checks and Shadow/live separation. Bootstrap, doctor, research, Shadow and test-counter results do not authorize real-money trading; do not automatically adopt damaged state or `.prev`.
