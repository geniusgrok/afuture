"""Behavior-neutral audit primitives for directional production mechanics.

The helpers in this module classify already-requested/executed activity. They do not
own signal selection, account state, risk authority, or order generation.
"""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite

import pandas as pd

AUDIT_EVENT_COLUMNS = (
    "date",
    "kind",
    "action",
    "product",
    "symbol",
    "side",
    "lots_before",
    "lots_after",
    "delta_lots",
    "price",
    "turnover_notional",
    "transaction_cost",
    "gross_pnl",
)


def classify_rebalance_action(
    *,
    symbol: str,
    original_lots: Mapping[str, int],
    target_lots: Mapping[str, int],
    symbol_products: Mapping[str, str],
) -> str:
    """Classify one normal-rebalance symbol delta using product-level intent.

    Entry and exit are deliberately separate here even though the legacy turnover report
    keeps their combined ``entry_exit`` bucket for backward-compatible evidence.
    """
    product = str(symbol_products.get(symbol, "")).upper()
    if not product:
        raise ValueError(f"missing product classification: {symbol}")
    original_by_product = {
        str(symbol_products[item]).upper(): (item, int(volume))
        for item, volume in original_lots.items()
        if int(volume) != 0 and item in symbol_products
    }
    target_by_product = {
        str(symbol_products[item]).upper(): (item, int(volume))
        for item, volume in target_lots.items()
        if int(volume) != 0 and item in symbol_products
    }
    original = original_by_product.get(product)
    target = target_by_product.get(product)
    if original is not None and target is not None and original[0] != target[0]:
        return "roll"
    if original is not None and target is not None:
        return "reversal" if (original[1] > 0) != (target[1] > 0) else "resize"
    if original is None and target is not None:
        return "entry"
    if original is not None and target is None:
        return "exit"
    raise ValueError(f"executed symbol is absent from original and target intent: {symbol}")


def exposure_side(volume: int) -> str:
    if int(volume) > 0:
        return "long"
    if int(volume) < 0:
        return "short"
    return "flat"


def _annualized_return(returns) -> float:
    values = [float(value) for value in returns]
    if not values:
        return 0.0
    wealth = 1.0
    for value in values:
        wealth *= 1.0 + value
    if wealth <= 0.0:
        return -1.0
    return float(wealth ** (252.0 / len(values)) - 1.0)


def _holding_sessions(daily, pnl_events) -> float:
    if daily.empty or pnl_events.empty:
        return 0.0
    session_index = {
        pd.Timestamp(day).normalize(): position
        for position, day in enumerate(pd.to_datetime(daily.index))
    }
    episodes: list[int] = []
    grouped = pnl_events.copy()
    grouped["date"] = pd.to_datetime(grouped["date"]).dt.normalize()
    for (_, _), frame in grouped.groupby(["product", "side"], sort=True):
        positions = sorted({session_index[day] for day in frame["date"] if day in session_index})
        if not positions:
            continue
        length = 1
        for previous, current in zip(positions, positions[1:], strict=False):
            if current == previous + 1:
                length += 1
            else:
                episodes.append(length)
                length = 1
        episodes.append(length)
    return float(sum(episodes) / len(episodes)) if episodes else 0.0


def _empty_robustness_diagnostics() -> dict:
    """Return the stable no-observation form of the offline diagnostics."""
    return {
        "worst_calendar_quarter": None,
        "worst_calendar_quarter_return": None,
        "worst_rolling_63_session_return": None,
        "worst_rolling_63_start": None,
        "worst_rolling_63_end": None,
        "max_drawdown_duration_sessions": 0,
        "drawdown_start": None,
        "recovery_date": None,
        "positive_product_pnl_total": 0.0,
        "best_product": None,
        "best_product_gross_pnl": 0.0,
        "best_product_positive_pnl_share": 0.0,
        "top3_positive_pnl_share": 0.0,
        "positive_product_pnl_hhi": 0.0,
        "gross_pnl_without_best_product_proxy": 0.0,
        "best_calendar_month": None,
        "best_calendar_month_return": None,
        "compounded_return_excluding_best_month_proxy": 0.0,
        "proxy_methodology": (
            "best-product removal is a fixed realized pathwise additive proxy, not a "
            "re-simulation or full counterfactual; it does not reflect equity, integer lots, "
            "margin, reallocation, risk state, or future orders. Best-month removal is also a "
            "fixed realized pathwise proxy, not a re-simulation or full counterfactual."
        ),
    }


