import hashlib
from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest


def _result(scenario, window, annualized):
    return {
        "scenario": scenario,
        "window": window,
        "stats": {
            "annualized_return": annualized,
            "max_drawdown": -0.20,
            "halted": False,
            "max_realized_gross_notional_ratio": 1.8,
            "margin_reject_days": 0,
        },
        "economics": {
            "net_alpha": 1_000_000.0,
            "net_alpha_per_turnover_bps": 40.0,
        },
    }


def _payload(scenario, window, annualized):
    from tools.evaluate_directional_stress90_final import (
        EXPECTED_CANDIDATE_WEIGHT_SHA256,
        EXPECTED_CONSTRAINTS,
    )

    return {
        "role": "final fixed Stress90 Production evidence",
        "parameter_search": False,
        "production_wiring": False,
        "candidate": {
            "candidate_weight_sha256": EXPECTED_CANDIDATE_WEIGHT_SHA256,
        },
        "input_manifest": [
            {"basename": basename, "sha256": digest, "size_bytes": 1}
            for basename, digest in sorted(
                __import__(
                    "tools.evaluate_directional_stress80_final", fromlist=["x"]
                ).FIXED_INPUT_SHA256.items()
            )
        ],
        "constraints": EXPECTED_CONSTRAINTS,
        "result": _result(scenario, window, annualized),
    }


def test_concentration_seed_is_strictly_before_window_and_skips_inactive_targets():
    from tools.evaluate_directional_stress90_final import (
        completed_concentrations_before,
    )

    index = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    weights = pd.DataFrame(
        {"A": [1.0, 0.0, 1.0], "AG": [-1.0, 0.0, 0.0]},
        index=index,
    )

    assert completed_concentrations_before(
        weights,
        start=pd.Timestamp("2024-01-04"),
    ) == (0.5,)


def test_final_matrix_assembly_applies_frozen_gate_to_independent_windows():
    from tools.evaluate_directional_stress90_final import assemble_matrix_payload

    payloads = [
        _payload("base", "full_recent", 1.20),
        _payload("stress", "train", 0.10),
        _payload("stress", "validation", 1.0),
        _payload("stress", "oos", 0.10),
        _payload("stress", "full_recent", 0.90),
    ]

    matrix = assemble_matrix_payload(payloads)

    assert matrix["gate"] == {"passed": True, "reasons": []}
    assert len(matrix["results"]) == 5


def test_final_matrix_assembly_rejects_duplicate_window():
    from tools.evaluate_directional_stress90_final import assemble_matrix_payload

    payload = _payload("base", "full_recent", 1.20)

    with pytest.raises(ValueError, match="duplicate"):
        assemble_matrix_payload([payload, payload])


def test_final_matrix_assembly_rejects_altered_hard_constraints():
    from tools.evaluate_directional_stress90_final import assemble_matrix_payload

    payload = _payload("base", "full_recent", 1.20)
    payload["constraints"] = dict(payload["constraints"])
    payload["constraints"]["total_drawdown_ratio"] = 0.31

    with pytest.raises(ValueError, match="constraints"):
        assemble_matrix_payload([payload])


def test_final_matrix_assembly_rejects_manifest_with_wrong_frozen_digest():
    from tools.evaluate_directional_stress90_final import assemble_matrix_payload

    payload = _payload("base", "full_recent", 1.20)
    payload["input_manifest"][0]["sha256"] = "0" * 64

    with pytest.raises(ValueError, match="input manifest.*SHA-256"):
        assemble_matrix_payload([payload])


def test_final_matrix_assembly_rejects_missing_or_extra_manifest_entries():
    from tools.evaluate_directional_stress90_final import assemble_matrix_payload

    payload = _payload("base", "full_recent", 1.20)
    payload["input_manifest"] = payload["input_manifest"][:-1]

    with pytest.raises(ValueError, match="input manifest.*basename"):
        assemble_matrix_payload([payload])


def test_final_matrix_assembly_rejects_missing_manifest():
    from tools.evaluate_directional_stress90_final import assemble_matrix_payload

    payload = _payload("base", "full_recent", 1.20)
    del payload["input_manifest"]

    with pytest.raises(ValueError, match="input manifest.*basename"):
        assemble_matrix_payload([payload])


def test_final_matrix_assembly_rejects_duplicate_and_extra_manifest_basenames():
    from tools.evaluate_directional_stress90_final import assemble_matrix_payload

    duplicate = _payload("base", "full_recent", 1.20)
    duplicate["input_manifest"][-1] = dict(duplicate["input_manifest"][0])
    with pytest.raises(ValueError, match="input manifest.*basename"):
        assemble_matrix_payload([duplicate])

    extra = _payload("base", "full_recent", 1.20)
    extra["input_manifest"].append({"basename": "extra.csv", "sha256": "0" * 64, "size_bytes": 1})
    with pytest.raises(ValueError, match="input manifest.*basename"):
        assemble_matrix_payload([extra])


def test_final_matrix_assembly_rejects_different_window_manifests():
    from tools.evaluate_directional_stress90_final import assemble_matrix_payload

    first = _payload("base", "full_recent", 1.20)
    second = _payload("stress", "train", 0.10)
    second["input_manifest"][0]["size_bytes"] = 2

    with pytest.raises(ValueError, match="input manifests differ"):
        assemble_matrix_payload([first, second])


