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
