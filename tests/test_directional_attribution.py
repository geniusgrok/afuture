import pandas as pd
import pytest

from afuture.directional_acceptance import ProductionMechanicsConfig
from afuture.directional_attribution import classify_rebalance_action
from afuture.directional_robustness import MarginAwareDirectionalProductionAcceptance


def _audit_event(*, date, product, gross_pnl, kind="pnl", transaction_cost=0.0):
    return {
        "date": date,
        "kind": kind,
        "action": "intraday" if kind == "pnl" else "entry",
        "product": product,
        "symbol": f"{product}2609",
        "side": "long",
        "lots_before": 1,
        "lots_after": 1,
        "delta_lots": 0,
        "price": 100.0,
        "turnover_notional": 0.0,
        "transaction_cost": transaction_cost,
        "gross_pnl": gross_pnl,
    }


def _robustness_fixture():
    dates = pd.date_range("2025-11-03", periods=70, freq="B")
    returns = pd.Series(0.0, index=dates)
    returns.iloc[0] = 0.10
    returns.iloc[1] = -0.05
    returns.iloc[3] = 0.06
    january = returns.index[returns.index.month == 1]
    returns.loc[january[0]] = -0.10
    returns.loc[january[1]] = 0.12
    equity = 1_000_000.0 * (1.0 + returns).cumprod()
    daily = pd.DataFrame({"equity": equity, "daily_return": returns})
    events = pd.DataFrame(
        [
            _audit_event(date=dates[0], product="B", gross_pnl=40.0),
            _audit_event(date=dates[0], product="A", gross_pnl=60.0),
            _audit_event(date=dates[1], product="A", gross_pnl=40.0),
            _audit_event(date=dates[1], product="B", gross_pnl=60.0),
            _audit_event(date=dates[2], product="C", gross_pnl=50.0),
            _audit_event(date=dates[3], product="D", gross_pnl=-10.0),
            _audit_event(
                date=dates[3],
                product="A",
                gross_pnl=0.0,
                kind="trade",
                transaction_cost=999.0,
            ),
        ]
    )
    return daily, events


def test_production_attribution_adds_flat_pathwise_robustness_diagnostics():
    from afuture.directional_attribution import summarize_production_attribution

    daily, events = _robustness_fixture()
    expected_daily = daily.copy(deep=True)
    expected_events = events.copy(deep=True)

    # Deliberately scramble the daily rows: date parsing/sorting must make this a no-op.
    summary = summarize_production_attribution(
        daily=daily.iloc[::-1], events=events, initial_capital=1_000_000.0
    )
    diagnostics = summary["robustness_diagnostics"]
    sorted_returns = expected_daily["daily_return"].sort_index()
    rolling = (1.0 + sorted_returns).rolling(63).apply(lambda values: values.prod(), raw=True) - 1.0
    best_rolling_end = rolling.idxmin()

    assert diagnostics["worst_calendar_quarter"] == "2026Q1"
    assert diagnostics["worst_calendar_quarter_return"] == pytest.approx(0.008)
    assert diagnostics["worst_rolling_63_session_return"] == pytest.approx(rolling.min())
    assert (
        diagnostics["worst_rolling_63_start"]
        == (best_rolling_end - pd.offsets.BDay(62)).date().isoformat()
    )
    assert diagnostics["worst_rolling_63_end"] == best_rolling_end.date().isoformat()
    assert "worst_rolling_63_session_start" not in diagnostics
    assert "worst_rolling_63_session_end" not in diagnostics
    assert diagnostics["max_drawdown_duration_sessions"] == 2
    assert diagnostics["drawdown_start"] == expected_daily.index[1].date().isoformat()
    assert diagnostics["recovery_date"] == expected_daily.index[3].date().isoformat()
    assert diagnostics["positive_product_pnl_total"] == pytest.approx(250.0)
    assert diagnostics["best_product"] == "A"
    assert diagnostics["best_product_gross_pnl"] == pytest.approx(100.0)
    assert diagnostics["best_product_positive_pnl_share"] == pytest.approx(0.4)
    assert diagnostics["top3_positive_pnl_share"] == pytest.approx(1.0)
    assert diagnostics["positive_product_pnl_hhi"] == pytest.approx(0.36)
    assert summary["alpha"]["gross_signal_pnl"] == pytest.approx(240.0)
    assert diagnostics["gross_pnl_without_best_product_proxy"] == pytest.approx(140.0)
    assert diagnostics["best_calendar_month"] == "2025-11"
    assert diagnostics["best_calendar_month_return"] == pytest.approx(0.1077)
    assert diagnostics["compounded_return_excluding_best_month_proxy"] == pytest.approx(0.008)
    assert "pathwise additive" in diagnostics["proxy_methodology"]
    assert "not a re-simulation or full counterfactual" in diagnostics["proxy_methodology"]
    for omitted_state in (
        "equity",
        "integer lots",
        "margin",
        "reallocation",
        "risk state",
        "future orders",
    ):
        assert omitted_state in diagnostics["proxy_methodology"]
    pd.testing.assert_frame_equal(daily, expected_daily)
    pd.testing.assert_frame_equal(events, expected_events)


