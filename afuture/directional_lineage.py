"""Behavior-neutral lineage for the frozen directional policy and execution ledger.

The policy remains the sole owner of signals and targets. This module replays the exact
frozen template/meta calculation for audit only, verifies that the reconstructed target
weights are identical to ``ExecutionAlignedAggressivePolicy.weight_history()``, and then
joins product targets to the existing realized trade ledger.

Execution turnover and transaction cost deliberately remain product-level truth. When
multiple templates contribute to one product target, this module does *not* invent a
per-template allocation of integer lots, turnover, or cost.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .execution_aligned_policy import (
    BASE_COST_BPS,
    MAX_ABS_DAILY_RETURN,
    MAX_GROSS_LEVERAGE,
    STRESS_COST_BPS,
    _EXECUTION_TEMPLATES,
    _clean_prices,
    _intraday_proxy_stream,
    _robust_trailing_scores,
    _template_weight_path,
)


def build_weight_lineage(
    policy,
    open_prices: pd.DataFrame,
    close: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Reconstruct exact target weights plus template/meta ownership for audit.

    This function is intentionally independent of the production method. It fails if the
    audit reconstruction ever diverges from ``policy.weight_history`` so observability
    cannot silently become a second policy implementation.
    """
    close = _clean_prices(close, policy.products)
    open_prices = _clean_prices(open_prices, policy.products).reindex(close.index)
    returns = close.pct_change(fill_method=None)
    returns = returns.mask(returns.abs() > MAX_ABS_DAILY_RETURN)

    base_streams: dict[str, pd.Series] = {}
    stress_streams: dict[str, pd.Series] = {}
    paths: dict[str, pd.DataFrame] = {}
    for template_id, template in zip(policy.template_ids, _EXECUTION_TEMPLATES):
        weights = _template_weight_path(returns, template)
        paths[template_id] = weights
        base_streams[template_id] = _intraday_proxy_stream(
            open_prices, close, weights, cost_bps=BASE_COST_BPS
        )
        stress_streams[template_id] = _intraday_proxy_stream(
            open_prices, close, weights, cost_bps=STRESS_COST_BPS
        )

    base_frame = pd.DataFrame(base_streams).sort_index().fillna(0.0)
    stress_frame = pd.DataFrame(stress_streams).reindex(
        index=base_frame.index,
        columns=base_frame.columns,
    ).fillna(0.0)
    scores = _robust_trailing_scores(
        base_frame,
        stress_frame,
        lookback=policy.meta_lookback,
    )
    names = list(base_frame.columns)
    final = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    selected: list[int] = []
    meta_rows: list[dict] = []
    lineage_rows: list[dict] = []

    def aggregate(indices: list[int], timestamp) -> dict[str, float]:
        if not indices:
            return {}
        rows = [paths[names[item]].loc[timestamp] for item in indices]
        series = pd.concat(rows, axis=1).mean(axis=1)
        return {
            str(product): float(value)
            for product, value in series.items()
            if abs(float(value)) > 1e-15
        }

    for position, timestamp in enumerate(close.index):
        if position >= policy.meta_lookback and (
            not selected or position % policy.meta_rebalance == 0
        ):
            row = scores[position]
            valid = np.flatnonzero(np.isfinite(row))
            candidate = (
                [
                    int(item)
                    for item in valid[
                        np.argsort(-row[valid], kind="stable")[: policy.meta_count]
                    ]
                ]
                if valid.size
                else []
            )
            if not selected:
                selected = candidate
            elif candidate != selected:
                selected = candidate

        raw = aggregate(selected, timestamp)
        if raw:
            final.loc[timestamp] = (
                pd.Series(raw).reindex(final.columns).fillna(0.0)
            )

        selected_names = tuple(names[item] for item in selected)
        meta_rows.append(
            {
                "date": pd.Timestamp(timestamp),
                "selected_templates": selected_names,
            }
        )
        if selected:
            divisor = float(len(selected))
            for item in selected:
                template_id = names[item]
                template_row = paths[template_id].loc[timestamp]
                for product in final.columns:
                    raw_weight = float(template_row[product])
                    lineage_rows.append(
                        {
                            "date": pd.Timestamp(timestamp),
                            "template_id": template_id,
                            "product": str(product),
                            "raw_template_weight": raw_weight,
                            "contribution_weight": raw_weight / divisor,
                            "aggregate_weight": float(final.loc[timestamp, product]),
                        }
                    )

    gross = final.abs().sum(axis=1)
    if bool((gross > MAX_GROSS_LEVERAGE + 1e-10).any()):
        raise AssertionError("directional lineage reconstruction exceeded 2x gross")

    production = policy.weight_history(open_prices, close)
    try:
        pd.testing.assert_frame_equal(final, production)
    except AssertionError as exc:
        raise AssertionError(
            "directional lineage reconstruction diverged from production policy"
        ) from exc

    meta = pd.DataFrame(
        meta_rows,
        columns=("date", "selected_templates"),
    )
    lineage = pd.DataFrame(
        lineage_rows,
        columns=(
            "date",
            "template_id",
            "product",
            "raw_template_weight",
            "contribution_weight",
            "aggregate_weight",
        ),
    )
    return production, meta, lineage


