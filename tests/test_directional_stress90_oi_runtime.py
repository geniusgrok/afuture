from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from afuture.models import ContractInfo, Tick

_CHINA = ZoneInfo("Asia/Shanghai")
_SUPPORTED = ("A", "C", "EG", "I", "M", "P", "PP", "TA", "Y")


def _catalog(*, two_a_contracts: bool = False) -> list[ContractInfo]:
    from afuture.directional_sessions import PRODUCT_SESSION_MANIFEST

    result = [
        ContractInfo(
            f"{product}2612",
            PRODUCT_SESSION_MANIFEST[product].exchange,
            product,
            "2026-12-15",
        )
        for product in _SUPPORTED
    ]
    if two_a_contracts:
        result.append(ContractInfo("A2701", "DCE", "A", "2027-01-15"))
    return result


def _tick(
    contract: ContractInfo,
    trading_day: str,
    timestamp: datetime,
    *,
    price: float = 100.0,
    volume: float = 10.0,
    hold: float = 100.0,
) -> Tick:
    return Tick(
        symbol=contract.symbol,
        exchange=contract.exchange,
        timestamp=timestamp,
        bid_price=price - 0.5,
        ask_price=price + 0.5,
        last_price=price,
        bid_volume=10.0,
        ask_volume=10.0,
        trading_day=trading_day,
        volume=volume,
        open_interest=hold,
        open_price=price,
    )


def _observe_complete_flat_products(aggregator, catalog, day="20260825") -> None:
    for contract in catalog:
        if contract.product == "A":
            continue
        aggregator.observe_raw_tick(
            _tick(contract, day, datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA)), contract
        )
        aggregator.observe_raw_tick(
            _tick(
                contract,
                day,
                datetime(2026, 8, 25, 14, 59, tzinfo=_CHINA),
                volume=20,
            ),
            contract,
        )


def _fixed_historical_bars(day: str = "20260825") -> list[dict[str, object]]:
    timestamp = datetime.strptime(day, "%Y%m%d").replace(tzinfo=_CHINA)
    bars: list[dict[str, object]] = []
    for product in _SUPPORTED:
        bars.extend(
            [
                {
                    "datetime": timestamp.replace(hour=9),
                    "product": product,
                    "symbol": f"{product}2612",
                    "open": 100.0,
                    "close": 100.5,
                    "volume": 10.0,
                    "hold": 100.0,
                },
                {
                    "datetime": timestamp.replace(hour=14),
                    "product": product,
                    "symbol": f"{product}2612",
                    "open": 100.5,
                    "close": 101.0,
                    "volume": 20.0,
                    "hold": 110.0,
                },
            ]
        )
    return bars


def test_fixed_historical_60m_bridge_round_trips_through_verified_store(tmp_path: Path):
    from afuture.directional_stress90_oi_runtime import (
        Stress90OiEvidenceState,
        Stress90OiEvidenceStore,
        build_fixed_historical_60m_evidence,
    )

    evidence = build_fixed_historical_60m_evidence("20260825", _fixed_historical_bars())
    store = Stress90OiEvidenceStore(tmp_path / "stress90_oi_evidence.json")

    saved = store.save_state(Stress90OiEvidenceState(completed=(evidence,)))
    loaded = store.load_required_record()

    assert saved.sequence == 1
    assert loaded == saved
    assert loaded.state.completed[0].source == "fixed_historical_60m"
    assert loaded.state.completed[0].trading_day == "20260825"
    assert loaded.state.completed[0].complete is True
    assert loaded.state.completed[0].missing_contracts == ()
    assert set(loaded.state.completed[0].flows) == set(_SUPPORTED)
    assert loaded.state.completed[0].evidence_digest == evidence.evidence_digest


def test_fixed_historical_60m_bridge_rejects_missing_supported_product():
    from afuture.directional_stress90_oi_runtime import (
        OiEvidenceIntegrityError,
        build_fixed_historical_60m_evidence,
    )

    bars = [
        {
            "datetime": datetime(2026, 8, 25, 9, 0, tzinfo=_CHINA),
            "product": product,
            "symbol": f"{product}2612",
            "open": 100.0,
            "close": 101.0,
            "volume": 10.0,
            "hold": 110.0,
        }
        for product in _SUPPORTED
        if product != "A"
    ]

    with pytest.raises(OiEvidenceIntegrityError, match="coverage.*missing=.*A"):
        build_fixed_historical_60m_evidence("20260825", bars)


def test_runtime_bootstrap_arms_catalog_without_reopening_fixed_completed_day(tmp_path: Path):
    from afuture.directional_stress90_oi_runtime import (
        Stress90OiEvidenceAggregator,
        Stress90OiEvidenceState,
        Stress90OiEvidenceStore,
        build_fixed_historical_60m_evidence,
    )

    store = Stress90OiEvidenceStore(tmp_path / "stress90_oi_evidence.json")
    bridge = build_fixed_historical_60m_evidence("20260825", _fixed_historical_bars())
    store.save_state(Stress90OiEvidenceState(completed=(bridge,)))
    aggregator = Stress90OiEvidenceAggregator(store=store)

    aggregator.set_expected_contracts("20260825", _catalog())
    aggregator.refresh_contract_catalog("20260825", _catalog())
    aggregator.checkpoint()

    record = store.load_required_record()
    assert record.sequence == 1
    assert record.state.completed == (bridge,)
    assert record.state.in_progress is None