def test_robustness_diagnostics_handle_short_empty_and_unrecovered_paths():
    from afuture.directional_attribution import summarize_production_attribution

    empty = summarize_production_attribution(
        daily=pd.DataFrame(), events=pd.DataFrame(), initial_capital=1_000_000.0
    )["robustness_diagnostics"]
    assert empty["worst_calendar_quarter"] is None
    assert empty["worst_rolling_63_session_return"] is None
    assert empty["worst_rolling_63_start"] is None
    assert empty["worst_rolling_63_end"] is None
    assert empty["max_drawdown_duration_sessions"] == 0
    assert empty["positive_product_pnl_total"] == 0.0
    assert empty["best_product"] is None
    assert empty["gross_pnl_without_best_product_proxy"] == 0.0
    assert empty["best_calendar_month"] is None
    assert empty["compounded_return_excluding_best_month_proxy"] == 0.0

    dates = pd.date_range("2026-01-05", periods=4, freq="B")
    daily = pd.DataFrame(
        {"equity": [100.0, 90.0, 90.0, 90.0], "daily_return": [0.0, -0.1, 0.0, 0.0]},
        index=dates,
    )
    diagnostics = summarize_production_attribution(
        daily=daily, events=pd.DataFrame(), initial_capital=100.0
    )["robustness_diagnostics"]
    assert diagnostics["worst_rolling_63_session_return"] is None
    assert diagnostics["max_drawdown_duration_sessions"] == 3
    assert diagnostics["drawdown_start"] == "2026-01-06"
    assert diagnostics["recovery_date"] is None


def test_robustness_diagnostics_select_only_positive_best_product_and_preserve_gross_proxy():
    from afuture.directional_attribution import summarize_production_attribution

    daily = pd.DataFrame(
        {"equity": [100.0], "daily_return": [0.0]}, index=[pd.Timestamp("2026-01-05")]
    )
    nonpositive_events = pd.DataFrame(
        [
            _audit_event(date="2026-01-05", product="A", gross_pnl=-5.0),
            _audit_event(date="2026-01-05", product="B", gross_pnl=0.0),
        ]
    )
    nonpositive = summarize_production_attribution(
        daily=daily, events=nonpositive_events, initial_capital=100.0
    )["robustness_diagnostics"]
    assert nonpositive["positive_product_pnl_total"] == 0.0
    assert nonpositive["best_product"] is None
    assert nonpositive["best_product_gross_pnl"] == 0.0
    assert nonpositive["best_product_positive_pnl_share"] == 0.0
    assert nonpositive["top3_positive_pnl_share"] == 0.0
    assert nonpositive["positive_product_pnl_hhi"] == 0.0
    assert nonpositive["gross_pnl_without_best_product_proxy"] == pytest.approx(-5.0)

    tied_events = pd.DataFrame(
        [
            _audit_event(date="2026-01-05", product="B", gross_pnl=10.0),
            _audit_event(date="2026-01-05", product="A", gross_pnl=10.0),
            _audit_event(date="2026-01-05", product="C", gross_pnl=5.0),
            _audit_event(date="2026-01-05", product="D", gross_pnl=1.0),
        ]
    )
    tied = summarize_production_attribution(daily=daily, events=tied_events, initial_capital=100.0)[
        "robustness_diagnostics"
    ]
    assert tied["best_product"] == "A"
    assert tied["best_product_gross_pnl"] == 10.0
    assert tied["top3_positive_pnl_share"] == pytest.approx(25.0 / 26.0)
    assert tied["positive_product_pnl_hhi"] == pytest.approx((100 + 100 + 25 + 1) / 26**2)

    empty_daily = summarize_production_attribution(
        daily=pd.DataFrame(), events=nonpositive_events, initial_capital=100.0
    )["robustness_diagnostics"]
    assert empty_daily["gross_pnl_without_best_product_proxy"] == pytest.approx(-5.0)

    empty_daily_positive = summarize_production_attribution(
        daily=pd.DataFrame(), events=tied_events, initial_capital=100.0
    )["robustness_diagnostics"]
    assert empty_daily_positive["positive_product_pnl_total"] == pytest.approx(26.0)
    assert empty_daily_positive["best_product"] == "A"
    assert empty_daily_positive["best_product_gross_pnl"] == pytest.approx(10.0)
    assert empty_daily_positive["gross_pnl_without_best_product_proxy"] == pytest.approx(16.0)

    with pytest.raises(ValueError, match="gross_pnl"):
        summarize_production_attribution(
            daily=pd.DataFrame(),
            events=pd.DataFrame(
                [_audit_event(date="2026-01-05", product="A", gross_pnl=float("nan"))]
            ),
            initial_capital=100.0,
        )
    with pytest.raises(ValueError, match="gross_pnl"):
        summarize_production_attribution(
            daily=daily,
            events=pd.DataFrame(
                [
                    _audit_event(date="2026-01-05", product="A", gross_pnl=1e308),
                    _audit_event(date="2026-01-05", product="A", gross_pnl=1e308),
                ]
            ),
            initial_capital=100.0,
        )


