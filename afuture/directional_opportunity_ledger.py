"""Exploration-independent Product x Alpha opportunity labels for research.

This module is intentionally research-only.  It records observable signal opportunities
and attaches labels supplied by a roll-safe historical return builder.  Whether the
Production candidate actually traded an opportunity is not an input, which avoids the
endogenous off-policy censoring found in the earlier candidate-owned MPV experiment.
"""
from __future__ import annotations

from math import isfinite
from typing import Mapping, Sequence

import pandas as pd

ALLOWED_HORIZONS = (5, 10, 20)
ROUND_TRIP_STRESS_COST_RATE = 0.003  # 15bp one-way, entry plus exit.

_LEDGER_COLUMNS = (
    "signal_date",
    "product",
    "family",
    "signal_direction",
    "signal_strength",
    "forward_horizon_sessions",
    "label_available_date",
    "raw_future_specific_contract_return",
    "future_specific_contract_gross_return",
    "future_specific_contract_net_return_15bp",
)


def _normalize_panel(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result.index = pd.to_datetime(result.index, errors="coerce").normalize()
    result = result.loc[~result.index.isna()]
    result.columns = [str(column).upper() for column in result.columns]
    return result.sort_index()


def build_opportunity_ledger(
    *,
    signals: Mapping[str, pd.DataFrame],
    forward_returns: Mapping[int, pd.DataFrame],
    label_available_dates: Mapping[int, pd.DataFrame],
    horizons: Sequence[int] = ALLOWED_HORIZONS,
) -> pd.DataFrame:
    """Build direction-conditioned labels without consulting candidate trade history."""
    requested = tuple(int(item) for item in horizons)
    if not requested or any(item not in ALLOWED_HORIZONS for item in requested):
        raise ValueError("opportunity horizon must be one of 5, 10, 20 sessions")
    if len(set(requested)) != len(requested):
        raise ValueError("opportunity horizons must be unique")

    normalized_forward: dict[int, pd.DataFrame] = {}
    normalized_available: dict[int, pd.DataFrame] = {}
    for horizon in requested:
        if horizon not in forward_returns or horizon not in label_available_dates:
            raise ValueError(f"missing forward label panel for horizon {horizon}")
        normalized_forward[horizon] = _normalize_panel(forward_returns[horizon])
        normalized_available[horizon] = _normalize_panel(label_available_dates[horizon])

    rows: list[dict] = []
    for raw_family in sorted(signals):
        family = str(raw_family).strip().lower()
        if not family:
            raise ValueError("opportunity family cannot be empty")
        signal_panel = _normalize_panel(signals[raw_family])
        for signal_date, signal_row in signal_panel.iterrows():
            for raw_product, raw_signal in signal_row.items():
                try:
                    signal = float(raw_signal)
                except (TypeError, ValueError):
                    continue
                if not isfinite(signal) or abs(signal) <= 1e-15:
                    continue
                product = str(raw_product).upper()
                direction = 1 if signal > 0.0 else -1
                for horizon in requested:
                    returns = normalized_forward[horizon]
                    availability = normalized_available[horizon]
                    if (
                        signal_date not in returns.index
                        or signal_date not in availability.index
                        or product not in returns.columns
                        or product not in availability.columns
                    ):
                        continue
                    raw_return = returns.at[signal_date, product]
                    available_on = availability.at[signal_date, product]
                    try:
                        future_return = float(raw_return)
                    except (TypeError, ValueError):
                        continue
                    available_on = pd.to_datetime(available_on, errors="coerce")
                    if not isfinite(future_return) or pd.isna(available_on):
                        continue
                    gross = float(direction) * future_return
                    rows.append(
                        {
                            "signal_date": pd.Timestamp(signal_date).normalize(),
                            "product": product,
                            "family": family,
                            "signal_direction": direction,
                            "signal_strength": abs(signal),
                            "forward_horizon_sessions": horizon,
                            "label_available_date": pd.Timestamp(available_on).normalize(),
                            "raw_future_specific_contract_return": future_return,
                            "future_specific_contract_gross_return": gross,
                            "future_specific_contract_net_return_15bp": (
                                gross - ROUND_TRIP_STRESS_COST_RATE
                            ),
                        }
                    )

    if not rows:
        return pd.DataFrame(columns=_LEDGER_COLUMNS)
    return pd.DataFrame(rows, columns=_LEDGER_COLUMNS).sort_values(
        ["signal_date", "family", "product", "forward_horizon_sessions"],
        kind="stable",
        ignore_index=True,
    )


def completed_opportunities(
    ledger: pd.DataFrame,
    *,
    decision_date,
) -> pd.DataFrame:
    """Return only labels that were fully available strictly before a decision."""
    if ledger.empty:
        return ledger.copy()
    missing = set(_LEDGER_COLUMNS) - set(ledger.columns)
    if missing:
        raise ValueError(f"opportunity ledger missing columns: {sorted(missing)}")
    decision = pd.to_datetime(decision_date, errors="coerce")
    if pd.isna(decision):
        raise ValueError("decision_date must be a valid date")
    frame = ledger.copy()
    frame["label_available_date"] = pd.to_datetime(
        frame["label_available_date"], errors="coerce"
    ).dt.normalize()
    frame["signal_date"] = pd.to_datetime(frame["signal_date"], errors="coerce").dt.normalize()
    cutoff = pd.Timestamp(decision).normalize()
    return frame[
        frame["label_available_date"].notna()
        & (frame["label_available_date"] < cutoff)
        & frame["signal_date"].notna()
        & (frame["signal_date"] < cutoff)
    ].copy()