def test_first_live_tick_after_fixed_bridge_starts_next_raw_ctp_day(tmp_path: Path):
    from afuture.directional_stress90_oi_runtime import (
        Stress90OiEvidenceAggregator,
        Stress90OiEvidenceState,
        Stress90OiEvidenceStore,
        build_fixed_historical_60m_evidence,
    )

    store = Stress90OiEvidenceStore(tmp_path / "stress90_oi_evidence.json")
    bridge = build_fixed_historical_60m_evidence("20260825", _fixed_historical_bars())
    store.save_state(Stress90OiEvidenceState(completed=(bridge,)))
    catalog = _catalog()
    aggregator = Stress90OiEvidenceAggregator(store=store)
    aggregator.set_expected_contracts("20260825", catalog)
    first = catalog[0]

    aggregator.observe_raw_tick(
        _tick(first, "20260826", datetime(2026, 8, 25, 21, 0, tzinfo=_CHINA)),
        first,
    )

    assert aggregator.completed_evidence("20260825").source == "fixed_historical_60m"
    assert aggregator.in_progress_contract(first.symbol).trading_day == "20260826"


def test_raw_ticks_build_hourly_bars_volume_deltas_and_completed_flow():
    from afuture.directional_stress90_oi_runtime import Stress90OiEvidenceAggregator

    catalog = _catalog()
    by_product = {item.product: item for item in catalog}
    aggregator = Stress90OiEvidenceAggregator()
    aggregator.set_expected_contracts("20260825", catalog)
    _observe_complete_flat_products(aggregator, catalog)
    contract = by_product["A"]
    aggregator.observe_raw_tick(
        _tick(
            contract,
            "20260825",
            datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA),
            price=100,
            volume=10,
            hold=100,
        ),
        contract,
    )
    aggregator.observe_raw_tick(
        _tick(
            contract,
            "20260825",
            datetime(2026, 8, 24, 22, 0, tzinfo=_CHINA),
            price=101,
            volume=15,
            hold=105,
        ),
        contract,
    )
    aggregator.observe_raw_tick(
        _tick(
            contract,
            "20260825",
            datetime(2026, 8, 25, 14, 59, tzinfo=_CHINA),
            price=102,
            volume=30,
            hold=110,
        ),
        contract,
    )

    aggregator.set_expected_contracts("20260826", catalog)
    completed = aggregator.completed_evidence("20260825")
    a = completed.contracts[contract.symbol]

    assert completed.source == "ctp_raw_tick"
    assert completed.complete is True
    assert completed.flows["A"] == 1
    assert completed.flows["C"] == 0
    assert completed.dominant_symbols["A"] == contract.symbol
    assert (a.first_open, a.last_close, a.first_hold, a.last_hold) == (100, 102, 100, 110)
    assert a.total_volume == 30
    assert [bar.bucket_start.hour for bar in a.bars[:2]] == [21, 22]
    assert a.bars[0].total_volume == 10
    assert a.bars[1].total_volume == 5
    assert completed.missing_contracts == ()


def test_raw_flow_uses_first_completed_60m_bar_hold_not_session_open_tick_hold():
    from afuture.directional_stress90_oi_runtime import Stress90OiEvidenceAggregator

    catalog = _catalog()
    by_product = {item.product: item for item in catalog}
    aggregator = Stress90OiEvidenceAggregator()
    aggregator.set_expected_contracts("20260825", catalog)
    _observe_complete_flat_products(aggregator, catalog)
    contract = by_product["A"]
    aggregator.observe_raw_tick(
        _tick(
            contract,
            "20260825",
            datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA),
            price=100,
            volume=10,
            hold=100,
        ),
        contract,
    )
    aggregator.observe_raw_tick(
        _tick(
            contract,
            "20260825",
            datetime(2026, 8, 24, 21, 30, tzinfo=_CHINA),
            price=101,
            volume=15,
            hold=120,
        ),
        contract,
    )
    aggregator.observe_raw_tick(
        _tick(
            contract,
            "20260825",
            datetime(2026, 8, 25, 14, 59, tzinfo=_CHINA),
            price=102,
            volume=30,
            hold=110,
        ),
        contract,
    )

    aggregator.set_expected_contracts("20260826", catalog)
    completed = aggregator.completed_evidence("20260825")

    assert completed.contracts[contract.symbol].bars[0].last_hold == 120
    assert completed.contracts[contract.symbol].first_hold == 120
    assert completed.contracts[contract.symbol].last_hold == 110
    assert completed.flows["A"] == 0


def test_raw_first_60m_open_ignores_zero_volume_pretrade_snapshot():
    from afuture.directional_stress90_oi_runtime import Stress90OiEvidenceAggregator

    catalog = _catalog()
    by_product = {item.product: item for item in catalog}
    aggregator = Stress90OiEvidenceAggregator()
    aggregator.set_expected_contracts("20260825", catalog)
    _observe_complete_flat_products(aggregator, catalog)
    contract = by_product["A"]
    pretrade = _tick(
        contract,
        "20260825",
        datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA),
        price=90,
        volume=0,
        hold=100,
    )
    first_trade = _tick(
        contract,
        "20260825",
        datetime(2026, 8, 24, 21, 1, tzinfo=_CHINA),
        price=100,
        volume=1,
        hold=101,
    )
    aggregator.observe_raw_tick(pretrade, contract)
    aggregator.observe_raw_tick(first_trade, contract)
    aggregator.observe_raw_tick(
        _tick(
            contract,
            "20260825",
            datetime(2026, 8, 25, 14, 59, tzinfo=_CHINA),
            price=102,
            volume=30,
            hold=120,
        ),
        contract,
    )

    aggregator.set_expected_contracts("20260826", catalog)
    completed = aggregator.completed_evidence("20260825")

    assert completed.contracts[contract.symbol].first_open == 100
    assert completed.contracts[contract.symbol].first_tick_timestamp == first_trade.timestamp