def test_robustness_diagnostics_choose_earliest_equal_duration_drawdown():
    from afuture.directional_attribution import summarize_production_attribution

    dates = pd.date_range("2026-01-05", periods=7, freq="B")
    daily = pd.DataFrame(
        {
            "equity": [100.0, 90.0, 90.0, 100.0, 90.0, 90.0, 100.0],
            "daily_return": [0.0, -0.1, 0.0, 1.0 / 9.0, -0.1, 0.0, 1.0 / 9.0],
        },
        index=dates,
    )

    diagnostics = summarize_production_attribution(
        daily=daily, events=pd.DataFrame(), initial_capital=100.0
    )["robustness_diagnostics"]

    assert diagnostics["max_drawdown_duration_sessions"] == 2
    assert diagnostics["drawdown_start"] == dates[1].date().isoformat()
    assert diagnostics["recovery_date"] == dates[3].date().isoformat()


def test_robustness_diagnostics_resolve_time_ties_earliest_and_single_product_hhi():
    from afuture.directional_attribution import summarize_production_attribution

    dates = pd.date_range("2025-10-01", periods=126, freq="B")
    daily = pd.DataFrame(
        {"equity": [100.0] * len(dates), "daily_return": [0.0] * len(dates)}, index=dates
    )
    events = pd.DataFrame([_audit_event(date=dates[0], product="AG", gross_pnl=7.0)])
    diagnostics = summarize_production_attribution(
        daily=daily, events=events, initial_capital=100.0
    )["robustness_diagnostics"]

    assert diagnostics["worst_calendar_quarter"] == "2025Q4"
    assert diagnostics["worst_calendar_quarter_return"] == 0.0
    assert diagnostics["worst_rolling_63_session_return"] == 0.0
    assert diagnostics["worst_rolling_63_start"] == dates[0].date().isoformat()
    assert diagnostics["worst_rolling_63_end"] == dates[62].date().isoformat()
    assert diagnostics["best_calendar_month"] == "2025-10"
    assert diagnostics["best_calendar_month_return"] == 0.0
    assert diagnostics["compounded_return_excluding_best_month_proxy"] == 0.0
    assert diagnostics["best_product"] == "AG"
    assert diagnostics["best_product_positive_pnl_share"] == 1.0
    assert diagnostics["top3_positive_pnl_share"] == 1.0
    assert diagnostics["positive_product_pnl_hhi"] == 1.0


