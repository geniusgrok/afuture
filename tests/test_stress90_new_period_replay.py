import pandas as pd


def test_oi_minute_records_use_next_open_day_for_night_session():
    from tools.replay_directional_stress90_new_period import map_oi_bars_to_trading_day

    raw = pd.DataFrame(
        {
            "datetime": [
                "2026-08-21 22:00",
                "2026-08-24 15:00",
                "2026-08-25 20:00",
                "2026-08-25 22:00",
            ]
        }
    )

    mapped = map_oi_bars_to_trading_day(
        raw,
        trading_days=pd.to_datetime(["2026-08-21", "2026-08-24", "2026-08-25"]),
    )

    assert mapped.loc[0, "trading_day"] == pd.Timestamp("2026-08-24")
    assert mapped.loc[1, "trading_day"] == pd.Timestamp("2026-08-24")
    assert mapped.loc[2, "mapping_status"] == "outside_session"
    assert mapped.loc[3, "mapping_status"] == "after_last_calendar_day"


def test_oi_source_session_day_before_replay_start_remains_in_calendar():
    from tools.replay_directional_stress90_new_period import map_oi_bars_to_trading_day

    raw = pd.DataFrame(
        {
            "datetime": ["2026-08-19 22:00", "2026-08-20 10:00", "2026-08-20 22:00"],
        }
    )

    mapped = map_oi_bars_to_trading_day(
        raw,
        trading_days=pd.to_datetime(["2026-08-20", "2026-08-21", "2026-08-24"]),
    )

    assert mapped["trading_day"].tolist() == [
        pd.Timestamp("2026-08-20"),
        pd.Timestamp("2026-08-20"),
        pd.Timestamp("2026-08-21"),
    ]


def test_complete_observed_calendar_keeps_prestart_warmup_sessions(monkeypatch):
    import tools.replay_directional_stress90_new_period as replay

    monkeypatch.setattr(replay.broad_fetch, "PRODUCTS", ["A", "B"])
    rows = []
    for day in pd.to_datetime(["2026-08-19", "2026-08-20", "2026-08-21"]):
        for product in replay.broad_fetch.PRODUCTS:
            rows.append(
                {
                    "date": day,
                    "product": product,
                    "open": 1.0,
                    "high": 1.0,
                    "low": 1.0,
                    "close": 1.0,
                    "volume": 1000.0,
                    "hold": 5000.0,
                }
            )

    calendar = replay._complete_observed_calendar(
        pd.DataFrame(rows),
        start=pd.Timestamp("2026-08-19"),
        end_cap=pd.Timestamp("2026-08-21"),
    )

    assert calendar.tolist() == list(pd.to_datetime(["2026-08-19", "2026-08-20", "2026-08-21"]))


def test_contract_spec_extracts_multiplier_tick_and_last_trading_rule():
    from tools.replay_directional_stress90_new_period import _extract_contract_spec

    raw = pd.DataFrame(
        {
            "item": ["交易单位", "最小变动价位", "最后交易日", "交易代码"],
            "value": ["100吨/手", "0.5元/吨", "合约月份第10个交易日", "I"],
        }
    )

    spec = _extract_contract_spec("I2609", raw)

    assert spec["provider_multiplier"] == 100.0
    assert spec["price_tick"] == 0.5
    assert spec["last_trading_day_rule"] == "合约月份第10个交易日"
    assert spec["product"] == "I"


def test_base_weight_prefix_audit_scopes_parity_to_frozen_policy_window():
    from tools.replay_directional_stress90_new_period import _base_weight_prefix_audit

    dates = pd.to_datetime(["2024-08-20", "2024-08-21", "2024-08-22"])
    generated = pd.DataFrame({"A": [0.5, 0.25, 0.0], "B": [0.0, -0.25, 0.5]}, index=dates)
    archived = pd.DataFrame({"A": [0.0, 0.25, 0.0], "B": [0.0, -0.25, 0.5]}, index=dates)

    summary, policy_mismatches, pre_policy_mismatches = _base_weight_prefix_audit(
        generated,
        archived,
        policy_start=pd.Timestamp("2024-08-21"),
        cutoff=pd.Timestamp("2024-08-22"),
    )

    assert summary["passed"] is True
    assert summary["comparison_start"] == "2024-08-21"
    assert summary["cell_comparisons"] == 4
    assert policy_mismatches.empty
    assert summary["pre_policy_cell_comparisons"] == 2
    assert summary["pre_policy_mismatch_count"] == 1
    assert pre_policy_mismatches[["date", "product", "generated", "frozen"]].to_dict(
        orient="records"
    ) == [{"date": "2024-08-20", "product": "A", "generated": 0.5, "frozen": 0.0}]


