import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_FIXED_MANIFEST = [
    {
        "basename": "broad_daily_universe.csv",
        "sha256": "c1d46bc113a79bd2e000bf5d66ee53c59337eda750b2d3b1409e40f3f4833d0f",
        "size_bytes": 3_278_200,
    },
    {
        "basename": "execution_aligned_weights.csv",
        "sha256": "250a55c1c18ecd3754f489136515275eec3a48d505fd41026d68ab43924e08c1",
        "size_bytes": 109_977,
    },
    {
        "basename": "prior_two_year_broad_60m.csv",
        "sha256": "3351aa3ae8dec0cf0e9b9a64026181ac8ff2cc22858c442821fe3c94767036b1",
        "size_bytes": 3_698_425,
    },
    {
        "basename": "return_target_specific_contracts.csv",
        "sha256": "f8b3f4232cb1bc9eaed65a4401dff6bed121faee064252727202874ad7d53c64",
        "size_bytes": 40_842_073,
    },
    {
        "basename": "two_year_broad_60m.csv",
        "sha256": "5faf112bb69dd5ddf48ed34419e2046b1c6bdd651b595ca46a46804d8317a27b",
        "size_bytes": 4_080_136,
    },
]


def test_fixed_archive_manifest_is_the_only_compatibility_authority():
    from tools.stress90_fixed_archive_compat import (
        FixedArchiveCompatibilityError,
        validate_fixed_archive_manifest,
    )

    verified = validate_fixed_archive_manifest(_FIXED_MANIFEST)

    assert [entry["basename"] for entry in verified] == [
        entry["basename"] for entry in _FIXED_MANIFEST
    ]
    altered = [dict(entry) for entry in _FIXED_MANIFEST]
    altered[0]["sha256"] = "0" * 64
    with pytest.raises(FixedArchiveCompatibilityError, match="SHA-256 mismatch"):
        validate_fixed_archive_manifest(altered)


def test_public_replay_rejects_non_frozen_directory_before_parsing(tmp_path: Path):
    from tools.stress90_fixed_archive_compat import replay_fixed_archive

    for entry in _FIXED_MANIFEST:
        (tmp_path / entry["basename"]).write_bytes(b"not frozen evidence")

    with pytest.raises(SystemExit, match="input verification failed"):
        replay_fixed_archive(tmp_path)


def test_historical_missing_trend_blocks_only_entries_and_same_side_adds():
    from tools.stress90_fixed_archive_compat import _apply_historical_cost_gate

    close_days = pd.bdate_range("2026-01-01", periods=24)
    target_days = close_days[-4:]
    close = pd.DataFrame({"A": np.nan}, index=close_days)
    weights = pd.DataFrame(
        {"A": [2.0, 0.5, -0.5, 0.0]},
        index=target_days,
    )

    applied, trends = _apply_historical_cost_gate(
        weights=weights,
        close_prices=close,
        initial_weights={"A": 1.0},
    )

    assert close["A"].isna().all()
    assert trends["A"].isna().all()
    assert applied["A"].tolist() == [1.0, 0.5, -0.5, 0.0]


def test_historical_oi_missing_evidence_remains_missing_and_cannot_confirm_new_risk():
    from tools.stress90_fixed_archive_compat import _apply_historical_oi_confirmation

    days = pd.bdate_range("2026-01-01", periods=4)
    weights = pd.DataFrame({"A": [1.0, 2.0, 0.5, -0.5]}, index=days)
    flow = pd.DataFrame({"A": [np.nan, np.nan, np.nan, np.nan]}, index=days)

    applied = _apply_historical_oi_confirmation(
        raw_weights=weights,
        confirming_flow=flow,
        supported_products=("A",),
    )

    assert flow["A"].isna().all()
    assert applied["A"].tolist() == [0.0, 0.0, 0.0, 0.0]


def test_live_runtime_imports_do_not_load_fixed_archive_compatibility():
    script = """
import sys
import afuture.cli
import afuture.directional_runtime
import afuture.runtime_factory
assert not any(name.endswith('stress90_fixed_archive_compat') for name in sys.modules)
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
