import pandas as pd

from afuture.directional_basis_alpha import build_basis_carry_weights


def test_basis_carry_uses_only_previous_target_session_basis():
    basis = pd.DataFrame([
        {"date": "2026-08-18", "symbol": "AG", "dom_basis_rate": 0.02},
        {"date": "2026-08-18", "symbol": "CU", "dom_basis_rate": -0.01},
        {"date": "2026-08-19", "symbol": "AG", "dom_basis_rate": -0.03},
        {"date": "2026-08-19", "symbol": "CU", "dom_basis_rate": 0.04},
    ])
    index = pd.to_datetime(["2026-08-18", "2026-08-19", "2026-08-20"])

    weights = build_basis_carry_weights(
        basis,
        target_index=index,
        products=("AG", "CU"),
    )

    assert weights.loc[pd.Timestamp("2026-08-18")].to_dict() == {"AG": 0.0, "CU": 0.0}
    # AKShare dom_basis_rate is dominant_futures / spot - 1. A positive value is
    # futures premium (contango), so carry is short; a negative value is long.
    assert weights.loc[pd.Timestamp("2026-08-19")].to_dict() == {"AG": -1.0, "CU": 1.0}
    assert weights.loc[pd.Timestamp("2026-08-20")].to_dict() == {"AG": 1.0, "CU": -1.0}


def test_basis_carry_fails_closed_on_missing_previous_session_product_evidence():
    basis = pd.DataFrame([
        {"date": "2026-08-18", "symbol": "AG", "dom_basis_rate": 0.02},
        {"date": "2026-08-18", "symbol": "CU", "dom_basis_rate": -0.01},
        {"date": "2026-08-19", "symbol": "AG", "dom_basis_rate": -0.03},
    ])
    index = pd.to_datetime(["2026-08-18", "2026-08-19", "2026-08-20"])

    weights = build_basis_carry_weights(
        basis,
        target_index=index,
        products=("AG", "CU"),
    )

    assert weights.loc[pd.Timestamp("2026-08-20"), "AG"] == 2.0
    assert weights.loc[pd.Timestamp("2026-08-20"), "CU"] == 0.0
    assert weights.loc[pd.Timestamp("2026-08-20")].abs().sum() == 2.0


def test_basis_carry_zero_basis_creates_no_position_and_never_exceeds_two_x_gross():
    basis = pd.DataFrame([
        {"date": "2026-08-18", "symbol": "AG", "dom_basis_rate": 0.0},
        {"date": "2026-08-18", "symbol": "CU", "dom_basis_rate": -0.01},
        {"date": "2026-08-18", "symbol": "RB", "dom_basis_rate": 0.01},
    ])
    index = pd.to_datetime(["2026-08-18", "2026-08-19"])

    weights = build_basis_carry_weights(
        basis,
        target_index=index,
        products=("AG", "CU", "RB"),
    )

    assert weights.loc[pd.Timestamp("2026-08-19"), "AG"] == 0.0
    assert weights.loc[pd.Timestamp("2026-08-19"), "CU"] == 1.0
    assert weights.loc[pd.Timestamp("2026-08-19"), "RB"] == -1.0
    assert float(weights.abs().sum(axis=1).max()) <= 2.0
