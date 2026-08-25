from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_synthetic_archive(runtime: Path):
    from afuture.directional_60m_oi_confirmation import (
        build_daily_price_oi_flow,
        lag_flow_to_target_days,
    )
    from afuture.directional_stress90_bootstrap import Stress90BootstrapExpectations
    from afuture.directional_stress90_policy import (
        STRESS90_POLICY,
        build_stress90_candidate_path,
        candidate_weight_digest,
    )
    from afuture.execution_aligned_policy import ExecutionAlignedAggressivePolicy

    runtime.mkdir(parents=True)
    days = pd.bdate_range("2026-01-02", periods=44)
    open_prices = pd.DataFrame(index=days, columns=STRESS90_POLICY.products, dtype=float)
    close_prices = open_prices.copy()
    rows: list[dict[str, object]] = []
    positions = np.arange(len(days), dtype=float)
    for offset, product in enumerate(STRESS90_POLICY.products):
        close_values = (
            100.0
            + offset
            + np.cumsum(
                0.18 * np.sin(positions * ((offset % 7) + 1) / 9.0) + 0.025 * ((offset % 3) - 1)
            )
        )
        open_values = close_values / (1.0 + 0.0015 * np.sin(positions + offset))
        open_prices[product] = open_values
        close_prices[product] = close_values
        rows.extend(
            {
                "date": day,
                "product": product,
                "open": float(open_value),
                "close": float(close_value),
            }
            for day, open_value, close_value in zip(days, open_values, close_values, strict=True)
        )
    pd.DataFrame(rows).to_csv(runtime / "broad_daily_universe.csv", index=False)

    rebuilt = ExecutionAlignedAggressivePolicy(STRESS90_POLICY.products).weight_history(
        open_prices, close_prices
    )
    target_days = days[-12:]
    archived = rebuilt.loc[target_days].stack(future_stack=True).rename("weight").reset_index()
    archived.columns = ["level_0", "level_1", "weight"]
    archived.to_csv(runtime / "execution_aligned_weights.csv", index=False)

    contracts: list[dict[str, object]] = []
    for day in target_days:
        for product in STRESS90_POLICY.products:
            contracts.append(
                {
                    "date": day,
                    "delivery": day + pd.Timedelta(days=120),
                    "product": product,
                    "symbol": f"{product}2612",
                    "open": float(open_prices.at[day, product]),
                    "close": float(close_prices.at[day, product]),
                    "volume": 2_000.0,
                    "hold": 6_000.0,
                }
            )
    pd.DataFrame(contracts).to_csv(runtime / "return_target_specific_contracts.csv", index=False)

    source_days = [days[days.get_loc(target_days[0]) - 1], *target_days[:-1]]
    bars: list[dict[str, object]] = []
    for source_day in source_days:
        for product in STRESS90_POLICY.oi_products:
            bars.extend(
                [
                    {
                        "datetime": source_day + pd.Timedelta(hours=9),
                        "product": product,
                        "symbol": f"{product}2612",
                        "open": 100.0,
                        "close": 100.0,
                        "volume": 10.0,
                        "hold": 100.0,
                    },
                    {
                        "datetime": source_day + pd.Timedelta(hours=14),
                        "product": product,
                        "symbol": f"{product}2612",
                        "open": 100.0,
                        "close": 100.0,
                        "volume": 20.0,
                        "hold": 100.0,
                    },
                ]
            )
    midpoint = len(bars) // 2
    pd.DataFrame(bars[:midpoint]).to_csv(runtime / "prior_two_year_broad_60m.csv", index=False)
    pd.DataFrame(bars[midpoint:]).to_csv(runtime / "two_year_broad_60m.csv", index=False)

    flow = build_daily_price_oi_flow(pd.DataFrame(bars))
    lagged = lag_flow_to_target_days(
        flow,
        target_days=target_days,
        products=STRESS90_POLICY.oi_products,
    )
    candidate = build_stress90_candidate_path(
        base_weights=rebuilt.loc[target_days],
        completed_close_prices=close_prices,
        confirming_flow=lagged,
    ).survivor_weights
    source_manifest = {
        name: _sha256(runtime / name)
        for name in (
            "broad_daily_universe.csv",
            "return_target_specific_contracts.csv",
            "execution_aligned_weights.csv",
            "prior_two_year_broad_60m.csv",
            "two_year_broad_60m.csv",
        )
    }
    expectations = Stress90BootstrapExpectations(
        input_sha256=source_manifest,
        candidate_weight_sha256=candidate_weight_digest(candidate),
        official_historical_profile=False,
    )
    return target_days[-1].strftime("%Y%m%d"), expectations


def _refreshed_expectations(runtime: Path, old):
    from dataclasses import replace

    return replace(
        old,
        input_sha256={name: _sha256(runtime / name) for name in old.input_sha256},
    )


def test_dry_run_rebuilds_base_and_replays_incremental_candidate_exactly(tmp_path: Path):
    from afuture.directional_stress90_bootstrap import bootstrap_stress90

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)

    result = bootstrap_stress90(
        runtime_dir=runtime,
        through_day=through,
        expectations=expectations,
        write_artifacts=False,
    )

    assert result.source_manifest == expectations.input_sha256
    assert result.candidate_weight_sha256 == expectations.candidate_weight_sha256
    assert result.base_max_abs_error <= 5e-15
    assert result.batch_incremental_max_abs_error <= 1e-14
    assert result.last_target_day == through
    assert result.last_input_day < through
    assert result.historical_candidate_parity is False
    assert result.seed_path is None
    assert result.state_path is None
    assert not (runtime / "stress90_bootstrap_seed.json").exists()


