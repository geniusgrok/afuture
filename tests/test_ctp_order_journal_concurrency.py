from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import queue
import threading
from base64 import b64decode, b64encode
from dataclasses import replace
from pathlib import Path

import pytest

from afuture.broker.ctp_order_journal import (
    CTP_ORDER_JOURNAL_EPOCH_CONFIRMATION,
    CtpOrderFillEvidence,
    CtpOrderJournalIntegrityError,
    CtpOrderSubmissionEntry,
    CtpOrderSubmissionJournal,
)
from afuture.models import Offset, OrderRequest, OrderSide, OrderType


def _entry(
    sequence: int,
    *,
    status: str = "prepared",
    authorization_kind: str = "candidate",
    offset: Offset = Offset.OPEN,
    target_trading_day: str = "20260825",
) -> CtpOrderSubmissionEntry:
    order_ref = 100 + sequence
    request = OrderRequest(
        "A2701",
        "DCE",
        OrderSide.BUY if offset is Offset.OPEN else OrderSide.SELL,
        offset,
        3,
        1_001.0,
        OrderType.FAK,
        f"directional:test:{sequence}",
    )
    return CtpOrderSubmissionEntry(
        sequence=sequence,
        account_identity_digest="1" * 64,
        policy_id="directional.stress90",
        policy_definition_digest="2" * 64,
        products_manifest_digest="3" * 64,
        target_trading_day=target_trading_day,
        daily_decision_digest="4" * 64,
        execution_intent_digest="5" * 64,
        transition={"freeze_authorized_lots": {}, "transitions": []},
        front_id=7,
        session_id=11,
        order_ref=order_ref,
        order_id=f"CTP.7_11_{order_ref}",
        request=request,
        status=status,
        authorization_kind=authorization_kind,
        filled_volume=0,
        fill_keys=(),
    )


def _fill(
    order_id: str,
    trade_id: str,
    *,
    volume: int = 1,
    target_trading_day: str = "20260825",
) -> CtpOrderFillEvidence:
    return CtpOrderFillEvidence(
        key=f"{target_trading_day}:DCE:{trade_id}",
        order_id=order_id,
        symbol="A2701",
        exchange="DCE",
        side=OrderSide.BUY,
        offset=Offset.OPEN,
        volume=volume,
        price=1_000.0,
        timestamp=(
            f"{target_trading_day[:4]}-{target_trading_day[4:6]}-"
            f"{target_trading_day[6:]}T09:00:00.000000+08:00"
        ),
    )


def _child_prepare(path: str, started, result) -> None:
    journal = CtpOrderSubmissionJournal(path)
    started.set()
    try:
        prepared = journal.prepare(_entry(1))
    except Exception as exc:  # pragma: no cover - returned to the parent for assertion
        result.put(("error", type(exc).__name__, str(exc)))
    else:
        result.put(("ok", prepared.order_id))


def _seal_epoch(
    journal: CtpOrderSubmissionJournal,
    *,
    transaction_id: str = "a" * 64,
    source_account_identity_digest: str = "1" * 64,
    target_account_identity_digest: str = "1" * 64,
):
    return journal.seal_epoch(
        transaction_id=transaction_id,
        source_account_identity_digest=source_account_identity_digest,
        target_account_identity_digest=target_account_identity_digest,
        trading_day="20260825",
        operator_reason="bounded Stress-90 order journal rollover",
        halted=True,
        broker_flat=True,
        local_flat=True,
        no_active_orders=True,
        reconciled=True,
        strong_confirmation=CTP_ORDER_JOURNAL_EPOCH_CONFIRMATION,
    )


def test_safe_epoch_seal_preserves_full_audit_and_blocks_cross_epoch_order_reuse(
    tmp_path: Path,
) -> None:
    path = tmp_path / "orders.json"
    journal = CtpOrderSubmissionJournal(path)
    first = journal.prepare(_entry(1))
    journal.update_status(first.order_id, "aborted_before_send")
    assert journal.compact_terminal() == 1

    manifest = _seal_epoch(journal)

    assert manifest.current_account_identity_digest == "1" * 64
    assert manifest.pending_cleanup_epoch_id == ""
    assert len(manifest.sealed_epochs) == 1
    assert journal.load() is None
    audit = journal.audit_epochs()
    assert audit.sealed_epoch_count == 1
    assert audit.sealed_entry_count == 1
    assert audit.current_record is None
    with pytest.raises(CtpOrderJournalIntegrityError, match="already reserved"):
        journal.prepare(_entry(1))
    assert journal.prepare(_entry(2)).order_id == _entry(2).order_id


