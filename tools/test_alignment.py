import importlib.util
from pathlib import Path

import pandas as pd

spec = importlib.util.spec_from_file_location(
    "evidence", Path(__file__).with_name("evaluate_daily_relative_strategy.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

# Production parity: same cumulative-volume gate and no hard-coded product identity.
assert module.PROFILE["min_bar_volume"] == 1000.0
assert not hasattr(module, "EXPECTED_PRIOR_PRODUCTS")
assert not hasattr(module, "EXPECTED_CURRENT_PRODUCTS")

passing = {
    "qualified_prior": ["MA"],
    "qualified_current": ["M"],
    "prior_forward": {"trades": 3, "R": 0.5, "mdd_R": -0.2},
    "final_oos": {"trades": 3, "R": 0.4, "mdd_R": -0.2},
    "full_recent": {"trades": 10, "R": 2.1, "mdd_R": -0.3},
    "neighbor_pass_ratio": 0.5,
}
assert module.promotion_reasons(passing) == []
failing = dict(passing)
failing["prior_forward"] = {"trades": 4, "R": -0.1, "mdd_R": -0.3}
failing["final_oos"] = {"trades": 2, "R": 0.2, "mdd_R": -0.1}
failing["neighbor_pass_ratio"] = 0.0
reasons = module.promotion_reasons(failing)
assert any("prior forward" in reason for reason in reasons)
assert any("final OOS trade sample" in reason for reason in reasons)
assert any("neighbor stability" in reason for reason in reasons)

cols = ["datetime", "open", "high", "low", "close", "volume", "hold", "symbol", "product"]
current = pd.DataFrame(
    [
        ["2025-01-02 15:00:00", 100, 100, 100, 100, 100, 8000, "OI2505", "OI"],
        ["2025-01-02 15:00:00", 90, 90, 90, 90, 200, 80000, "OI2509", "OI"],
        ["2025-01-02 21:00:00", 101, 101, 101, 101, 10, 8100, "OI2505", "OI"],
        ["2025-01-02 21:00:00", 91, 91, 91, 91, 20, 80100, "OI2509", "OI"],
        ["2025-01-02 23:00:00", 92, 92, 92, 92, 30, 80200, "OI2509", "OI"],
        ["2025-01-03 09:00:00", 102, 102, 102, 102, 300, 8200, "OI2505", "OI"],
        ["2025-01-03 09:00:00", 92, 92, 92, 92, 400, 80300, "OI2509", "OI"],
        ["2025-01-03 21:00:00", 103, 103, 103, 103, 11, 8300, "OI2505", "OI"],
        ["2025-01-03 21:00:00", 93, 93, 93, 93, 21, 80400, "OI2509", "OI"],
        ["2025-01-03 23:00:00", 104, 104, 104, 104, 12, 8400, "OI2505", "OI"],
        ["2025-01-03 23:00:00", 94, 94, 94, 94, 22, 80500, "OI2509", "OI"],
        ["2025-01-06 09:00:00", 105, 105, 105, 105, 500, 8500, "OI2505", "OI"],
        ["2025-01-06 09:00:00", 95, 95, 95, 95, 600, 80600, "OI2509", "OI"],
    ],
    columns=cols,
)
prior = current.iloc[0:0].copy()
frames = module.build_pair_frames(prior, current)
pair = frames[("OI", "OI2505", "OI2509")]
assert len(pair) == 1
assert str(pair.iloc[0].datetime.date()) == "2025-01-06"
assert str(pair.iloc[0].sample_timestamp) == "2025-01-03 23:00:00"
assert float(pair.iloc[0].near_vol) == 23.0
assert float(pair.iloc[0].far_vol) == 43.0

front_rows = []
for symbol, price in (("M2501", 2800), ("M2505", 2810), ("M2509", 2820), ("M2601", 2830)):
    front_rows.append(["2025-01-02 23:00:00", price, price, price, price, 10, 20000, symbol, "M"])
    front_rows.append(["2025-01-03 09:00:00", price, price, price, price, 100, 21000, symbol, "M"])
front = pd.DataFrame(front_rows, columns=cols)
front_frames = module.build_pair_frames(front.iloc[0:0].copy(), front)
assert ("M", "M2501", "M2505") not in front_frames
assert ("M", "M2505", "M2509") in front_frames
assert ("M", "M2509", "M2601") in front_frames
assert len([key for key in front_frames if key[0] == "M"]) == 2
