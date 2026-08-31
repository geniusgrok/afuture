# Signal Evidence and Repository Gate Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close signal UNKNOWN, offline diagnostic, historical-evidence, CI duplication,
and main-ruleset gaps without changing legal-input economics.

**Architecture:** Keep all changes inside existing pure helpers, evaluator payloads,
workflow triggers, and the existing repository ruleset. Test each boundary before the
minimal implementation, checkpoint coherent groups remotely, and preserve frozen
strategy/candidate/risk contracts.

**Tech Stack:** Python 3.10/3.13, pandas, NumPy, pytest, Ruff, mypy, GitHub Actions, GitHub rulesets.

**Spec:** `docs/superpowers/specs/2026-08-31-signal-evidence-gate-closure-design.md`

## Global Constraints

- Preserve all 96 templates and order, product pool, Meta constants/formulas, costs,
  candidates/digests, HHI/drawdown/survivor behavior, scaling, and risk thresholds.
- Preserve D→D+1, reduction-first, Broker/CTP truth, `PositionBook`, and `RiskManager` authority.
- Do not add Alpha, parameters, state machines, dependencies, CLI/config, simulators, or live authority.
- Do not run or fabricate the fixed historical matrix without all five exact inputs.
- Do not reset, clean, rebase, force-push, delete branches/worktrees, or push `main` directly.

---

### Task 1: UNKNOWN-aware signal paths

**Files:**
- Modify: `tests/test_execution_aligned_policy.py`
- Modify: `afuture/execution_aligned_policy.py`

**Interfaces:**
- Consumes: close-to-close returns produced with `pct_change(fill_method=None)`.
- Produces: `_normalized_price(returns: pd.DataFrame) -> pd.DataFrame` with segmented
  UNKNOWN semantics and `_template_weight_path(...)` with stale-target clearing.

- [ ] **Step 1: Record the legal-history compatibility oracle**

  Compute and hand-record stable digests for all frozen template scores, template weight
  paths, and final policy weights on the existing deterministic `_history()` fixture.

- [ ] **Step 2: Add failing normalized-price and template tests**

  Add tests proving moving-average and breakout gaps differ from observed `0.0`, do not
  cross gaps, recover only after a complete finite window, accept observed zero, and clear
  stale product targets without selecting replacements off-cycle.

- [ ] **Step 3: Run the new tests and verify RED**

  Run the named new tests from `tests/test_execution_aligned_policy.py`. Confirm failures
  specifically show gap-crossing, stale-target retention, or compatibility mismatch.

- [ ] **Step 4: Implement segmented normalized prices**

  Keep row zero as the natural neutral baseline. For later rows, emit NaN and reset the
  product segment on non-finite returns; compound only finite values inside one segment.

- [ ] **Step 5: Clear stale targets and remove dead code**

  Before each output row, zero current products whose lagged score is non-finite. Keep
  scheduled selection unchanged and remove the unused `intraday` calculation from
  `weight_history()`.

- [ ] **Step 6: Run policy L1/L2 verification**

  Run all new tests, `tests/test_execution_aligned_policy.py`, execution-aligned runtime,
  lineage, opportunity integration, alpha-efficiency, causality, and active-proxy tests.

- [ ] **Step 7: Create Checkpoint A**

  Commit the coherent signal change, publish it as a child of the verified remote head,
  verify the remote SHA/tree, and update the PR body before continuing.

---

### Task 2: Equity/return diagnostic consistency

**Files:**
- Modify: `tests/test_directional_attribution.py`
- Modify: `afuture/directional_attribution.py`
- Modify: `docs/data-and-backtest.md`

**Interfaces:**
- Consumes: offline simulator `daily`, audit `events`, and positive finite `initial_capital`.
- Produces: the existing diagnostics plus four boundary fields and strict path validation.

- [ ] **Step 1: Add failing accounting tests**

  Cover first-day and later-day mismatch, strict-tolerance acceptance, detailed errors,
  invalid initial capital, and positive-equity preservation.

- [ ] **Step 2: Add failing empty-month and boundary tests**

  Require `None` after removing the sole month, correct multi-month proxy, observed-session
  counts, first/last boundary flags, middle-period false flags, backward-compatible fields,
  and input immutability.

- [ ] **Step 3: Verify diagnostic RED failures**

  Run only the new named attribution tests and confirm each fails because the intended
  validation or field is absent.

- [ ] **Step 4: Implement strict path validation**

  Validate initial capital and daily path without rewriting inputs. Use fixed
  `rel_tol=1e-12` and `abs_tol=1e-12`; raise on the first mismatch with all required facts.

- [ ] **Step 5: Implement null and boundary metadata**

  Return `None` for an empty remaining-month sample. Count observed sessions for the
  selected quarter/month and mark only sample-first or sample-last periods as boundaries.

- [ ] **Step 6: Document the offline caller and field semantics**

  State that current callers are no-external-cash-flow offline simulators, `null` means no
  remaining sample, and boundary flags do not prove calendar-period completeness.

- [ ] **Step 7: Run attribution/caller L1/L2 verification**

  Run the attribution suite plus the directional mechanics and MPV tests that exercise
  `summarize_production_attribution()`.

---

### Task 3: Historical research authorization metadata