def _as_validated_diagnostic_daily(daily) -> pd.DataFrame:
    """Copy and chronologically order only the fields required by diagnostics."""
    if daily.empty:
        return pd.DataFrame(columns=("equity", "daily_return"))
    frame = daily.copy()
    dates = pd.to_datetime(frame.index, errors="coerce")
    if bool(pd.isna(dates).any()):
        raise ValueError("robustness diagnostics daily date is invalid")
    dates = pd.DatetimeIndex(dates).normalize()
    if bool(dates.duplicated(keep=False).any()):
        raise ValueError("robustness diagnostics daily date is duplicate")
    for column in ("equity", "daily_return"):
        if column not in frame:
            raise ValueError(f"robustness diagnostics daily {column} is missing")
        values = pd.to_numeric(frame[column], errors="coerce")
        if not bool(values.map(isfinite).all()):
            raise ValueError(f"robustness diagnostics daily {column} must be finite")
        if column == "equity" and bool((values <= 0.0).any()):
            raise ValueError("robustness diagnostics daily equity must be positive")
        frame[column] = values.astype(float)
    frame.index = dates
    return frame.sort_index()


def _validated_product_gross_pnl(pnl_events) -> dict[str, float]:
    """Aggregate realized product PnL without changing event multiplicity."""
    if pnl_events.empty:
        return {}
    values = pd.to_numeric(pnl_events["gross_pnl"], errors="coerce")
    if not bool(values.map(isfinite).all()):
        raise ValueError("robustness diagnostics product gross_pnl must be finite")
    frame = pnl_events.copy()
    frame["gross_pnl"] = values.astype(float)
    totals: dict[str, float] = {}
    for product, value in zip(frame["product"], frame["gross_pnl"], strict=False):
        key = str(product)
        total = totals.get(key, 0.0) + float(value)
        if not isfinite(total):
            raise ValueError("robustness diagnostics product gross_pnl must be finite")
        totals[key] = total
    return {product: totals[product] for product in sorted(totals)}


def _compounded_return(values) -> float:
    wealth = 1.0
    for value in values:
        wealth *= 1.0 + float(value)
    return float(wealth - 1.0)


def _longest_drawdown(
    daily: pd.DataFrame, initial_capital: float
) -> tuple[int, str | None, str | None]:
    high_watermark = float(initial_capital)
    current_duration = 0
    current_start: str | None = None
    longest_duration = 0
    longest_start: str | None = None
    longest_recovery: str | None = None

    for day, equity in daily["equity"].items():
        date = pd.Timestamp(day).date().isoformat()
        value = float(equity)
        if value < high_watermark:
            if current_duration == 0:
                current_start = date
            current_duration += 1
            continue
        if current_duration > longest_duration:
            longest_duration = current_duration
            longest_start = current_start
            longest_recovery = date
        current_duration = 0
        current_start = None
        high_watermark = max(high_watermark, value)

    if current_duration > longest_duration:
        longest_duration = current_duration
        longest_start = current_start
        longest_recovery = None
    return longest_duration, longest_start, longest_recovery


def _robustness_diagnostics(
    *, daily, pnl_events, gross_signal_pnl: float, initial_capital: float
) -> dict:
    """Summarize offline path fragility without altering the realized simulation path."""
    daily_frame = _as_validated_diagnostic_daily(daily)
    result = _empty_robustness_diagnostics()
    result["gross_pnl_without_best_product_proxy"] = float(gross_signal_pnl)
    product_pnl = _validated_product_gross_pnl(pnl_events)
    positive_product_pnl = {product: value for product, value in product_pnl.items() if value > 0.0}
    if positive_product_pnl:
        best_product, best_product_pnl = min(
            positive_product_pnl.items(), key=lambda item: (-item[1], item[0])
        )
        positive_pnl = sorted(positive_product_pnl.values(), reverse=True)
        positive_total = float(sum(positive_pnl))
        if not isfinite(positive_total):
            raise ValueError("robustness diagnostics product gross_pnl must be finite")
        result["positive_product_pnl_total"] = positive_total
        result["best_product"] = best_product
        result["best_product_gross_pnl"] = float(best_product_pnl)
        result["gross_pnl_without_best_product_proxy"] = float(gross_signal_pnl - best_product_pnl)
        shares = [value / positive_total for value in positive_pnl]
        result["best_product_positive_pnl_share"] = float(best_product_pnl / positive_total)
        result["top3_positive_pnl_share"] = float(sum(shares[:3]))
        result["positive_product_pnl_hhi"] = float(sum(share * share for share in shares))
    if daily_frame.empty:
        return result

    returns = daily_frame["daily_return"]
    quarter_returns = returns.groupby(returns.index.to_period("Q"), sort=True).apply(
        _compounded_return
    )
    worst_quarter = quarter_returns.idxmin()
    result["worst_calendar_quarter"] = str(worst_quarter)
    result["worst_calendar_quarter_return"] = float(quarter_returns.loc[worst_quarter])

    if len(returns) >= 63:
        window_return: float | None = None
        window_start: str | None = None
        window_end: str | None = None
        for position in range(len(returns) - 62):
            candidate = _compounded_return(returns.iloc[position : position + 63])
            if window_return is None or candidate < window_return:
                window_return = candidate
                window_start = pd.Timestamp(returns.index[position]).date().isoformat()
                window_end = pd.Timestamp(returns.index[position + 62]).date().isoformat()
        assert window_return is not None
        result["worst_rolling_63_session_return"] = float(window_return)
        result["worst_rolling_63_start"] = window_start
        result["worst_rolling_63_end"] = window_end

    duration, drawdown_start, recovery_date = _longest_drawdown(daily_frame, initial_capital)
    result["max_drawdown_duration_sessions"] = int(duration)
    result["drawdown_start"] = drawdown_start
    result["recovery_date"] = recovery_date

    month_returns = returns.groupby(returns.index.to_period("M"), sort=True).apply(
        _compounded_return
    )
    best_month = month_returns.idxmax()
    result["best_calendar_month"] = str(best_month)
    result["best_calendar_month_return"] = float(month_returns.loc[best_month])
    result["compounded_return_excluding_best_month_proxy"] = _compounded_return(
        returns.loc[returns.index.to_period("M") != best_month]
    )
    return result