def test_bootstrap_hashes_every_fixed_input_before_parsing(tmp_path: Path):
    from afuture.directional_stress90_bootstrap import (
        Stress90BootstrapError,
        bootstrap_stress90,
    )

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)
    with (runtime / "broad_daily_universe.csv").open("a", encoding="utf-8") as handle:
        handle.write("corrupt-after-fixed-hash\n")

    with pytest.raises(Stress90BootstrapError, match="SHA-256 mismatch"):
        bootstrap_stress90(
            runtime_dir=runtime,
            through_day=through,
            expectations=expectations,
            write_artifacts=False,
        )


def test_bootstrap_rejects_base_weight_drift_even_with_updated_input_hash(tmp_path: Path):
    from afuture.directional_stress90_bootstrap import (
        Stress90BootstrapError,
        bootstrap_stress90,
    )

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)
    path = runtime / "execution_aligned_weights.csv"
    weights = pd.read_csv(path)
    weights.loc[0, "weight"] = float(weights.loc[0, "weight"]) + 0.01
    weights.to_csv(path, index=False)
    expectations = _refreshed_expectations(runtime, expectations)

    with pytest.raises(Stress90BootstrapError, match="base policy parity"):
        bootstrap_stress90(
            runtime_dir=runtime,
            through_day=through,
            expectations=expectations,
            write_artifacts=False,
        )


def test_bootstrap_rejects_missing_supported_oi_coverage_not_as_zero(tmp_path: Path):
    from afuture.directional_stress90_bootstrap import (
        Stress90BootstrapError,
        bootstrap_stress90,
    )

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)
    paths = [
        runtime / "prior_two_year_broad_60m.csv",
        runtime / "two_year_broad_60m.csv",
    ]
    frames = [pd.read_csv(path) for path in paths]
    bars = pd.concat(frames, ignore_index=True)
    first_day = pd.to_datetime(bars["datetime"]).dt.normalize().min()
    bars = bars[
        ~((pd.to_datetime(bars["datetime"]).dt.normalize() == first_day) & (bars["product"] == "A"))
    ]
    split = len(bars) // 2
    bars.iloc[:split].to_csv(paths[0], index=False)
    bars.iloc[split:].to_csv(paths[1], index=False)
    expectations = _refreshed_expectations(runtime, expectations)

    with pytest.raises(Stress90BootstrapError, match="60m OI coverage"):
        bootstrap_stress90(
            runtime_dir=runtime,
            through_day=through,
            expectations=expectations,
            write_artifacts=False,
        )


def test_nonhistorical_profile_can_never_write_live_seed_or_state(tmp_path: Path):
    from afuture.directional_stress90_bootstrap import (
        Stress90BootstrapError,
        bootstrap_stress90,
    )

    runtime = tmp_path / "runtime"
    through, expectations = _write_synthetic_archive(runtime)

    with pytest.raises(Stress90BootstrapError, match="official immutable historical profile"):
        bootstrap_stress90(
            runtime_dir=runtime,
            through_day=through,
            expectations=expectations,
            write_artifacts=True,
        )
    assert not (runtime / "stress90_bootstrap_seed.json").exists()
    assert not (runtime / "stress90_policy_state.json").exists()


def test_cli_exposes_bootstrap_arguments_and_does_not_require_ctp_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    from types import SimpleNamespace

    from afuture.cli import build_parser, main

    parsed = build_parser().parse_args(
        [
            "stress90-bootstrap",
            "--config",
            "live.toml",
            "--runtime-dir",
            "runtime",
            "--through",
            "20260820",
        ]
    )
    assert parsed.runtime_dir == "runtime"
    assert parsed.through == "20260820"

    config_path = tmp_path / "bootstrap.toml"
    config_path.write_text(
        """
[system]
mode = "live"
initial_capital = 500000

[ctp]
environment = "test"
td_address = "tcp://trade.example"
md_address = "tcp://market.example"

[directional]
enabled = true
products = ["M"]

[paths]
state = "{state}"
log = "{log}"
report = "{report}"
journal = "{journal}"
alert = "{alert}"
""".format(
            state=tmp_path / "state.json",
            log=tmp_path / "afuture.log",
            report=tmp_path / "report.json",
            journal=tmp_path / "audit.jsonl",
            alert=tmp_path / "alerts.jsonl",
        ),
        encoding="utf-8",
    )
    for name in ("AFUTURE_CTP_USER", "AFUTURE_CTP_PASSWORD", "AFUTURE_CTP_BROKER"):
        monkeypatch.delenv(name, raising=False)
    calls = []

    def fake_bootstrap_stress90(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(to_dict=lambda: {"historical_candidate_parity": True})

    monkeypatch.setattr(
        "afuture.directional_stress90_bootstrap.bootstrap_stress90",
        fake_bootstrap_stress90,
    )
    assert (
        main(
            [
                "stress90-bootstrap",
                "--config",
                str(config_path),
                "--runtime-dir",
                str(tmp_path / "runtime"),
                "--through",
                "20260820",
            ]
        )
        == 0
    )
    assert calls == [
        {
            "runtime_dir": str(tmp_path / "runtime"),
            "through_day": "20260820",
            "write_artifacts": True,
        }
    ]
    assert '"historical_candidate_parity": true' in capsys.readouterr().out
