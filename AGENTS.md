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

## Git/GitHub: chunked large-original preservation

- Large originals are not optional when the active task or evidence contract requires them. Do not drop, summarize away, or leave them unpreserved merely because a direct one-shot upload is risky.
- Avoid direct large-payload transfer through the model/agent boundary. Do not print an entire large file, full Base64 body, huge JSON, or complete archive into command/tool output and then reuse that truncated output as upload input.
- For large originals, read in bounded chunks. Record and verify chunk offset/order and expected versus actual byte length; when encoding is involved, distinguish raw byte length from encoded character length. Verify the aggregate length before upload and verify the final reconstructed/raw content hash afterward.
- Prefer an already-authorized file-aware path that reads the local file directly, such as native Git/Git LFS where appropriate or a programmatic request executed inside the runtime, so the model does not have to reproduce the whole payload. If the native/API path lacks write permission or is unavailable, switch to the authorized GitHub connector instead of stopping or asking the user to manually recover the transfer.
- When using a connector, do not trust a large body obtained from one truncated read. Build from verified chunks using the safest supported transport. If the connector cannot safely carry the final object in one request, use a deterministic multi-part representation or other supported resumable route with a manifest containing part order, raw/encoded lengths and cryptographic hashes; verify byte-for-byte reconstruction before claiming the original is preserved. Multiple Git blobs are separate objects, not append operations on one file.
- If Base64 is used, verify decoded byte count and decoded hash, not only encoded text length. For Git object verification, compare the returned blob SHA with the expected Git object ID; a plain file SHA-1 is not the same calculation.
- Keep transfers serial and bounded. Reuse already-verified chunks/objects and a concise transfer ledger. After a cancelled, timed-out or missing response, read back remote state before retrying so a successful write is not duplicated.
- A created blob alone is not remote preservation. Verify the tree/commit/reference or the storage manifest, then read back or reconstruct and verify the original byte identity before reporting success.
- Large-original transfer must not unnecessarily block independent implementation or validation, but required originals remain an unmet preservation item until the verified upload/reconstruction succeeds. Never fabricate evidence, silently weaken acceptance, or call a summary/hash a backup.
- Apply this rule to current task instructions, project briefs and handoffs. It replaces blanket `no large uploads` directions while preserving the task goal, authorization and evidence contract. Do not create or modify scheduled tasks without explicit authorization for that scheduling change.
