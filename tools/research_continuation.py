"""Run evidence-directed offline experiments and preserve recoverable attempts."""

from __future__ import annotations

import hashlib
import json
import os
import traceback
from pathlib import Path


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode()


def _read(path):
    def invalid_constant(value):
        raise ValueError(f"non-finite JSON value: {value}")

    return json.loads(path.read_text(encoding="utf-8"), parse_constant=invalid_constant)


def _write(path, value):
    payload = _json_bytes(value)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_context(context):
    for group in ("sources", "inputs"):
        if not isinstance(context.get(group), dict) or not context[group]:
            raise ValueError(f"context requires nonempty {group} path-to-SHA256 mapping")
        for filename, expected in context[group].items():
            path = Path(filename)
            if not path.is_absolute() or not path.is_file() or _digest(path) != expected:
                raise ValueError(f"changed or missing {group} evidence: {filename}")


def _manifest(folder):
    result = {}
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"attempt evidence cannot be a symlink: {path}")
        if path.is_file() and path != folder / "complete.json":
            result[path.relative_to(folder).as_posix()] = {
                "bytes": path.stat().st_size,
                "sha256": _digest(path),
            }
    return result


def execute_chain(output, select_next, execute, validate, *, context):
    """Execute until the caller needs a new hypothesis, never declare the goal met.

    ``select_next(history)`` returns a JSON specification or None. ``execute(spec,
    attempt_dir)`` writes its original outputs. ``validate(spec, attempt_dir)``
    verifies the caller's complete account matrix and returns finite JSON containing
    the boolean ``economic_passed`` plus its diagnosis. The caller owns economic
    gates; this driver neither scores strategies nor relaxes those gates.

    ``context`` freezes nonempty ``sources`` and ``inputs`` absolute-path-to-SHA256
    mappings plus any other metadata. Only one writer may own an output directory.
    The immutable attempt receipts are the trial log; state atomically appends their
    identities. An interrupted attempt is preserved and may be retried in a new
    directory. A completed specification cannot be accidentally executed twice.
    """
    output = Path(output)
    context_bytes = _json_bytes(context)
    context_hash = hashlib.sha256(context_bytes).hexdigest()
    _verify_context(context)
    output.mkdir(parents=True, exist_ok=True)
    context_path = output / "context.json"
    if context_path.exists():
        if context_path.read_bytes() != context_bytes:
            raise ValueError("frozen context changed; use a new campaign directory")
    else:
        if any(output.iterdir()):
            raise ValueError("nonempty campaign lacks its frozen context")
        _write(context_path, context)
    attempts = output / "attempts"
    attempts.mkdir(exist_ok=True)
    state_path = output / "state.json"
    prior_state = _read(state_path) if state_path.exists() else {}
    if prior_state and prior_state.get("context_sha256") != context_hash:
        raise ValueError("state context identity changed")
    recorded = prior_state.get("receipts", {})
    history, receipts = [], {}

    def save_state(status):
        state = {
            "status": status,
            "goal_achieved": False,
            "context_sha256": context_hash,
            "receipts": dict(receipts),
        }
        _write(state_path, state)
        return {**state, "history": list(history)}

    def finish(folder, frozen, status, result):
        record = {
            **frozen,
            "attempt": folder.name,
            "status": status,
            "result": result,
            "manifest": _manifest(folder),
        }
        _write(folder / "complete.json", record)
        return record

    def checked_result(spec, folder):
        result = validate(json.loads(_json_bytes(spec)), folder)
        _json_bytes(result)
        if not isinstance(result, dict) or type(result.get("economic_passed")) is not bool:
            raise ValueError("validator must return finite JSON with boolean economic_passed")
        status = "candidate_passed" if result["economic_passed"] else "economic_failed"
        return status, result

    for number, folder in enumerate(sorted(attempts.iterdir()), start=1):
        if not folder.is_dir() or folder.is_symlink():
            raise ValueError(f"unexpected attempt entry: {folder}")
        frozen = _read(folder / "spec.json")
        spec_hash = hashlib.sha256(_json_bytes(frozen["spec"])).hexdigest()
        if (
            frozen
            != {
                "context_sha256": context_hash,
                "spec_sha256": spec_hash,
                "spec": frozen["spec"],
            }
            or folder.name != f"{number:06d}-{spec_hash}"
        ):
            raise ValueError(f"attempt specification identity changed: {folder}")
        completion = folder / "complete.json"
        if not completion.exists():
            if folder.name in recorded:
                raise ValueError(f"completed receipt missing: {folder}")
            temporary_receipt = folder / "complete.json.tmp"
            if temporary_receipt.exists():
                preserved = folder / "interrupted-completion.json"
                suffix = 1
                while preserved.exists():
                    preserved = folder / f"interrupted-completion-{suffix}.json"
                    suffix += 1
                temporary_receipt.rename(preserved)
            try:
                status, result = checked_result(frozen["spec"], folder)
                result = {**result, "recovered_without_receipt": True}
            except Exception as exc:
                status = "invalid_evidence"
                result = {
                    "stage": "interrupted",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
            record = finish(folder, frozen, status, result)
        else:
            record = _read(completion)
            if any(record.get(key) != value for key, value in frozen.items()):
                raise ValueError(f"completed identity changed: {folder}")
            if record.get("attempt") != folder.name or record.get("manifest") != _manifest(folder):
                raise ValueError(f"completed evidence changed: {folder}")
        receipt_hash = _digest(completion)
        if folder.name in recorded and recorded[folder.name] != receipt_hash:
            raise ValueError(f"completed receipt changed: {folder}")
        history.append(record)
        receipts[folder.name] = receipt_hash
    if set(recorded) - set(receipts):
        raise ValueError("recorded attempt evidence missing")
    save_state("running")

    while True:
        # Give the selector a detached snapshot so it cannot mutate frozen evidence.
        spec = select_next(json.loads(_json_bytes(history)))
        if spec is None:
            return save_state("needs_new_hypothesis")
        if not isinstance(spec, dict) or not spec:
            raise ValueError("selector must return a nonempty JSON specification or None")
        spec_hash = hashlib.sha256(_json_bytes(spec)).hexdigest()
        if any(
            item["spec_sha256"] == spec_hash
            and not (
                item["status"] == "invalid_evidence"
                and item["result"].get("stage") == "interrupted"
            )
            for item in history
        ):
            raise ValueError("selector repeated an executed specification; choose a new hypothesis")
        _verify_context(context)
        folder = attempts / f"{len(history) + 1:06d}-{spec_hash}"
        folder.mkdir()
        frozen = {"context_sha256": context_hash, "spec_sha256": spec_hash, "spec": spec}
        _write(folder / "spec.json", frozen)
        stage = "execute"
        try:
            execute(json.loads(_json_bytes(spec)), folder)
            stage = "input_validation"
            _verify_context(context)
            stage = "validate"
            status, result = checked_result(spec, folder)
        except Exception as exc:
            status = "invalid_evidence"
            result = {
                "stage": stage,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
            _write(folder / "error.json", result)
        record = finish(folder, frozen, status, result)
        history.append(record)
        receipts[folder.name] = _digest(folder / "complete.json")
        save_state("running")
