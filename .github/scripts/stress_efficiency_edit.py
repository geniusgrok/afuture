# message: fix: bound margin headroom and restore base meta rotation
from pathlib import Path


def once(path: str, old: str, new: str, label: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one match, got {count}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


# Meta cost gate is retained as a tested primitive, but the first L3 showed that using it
# as a production selector destroys too much Base alpha. Keep the frozen Stress-survival
# top-3 rotation authoritative; subsequent turnover gates operate at execution level.
once(
    "afuture/execution_aligned_policy.py",
    '''                elif candidate != selected:\n                    incumbent_survives = all(np.isfinite(row[item]) for item in selected)\n                    history = base_frame.iloc[position - self.meta_lookback : position]\n                    incumbent_mean = float(history.iloc[:, selected].mean(axis=1).mean()) if selected else 0.0\n                    candidate_mean = float(history.iloc[:, candidate].mean(axis=1).mean()) if candidate else 0.0\n                    if should_switch_meta(\n                        incumbent_mean_return=incumbent_mean,\n                        candidate_mean_return=candidate_mean,\n                        incumbent_weights=aggregate(selected, timestamp),\n                        candidate_weights=aggregate(candidate, timestamp),\n                        horizon=self.meta_rebalance,\n                        cost_bps=STRESS_COST_BPS,\n                        incumbent_survives=incumbent_survives,\n                    ):\n                        selected = candidate\n''',
    '''                elif candidate != selected:\n                    selected = candidate\n''',
    "restore frozen meta rotation",
)

# The first L3 proved that expanding the 30% soft margin envelope can reintroduce a 35%
# hard margin HALT under the 15% Stress proxy. Adaptive sizing therefore becomes a
# causal tightening mechanism only: calm conditions keep 30%, completed shocks above
# the existing 3% volatility trigger reserve additional space. The hard 35% gate is
# unchanged.
once(
    "afuture/directional.py",
    '''    if not values:\n        return min(hard_share, conservative)\n    sample = values[-2:]\n    sample_vol = stdev(sample) if len(sample) >= 2 else 0.0\n    shock = max(volatility_trigger, abs(values[-1]), sample_vol)\n    shock = min(max(shock, volatility_trigger), daily_loss_ratio)\n    adaptive = hard_share * (1.0 - daily_loss_ratio) / (1.0 + shock)\n    return min(hard_share, max(conservative, adaptive))\n''',
    '''    if not values:\n        return min(hard_share, conservative)\n    sample = values[-2:]\n    sample_vol = stdev(sample) if len(sample) >= 2 else 0.0\n    shock = max(volatility_trigger, abs(values[-1]), sample_vol)\n    shock = min(max(shock, volatility_trigger), daily_loss_ratio)\n    excess_shock = max(0.0, shock - volatility_trigger)\n    adaptive = conservative * (1.0 - excess_shock)\n    return min(hard_share, max(0.0, min(conservative, adaptive)))\n''',
    "bound adaptive margin envelope",
)

# Update the causal-margin unit contract to reflect the safety result from L3: no history
# and calm completed evidence remain at 30%; stronger completed shocks only tighten it.
once(
    "tests/test_directional_execution_efficiency.py",
    '''    assert adaptive_margin_sizing_share(completed_returns=(), **common) == pytest.approx(0.30)\n    calm = adaptive_margin_sizing_share(completed_returns=(0.002, 0.003), **common)\n    stressed = adaptive_margin_sizing_share(completed_returns=(-0.04, 0.01), **common)\n    assert 0.30 < calm < 0.35\n    assert 0.30 <= stressed < calm\n    assert calm <= 0.35\n    assert stressed <= 0.35\n''',
    '''    assert adaptive_margin_sizing_share(completed_returns=(), **common) == pytest.approx(0.30)\n    calm = adaptive_margin_sizing_share(completed_returns=(0.002, 0.003), **common)\n    stressed = adaptive_margin_sizing_share(completed_returns=(-0.04, 0.01), **common)\n    assert calm == pytest.approx(0.30)\n    assert 0.0 < stressed < calm\n    assert calm <= 0.35\n    assert stressed <= 0.35\n''',
    "update adaptive margin test",
)