def build_exact_lineage_attribution(
    *,
    meta: pd.DataFrame,
    template_product: pd.DataFrame,
    events: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    """Join target lineage to exact realized product position/turnover/cost.

    ``template_product`` owns only signal contribution. Realized position is reconstructed
    from the product/symbol trade ledger, including carry-forward on no-trade days.
    Product turnover and cost are summed directly from trade events. They remain ``NaN``
    at template level because integer execution is not uniquely attributable to one of
    several contributing templates.
    """
    meta_frame = meta.copy()
    if not meta_frame.empty:
        meta_frame["date"] = pd.to_datetime(
            meta_frame["date"], errors="coerce"
        ).dt.normalize()
        meta_frame = meta_frame.dropna(subset=["date"]).sort_values("date")

    lineage = template_product.copy()
    if not lineage.empty:
        lineage["date"] = pd.to_datetime(
            lineage["date"], errors="coerce"
        ).dt.normalize()
        lineage["product"] = lineage["product"].astype(str).str.upper()
        lineage = lineage.dropna(subset=["date"])
    lineage["turnover_notional"] = np.nan
    lineage["transaction_cost"] = np.nan

    if lineage.empty:
        target_weights = pd.DataFrame(
            columns=["date", "product", "target_weight"]
        )
    else:
        target_weights = (
            lineage[["date", "product", "aggregate_weight"]]
            .drop_duplicates(["date", "product"], keep="last")
            .rename(columns={"aggregate_weight": "target_weight"})
            .sort_values(["date", "product"])
        )

    event_frame = events.copy()
    if event_frame.empty:
        event_frame = pd.DataFrame(
            columns=[
                "date",
                "kind",
                "action",
                "product",
                "symbol",
                "lots_before",
                "lots_after",
                "delta_lots",
                "turnover_notional",
                "transaction_cost",
            ]
        )
    else:
        event_frame["date"] = pd.to_datetime(
            event_frame["date"], errors="coerce"
        ).dt.normalize()
        event_frame = event_frame.dropna(subset=["date"])
        event_frame["product"] = event_frame["product"].astype(str).str.upper()
        event_frame["symbol"] = event_frame["symbol"].astype(str)

    trades = event_frame[event_frame["kind"] == "trade"].copy()
    if trades.empty:
        trade_summary = pd.DataFrame(
            columns=[
                "date",
                "product",
                "turnover_notional",
                "transaction_cost",
                "execution_actions",
            ]
        )
    else:
        trade_summary = (
            trades.groupby(["date", "product"], sort=True)
            .agg(
                turnover_notional=("turnover_notional", "sum"),
                transaction_cost=("transaction_cost", "sum"),
                execution_actions=(
                    "action",
                    lambda values: tuple(dict.fromkeys(str(value) for value in values)),
                ),
            )
            .reset_index()
        )

    target_key_rows = {
        (pd.Timestamp(row.date), str(row.product).upper()): float(row.target_weight)
        for row in target_weights.itertuples(index=False)
    }
    trade_groups = {
        pd.Timestamp(day): frame.copy()
        for day, frame in trades.groupby("date", sort=True)
    }
    summary_rows = {
        (pd.Timestamp(row.date), str(row.product).upper()): row
        for row in trade_summary.itertuples(index=False)
    }
    all_dates = sorted(
        set(pd.Timestamp(value) for value in target_weights.get("date", []))
        | set(pd.Timestamp(value) for value in trades.get("date", []))
    )

    symbol_positions: dict[str, int] = {}
    symbol_products: dict[str, str] = {}
    product_rows: list[dict] = []
    for day in all_dates:
        day_trades = trade_groups.get(day)
        traded_products: set[str] = set()
        if day_trades is not None:
            for row in day_trades.itertuples(index=False):
                symbol = str(row.symbol)
                product = str(row.product).upper()
                symbol_products[symbol] = product
                symbol_positions[symbol] = int(row.lots_after)
                if symbol_positions[symbol] == 0:
                    symbol_positions.pop(symbol, None)
                traded_products.add(product)

        target_products = {
            product
            for (target_day, product), _ in target_key_rows.items()
            if target_day == day
        }
        for product in sorted(target_products | traded_products):
            realized = sum(
                int(volume)
                for symbol, volume in symbol_positions.items()
                if symbol_products.get(symbol) == product
            )
            summary = summary_rows.get((day, product))
            product_rows.append(
                {
                    "date": day,
                    "product": product,
                    "target_weight": target_key_rows.get((day, product), np.nan),
                    "realized_lots_after": int(realized),
                    "turnover_notional": (
                        float(summary.turnover_notional) if summary is not None else 0.0
                    ),
                    "transaction_cost": (
                        float(summary.transaction_cost) if summary is not None else 0.0
                    ),
                    "execution_actions": (
                        summary.execution_actions if summary is not None else ()
                    ),
                }
            )

    product_execution = pd.DataFrame(
        product_rows,
        columns=(
            "date",
            "product",
            "target_weight",
            "realized_lots_after",
            "turnover_notional",
            "transaction_cost",
            "execution_actions",
        ),
    )
    return {
        "meta": meta_frame.reset_index(drop=True),
        "template_product": lineage.reset_index(drop=True),
        "product_execution": product_execution,
    }
