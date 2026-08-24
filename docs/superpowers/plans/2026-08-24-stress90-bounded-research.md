# Stress90 Bounded Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 以最多三个 hypothesis family、六次 Production quick run 判断 Stress80 winner 能否在不放宽风险门和不做参数搜索的前提下晋级到至少 85%。

**Architecture:** 先构建 behavior-neutral Production attribution，并用总账 reconciliation 定位 top 3 structural opportunity；随后把每个候选规则预先写入 ledger，按 TDD、cheap gate、五窗口 quick gate 推进。只有过门 winner 才复制到 clean promotion branch，未过门则保留负证据而不改 main。

**Tech Stack:** Python 3.10+、pandas、numpy、pytest、现有 deterministic Production simulator、GitHub Actions。

**Spec:** `docs/superpowers/specs/2026-08-24-stress90-bounded-research-design.md`

## Global Constraints

- 基线必须精确复现 `main@b4207abb50aca1e39d5ebba3affc04765857251a` 和候选 digest `8e38dbf6441b561dd1728df08665b94b15cc3358823257505c2fcb9d63f09f28`。
- target/realized gross `<=2x`、margin `<=35%`、available `>=25%`、daily loss `5%`、total DD `30%`、max lots `35`。
- reduction-first、RiskManager、Broker/CTP truth、circuit/HALT authority 不变；研究模块不得接入 live runtime。
- 每个 hypothesis 在结果前冻结 exact rule；每 family 最多一个 canonical implementation 与一个 correctness fix。
- family `<=3`、每 family candidates `<=2`、Production quick runs `<=6`、full matrix `<=2`。
- Stress `<85%` 的候选不得晋级；六次 quick 均失败时停止。

---

### Task 1: Stress80 behavior-neutral attribution

**Files:**
- Create: `afuture/directional_stress80_attribution.py`
- Create: `tests/test_directional_stress80_attribution.py`
- Create: `tools/analyze_directional_stress80_production.py`
- Create: `docs/stress90-bounded-research-evidence.md`

**Interfaces:**
- Consumes: `ProductionSimulationResult.daily/events`, `SUPPORTED_PRODUCTS`, fixed Stress80 candidate builder and simulator.
- Produces: `build_stress80_attribution(daily, events, supported_products, initial_capital) -> dict`, plus JSON/CSV evidence artifacts under ignored `runtime/`.

- [ ] **Step 1: Write failing reconciliation and classification tests**

Construct a two-product synthetic ledger that includes entry, increase, reduction, reversal, roll, gap/intraday PnL, risk-day and capacity columns. Assert exact totals and the fixed duration buckets.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `.venv/bin/python -m pytest -q tests/test_directional_stress80_attribution.py`

Expected: FAIL because `afuture.directional_stress80_attribution` does not exist.

- [ ] **Step 3: Implement minimal behavior-neutral ledger and summary**

Implement deterministic event ordering, resize split, lifecycle action/duration tagging, product/action/risk/OI/turnover/capacity summaries, realized peak-to-trough contribution, and explicit reconciliation errors.

- [ ] **Step 4: Add CLI wrapper without changing the final evaluator**

Load the five fixed inputs, rebuild the exact candidate once, simulate Stress full_recent once, call the attribution module, and write `runtime/stress90_p0_attribution.json` plus audit CSVs. Assert the fixed digest and Stress80 baseline metrics before writing evidence.

- [ ] **Step 5: Verify GREEN and affected Production mechanics**

Run: `.venv/bin/python -m pytest -q tests/test_directional_stress80_attribution.py tests/test_directional_attribution.py tests/test_directional_stress80_final_gate.py`

Run: `.venv/bin/python tools/test_directional_production_mechanics.py`

- [ ] **Step 6: Run P0 once and record top 3 structural opportunity**

Run: `.venv/bin/python tools/analyze_directional_stress80_production.py --output runtime/stress90_p0_attribution.json`