@pytest.mark.parametrize(
    ("daily", "events", "message"),
    [
        (
            pd.DataFrame(
                {"equity": [100.0, 100.0], "daily_return": [0.0, 0.0]},
                index=["2026-01-05", "2026-01-05"],
            ),
            pd.DataFrame(),
            "duplicate",
        ),
        (
            pd.DataFrame({"equity": [100.0], "daily_return": [0.0]}, index=["not-a-date"]),
            pd.DataFrame(),
            "date",
        ),
        (
            pd.DataFrame({"equity": [float("inf")], "daily_return": [0.0]}, index=["2026-01-05"]),
            pd.DataFrame(),
            "equity",
        ),
        (
            pd.DataFrame({"equity": [0.0], "daily_return": [0.0]}, index=["2026-01-05"]),
            pd.DataFrame(),
            "equity",
        ),
        (
            pd.DataFrame({"equity": [100.0], "daily_return": [float("nan")]}, index=["2026-01-05"]),
            pd.DataFrame(),
            "daily_return",
        ),
        (
            pd.DataFrame({"equity": [100.0], "daily_return": [0.0]}, index=["2026-01-05"]),
            pd.DataFrame([_audit_event(date="2026-01-05", product="A", gross_pnl=float("nan"))]),
            "gross_pnl",
        ),
    ],
)
def test_robustness_diagnostics_fail_closed_only_for_contracted_data_quality(
    daily, events, message
):
    from afuture.directional_attribution import summarize_production_attribution

    with pytest.raises(ValueError, match=message):
        summarize_production_attribution(daily=daily, events=events, initial_capital=100.0)


def test_rebalance_action_splits_entry_and_exit_without_losing_existing_classes():
    original = {
        "A2609": 5,
        "M2609": -4,
        "CU2609": 2,
        "AL2609": 3,
    }
    target = {
        "A2701": 5,
        "M2609": -3,
        "CU2609": -2,
        "RB2610": 1,
    }
    products = {
        "A2609": "A",
        "A2701": "A",
        "M2609": "M",
        "CU2609": "CU",
        "AL2609": "AL",
        "RB2610": "RB",
    }

    assert (
        classify_rebalance_action(
            symbol="A2609",
            original_lots=original,
            target_lots=target,
            symbol_products=products,
        )
        == "roll"
    )
    assert (
        classify_rebalance_action(
            symbol="A2701",
            original_lots=original,
            target_lots=target,
            symbol_products=products,
        )
        == "roll"
    )
    assert (
        classify_rebalance_action(
            symbol="M2609",
            original_lots=original,
            target_lots=target,
            symbol_products=products,
        )
        == "resize"
    )
    assert (
        classify_rebalance_action(
            symbol="CU2609",
            original_lots=original,
            target_lots=target,
            symbol_products=products,
        )
        == "reversal"
    )
    assert (
        classify_rebalance_action(
            symbol="RB2610",
            original_lots=original,
            target_lots=target,
            symbol_products=products,
        )
        == "entry"
    )
    assert (
        classify_rebalance_action(
            symbol="AL2609",
            original_lots=original,
            target_lots=target,
            symbol_products=products,
        )
        == "exit"
    )


def test_margin_aware_target_stages_expose_capacity_losses_without_changing_final_lots():
    sim = MarginAwareDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=100000.0,
            margin_rate_proxy=0.15,
            margin_estimate_buffer=1.25,
            max_contract_volume=200,
        )
    )
    kwargs = dict(
        equity=100000.0,
        product_weights={"A": 2.0},
        product_open_prices={"A": 100.0},
        selected_symbols={"A": "A2609"},
        current_lots={},
        completed_returns=(),
    )

    stages = sim.target_lot_stages(**kwargs)

    assert stages.raw_integer_lots == {"A2609": 200}
    assert stages.margin_fitted_lots == {"A2609": 160}
    assert stages.final_lots == {"A2609": 160}
    assert stages.desired_notional == pytest.approx(200000.0)
    assert stages.raw_integer_notional == pytest.approx(200000.0)
    assert stages.margin_fitted_notional == pytest.approx(160000.0)
    assert stages.final_notional == pytest.approx(160000.0)
    assert stages.integer_rounding_loss_notional == pytest.approx(0.0)
    assert stages.max_volume_clipping_notional == pytest.approx(0.0)
    assert stages.unavailable_contract_notional == pytest.approx(0.0)
    assert stages.soft_margin_share == pytest.approx(0.30)
    assert sim.target_lots(**kwargs) == stages.final_lots


