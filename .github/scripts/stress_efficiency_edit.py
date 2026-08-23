# message: fix: keep product hysteresis at execution lot layer only
from pathlib import Path

path = Path("afuture/execution_aligned_policy.py")
text = path.read_text(encoding="utf-8")
old = '''            raw = aggregate(selected, timestamp)\n            if position > 0 and raw:\n                trailing = intraday.iloc[max(0, position - self.meta_lookback) : position].mean(axis=0).to_dict()\n                raw = stabilize_same_direction_weights(\n                    previous_weights,\n                    raw,\n                    trailing_mean_returns=trailing,\n                    horizon=self.meta_rebalance,\n                    cost_bps=STRESS_COST_BPS,\n                )\n            if raw:\n                final.loc[timestamp] = pd.Series(raw).reindex(final.columns).fillna(0.0)\n            previous_weights = {str(product): float(value) for product, value in final.loc[timestamp].items() if abs(float(value)) > 1e-15}\n'''
new = '''            raw = aggregate(selected, timestamp)\n            if raw:\n                final.loc[timestamp] = pd.Series(raw).reindex(final.columns).fillna(0.0)\n            previous_weights = {str(product): float(value) for product, value in final.loc[timestamp].items() if abs(float(value)) > 1e-15}\n'''
if text.count(old) != 1:
    raise SystemExit(f"expected one product-hysteresis block, got {text.count(old)}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")

# The primitive remains unit-tested because execution-layer no-trade logic may use the same
# economic rule, but the full signal weights must remain frozen after L3 proved this layer
# destroys Base alpha.
