# Return Target 100 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and validate a low-gross-leverage multi-alpha futures portfolio research engine that prioritizes >=100% annualized historical return, then wire only genuinely surviving signals into afuture's existing risk/execution boundary.

**Architecture:** Keep production order/account/fill semantics unchanged. Add a standalone return-target research engine that generates causal same-exchange long/short pairs from multiple alpha families, optimizes a bounded template set, and dynamically rotates among historically strongest templates. Promote only candidates that survive specific-contract roll-safe validation; production integration is a thin policy layer over existing AutoPairManager/RiskManager/PairExecutor.

**Tech Stack:** Python 3.10/3.13, pandas, numpy, AKShare/Sina via GitHub Actions, existing afuture execution/risk stack.

**Spec:** `docs/archive/development/specs/2026-08-22-return-target-100-design.md`

## Global Constraints

- Primary historical target: base-cost annualized return >= 100% on `full_recent`.
- Base/stress/extreme one-way costs: 5bp / 15bp / 30bp.
- Research gross leverage hard cap: 2.0 in the primary search.
- Decisions at date t may earn only t+1 returns.
- Continuous contracts are L3 discovery only; production promotion requires roll-safe specific contracts.
- Existing RiskManager/PairExecutor/account/order/fill state semantics must not be bypassed.
- `pristine_final_oos=false` remains explicit because the historical window has already been observed.
- Verification follows `AGENTS.md`: L1 targeted tests, L2 smoke, L3 broad data, L4 surviving-candidate validation, one final full CI.

---

### Task 1: Causal Return-Target Research Core

**Files:**
- Create: `tools/evaluate_return_target_portfolio.py`
- Create: `tools/test_return_target_portfolio.py`

**Interfaces:**
- Consumes: broad daily CSV columns `date, product, close, volume, hold`.
- Produces: `AlphaTemplate`, `build_panel(raw)`, `signal_scores(...)`, `simulate_template(...)`, `evaluate_templates(...)`, `select_portfolio(...)`, `evaluate(raw) -> dict`.

- [ ] **Step 1: Write failing causality/pairing/gross/cost tests**

```python
# tools/test_return_target_portfolio.py
from pathlib import Path
import importlib.util, sys
import numpy as np
import pandas as pd

spec = importlib.util.spec_from_file_location(
    "return_target", Path(__file__).with_name("evaluate_return_target_portfolio.py")
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

# Synthetic exchange universe: A persistently strongest, D weakest.
dates = pd.date_range("2024-01-01", periods=180, freq="B")
returns = pd.DataFrame({
    "A": np.full(len(dates), 0.010),
    "B": np.full(len(dates), 0.003),
    "C": np.full(len(dates), -0.002),
    "D": np.full(len(dates), -0.009),
}, index=dates)
meta = {name: "DCE" for name in returns.columns}
template = module.AlphaTemplate("momentum", 20, 0, 20, 1, 1, 1.0)
series, audit = module.simulate_template(returns, meta, template, cost_bps=0.0)
assert series.iloc[:20].abs().sum() == 0.0
assert series.iloc[25:].mean() > 0.0
assert max(row["gross"] for row in audit if row["gross"] is not None) <= 1.0 + 1e-12

costed, _ = module.simulate_template(returns, meta, template, cost_bps=30.0)
assert costed.sum() < series.sum()

# Disjoint-leg rule: no product appears in two simultaneous pairs.
for row in audit:
    legs = row.get("legs", [])
    assert len(legs) == len(set(legs))
```

- [ ] **Step 2: Run targeted test and confirm red**

Run in CI/local equivalent: `python tools/test_return_target_portfolio.py`
Expected: import/file failure because implementation does not exist.

- [ ] **Step 3: Implement bounded alpha template model and causal simulator**

Core definitions:

```python
@dataclass(frozen=True)
class AlphaTemplate:
    family: str
    slow: int
    fast: int
    vol_window: int
    rebalance: int
    max_pairs: int
    gross_leverage: float
```

`signal_scores()` implements:
- `momentum`: lagged rolling log return over `slow`;
- `reversal`: negative lagged rolling log return over `fast`;
- `slow_fast`: z-scored slow momentum minus 0.5 * z-scored fast momentum;
- `breakout`: lagged close location within rolling high/low proxy from close history plus slow return acceleration.

`simulate_template()`:
- forms pairs only within the same exchange;
- long highest scores, short lowest scores;
- prevents product overlap;
- inverse-vol normalizes leg weights;
- caps sum(abs(weights)) to `gross_leverage`;
- applies turnover * cost_bps / 10000;
- uses weights decided from t-1 information for t realized returns.