def test_base_weight_prefix_audit_rejects_mismatch_inside_frozen_policy_window():
    from tools.replay_directional_stress90_new_period import _base_weight_prefix_audit

    dates = pd.to_datetime(["2024-08-20", "2024-08-21"])
    generated = pd.DataFrame({"A": [0.5, 0.25]}, index=dates)
    archived = pd.DataFrame({"A": [0.0, -0.25]}, index=dates)

    summary, policy_mismatches, _pre_policy_mismatches = _base_weight_prefix_audit(
        generated,
        archived,
        policy_start=pd.Timestamp("2024-08-21"),
        cutoff=pd.Timestamp("2024-08-21"),
    )

    assert summary["passed"] is False
    assert summary["mismatch_count"] == 1
    assert policy_mismatches[["date", "product", "generated", "frozen"]].to_dict(
        orient="records"
    ) == [{"date": "2024-08-21", "product": "A", "generated": 0.25, "frozen": -0.25}]


def test_fixed_oi_flow_tail_filters_the_product_column_and_time_boundaries(monkeypatch):
    import tools.replay_directional_stress90_new_period as replay

    monkeypatch.setattr(replay.oi_gate, "SUPPORTED_PRODUCTS", ("A",))
    bars = pd.DataFrame(
        {
            "datetime": pd.to_datetime(
                ["2026-08-19 22:00", "2026-08-18 22:00", "2026-08-19 22:00", "2026-08-21 22:00"]
            ),
            "product": ["A", "A", "Z", "A"],
            "symbol": ["A2610", "A2610", "Z2610", "A2610"],
        }
    )

    selected = replay._fixed_oi_flow_tail(bars)

    assert selected[["datetime", "product", "symbol"]].to_dict(orient="records") == [
        {"datetime": pd.Timestamp("2026-08-19 22:00"), "product": "A", "symbol": "A2610"}
    ]


def test_oi_session_coverage_keeps_rows_for_each_supported_product(monkeypatch):
    import tools.replay_directional_stress90_new_period as replay

    monkeypatch.setattr(replay.oi_gate, "SUPPORTED_PRODUCTS", ("A",))
    day = pd.Timestamp("2026-08-21")
    mapped = pd.DataFrame(
        {
            "trading_day": [day],
            "datetime": [pd.Timestamp("2026-08-20 22:00")],
            "product": ["A"],
            "symbol": ["A2610"],
        }
    )

    coverage = replay._session_coverage(mapped, pd.DatetimeIndex([day]))

    assert coverage.loc[0, "rows"] == 1
    assert coverage.loc[0, "symbols"] == "A2610"


def test_preperiod_terminal_state_is_first_decision_prior_state():
    from afuture.directional_stress90_policy import (
        STRESS90_POLICY,
        Stress90CandidateState,
        build_stress90_candidate_path,
        candidate_state_digest,
    )
    from tools.replay_directional_stress90_new_period import _preperiod_terminal_state

    target_day = pd.Timestamp("2026-08-21")
    products = list(STRESS90_POLICY.products)
    oi_products = list(STRESS90_POLICY.oi_products)
    initial_state = Stress90CandidateState.initial(STRESS90_POLICY)
    path = build_stress90_candidate_path(
        base_weights=pd.DataFrame(0.0, index=[target_day], columns=products),
        completed_close_prices=pd.DataFrame(
            100.0, index=[pd.Timestamp("2026-08-20")], columns=products
        ),
        confirming_flow=pd.DataFrame(float("nan"), index=[target_day], columns=oi_products),
        initial_state=initial_state,
    )

    assert path.decisions[0].prior_state == initial_state
    assert _preperiod_terminal_state(path) == initial_state
    assert (
        candidate_state_digest(_preperiod_terminal_state(path))
        == path.decisions[0].input_digests["prior_state"]
    )