def test_vendor_comparator_covers_mechanics_and_blocks_unexplained_flow_difference():
    from dataclasses import replace

    from afuture.directional_stress90_oi_comparator import (
        VendorOiProductEvidence,
        VendorOiReference,
        compare_completed_oi_evidence,
    )
    from afuture.directional_stress90_oi_runtime import Stress90OiEvidenceAggregator

    catalog = _catalog()
    aggregator = Stress90OiEvidenceAggregator()
    aggregator.set_expected_contracts("20260825", catalog)
    _observe_complete_flat_products(aggregator, catalog)
    a = next(item for item in catalog if item.product == "A")
    aggregator.observe_raw_tick(
        _tick(
            a,
            "20260825",
            datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA),
            price=100,
            volume=10,
            hold=100,
        ),
        a,
    )
    aggregator.observe_raw_tick(
        _tick(
            a,
            "20260825",
            datetime(2026, 8, 25, 14, 59, tzinfo=_CHINA),
            price=102,
            volume=30,
            hold=110,
        ),
        a,
    )
    aggregator.set_expected_contracts("20260826", catalog)
    completed = aggregator.completed_evidence("20260825")
    products = {}
    for product in _SUPPORTED:
        symbol = completed.dominant_symbols[product]
        assert symbol is not None
        row = completed.contracts[symbol]
        products[product] = VendorOiProductEvidence(
            symbol,
            row.first_open,
            row.last_close,
            row.first_hold,
            row.last_hold,
            row.total_volume,
            int(completed.flows[product]),
        )
    reference = VendorOiReference("approved_vendor", "20260825", products)

    exact = compare_completed_oi_evidence(completed, reference)
    changed = dict(products)
    changed["A"] = replace(changed["A"], flow=-1)
    mismatch = compare_completed_oi_evidence(
        completed,
        VendorOiReference("approved_vendor", "20260825", changed),
    )

    assert exact.matched is True
    assert exact.unexplained_flow_differences == ()
    assert mismatch.matched is False
    assert mismatch.unexplained_flow_differences == ("A",)
    assert mismatch.product_results["A"]["flow_matched"] is False


def test_vendor_oi_reference_loader_is_schema_strict_and_reports_input_sha(tmp_path: Path):
    from afuture.directional_stress90_oi_comparator import load_vendor_oi_reference

    payload = {
        "schema_version": 1,
        "source": "approved_vendor",
        "trading_day": "20260825",
        "products": {
            product: {
                "dominant_symbol": f"{product}2612",
                "first_open": 100.0,
                "last_close": 101.0,
                "first_hold": 1000.0,
                "last_hold": 1100.0,
                "total_volume": 500.0,
                "flow": 1,
            }
            for product in _SUPPORTED
        },
    }
    path = tmp_path / "vendor.json"
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    path.write_bytes(encoded)

    reference, digest = load_vendor_oi_reference(path)

    assert reference.trading_day == "20260825"
    assert digest == __import__("hashlib").sha256(encoded).hexdigest()
    path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_vendor_oi_reference(path)


def test_dominant_contract_tie_break_is_hold_then_volume_then_symbol():
    from afuture.directional_stress90_oi_runtime import Stress90OiEvidenceAggregator

    catalog = _catalog(two_a_contracts=True)
    extra = ContractInfo("A2609", "DCE", "A", "2026-09-15")
    catalog.append(extra)
    aggregator = Stress90OiEvidenceAggregator()
    aggregator.set_expected_contracts("20260825", catalog)
    _observe_complete_flat_products(aggregator, catalog)
    a_contracts = [item for item in catalog if item.product == "A"]
    settings = {
        "A2612": (120.0, 100.0),
        "A2701": (120.0, 120.0),
        "A2609": (120.0, 120.0),
    }
    for contract in a_contracts:
        final_hold, final_volume = settings[contract.symbol]
        aggregator.observe_raw_tick(
            _tick(
                contract,
                "20260825",
                datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA),
                price=101,
                volume=10,
                hold=100,
            ),
            contract,
        )
        aggregator.observe_raw_tick(
            _tick(
                contract,
                "20260825",
                datetime(2026, 8, 25, 14, 59, tzinfo=_CHINA),
                price=99,
                volume=final_volume,
                hold=final_hold,
            ),
            contract,
        )
    aggregator.set_expected_contracts("20260826", catalog)

    completed = aggregator.completed_evidence("20260825")
    assert completed.dominant_symbols["A"] == "A2609"
    assert completed.flows["A"] == -1


def test_missing_expected_higher_oi_contract_is_missing_not_legal_zero():
    from afuture.directional_stress90_oi_runtime import Stress90OiEvidenceAggregator

    catalog = _catalog(two_a_contracts=True)
    aggregator = Stress90OiEvidenceAggregator()
    aggregator.set_expected_contracts("20260825", catalog)
    _observe_complete_flat_products(aggregator, catalog)
    a = next(item for item in catalog if item.symbol == "A2612")
    aggregator.observe_raw_tick(
        _tick(a, "20260825", datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA)), a
    )
    aggregator.observe_raw_tick(
        _tick(
            a,
            "20260825",
            datetime(2026, 8, 25, 14, 59, tzinfo=_CHINA),
            volume=20,
        ),
        a,
    )
    aggregator.set_expected_contracts("20260826", catalog)

    completed = aggregator.completed_evidence("20260825")
    assert completed.complete is False
    assert completed.flows["A"] is None
    assert completed.flows["C"] == 0
    assert completed.missing_contracts == ("A2701",)


def test_unverified_ctp_source_trading_day_marks_oi_day_incomplete():
    from afuture.directional_stress90_oi_runtime import (
        OiEvidenceIntegrityError,
        Stress90OiEvidenceAggregator,
    )

    catalog = _catalog()
    a = catalog[0]
    aggregator = Stress90OiEvidenceAggregator()
    aggregator.set_expected_contracts("20260825", catalog)
    tick = replace(
        _tick(a, "20260825", datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA)),
        source_trading_day_verified=False,
    )

    with pytest.raises(OiEvidenceIntegrityError, match="source trading day"):
        aggregator.observe_raw_tick(tick, a)

    assert aggregator.in_progress_complete is False


