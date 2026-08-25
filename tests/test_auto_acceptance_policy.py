from __future__ import annotations

from types import SimpleNamespace

from afuture.auto import AutoConfig
from afuture.auto_acceptance import AutoPortfolioAcceptanceGate
from afuture.auto_research import AutoPortfolioResearchConfig, AutoPortfolioRunner
from afuture.config import AppConfig
from afuture.risk import RiskConfig


def config() -> AppConfig:
    return AppConfig(
        mode="replay",
        initial_capital=500000,
        contracts={},
        pairs=[],
        risk=RiskConfig(),
        ctp=None,
        auto=AutoConfig(enabled=True, products=("m",), exchanges=("DCE",)),
        contract_catalog=[],
    )


def test_accept_auto_default_uses_small_global_parameter_neighborhood():
    runner = AutoPortfolioRunner(config())
    grid = runner.parameter_grid(AutoPortfolioResearchConfig())
    assert 3 <= len(grid) <= 20
    assert all("entry_z" in row and "lookback" in row for row in grid)
    assert len({tuple(sorted(row.items())) for row in grid}) == len(grid)


def test_gate_rejects_negative_aggregate_oos_even_when_majority_folds_positive():
    folds = [
        SimpleNamespace(
            oos_metrics={"total_return": 0.01, "max_drawdown": -0.01, "trade_count": 4}
        ),
        SimpleNamespace(
            oos_metrics={"total_return": 0.01, "max_drawdown": -0.01, "trade_count": 4}
        ),
        SimpleNamespace(
            oos_metrics={"total_return": -0.05, "max_drawdown": -0.05, "trade_count": 4}
        ),
    ]
    result = SimpleNamespace(
        folds=folds,
        stress_results={1.0: {"total_return": 0.01}, 2.0: {"total_return": 0.0}},
        robustness={"leave_one_product_out": {}, "single_product": {}},
    )
    decision = AutoPortfolioAcceptanceGate(
        min_positive_oos_ratio=0.60,
        max_oos_drawdown=0.06,
        min_oos_trade_legs=4,
    ).evaluate(result)
    assert not decision.accepted
    assert "aggregate OOS return is not positive" in decision.reasons
