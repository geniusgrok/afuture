from pathlib import Path

import pandas as pd
import pytest

from afuture.directional_acceptance import ProductionMechanicsConfig
from afuture.directional_mpv_robustness import MPVDirectionalProductionAcceptance
from afuture.directional_robustness import MarginAwareDirectionalProductionAcceptance


def _config():
    return ProductionMechanicsConfig(
        initial_capital=500000.0,
        margin_rate_proxy=0.15,
        margin_estimate_buffer=1.25,
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
        max_contract_volume=35,
        max_daily_loss_ratio=0.05,
        max_realized_gross_ratio=2.0,
    )


def _target_kwargs():
    return dict(
        equity=500000.0,
        product_weights={"AG": 1.0, "CU": 1.0},
        product_open_prices={"AG": 100000.0 / 15.0, "CU": 100000.0 / 5.0},
        selected_symbols={"AG": "AG2612", "CU": "CU2610"},
        current_lots={"AG2612": 4, "CU2610": 4},
        completed_returns=(),
    )


def _observed_intraday_events():
    rows = []
    for offset in range(10):
        day = pd.Timestamp("2026-08-01") + pd.Timedelta(days=offset)
        rows.extend(
            [
                {
                    "date": day,
                    "kind": "pnl",
                    "action": "intraday",
                    "product": "AG",
                    "lots_before": 1,
                    "gross_pnl": 300.0,
                    "turnover_notional": 0.0,
                    "transaction_cost": 0.0,
                },
                {
                    "date": day,
                    "kind": "pnl",
                    "action": "intraday",
                    "product": "CU",
                    "lots_before": 1,
                    "gross_pnl": 50.0,
                    "turnover_notional": 0.0,
                    "transaction_cost": 0.0,
                },
            ]
        )
    return rows


def test_mpv_acceptance_without_causal_evidence_matches_validated_stages_exactly():
    baseline = MarginAwareDirectionalProductionAcceptance(_config())
    candidate = MPVDirectionalProductionAcceptance(_config())

    assert candidate.target_lot_stages(**_target_kwargs()) == baseline.target_lot_stages(
        **_target_kwargs()
    )


def test_mpv_acceptance_reallocates_same_soft_margin_capacity_to_higher_value_product():
    candidate = MPVDirectionalProductionAcceptance(_config())
    candidate._mpv_observed_event_rows = _observed_intraday_events()
    candidate._mpv_cost_rate = 0.0

    stages = candidate.target_lot_stages(**_target_kwargs())

    assert stages.raw_integer_lots == {"AG2612": 5, "CU2610": 5}
    assert stages.margin_fitted_lots == {"AG2612": 4, "CU2610": 4}
    assert stages.final_lots == {"AG2612": 5, "CU2610": 3}
    assert stages.soft_margin_share == pytest.approx(0.30)
    assert stages.final_notional == pytest.approx(800000.0)
    assert candidate.last_mpv_optimization is not None
    assert candidate.last_mpv_optimization.product_estimates[
        "AG"
    ].expected_gross_alpha_per_lot_segment > candidate.last_mpv_optimization.product_estimates[
        "CU"
    ].expected_gross_alpha_per_lot_segment


def test_live_runtime_modules_do_not_import_offline_mpv_labels_or_research_adapter():
    for path in (
        Path("afuture/directional_runtime.py"),
        Path("afuture/execution_aligned_runtime.py"),
        Path("afuture/runtime_factory.py"),
    ):
        source = path.read_text(encoding="utf-8")
        assert "directional_mpv_attribution" not in source
        assert "directional_mpv_robustness" not in source
