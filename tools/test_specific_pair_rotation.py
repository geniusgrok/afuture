import importlib.util
import sys
from pathlib import Path

import pandas as pd

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

fetch_spec = importlib.util.spec_from_file_location(
    "specific_fetch", TOOLS / "fetch_specific_pair_daily_universe.py"
)
specific_fetch = importlib.util.module_from_spec(fetch_spec)
sys.modules[fetch_spec.name] = specific_fetch
fetch_spec.loader.exec_module(specific_fetch)

validation_spec = importlib.util.spec_from_file_location(
    "specific_validation", TOOLS / "evaluate_specific_pair_rotation.py"
)
validation = importlib.util.module_from_spec(validation_spec)
sys.modules[validation_spec.name] = validation
validation_spec.loader.exec_module(validation)

symbols = set(specific_fetch.contract_symbols())
assert ("P", "DCE", "P2205") in symbols
assert ("J", "DCE", "J2609") in symbols
assert ("AL", "SHFE", "AL2205") in symbols
assert ("CU", "SHFE", "CU2612") in symbols
assert specific_fetch.delivery_date("AL2609") == pd.Timestamp("2026-09-15")

# A tiny synthetic roll proves t->t+1 PnL uses the contract selected at t,
# rather than joining two different contract prices on the roll date.
dates = pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06"])
rows = []
for product, first_prices, second_prices in (
    ("P", (100.0, 110.0, 108.0), (200.0, 220.0, 242.0)),
    ("Y", (50.0, 55.0, 54.0), (100.0, 110.0, 121.0)),
):
    for day, first, second, first_oi, second_oi in zip(
        dates,
        first_prices,
        second_prices,
        (100.0, 80.0, 70.0),
        (50.0, 120.0, 140.0),
        strict=True,
    ):
        rows.append(
            {
                "date": day,
                "product": product,
                "symbol": f"{product}2509",
                "delivery": "2025-09-15",
                "close": first,
                "volume": 1000.0,
                "hold": first_oi,
            }
        )
        rows.append(
            {
                "date": day,
                "product": product,
                "symbol": f"{product}2601",
                "delivery": "2026-01-15",
                "close": second,
                "volume": 900.0,
                "hold": second_oi,
            }
        )

original_pairs = validation.PAIRS
original_min_days = validation.MIN_PRODUCT_DAYS
try:
    validation.PAIRS = (validation.base.EconomicPair("P", "Y", "DCE", "test"),)
    validation.MIN_PRODUCT_DAYS = 3
    close, returns, selections, quality = validation.build_roll_safe_panel(pd.DataFrame(rows))
finally:
    validation.PAIRS = original_pairs
    validation.MIN_PRODUCT_DAYS = original_min_days

assert selections.loc[selections["product"] == "P", "symbol"].tolist() == [
    "P2509",
    "P2601",
    "P2601",
]
assert abs(returns.loc[dates[1], "P"] - 0.10) < 1e-12
assert abs(returns.loc[dates[2], "P"] - 0.10) < 1e-12
assert abs(close.loc[dates[2], "P"] - 121.0) < 1e-9
assert quality["P"]["rolls"] == 1
assert int(selections["days_to_delivery"].min()) >= validation.MIN_DAYS_TO_DELIVERY
