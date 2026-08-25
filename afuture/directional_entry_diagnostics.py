"""Research-only entry/exit diagnostics for directional production events.

``feature_*`` values use only completed history strictly before an event date.
``label_*`` values may use future sessions and must never enter production decisions.
"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd


def _market(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.normalize()
    for name in ("open", "high", "low", "close"):
        if name in out:
            out[name] = pd.to_numeric(out[name], errors="coerce")
    for name in ("product", "symbol"):
        if name in out:
            out[name] = out[name].astype(str).str.upper()
    return out.dropna(subset=["date"]).sort_values("date")


def _role(row: pd.Series) -> str:
    action = str(row.get("action", ""))
    before, after = int(row.get("lots_before", 0)), int(row.get("lots_after", 0))
    if action in {"entry", "exit"}:
        return action
    if action == "reversal":
        if before == 0 and after != 0:
            return "entry"
        if before != 0 and after == 0:
            return "exit"
    return ""


def _sign(side: str) -> float:
    return 1.0 if str(side).lower() == "long" else -1.0


def _completed_features(history: pd.DataFrame, day: pd.Timestamp, side: str) -> dict:
    completed = history[history["date"] < day]
    close = completed["close"].astype(float) if not completed.empty else pd.Series(dtype=float)

    def trailing(n: int) -> float:
        return float(close.iloc[-1] / close.iloc[-1 - n] - 1.0) if len(close) > n else 0.0

    changes = close.pct_change(fill_method=None).dropna()
    vol20 = float(changes.iloc[-20:].std(ddof=1)) if len(changes) >= 20 else 0.0
    prior_range = 0.0
    if not completed.empty and float(completed.iloc[-1]["close"]) > 0:
        last = completed.iloc[-1]
        prior_range = float((float(last["high"]) - float(last["low"])) / float(last["close"]))
    ret20 = trailing(20)
    return {
        "feature_completed_history_sessions": int(len(completed)),
        "feature_prior_1_return": trailing(1),
        "feature_prior_5_return": trailing(5),
        "feature_prior_20_return": ret20,
        "feature_prior_20_vol": vol20,
        "feature_prior_day_range": prior_range,
        "feature_trend_alignment_20": float(_sign(side) * ret20),
    }


def _forward_labels(
    contract: pd.DataFrame,
    day: pd.Timestamp,
    price: float,
    side: str,
    horizons: Iterable[int],
    round_trip_cost: float,
) -> dict:
    future = contract[contract["date"] >= day].sort_values("date")
    sign, out = _sign(side), {}
    for raw in horizons:
        h = int(raw)
        if h <= 0:
            raise ValueError("diagnostic horizons must be positive")
        available = len(future) >= h and price > 0
        gross = net = mfe = mae = 0.0
        if available:
            window = future.iloc[:h]
            gross = float(sign * (float(window.iloc[-1]["close"]) / price - 1.0))
            extremes = pd.concat(
                [
                    sign * (window["high"].astype(float) / price - 1.0),
                    sign * (window["low"].astype(float) / price - 1.0),
                ],
                ignore_index=True,
            )
            mfe, mae = float(extremes.max()), float(extremes.min())
            net = float(gross - round_trip_cost)
        out.update(
            {
                f"label_available_h{h}": bool(available),
                f"label_gross_return_h{h}": gross,
                f"label_net_return_h{h}": net,
                f"label_mfe_h{h}": mfe,
                f"label_mae_h{h}": mae,
                f"label_winner_after_cost_h{h}": bool(available and net > 0.0),
            }
        )
    return out


def label_directional_entry_exit_events(
    *,
    events: pd.DataFrame,
    specific_contracts: pd.DataFrame,
    product_history: pd.DataFrame,
    horizons: tuple[int, ...] = (1, 3, 5, 10),
    stress_cost_bps: float = 15.0,
    rapid_reentry_sessions: int = 3,
) -> pd.DataFrame:
    """Attach causal pre-trade features and explicitly future-only labels."""
    if stress_cost_bps < 0:
        raise ValueError("stress_cost_bps must be non-negative")
    if rapid_reentry_sessions < 1:
        raise ValueError("rapid_reentry_sessions must be positive")
    if events.empty:
        return pd.DataFrame()

    event = events.copy()
    event["date"] = pd.to_datetime(event["date"], errors="coerce").dt.normalize()
    event["product"] = event["product"].astype(str).str.upper()
    event["symbol"] = event["symbol"].astype(str).str.upper()
    event = event[event["kind"].astype(str) == "trade"].copy()
    event["event_role"] = event.apply(_role, axis=1)
    event = (
        event[event["event_role"] != ""].sort_values("date", kind="stable").reset_index(drop=True)
    )
    if event.empty:
        return event

    contracts, history = _market(specific_contracts), _market(product_history)
    by_symbol = {str(k): v.sort_values("date") for k, v in contracts.groupby("symbol", sort=False)}
    by_product = {str(k): v.sort_values("date") for k, v in history.groupby("product", sort=False)}
    sessions = pd.DatetimeIndex(sorted(history["date"].dropna().unique()))
    position = {pd.Timestamp(day).normalize(): i for i, day in enumerate(sessions)}
    empty_contract = pd.DataFrame(columns=contracts.columns)
    empty_history = pd.DataFrame(columns=history.columns)
    round_trip = 2.0 * float(stress_cost_bps) / 10000.0
    last_exit: dict[tuple[str, str], int] = {}
    last_entry: dict[tuple[str, str], int] = {}
    rows: list[dict] = []

    for _, source in event.iterrows():
        day = pd.Timestamp(source["date"]).normalize()
        product, side, role = str(source["product"]), str(source["side"]), str(source["event_role"])
        pos = position.get(day, -1)
        row = source.to_dict()
        row.update(_completed_features(by_product.get(product, empty_history), day, side))

        previous_exit = last_exit.get((product, side), -1)
        since_exit = pos - previous_exit if pos >= 0 and previous_exit >= 0 else -1
        row["feature_sessions_since_same_side_exit"] = int(since_exit)
        row["feature_is_rapid_reentry"] = bool(
            role == "entry" and 0 <= since_exit <= rapid_reentry_sessions
        )

        previous_entry = last_entry.get((product, side), -1)
        since_entry = pos - previous_entry if pos >= 0 and previous_entry >= 0 else -1
        row["feature_sessions_since_same_side_entry"] = int(since_entry if role == "exit" else -1)
        row["feature_is_short_hold_exit"] = bool(
            role == "exit" and 0 <= since_entry <= rapid_reentry_sessions
        )
        row["feature_round_trip_cost"] = round_trip
        row.update(
            _forward_labels(
                by_symbol.get(str(source["symbol"]), empty_contract),
                day,
                float(source["price"]),
                side,
                horizons,
                round_trip,
            )
        )
        if role == "entry":
            last_entry[(product, side)] = pos
        else:
            last_exit[(product, side)] = pos
            last_entry.pop((product, side), None)
        rows.append(row)

    labeled = pd.DataFrame(rows)
    labeled["label_holding_sessions_to_exit"] = -1
    for i, row in labeled.iterrows():
        role = str(row["event_role"])
        day = pd.Timestamp(row["date"]).normalize()
        product, side = str(row["product"]), str(row["side"])
        pos = position.get(day, -1)
        if role == "entry":
            later = labeled.iloc[i + 1 :]
            later = later[
                (later["product"] == product)
                & (later["side"] == side)
                & (later["event_role"] == "exit")
            ]
            if not later.empty and pos >= 0:
                exit_pos = position.get(pd.Timestamp(later.iloc[0]["date"]).normalize(), -1)
                if exit_pos >= pos:
                    labeled.loc[i, "label_holding_sessions_to_exit"] = int(exit_pos - pos)
            labeled.loc[i, ["label_same_side_reentry_delay_sessions"]] = -1
            labeled.loc[
                i, ["label_rapid_same_side_reentry_within_3", "label_genuine_reversal_within_1"]
            ] = False
            continue

        later = labeled.iloc[i + 1 :]
        later = later[(later["product"] == product) & (later["event_role"] == "entry")]
        same_delay, opposite_one = -1, False
        for _, candidate in later.iterrows():
            cpos = position.get(pd.Timestamp(candidate["date"]).normalize(), -1)
            delay = cpos - pos if cpos >= 0 and pos >= 0 else -1
            if delay < 0:
                continue
            if str(candidate["side"]) == side and same_delay < 0:
                same_delay = int(delay)
            if str(candidate["side"]) != side and delay <= 1:
                opposite_one = True
            if same_delay >= 0 and (opposite_one or delay > rapid_reentry_sessions):
                break
        labeled.loc[i, "label_same_side_reentry_delay_sessions"] = same_delay
        labeled.loc[i, "label_rapid_same_side_reentry_within_3"] = bool(
            0 <= same_delay <= rapid_reentry_sessions
        )
        labeled.loc[i, "label_genuine_reversal_within_1"] = opposite_one

    h5 = 5 if 5 in horizons else max(int(x) for x in horizons)
    available = labeled[f"label_available_h{h5}"].astype(bool)
    continuation = labeled[f"label_gross_return_h{h5}"].astype(float) > 0.0
    is_entry, is_exit = labeled["event_role"].eq("entry"), labeled["event_role"].eq("exit")
    labeled[f"label_trend_continuation_exit_h{h5}"] = is_exit & available & continuation
    labeled[f"label_trend_exhaustion_exit_h{h5}"] = is_exit & available & ~continuation
    labeled[f"label_false_breakout_entry_h{h5}"] = (
        is_entry
        & available
        & (labeled[f"label_net_return_h{h5}"].astype(float) < 0.0)
        & (labeled[f"label_mfe_h{h5}"].astype(float) < round_trip)
    )
    labeled[f"label_temporary_displacement_h{h5}"] = (
        is_exit
        & labeled["label_rapid_same_side_reentry_within_3"].astype(bool)
        & labeled[f"label_trend_continuation_exit_h{h5}"].astype(bool)
    )
    labeled["label_meta_cause_reliably_reconstructable"] = False
    return labeled


def _horizons(frame: pd.DataFrame, horizons: Iterable[int]) -> dict:
    out = {}
    for raw in horizons:
        h = int(raw)
        available = frame[frame[f"label_available_h{h}"].astype(bool)].copy()
        turnover = (
            float(available["turnover_notional"].astype(float).sum())
            if not available.empty
            else 0.0
        )
        net = (
            available[f"label_net_return_h{h}"].astype(float)
            if not available.empty
            else pd.Series(dtype=float)
        )
        out[str(h)] = {
            "available_count": int(len(available)),
            "turnover_notional": turnover,
            "mean_gross_return": float(available[f"label_gross_return_h{h}"].astype(float).mean())
            if not available.empty
            else 0.0,
            "mean_net_return": float(net.mean()) if not net.empty else 0.0,
            "median_net_return": float(net.median()) if not net.empty else 0.0,
            "turnover_weighted_net_return": float(
                (net * available["turnover_notional"].astype(float)).sum() / turnover
            )
            if turnover > 0
            else 0.0,
            "win_rate_after_cost": float(
                available[f"label_winner_after_cost_h{h}"].astype(bool).mean()
            )
            if not available.empty
            else 0.0,
            "mean_mfe": float(available[f"label_mfe_h{h}"].astype(float).mean())
            if not available.empty
            else 0.0,
            "mean_mae": float(available[f"label_mae_h{h}"].astype(float).mean())
            if not available.empty
            else 0.0,
        }
    return out


def _quality(frame: pd.DataFrame, horizons: Iterable[int]) -> dict:
    holding = pd.Series(dtype=float)
    if "label_holding_sessions_to_exit" in frame:
        values = frame["label_holding_sessions_to_exit"].astype(float)
        holding = values[values >= 0]
    return {
        "count": int(len(frame)),
        "turnover_notional": float(frame["turnover_notional"].astype(float).sum())
        if not frame.empty
        else 0.0,
        "average_holding_sessions": float(holding.mean()) if not holding.empty else 0.0,
        "horizons": _horizons(frame, horizons),
    }


def _flag(frame: pd.DataFrame, column: str) -> dict:
    if column not in frame:
        return {"count": 0, "turnover_notional": 0.0}
    selected = frame[frame[column].map(lambda x: bool(x) if pd.notna(x) else False)]
    return {
        "count": int(len(selected)),
        "turnover_notional": float(selected["turnover_notional"].astype(float).sum())
        if not selected.empty
        else 0.0,
    }


def summarize_entry_exit_quality(
    labeled: pd.DataFrame,
    *,
    horizons: tuple[int, ...] = (1, 3, 5, 10),
) -> dict:
    """Summarize future outcomes without mixing them into causal cohort membership."""
    if labeled.empty:
        empty = _quality(labeled, horizons)
        return {"entries": empty, "exits": empty, "meta_cause_reliably_reconstructable": False}

    frame = labeled.copy()
    entries, exits = (
        frame[frame["event_role"] == "entry"].copy(),
        frame[frame["event_role"] == "exit"].copy(),
    )
    rapid_mask = entries["feature_is_rapid_reentry"].fillna(False).astype(bool)
    trend = entries["feature_trend_alignment_20"].astype(float)
    cost = entries["feature_round_trip_cost"].astype(float)
    entry = _quality(entries, horizons)
    entry["causal_cohorts"] = {
        "rapid_reentry": _quality(entries[rapid_mask], horizons),
        "fresh_entry": _quality(entries[~rapid_mask], horizons),
        "trend_aligned_20": _quality(entries[trend > 0.0], horizons),
        "countertrend_20": _quality(entries[trend <= 0.0], horizons),
        "trailing_20_edge_covers_round_trip_cost": _quality(entries[trend > cost], horizons),
        "trailing_20_edge_below_round_trip_cost": _quality(entries[trend <= cost], horizons),
    }
    h5 = 5 if 5 in horizons else max(int(x) for x in horizons)
    entry["future_only_labels"] = {
        f"false_breakout_h{h5}": _flag(entries, f"label_false_breakout_entry_h{h5}")
    }

    age = exits["feature_sessions_since_same_side_entry"].astype(float)
    exit_trend = exits["feature_trend_alignment_20"].astype(float)
    short_mask = exits["feature_is_short_hold_exit"].fillna(False).astype(bool)
    exit_summary = _quality(exits, horizons)
    exit_summary["causal_cohorts"] = {
        "short_hold_exit_le_3": _quality(exits[short_mask], horizons),
        "established_exit_gt_3": _quality(exits[age > 3.0], horizons),
        "unknown_position_age": _quality(exits[age < 0.0], horizons),
        "trend_aligned_20": _quality(exits[exit_trend > 0.0], horizons),
        "countertrend_20": _quality(exits[exit_trend <= 0.0], horizons),
    }
    exit_summary["future_only_labels"] = {
        "rapid_same_side_reentry_within_3": _flag(exits, "label_rapid_same_side_reentry_within_3"),
        "genuine_reversal_within_1": _flag(exits, "label_genuine_reversal_within_1"),
        f"temporary_displacement_h{h5}": _flag(exits, f"label_temporary_displacement_h{h5}"),
        f"trend_continuation_exit_h{h5}": _flag(exits, f"label_trend_continuation_exit_h{h5}"),
        f"trend_exhaustion_exit_h{h5}": _flag(exits, f"label_trend_exhaustion_exit_h{h5}"),
    }
    return {
        "entries": entry,
        "exits": exit_summary,
        "meta_cause_reliably_reconstructable": bool(
            frame["label_meta_cause_reliably_reconstructable"].fillna(False).astype(bool).all()
        ),
        "future_labels_are_production_inputs": False,
    }
