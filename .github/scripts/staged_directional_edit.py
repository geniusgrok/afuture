# message: feat: suppress low-value same-direction increases
from pathlib import Path

path = Path("afuture/execution_aligned_policy.py")
text = path.read_text(encoding="utf-8")
old = '''            raw = aggregate(selected, timestamp)\n            if raw:\n                final.loc[timestamp] = pd.Series(raw).reindex(final.columns).fillna(0.0)\n            previous_weights = {str(product): float(value) for product, value in final.loc[timestamp].items() if abs(float(value)) > 1e-15}\n'''
new = '''            raw = aggregate(selected, timestamp)\n            if raw and previous_weights and position > 0:\n                trailing = intraday.iloc[\n                    max(0, position - self.meta_lookback) : position\n                ].mean(axis=0).to_dict()\n                raw = stabilize_same_direction_weights(\n                    previous_weights,\n                    raw,\n                    trailing_mean_returns=trailing,\n                    horizon=self.meta_rebalance,\n                    cost_bps=STRESS_COST_BPS,\n                )\n            if raw:\n                final.loc[timestamp] = pd.Series(raw).reindex(final.columns).fillna(0.0)\n            previous_weights = {str(product): float(value) for product, value in final.loc[timestamp].items() if abs(float(value)) > 1e-15}\n'''
count = text.count(old)
if count != 1:
    raise SystemExit(f"expected one policy output block, got {count}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