def test_duplicate_is_idempotent_out_of_order_marks_day_incomplete_and_reset_is_counted():
    from afuture.directional_stress90_oi_runtime import (
        OiEvidenceIntegrityError,
        Stress90OiEvidenceAggregator,
    )

    catalog = _catalog()
    a = catalog[0]
    aggregator = Stress90OiEvidenceAggregator()
    aggregator.set_expected_contracts("20260825", catalog)
    first = _tick(
        a,
        "20260825",
        datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA),
        volume=100,
    )
    aggregator.observe_raw_tick(first, a)
    aggregator.observe_raw_tick(first, a)
    aggregator.observe_raw_tick(
        _tick(
            a,
            "20260825",
            datetime(2026, 8, 24, 21, 1, tzinfo=_CHINA),
            volume=5,
        ),
        a,
    )
    with pytest.raises(OiEvidenceIntegrityError, match="out-of-order"):
        aggregator.observe_raw_tick(
            _tick(
                a,
                "20260825",
                datetime(2026, 8, 24, 21, 0, 30, tzinfo=_CHINA),
                volume=101,
            ),
            a,
        )

    counters = aggregator.counters()
    assert counters["raw_ticks_observed"] == 4
    assert counters["duplicate_ticks"] == 1
    assert counters["volume_resets"] == 1
    assert aggregator.in_progress_contract(a.symbol).total_volume == 105
    assert aggregator.in_progress_complete is False


def test_no_trade_and_outside_session_remain_incomplete():
    from afuture.directional_stress90_oi_runtime import (
        OiEvidenceIntegrityError,
        Stress90OiEvidenceAggregator,
    )

    catalog = _catalog()
    aggregator = Stress90OiEvidenceAggregator()
    aggregator.set_expected_contracts("20260825", catalog)
    a = catalog[0]
    with pytest.raises(OiEvidenceIntegrityError, match="outside fixed session"):
        aggregator.observe_raw_tick(
            _tick(a, "20260825", datetime(2026, 8, 24, 16, 0, tzinfo=_CHINA)), a
        )
    aggregator.set_expected_contracts("20260826", catalog)

    completed = aggregator.completed_evidence("20260825")
    assert completed.complete is False
    assert all(value is None for value in completed.flows.values())
    assert set(completed.missing_contracts) == {item.symbol for item in catalog}


def test_starting_only_at_day_session_cannot_claim_complete_night_evidence():
    from afuture.directional_stress90_oi_runtime import Stress90OiEvidenceAggregator

    catalog = _catalog()
    aggregator = Stress90OiEvidenceAggregator()
    aggregator.set_expected_contracts("20260825", catalog)
    for contract in catalog:
        aggregator.observe_raw_tick(
            _tick(
                contract,
                "20260825",
                datetime(2026, 8, 25, 9, 0, tzinfo=_CHINA),
            ),
            contract,
        )
        aggregator.observe_raw_tick(
            _tick(
                contract,
                "20260825",
                datetime(2026, 8, 25, 14, 59, tzinfo=_CHINA),
                volume=20,
            ),
            contract,
        )
    aggregator.set_expected_contracts("20260826", catalog)

    completed = aggregator.completed_evidence("20260825")
    assert completed.complete is False
    assert all(value is None for value in completed.flows.values())
    assert all(
        "opening_session_boundary_missing" in contract.issues
        for contract in completed.contracts.values()
    )


def test_in_progress_coverage_is_not_complete_before_closing_boundary():
    from afuture.directional_stress90_oi_runtime import Stress90OiEvidenceAggregator

    catalog = _catalog()
    aggregator = Stress90OiEvidenceAggregator()
    aggregator.set_expected_contracts("20260825", catalog)
    for contract in catalog:
        aggregator.observe_raw_tick(
            _tick(
                contract,
                "20260825",
                datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA),
            ),
            contract,
        )

    assert aggregator.in_progress_complete is False


def test_expected_universe_uses_target_day_for_listing_and_expiry(tmp_path: Path):
    from afuture.directional_stress90_oi_runtime import (
        Stress90OiEvidenceAggregator,
        Stress90OiEvidenceStore,
    )

    path = tmp_path / "stress90_oi_evidence.json"
    catalog = _catalog()
    catalog.extend(
        [
            ContractInfo("A2607", "DCE", "A", "2026-08-24"),
            ContractInfo("A2701", "DCE", "A", "2027-01-15", listing="2026-08-26"),
        ]
    )
    aggregator = Stress90OiEvidenceAggregator(store=Stress90OiEvidenceStore(path))
    aggregator.set_expected_contracts("20260825", catalog)
    aggregator.checkpoint()

    state = Stress90OiEvidenceStore(path).load_required_record().state
    assert state.in_progress is not None
    assert state.in_progress.expected_contracts["A"] == ("A2612",)


def test_expected_universe_rejects_non_future_symbol_and_same_day_catalog_change():
    from afuture.directional_stress90_oi_runtime import (
        OiEvidenceIntegrityError,
        Stress90OiEvidenceAggregator,
    )

    catalog = _catalog()
    aggregator = Stress90OiEvidenceAggregator()
    aggregator.set_expected_contracts("20260825", catalog)
    with pytest.raises(OiEvidenceIntegrityError, match="changed during trading day"):
        aggregator.set_expected_contracts(
            "20260825",
            [*catalog, ContractInfo("A2701", "DCE", "A", "2027-01-15")],
        )
    with pytest.raises(OiEvidenceIntegrityError, match="futures symbol"):
        Stress90OiEvidenceAggregator().set_expected_contracts(
            "20260825",
            [*catalog, ContractInfo("A2612-C-100", "DCE", "A", "2026-12-15")],
        )


