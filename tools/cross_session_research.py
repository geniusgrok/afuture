"""Offline causal SC/January WTI residual research with owned five-minute cash.

This is a historical vendor-price/delay proxy, never a live account owner. SC
15-minute CLOSE observations may calibrate the response; only the separate
five-minute OPEN quotations may fill the account. Missing held quotations fail
rather than delete a date. Parameters are fixed before inspecting cash results.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from math import isfinite, log, sqrt
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from adaptive_alpha_research import account_metrics, write_json

from afuture.new_mechanisms import Observation, cross_session_residual
from afuture.runtime_calendar import RuntimeTradingCalendar

INITIAL = 500000.0
UNIT = 1000
LAG = pd.Timedelta(minutes=15)
MINIMUM = 12
MAXIMUM = 60
ENTRY = "09:45"
EXIT = "14:30"
EXPECTED_5M = tuple(
    [f"09:{m:02}" for m in range(5, 60, 5)]
    + [f"10:{m:02}" for m in (0, 5, 10, 15, 35, 40, 45, 50, 55)]
    + [f"11:{m:02}" for m in (0, 5, 10, 15, 20, 25, 30)]
    + [f"13:{m:02}" for m in (35, 40, 45, 50, 55)]
    + [f"14:{m:02}" for m in range(0, 60, 5)]
    + ["15:00"]
)
PROTOCOL = {
    "mechanisms": ["XS1", "XS2"],
    "primary_foreign": "CLF27.NYM January 2027",
    "domestic": "SC2701 January 2027",
    "fx_routes": ["USD/CNY", "USD/CNH explicit offshore proxy"],
    "source_lag_seconds_proxy": 900,
    "domestic_closed": "last actual completed SC session bar before09:00; normally02:30, or15:00 when no night session; retain weekend/holiday closure",
    "domestic_reopened": "09:00 Asia/Shanghai",
    "observed_response_completed": "09:15 Asia/Shanghai",
    "decision": "09:30:01 Asia/Shanghai",
    "entry": "09:45 actual five-minute bar open (end stamp09:50)",
    "exit": "14:30 actual five-minute bar open (end stamp14:35)",
    "foreign_interval": "completed bars inside actual closed_at..09:00; no09:00 start bar close",
    "domestic_clock_qualification": "both bar_start+1us and bar_end-1us must belong to same physical SC RuntimeTradingCalendar session; reject dated markers, never backdate",
    "fx_alignment": "start no later than foreign start; end no earlier than foreign end, <=09:00",
    "fx_max_staleness_seconds": 300,
    "beta": "no-intercept OLS on prior12..60 completed joint response dates",
    "XS1": "beta*log(WTI*FX change)-already observed domestic reopening response",
    "XS2": "prior12..60 mature actual-five-minute residual/remaining-return pairs; no-intercept ridge1",
    "XS3": "same mature residual ridge with prior SC15m09:45close->14:30close MARKET LABEL proxy, real5m account fills unchanged",
    "XS3_label_maturity": "14:30 close completion+15min strictly before current decision",
    "XS2_scale": "sqrt(mean(training residual squared)), minimum1e-6",
    "forecast_entry_gate": "abs(expected remaining return)>0.003, fixed Stress roundtrip gate",
    "initial_cash": INITIAL,
    "multiplier_barrels": UNIT,
    "maximum_absolute_lots": 1,
    "maximum_gross_notional": 1000000,
    "maximum_margin_fraction": 0.35,
    "margin_buffer": 1.25,
    "base": {"one_way_cost_bps": 5, "margin_proxy": 0.12},
    "stress": {"one_way_cost_bps": 15, "margin_proxy": 0.15},
    "hard_drawdown": 0.20,
    "risk_execution": "after completed held five-minute bar plus15min lag, next actual open strictly later",
    "development_only": True,
    "first_observation_dates": "warmup only until actual5m day-session coverage starts; all later domestic calendar dates retained",
    "missing_future_fill": "invalid account with partial cash preserved, never ex-ante entry filtering",
    "fallback": "run XS2 after XS1 failure without retuning gate, minimum history, clocks, or fees",
}


def stamp(day, clock):
    return pd.Timestamp(f"{pd.Timestamp(day).date()} {clock}", tz="Asia/Shanghai").tz_convert("UTC")


def identity(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_normalized(path):
    frame = pd.read_csv(path, float_precision="round_trip").rename(
        columns={"bar_start": "bar_start_utc", "bar_end": "bar_end_utc"}
    )
    for key in ("bar_start_utc", "bar_end_utc"):
        frame[key] = pd.to_datetime(frame[key], utc=True)
    if frame.bar_end_utc.duplicated().any():
        raise ValueError("duplicate market bars")
    if not (frame.bar_end_utc > frame.bar_start_utc).all():
        raise ValueError("bar completion precedes start")
    if "source_sha256" not in frame or not frame.source_sha256.str.fullmatch("[0-9a-f]{64}").all():
        raise ValueError("source byte identity required")
    return frame.sort_values("bar_end_utc").reset_index(drop=True)


def read_domestic_five_minute(path, source_sha256):
    frame = pd.read_csv(path, float_precision="round_trip")
    if set(frame.symbol) != {"SC2701"}:
        raise ValueError("five-minute execution source is not the registered concrete contract")
    frame["bar_end_utc"] = (
        pd.to_datetime(frame.datetime).dt.tz_localize("Asia/Shanghai").dt.tz_convert("UTC")
    )
    frame["bar_start_utc"] = frame.bar_end_utc - pd.Timedelta(minutes=5)
    frame["source_sha256"] = source_sha256
    if len(source_sha256) != 64 or frame.bar_end_utc.duplicated().any():
        raise ValueError("execution source identity or unique bar clock missing")
    return frame.sort_values("bar_end_utc").reset_index(drop=True)


def valid(row):
    return all(
        isfinite(float(row[key])) and float(row[key]) > 0
        for key in ("open", "high", "low", "close")
    )


def observation(row):
    return Observation(
        float(row.close),
        row.bar_end_utc.to_pydatetime(),
        (row.bar_end_utc + LAG).to_pydatetime(),
        row.source_sha256,
    )


def qualify_domestic_clock(frame, calendar):
    result = frame.copy()
    flags = []
    for row in result.itertuples():
        begin = calendar.expected_trading_day(
            "SC2701", (row.bar_start_utc + pd.Timedelta(microseconds=1)).to_pydatetime()
        )
        end = calendar.expected_trading_day(
            "SC2701", (row.bar_end_utc - pd.Timedelta(microseconds=1)).to_pydatetime()
        )
        flags.append(begin is not None and begin == end)
    result["physical_session_qualified"] = flags
    return result


def select_closure(foreign, fx, closed, opened):
    """Joint causal references with a maximum five-minute FX mismatch."""
    f = foreign.loc[(foreign.bar_start_utc >= closed) & (foreign.bar_end_utc <= opened)].copy()
    f = f.loc[f.apply(valid, axis=1)]
    x = fx.loc[
        (fx.bar_end_utc <= opened) & (fx.bar_end_utc >= closed - pd.Timedelta(minutes=5))
    ].copy()
    x = x.loc[x.apply(valid, axis=1)]
    if len(f) < 2 or len(x) < 2:
        raise ValueError("insufficient joint foreign/FX closure bars")
    begin = f.iloc[0]
    if begin.bar_end_utc - closed > pd.Timedelta(minutes=5):
        raise ValueError("missing fresh foreign closure starting reference")
    starts = x.loc[x.bar_end_utc <= begin.bar_end_utc]
    if starts.empty or begin.bar_end_utc - starts.iloc[-1].bar_end_utc > pd.Timedelta(minutes=5):
        raise ValueError("missing timely FX starting reference")
    end_fx = x.iloc[-1]
    ends = f.loc[f.bar_end_utc <= end_fx.bar_end_utc]
    if ends.empty:
        raise ValueError("missing joint foreign endpoint")
    end = ends.iloc[-1]
    if opened - end.bar_end_utc > pd.Timedelta(minutes=5):
        raise ValueError("missing fresh foreign reopening ending reference")
    if (
        end_fx.bar_end_utc - end.bar_end_utc > pd.Timedelta(minutes=5)
        or end.bar_end_utc <= begin.bar_end_utc
    ):
        raise ValueError("missing timely joint FX/foreign ending reference")
    return begin, end, starts.iloc[-1], end_fx


def build_observations(domestic, foreign, fx, executions):
    calendar = RuntimeTradingCalendar.load()
    domestic = qualify_domestic_clock(domestic, calendar)
    domestic = domestic.loc[domestic.physical_session_qualified].copy()
    cn = domestic.set_index("bar_end_utc", drop=False)
    fill = executions.set_index("bar_start_utc", drop=False)
    local = domestic.bar_end_utc.dt.tz_convert("Asia/Shanghai")
    # Calendar is derived from domestic day-session observations, never from PnL,
    # later foreign/FX availability, or a future remaining-return endpoint.
    covered = local.loc[(local.dt.hour >= 9) & (local.dt.hour <= 15)].dt.date
    days = [day for day in calendar.open_days["INE"] if covered.min() <= day <= covered.max()]
    rows = []
    for day in days:
        decision = stamp(day, "09:30:01")
        record = {
            "date": pd.Timestamp(day),
            "decision": decision,
            "entry_at": stamp(day, ENTRY),
            "exit_at": stamp(day, EXIT),
            "status": "missing_domestic_response",
            "x": np.nan,
            "y": np.nan,
            "response_available": stamp(day, "09:15") + LAG,
            "target": np.nan,
            "target_close": np.nan,
            "target_close_matured_at": stamp(day, EXIT) + LAG,
            "target_close_status": "missing_close_market_label_endpoint",
            "target_status": "missing_actual_five_minute_endpoint",
            "target_matured_at": stamp(day, EXIT) + pd.Timedelta(minutes=5) + LAG,
        }
        prior_closes = domestic.loc[
            (domestic.bar_end_utc < stamp(day, "09:00"))
            & (domestic.bar_end_utc >= stamp(day, "09:00") - pd.Timedelta(days=7))
        ]
        start = prior_closes.iloc[-1] if len(prior_closes) else None
        response = cn.loc[stamp(day, "09:15")] if stamp(day, "09:15") in cn.index else None
        record["closed_at"] = start.bar_end_utc if start is not None else pd.NaT
        if start is not None and response is not None and valid(start) and valid(response):
            try:
                a, b, c, d = select_closure(foreign, fx, record["closed_at"], stamp(day, "09:00"))
            except ValueError as error:
                record["status"] = str(error)
            else:
                points = tuple(observation(p) for p in (a, b, c, d, start, response))
                for name, point in zip(
                    (
                        "foreign_start",
                        "foreign_end",
                        "fx_start",
                        "fx_end",
                        "domestic_start",
                        "domestic_response",
                    ),
                    points,
                    strict=True,
                ):
                    record[name + "_value"] = point.value
                    record[name + "_measured"] = point.measured_at
                    record[name + "_available"] = point.available_at
                    record[name + "_sha256"] = point.source_sha256
                record.update(
                    status="qualified_observations",
                    x=log(b.close * d.close / (a.close * c.close)),
                    y=log(response.close / start.close),
                )
        entry, exit_row = (
            fill.loc[record["entry_at"]] if record["entry_at"] in fill.index else None,
            fill.loc[record["exit_at"]] if record["exit_at"] in fill.index else None,
        )
        if entry is not None and exit_row is not None and valid(entry) and valid(exit_row):
            record.update(
                target=float(exit_row.open / entry.open - 1),
                target_status="actual_five_minute_endpoints",
                target_entry_price=float(entry.open),
                target_exit_price=float(exit_row.open),
            )
        close_entry_at, close_exit_at = stamp(day, ENTRY), stamp(day, EXIT)
        if close_entry_at in cn.index and close_exit_at in cn.index:
            close_entry, close_exit = cn.loc[close_entry_at], cn.loc[close_exit_at]
            if valid(close_entry) and valid(close_exit):
                record.update(
                    target_close=float(close_exit.close / close_entry.close - 1),
                    target_close_status="real_close_market_label_proxy",
                )
        rows.append(record)
    return pd.DataFrame(rows)


def forecasts(observations, mechanism):
    data = observations.copy()
    data["residual"] = np.nan
    data["forecast"] = np.nan
    data["forecast_status"] = "missing_current_observations"
    audit = []
    for index, row in data.iterrows():
        fit = {"date": row.date, "mechanism": mechanism, "response_dates": 0, "remaining_dates": 0}
        if row.status != "qualified_observations":
            audit.append(fit)
            continue
        prior = data.loc[
            (data.date < row.date)
            & (data.response_available < row.decision)
            & data.status.eq("qualified_observations")
        ].tail(MAXIMUM)
        fit["response_dates"] = len(prior)
        if len(prior) < MINIMUM:
            data.loc[index, "forecast_status"] = "insufficient_mature_response_dates"
            audit.append(fit)
            continue
        denominator = float(np.dot(prior.x, prior.x))
        if denominator <= 1e-12:
            data.loc[index, "forecast_status"] = "degenerate_prior_foreign_changes"
            audit.append(fit)
            continue
        beta = float(np.dot(prior.x, prior.y) / denominator)
        points = [
            Observation(
                float(row[name + "_value"]),
                row[name + "_measured"].to_pydatetime(),
                row[name + "_available"].to_pydatetime(),
                row[name + "_sha256"],
            )
            for name in (
                "foreign_start",
                "foreign_end",
                "fx_start",
                "fx_end",
                "domestic_start",
                "domestic_response",
            )
        ]
        residual = cross_session_residual(
            (points[0], points[1]),
            (points[2], points[3]),
            (points[4], points[5]),
            decision=row.decision.to_pydatetime(),
            closure_started_at=row.closed_at.to_pydatetime(),
            domestic_opened_at=stamp(row.date, "09:00").to_pydatetime(),
            beta=beta,
            calibration_matured_at=prior.response_available.max().to_pydatetime(),
        )
        data.loc[index, "residual"] = residual
        fit.update(beta=beta, beta_max_maturity=prior.response_available.max())
        if mechanism == "XS1":
            predicted = residual
        elif mechanism in ("XS2", "XS3"):
            label = "target" if mechanism == "XS2" else "target_close"
            maturity = "target_matured_at" if mechanism == "XS2" else "target_close_matured_at"
            mature = data.loc[
                (data.date < row.date)
                & (data[maturity] < row.decision)
                & np.isfinite(data.residual)
                & np.isfinite(data[label])
            ].tail(MAXIMUM)
            fit["remaining_dates"] = len(mature)
            if len(mature) < MINIMUM:
                data.loc[index, "forecast_status"] = (
                    "insufficient_actual_mature_remaining_dates"
                    if mechanism == "XS2"
                    else "insufficient_mature_close_proxy_remaining_dates"
                )
                audit.append(fit)
                continue
            scale = max(sqrt(float(np.mean(mature.residual.to_numpy() ** 2))), 1e-6)
            z = mature.residual.to_numpy() / scale
            coefficient = float(np.dot(z, mature[label]) / (np.dot(z, z) + 1.0))
            predicted = residual / scale * coefficient
            fit.update(
                scale=scale,
                ridge_coefficient=coefficient,
                remaining_max_maturity=mature[maturity].max(),
                label_kind="actual_five_minute_fill_return"
                if mechanism == "XS2"
                else "fifteen_minute_close_market_proxy",
            )
        else:
            raise ValueError("unregistered mechanism")
        data.loc[index, "forecast"] = predicted
        data.loc[index, "forecast_status"] = "mature_prediction"
        audit.append(fit)
    return data, pd.DataFrame(audit)


def replay(executions, predictions, days, *, cost_bps, margin, failure_output=None):
    bars = executions.set_index("bar_start_utc", drop=False)
    decisions: list[dict[str, Any]] = []
    ledger: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    cash, peak, halted, marked_peak = INITIAL, INITIAL, False, INITIAL
    max_marked_dd = 0.0
    chosen = predictions.set_index("date")

    def preserve(error):
        if failure_output is not None:
            pd.DataFrame(events).to_csv(failure_output / "partial_events.csv", index=False)
            pd.DataFrame(ledger).to_csv(failure_output / "partial_ledger.csv", index=False)
            pd.DataFrame(decisions).to_csv(failure_output / "partial_decisions.csv", index=False)
            write_json(
                failure_output / "partial_state.json",
                {
                    "cash": cash,
                    "peak": peak,
                    "halted": halted,
                    "error": str(error),
                    "account_valid": False,
                },
            )
        raise ValueError(str(error))

    def quote(at):
        if at not in bars.index or not valid(bars.loc[at]):
            preserve(f"missing held five-minute quotation {at}")
        return bars.loc[at]

    for day in days:
        before = cash
        row = chosen.loc[pd.Timestamp(day)]
        predicted = row.forecast
        reason = (
            "no_mature_prediction"
            if not np.isfinite(predicted)
            else "below_fixed_cost_gate"
            if abs(predicted) <= 0.003
            else "hard_halt"
            if halted
            else "accepted"
        )
        decisions.append(
            {
                "date": day,
                "decision": row.decision,
                "forecast": predicted,
                "forecast_status": row.forecast_status,
                "reason": reason,
            }
        )
        start_event = len(events)
        if reason == "accepted":
            entry_at, exit_at = row.entry_at, row.exit_at
            if entry_at <= row.decision:
                preserve("entry is not strictly after signal availability/decision")
            entry = quote(entry_at)
            notional = float(entry.open) * UNIT
            fee = notional * cost_bps / 10000
            if notional > 1000000 or notional * margin * 1.25 > max(cash - fee, 0) * 0.35:
                decisions[-1]["reason"] = "shared_integer_capacity"
            else:
                lots = 1 if predicted > 0 else -1
                last_price = float(entry.open)
                owned = lots
                pending_exit = None
                pending_reason = "planned_exit"

                def event(
                    at,
                    bar,
                    action,
                    price,
                    lots_before,
                    lots_after,
                    pnl,
                    turnover=0.0,
                    event_day=day,
                ):
                    nonlocal cash, marked_peak, max_marked_dd
                    paid = turnover * cost_bps / 10000
                    cash += pnl - paid
                    marked_peak = max(marked_peak, cash)
                    max_marked_dd = max(max_marked_dd, 1 - cash / marked_peak)
                    events.append(
                        {
                            "date": event_day,
                            "timestamp": at,
                            "bar_start": bar.bar_start_utc,
                            "bar_end": bar.bar_end_utc,
                            "symbol": "SC2701",
                            "action": action,
                            "kind": "trade" if lots_before != lots_after else "pnl",
                            "unit": UNIT,
                            "lots_before": lots_before,
                            "lots_after": lots_after,
                            "price": price,
                            "gross_pnl": pnl,
                            "turnover": turnover,
                            "transaction_cost": paid,
                            "cash": cash,
                            "source_sha256": bar.source_sha256,
                        }
                    )

                if entry.volume < 1:
                    preserve(
                        "chosen actual fill has less than one total-bar contract; invalid evidence"
                    )
                event(entry_at, entry, "entry", last_price, 0, owned, 0, notional)
                held_stamps = [
                    stamp(day, clock)
                    for clock in EXPECTED_5M
                    if entry_at < stamp(day, clock) <= exit_at + pd.Timedelta(minutes=5)
                ]
                for end_at in held_stamps:
                    start_at = end_at - pd.Timedelta(minutes=5)
                    bar = quote(start_at)
                    at_planned_exit = start_at >= exit_at
                    at_risk_exit = pending_exit is not None and start_at > pending_exit
                    if at_planned_exit or at_risk_exit:
                        event(
                            start_at,
                            bar,
                            "exit_mark",
                            float(bar.open),
                            owned,
                            owned,
                            owned * UNIT * (float(bar.open) - last_price),
                        )
                        if bar.volume < 1:
                            preserve("chosen exit lacks one contract in total-bar volume")
                        event(
                            start_at,
                            bar,
                            "exit",
                            float(bar.open),
                            owned,
                            0,
                            0,
                            float(bar.open) * UNIT,
                        )
                        decisions[-1]["exit_reason"] = (
                            pending_reason if at_risk_exit else "planned_exit"
                        )
                        owned = 0
                        break
                    event(
                        end_at,
                        bar,
                        "close_mark",
                        float(bar.close),
                        owned,
                        owned,
                        owned * UNIT * (float(bar.close) - last_price),
                    )
                    last_price = float(bar.close)
                    marked_peak = max(marked_peak, cash)
                    max_marked_dd = max(max_marked_dd, 1 - cash / marked_peak)
                    peak = max(peak, cash)
                    if cash <= 0 or 1 - cash / peak >= 0.20:
                        halted = True
                        if pending_exit is None:
                            pending_exit, pending_reason = end_at + LAG, "hard_drawdown"
                    elif (
                        abs(owned) * UNIT * last_price * margin * 1.25 > cash * 0.35
                        and pending_exit is None
                    ):
                        pending_exit, pending_reason = end_at + LAG, "margin_reduction"
                if owned:
                    preserve("registered intraday exit was not filled; unfinished account")
        peak = max(peak, cash)
        if cash <= 0 or 1 - cash / peak >= 0.20:
            halted = True
        today = events[start_event:]
        ledger.append(
            {
                "date": day,
                "equity": cash,
                "daily_return": cash / before - 1,
                "gross_pnl": sum(e["gross_pnl"] for e in today),
                "fees": sum(e["transaction_cost"] for e in today),
                "halted": halted,
                "ending_lots": 0,
            }
        )
    fields = [
        "date",
        "timestamp",
        "bar_start",
        "bar_end",
        "symbol",
        "action",
        "kind",
        "unit",
        "lots_before",
        "lots_after",
        "price",
        "gross_pnl",
        "turnover",
        "transaction_cost",
        "cash",
        "source_sha256",
    ]
    return (
        pd.DataFrame(ledger),
        pd.DataFrame(events, columns=fields),
        pd.DataFrame(decisions),
        max_marked_dd,
    )


def verify_cash(executions, ledger, events, cost_bps):
    """Rebuild ownership, minute-phase amounts and cash from source quotations."""
    source = executions.set_index("bar_start_utc")
    cash, owned, last_price, error = INITIAL, 0, None, 0.0
    per_day: dict[Any, float] = {}
    for row in events.itertuples():
        bar = source.loc[row.bar_start]
        if (
            row.symbol != "SC2701"
            or row.unit != UNIT
            or row.source_sha256 != bar.source_sha256
            or row.lots_before != owned
            or abs(row.lots_after) > 1
        ):
            raise ValueError("account identity/ownership invariant differs")
        expected = float(bar.close if row.action == "close_mark" else bar.open)
        if row.price != expected:
            raise ValueError("event is not the actual registered minute phase quotation")
        turnover, pnl = 0.0, 0.0
        if row.action in ("entry", "exit"):
            if (row.action == "entry" and owned != 0) or (
                row.action == "exit" and row.lots_after != 0
            ):
                raise ValueError("fill ownership invariant differs")
            turnover = abs(row.lots_after - row.lots_before) * UNIT * expected
            owned = row.lots_after
        else:
            if last_price is None or row.lots_after != owned:
                raise ValueError("mark without an owned actual entry")
            pnl = owned * UNIT * (expected - last_price)
        last_price = expected if owned else None
        fee = turnover * cost_bps / 10000
        if (
            abs(pnl - row.gross_pnl) > 1e-8
            or abs(turnover - row.turnover) > 1e-8
            or abs(fee - row.transaction_cost) > 1e-8
        ):
            raise ValueError("independent fill/mark cash differs")
        cash += pnl - fee
        error = max(error, abs(cash - row.cash))
        per_day[row.date] = per_day.get(row.date, 0.0) + pnl - fee
    if owned:
        raise ValueError("account does not end flat")
    reconstructed = INITIAL
    for row in ledger.itertuples():
        reconstructed += per_day.get(row.date, 0.0)
        error = max(error, abs(reconstructed - row.equity))
    if error > 1e-6:
        raise ValueError("cash/event/day reconciliation failed")
    return {
        "cash_max_error": error,
        "ending_lots": owned,
        "actual_five_minute_quotes_verified": True,
    }


def run(
    domestic_path,
    foreign_path,
    fx_paths,
    execution_path,
    execution_sha256,
    output,
    prior_run=None,
    mechanisms=("XS1", "XS2"),
):
    output.mkdir(parents=True, exist_ok=False)
    if prior_run is not None:
        write_json(
            output / "CLOCK_CORRECTION.json",
            {
                "supersedes": str(prior_run),
                "prior_results_sha256": identity(prior_run / "RESULTS.json"),
                "prior_protocol_sha256": identity(prior_run / "PROTOCOL.json"),
                "prior_executed_source_sha256": identity(prior_run / "executed_source.py"),
                "prior_economic_qualification": "withdrawn; raw unphysical Monday-midnight markers incorrectly blocked legitimate closure signals",
                "correction": "qualify completed domestic bars using both physical session endpoints; retain rejects, no date backfill; choose last qualified actual session completion",
                "parameters_unchanged": [
                    "12/60 maturity",
                    "ridge1",
                    "15min source lag",
                    "09:45/14:30 fills",
                    "0.003 gate",
                    "integer lot",
                    "fees",
                    "margin buffer",
                    "account limits",
                ],
            },
        )
    # Persist the exact protocol and input/code identities before any forecast or cash.
    write_json(output / "PROTOCOL.json", {**PROTOCOL, "mechanisms": list(mechanisms)})
    paths = {
        "calendar": Path(__file__).resolve().parents[1] / "afuture/runtime_calendar.json",
        "domestic15": domestic_path,
        "foreign5": foreign_path,
        "execution5": execution_path,
        **fx_paths,
    }
    write_json(
        output / "INPUT_IDENTITIES.json",
        {k: {"path": str(v), "sha256": identity(v)} for k, v in paths.items()},
    )
    (output / "executed_source.py").write_bytes(Path(__file__).read_bytes())
    domestic, foreign = read_normalized(domestic_path), read_normalized(foreign_path)
    domestic_clock = qualify_domestic_clock(domestic, RuntimeTradingCalendar.load())
    domestic_clock.loc[~domestic_clock.physical_session_qualified].to_csv(
        output / "rejected_domestic_clock.csv", index=False
    )
    domestic = domestic_clock.loc[domestic_clock.physical_session_qualified].copy()
    (output / "runtime_calendar.json").write_bytes(paths["calendar"].read_bytes())
    execution = read_domestic_five_minute(execution_path, execution_sha256)
    local = execution.bar_end_utc.dt.tz_convert("Asia/Shanghai")
    actual_days = sorted(set(local.loc[(local.dt.hour >= 9) & (local.dt.hour <= 15)].dt.date))
    # Covered official calendar dates survive missing whole domestic/FX observations.
    days = [
        day
        for day in RuntimeTradingCalendar.load().open_days["INE"]
        if actual_days[0] <= day <= actual_days[-1]
    ]
    write_json(
        output / "CALENDAR.json",
        {
            "days": [str(d) for d in days],
            "actual5m_left_boundary": str(actual_days[0]),
            "actual5m_right_boundary": str(actual_days[-1]),
            "not_a_two_month_replay": True,
        },
    )
    results = []
    for fx_name, fx_path in fx_paths.items():
        folder = output / fx_name
        folder.mkdir()
        observations = build_observations(domestic, foreign, read_normalized(fx_path), execution)
        observations.to_csv(folder / "observations.csv", index=False)
        for mechanism in mechanisms:
            predictions, audit = forecasts(observations, mechanism)
            target = folder / mechanism
            target.mkdir()
            predictions.to_csv(target / "predictions.csv", index=False)
            audit.to_csv(target / "fit_audit.csv", index=False)
            for name, cost, margin in (("base", 5, 0.12), ("stress", 15, 0.15)):
                account = target / name
                account.mkdir()
                try:
                    ledger, events, decisions, minute_dd = replay(
                        execution,
                        predictions,
                        days,
                        cost_bps=cost,
                        margin=margin,
                        failure_output=account,
                    )
                    checked = verify_cash(execution, ledger, events, cost)
                    metrics = account_metrics(ledger, events)
                    ledger.to_csv(account / "ledger.csv", index=False)
                    events.to_csv(account / "events.csv", index=False)
                    decisions.to_csv(account / "decisions.csv", index=False)
                    result = {
                        "fx": fx_name,
                        "mechanism": mechanism,
                        "cost": name,
                        "status": "complete_development_proxy",
                        **metrics,
                        "minute_close_mdd": minute_dd,
                        "entries": int(events.action.eq("entry").sum()),
                        "verification": checked,
                        "prior_to_first_trade_response_qualified_dates": int(
                            (observations.date < pd.Timestamp(days[0]))
                            .mul(observations.status.eq("qualified_observations"))
                            .sum()
                        ),
                    }
                except ValueError as error:
                    result = {
                        "fx": fx_name,
                        "mechanism": mechanism,
                        "cost": name,
                        "status": "invalid_evidence",
                        "error": str(error),
                        "economic_result": None,
                    }
                write_json(account / "RESULT.json", result)
                results.append(result)
                write_json(output / "RESULTS.json", results)
                print(json.dumps(result), flush=True)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domestic15", type=Path, required=True)
    parser.add_argument("--foreign5", type=Path, required=True)
    parser.add_argument("--cny5", type=Path, required=True)
    parser.add_argument("--cnh5", type=Path, required=True)
    parser.add_argument("--execution5", type=Path, required=True)
    parser.add_argument("--execution-source-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--supersedes", type=Path)
    parser.add_argument(
        "--mechanisms", nargs="+", choices=("XS1", "XS2", "XS3"), default=("XS1", "XS2")
    )
    args = parser.parse_args()
    run(
        args.domestic15,
        args.foreign5,
        {"CNY": args.cny5, "CNH_PROXY": args.cnh5},
        args.execution5,
        args.execution_source_sha256,
        args.output,
        prior_run=args.supersedes,
        mechanisms=tuple(args.mechanisms),
    )
