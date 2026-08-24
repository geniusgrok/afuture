from pathlib import Path


def test_baseline_margin_aware_exposes_same_30pct_soft_share():
    from afuture.directional_acceptance import ProductionMechanicsConfig
    from afuture.directional_robustness import MarginAwareDirectionalProductionAcceptance

    simulator = MarginAwareDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            max_margin_ratio=0.35,
            min_available_ratio=0.25,
            max_daily_loss_ratio=0.05,
        )
    )

    assert abs(simulator.margin_sizing_share(()) - 0.30) < 1e-12


def test_hard_feasible_candidate_uses_exact_35pct_not_a_searched_share():
    from afuture.directional_acceptance import ProductionMechanicsConfig
    from afuture.directional_hard_feasible_margin import (
        HardFeasibleMarginFreezeDirectionalProductionAcceptance,
    )

    config = ProductionMechanicsConfig(
        margin_rate_proxy=0.15,
        margin_estimate_buffer=1.25,
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
        max_daily_loss_ratio=0.05,
        max_total_drawdown_ratio=0.30,
        max_realized_gross_ratio=2.0,
        max_contract_volume=35,
    )
    simulator = HardFeasibleMarginFreezeDirectionalProductionAcceptance(config)

    assert abs(simulator.margin_sizing_share(()) - 0.35) < 1e-12
    assert abs(simulator.margin_sizing_share((-0.01, 0.01)) - 0.35) < 1e-12
    assert simulator.config.max_margin_ratio == 0.35
    assert simulator.config.min_available_ratio == 0.25
    assert simulator.config.max_daily_loss_ratio == 0.05
    assert simulator.config.max_total_drawdown_ratio == 0.30
    assert simulator.config.max_realized_gross_ratio == 2.0
    assert simulator.config.max_contract_volume == 35


def test_hard_feasible_candidate_only_uses_extra_capacity_when_not_frozen():
    from afuture.directional_acceptance import ProductionMechanicsConfig
    from afuture.directional_hard_feasible_margin import (
        HardFeasibleMarginFreezeDirectionalProductionAcceptance,
    )
    from afuture.directional_robustness import MarginAwareDirectionalProductionAcceptance

    config = ProductionMechanicsConfig(
        initial_capital=10000.0,
        margin_rate_proxy=0.15,
        margin_estimate_buffer=1.25,
        max_margin_ratio=0.35,
        min_available_ratio=0.25,
        max_daily_loss_ratio=0.05,
        max_total_drawdown_ratio=0.30,
        max_realized_gross_ratio=2.0,
        max_contract_volume=35,
    )
    kwargs = dict(
        equity=10000.0,
        product_weights={"A": 2.0},
        product_open_prices={"A": 100.0},
        selected_symbols={"A": "A2501"},
        current_lots={},
        completed_returns=(),
    )

    baseline = MarginAwareDirectionalProductionAcceptance(config).target_lot_stages(**kwargs)
    candidate = HardFeasibleMarginFreezeDirectionalProductionAcceptance(config).target_lot_stages(**kwargs)

    assert baseline.final_lots == {"A2501": 16}
    assert candidate.final_lots == {"A2501": 18}
    assert candidate.final_lots["A2501"] > baseline.final_lots["A2501"]

    frozen = HardFeasibleMarginFreezeDirectionalProductionAcceptance(config).target_lot_stages(
        equity=10000.0,
        product_weights={"A": 2.0},
        product_open_prices={"A": 100.0},
        selected_symbols={"A": "A2501"},
        current_lots={"A2501": 10},
        completed_returns=(-0.03,),
    )
    assert frozen.final_lots == {"A2501": 10}


def test_hard_feasible_margin_research_is_not_live_wired():
    for path in (
        Path("afuture/runtime_factory.py"),
        Path("afuture/execution_aligned_runtime.py"),
        Path("afuture/directional_runtime.py"),
    ):
        if path.exists():
            assert "directional_hard_feasible_margin" not in path.read_text(encoding="utf-8")