def test_valid_unsupported_product_tick_is_observed_but_does_not_poison_oi_state():
    from afuture.directional_stress90_oi_runtime import Stress90OiEvidenceAggregator

    catalog = _catalog()
    unsupported = ContractInfo("AG2612", "SHFE", "AG", "2026-12-15")
    aggregator = Stress90OiEvidenceAggregator()
    aggregator.set_expected_contracts("20260825", catalog)

    aggregator.observe_raw_tick(
        _tick(
            unsupported,
            "20260825",
            datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA),
        ),
        unsupported,
    )

    assert aggregator.counters()["raw_ticks_observed"] == 1
    assert aggregator.in_progress_complete is False


def test_unsupported_symbol_without_catalog_metadata_does_not_poison_supported_oi_state():
    from afuture.directional_stress90_oi_runtime import Stress90OiEvidenceAggregator

    catalog = _catalog()
    unsupported = ContractInfo("AG2612", "SHFE", "AG", "2026-12-15")
    aggregator = Stress90OiEvidenceAggregator()
    aggregator.set_expected_contracts("20260825", catalog)

    aggregator.observe_raw_tick(
        _tick(
            unsupported,
            "20260825",
            datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA),
        ),
        None,
    )

    assert aggregator.counters()["raw_ticks_observed"] == 1
    assert aggregator.in_progress_issues() == ()


def test_supported_raw_tick_hot_path_does_not_rebuild_global_collections(monkeypatch):
    import afuture.directional_stress90_oi_runtime as oi_runtime

    catalog = _catalog()
    a = catalog[0]
    aggregator = oi_runtime.Stress90OiEvidenceAggregator()
    aggregator.set_expected_contracts("20260825", catalog)
    aggregator.observe_raw_tick(
        _tick(a, "20260825", datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA)),
        a,
    )

    def fail_hot_path(*_args, **_kwargs):
        raise AssertionError("raw Tick hot path rebuilt a global collection")

    monkeypatch.setattr(oi_runtime, "_all_expected_symbols", fail_hot_path)
    monkeypatch.setattr(oi_runtime, "MappingProxyType", fail_hot_path)
    aggregator.observe_raw_tick(
        _tick(
            a,
            "20260825",
            datetime(2026, 8, 24, 21, 1, tzinfo=_CHINA),
            price=101.0,
            volume=11.0,
        ),
        a,
    )

    assert aggregator.in_progress_contract(a.symbol).last_close == 101.0


def test_checkpoint_snapshot_is_isolated_from_tick_observed_during_store_write():
    from afuture.directional_stress90_oi_runtime import (
        Stress90OiEvidenceAggregator,
        Stress90OiEvidenceRecord,
    )

    catalog = _catalog()
    a = catalog[0]

    class ReentrantStore:
        def __init__(self) -> None:
            self.aggregator = None
            self.captured = None

        def load_record(self):
            return None

        def save_state(self, state, *, expected_sequence=None):
            self.captured = state
            self.aggregator.observe_raw_tick(
                _tick(
                    a,
                    "20260825",
                    datetime(2026, 8, 24, 21, 1, tzinfo=_CHINA),
                    price=102.0,
                    volume=12.0,
                ),
                a,
            )
            assert state.in_progress.contracts[a.symbol].last_close == 100.0
            return Stress90OiEvidenceRecord(state, int(expected_sequence or 0) + 1, "a" * 64)

    store = ReentrantStore()
    aggregator = Stress90OiEvidenceAggregator(store=store)
    store.aggregator = aggregator
    aggregator.set_expected_contracts("20260825", catalog)
    aggregator.observe_raw_tick(
        _tick(a, "20260825", datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA)),
        a,
    )

    aggregator.checkpoint()

    assert store.captured.in_progress.contracts[a.symbol].last_close == 100.0
    assert aggregator.in_progress_contract(a.symbol).last_close == 102.0


def test_raw_oi_issue_memory_is_bounded_under_invalid_contract_flood():
    from afuture.directional_stress90_oi_runtime import (
        MAX_OI_ISSUES,
        OiEvidenceIntegrityError,
        Stress90OiEvidenceAggregator,
    )

    aggregator = Stress90OiEvidenceAggregator()
    aggregator.set_expected_contracts("20260825", _catalog())
    for index in range(MAX_OI_ISSUES + 20):
        contract = ContractInfo(f"A{index:04d}", "DCE", "A", "2027-12-15")
        with pytest.raises(OiEvidenceIntegrityError, match="outside expected universe"):
            aggregator.observe_raw_tick(
                _tick(
                    contract,
                    "20260825",
                    datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA),
                ),
                contract,
            )

    issues = aggregator.in_progress_issues()
    assert len(issues) <= MAX_OI_ISSUES
    assert "issue_memory_bound_exceeded" in issues


def test_checkpoint_restores_in_progress_then_persists_rollover_once(tmp_path: Path):
    from afuture.directional_stress90_oi_runtime import (
        Stress90OiEvidenceAggregator,
        Stress90OiEvidenceStore,
    )

    path = tmp_path / "stress90_oi_evidence.json"
    store = Stress90OiEvidenceStore(path)
    catalog = _catalog()
    first = Stress90OiEvidenceAggregator(store=store)
    first.set_expected_contracts("20260825", catalog)
    for contract in catalog:
        first.observe_raw_tick(
            _tick(
                contract,
                "20260825",
                datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA),
            ),
            contract,
        )
    assert not path.exists()
    first.checkpoint()
    assert store.load_required_record().sequence == 1

    restarted = Stress90OiEvidenceAggregator(store=Stress90OiEvidenceStore(path))
    restarted.note_raw_market_connection(connected=True, generation=1)
    for contract in catalog:
        restarted.observe_raw_tick(
            _tick(
                contract,
                "20260825",
                datetime(2026, 8, 25, 14, 59, tzinfo=_CHINA),
                volume=20,
            ),
            contract,
        )
    restarted.set_expected_contracts("20260826", catalog)
    restarted.observe_raw_tick(
        _tick(
            catalog[0],
            "20260826",
            datetime(2026, 8, 25, 21, 0, tzinfo=_CHINA),
        ),
        catalog[0],
    )
    restarted.checkpoint()

    record = store.load_required_record()
    assert record.sequence == 2
    assert record.state.completed[-1].trading_day == "20260825"
    assert record.state.completed[-1].complete is True
    assert len(record.state.observed_transitions) == 1
    transition = record.state.observed_transitions[0]
    assert transition.source_trading_day == "20260825"
    assert transition.target_trading_day == "20260826"
    assert transition.completed_oi_evidence_digest == record.state.completed[-1].evidence_digest
    assert record.state.in_progress is not None
    assert record.state.in_progress.trading_day == "20260826"