def test_account_lots_use_current_selected_contract_multiplier_and_preserve_audit():
    from tools.replay_directional_stress90_new_period import (
        PRODUCT_MULTIPLIERS,
        _account_multipliers_from_specs,
    )

    selected = pd.DataFrame(
        {
            "product": ["TA", "TA", "SR"],
            "symbol": ["TA2701", "TA2612", "SR2701"],
            "provider_multiplier": [5.0, 5.0, 5.0],
        }
    )

    multipliers, audited_specs, product_audit = _account_multipliers_from_specs(selected)

    assert PRODUCT_MULTIPLIERS["TA"] == 10.0
    assert multipliers["TA"] == 5.0
    assert multipliers["SR"] == 5.0
    ta_audit = product_audit.set_index("product").loc["TA"]
    assert ta_audit["frozen_model_multiplier"] == 10.0
    assert ta_audit["account_multiplier_used"] == 5.0
    assert bool(ta_audit["differs_from_frozen_model"])
    assert audited_specs.account_multiplier_used.tolist() == [5.0, 5.0, 5.0]


def test_account_event_restart_comparison_uses_canonical_within_day_order():
    from tools.replay_directional_stress90_new_period import _canonical_account_events

    events = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-09-07"] * 3),
            "kind": ["pnl", "trade", "pnl"],
            "action": ["gap", "entry", "intraday"],
            "product": ["BU", "RU", "BU"],
            "symbol": ["BU2610", "RU2701", "BU2610"],
            "delta_lots": [0, 1, 0],
        }
    )

    joined = _canonical_account_events(events.iloc[[2, 0, 1]])
    continuous = _canonical_account_events(events)

    pd.testing.assert_frame_equal(joined, continuous)
    assert joined.action.tolist() == ["gap", "entry", "intraday"]


def test_contract_spec_query_summary_accepts_the_fetchers_dataframe_result():
    from tools.replay_directional_stress90_new_period import _contract_spec_query_summary

    summary = _contract_spec_query_summary(
        pd.DataFrame({"result": ["ok", "ok", "empty_response", "error"]})
    )

    assert summary == {"responses_ok": 2, "responses_empty": 1, "responses_error": 1}


def test_future_data_append_does_not_change_completed_candidate_decision():
    from afuture.directional_stress90_policy import (
        STRESS90_POLICY,
        Stress90CandidateState,
        build_stress90_candidate_path,
    )
    from tools.replay_directional_stress90_new_period import (
        _verify_future_append_invariance,
    )

    products = list(STRESS90_POLICY.products)
    oi_products = list(STRESS90_POLICY.oi_products)
    target_days = pd.to_datetime(["2026-08-21", "2026-08-24"])
    base = pd.DataFrame(0.0, index=target_days, columns=products)
    base.loc[target_days[1], "TA"] = 0.5
    close = pd.DataFrame(
        100.0,
        index=pd.to_datetime(["2026-08-20", "2026-08-21", "2026-08-24"]),
        columns=products,
    )
    close.loc[pd.Timestamp("2026-08-24")] = 10000.0
    flow = pd.DataFrame(float("nan"), index=target_days, columns=oi_products)
    flow.loc[target_days[1], "TA"] = 1.0
    initial = Stress90CandidateState.initial(STRESS90_POLICY)
    full_path = build_stress90_candidate_path(
        base_weights=base,
        completed_close_prices=close,
        confirming_flow=flow,
        initial_state=initial,
    )

    result = _verify_future_append_invariance(
        full_path,
        base_weights=base,
        completed_close_prices=close,
        confirming_flow=flow,
        initial_state=initial,
        control_position=0,
    )

    assert result["passed"] is True
    assert result["future_target_sessions_appended"] == 1
    assert result["decision_digest_match"] is True
    assert result["prior_state_digest_match"] is True


def test_oi_source_day_for_first_target_uses_the_prestart_observed_session():
    from tools.replay_directional_stress90_new_period import _oi_source_days_for_targets

    target_days = pd.to_datetime(["2026-08-21", "2026-08-24"])
    observed_days = pd.to_datetime(["2026-08-19", "2026-08-20", "2026-08-21", "2026-08-24"])

    source_days = _oi_source_days_for_targets(target_days, observed_days)

    assert source_days.tolist() == list(pd.to_datetime(["2026-08-20", "2026-08-21"]))


def test_evidence_json_serializes_readonly_mappings_in_frozen_dataclasses():
    from dataclasses import dataclass
    from json import dumps
    from types import MappingProxyType

    from tools.replay_directional_stress90_new_period import _json_default

    @dataclass(frozen=True)
    class Decision:
        state: object

    value = Decision(MappingProxyType({"layers": MappingProxyType({"base": 1})}))

    payload = dumps(value, default=_json_default, sort_keys=True)

    assert payload == '{"state": {"layers": {"base": 1}}}'
