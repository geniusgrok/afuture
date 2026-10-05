import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
SPEC = importlib.util.spec_from_file_location(
    "overall_rebuild_research", TOOLS / "overall_rebuild_research.py"
)
assert SPEC and SPEC.loader
research = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(research)


def predictions():
    return pd.DataFrame(
        {
            "entry_day": [pd.Timestamp("2025-01-08")],
            "decision_day": [pd.Timestamp("2025-01-08")],
            "product": ["RB"],
            "symbol": ["RB2505"],
            "expected_return": [0.01],
            "volatility": [0.02],
        }
    )


@pytest.mark.parametrize("through", ["2025-01-08", "2025-01-09", None])
def test_archived_forecast_requires_actual_strict_prior_training_evidence(through):
    audit = [{"decision_day": "2025-01-08", "last_maturity_day": through}]
    with pytest.raises(ValueError, match="prior archived"):
        research.causal_archive(predictions(), audit)


def test_parent_oof_observations_are_exact_identity_and_missing_is_not_backfilled():
    archive = research.causal_archive(
        predictions(), [{"decision_day": "2025-01-08", "last_maturity_day": "2025-01-07"}]
    )
    observations = pd.DataFrame(
        {
            "entry_day": [pd.Timestamp("2025-01-08"), pd.Timestamp("2025-01-09")],
            "product": ["RB", "RB"],
            "symbol": ["RB2505", "RB2505"],
            "gross_return": [0.2, 0.3],
        }
    )
    result = research.attach_parent_observations(observations, archive)
    assert result.parent_expected_return.iloc[0] == 0.01
    assert pd.isna(result.parent_expected_return.iloc[1])
    assert result.parent_training_through.iloc[0] == pd.Timestamp("2025-01-07")
    changed = observations.copy()
    changed.loc[0, "symbol"] = "RB2510"
    assert research.attach_parent_observations(changed, archive).parent_expected_return.isna().all()


def test_registered_mechanisms_continue_in_frozen_order_after_economic_failure():
    experiments = [{"id": "first"}, {"id": "second"}, {"id": "third"}]
    history = [{"spec": {"id": "first"}, "status": "economic_failed"}]
    assert research.next_registered_recipe(experiments, history)["id"] == "second"
    history.append({"spec": {"id": "second"}, "status": "candidate_passed"})
    assert research.next_registered_recipe(experiments, history)["id"] == "third"
    history.append({"spec": {"id": "third"}, "status": "invalid_evidence"})
    assert research.next_registered_recipe(experiments, history) is None
    with pytest.raises(ValueError, match="duplicate"):
        research.next_registered_recipe([{"id": "same"}, {"id": "same"}], [])


def test_one_account_combination_nets_targets_before_integer_projection():
    index = pd.DatetimeIndex(["2025-01-08"])
    old = pd.DataFrame([[0.25, -0.25]], index=index, columns=["RB", "CU"])
    new = pd.DataFrame([[-0.25, -0.25]], index=index, columns=["RB", "CU"])
    combined = research.one_account_targets({"legacy": old, "curve": new})
    assert combined.loc[index[0]].to_dict() == {"RB": 0.0, "CU": -0.25}
    assert old.loc[index[0], "RB"] == 0.25
    with pytest.raises(ValueError, match="identities"):
        research.one_account_targets({"legacy": old, "curve": new.iloc[:, ::-1]})


def test_incremental_mixture_preserves_legacy_and_adds_only_spare_compatible_risk():
    index = pd.DatetimeIndex(["2025-01-08"])
    old = pd.DataFrame([[0.2, -0.1, 0]], index=index, columns=["RB", "CU", "FU"])
    new = pd.DataFrame([[0.2, 0.2, 0.25]], index=index, columns=old.columns)
    combined = research.incremental_account_targets(old, new)
    np.testing.assert_allclose(combined.to_numpy(), [[0.25, -0.1, 0.25]])
    full = pd.DataFrame([[0.25] * 8 + [0.0]], index=index, columns=list("ABCDEFGHI"))
    requested = pd.DataFrame([[0] * 8 + [0.25]], index=index, columns=full.columns)
    pd.testing.assert_frame_equal(research.incremental_account_targets(full, requested), full)


def test_inertia_keeps_risk_reductions_reversals_and_new_roll_symbols():
    current = {"RB2505": 2, "CU2505": -4, "FU2505": 3}
    targets = {"RB2505": 5, "CU2505": -2, "FU2505": -1, "LU2505": 1}
    assert research.reduce_incumbent_resizing(targets, current) == {
        "RB2505": 2,
        "CU2505": -2,
        "FU2505": -1,
        "LU2505": 1,
    }
    assert targets["RB2505"] == 5


def test_fixed_forecast_control_never_reads_observed_returns():
    archive = research.causal_archive(
        predictions(), [{"decision_day": "2025-01-08", "last_maturity_day": "2025-01-07"}]
    )
    other = archive.copy()
    other["expected_return"] = -0.02
    result, fallback = research.equal_forecasts({"old": archive, "new": other}, archive)
    assert not fallback
    assert result.expected_return.iloc[0] == pytest.approx(-0.005)
    result, fallback = research.equal_forecasts({"old": archive, "new": other.iloc[:0]}, archive)
    assert fallback
    pd.testing.assert_frame_equal(result, archive)


def test_shared_exit_reduces_actual_incumbent_after_one_common_projection(monkeypatch):
    from afuture.directional_acceptance import TargetLotStages

    days = pd.bdate_range("2025-01-01", periods=25)
    market = pd.DataFrame(
        {
            "date": days,
            "symbol": "RB2505",
            "open": 100.0,
            "close": [100.0] * 24 + [97.0],
            "high": 101.0,
            "low": 96.0,
        }
    )
    calls = []

    def projected(account, **kwargs):
        calls.append(dict(kwargs["current_lots"]))
        return TargetLotStages(
            {"RB2505": 2},
            {"RB2505": 2},
            {"RB2505": 2},
            2000,
            2000,
            2000,
            2000,
            0,
            0,
            0,
        )

    monkeypatch.setattr(research.ProjectedCovarianceAccount, "target_lot_stages", projected)
    account = research.ProjectedHoldingCovarianceAccount(
        market, market_returns=pd.DataFrame({"RB": 0.001}, index=days)
    )
    account.day = days[-1] + pd.offsets.BDay()
    account.tracks["RB2505"] = {
        "sign": 1,
        "entry": 100.0,
        "peak": 100.0,
        "atr": 1.0,
        "armed": True,
    }
    target = account.target_lot_stages(current_lots={"RB2505": 1}, product_open_prices={"RB": 97.0})
    assert calls == [{"RB2505": 1}]
    assert not target.final_lots and target.final_notional == 0
    assert account.exit_audit[0]["filled_lots"] == 1
    assert account.exit_audit[0]["source_day"] == days[-1]