def test_safe_epoch_seal_preserves_fill_identity_and_releases_current_capacity(
    tmp_path: Path,
) -> None:
    journal = CtpOrderSubmissionJournal(tmp_path / "orders.json")
    first_template = _entry(1)
    first = journal.prepare(
        replace(first_template, request=replace(first_template.request, volume=1)),
        fill_identity_capacity=1,
    )
    old_fill = _fill(first.order_id, "CTP.OLD-FILL")
    journal.apply_updates(
        statuses={first.order_id: "terminal"},
        fills={first.order_id: (old_fill,)},
    )
    journal.compact_terminal()

    manifest = _seal_epoch(journal)

    epoch = manifest.sealed_epochs[0]
    assert epoch.fill_identity_count == 1
    assert journal.audit_epochs().sealed_fill_identity_count == 1
    assert journal.load_runtime(account_identity_digest="1" * 64) is None
    assert journal.contains_runtime_sealed_fill_identity(
        account_identity_digest="1" * 64,
        fill_identity=old_fill.key,
    )
    assert not journal.contains_runtime_sealed_fill_identity(
        account_identity_digest="6" * 64,
        fill_identity=old_fill.key,
    )
    second_template = _entry(2)
    second = journal.prepare(
        replace(second_template, request=replace(second_template.request, volume=1)),
        fill_identity_capacity=1,
        observed_fill_identity_count=0,
    )
    duplicate = replace(old_fill, order_id=second.order_id)
    with pytest.raises(CtpOrderJournalIntegrityError, match="sealed epoch"):
        journal.apply_updates(fills={second.order_id: (duplicate,)})


def test_account_switch_epoch_binds_new_identity_and_rejects_incomplete_cleanup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "orders.json"
    journal = CtpOrderSubmissionJournal(path)
    first = journal.prepare(_entry(1))
    journal.update_status(first.order_id, "aborted_before_send")
    journal.compact_terminal()
    original = journal._unlink_epoch_source
    attempts = 0

    def crash_once(source: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected epoch cleanup crash")
        original(source)

    monkeypatch.setattr(journal, "_unlink_epoch_source", crash_once)
    with pytest.raises(OSError, match="cleanup crash"):
        _seal_epoch(
            journal,
            target_account_identity_digest="6" * 64,
        )
    with pytest.raises(CtpOrderJournalIntegrityError, match="cleanup is incomplete"):
        journal.load_runtime()

    monkeypatch.setattr(journal, "_unlink_epoch_source", original)
    manifest = _seal_epoch(
        journal,
        target_account_identity_digest="6" * 64,
    )
    assert manifest.pending_cleanup_epoch_id == ""
    old_identity = _entry(2)
    with pytest.raises(CtpOrderJournalIntegrityError, match="account identity"):
        journal.prepare(old_identity)
    new_identity = replace(_entry(1), account_identity_digest="6" * 64)
    assert journal.prepare(new_identity).account_identity_digest == "6" * 64


def test_epoch_retry_recovers_duplicate_manifest_after_pending_commit_crash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    journal = CtpOrderSubmissionJournal(tmp_path / "orders.json")
    first = journal.prepare(_entry(1))
    journal.update_status(first.order_id, "aborted_before_send")
    journal.compact_terminal()
    _seal_epoch(journal)
    second = journal.prepare(_entry(2))
    journal.update_status(second.order_id, "aborted_before_send")
    journal.compact_terminal()
    original_replace = journal._atomic_replace

    def crash_before_pending_current(target: Path, payload: bytes) -> None:
        if target == journal.epoch_manifest_path:
            raise OSError("injected pending manifest commit crash")
        original_replace(target, payload)

    monkeypatch.setattr(journal, "_atomic_replace", crash_before_pending_current)
    with pytest.raises(OSError, match="pending manifest commit crash"):
        _seal_epoch(journal, transaction_id="b" * 64)
    assert (
        journal.epoch_manifest_path.read_bytes()
        == journal.epoch_manifest_previous_path.read_bytes()
    )

    monkeypatch.setattr(journal, "_atomic_replace", original_replace)
    manifest = _seal_epoch(journal, transaction_id="b" * 64)
    assert manifest.pending_cleanup_epoch_id == ""
    assert journal.audit_epochs().sealed_epoch_count == 2


def test_epoch_retry_recovers_duplicate_manifest_after_cleanup_commit_crash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    journal = CtpOrderSubmissionJournal(tmp_path / "orders.json")
    first = journal.prepare(_entry(1))
    journal.update_status(first.order_id, "aborted_before_send")
    journal.compact_terminal()
    original_replace = journal._atomic_replace
    current_replaces = 0

    def crash_before_cleared_current(target: Path, payload: bytes) -> None:
        nonlocal current_replaces
        if target == journal.epoch_manifest_path:
            current_replaces += 1
            if current_replaces == 2:
                raise OSError("injected cleared manifest commit crash")
        original_replace(target, payload)

    monkeypatch.setattr(journal, "_atomic_replace", crash_before_cleared_current)
    with pytest.raises(OSError, match="cleared manifest commit crash"):
        _seal_epoch(journal)
    assert (
        journal.epoch_manifest_path.read_bytes()
        == journal.epoch_manifest_previous_path.read_bytes()
    )

    monkeypatch.setattr(journal, "_atomic_replace", original_replace)
    manifest = _seal_epoch(journal)
    assert manifest.pending_cleanup_epoch_id == ""
    assert journal.audit_epochs().sealed_epoch_count == 1


def test_completed_epoch_operation_id_cannot_seal_a_later_current_journal(
    tmp_path: Path,
) -> None:
    journal = CtpOrderSubmissionJournal(tmp_path / "orders.json")
    first = journal.prepare(_entry(1))
    journal.update_status(first.order_id, "aborted_before_send")
    journal.compact_terminal()
    completed = _seal_epoch(journal)
    second = journal.prepare(_entry(2))
    journal.update_status(second.order_id, "aborted_before_send")
    journal.compact_terminal()

    with pytest.raises(CtpOrderJournalIntegrityError, match="already consumed"):
        _seal_epoch(journal)

    assert journal.load_required().all_entries == (journal.get_entry(second.order_id),)
    assert journal.load_epoch_manifest() == completed
    next_manifest = _seal_epoch(journal, transaction_id="b" * 64)
    assert len(next_manifest.sealed_epochs) == 2


def test_same_logical_path_os_lock_blocks_another_process(tmp_path: Path) -> None:
    path = tmp_path / "orders.json"
    journal = CtpOrderSubmissionJournal(path)
    context = multiprocessing.get_context("spawn")
    started = context.Event()
    result = context.Queue()

    with journal._exclusive_lock():
        worker = context.Process(target=_child_prepare, args=(str(path), started, result))
        worker.start()
        assert started.wait(2.0)
        with pytest.raises(queue.Empty):
            result.get(timeout=0.2)

    assert result.get(timeout=2.0) == ("ok", "CTP.7_11_101")
    worker.join(timeout=2.0)
    assert worker.exitcode == 0


def test_unlinking_and_recreating_visible_lock_cannot_split_kernel_lock(
    tmp_path: Path,
) -> None:
    path = tmp_path / "orders.json"
    journal = CtpOrderSubmissionJournal(path)
    context = multiprocessing.get_context("spawn")
    started = context.Event()
    result = context.Queue()
    worker = None

    try:
        with journal._exclusive_lock():
            journal.lock_path.unlink()
            journal.lock_path.touch(mode=0o600)
            worker = context.Process(
                target=_child_prepare,
                args=(str(path), started, result),
            )
            worker.start()
            assert started.wait(2.0)
            with pytest.raises(queue.Empty):
                result.get(timeout=0.2)

        assert result.get(timeout=2.0) == ("ok", "CTP.7_11_101")
    finally:
        if worker is not None:
            worker.join(timeout=2.0)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=2.0)