- [ ] **Step 4: Run targeted test and make it green**

Run: `python tools/test_return_target_portfolio.py`
Expected: exit 0.

- [ ] **Step 5: Commit checkpoint**

Commit message: `feat: add causal return-target alpha engine`.

---

### Task 2: Bounded Search, Dynamic Alpha Rotation, and Reporting

**Files:**
- Modify: `tools/evaluate_return_target_portfolio.py`
- Modify: `tools/test_return_target_portfolio.py`

**Interfaces:**
- Produces report keys: `template_count`, `selection`, `base`, `stress`, `extreme`, `family_contribution`, `target`, `pristine_final_oos`.

- [ ] **Step 1: Add failing selection and no-lookahead tests**

```python
# Template with strong first 120 days then weak tail must be chosen only from
# the explicit calibration slice, not future evaluation days.
left = pd.Series([0.01] * 120 + [-0.01] * 60)
right = pd.Series([0.002] * 180)
selected = module.choose_templates(
    {"left": left, "right": right},
    start=left.index[0], end=left.index[119], count=1
)
assert selected == ["left"]
```

Also assert primary template grid has `gross_leverage <= 2.0` and contains at least momentum, reversal, slow_fast, breakout.

- [ ] **Step 2: Run targeted test and confirm red on missing APIs**

Run: `python tools/test_return_target_portfolio.py`.

- [ ] **Step 3: Implement staged optimizer**

Use a finite template set built from economically meaningful combinations, not Cartesian explosion. Evaluate each template on prior1/prior2/train/validation/full_recent at base/stress costs.

Selection objective on train+validation:

```python
score = (
    4.0 * annualized_return
    + 0.5 * sharpe
    + 0.5 * min(train_return, validation_return)
    - 2.0 * abs(max_drawdown)
    - 0.25 * annual_turnover
)
```

Dynamic rotation:
- template daily return streams are computed causally;
- every 5 trading days, rank templates using the preceding 60/120 days only;
- select top 1-3 templates with positive rolling return and drawdown > -20%;
- average selected template returns, then apply a portfolio gross multiplier <=2.0 chosen from calibration only;
- if no template qualifies, stay flat.

- [ ] **Step 4: Add report target gate**

```python
report["target"] = {
    "annualized_return": 1.0,
    "max_drawdown": -0.30,
    "gross_leverage_cap": 2.0,
    "target_met": (
        base_full_recent["annualized_return"] >= 1.0
        and base_full_recent["max_drawdown"] > -0.30
        and stress_full_recent["annualized_return"] > 0
        and active_days >= 50
    ),
}
report["pristine_final_oos"] = False
```

- [ ] **Step 5: Run targeted test and commit**

Run: `python tools/test_return_target_portfolio.py`.
Commit: `feat: optimize and rotate return-target alphas`.

---

### Task 3: GitHub L3 Real-Data Search Workflow

**Files:**
- Create: `.github/workflows/research-return-target.yml`
- Modify: `tools/fetch_broad_daily_universe.py` only if exchange metadata is not already emitted.

**Interfaces:**
- Workflow artifact: `runtime/return_target_report.json`, `runtime/return_target_template_results.csv`, `runtime/broad_daily_universe.csv`.

- [ ] **Step 1: Add workflow that runs only for return-target paths or manual dispatch**

```yaml
name: research-return-target
on:
  workflow_dispatch:
  pull_request:
    paths:
      - "tools/evaluate_return_target_portfolio.py"
      - "tools/test_return_target_portfolio.py"
      - "tools/fetch_broad_daily_universe.py"
      - ".github/workflows/research-return-target.yml"
```

- [ ] **Step 2: Workflow commands**

```yaml
- run: python -m pip install akshare pandas numpy
- run: python tools/test_return_target_portfolio.py
- run: python tools/fetch_broad_daily_universe.py
- run: python tools/evaluate_return_target_portfolio.py
- uses: actions/upload-artifact@v4
  if: always()
  with:
    name: return-target-evidence
    path: |
      runtime/broad_daily_universe.csv
      runtime/broad_daily_universe_summary.csv
      runtime/return_target_report.json
      runtime/return_target_template_results.csv
```

- [ ] **Step 3: Push checkpoint and open PR to trigger L3**

PR remains a research branch; do not merge yet.

- [ ] **Step 4: Inspect artifact and decide by evidence**

If base `full_recent` >=100% with drawdown >-30% and stress positive, freeze the winner and proceed to Task 4.

