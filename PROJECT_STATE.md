# PROJECT_STATE

> Low-token recovery index. This file is a routing aid, not authority for mutable branch, SHA, CI, artifact, or task facts. Resolve those from GitHub when they matter. Do not preload historical handoffs, logs, or evidence unless the active task requires them.

## Resume path

1. Read `AGENTS.md` and applicable nested instructions.
2. Use this file to decide whether there is a matching active owner-authored PR.
3. Read `.github/CHATGPT_PROJECT_BRIEF.md` only for stable architecture, commands, or boundaries that are not already in context.
4. Read only the matching PR/diff and affected code, tests, configuration, workflows, and runbooks needed for the next decision.

## Current routing state

- As reviewed on 2026-09-07, there is no open owner-authored implementation PR to treat as the default continuation target.
- For a new authorized task, resolve the latest `main`, then create a feature branch and deliver through a PR as required by `AGENTS.md`.
- If a later matching PR exists, resume it instead of creating a replacement and treat its current body/comments as the mutable task-state authority.

## Guardrails

- Preserve broker/CTP ownership of external facts, fill-driven position state, RiskManager hard limits, verified restart evidence, reduction-first execution, D-to-D+1 causality, and fail-closed identity/schema/sequence checks.
- Do not infer real-money trading authority from bootstrap, doctor, research, Shadow, test, or documentation work.
- Do not restore legacy compatibility paths or create a second account/risk owner unless an explicit later task authorizes that architectural change.
- Use risk-driven progressive verification and avoid unrelated full-suite or stress reruns for behavior-neutral documentation changes.

## Freshness rule

Before acting on mutable state, verify the current `main`, matching open PRs, relevant checks, and the explicit task contract. If this index conflicts with current GitHub state or a later authorized task, GitHub/task authority wins and this file should be updated narrowly.

_Last reviewed: 2026-09-07._
