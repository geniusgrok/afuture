# afuture working agreement

## Scope and working method

Follow the current task's explicit scope and acceptance criteria within platform permissions; preserve business, safety, data and release contracts. Read applicable nested `AGENTS.md` before editing that directory. Resume still-valid authorization and surface material conflicts rather than silently relaxing them. Historical plans are context, not authority; analysis-only or approval-before-edit requests remain read-only until authorized.

For continuation, read `PROJECT_STATE.md`, resolve mutable branch/PR/SHA/check/artifact facts from GitHub and inspect existing local changes. Resume matching work. Consult `.github/CHATGPT_PROJECT_BRIEF.md` for relevant architecture, commands or boundaries; read the active diff and affected files, expanding only for uncertainty or impact. Reuse unchanged context rather than preloading history, skills or logs.

Skills provide methods, not additional authorization, approval or stopping gates. Respect platform requirements without adding repeated design confirmations, mandatory full workflows or ceremonial announcements to bounded authorized work. Prefer existing implementation/dependencies and the smallest sufficient change. Plan material decisions, batch related edits, and keep one writer per shared file, branch, runtime or evidence identity.

## Testing and verification

- Use risk-based, minimum sufficient verification based on changed behavior and real call paths. Do not enforce Superpowers TDD or fresh-verification-per-message workflows. Tests may precede or follow implementation; never delete working code solely because it was written before a test. For defects, prefer a minimal reproduction and necessary regression coverage; reuse existing tests rather than adding redundant or brittle implementation-detail assertions.
- Run the failing case and directly affected checks first. Expand for concrete cross-module risk, uncertain impact or explicit applicable acceptance. Neither each small edit nor each final delivery automatically requires all tools, the full suite or complete stress/economic matrices. Batch related fixes before expanding.
- Reuse results while covered behavior/code, tests, dependencies, configuration, data and relevant environment remain equivalent; only affected evidence is invalidated. Messages, handoffs, commits, pushes or new SHAs alone do not require rerunning unchanged checks. Record actual tested source and reuse scope; never present old CI results as a new HEAD's status. Actual required checks and explicit exact-revision acceptance still apply.
- Behavior-neutral documentation/comments need only relevant format, link, command or documentation-contract checks. Instruction changes also need ambiguity, duplication, conflict, authorization and completion-boundary review, not unrelated stress/economic reruns. Executable configuration, generated inputs and parsed documentation are not automatically behavior-neutral.
- For money, orders, risk, security or persistent-data changes, verify affected invariants and necessary integration, recovery or replay paths. Run expensive/full validation on a stable candidate only when impact or the applicable delivery contract requires it. Do not weaken safety checks, business gates, fixtures, frozen thresholds or data integrity to pass tests.
- Review the task diff once; repeat focused review after material changes. Reuse existing tests/logs, not a new verification framework. Report verified, reused, unrun and failed items with scope. A partial pass is not full acceptance; do not fabricate evidence, hide failures or bypass required reviews/checks and branch protection.

## afuture boundaries

The implementation is in `afuture/`; use `pyproject.toml`, `constraints/core-dev.txt`, `tests/` and `.github/workflows/ci.yml` for applicable engineering checks. Current safety contracts are indexed by `docs/documentation-index.md`; startup/live procedures are in `docs/live-trading.md`, `docs/production-checklist.md` and `docs/stress90-live-runbook.md`.

Broker/CTP events own external account and order facts; PositionBook advances only from fills, RiskManager owns hard limits, and StateStore holds verified restart evidence. Do not create a second account/risk owner from sidecars or reports. Preserve reduction-first execution, D-to-D+1 causality, fail-closed schema/sequence/checksum/identity checks and Shadow/live separation. Bootstrap, doctor, research, Shadow and test-counter results do not authorize real-money trading; do not automatically adopt damaged state or `.prev`.

## Execution, Git and recovery

Continue safe authorized work without repeated requests to continue. Resolve factual ambiguity by reading; ask only for a necessary decision that cannot be resolved safely. Authorization does not imply spending, live trading, credential/permission changes, irreversible actions, unrelated writes or scheduled-task changes.

Resume the matching active branch/PR. New work uses a feature branch from a verified default-branch SHA and is delivered through a PR; do not push directly to the default branch. Merge only with explicit authorization and required reviews/checks/protection satisfied. Save coherent milestones, push authorized changes and verify remote SHA. Keep mutable recovery state in the matching PR, not permanent instructions; no empty bootstrap commits or routine process files/Issues are required. Without explicit authorization, do not reset, clean, rebase, force-push, rewrite history, delete branches/worktrees, discard unknown work or overwrite unrelated changes. Never commit secrets.

A failed check or candidate calls for diagnosis and an evidence-supported correction or bounded alternative, not automatic termination or repetitive trials. Stay within scope and budget, preserve failures and continue independent work past local blockers. Finish when applicable acceptance and actual required checks are satisfied; do not add marginal optimization afterward. A checkpoint, PR or partial pass alone is not completion. If no safe authorized action remains, preserve recoverable progress and report completed, failed, blocked and unverified items without promising background completion.

## Git/GitHub transfer and preservation

- Inspect identities, sizes and outgoing paths before transfers; select necessary files and reuse unchanged references rather than entire working directories. Prefer authorized native Git or file-aware programmatic transfer; when unavailable, use the authorized connector without extracting credentials. Do not repeat a confirmed-failed transport.
- Keep large bodies out of model/tool output and arguments: no complete Base64, huge JSON, archives, full logs or chunk-then-giant-request uploads. Prefer file-to-file transfer, selective/range reads and bounded excerpts. String-only connectors are for small recoverable batches.
- Required large originals still need preservation. Use supported resumable or deterministic multipart transport where necessary, recording order, offsets, raw/encoded lengths and hashes. Verify byte-for-byte reconstruction and expected Git/LFS object identities where applicable. Separate blobs are not append operations; a summary/hash is not a backup.
- Keep large transfers bounded and serial. Reuse a concise ledger of local/remote locations, identities and verification status; inspect state after interruption before retrying. Verify final local bytes and remote tree/commit/ref or storage manifest, not merely a created blob. Report unpreserved originals and temporary-runtime loss risk while continuing independent work. Never replace required evidence with summaries or rewrite historical evidence.