@pytest.mark.parametrize("invalid_size", [-1, True, 1.5])
def test_final_matrix_assembly_rejects_invalid_manifest_size(invalid_size):
    from tools.evaluate_directional_stress90_final import assemble_matrix_payload

    payload = _payload("base", "full_recent", 1.20)
    payload["input_manifest"][0]["size_bytes"] = invalid_size

    with pytest.raises(ValueError, match="size_bytes"):
        assemble_matrix_payload([payload])


def test_final_matrix_assembly_is_order_independent_and_keeps_manifest():
    from tools.evaluate_directional_stress90_final import assemble_matrix_payload

    payloads = [
        _payload("stress", "prior2", 0.08),
        _payload("stress", "full_recent", 0.90),
        _payload("base", "full_recent", 1.20),
        _payload("stress", "oos", 0.10),
        _payload("stress", "prior1", 0.12),
        _payload("stress", "validation", 1.0),
        _payload("stress", "train", 0.10),
    ]

    first = assemble_matrix_payload(payloads)
    second = assemble_matrix_payload(list(reversed(payloads)))

    assert first == second
    assert first["input_manifest"] == payloads[0]["input_manifest"]


def test_fixed_input_loader_rejects_bad_input_before_any_csv_parser(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    import tools.evaluate_directional_stress80_final as stress80

    expected = {
        basename: hashlib.sha256(b"expected").hexdigest()
        for basename in stress80.FIXED_INPUT_BASENAMES
    }
    for basename in expected:
        (tmp_path / basename).write_bytes(b"actual")
    parser_calls = 0

    def parser_must_not_run(*args, **kwargs):
        nonlocal parser_calls
        parser_calls += 1
        raise AssertionError("CSV parser must not run")

    monkeypatch.setattr(stress80.pd, "read_csv", parser_must_not_run)

    with pytest.raises(
        SystemExit,
        match=r"broad_daily_universe\.csv.*expected=.*actual=",
    ):
        stress80._load_inputs(tmp_path, expected_sha256=expected)

    assert parser_calls == 0


def _valid_frozen_input_bytes() -> dict[str, bytes]:
    bar = b"datetime,product,symbol,open,close,volume,hold\n2026-01-02 10:00,A,A2605,1,1,1,1\n"
    return {
        "broad_daily_universe.csv": b"date,product,close\n2026-01-02,A,1\n",
        "return_target_specific_contracts.csv": b"date,product,close\n2026-01-02,A,1\n",
        "execution_aligned_weights.csv": b"level_0,level_1,weight\n2026-01-02,A,1\n",
        "prior_two_year_broad_60m.csv": bar,
        "two_year_broad_60m.csv": bar,
    }


def _write_frozen_inputs(tmp_path: Path) -> tuple[dict[str, bytes], dict[str, str]]:
    contents = _valid_frozen_input_bytes()
    for basename, payload in contents.items():
        (tmp_path / basename).write_bytes(payload)
    return contents, {
        name: hashlib.sha256(payload).hexdigest() for name, payload in contents.items()
    }


def test_fixed_input_loader_verifies_every_file_before_parsing_verified_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import tools.evaluate_directional_stress80_final as stress80

    contents, expected = _write_frozen_inputs(tmp_path)
    original_verify = stress80.verify_frozen_input_files
    original_read_csv = stress80.pd.read_csv
    verified = False
    parser_inputs: list[object] = []

    def verified_loader(*args, **kwargs):
        nonlocal verified
        result = original_verify(*args, **kwargs)
        verified = True
        return result

    def read_csv_after_verification(source, *args, **kwargs):
        assert verified is True
        parser_inputs.append(source)
        return original_read_csv(source, *args, **kwargs)

    monkeypatch.setattr(stress80, "verify_frozen_input_files", verified_loader)
    monkeypatch.setattr(stress80.pd, "read_csv", read_csv_after_verification)

    specific, continuous, weights, bars, manifest = stress80._load_inputs(
        tmp_path,
        expected_sha256=expected,
    )

    assert not specific.empty and not continuous.empty and not weights.empty and not bars.empty
    assert len(parser_inputs) == 5
    assert all(isinstance(source, BytesIO) for source in parser_inputs)
    assert manifest == [
        {
            "basename": basename,
            "sha256": expected[basename],
            "size_bytes": len(contents[basename]),
        }
        for basename in sorted(contents)
    ]
    assert all(set(item) == {"basename", "sha256", "size_bytes"} for item in manifest)
    assert all("/" not in item["basename"] for item in manifest)


@pytest.mark.parametrize("unsafe_kind", ["missing", "directory", "symlink"])
def test_fixed_input_loader_rejects_unsafe_input_before_any_csv_parser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_kind: str,
):
    import tools.evaluate_directional_stress80_final as stress80

    _contents, expected = _write_frozen_inputs(tmp_path)
    unsafe = tmp_path / "broad_daily_universe.csv"
    if unsafe_kind == "missing":
        unsafe.unlink()
    elif unsafe_kind == "directory":
        unsafe.unlink()
        unsafe.mkdir()
    else:
        target = tmp_path / "alternate.csv"
        target.write_bytes(b"date,product,close\n2026-01-02,A,1\n")
        unsafe.unlink()
        unsafe.symlink_to(target)
    parser_calls = 0

    def parser_must_not_run(*args, **kwargs):
        nonlocal parser_calls
        parser_calls += 1
        raise AssertionError("CSV parser must not run")

    monkeypatch.setattr(stress80.pd, "read_csv", parser_must_not_run)

    with pytest.raises(SystemExit, match="input verification failed"):
        stress80._load_inputs(tmp_path, expected_sha256=expected)

    assert parser_calls == 0
