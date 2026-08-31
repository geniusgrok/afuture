# Signal Evidence and Repository Gate Closure Design

## Goal

Close five evidence-boundary gaps without changing any legal-input strategy, account,
candidate, risk, or live-authorization economics established by PR #37.

## Scope

The change is limited to:

1. UNKNOWN-aware normalized signal paths and stale-target clearing.
2. Offline directional diagnostic equity/return consistency and boundary metadata.
3. Fixed historical-research authorization metadata on Stress-80/90 output.
4. Standard CI trigger deduplication.
5. Post-merge strict required-check configuration on the existing `Protect main` ruleset.

No Alpha, template, parameter, product, cost, candidate, digest, risk threshold, account
state, CLI, dependency, simulator, or live activation behavior may change.

## Signal semantics

`_normalized_price()` preserves the first natural no-prior-return row as the same neutral
baseline used by the frozen legal-input implementation. Every later non-finite return is
UNKNOWN: its normalized price is unavailable, the path segment is broken, and subsequent
prices start a new segment instead of compounding across the gap. Moving-average and
breakout rolling operations therefore remain unavailable until their complete price
window is finite again.

`_template_weight_path()` keeps scheduled rebalance semantics, but clears a previously
held product target whenever the immediately lagged score for that product is no longer
finite. It does not select a replacement between scheduled rebalances. Thus an invalid
signal never persists as stale risk, while complete legal histories retain byte-identical
template and final policy outputs. The unused intraday calculation in `weight_history()`
is removed; the independent `_intraday_proxy_stream()` active-exposure fail-closed path
is unchanged.

## Diagnostic consistency

All current callers of `summarize_production_attribution()` are offline account
simulators with no external cash flows. The diagnostics validate positive finite
`initial_capital`, positive finite equity, and reported returns against:

- first session: `equity[0] / initial_capital - 1`;
- later sessions: `equity[t] / equity[t-1] - 1`.

The fixed comparison uses `rel_tol=1e-12` and `abs_tol=1e-12`. The first mismatch raises
a deterministic error containing the date, reported and expected return, prior or initial
equity, and current equity. No value is rewritten.

Removing the sole observed calendar month yields `null`, not `0.0`. The selected worst
quarter and best month gain observed-session counts and a sample-boundary flag. The flag
only means that the period is the sample's first or last observed period; it makes no
exchange-calendar completeness claim.

## Historical research boundary

Every Stress-80 and Stress-90 window payload, and the assembled Stress-90 matrix, adds:

```json
{
  "evidence_scope": "historical_research_only",
  "live_authorized": false,
  "risk_increase_authorized": false,
  "prospective_evidence": false
}
```

Stress-90 assembly validates presence, exact type, and exact value before the unchanged
frozen gate is evaluated. Existing roles, candidate digests, manifests, constraints,
result fields, and gate calculations remain unchanged.

## CI and ruleset

Standard CI runs on pull requests targeting `main` and pushes to `main` only. Its four job
names and job bodies remain unchanged. After the code PR succeeds and is squash merged,
the existing ruleset is updated in place: strict required checks are enabled and all four
contexts are bound to the integration ID observed from that PR's actual check runs. The
target, no-bypass state, PR requirement, deletion block, force-push block, review count,
and merge methods remain otherwise unchanged.

## Verification

Use test-first regressions for each behavioral requirement, then the affected policy,
attribution, Stress evaluator, workflow, caller, and documentation suites. The final tree
must pass pip check, Ruff lint/format, mypy, compileall, standard non-live configuration
validation, one full pytest run, one focused independent review, required PR CI, and
post-merge main CI. The five-input historical matrix is run only if the exact inputs are
already present and SHA-matched.
