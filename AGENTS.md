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

## Git/GitHub: no large uploads

- Prevent interruption before a transfer; do not rely on recovery after the whole session stops. Do not upload large raw data, replay archives, evidence packages, Git bundles, compressed archives or complete working directories from an agent session. Native Git is not an exception.
- Never print or pass large file bodies, full Base64 or huge JSON through model/tool output or arguments. No chunk-and-reassemble requests, mass small-part uploads, compression or alternative transport to circumvent this rule. Do not probe large-payload limits or extract connector credentials.
- Remotely save only necessary small source changes, relevant tests, concise results and the existing recovery entry. Inspect file sizes and the full outgoing change/object set before writing; select explicit paths, not an indiscriminate `git add .`. Exclude large objects before the call without deleting originals or rewriting unrelated history.
- Keep large originals where they already exist. Record their actual location, size/hash, reproduction command and preservation status in a small manifest. A summary/hash is not a backup. Explicitly report originals not remotely preserved and temporary-runtime loss risk; never claim complete preservation without it.
- Reuse concise recovery state: objective, source identity, completed/unverified work, key results and next action. Do not embed full logs or historical archives. Use small serial saves, one writer per branch, and remote readback verification.
- A blocked large upload must not block independent implementation or validation. If acceptance requires an unavailable original, mark that requirement unmet; do not fabricate evidence or weaken acceptance.
- Apply this boundary to task instructions, project briefs and handoffs. It supersedes conflicting legacy upload/archival directions only, not the task goal, authorization or evidence contract. Do not rewrite historical evidence. Do not create or modify scheduled tasks without the user's explicit approval for that scheduling change.
