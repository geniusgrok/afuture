# message: revert: keep only economically promoted efficiency rules
from pathlib import Path

# Restore the frozen policy output path after the same-direction candidate failed L3.
policy = Path("afuture/execution_aligned_policy.py")
text = policy.read_text(encoding="utf-8")
old = '''            raw = aggregate(selected, timestamp)\n            if raw and previous_weights and position > 0:\n                trailing = intraday.iloc[\n                    max(0, position - self.meta_lookback) : position\n                ].mean(axis=0).to_dict()\n                raw = stabilize_same_direction_weights(\n                    previous_weights,\n                    raw,\n                    trailing_mean_returns=trailing,\n                    horizon=self.meta_rebalance,\n                    cost_bps=STRESS_COST_BPS,\n                )\n            if raw:\n                final.loc[timestamp] = pd.Series(raw).reindex(final.columns).fillna(0.0)\n            previous_weights = {str(product): float(value) for product, value in final.loc[timestamp].items() if abs(float(value)) > 1e-15}\n'''
new = '''            raw = aggregate(selected, timestamp)\n            if raw:\n                final.loc[timestamp] = pd.Series(raw).reindex(final.columns).fillna(0.0)\n            previous_weights = {str(product): float(value) for product, value in final.loc[timestamp].items() if abs(float(value)) > 1e-15}\n'''
if text.count(old) != 1:
    raise SystemExit(f"expected one same-direction policy block, got {text.count(old)}")
text = text.replace(old, new, 1)
old_import = "from .directional_efficiency import should_switch_meta, stabilize_same_direction_weights\n"
if text.count(old_import) != 1:
    raise SystemExit(f"expected one rejected-helper import, got {text.count(old_import)}")
text = text.replace(old_import, "", 1)
policy.write_text(text, encoding="utf-8")

# Remove experiment-only helpers after product/meta/same-direction candidates failed L3.
eff = Path("afuture/directional_efficiency.py")
text = eff.read_text(encoding="utf-8")
imports = "from typing import Mapping\n\nimport numpy as np\nimport pandas as pd\n"
if text.count(imports) != 1:
    raise SystemExit("unexpected directional_efficiency import block")
text = text.replace(imports, "from typing import Mapping\n", 1)
start = "\ndef weight_turnover("
end = "\ndef stabilize_one_lot_increases("
if text.count(start) != 1 or text.count(end) != 1:
    raise SystemExit("rejected helper section markers invalid")
left = text.index(start)
right = text.index(end, left)
text = text[:left] + text[right:]
audit = "\ndef audit_policy_weight_history("
if text.count(audit) != 1:
    raise SystemExit("audit helper marker invalid")
text = text[:text.index(audit)].rstrip() + "\n"
eff.write_text(text, encoding="utf-8")