def test_two_instances_keep_every_concurrent_prepare_under_lock(tmp_path: Path) -> None:
    path = tmp_path / "orders.json"
    first = CtpOrderSubmissionJournal(path)
    second = CtpOrderSubmissionJournal(path)
    barrier = threading.Barrier(2)
    failures: list[Exception] = []

    def prepare(journal: CtpOrderSubmissionJournal, entry: CtpOrderSubmissionEntry) -> None:
        try:
            barrier.wait(timeout=2.0)
            journal.prepare(entry)
        except Exception as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    workers = [
        threading.Thread(target=prepare, args=(first, _entry(1))),
        threading.Thread(target=prepare, args=(second, _entry(2))),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=2.0)

    assert failures == []
    record = first.load_required()
    assert [entry.sequence for entry in record.all_entries] == [1, 2]
    assert {entry.order_id for entry in record.all_entries} == {
        "CTP.7_11_101",
        "CTP.7_11_102",
    }


def test_apply_updates_batches_status_and_fills_into_one_revision(tmp_path: Path) -> None:
    journal = CtpOrderSubmissionJournal(tmp_path / "orders.json")
    first = journal.prepare(_entry(1))
    second = journal.prepare(_entry(2))
    before = journal.load_required().sequence

    updated = journal.apply_updates(
        statuses={first.order_id: "submitted", second.order_id: "terminal"},
        fills={
            first.order_id: (_fill(first.order_id, "CTP.T1", volume=2),),
            second.order_id: (_fill(second.order_id, "CTP.T2"),),
        },
    )

    record = journal.load_required()
    assert record.sequence == before + 1
    assert {entry.order_id for entry in updated} == {first.order_id, second.order_id}
    by_id = {entry.order_id: entry for entry in record.all_entries}
    assert by_id[first.order_id].status == "submitted"
    assert by_id[first.order_id].filled_volume == 2
    assert by_id[first.order_id].fill_keys == ("20260825:DCE:CTP.T1",)
    assert by_id[second.order_id].status == "terminal"
    assert by_id[second.order_id].filled_volume == 1


