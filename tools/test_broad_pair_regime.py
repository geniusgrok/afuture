import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

spec = importlib.util.spec_from_file_location(
    "pair_research",
    Path(__file__).with_name("evaluate_broad_pair_regime.py"),
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

assert module.PROFILES
assert all(pair.exchange in {"DCE", "CZCE", "SHFE"} for pair in module.PAIRS)
assert len({(pair.left, pair.right) for pair in module.PAIRS}) == len(module.PAIRS)

# Build a causal synthetic pair: right leg trends, left leg is right plus a
# stationary oscillating residual. The strategy must stay idle before formation.
dates = pd.date_range("2024-01-01", periods=220, freq="B")
right = 100.0 * np.exp(np.linspace(0.0, 0.18, len(dates)))
residual = 0.025 * np.sin(np.arange(len(dates)) / 2.5)
left = right * np.exp(residual)
close = pd.DataFrame({"L": left, "R": right}, index=dates)
returns = close.pct_change(fill_method=None)
pair = module.EconomicPair("L", "R", "DCE", "test")
profile = module.PairProfile(
    formation=60,
    entry_z=1.5,
    min_correlation=0.4,
    min_volatility_ratio=0.0,
)
stats = module._pair_statistics(close, returns, pair, profile.formation)
series, entries = module._simulate_pair(close, returns, pair, profile, stats, cost_bps=0.0)
assert series.iloc[:60].abs().sum() == 0.0
assert all(timestamp >= dates[60] for timestamp in entries)
assert np.isfinite(series.to_numpy()).all()

# Risk calibration is fail-closed: if even the smallest leverage violates the
# 15% calibration drawdown, it must return zero rather than silently using 1x.
rough = pd.Series([0.01] * 20 + [-0.35] + [0.01] * 20)
assert module._choose_leverage(rough) == 0.0

moderate = pd.Series([0.006] * 30 + [-0.02] * 2 + [0.006] * 30)
leverage = module._choose_leverage(moderate)
assert leverage in module.LEVERAGE_GRID
assert leverage <= module.MAX_GROSS_LEVERAGE
assert module._metrics(moderate * leverage)["max_drawdown"] > module.MAX_CALIBRATION_DRAWDOWN