def test_restart_cannot_invent_unobserved_ctp_trading_day_transition(tmp_path: Path):
    from afuture.directional_stress90_oi_runtime import (
        Stress90OiEvidenceAggregator,
        Stress90OiEvidenceStore,
    )

    path = tmp_path / "stress90_oi_evidence.json"
    catalog = _catalog()
    first = Stress90OiEvidenceAggregator(store=Stress90OiEvidenceStore(path))
    first.set_expected_contracts("20260821", catalog)
    for contract in catalog:
        first.observe_raw_tick(
            _tick(
                contract,
                "20260821",
                datetime(2026, 8, 20, 21, 0, tzinfo=_CHINA),
            ),
            contract,
        )
    first.checkpoint()

    restarted = Stress90OiEvidenceAggregator(store=Stress90OiEvidenceStore(path))
    restarted.set_expected_contracts("20260825", catalog)
    restarted.checkpoint()

    record = Stress90OiEvidenceStore(path).load_required_record()
    assert record.state.completed[-1].trading_day == "20260821"
    assert record.state.observed_transitions == ()


def test_same_process_unproven_ctp_day_jump_cannot_invent_transition(tmp_path: Path):
    """A live process can remain up while MD disconnects across a real target day."""

    from afuture.directional_stress90_oi_runtime import (
        Stress90OiEvidenceAggregator,
        Stress90OiEvidenceStore,
    )

    path = tmp_path / "stress90_oi_evidence.json"
    catalog = _catalog()
    aggregator = Stress90OiEvidenceAggregator(store=Stress90OiEvidenceStore(path))
    aggregator.set_expected_contracts("20260821", catalog)
    aggregator.note_raw_market_connection(connected=True, generation=1)
    first = catalog[0]
    aggregator.observe_raw_tick(
        _tick(
            first,
            "20260821",
            datetime(2026, 8, 20, 21, 0, tzinfo=_CHINA),
        ),
        first,
    )

    # Even an uninterrupted MD generation cannot prove that 20260824 was a
    # holiday rather than an entirely missed session. Fail closed rather than
    # manufacturing a 20260821 -> 20260825 authoritative transition.
    aggregator.observe_raw_tick(
        _tick(
            first,
            "20260825",
            datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA),
        ),
        first,
    )
    aggregator.checkpoint()

    record = Stress90OiEvidenceStore(path).load_required_record()
    assert record.state.completed[-1].trading_day == "20260821"
    assert record.state.observed_transitions == ()


def test_ctp_md_disconnect_breaks_same_process_day_continuity(tmp_path: Path):
    from afuture.directional_stress90_oi_runtime import (
        Stress90OiEvidenceAggregator,
        Stress90OiEvidenceStore,
    )

    path = tmp_path / "stress90_oi_evidence.json"
    catalog = _catalog()
    first = catalog[0]
    aggregator = Stress90OiEvidenceAggregator(store=Stress90OiEvidenceStore(path))
    aggregator.set_expected_contracts("20260821", catalog)
    aggregator.note_raw_market_connection(connected=True, generation=1)
    aggregator.observe_raw_tick(
        _tick(first, "20260821", datetime(2026, 8, 20, 21, 0, tzinfo=_CHINA)),
        first,
    )
    aggregator.note_raw_market_connection(connected=False, generation=1)
    aggregator.note_raw_market_connection(connected=True, generation=2)
    aggregator.observe_raw_tick(
        _tick(first, "20260825", datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA)),
        first,
    )
    aggregator.checkpoint()

    record = Stress90OiEvidenceStore(path).load_required_record()
    assert record.state.completed[-1].complete is False
    assert "raw_market_connection_interrupted" in record.state.completed[-1].issues
    assert record.state.observed_transitions == ()


def test_first_supported_tick_of_next_ctp_day_rolls_memory_without_disk_io():
    from afuture.directional_stress90_oi_runtime import Stress90OiEvidenceAggregator

    catalog = _catalog()
    aggregator = Stress90OiEvidenceAggregator()
    aggregator.set_expected_contracts("20260825", catalog)
    for contract in catalog:
        aggregator.observe_raw_tick(
            _tick(
                contract,
                "20260825",
                datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA),
            ),
            contract,
        )
        aggregator.observe_raw_tick(
            _tick(
                contract,
                "20260825",
                datetime(2026, 8, 25, 14, 59, tzinfo=_CHINA),
                volume=20,
            ),
            contract,
        )

    first = catalog[0]
    aggregator.observe_raw_tick(
        _tick(
            first,
            "20260826",
            datetime(2026, 8, 25, 21, 0, tzinfo=_CHINA),
        ),
        first,
    )

    assert aggregator.completed_evidence("20260825").complete is True
    assert aggregator.in_progress_contract(first.symbol).trading_day == "20260826"