If not, add only one bounded second-wave family from this list, in order, rerunning L3 after each material change:
1. dispersion-gated relative momentum;
2. volatility-breakout acceleration;
3. rolling template ensemble with 20/60/120-day meta lookback;
4. same-exchange pair residual momentum rather than absolute product momentum.

Stop expanding once the new family fails to materially improve annualized return or introduces leakage/cost fragility.

---

### Task 4: Specific-Contract L4 for Surviving Return-Target Pairs

**Files:**
- Create or modify: `tools/fetch_return_target_specific_daily.py`
- Create: `tools/evaluate_return_target_specific.py`
- Create: `tools/test_return_target_specific.py`
- Extend: `.github/workflows/research-return-target.yml`

**Interfaces:**
- Consumes only product roots actually selected by the frozen L3 candidate.
- Produces `runtime/return_target_specific_report.json` with roll-safe and cost metrics.

- [ ] **Step 1: Write failing roll test**

Synthetic two-contract series must prove selection at t earns t→t+1 on the same symbol even when OI switches at t+1.

- [ ] **Step 2: Implement roll-safe panel builder**

Reuse the already-correct pattern in `evaluate_specific_pair_rotation.py`: delivery blackout, OI/volume ranking, same selected contract next-day return, missing-next fail closed.

- [ ] **Step 3: Reconstruct frozen L3 signal on roll-safe indexes**

No new parameter search in L4. Apply frozen family/template/meta-rotation settings exactly.

- [ ] **Step 4: L4 target gate**

Require:
- selected_leverage <=2.0;
- base full_recent annualized >=100% for formal target success;
- stress annualized >0;
- max drawdown >-30%;
- no roll/data-quality violation.

If L4 drops below target, report the exact L3→L4 degradation and do not silently tune on L4.

- [ ] **Step 5: Commit L4 milestone**

Commit: `research: validate return target on specific contracts`.

---

### Task 5: Production Policy Integration for a Passing Candidate

**Files:**
- Create: `afuture/aggressive_portfolio.py`
- Modify: `afuture/auto.py`
- Modify: `afuture/config.py`
- Modify: `config/afuture.live.example.toml`
- Create: `tests/test_aggressive_portfolio.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class AggressivePortfolioConfig:
    enabled: bool = False
    max_selected_pairs: int = 1
    max_risk_multiplier: float = 1.0

class AggressivePortfolioPolicy:
    def select(
        self,
        candidates: list[tuple[PairConfig, float]],
        protected_pair_ids: set[str],
    ) -> list[PairConfig]: ...
```

- [ ] **Step 1: Write failing policy tests**

Prove the policy:
- never removes a protected/managed pair;
- never selects more than configured pairs;
- never changes order, account, fill or RiskManager state;
- only ranks already hard-gate-eligible pairs.

- [ ] **Step 2: Implement policy as a pure selector**

No broker dependency and no order creation.

- [ ] **Step 3: Integrate behind explicit config**

Only enable defaults if Task 4 formally meets the target. Otherwise keep disabled and retain research tools only.

- [ ] **Step 4: Run affected Auto/risk/execution tests**

Run targeted tests for `auto`, aggressive policy, risk manager, pair executor.

- [ ] **Step 5: Commit production integration**

Commit only if L4 passed; otherwise skip Task 5 behavior changes.

---

### Task 6: Final Review, Documentation, and Main Merge

**Files:**
- Modify: `README.md`
- Modify: `docs/data-and-backtest.md`
- Modify: `docs/archive/evidence/research-final-evidence.md`
- Add: `docs/return-target-100-results.md`

- [ ] **Step 1: Update docs with exact measured result**

Separate:
- optimization/selection metrics;
- descriptive full-history metrics;
- non-pristine OOS metrics;
- L3 vs L4;
- leverage and cost assumptions;
- whether production integration is enabled.

- [ ] **Step 2: Request/perform code review**

Review for leakage, roll artifacts, cost accounting, gross cap, product overlap, production permissions, stale docs, and failure-open behavior.

- [ ] **Step 3: Fix Critical/Important findings and rerun affected tests only**

Do not rerun L3/L4 for documentation-only changes.

- [ ] **Step 4: Run one complete final CI on stable candidate**

Require Python 3.10 and 3.13 jobs green.

- [ ] **Step 5: Squash merge PR to `main` and verify remote main SHA/tree**

Final response must report the actual achieved annualized return, drawdown, costs, leverage, whether target was met, and the exact main commit.