def summarize_production_attribution(
    *,
    daily,
    events,
    initial_capital: float,
) -> dict:
    """Aggregate behavior-neutral production audit evidence.

    Annualized impacts are pathwise additive proxies on the realized production path.
    They intentionally do not claim a full counterfactual re-sizing simulation because
    removing a component would change future equity, integer lots, and risk state.
    """
    daily_frame = daily.copy()
    if not daily_frame.empty:
        daily_frame.index = pd.to_datetime(daily_frame.index).normalize()
    event_frame = events.copy()
    if event_frame.empty:
        event_frame = pd.DataFrame(columns=AUDIT_EVENT_COLUMNS)
    else:
        event_frame["date"] = pd.to_datetime(event_frame["date"]).dt.normalize()

    pnl_events = event_frame[event_frame["kind"] == "pnl"].copy()
    trade_events = event_frame[event_frame["kind"] == "trade"].copy()
    _validated_product_gross_pnl(pnl_events)
    gross_signal_pnl = float(pnl_events["gross_pnl"].sum()) if not pnl_events.empty else 0.0
    long_pnl = (
        float(pnl_events.loc[pnl_events["side"] == "long", "gross_pnl"].sum())
        if not pnl_events.empty
        else 0.0
    )
    short_pnl = (
        float(pnl_events.loc[pnl_events["side"] == "short", "gross_pnl"].sum())
        if not pnl_events.empty
        else 0.0
    )
    product_pnl = (
        {
            str(product): float(value)
            for product, value in pnl_events.groupby("product", sort=True)["gross_pnl"]
            .sum()
            .items()
        }
        if not pnl_events.empty
        else {}
    )

    if daily_frame.empty:
        previous_equity = pd.Series(dtype=float)
        actual_returns = pd.Series(dtype=float)
    else:
        previous_equity = daily_frame["equity"].shift(1).astype(float)
        previous_equity.iloc[0] = float(initial_capital)
        actual_returns = daily_frame["daily_return"].astype(float)

    def dollar_stream(frame, column: str) -> pd.Series:
        if daily_frame.empty or frame.empty:
            return pd.Series(0.0, index=daily_frame.index, dtype=float)
        values = frame.groupby("date")[column].sum().astype(float)
        return values.reindex(daily_frame.index).fillna(0.0)

    gross_daily = dollar_stream(pnl_events, "gross_pnl")
    gross_return_proxy = (
        gross_daily.div(previous_equity.replace(0.0, pd.NA)).fillna(0.0)
        if not daily_frame.empty
        else gross_daily
    )

    by_action: dict[str, dict] = {}
    if not trade_events.empty:
        for action, frame in trade_events.groupby("action", sort=True):
            cost_daily = dollar_stream(frame, "transaction_cost")
            if not daily_frame.empty:
                cost_return = cost_daily.div(previous_equity.replace(0.0, pd.NA)).fillna(0.0)
                no_action_cost = actual_returns + cost_return
                drag = _annualized_return(no_action_cost) - _annualized_return(actual_returns)
            else:
                drag = 0.0
            by_action[str(action)] = {
                "execution_events": int(len(frame)),
                "affected_days": int(frame["date"].nunique()),
                "turnover_notional": float(frame["turnover_notional"].sum()),
                "cost": float(frame["transaction_cost"].sum()),
                "annualized_return_drag_proxy": float(drag),
            }
    total_cost = float(trade_events["transaction_cost"].sum()) if not trade_events.empty else 0.0
    total_turnover = (
        float(trade_events["turnover_notional"].sum()) if not trade_events.empty else 0.0
    )
    if not daily_frame.empty:
        total_cost_daily = dollar_stream(trade_events, "transaction_cost")
        total_cost_return = total_cost_daily.div(previous_equity.replace(0.0, pd.NA)).fillna(0.0)
        no_cost_ann = _annualized_return(actual_returns + total_cost_return)
        actual_ann = _annualized_return(actual_returns)
        total_cost_drag = no_cost_ann - actual_ann
    else:
        total_cost_drag = 0.0

    capacity_columns = (
        "integer_rounding_loss_notional",
        "max_volume_clipping_notional",
        "unavailable_contract_notional",
        "margin_capacity_loss_notional",
        "lot_stabilization_loss_notional",
    )
    capacity: dict[str, float | int] = {}
    for column in capacity_columns:
        values = (
            daily_frame[column].astype(float)
            if column in daily_frame
            else pd.Series(0.0, index=daily_frame.index, dtype=float)
        )
        capacity[column] = float(values.sum())
        capacity[f"{column}_affected_days"] = int((values > 1e-12).sum())
    raw_ratios = (
        daily_frame["raw_target_gross_ratio"].astype(float)
        if "raw_target_gross_ratio" in daily_frame
        else pd.Series(dtype=float)
    )
    governor_ratios = (
        daily_frame["governor_target_gross_ratio"].astype(float)
        if "governor_target_gross_ratio" in daily_frame
        else pd.Series(dtype=float)
    )
    realized_ratios = (
        daily_frame["gross_notional"]
        .astype(float)
        .div(daily_frame["equity"].replace(0.0, pd.NA))
        .fillna(0.0)
        if not daily_frame.empty and "gross_notional" in daily_frame
        else pd.Series(dtype=float)
    )
    capacity.update(
        {
            "average_raw_target_gross_ratio": float(raw_ratios.mean())
            if not raw_ratios.empty
            else 0.0,
            "peak_raw_target_gross_ratio": float(raw_ratios.max()) if not raw_ratios.empty else 0.0,
            "average_governor_target_gross_ratio": float(governor_ratios.mean())
            if not governor_ratios.empty
            else 0.0,
            "peak_governor_target_gross_ratio": float(governor_ratios.max())
            if not governor_ratios.empty
            else 0.0,
            "average_realized_gross_ratio": float(realized_ratios.mean())
            if not realized_ratios.empty
            else 0.0,
            "peak_realized_gross_ratio": float(realized_ratios.max())
            if not realized_ratios.empty
            else 0.0,
        }
    )
    if not daily_frame.empty:
        governor_denied_ratio = (raw_ratios - governor_ratios).clip(lower=0.0)
        capacity["governor_denied_gross_ratio_days"] = float(governor_denied_ratio.sum())
        capacity["governor_affected_days"] = int((governor_denied_ratio > 1e-12).sum())
    else:
        capacity["governor_denied_gross_ratio_days"] = 0.0
        capacity["governor_affected_days"] = 0

    risk_actions = {
        action: by_action[action]
        for action in ("daily_circuit", "gross_guard", "hard_halt")
        if action in by_action
    }
    return {
        "alpha": {
            "gross_signal_pnl": gross_signal_pnl,
            "gross_signal_annualized_return_proxy": _annualized_return(gross_return_proxy),
            "long_pnl": long_pnl,
            "short_pnl": short_pnl,
            "product_pnl": product_pnl,
        },
        "transaction_cost": {
            "turnover_notional": total_turnover,
            "total_cost": total_cost,
            "commission_slippage_proxy": total_cost,
            "annualized_return_drag_proxy": float(total_cost_drag),
            "by_action": by_action,
        },
        "capacity": capacity,
        "risk_actions": risk_actions,
        "activity": {
            "execution_event_count": int(len(trade_events)),
            "affected_trade_days": int(trade_events["date"].nunique())
            if not trade_events.empty
            else 0,
            "average_holding_sessions": _holding_sessions(daily_frame, pnl_events),
        },
        "robustness_diagnostics": _robustness_diagnostics(
            daily=daily,
            pnl_events=pnl_events,
            gross_signal_pnl=gross_signal_pnl,
            initial_capital=initial_capital,
        ),
    }