def test_apply_updates_is_atomic_when_one_fill_is_invalid(tmp_path: Path) -> None:
    journal = CtpOrderSubmissionJournal(tmp_path / "orders.json")
    first = journal.prepare(_entry(1))
    second = journal.prepare(_entry(2))
    before = journal.load_required()

    with pytest.raises(CtpOrderJournalIntegrityError, match="filled volume"):
        journal.apply_updates(
            statuses={first.order_id: "submitted"},
            fills={second.order_id: (_fill(second.order_id, "CTP.T2", volume=4),)},
        )

    after = journal.load_required()
    assert after.sequence == before.sequence
    assert after.checksum == before.checksum
    assert all(entry.status == "prepared" for entry in after.entries)


def test_fill_key_is_globally_unique_across_active_and_archived_entries(
    tmp_path: Path,
) -> None:
    journal = CtpOrderSubmissionJournal(tmp_path / "orders.json")
    first = journal.prepare(_entry(1))
    second = journal.prepare(_entry(2))
    journal.apply_updates(fills={first.order_id: (_fill(first.order_id, "CTP.T1"),)})
    journal.update_status(first.order_id, "terminal")
    assert journal.compact_terminal() == 1
    before = journal.load_required()

    with pytest.raises(CtpOrderJournalIntegrityError, match="duplicate.*fill"):
        journal.apply_updates(fills={second.order_id: (_fill(second.order_id, "CTP.T1"),)})

    assert journal.load_required().checksum == before.checksum


def test_late_fill_amends_archived_identity_with_new_immutable_segment(
    tmp_path: Path,
) -> None:
    path = tmp_path / "orders.json"
    journal = CtpOrderSubmissionJournal(path)
    first = journal.prepare(_entry(1))
    journal.update_status(first.order_id, "terminal")
    assert journal.compact_terminal() == 1
    original_segments = {
        item.name: item.read_bytes() for item in path.parent.glob(f"{path.name}.archive.*.json")
    }

    journal.apply_updates(
        fills={first.order_id: (_fill(first.order_id, "CTP.LATE", volume=2),)},
    )

    updated = journal.get_entry(first.order_id)
    assert updated is not None
    assert updated.filled_volume == 2
    assert updated.fill_keys == ("20260825:DCE:CTP.LATE",)
    segments = sorted(path.parent.glob(f"{path.name}.archive.*.json"))
    assert len(segments) == 2
    for name, payload in original_segments.items():
        assert (path.parent / name).read_bytes() == payload


def test_retrying_exact_immutable_segment_fsyncs_file_and_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "orders.json"
    journal = CtpOrderSubmissionJournal(path)
    first = journal.prepare(_entry(1))
    journal.update_status(first.order_id, "terminal")
    journal.compact_terminal()
    segment_path = next(path.parent.glob(f"{path.name}.archive.*.json"))
    payload = segment_path.read_bytes()
    real_fsync = os.fsync
    fsync_calls = 0

    def track_fsync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        real_fsync(descriptor)

    monkeypatch.setattr("afuture.broker.ctp_order_journal.os.fsync", track_fsync)

    journal._write_immutable(segment_path, payload)

    assert fsync_calls >= 2


def test_immutable_segment_retry_rejects_non_regular_namespace(
    tmp_path: Path,
) -> None:
    segment_path = tmp_path / "claimed-segment.json"
    segment_path.mkdir()

    with pytest.raises(CtpOrderJournalIntegrityError, match="immutable.*changed"):
        CtpOrderSubmissionJournal._write_immutable(segment_path, b"evidence")


def test_capacity_archives_terminal_identity_before_accepting_reduction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import afuture.broker.ctp_order_journal as journal_module

    monkeypatch.setattr(journal_module, "_MAX_ENTRIES", 2)
    journal = CtpOrderSubmissionJournal(tmp_path / "orders.json")
    first = journal.prepare(_entry(1))
    journal.update_status(first.order_id, "terminal")
    second = journal.prepare(_entry(2))

    reduction = journal.prepare(
        _entry(
            3,
            authorization_kind="risk_reduction",
            offset=Offset.CLOSE,
        )
    )

    record = journal.load_required()
    assert [entry.order_id for entry in record.entries] == [second.order_id, reduction.order_id]
    assert [entry.order_id for entry in record.archived_entries] == [first.order_id]
    assert [entry.sequence for entry in record.all_entries] == [1, 2, 3]
    assert journal.get_entry(first.order_id) == record.archived_entries[0]


