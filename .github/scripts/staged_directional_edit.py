# message: revert: reject cost-aware meta hysteresis
from pathlib import Path

policy = Path("afuture/execution_aligned_policy.py")
text = policy.read_text(encoding="utf-8")
old = '''                if not selected:\n                    selected = candidate\n                elif candidate != selected:\n                    incumbent_survives = all(np.isfinite(row[item]) for item in selected)\n                    history = base_frame.iloc[\n                        position - self.meta_lookback : position\n                    ]\n                    incumbent_mean = (\n                        float(history.iloc[:, selected].mean(axis=1).mean())\n                        if selected\n                        else 0.0\n                    )\n                    candidate_mean = (\n                        float(history.iloc[:, candidate].mean(axis=1).mean())\n                        if candidate\n                        else 0.0\n                    )\n                    if should_switch_meta(\n                        incumbent_mean_return=incumbent_mean,\n                        candidate_mean_return=candidate_mean,\n                        incumbent_weights=aggregate(selected, timestamp),\n                        candidate_weights=aggregate(candidate, timestamp),\n                        horizon=self.meta_rebalance,\n                        cost_bps=STRESS_COST_BPS,\n                        incumbent_survives=incumbent_survives,\n                    ):\n                        selected = candidate\n\n            raw = aggregate(selected, timestamp)\n'''
new = '''                if not selected:\n                    selected = candidate\n                elif candidate != selected:\n                    selected = candidate\n\n            raw = aggregate(selected, timestamp)\n'''
if text.count(old) != 1:
    raise SystemExit(f"expected one meta hysteresis block, got {text.count(old)}")
policy.write_text(text.replace(old, new, 1), encoding="utf-8")

test = Path("tests/test_directional_execution_efficiency.py")
text = test.read_text(encoding="utf-8")
old = "    assert candidate_turnover < legacy_turnover - 1e-12\n"
new = "    assert candidate_turnover <= legacy_turnover + 1e-12\n"
if text.count(old) != 1:
    raise SystemExit(f"expected one strict turnover assertion, got {text.count(old)}")
test.write_text(text.replace(old, new, 1), encoding="utf-8")
