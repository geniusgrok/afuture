"""Offline attribution and future-outcome labels for Production MPV research.

Nothing in this module is decision-side runtime input. Functions that use prices after a
decision date return explicit labels for research evaluation only.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


def _empty_product_evidence() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "gross_pnl",
            "turnover_notional",
            "transaction_cost",
            "net_alpha",
            "pnl_event_count",
            "trade_event_count",
            "lot_segment_exposure",
        ]
    ).rename_axis("product")


def _aggregate_product_evidence(
    frame: pd.DataFrame,
    *,
    pnl_actions: tuple[str, ...] | None = None,
) -> pd.DataFrame:
    columns = list(_empty_product_evidence().columns)
    if frame.empty:
        return _empty_product_evidence()
    work = frame.copy()
    work["product"] = work["product"].astype(str).str.upper()
    for column in ("gross_pnl", "turnover_notional", "transaction_cost", "lots_before"):
        if column not in work:
            work[column] = 0.0
        work[column] = pd.to_numeric(work[column], errors="coerce").fillna(0.0)
    rows = []
    allowed_actions = {str(value) for value in pnl_actions} if pnl_actions is not None else None
    for product, group in work.groupby("product", sort=True):
        pnl = group[group["kind"] == "pnl"]
        if allowed_actions is not None:
            if "action" not in pnl:
                pnl = pnl.iloc[0:0]
            else:
                pnl = pnl[pnl["action"].astype(str).isin(allowed_actions)]
        trades = group[group["kind"] == "trade"]
        gross_pnl = float(pnl["gross_pnl"].sum())
        lot_segment_exposure = float(pnl["lots_before"].abs().sum())
        turnover = float(trades["turnover_notional"].sum())
        cost = float(trades["transaction_cost"].sum())
        rows.append(
            {
                "product": str(product),
                "gross_pnl": gross_pnl,
                "turnover_notional": turnover,
                "transaction_cost": cost,
                "net_alpha": gross_pnl - cost,
                "pnl_event_count": int(len(pnl)),
                "trade_event_count": int(len(trades)),
                "lot_segment_exposure": lot_segment_exposure,
            }
        )
    return pd.DataFrame(rows).set_index("product")[columns]


def observed_product_evidence(
    *,
    events: pd.DataFrame,
    pnl_actions: tuple[str, ...] | None = None,
) -> pd.DataFrame:
    """Aggregate only events the caller has already observed at decision time."""
    if events.empty:
        return _empty_product_evidence()
    return _aggregate_product_evidence(events, pnl_actions=pnl_actions)


def completed_product_evidence(*, events: pd.DataFrame, decision_date) -> pd.DataFrame:
    """Aggregate events strictly before ``decision_date`` for offline causal research."""
    cutoff = pd.Timestamp(decision_date).normalize()
    if events.empty:
        return _empty_product_evidence()
    frame = events.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    frame = frame[frame["date"].notna() & (frame["date"] < cutoff)]
    return _aggregate_product_evidence(frame)


@dataclass(frozen=True)
class OneLotCounterfactualLabel:
    decision_date: pd.Timestamp
    label_end_date: pd.Timestamp
    product: str
    symbol: str
    delta_lots: int
    realized_incremental_gross_pnl: float
    realized_incremental_turnover_notional: float
    realized_incremental_transaction_cost: float
    realized_net_counterfactual_value: float


def build_one_lot_counterfactual_label(
    *,
    decision_date,
    label_end_date,
    product: str,
    symbol: str,
    delta_lots: int,
    start_price: float,
    end_price: float,
    multiplier: float,
    cost_bps: float,
) -> OneLotCounterfactualLabel:
    """Build an ex-post local one-lot outcome label; never use this as a decision feature."""
    decision = pd.Timestamp(decision_date).normalize()
    label_end = pd.Timestamp(label_end_date).normalize()
    if label_end <= decision:
        raise ValueError("label_end_date must be after decision_date")
    if int(delta_lots) not in (-1, 1) or int(delta_lots) != delta_lots:
        raise ValueError("delta_lots must be exactly -1 or +1")
    start = float(start_price)
    end = float(end_price)
    contract_multiplier = float(multiplier)
    rate = float(cost_bps) / 10000.0
    if start <= 0.0 or end <= 0.0 or contract_multiplier <= 0.0 or rate < 0.0:
        raise ValueError("counterfactual price/multiplier/cost inputs are invalid")
    gross = (end - start) * int(delta_lots) * contract_multiplier
    turnover = abs(int(delta_lots)) * start * contract_multiplier
    cost = turnover * rate
    return OneLotCounterfactualLabel(
        decision_date=decision,
        label_end_date=label_end,
        product=str(product).upper(),
        symbol=str(symbol).upper(),
        delta_lots=int(delta_lots),
        realized_incremental_gross_pnl=float(gross),
        realized_incremental_turnover_notional=float(turnover),
        realized_incremental_transaction_cost=float(cost),
        realized_net_counterfactual_value=float(gross - cost),
    )