def test_capacity_with_only_unresolved_entries_still_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import afuture.broker.ctp_order_journal as journal_module

    monkeypatch.setattr(journal_module, "_MAX_ENTRIES", 2)
    journal = CtpOrderSubmissionJournal(tmp_path / "orders.json")
    journal.prepare(_entry(1))
    journal.prepare(_entry(2))

    with pytest.raises(CtpOrderJournalIntegrityError, match="entry limit"):
        journal.prepare(
            _entry(
                3,
                authorization_kind="risk_reduction",
                offset=Offset.CLOSE,
            )
        )


def test_terminal_history_moves_to_immutable_bounded_archive_segments(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import afuture.broker.ctp_order_journal as journal_module

    monkeypatch.setattr(journal_module, "_MAX_ENTRIES", 2)
    monkeypatch.setattr(
        journal_module,
        "_ARCHIVE_SEGMENT_MAX_ENTRIES",
        1,
        raising=False,
    )
    path = tmp_path / "orders.json"
    journal = CtpOrderSubmissionJournal(path)

    for index in range(1, 5):
        entry = journal.prepare(_entry(index))
        journal.update_status(entry.order_id, "terminal")
        assert journal.compact_terminal() == 1

    first_generation = journal.load_required()
    assert [entry.sequence for entry in first_generation.archived_entries] == [1, 2, 3, 4]
    segment_paths = sorted(path.parent.glob(f"{path.name}.archive.*.json"))
    assert len(segment_paths) == 4
    immutable_segments = {
        segment_path.name: (segment_path.stat().st_ino, segment_path.read_bytes())
        for segment_path in segment_paths
    }
    current = json.loads(path.read_text(encoding="utf-8"))
    assert "archived_entries" not in current
    assert current["current"]["entries"] == []
    assert current["current"]["archive_head"]["segment_sequence"] == 4
    assert first_generation.archived_entries[0].order_id.encode() not in path.read_bytes()
    for segment_path in segment_paths:
        segment = json.loads(segment_path.read_text(encoding="utf-8"))
        assert 1 <= len(segment["entries"]) <= 1

    fifth = journal.prepare(_entry(5))
    journal.update_status(fifth.order_id, "terminal")
    assert journal.compact_terminal() == 1

    assert [entry.sequence for entry in journal.load_all_entries()] == [1, 2, 3, 4, 5]
    assert journal.get_entry(first_generation.archived_entries[0].order_id) is not None
    for name, (inode, payload) in immutable_segments.items():
        unchanged = path.parent / name
        assert unchanged.stat().st_ino == inode
        assert unchanged.read_bytes() == payload


def test_runtime_load_and_steady_mutations_never_scan_lifetime_archive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import afuture.broker.ctp_order_journal as journal_module

    monkeypatch.setattr(journal_module, "_MAX_ENTRIES", 2)
    monkeypatch.setattr(journal_module, "_ARCHIVE_SEGMENT_MAX_ENTRIES", 1)
    monkeypatch.setattr(journal_module, "_RUNTIME_RECENT_DAYS", 2)
    monkeypatch.setattr(journal_module, "_RUNTIME_RECENT_MAX_ENTRIES", 2)
    journal = CtpOrderSubmissionJournal(tmp_path / "orders.json")
    for index in range(1, 21):
        entry = journal.prepare(_entry(index, target_trading_day=f"202608{index:02d}"))
        journal.update_status(entry.order_id, "terminal")
        assert journal.compact_terminal() == 1

    assert len(journal.load_required().archived_entries) == 20
    original_full_loader = journal._load_archive
    monkeypatch.setattr(
        journal,
        "_load_archive",
        lambda _head: (_ for _ in ()).throw(AssertionError("cold archive scan")),
    )

    runtime = journal.load_runtime()
    assert runtime is not None
    assert runtime.archive_complete is False
    assert len(runtime.archived_entries) == 2
    prepared = journal.prepare(
        _entry(
            21,
            authorization_kind="risk_reduction",
            offset=Offset.CLOSE,
            target_trading_day="20260821",
        )
    )
    assert prepared.sequence == 21
    journal.update_status(prepared.order_id, "submitted")
    journal.update_status(prepared.order_id, "terminal")
    assert journal.compact_terminal() == 1

    monkeypatch.setattr(journal, "_load_archive", original_full_loader)
    assert len(journal.load_required().archived_entries) == 21


def test_cold_exact_identity_and_bloom_false_positive_both_fail_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import afuture.broker.ctp_order_journal as journal_module

    monkeypatch.setattr(journal_module, "_RUNTIME_RECENT_DAYS", 1)
    monkeypatch.setattr(journal_module, "_RUNTIME_RECENT_MAX_ENTRIES", 1)
    journal = CtpOrderSubmissionJournal(tmp_path / "orders.json")
    first = journal.prepare(_entry(1, target_trading_day="20260823"))
    journal.update_status(first.order_id, "terminal")
    journal.compact_terminal()
    second = journal.prepare(_entry(2, target_trading_day="20260824"))
    journal.update_status(second.order_id, "terminal")
    journal.compact_terminal()
    runtime = journal.load_runtime()
    assert runtime is not None
    assert [entry.order_id for entry in runtime.archived_entries] == [second.order_id]

    with pytest.raises(CtpOrderJournalIntegrityError, match="already reserved"):
        journal.prepare(_entry(1, target_trading_day="20260825"))

    original_contains = journal_module._order_identity_bloom_contains
    false_positive_id = _entry(3).order_id
    monkeypatch.setattr(
        journal_module,
        "_order_identity_bloom_contains",
        lambda bloom, order_id: (
            True if order_id == false_positive_id else original_contains(bloom, order_id)
        ),
    )
    with pytest.raises(CtpOrderJournalIntegrityError, match="already reserved"):
        journal.prepare(_entry(3, target_trading_day="20260825"))


def test_rechecksummed_runtime_index_cannot_drop_cold_bloom_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import afuture.broker.ctp_order_journal as journal_module

    monkeypatch.setattr(journal_module, "_RUNTIME_RECENT_DAYS", 1)
    monkeypatch.setattr(journal_module, "_RUNTIME_RECENT_MAX_ENTRIES", 1)
    path = tmp_path / "orders.json"
    journal = CtpOrderSubmissionJournal(path)
    first = journal.prepare(_entry(1, target_trading_day="20260823"))
    journal.update_status(first.order_id, "terminal")
    journal.compact_terminal()
    second = journal.prepare(_entry(2, target_trading_day="20260824"))
    journal.update_status(second.order_id, "terminal")
    journal.compact_terminal()
    # Advance once so both embedded generations retain only the second identity
    # as exact recent evidence; the first identity is protected only by Bloom bits.
    journal.prepare(_entry(3, target_trading_day="20260825"))

    envelope = json.loads(path.read_text(encoding="utf-8"))
    pointer = envelope["current"]["runtime_index"]
    index_path = path.parent / pointer["filename"]
    runtime_index = json.loads(index_path.read_text(encoding="utf-8"))
    bloom = bytearray(b64decode(runtime_index["order_identity_bloom"]))
    first_positions = set(journal_module._order_identity_bloom_positions(first.order_id))
    second_positions = set(journal_module._order_identity_bloom_positions(second.order_id))
    cold_only = next(position for position in first_positions if position not in second_positions)
    bloom[cold_only // 8] &= ~(1 << (cold_only % 8))
    runtime_index["order_identity_bloom"] = b64encode(bytes(bloom)).decode("ascii")
    index_unsigned = {key: value for key, value in runtime_index.items() if key != "checksum"}
    index_checksum = hashlib.sha256(
        json.dumps(
            index_unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    runtime_index["checksum"] = index_checksum
    index_name = journal._runtime_index_name(index_checksum)
    (path.parent / index_name).write_text(
        json.dumps(runtime_index, sort_keys=True),
        encoding="utf-8",
    )
    envelope["current"]["runtime_index"] = {
        "filename": index_name,
        "checksum": index_checksum,
    }
    generation_unsigned = {
        key: value for key, value in envelope["current"].items() if key != "checksum"
    }
    envelope["current"]["checksum"] = hashlib.sha256(
        json.dumps(
            generation_unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    envelope_unsigned = {key: value for key, value in envelope.items() if key != "checksum"}
    envelope["checksum"] = hashlib.sha256(
        json.dumps(
            envelope_unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    path.write_text(json.dumps(envelope, sort_keys=True), encoding="utf-8")

    with pytest.raises(CtpOrderJournalIntegrityError, match="Bloom evidence regressed"):
        journal.load_runtime()


def test_old_unresolved_hot_order_can_fill_and_compact_after_newer_archive_days(
    tmp_path: Path,
) -> None:
    journal = CtpOrderSubmissionJournal(tmp_path / "orders.json")
    old = journal.prepare(_entry(1, target_trading_day="20260823"))
    journal.update_status(old.order_id, "submitted")
    for sequence, day in ((2, "20260824"), (3, "20260825")):
        newer = journal.prepare(_entry(sequence, target_trading_day=day))
        journal.update_status(newer.order_id, "terminal")
        assert journal.compact_terminal() == 1

    journal.apply_updates(
        fills={
            old.order_id: (
                _fill(
                    old.order_id,
                    "CTP.OLD-LATE",
                    target_trading_day="20260823",
                ),
            )
        }
    )
    journal.update_status(old.order_id, "terminal")
    assert journal.compact_terminal() == 1

    runtime = journal.load_runtime()
    assert runtime is not None
    recovered = next(entry for entry in runtime.archived_entries if entry.order_id == old.order_id)
    assert recovered.fill_keys == ("20260823:DCE:CTP.OLD-LATE",)
    assert recovered.filled_volume == 1


def test_order_identity_bloom_capacity_fails_closed_without_runtime_reset(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import afuture.broker.ctp_order_journal as journal_module

    monkeypatch.setattr(journal_module, "_ORDER_ID_BLOOM_MAX_ITEMS", 1)
    journal = CtpOrderSubmissionJournal(tmp_path / "orders.json")
    first = journal.prepare(_entry(1))
    journal.update_status(first.order_id, "terminal")
    journal.compact_terminal()

    with pytest.raises(
        CtpOrderJournalIntegrityError,
        match="HALTED epoch rollover is required",
    ):
        journal.prepare(_entry(2))

    runtime = journal.load_runtime()
    assert runtime is not None
    assert runtime.archive_entry_count == 1
    assert runtime.entries == ()


def test_prepare_rejects_total_identity_overflow_and_exact_cap_epoch_can_seal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import afuture.broker.ctp_order_journal as journal_module

    monkeypatch.setattr(journal_module, "_MAX_ENTRIES", 2)
    monkeypatch.setattr(journal_module, "_ORDER_ID_BLOOM_MAX_ITEMS", 2)
    journal = CtpOrderSubmissionJournal(tmp_path / "orders.json")
    first = journal.prepare(_entry(1))
    journal.update_status(first.order_id, "terminal")
    journal.compact_terminal()
    second = journal.prepare(_entry(2))
    journal.update_status(second.order_id, "terminal")

    with pytest.raises(
        CtpOrderJournalIntegrityError,
        match="HALTED epoch rollover is required",
    ):
        journal.prepare(_entry(3))

    manifest = _seal_epoch(journal)

    assert manifest.sealed_epochs[0].entry_count == 2
    audit = journal.audit_epochs()
    assert audit.sealed_entry_count == 2
    with pytest.raises(CtpOrderJournalIntegrityError, match="sealed epoch"):
        journal.prepare(_entry(2))


def test_crash_between_previous_evidence_and_current_replace_keeps_last_commit_loadable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "orders.json"
    journal = CtpOrderSubmissionJournal(path)
    first = journal.prepare(_entry(1))
    committed = journal.load_required()
    original_replace = journal._atomic_replace

    def crash_before_current(target: Path, payload: bytes) -> None:
        if target == journal.path:
            raise OSError("injected crash before current generation replace")
        original_replace(target, payload)

    monkeypatch.setattr(journal, "_atomic_replace", crash_before_current)
    with pytest.raises(OSError, match="current generation replace"):
        journal.prepare(_entry(2))
    monkeypatch.setattr(journal, "_atomic_replace", original_replace)

    recovered = journal.load_required()
    assert recovered.sequence == committed.sequence
    assert recovered.checksum == committed.checksum
    assert recovered.all_entries == (first,)


def test_noncurrent_schema_three_envelope_is_rejected(
    tmp_path: Path,
) -> None:
    path = tmp_path / "orders.json"
    journal = CtpOrderSubmissionJournal(path)
    journal.prepare(_entry(1))
    current = json.loads(path.read_text(encoding="utf-8"))["current"]
    noncurrent_unsigned = {
        "kind": "afuture.ctp.order-submission-journal",
        "schema_version": 3,
        "sequence": current["sequence"],
        "parent_checksum": current["parent_checksum"],
        "entries": current["entries"],
        "archived_entries": [],
    }
    noncurrent = {
        **noncurrent_unsigned,
        "checksum": hashlib.sha256(
            json.dumps(
                noncurrent_unsigned,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest(),
    }
    path.write_text(json.dumps(noncurrent, sort_keys=True), encoding="utf-8")

    with pytest.raises(CtpOrderJournalIntegrityError, match="schema"):
        journal.load_required()


def test_missing_current_cannot_reinitialize_over_archive_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "orders.json"
    journal = CtpOrderSubmissionJournal(path)
    prepared = journal.prepare(_entry(1))
    journal.update_status(prepared.order_id, "terminal")
    journal.compact_terminal()
    path.unlink()
    journal.previous_path.unlink()

    with pytest.raises(CtpOrderJournalIntegrityError, match="current.*missing"):
        journal.load()
    with pytest.raises(CtpOrderJournalIntegrityError, match="current.*missing"):
        journal.prepare(_entry(2))


def test_aborted_before_send_is_durable_terminal_like_and_cannot_regress(
    tmp_path: Path,
) -> None:
    journal = CtpOrderSubmissionJournal(tmp_path / "orders.json")
    prepared = journal.prepare(_entry(1))

    journal.update_status(prepared.order_id, "aborted_before_send")
    aborted = journal.get_entry(prepared.order_id)
    assert aborted is not None
    assert aborted.status == "aborted_before_send"
    with pytest.raises(CtpOrderJournalIntegrityError, match="status transition"):
        journal.update_status(prepared.order_id, "submitted")

    assert journal.compact_terminal() == 1
    archived = journal.load_required()
    assert archived.entries == ()
    assert archived.archived_entries[0].status == "aborted_before_send"


def test_prepare_rejects_prepopulated_status_or_fill_evidence(tmp_path: Path) -> None:
    journal = CtpOrderSubmissionJournal(tmp_path / "orders.json")

    with pytest.raises(CtpOrderJournalIntegrityError, match="prepared entry"):
        journal.prepare(_entry(1, status="terminal"))
    prefilled = replace(
        _entry(2),
        filled_volume=1,
        fill_keys=("20260825:DCE:CTP.PREEXISTING",),
        fill_evidence=(_fill("CTP.7_11_102", "CTP.PREEXISTING"),),
    )
    with pytest.raises(CtpOrderJournalIntegrityError, match="prepared entry"):
        journal.prepare(prefilled)


def test_corrupt_current_archive_fails_closed_without_prev_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import afuture.broker.ctp_order_journal as journal_module

    monkeypatch.setattr(journal_module, "_MAX_ENTRIES", 1)
    path = tmp_path / "orders.json"
    journal = CtpOrderSubmissionJournal(path)
    first = journal.prepare(_entry(1))
    journal.update_status(first.order_id, "terminal")
    journal.prepare(
        _entry(
            2,
            authorization_kind="risk_reduction",
            offset=Offset.CLOSE,
        )
    )
    assert journal.previous_path.exists()
    raw = json.loads(path.read_text(encoding="utf-8"))
    archive_path = path.parent / raw["current"]["archive_head"]["filename"]
    archive = json.loads(archive_path.read_text(encoding="utf-8"))
    archive["entries"][0]["request"]["symbol"] = "CORRUPT"
    archive_path.write_text(json.dumps(archive), encoding="utf-8")

    with pytest.raises(CtpOrderJournalIntegrityError, match="archive checksum"):
        journal.load_required()


def test_rechecksummed_archive_must_remain_terminal_and_globally_unique(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import afuture.broker.ctp_order_journal as journal_module

    monkeypatch.setattr(journal_module, "_MAX_ENTRIES", 1)
    path = tmp_path / "orders.json"
    journal = CtpOrderSubmissionJournal(path)
    first = journal.prepare(_entry(1))
    journal.update_status(first.order_id, "terminal")
    journal.prepare(
        _entry(
            2,
            authorization_kind="risk_reduction",
            offset=Offset.CLOSE,
        )
    )

    current = json.loads(path.read_text(encoding="utf-8"))
    old_head = current["current"]["archive_head"]
    archive_path = path.parent / old_head["filename"]
    archive = json.loads(archive_path.read_text(encoding="utf-8"))
    archive["entries"][0]["status"] = "submitted"
    unsigned_archive = {key: value for key, value in archive.items() if key != "checksum"}
    archive_checksum = hashlib.sha256(
        json.dumps(
            unsigned_archive,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    archive["checksum"] = archive_checksum
    archive_name = (
        f"{path.name}.archive.{old_head['segment_sequence']:020d}.{archive_checksum}.json"
    )
    (path.parent / archive_name).write_text(
        json.dumps(archive, sort_keys=True),
        encoding="utf-8",
    )
    current["current"]["archive_head"] = {
        **old_head,
        "filename": archive_name,
        "checksum": archive_checksum,
    }
    generation_unsigned = {
        key: value for key, value in current["current"].items() if key != "checksum"
    }
    current["current"]["checksum"] = hashlib.sha256(
        json.dumps(
            generation_unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    envelope_unsigned = {key: value for key, value in current.items() if key != "checksum"}
    current["checksum"] = hashlib.sha256(
        json.dumps(
            envelope_unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    path.write_text(json.dumps(current, sort_keys=True), encoding="utf-8")

    with pytest.raises(CtpOrderJournalIntegrityError, match="archived.*terminal-like"):
        journal.load_required()
