from pathlib import Path
import importlib.util
import sys

import pandas as pd

TOOLS = Path(__file__).resolve().parent
PATH = TOOLS / "evaluate_directional_production_mechanics.py"
assert PATH.exists(), "production mechanics evaluator is missing"
spec = importlib.util.spec_from_file_location("directional_production_mechanics", PATH)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

raw = pd.DataFrame([
    {"date":"2026-08-20","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":99,"close":99,"volume":5000,"hold":30000},
    {"date":"2026-08-21","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":101,"volume":5000,"hold":30000},
])
weights = pd.DataFrame({"A":[0.5]}, index=pd.to_datetime(["2026-08-21"]))
report = module.evaluate_with_weights(raw, weights)
assert report["selection_frozen"] is True
assert report["parameter_search"] is False
assert report["mechanics"]["integer_lots"] is True
assert report["mechanics"]["previous_completed_activity"] is True
assert report["mechanics"]["target_gross_leverage_cap"] == 2.0
assert report["mechanics"]["max_contract_volume"] == 35
assert report["mechanics"]["daily_loss_same_day_circuit"] is True
assert report["mechanics"]["daily_loss_next_trading_day_safe_recovery"] is True
assert report["mechanics"]["hard_account_risk_permanent_halt"] is True
assert report["mechanics"]["hard_account_risk_precedes_daily_circuit"] is True
assert report["mechanics"]["causal_completed_return_risk_governor"] == {
    "lookback_days": 2,
    "sample_volatility_trigger": 0.03,
    "completed_daily_loss_trigger": 0.03,
    "defensive_scale": 0.25,
}
assert report["base"]["max_contract_volume"] == 35
assert report["base"]["margin_rate_proxy"] == 0.12
assert report["stress"]["margin_rate_proxy"] == 0.15
assert report["base"]["cost_bps"] == 5.0
assert report["stress"]["cost_bps"] == 15.0
assert report["margin_is_historical_truth"] is False
assert report["base"]["final_equity"] > 0

# Each published window is an independent account experiment. Circuit behavior itself
# is covered by the acceptance unit tests; this smoke test only proves that an earlier
# standalone window cannot contaminate the account state of a later window.
module.WINDOWS = {
    "early": ("2026-08-21", "2026-08-21"),
    "late": ("2026-08-24", "2026-08-24"),
}
window_raw = pd.DataFrame([
    {"date":"2026-08-20","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":100,"volume":5000,"hold":30000},
    {"date":"2026-08-21","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":70,"volume":5000,"hold":30000},
    {"date":"2026-08-24","product":"A","exchange":"DCE","symbol":"A2609","delivery":"2026-12-15","open":100,"close":110,"volume":5000,"hold":30000},
])
window_weights = pd.DataFrame(
    {"A":[1.0,1.0]},
    index=pd.to_datetime(["2026-08-21","2026-08-24"]),
)
window_report = module.evaluate_with_weights(window_raw, window_weights)
assert window_report["base"]["state_reset_per_window"] is True
assert window_report["base"]["windows"]["late"]["active_days"] == 1
assert window_report["base"]["windows"]["late"]["halted"] is False
print("directional production mechanics tool tests passed")
