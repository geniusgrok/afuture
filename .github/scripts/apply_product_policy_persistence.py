from pathlib import Path

path = Path("afuture/execution_aligned_policy.py")
text = path.read_text(encoding="utf-8")

old_import = "from .directional_efficiency import should_switch_meta, stabilize_same_direction_weights\n"
new_import = "from .directional_efficiency import (\n    should_switch_meta,\n    stabilize_product_replacements,\n    stabilize_same_direction_weights,\n)\n"
if text.count(old_import) != 1:
    raise SystemExit(f"expected exactly one efficiency import, got {text.count(old_import)}")
text = text.replace(old_import, new_import, 1)

old_block = '''            raw = aggregate(selected, timestamp)\n            if raw:\n                final.loc[timestamp] = pd.Series(raw).reindex(final.columns).fillna(0.0)\n            previous_weights = {str(product): float(value) for product, value in final.loc[timestamp].items() if abs(float(value)) > 1e-15}\n'''
new_block = '''            raw = aggregate(selected, timestamp)\n            if raw and previous_weights and position > 0:\n                trailing = intraday.iloc[\n                    max(0, position - self.meta_lookback) : position\n                ].mean(axis=0).to_dict()\n                raw = stabilize_product_replacements(\n                    previous_weights,\n                    raw,\n                    trailing_mean_returns=trailing,\n                    horizon=self.meta_rebalance,\n                    cost_bps=STRESS_COST_BPS,\n                )\n            if raw:\n                final.loc[timestamp] = pd.Series(raw).reindex(final.columns).fillna(0.0)\n            previous_weights = {str(product): float(value) for product, value in final.loc[timestamp].items() if abs(float(value)) > 1e-15}\n'''
if text.count(old_block) != 1:
    raise SystemExit(f"expected exactly one policy output block, got {text.count(old_block)}")
text = text.replace(old_block, new_block, 1)
path.write_text(text, encoding="utf-8")