def test_late_tick_after_completed_day_is_fatal_instead_of_revising_consumable_evidence():
    from afuture.broker.base import RawMarketEvidenceFatalError
    from afuture.directional_stress90_oi_runtime import Stress90OiEvidenceAggregator

    catalog = _catalog()
    aggregator = Stress90OiEvidenceAggregator()
    aggregator.set_expected_contracts("20260825", catalog)
    for contract in catalog:
        aggregator.observe_raw_tick(
            _tick(
                contract,
                "20260825",
                datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA),
            ),
            contract,
        )
        aggregator.observe_raw_tick(
            _tick(
                contract,
                "20260825",
                datetime(2026, 8, 25, 14, 59, tzinfo=_CHINA),
                volume=20,
            ),
            contract,
        )
    aggregator.set_expected_contracts("20260826", catalog)
    assert aggregator.completed_evidence("20260825").complete is True

    with pytest.raises(RawMarketEvidenceFatalError, match="completed OI day"):
        aggregator.observe_raw_tick(
            _tick(
                catalog[0],
                "20260825",
                datetime(2026, 8, 25, 15, 0, tzinfo=_CHINA),
                volume=21,
            ),
            catalog[0],
        )

    assert aggregator.completed_evidence("20260825").complete is True


def test_first_tick_of_next_ctp_day_recomputes_expected_universe_from_catalog():
    from afuture.directional_stress90_oi_runtime import Stress90OiEvidenceAggregator

    catalog = [
        *_catalog(),
        ContractInfo("A2701", "DCE", "A", "2027-01-15", listing="2026-08-26"),
    ]
    aggregator = Stress90OiEvidenceAggregator()
    aggregator.set_expected_contracts("20260825", catalog)

    newly_listed = catalog[-1]
    aggregator.observe_raw_tick(
        _tick(
            newly_listed,
            "20260826",
            datetime(2026, 8, 25, 21, 0, tzinfo=_CHINA),
        ),
        newly_listed,
    )

    assert aggregator.in_progress_contract("A2701").trading_day == "20260826"


def test_new_contract_subscription_after_first_supported_tick_marks_day_incomplete():
    from afuture.directional_stress90_oi_runtime import Stress90OiEvidenceAggregator

    catalog = [
        *_catalog(),
        ContractInfo("A2701", "DCE", "A", "2027-01-15", listing="2026-08-26"),
    ]
    aggregator = Stress90OiEvidenceAggregator()
    aggregator.set_expected_contracts("20260825", catalog)
    first = catalog[0]
    aggregator.observe_raw_tick(
        _tick(first, "20260826", datetime(2026, 8, 25, 21, 0, tzinfo=_CHINA)),
        first,
    )

    aggregator.note_contract_subscriptions("20260826", ("A2701",))
    aggregator.set_expected_contracts("20260827", catalog)

    completed = aggregator.completed_evidence("20260826")
    assert completed.complete is False
    assert "late_contract_subscription:A2701" in completed.issues


def test_corrupt_current_store_fails_without_automatic_prev_fallback(tmp_path: Path):
    from afuture.directional_stress90_oi_runtime import (
        OiEvidenceIntegrityError,
        Stress90OiEvidenceAggregator,
        Stress90OiEvidenceStore,
    )

    path = tmp_path / "stress90_oi_evidence.json"
    store = Stress90OiEvidenceStore(path)
    catalog = _catalog()
    aggregator = Stress90OiEvidenceAggregator(store=store)
    aggregator.set_expected_contracts("20260825", catalog)
    aggregator.checkpoint()
    aggregator.set_expected_contracts("20260826", catalog)
    aggregator.checkpoint()
    assert store.previous_path.exists()
    path.write_text("{corrupt", encoding="utf-8")

    with pytest.raises(OiEvidenceIntegrityError, match="JSON"):
        Stress90OiEvidenceAggregator(store=Stress90OiEvidenceStore(path))
    assert store.load_previous_record().sequence == 1


def test_missing_current_store_rejects_existing_previous_evidence(tmp_path: Path):
    """Losing current must not silently reset a previously initialized lineage."""
    from afuture.directional_stress90_oi_runtime import (
        OiEvidenceIntegrityError,
        Stress90OiEvidenceState,
        Stress90OiEvidenceStore,
    )

    store = Stress90OiEvidenceStore(tmp_path / "stress90_oi_evidence.json")
    store.save_state(Stress90OiEvidenceState())
    store.save_state(Stress90OiEvidenceState())
    store.path.unlink()

    with pytest.raises(OiEvidenceIntegrityError, match="current.*missing.*previous"):
        store.load_record()


