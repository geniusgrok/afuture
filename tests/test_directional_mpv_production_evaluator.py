import sys
from pathlib import Path

import pandas as pd
import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import evaluate_directional_mpv_production as evaluator


def test_load_frozen_weights_reconstructs_sparse_long_form_without_changing_gross(tmp_path):
    path = tmp_path / "weights.csv"
    pd.DataFrame(
        [
            {"level_0": "2026-08-19", "level_1": "AG", "weight": 1.0},
            {"level_0": "2026-08-19", "level_1": "CU", "weight": -0.5},
            {"level_0": "2026-08-20", "level_1": "AG", "weight": 0.75},
        ]
    ).to_csv(path, index=False)

    weights = evaluator.load_frozen_weights(path)

    assert list(weights.index) == [pd.Timestamp("2026-08-19"), pd.Timestamp("2026-08-20")]
    assert weights.loc[pd.Timestamp("2026-08-19"), "AG"] == 1.0
    assert weights.loc[pd.Timestamp("2026-08-19"), "CU"] == -0.5
    assert weights.loc[pd.Timestamp("2026-08-20"), "CU"] == 0.0


def test_load_frozen_weights_rejects_any_row_of_portfolio_weights_above_two_x(tmp_path):
    path = tmp_path / "weights.csv"
    pd.DataFrame(
        [
            {"level_0": "2026-08-20", "level_1": "AG", "weight": 1.25},
            {"level_0": "2026-08-20", "level_1": "CU", "weight": 1.0},
        ]
    ).to_csv(path, index=False)

    with pytest.raises(ValueError, match="2x gross"):
        evaluator.load_frozen_weights(path)


def test_economic_summary_uses_realized_gross_less_transaction_cost_per_turnover():
    attribution = {
        "alpha": {"gross_signal_pnl": 700245.0},
        "transaction_cost": {
            "turnover_notional": 256918290.0,
            "total_cost": 385377.435,
        },
    }

    summary = evaluator._economic_summary(attribution)

    assert summary["net_alpha_pnl"] == pytest.approx(314867.565)
    assert summary["net_alpha_per_turnover_bps"] == pytest.approx(12.2556, rel=1e-5)
