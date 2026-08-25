# P0/P1 Runtime Hardening Implementation Plan

> **归档说明：** 本文保存 2026-08-25 P0/P1 runtime hardening 的实施计划。当前运行契约以 `docs/architecture.md`、`docs/configuration.md`、`docs/data-and-backtest.md`、`docs/live-trading.md` 和 `docs/production-checklist.md` 为准。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the confirmed P0/P1 runtime correctness gaps without changing strategy economics.

**Architecture:** Keep the current domain/runtime boundaries. Tighten validation at configuration and persistence boundaries, make directional market evidence restart-safe, give critical broker events bounded-latency delivery, require authoritative broker trading days, normalize broker identities, and add a small durable cache for verified directional OHLC inputs.

**Tech Stack:** Python 3.10+, dataclasses, TOML, JSON, pandas, pytest, Ruff, mypy.

**Spec:** The approved P0/P1 findings in the 2026-08-25 adversarial review and current architecture contracts in `docs/architecture.md`.

## Global Constraints

- Preserve current strategy signals, frozen template weights, leverage, sizing, costs, stress scenarios, and acceptance windows.
- Broker fills remain the only account and position truth.
- Live paths fail closed on malformed, stale, ambiguous, or unverifiable inputs.
- Reuse existing modules; do not add databases, services, brokers, or framework layers.
- Each behavior change starts with a failing regression test and receives targeted verification before broader tests.

---

### Task 1: Strict configuration boundary

**Files:**
- Modify: `afuture/config.py`, `afuture/risk.py`, `afuture/directional.py`, `afuture/auto.py`
- Test: `tests/test_config.py`, `tests/test_risk.py`

**Interfaces:**
- Consumes: TOML sections loaded by `load_config(path)`.
- Produces: strict section/key/type validation and finite numeric domain values.

- [ ] Add failing tests for unknown fields, `nan`, `inf`, lossy integer conversion, and string-to-bool coercion.
- [ ] Run the focused tests and confirm failures identify the current permissive boundary.
- [ ] Add shared strict-key, exact-bool/integer, and finite-number helpers; apply them to every executable configuration section and nested fee row.
- [ ] Run config/risk tests, Ruff, and mypy for affected modules.
- [ ] Commit the independently safe configuration checkpoint.

### Task 2: Restart-safe and verified directional activity

**Files:**
- Modify: `afuture/directional_activity.py`, `afuture/operations.py`, `afuture/execution_aligned_runtime.py`
- Test: `tests/test_directional_activity.py`, `tests/test_execution_aligned_runtime.py`, `tests/test_operations.py`

**Interfaces:**
- Consumes: validated `Tick` and `ContractInfo` observations.
- Produces: versioned atomic activity envelope containing completed and in-progress snapshots, restored across restart.

- [ ] Add failing tests for mid-day restart, corrupt/tampered payloads, non-finite activity, identity mismatch, and long scheduled closures.
- [ ] Confirm each failure is caused by current loss of in-progress state or permissive load/freshness behavior.
- [ ] Implement a shared codec/validator with schema version and checksum; persist every materially changed latest observation atomically and restore it.
- [ ] Make required trading-day coverage authoritative; keep natural-hour age only when no required completed day is available.
- [ ] Reuse the runtime validator in `doctor` and run the directional activity/runtime/operations subsystem tests.
- [ ] Commit the directional evidence checkpoint.

### Task 3: Bounded broker delivery and authoritative identities

**Files:**
- Modify: `afuture/broker/ctp.py`, `afuture/engine.py`, `afuture/position.py`, `afuture/state.py`, `afuture/reconcile.py`, related models if required
- Test: `tests/test_ctp_compat.py`, `tests/test_position.py`, `tests/test_state.py`, `tests/test_integration.py`

**Interfaces:**
- Consumes: VeighNa tick/order/trade/account callbacks and broker trading-day response.
- Produces: critical FIFO events plus coalesced latest ticks, authoritative trading day, and `(symbol, exchange)` contract identity.

- [ ] Add failing tests for critical-event priority under a tick flood, bounded poll batches, missing CTP trading day, cross-exchange trade IDs, and cross-exchange position identity.
- [ ] Verify failures reproduce queue starvation, calendar fallback, or identity collision rather than test setup errors.
- [ ] Replace the mixed unbounded queue with a critical FIFO and coalesced tick buffer; expose counters without adding infrastructure.
- [ ] Reject missing/invalid live trading day and remove live natural-date fallback.
- [ ] Use `(trading_day, exchange, trade_id)` for fill idempotency and `(symbol, exchange)` for position maps/state validation, preserving legacy stored trade IDs during migration.
- [ ] Run CTP, engine, position, state, reconciliation, and integration tests.
- [ ] Commit the broker correctness checkpoint.

### Task 4: Reproducible directional signal input and final governance

**Files:**
- Modify: `afuture/execution_aligned_runtime.py`, `afuture/runtime_factory.py`, `afuture/operations.py`
- Modify: `docs/configuration.md`, `docs/data-and-backtest.md`, `docs/live-trading.md`, `docs/architecture.md`, `docs/production-checklist.md`, `docs/troubleshooting.md`
- Test: `tests/test_execution_aligned_runtime.py`, `tests/test_cli_runtime_factory.py`, documentation checks

**Interfaces:**
- Consumes: verified provider OHLC history.
- Produces: atomic local last-known-good cache plus manifest/digest and explicit bounded fallback behavior.

- [ ] Add failing tests for restart fallback, tampered cache, overlapping upstream revision, and expired cache.
- [ ] Implement a compact atomic cache beside runtime state; verify schema, product set, index, finite values, content digest, and revision consistency.
- [ ] Use fresh provider data when valid; otherwise permit only an already verified cache that covers the required completed trading day.
- [ ] Update operational documentation and final refactoring report with exact behavior and target-machine CTP limitations.
- [ ] Run affected subsystem tests, CLI smoke tests, lint, format, mypy, compile, Markdown consistency checks, and one final full repository suite.
- [ ] Review the final diff, request independent code review, fix Critical/Important findings, commit, and push the verified commit to `main`.
