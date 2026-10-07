"""Continuation runs real file-producing experiments and checks recovery identities."""

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import research_continuation as continuation


@pytest.fixture
def context(tmp_path):
    result = {}
    for kind in ("sources", "inputs"):
        path = tmp_path / f"{kind}.txt"
        path.write_text(kind)
        result[kind] = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()}
    return result


def execute(spec, folder):
    # The tiny executor actually generates independent evidence for all eight cells.
    for window in ("historical", "recent"):
        for pool in ("full", "exAG"):
            for cost in ("base", "stress"):
                (folder / f"{window}_{pool}_{cost}.json").write_text(
                    json.dumps({"cash": 500000 + spec["net"], "fee": 10})
                )


def validate(spec, folder):
    rows = [json.loads(path.read_text()) for path in folder.glob("*_*_*.json")]
    if len(rows) != 8 or any(row["fee"] != 10 for row in rows):
        raise ValueError("incomplete or unreconciled eight-cell account evidence")
    return {"economic_passed": all(row["cash"] > 500000 for row in rows), "cells": 8}


def test_economic_failure_executes_next_rule_without_second_invocation(tmp_path, context):
    def choose(history):
        if not history:
            return {"rule": "original", "net": -20}
        if len(history) == 1:
            assert history[0]["status"] == "economic_failed"
            return {"rule": "lower_turnover", "net": 20}
        return None

    result = continuation.execute_chain(
        tmp_path / "run", choose, execute, validate, context=context
    )
    assert [row["status"] for row in result["history"]] == [
        "economic_failed",
        "candidate_passed",
    ]
    assert all(len(row["manifest"]) == 9 for row in result["history"])
    assert result["status"] == "needs_new_hypothesis" and not result["goal_achieved"]


@pytest.mark.parametrize("stage", ["execute", "validate"])
def test_invalid_financial_evidence_routes_to_independent_experiment(tmp_path, context, stage):
    def broken_first(spec, folder):
        execute(spec, folder)
        if spec["rule"] == "broken":
            (folder / "recent_full_base.json").unlink()
            if stage == "execute":
                raise ValueError("execution could not complete the required account matrix")

    def choose(history):
        if not history:
            return {"rule": "broken", "net": 100}
        if len(history) == 1:
            assert history[0]["status"] == "invalid_evidence"
            assert history[0]["result"]["stage"] == stage
            return {"rule": "independent", "net": -10}
        return None

    result = continuation.execute_chain(
        tmp_path / "run", choose, broken_first, validate, context=context
    )
    assert [row["status"] for row in result["history"]] == [
        "invalid_evidence",
        "economic_failed",
    ]
    assert "error.json" in result["history"][0]["manifest"]


def test_receipt_before_state_crash_is_recovered_without_reexecution(
    tmp_path, context, monkeypatch
):
    output = tmp_path / "run"
    original = continuation._write

    def crash(path, value):
        if path.name == "state.json" and value["receipts"]:
            raise KeyboardInterrupt("crash after completed receipt")
        return original(path, value)

    monkeypatch.setattr(continuation, "_write", crash)

    def choose(history):
        return None if history else {"net": -10}

    with pytest.raises(KeyboardInterrupt):
        continuation.execute_chain(output, choose, execute, validate, context=context)
    monkeypatch.setattr(continuation, "_write", original)

    def cannot_run(*args):
        pytest.fail("a verified completed experiment must not execute again")

    result = continuation.execute_chain(output, choose, cannot_run, validate, context=context)
    assert len(result["history"]) == 1
    assert result["history"][0]["status"] == "economic_failed"


def test_complete_accounts_before_receipt_crash_are_validated_without_reexecution(
    tmp_path, context
):
    output = tmp_path / "run"

    def interrupted(spec, folder):
        execute(spec, folder)
        raise KeyboardInterrupt("all account originals saved before validation receipt")

    def choose(history):
        return None if history else {"net": -10}

    with pytest.raises(KeyboardInterrupt):
        continuation.execute_chain(output, choose, interrupted, validate, context=context)

    def cannot_run(*args):
        pytest.fail("complete account originals must be validated and reused")

    result = continuation.execute_chain(output, choose, cannot_run, validate, context=context)
    assert result["history"][0]["result"]["recovered_without_receipt"]
    assert result["history"][0]["status"] == "economic_failed"


def test_partial_attempt_is_preserved_and_retried_in_new_directory(tmp_path, context):
    output = tmp_path / "run"

    def interrupted(spec, folder):
        (folder / "partial.csv").write_text("original partial bytes\n")
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        continuation.execute_chain(
            output, lambda history: {"net": -10}, interrupted, validate, context=context
        )

    result = continuation.execute_chain(
        output,
        lambda history: {"net": -10} if len(history) == 1 else None,
        execute,
        validate,
        context=context,
    )
    assert [row["status"] for row in result["history"]] == [
        "invalid_evidence",
        "economic_failed",
    ]
    first = output / "attempts" / result["history"][0]["attempt"]
    assert (first / "partial.csv").read_text() == "original partial bytes\n"


def test_partial_receipt_bytes_are_preserved_when_attempt_recovers(tmp_path, context):
    output = tmp_path / "run"

    def interrupted(spec, folder):
        (folder / "complete.json.tmp").write_text('{"partial":')
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        continuation.execute_chain(
            output, lambda history: {"net": -10}, interrupted, validate, context=context
        )
    result = continuation.execute_chain(
        output, lambda history: None, execute, validate, context=context
    )
    folder = output / "attempts" / result["history"][0]["attempt"]
    assert (folder / "interrupted-completion.json").read_text() == '{"partial":'
    again = continuation.execute_chain(
        output, lambda history: None, execute, validate, context=context
    )
    assert again == result


@pytest.mark.parametrize("changed", ["inputs", "sources", "output", "receipt"])
def test_changed_evidence_is_never_reused(tmp_path, context, changed):
    output = tmp_path / "run"
    result = continuation.execute_chain(
        output,
        lambda history: None if history else {"net": -10},
        execute,
        validate,
        context=context,
    )
    folder = output / "attempts" / result["history"][0]["attempt"]
    if changed in ("inputs", "sources"):
        Path(next(iter(context[changed]))).write_text("changed")
    elif changed == "output":
        (folder / "recent_full_base.json").write_text('{"cash": 999999, "fee": 10}')
    else:
        record = json.loads((folder / "complete.json").read_text())
        record["status"] = "candidate_passed"
        (folder / "complete.json").write_text(json.dumps(record))
    with pytest.raises(ValueError, match="changed"):
        continuation.execute_chain(output, lambda history: None, execute, validate, context=context)


def test_nan_validation_is_invalid_and_empty_frontier_never_means_success(tmp_path, context):
    result = continuation.execute_chain(
        tmp_path / "run",
        lambda history: None if history else {"net": -10},
        execute,
        lambda spec, folder: {"economic_passed": True, "profit": float("nan")},
        context=context,
    )
    assert result["history"][0]["status"] == "invalid_evidence"
    assert not result["goal_achieved"] and result["status"] == "needs_new_hypothesis"


def test_updated_hashes_cannot_replace_frozen_context(tmp_path, context):
    output = tmp_path / "run"
    continuation.execute_chain(output, lambda history: None, execute, validate, context=context)
    path = Path(next(iter(context["inputs"])))
    path.write_text("new bytes")
    context["inputs"][str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="frozen context changed"):
        continuation.execute_chain(output, lambda history: None, execute, validate, context=context)
