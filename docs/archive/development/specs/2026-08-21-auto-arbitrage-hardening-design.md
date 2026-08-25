# Auto Arbitrage Hardening Design

## Goal

Close the highest-value correctness and tail-risk gaps between the current `afuture` implementation and a small personal-use automated calendar-spread trader. The system remains single-strategy, single-engine and CTP-based; this change does not add a second trading path, machine-learning selector, service platform or portfolio-management framework.

## Findings that must be fixed

1. **Protected auto pairs are falsely treated as ineligible.** `AutoPairManager.select()` skips protected open pairs when building `last_eligible_ids`, while `TradingEngine._refresh_auto_pairs()` interprets absence from that set as loss of eligibility. An open auto pair is therefore always marked to retire after it becomes flat, even when its market-quality gates still pass.
2. **Live auto history can fail to warm up.** `_history` stores raw CTP ticks in a deque sized roughly `lookback * 4`, but live `sample_seconds` is normally 60. A liquid contract can produce hundreds or thousands of raw ticks during that interval, so older samples are evicted before the scanner can accumulate the intended statistical window.
3. **Scanner and production strategy do not use the same executable entry condition.** Scanner preselection is based on mid-spread Z-score, while the strategy opens from direction-specific executable spreads. This can activate a pair that cannot actually cross the production entry threshold after bid/ask costs.
4. **Trading permits stale cross-leg combinations inside the generic quote-age limit.** `RiskManager.check_quotes()` checks each quote against a reference time but does not impose a tight maximum timestamp skew between the two legs. A new quote paired with an older quote can create a false spread.
5. **Pair orders are always submitted near leg first.** This ignores current executable depth. If the first, liquid leg fills and the second, thin leg fails, naked exposure is more likely. The thinner executable leg should be submitted first so the more liquid leg is available as the hedge.
6. **Normal exit and stop decisions use mid spread instead of the spread available for liquidation.** Bid/ask costs can therefore make a nominal reversion non-executable, or make adverse liquidation worse than the mid-spread stop suggests.

## Design

### Auto observation retention

`AutoPairManager.observe()` will bucket observations by `AutoConfig.sample_seconds`. For a symbol, only the latest tick in each sampling bucket is retained. A new bucket appends a new sample; a tick in the same bucket replaces the latest sample. When `sample_seconds == 0`, existing raw-tick behavior is retained for tests/research. The deque remains bounded, but now its capacity represents sampled observations rather than arbitrary market-data message rate.

### Protected-pair eligibility

Protected pairs remain protected in ranking, but they are still evaluated through the same activity, stationarity, half-life, executable-entry and net-edge gates as other candidates. `last_eligible_ids` therefore answers the intended question: which pairs still pass the selector's hard gates. Ranking continues to place protected positions first so selection never forces a rotation.

### Executable scanner alignment

`SpreadStatistics` will expose the historical mean and standard deviation. `SpreadScanner.scan_pair()` will derive long/short executable spreads from the latest synchronized quotes, compute direction-specific executable Z-scores, and return no candidate unless one of those crosses `PairConfig.entry_z`. The candidate Z-score and direction-dependent net-edge calculation will use the same executable side that the production strategy would trade.

The cheap statistical prefilter used by `AutoPairManager` will use the same executable-entry check before querying live contract metadata, preserving the existing CTP-rate optimization.

### Cross-leg quote skew gate

Add `RiskConfig.max_leg_skew_seconds` with a conservative default of `2.0`. `check_quotes()` rejects multi-leg quote sets whose newest and oldest timestamps differ by more than this limit. Configuration validation requires a positive value. Example live configuration documents the new setting.

### Liquidity-aware submission order

Before submission, `PairExecutor` will sort the two requests by executable top-of-book depth for that request side, ascending. The thinner leg is sent first; the deeper leg becomes the hedge. This applies to both opening and paired normal closing requests. Emergency imbalance repair remains independent and reduce-only.

### Executable liquidation exits

For an existing long spread, liquidation uses `near.bid - far.ask`; for an existing short spread it uses `near.ask - far.bid`. Normal reversion exit and directional stop checks use this executable liquidation spread against the entry anchor. Structural-regime detection continues to use the mid-spread history because it is a statistical-state test rather than an execution-price test.

## Non-goals

- No promise or hard-coded target for return, Sharpe ratio or drawdown.
- No automatic parameter fitting from the same live window used to trade.
- No new directional futures strategy, cross-product spread strategy, options strategy or high-frequency market maker.
- No database, web service, distributed scheduler or account-management platform.
- No automatic expansion to every CTP product or night session.

## Verification

Regression tests will prove each identified failure mode. CI must pass on Python 3.10 and 3.13, including the existing replay, auto-replay, scanner and walk-forward commands. The final branch will be merged to `main` only after fresh GitHub Actions evidence is green.