Append factual P0 results, reconciliation, prior/recent regime comparison and top 3 opportunities to `docs/stress90-bounded-research-evidence.md`.

- [ ] **Step 7: Commit milestone**

Commit the P0 attribution implementation, tests and evidence. Do not include runtime artifacts.

### Task 2: Freeze hypothesis ledger and implement bounded candidates

**Files:**
- Modify: `docs/stress90-bounded-research-evidence.md`
- Create/Modify: one research-only module and focused test per selected family.
- Create/Modify: one local evaluator wrapper that composes candidates with the current winner.

**Interfaces:**
- Consumes: P0 top 3 opportunity, only completed causal inputs, current winner weights.
- Produces: candidate weight path with support/sign/gross invariants and a predeclared five-window quick result.

- [ ] **Step 1: Write family declaration before implementation**

For the selected family, append economic rationale, causal inputs, allowed degrees of freedom, exact rule, expected mechanism, promotion gate and rejection condition. Record quick counter before execution.

- [ ] **Step 2: Write focused failing tests**

Tests must prove D→D+1/completed-history alignment, missing/stale fail-closed behavior, no support creation, no sign flip, no gross expansion, and unchanged risk-reducing actions.

- [ ] **Step 3: Run RED, implement the canonical rule, run GREEN**

Run only the new test module and directly affected Stress80 component tests. A result-driven threshold/lookback/product/window/weight change is prohibited.

- [ ] **Step 4: Run cheap diagnostic gate**

Reject before Production quick if lineage/digest invariants, causal checks, Base proxy economics, or structural mechanism fail. Record negative evidence and move to the next family.

- [ ] **Step 5: Consume at most one quick run for the canonical candidate**

Run Base full_recent plus Stress train/validation/OOS/full_recent as independent accounts. Record annualized return, DD, HALT, margin rejects, gross, max lots, turnover, cost, net alpha and net/turn.

- [ ] **Step 6: Apply at most one correctness fix**

Only an identified bug, data-definition error, leakage or incorrect economic implementation permits a second candidate in the same family. Document root cause before the fix; otherwise reject the family.

- [ ] **Step 7: Repeat until winner or bounded stop**

Stop immediately when a candidate satisfies every promotion gate, or when three families/six quick runs are exhausted. Commit each meaningful family result and its negative evidence.

### Task 3: Promotion or bounded closeout

**Files:**
- Modify: `docs/stress90-bounded-research-evidence.md`
- Modify/Create: only winner dependency closure and final tests when a winner exists.
- Remove: temporary research workflows/evaluators from the promotion tree.

**Interfaces:**
- Consumes: a quick-gate winner with Stress `>=85%`, or bounded negative evidence.
- Produces: clean promoted main or unchanged main with auditable closeout.

- [ ] **Step 1: Decide promotion eligibility without discretion**

If no candidate reaches Stress `85%` and every hard gate, do not create a promotion branch and do not change main. Finalize negative evidence and next-data recommendation.

- [ ] **Step 2: For a winner, create clean promotion branch from unchanged main**

Copy/cherry-pick only the winner dependency closure, final tests and evidence. Confirm no live runtime import and no temporary workflow remains.

- [ ] **Step 3: Run fresh full matrix at most twice**

Run Base full_recent, Stress prior1/prior2/train/validation/OOS/full_recent in parallel accounts. A second full matrix is allowed only after a material correctness fix.

- [ ] **Step 4: Review and permanent-tree verification**

Run focused affected tests, code review, Python 3.10 full CI and Python 3.13 full CI once on the final permanent tree. Confirm tested tree SHA/digest matches merge tree.

- [ ] **Step 5: Merge or close out**

Confirm remote main still equals the expected base, squash merge the clean PR, verify main merge SHA and CI-tested tree equality. If no winner, keep `b4207ab` unchanged and report that outcome explicitly.