def test_oi_store_save_propagates_parent_directory_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Raw evidence rename must be directory-durable before it is acknowledged."""
    import afuture.directional_stress90_oi_runtime as oi_module
    from afuture.directional_stress90_oi_runtime import (
        Stress90OiEvidenceState,
        Stress90OiEvidenceStore,
    )

    calls = 0
    real_fsync = oi_module.os.fsync

    def fail_parent_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected OI parent fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(oi_module.os, "fsync", fail_parent_fsync)

    with pytest.raises(OSError, match="parent fsync"):
        Stress90OiEvidenceStore(tmp_path / "oi.json").save_state(Stress90OiEvidenceState())

    assert calls == 2


def test_zero_order_evidence_arm_subscribes_the_exact_validated_universe() -> None:
    from afuture.directional_stress90_oi_runtime import (
        Stress90OiEvidenceAggregator,
        arm_stress90_raw_evidence_collection,
    )

    class MarketOnlyBroker:
        def __init__(self) -> None:
            self.observer = None
            self.subscriptions: list[tuple[str, str]] = []

        def set_raw_tick_observer(self, observer) -> None:
            self.observer = observer

        def subscribe(self, symbol: str, exchange: str) -> None:
            self.subscriptions.append((symbol, exchange))

        def send_order(self, _request):
            raise AssertionError("raw evidence arming must never send an order")

    broker = MarketOnlyBroker()
    aggregator = Stress90OiEvidenceAggregator()
    catalog = _catalog()

    expected = arm_stress90_raw_evidence_collection(
        broker,
        aggregator,
        trading_day="20260825",
        catalog=catalog,
    )

    assert broker.observer is aggregator
    assert set(broker.subscriptions) == {
        (contract.symbol, contract.exchange) for contract in catalog
    }
    assert expected == tuple(sorted(contract.symbol for contract in catalog))


def test_zero_order_evidence_arm_can_subscribe_full_directional_activity_universe() -> None:
    from afuture.directional_stress90_oi_runtime import (
        Stress90OiEvidenceAggregator,
        arm_stress90_raw_evidence_collection,
    )

    class MarketOnlyBroker:
        def __init__(self) -> None:
            self.observer = None
            self.subscriptions: list[tuple[str, str]] = []

        def set_raw_tick_observer(self, observer) -> None:
            self.observer = observer

        def subscribe(self, symbol: str, exchange: str) -> None:
            self.subscriptions.append((symbol, exchange))

    broker = MarketOnlyBroker()
    catalog = [*_catalog(), ContractInfo("AG2612", "SHFE", "AG", "2026-12-15")]
    aggregator = Stress90OiEvidenceAggregator()

    expected = arm_stress90_raw_evidence_collection(
        broker,
        aggregator,
        trading_day="20260825",
        catalog=catalog,
        subscription_catalog=catalog,
    )

    assert broker.observer is aggregator
    assert set(broker.subscriptions) == {
        (contract.symbol, contract.exchange) for contract in catalog
    }
    assert "AG2612" not in expected


def test_evidence_only_broker_fence_never_delegates_order_capabilities() -> None:
    from afuture.directional_stress90_oi_runtime import Stress90EvidenceOnlyBroker

    class Source:
        def __init__(self) -> None:
            self.sent = 0
            self.cancelled = 0

        def get_trading_day(self) -> str:
            return "20260825"

        def send_order(self, _request):
            self.sent += 1

        def cancel_order(self, _order_id):
            self.cancelled += 1

    source = Source()
    broker = Stress90EvidenceOnlyBroker(source)

    assert broker.get_trading_day() == "20260825"
    with pytest.raises(RuntimeError, match="cannot send orders"):
        broker.send_order(object())
    with pytest.raises(RuntimeError, match="cannot cancel orders"):
        broker.cancel_order("order-1")
    assert source.sent == 0
    assert source.cancelled == 0


def test_collection_catalog_filters_non_futures_and_requires_all_50_products() -> None:
    from types import SimpleNamespace

    from afuture.cli import _stress90_evidence_collection_catalog
    from afuture.directional_sessions import PRODUCT_SESSION_MANIFEST
    from afuture.execution_aligned_policy import FROZEN_PRODUCTS

    catalog = [
        ContractInfo(
            f"{product}2612",
            PRODUCT_SESSION_MANIFEST[product].exchange,
            product,
            "2026-12-15",
        )
        for product in FROZEN_PRODUCTS
    ]
    catalog.extend(
        [
            ContractInfo("A2612-C-4000", "DCE", "A", "2026-12-15"),
            ContractInfo("A2501", "DCE", "A", "2025-01-15"),
        ]
    )
    config = SimpleNamespace(
        products=FROZEN_PRODUCTS,
        exchanges=tuple(
            sorted({PRODUCT_SESSION_MANIFEST[product].exchange for product in FROZEN_PRODUCTS})
        ),
    )

    allowed = _stress90_evidence_collection_catalog(config, catalog, "20260825")

    assert len(allowed) == len(FROZEN_PRODUCTS)
    assert {contract.product for contract in allowed} == set(FROZEN_PRODUCTS)
    with pytest.raises(RuntimeError, match="missing products"):
        _stress90_evidence_collection_catalog(config, catalog[1:], "20260825")


def test_observer_never_performs_store_io_until_explicit_checkpoint(tmp_path: Path):
    from afuture.directional_stress90_oi_runtime import (
        Stress90OiEvidenceAggregator,
        Stress90OiEvidenceStore,
    )

    class FailingStore(Stress90OiEvidenceStore):
        def save_state(self, state, *, expected_sequence=None):
            raise OSError("injected disk I/O")

    catalog = _catalog()
    aggregator = Stress90OiEvidenceAggregator(
        store=FailingStore(tmp_path / "stress90_oi_evidence.json")
    )
    aggregator.set_expected_contracts("20260825", catalog)
    aggregator.observe_raw_tick(
        _tick(
            catalog[0],
            "20260825",
            datetime(2026, 8, 24, 21, 0, tzinfo=_CHINA),
        ),
        catalog[0],
    )
    with pytest.raises(OSError, match="injected disk I/O"):
        aggregator.checkpoint()


def test_store_rejects_tamper_duplicate_keys_and_nonfinite_values(tmp_path: Path):
    from hashlib import sha256

    from afuture.directional_stress90_oi_runtime import (
        OiEvidenceIntegrityError,
        Stress90OiEvidenceAggregator,
        Stress90OiEvidenceStore,
    )

    path = tmp_path / "stress90_oi_evidence.json"
    store = Stress90OiEvidenceStore(path)
    aggregator = Stress90OiEvidenceAggregator(store=store)
    aggregator.set_expected_contracts("20260825", _catalog())
    aggregator.checkpoint()
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["state"]["in_progress"]["contracts"] = {"x": {"total_volume": float("nan")}}
    unsigned = {key: value for key, value in envelope.items() if key != "checksum"}
    envelope["checksum"] = sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(OiEvidenceIntegrityError):
        store.load_required_record()

    path.write_text('{"kind":"a","kind":"b"}', encoding="utf-8")
    with pytest.raises(OiEvidenceIntegrityError, match="duplicate"):
        store.load_required_record()
