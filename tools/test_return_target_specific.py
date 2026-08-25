import importlib.util
import sys
from pathlib import Path

import pandas as pd

TOOLS = Path(__file__).resolve().parent
FETCH_PATH = TOOLS / "fetch_return_target_specific_daily.py"
BASE_PATH = TOOLS / "evaluate_return_target_specific.py"
FINAL_PATH = TOOLS / "evaluate_execution_aligned_target.py"
assert FETCH_PATH.exists() and BASE_PATH.exists() and FINAL_PATH.exists()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fetcher = _load("return_target_fetch", FETCH_PATH)
base = _load("return_target_specific", BASE_PATH)
evaluator = _load("execution_aligned_target", FINAL_PATH)

assert set(fetcher.PRODUCTS) == set(evaluator.REQUIRED_PRODUCTS)
assert len(fetcher.PRODUCTS) == 50
assert evaluator.MAX_GROSS_LEVERAGE == 2.0
assert evaluator.EXECUTION_SELECTION["pool_size"] == 96
assert evaluator.EXECUTION_SELECTION["meta_lookback"] == 10
assert evaluator.EXECUTION_SELECTION["rebalance"] == 5
assert evaluator.EXECUTION_SELECTION["count"] == 3
assert len(evaluator.EXECUTION_SELECTION["pool_ids"]) == 96
assert evaluator.EXECUTION_SELECTION["meta_score_source"] == "continuous_intraday_proxy"
assert evaluator.EXECUTION_SELECTION["selection_bias"] == (
    "full_recent_specific_execution_template_rank_fit"
)

symbols = set(fetcher.contract_symbols())
assert ("A", "DCE", "A2209") in symbols
assert ("MA", "CZCE", "MA2209") in symbols
assert ("AG", "SHFE", "AG2209") in symbols
assert ("BC", "INE", "BC2209") in symbols
assert fetcher.delivery_date("AG2609") == pd.Timestamp("2026-09-15")

dates = pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06"])
rows = []
for product in ("A", "M"):
    first = (100.0, 110.0, 108.0) if product == "A" else (50.0, 55.0, 54.0)
    second = (1000.0, 1100.0, 1210.0) if product == "A" else (500.0, 550.0, 605.0)
    for day, p1, p2, oi1, oi2 in zip(
        dates,
        first,
        second,
        (100.0, 80.0, 70.0),
        (50.0, 120.0, 140.0),
        strict=True,
    ):
        rows.extend(
            [
                {
                    "date": day,
                    "product": product,
                    "exchange": "DCE",
                    "symbol": f"{product}2509",
                    "delivery": "2025-09-15",
                    "open": p1 * 0.99,
                    "close": p1,
                    "volume": 1000.0,
                    "hold": oi1,
                },
                {
                    "date": day,
                    "product": product,
                    "exchange": "DCE",
                    "symbol": f"{product}2601",
                    "delivery": "2026-01-15",
                    "open": p2 * 0.99,
                    "close": p2,
                    "volume": 900.0,
                    "hold": oi2,
                },
            ]
        )

original_products = base.REQUIRED_PRODUCTS
original_min_days = base.MIN_PRODUCT_DAYS
try:
    base.REQUIRED_PRODUCTS = ("A", "M")
    base.MIN_PRODUCT_DAYS = 3
    close_returns, gap_returns, intraday_returns, selected, quality = (
        base.build_roll_safe_execution_returns(pd.DataFrame(rows))
    )
finally:
    base.REQUIRED_PRODUCTS = original_products
    base.MIN_PRODUCT_DAYS = original_min_days

assert selected.loc[selected["product"] == "A", "symbol"].tolist() == ["A2509", "A2601", "A2601"]
assert abs(close_returns.loc[dates[1], "A"] - 0.10) < 1e-12
assert quality["A"]["rolls"] == 1
assert int(selected["days_to_delivery"].min()) >= base.MIN_DAYS_TO_DELIVERY

idx = pd.date_range("2025-02-03", periods=3, freq="B")
weights = pd.DataFrame({"A": [0.0, 1.0, -1.0]}, index=idx)
gap = pd.DataFrame({"A": [0.0, 0.10, 0.20]}, index=idx)
intraday = pd.DataFrame({"A": [0.0, 0.03, 0.04]}, index=idx)
path = base.apply_next_open_product_weights(gap, intraday, weights, cost_bps=0.0)
assert abs(path.iloc[1] - 0.03) < 1e-12
assert abs(path.iloc[2] - (0.20 - 0.04)) < 1e-12
costed = base.apply_next_open_product_weights(gap, intraday, weights, cost_bps=10.0)
assert abs((path.iloc[1] - costed.iloc[1]) - 0.001) < 1e-12
assert abs((path.iloc[2] - costed.iloc[2]) - 0.002) < 1e-12

# The execution proxy freezes product ordering alphabetically. Stable sort ties in
# breakout/range signals must not change strategy behavior when callers reorder products.
proxy_rows = []
for product, scale in (("M", 2.0), ("A", 1.0)):
    proxy_rows.extend(
        [
            {"date": idx[0], "product": product, "open": 100.0 * scale, "close": 100.0 * scale},
            {"date": idx[1], "product": product, "open": 110.0 * scale, "close": 113.3 * scale},
            {
                "date": idx[2],
                "product": product,
                "open": 134.827 * scale,
                "close": 140.22008 * scale,
            },
        ]
    )
proxy_gap, proxy_intraday = evaluator.build_continuous_execution_proxy(
    pd.DataFrame(proxy_rows), products=("M", "A")
)
assert list(proxy_gap.columns) == ["A", "M"]
assert list(proxy_intraday.columns) == ["A", "M"]
assert abs(proxy_gap.loc[idx[1], "A"] - 0.10) < 1e-12
assert abs(proxy_intraday.loc[idx[1], "A"] - 0.03) < 1e-12
assert abs(proxy_gap.loc[idx[2], "A"] - 0.19) < 1e-12
assert abs(proxy_intraday.loc[idx[2], "A"] - 0.04) < 1e-12
assert evaluator.PRISTINE_FINAL_OOS is False

print("return-target specific-ranked deterministic-order tests passed")