**Files:**
- Modify: `tests/test_directional_stress90_final.py`
- Modify: `tests/test_directional_stress80_final_gate.py`
- Modify: `tools/evaluate_directional_stress80_final.py`
- Modify: `tools/evaluate_directional_stress90_final.py`
- Modify: `docs/stress90-final-evidence.md`
- Modify: `docs/data-and-backtest.md`
- Modify: `docs/production-checklist.md`

**Interfaces:**
- Consumes: independently produced Stress-80/90 window dictionaries.
- Produces: exact top-level research-only fields on every window and assembled matrix;
  assembly rejects any missing, mistyped, or non-fixed value before gate evaluation.

- [ ] **Step 1: Add failing metadata tests**

  Cover the valid boundary, every missing field, every forbidden true value, incorrect
  scope/type, assembled output propagation, and unchanged gate/candidate/manifest data.

- [ ] **Step 2: Verify evaluator RED failures**

  Run the new Stress-80/90 tests and confirm failure because metadata is absent or not validated.

- [ ] **Step 3: Implement one fixed boundary helper**

  Return a fresh dictionary containing the four exact fields. Add it to Stress-80 and
  Stress-90 window payloads without changing existing fields.

- [ ] **Step 4: Validate before the frozen gate**

  In Stress-90 assembly, require exact key presence, exact native types, and exact values
  before collecting results or calling `evaluate_stress90_gate()`. Add the same fixed fields
  to the assembled matrix.

- [ ] **Step 5: Tighten documentation**

  Explicitly state that historical `passed`/`promotion_gate` is research-contract output,
  not prospective evidence, live authorization, risk permission, or commissioning evidence.

- [ ] **Step 6: Run Stress evaluator/documentation L1/L2 verification**

  Run Stress-80/90 final/gate, manifest, candidate digest, workflow documentation, and
  directly related evaluator tests.

- [ ] **Step 7: Create Checkpoint B**

  Commit diagnostics, research metadata, and documentation as one coherent checkpoint;
  publish, verify remote SHA/tree, and refresh the PR body.

---

### Task 4: Standard CI deduplication

**Files:**
- Modify: `tests/test_workflow_contracts.py`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: push only on `main`, pull requests only to `main`, with unchanged `quality`,
  `test (3.10)`, `test (3.13)`, and `windows-smoke` jobs.

- [ ] **Step 1: Add failing workflow behavior contract**

  Parse the trigger block structurally enough to prove both branch filters, all four job
  names, no allow-failure, and unchanged manual research workflows.

- [ ] **Step 2: Verify workflow RED failure**

  Run `tests/test_workflow_contracts.py` and confirm it fails because branch filters are absent.

- [ ] **Step 3: Restrict standard triggers**

  Change only the `on.push.branches` and `on.pull_request.branches` configuration to `main`.

- [ ] **Step 4: Run affected tests and engineering gates**

  Run the workflow contract and all affected policy/diagnostic/evaluator suites, followed by
  pip check, Ruff lint/format, mypy, compileall, and non-live configuration validation.

- [ ] **Step 5: Run the stable final full suite once**

  Run `python -m pytest -q`; run the fixed historical matrix only if all five exact files
  already exist and verify against their frozen SHA-256 values.

- [ ] **Step 6: Perform one focused independent review**

  Review the complete base-to-head diff against the 13 requested focus areas. Reproduce and
  fix only Critical/Important findings, allowing one focused re-review.

- [ ] **Step 7: Create Checkpoint C and finalize the PR**

  Commit the CI/final-candidate state, publish and verify it, update the non-Draft PR body
  with exact evidence, and wait for all four required checks.

---

### Task 5: Merge and strict ruleset closure

**Files:**
- External: PR `Close signal evidence and repository gate gaps`
- External: GitHub ruleset `Protect main` (`21870776` if still current)

**Interfaces:**
- Consumes: actual successful PR-head check runs and their GitHub App IDs.
- Produces: squash-merged main plus an API-verified strict default-branch ruleset.

- [ ] **Step 1: Read actual PR-head check integrations**

  Confirm exact contexts and App IDs from GitHub check runs; do not reuse baseline IDs without verification.

- [ ] **Step 2: Squash merge without bypass**

  Require all checks successful, no conflicts, and no unresolved Critical/Important finding;
  then squash merge the exact verified head.

- [ ] **Step 3: Verify post-merge main**

  Verify the PR merge SHA, latest remote main, matching tree, no duplicate open task PR, and
  successful main-push versions of all four jobs.

- [ ] **Step 4: Update the existing ruleset in place**

  Set strict status checks true and bind exactly all four contexts to their observed App ID.
  Preserve default-only target, active enforcement, PR requirement, zero approvals, no
  bypass, deletion/non-fast-forward blocks, and existing merge methods.

- [ ] **Step 5: API-read back final governance**

  Verify strict=true, four exact integration-bound contexts, no bypass/current-user bypass,
  default-only target, PR requirement, deletion/non-fast-forward blocks, and no nonexistent check.

- [ ] **Step 6: Publish the final verified report**

  Distinguish completed static code/governance from the unrun five-input matrix, external
  data archival, target ABI, Shadow, test-counter, small-live, and risk-commissioning work.