def test_target_stages_split_rounding_volume_cap_and_unavailable_capacity():
    sim = MarginAwareDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=100000.0,
            margin_rate_proxy=0.01,
            margin_estimate_buffer=1.0,
            max_contract_volume=35,
            max_margin_ratio=0.9,
            min_available_ratio=0.0,
            max_daily_loss_ratio=0.05,
        )
    )
    stages = sim.target_lot_stages(
        equity=100000.0,
        product_weights={"A": 0.333, "M": 1.0, "RB": 0.25},
        product_open_prices={"A": 101.0, "M": 100.0},
        selected_symbols={"A": "A2609", "M": "M2609"},
        current_lots={},
        completed_returns=(),
    )

    # A: desired 33,300; 32 full lots at 1,010 -> 980 integer remainder.
    # M: desired 100,000; 100 full lots at 1,000, clipped to 35 -> 65,000 cap loss.
    # RB: no selected/priceable contract -> full 25,000 unavailable capacity.
    assert stages.integer_rounding_loss_notional == pytest.approx(980.0)
    assert stages.max_volume_clipping_notional == pytest.approx(65000.0)
    assert stages.unavailable_contract_notional == pytest.approx(25000.0)
    assert stages.raw_integer_lots == {"A2609": 32, "M2609": 35}


def test_simulation_audit_reconciles_gross_pnl_and_transaction_cost_to_equity():
    raw = pd.DataFrame(
        [
            {
                "date": "2026-08-20",
                "product": "A",
                "exchange": "DCE",
                "symbol": "A2609",
                "delivery": "2026-12-15",
                "open": 99,
                "close": 99,
                "volume": 5000,
                "hold": 30000,
            },
            {
                "date": "2026-08-21",
                "product": "A",
                "exchange": "DCE",
                "symbol": "A2609",
                "delivery": "2026-12-15",
                "open": 100,
                "close": 110,
                "volume": 5000,
                "hold": 30000,
            },
            {
                "date": "2026-08-24",
                "product": "A",
                "exchange": "DCE",
                "symbol": "A2609",
                "delivery": "2026-12-15",
                "open": 111,
                "close": 112,
                "volume": 5000,
                "hold": 30000,
            },
        ]
    )
    weights = pd.DataFrame(
        {"A": [1.0, 0.0]},
        index=pd.to_datetime(["2026-08-21", "2026-08-24"]),
    )
    sim = MarginAwareDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=100000,
            margin_rate_proxy=0.01,
            margin_estimate_buffer=1.0,
            max_contract_volume=100,
            max_daily_loss_ratio=0.5,
            max_total_drawdown_ratio=0.8,
            max_margin_ratio=0.9,
            min_available_ratio=0,
        )
    )

    result = sim.simulate(raw, weights, cost_bps=5)
    events = result.events

    assert {"entry", "exit"} <= set(events.loc[events["kind"] == "trade", "action"])
    assert events.loc[events["kind"] == "pnl", "gross_pnl"].sum() == pytest.approx(11000.0)
    assert events.loc[events["kind"] == "trade", "transaction_cost"].sum() == pytest.approx(105.5)
    assert result.final_equity == pytest.approx(
        sim.config.initial_capital + events["gross_pnl"].sum() - events["transaction_cost"].sum()
    )
    assert result.final_equity == pytest.approx(110894.5)

    first = result.daily.iloc[0]
    assert first["raw_target_gross_ratio"] == pytest.approx(1.0)
    assert first["governor_target_gross_ratio"] == pytest.approx(1.0)
    assert first["raw_integer_target_gross_notional"] == pytest.approx(100000.0)
    assert first["margin_fitted_target_gross_notional"] == pytest.approx(100000.0)
    assert first["final_target_gross_notional"] == pytest.approx(100000.0)


