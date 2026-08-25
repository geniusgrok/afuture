from __future__ import annotations

import json
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
    restarted.checkpoint()

    record = store.load_required_record()
    assert record.sequence == 2
    assert record.state.completed[-1].trading_day == "20260825"
    assert record.state.completed[-1].complete is True
    assert record.state.in_progress is not None
    assert record.state.in_progress.trading_day == "20260826"


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
