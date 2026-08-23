# message: revert: reject product replacement persistence
from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one match, got {count}")
    file.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "afuture/execution_aligned_policy.py",
    "from .directional_efficiency import (\n    should_switch_meta,\n    stabilize_product_replacements,\n    stabilize_same_direction_weights,\n)\n",
    "from .directional_efficiency import should_switch_meta, stabilize_same_direction_weights\n",
)
replace_once(
    "afuture/execution_aligned_policy.py",
    '''            raw = aggregate(selected, timestamp)\n            if raw and previous_weights and position > 0:\n                trailing = intraday.iloc[\n                    max(0, position - self.meta_lookback) : position\n                ].mean(axis=0).to_dict()\n                raw = stabilize_product_replacements(\n                    previous_weights,\n                    raw,\n                    trailing_mean_returns=trailing,\n                    horizon=self.meta_rebalance,\n                    cost_bps=STRESS_COST_BPS,\n                )\n            if raw:\n                final.loc[timestamp] = pd.Series(raw).reindex(final.columns).fillna(0.0)\n            previous_weights = {str(product): float(value) for product, value in final.loc[timestamp].items() if abs(float(value)) > 1e-15}\n''',
    '''            raw = aggregate(selected, timestamp)\n            if raw:\n                final.loc[timestamp] = pd.Series(raw).reindex(final.columns).fillna(0.0)\n            previous_weights = {str(product): float(value) for product, value in final.loc[timestamp].items() if abs(float(value)) > 1e-15}\n''',
)

path = Path("afuture/directional_efficiency.py")
text = path.read_text(encoding="utf-8")
start = "\ndef stabilize_product_replacements(\n"
end = "\ndef stabilize_same_direction_weights(\n"
if text.count(start) != 1 or text.count(end) != 1:
    raise SystemExit("directional_efficiency product persistence markers invalid")
left = text.index(start)
right = text.index(end, left)
path.write_text(text[:left] + text[right:], encoding="utf-8")

replace_once(
    "tests/test_directional_execution_efficiency.py",
    "    assert candidate_turnover < legacy_turnover - 1e-12\n",
    "    assert candidate_turnover <= legacy_turnover + 1e-12\n",
)

product_test = Path("tests/test_directional_product_persistence.py")
if not product_test.exists():
    raise SystemExit("expected product persistence test file")
product_test.unlink()