def test_production_attribution_summary_reports_pnl_cost_capacity_and_holding_quality():
    from afuture.directional_attribution import summarize_production_attribution

    raw = pd.DataFrame(
        [
            {
                "date": "2026-08-20",
                "product": "A",
                "exchange": "DCE",
                "symbol": "A2609",
                "delivery": "2026-12-15",
                "open": 99,
                "close": 99,
                "volume": 5000,
                "hold": 30000,
            },
            {
                "date": "2026-08-21",
                "product": "A",
                "exchange": "DCE",
                "symbol": "A2609",
                "delivery": "2026-12-15",
                "open": 100,
                "close": 110,
                "volume": 5000,
                "hold": 30000,
            },
            {
                "date": "2026-08-24",
                "product": "A",
                "exchange": "DCE",
                "symbol": "A2609",
                "delivery": "2026-12-15",
                "open": 111,
                "close": 112,
                "volume": 5000,
                "hold": 30000,
            },
        ]
    )
    weights = pd.DataFrame(
        {"A": [1.0, 0.0]},
        index=pd.to_datetime(["2026-08-21", "2026-08-24"]),
    )
    sim = MarginAwareDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=100000,
            margin_rate_proxy=0.01,
            margin_estimate_buffer=1.0,
            max_contract_volume=100,
            max_daily_loss_ratio=0.5,
            max_total_drawdown_ratio=0.8,
            max_margin_ratio=0.9,
            min_available_ratio=0,
        )
    )
    result = sim.simulate(raw, weights, cost_bps=5)

    summary = summarize_production_attribution(
        daily=result.daily,
        events=result.events,
        initial_capital=sim.config.initial_capital,
    )

    assert summary["alpha"]["gross_signal_pnl"] == pytest.approx(11000.0)
    assert summary["alpha"]["long_pnl"] == pytest.approx(11000.0)
    assert summary["alpha"]["short_pnl"] == pytest.approx(0.0)
    assert summary["alpha"]["product_pnl"] == {"A": pytest.approx(11000.0)}
    assert summary["transaction_cost"]["total_cost"] == pytest.approx(105.5)
    assert summary["transaction_cost"]["by_action"]["entry"]["turnover_notional"] == pytest.approx(
        100000.0
    )
    assert summary["transaction_cost"]["by_action"]["entry"]["cost"] == pytest.approx(50.0)
    assert summary["transaction_cost"]["by_action"]["entry"]["affected_days"] == 1
    assert summary["transaction_cost"]["by_action"]["exit"]["turnover_notional"] == pytest.approx(
        111000.0
    )
    assert summary["transaction_cost"]["by_action"]["exit"]["cost"] == pytest.approx(55.5)
    assert summary["transaction_cost"]["annualized_return_drag_proxy"] > 0.0
    assert summary["activity"]["execution_event_count"] == 2
    assert summary["activity"]["average_holding_sessions"] == pytest.approx(2.0)
    assert summary["capacity"]["peak_raw_target_gross_ratio"] == pytest.approx(1.0)
    assert summary["capacity"]["peak_governor_target_gross_ratio"] == pytest.approx(1.0)
    assert summary["capacity"]["integer_rounding_loss_notional"] == pytest.approx(0.0)
    assert summary["capacity"]["margin_capacity_loss_notional"] == pytest.approx(0.0)


def test_audit_records_end_of_day_daily_circuit_cost_exactly_once():
    raw = pd.DataFrame(
        [
            {
                "date": "2026-08-20",
                "product": "A",
                "exchange": "DCE",
                "symbol": "A2609",
                "delivery": "2026-12-15",
                "open": 100,
                "close": 100,
                "volume": 5000,
                "hold": 30000,
            },
            {
                "date": "2026-08-21",
                "product": "A",
                "exchange": "DCE",
                "symbol": "A2609",
                "delivery": "2026-12-15",
                "open": 100,
                "close": 70,
                "volume": 5000,
                "hold": 30000,
            },
        ]
    )
    weights = pd.DataFrame({"A": [1.0]}, index=pd.to_datetime(["2026-08-21"]))
    sim = MarginAwareDirectionalProductionAcceptance(
        ProductionMechanicsConfig(
            initial_capital=100000,
            margin_rate_proxy=0.01,
            margin_estimate_buffer=1.0,
            max_contract_volume=100,
            max_daily_loss_ratio=0.05,
            max_total_drawdown_ratio=0.8,
            max_margin_ratio=0.9,
            min_available_ratio=0,
        )
    )

    result = sim.simulate(raw, weights, cost_bps=5)
    trades = result.events[result.events["kind"] == "trade"]

    assert set(trades["action"]) == {"entry", "daily_circuit"}
    assert trades["turnover_notional"].sum() == pytest.approx(
        result.daily["turnover_notional"].sum()
    )
    assert trades["transaction_cost"].sum() == pytest.approx(
        result.daily["turnover_notional"].sum() * 5 / 10000.0
    )
