"""Replay official forecast revisions under explicit, uncertified archive clocks."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from adaptive_alpha_research import read_weights, write_json
from continuous_rebuild_research import qualify_oi_targets, run
from execute_research_continuation import reference_inputs, validate
from holding_exit_research import HoldingExitAccount
from lot_projection_research import LotProjectionAccount
from research_continuation import execute_chain
from usda_revision_research import entry_date_after_archive

from afuture.directional_stress90_policy import build_stress90_candidate_path
from afuture.execution_aligned_policy import FROZEN_PRODUCTS

PRODUCTS = {"soybeans": "B", "soybean_meal": "M", "soybean_oil": "Y"}


def revision_targets(panel, availability, trading_dates):
    """A new report resets targets; delayed comparable vintages activate later."""
    dates = pd.DatetimeIndex(trading_dates).sort_values()
    if dates.has_duplicates or dates.tz is not None:
        raise ValueError("trading calendar must contain unique naive dates")
    calendar = list(dates.strftime("%Y-%m-%d"))
    if panel.duplicated(["release_date", "commodity", "crop_year"]).any():
        raise ValueError("duplicate revision identity")
    known, reports, signals = {}, {}, {}
    for item in availability:
        release = item["report_date"]
        if release in known:
            raise ValueError("duplicate availability identity")
        entry = (
            entry_date_after_archive(item["archive_available_at"], calendar)
            if item.get("archive_time_assumption_eligible") is True
            else None
        )
        known[release] = {**item, "entry_date": entry}
        if entry is not None:
            reports.setdefault(pd.Timestamp(entry), []).append(release)
    if set(panel.release_date) - set(known):
        raise ValueError("revision report lacks availability")
    for (release, commodity), choices in panel.groupby(["release_date", "commodity"]):
        if commodity not in PRODUCTS:
            raise ValueError("unsupported USDA commodity")
        row = choices.sort_values("crop_year").iloc[-1]
        prior, current = known.get(row.previous_release_date), known[release]
        if prior is None or prior["entry_date"] is None or current["entry_date"] is None:
            continue
        if pd.Period(release, "M").ordinal - pd.Period(row.previous_release_date, "M").ordinal != 1:
            continue
        available_at = max(
            pd.Timestamp(prior["archive_available_at"]),
            pd.Timestamp(current["archive_available_at"]),
        )
        if "archive_available_at" in row and pd.Timestamp(row.archive_available_at) != available_at:
            raise ValueError("revision availability disagrees with both report clocks")
        entry = entry_date_after_archive(available_at.isoformat(), calendar)
        if entry is not None:
            signals.setdefault(pd.Timestamp(entry), []).append(row)
    weights = pd.DataFrame(0.0, index=dates, columns=FROZEN_PRODUCTS)
    state, latest, audit = dict.fromkeys(PRODUCTS.values(), 0.0), "", []
    for day in dates:
        incoming = reports.get(day, [])
        if incoming:
            report = max(incoming)
            for ignored in sorted(set(incoming) - {report}):
                audit.append(
                    {
                        "entry_date": day,
                        "release_date": ignored,
                        "reason": "older_report_superseded_in_batch",
                    }
                )
            if report <= latest:
                audit.append(
                    {
                        "entry_date": day,
                        "release_date": report,
                        "reason": "older_late_report_ignored",
                    }
                )
            else:
                latest = report
                for commodity, product in PRODUCTS.items():
                    state[product] = 0.0
                    audit.append(
                        {
                            "entry_date": day,
                            "release_date": report,
                            "commodity": commodity,
                            "product": product,
                            "crop_year": "",
                            "direction": 0,
                            "reason": "new_report_resets_pending_or_missing_revision",
                            "archive_time_assumption": True,
                            "historical_availability_certified": False,
                        }
                    )
        for row in signals.get(day, []):
            if row.release_date != latest:
                audit.append(
                    {
                        "entry_date": day,
                        "release_date": row.release_date,
                        "commodity": row.commodity,
                        "reason": "older_late_signal_ignored",
                    }
                )
                continue
            before, after, revision = map(
                float,
                [
                    row.previous_report_current_ratio,
                    row.current_report_current_ratio,
                    row.ratio_revision,
                ],
            )
            if (
                not np.isfinite([before, after, revision]).all()
                or before < 0
                or after < 0
                or abs(after - before - revision) > 1e-12
            ):
                raise ValueError("invalid stocks-use revision")
            product = PRODUCTS[row.commodity]
            direction = int(-np.sign(revision))
            state[product] = direction * (2.0 / 3.0)
            audit.append(
                {
                    "entry_date": day,
                    "release_date": latest,
                    "commodity": row.commodity,
                    "product": product,
                    "crop_year": row.crop_year,
                    "direction": direction,
                    "reason": "latest_common_crop_year",
                    "archive_time_assumption": True,
                    "historical_availability_certified": False,
                }
            )
        for product, weight in state.items():
            weights.at[day, product] = weight
    return weights, pd.DataFrame(audit)


def build_targets(usda, previous, continuous, baseline, output):
    prices = pd.read_csv(baseline / "market_normalized/continuous.csv", parse_dates=["date"])
    close = prices.pivot(index="date", columns="product", values="close")
    panel = pd.read_csv(usda / "signal_inputs_latest_common.csv", float_precision="round_trip")
    availability = json.loads((usda / "availability_manifest.json").read_text())
    weights, audit = revision_targets(panel, availability, close.index)
    output.mkdir(parents=True, exist_ok=False)
    audit.to_csv(output / "release_decisions.csv", index=False)
    weights.to_csv(output / "raw_997date_weights.csv", index_label="date")
    dates = read_weights(previous / "recent/B0_full_base/all_weights.csv").index
    flow = read_weights(continuous / "strategy/lagged_flow.csv")
    qualified = qualify_oi_targets(weights.loc[dates], flow)
    path = build_stress90_candidate_path(
        base_weights=qualified, completed_close_prices=close, confirming_flow=flow
    )
    for pool in ("full", "exAG"):
        folder = output / pool
        folder.mkdir()
        qualified.to_csv(folder / "U1_raw.csv", index_label="date")
        path.oi_confirmed_weights.to_csv(folder / "U1_oi.csv", index_label="date")
        path.cost_approved_weights.to_csv(folder / "U1_weights.csv", index_label="date")
    write_json(
        output / "input_check.json",
        {
            "calendar_days": len(close),
            "target_days": len(dates),
            "report_count": len(availability),
            "revision_report_count": int(panel.release_date.nunique()),
            "eligible_archived_reports": sum(
                item.get("archive_time_assumption_eligible") is True for item in availability
            ),
            "actual_newest_report_updates": int(
                audit.loc[
                    audit.reason.eq("new_report_resets_pending_or_missing_revision"), "release_date"
                ].nunique()
            ),
            "activated_revision_report_events": int(
                audit.loc[audit.reason.eq("latest_common_crop_year"), "release_date"].nunique()
            ),
            "nonzero_activated_revision_report_events": int(
                audit.loc[
                    audit.reason.eq("latest_common_crop_year") & audit.direction.ne(0),
                    "release_date",
                ].nunique()
            ),
            "late_ignored_report_count": int(
                audit.loc[
                    audit.reason.isin(
                        ["older_late_report_ignored", "older_report_superseded_in_batch"]
                    ),
                    "release_date",
                ].nunique()
            ),
            "late_ignored_signal_rows": int(audit.reason.eq("older_late_signal_ignored").sum()),
            "counts_are_signal_updates_not_account_fills": True,
            "gross_max": float(path.cost_approved_weights.abs().sum(axis=1).max()),
            "AG_target_zero": bool(path.cost_approved_weights.AG.eq(0).all()),
            "two_pools_same_input_because_only_B_M_Y": True,
            "archive_time_assumption": True,
            "historical_availability_certified": False,
        },
    )
    return output


def qualify_result(result):
    return {
        **result,
        "archive_time_assumption": True,
        "historical_availability_certified": False,
        "qualified_for_live": False,
        "acceptance_status": "pending_publication_timing_and_original_extended_acceptance"
        if result["economic_passed"]
        else "economic_rejected_under_archive_time_assumption",
    }


def next_experiment(history):
    if (
        history
        and history[-1]["status"] == "invalid_evidence"
        and history[-1]["result"].get("stage") == "interrupted"
    ):
        return history[-1]["spec"]
    seen = {item["spec"]["id"]: item for item in history}
    if "U1" not in seen:
        return {
            "id": "U1",
            "rule": "latest-common official forecast revision, original OI and approved cost budget",
            "archive_time_assumption": True,
            "historical_availability_certified": False,
        }
    parent = seen["U1"]
    if parent["status"] == "economic_failed" and "U1MIX" not in seen:
        result = parent["result"]
        if result.get("all_eight_account_ledgers_passed") and (
            result.get("component_net_positive") or result.get("component_protection_observed")
        ):
            return {
                "id": "U1MIX",
                "parent_attempt": parent["attempt"],
                "rule": "fixed equal U1 cost-approved and independent E1/B0 pool targets; one projected account",
                "archive_time_assumption": True,
                "historical_availability_certified": False,
            }
    return None


def execute(spec, attempt, *, usda, previous, continuous, baseline):
    if spec["id"] == "U1":
        strategy = build_targets(usda, previous, continuous, baseline, attempt / "strategy")
        account_type = HoldingExitAccount
    elif spec["id"] == "U1MIX":
        strategy = attempt / "strategy"
        for pool in ("full", "exAG"):
            parent = read_weights(previous / f"recent/B0_{pool}_base/all_weights.csv")
            component = read_weights(
                attempt.parent / spec["parent_attempt"] / f"strategy/{pool}/U1_weights.csv"
            )
            if not parent.index.equals(component.index) or not parent.columns.equals(
                component.columns
            ):
                raise ValueError("U1 mixture target identities differ")
            weights = (parent + component) / 2.0
            if (
                not np.isfinite(weights.to_numpy()).all()
                or (weights.abs().sum(axis=1) > 2 + 1e-10).any()
                or (pool == "exAG" and weights.AG.ne(0).any())
            ):
                raise ValueError("invalid U1 shared-account mixture")
            folder = strategy / pool
            folder.mkdir(parents=True)
            weights.to_csv(folder / "U1MIX_weights.csv", index_label="date")
        account_type = LotProjectionAccount
    else:
        raise ValueError("unknown USDA research mechanism")
    with (attempt / "execution.log").open("w") as log, contextlib.redirect_stdout(log):
        for window in ("historical", "recent"):
            run(
                previous,
                strategy,
                previous / "pair_inputs/specific.csv",
                baseline / "coverage/account_multiplier_audit.csv",
                attempt / window,
                spec["id"],
                window,
                account_type=account_type,
            )


def checked_validate(spec, attempt, *, previous, units):
    result = qualify_result(validate(spec, attempt, previous=previous, units=units))
    write_json(attempt / "analysis.json", result)
    return result


def main(args):
    protocol_path = args.output / "U1_ACCOUNT_PROTOCOL.json"
    protocol = json.loads(protocol_path.read_text())
    inputs = {row["path"]: row["sha256"] for row in protocol["inputs"]}
    inputs[str(protocol_path)] = hashlib.sha256(protocol_path.read_bytes()).hexdigest()
    addendum_path = args.output / "U1_ACCOUNT_PROTOCOL_ADDENDUM.json"
    addendum = json.loads(addendum_path.read_text())
    inputs[str(addendum_path)] = hashlib.sha256(addendum_path.read_bytes()).hexdigest()
    inputs.update({row["path"]: row["sha256"] for row in addendum["inputs"]})
    combination_path = args.output / "U1_COMBINATION_PROTOCOL.json"
    combination = json.loads(combination_path.read_text())
    inputs[str(combination_path)] = hashlib.sha256(combination_path.read_bytes()).hexdigest()
    inputs.update({row["path"]: row["sha256"] for row in combination["inputs"]})
    inputs.update(reference_inputs(args.previous))
    for item in json.loads((args.usda / "availability_manifest.json").read_text()):
        for filename, digest in (
            (item["txt_source_path"], item["source_sha256"]),
            (item["metadata_source_path"], item["metadata_source_sha256"]),
        ):
            if inputs.get(filename) != digest:
                raise ValueError("USDA availability source differs from frozen originals")
    source_dir = Path(__file__).parent
    dependencies = [
        source_dir / name
        for name in (
            "execute_usda_revision_research.py",
            "usda_revision_research.py",
            "execute_research_continuation.py",
            "research_continuation.py",
            "continuous_rebuild_research.py",
            "holding_exit_research.py",
            "adaptive_alpha_research.py",
            "lot_projection_research.py",
            "relative_sector_research.py",
        )
    ]
    dependencies.extend((source_dir.parent / "afuture").rglob("*.py"))
    sources = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(dependencies)
    }
    state = execute_chain(
        args.output / args.campaign,
        next_experiment,
        lambda spec, attempt: execute(
            spec,
            attempt,
            usda=args.usda,
            previous=args.previous,
            continuous=args.continuous,
            baseline=args.baseline,
        ),
        lambda spec, attempt: checked_validate(
            spec,
            attempt,
            previous=args.previous,
            units=args.baseline / "coverage/account_multiplier_audit.csv",
        ),
        context={
            "sources": sources,
            "inputs": inputs,
            "archive_time_assumption": True,
            "historical_availability_certified": False,
            "qualified_for_live": False,
        },
    )
    print(
        json.dumps(
            {
                "status": state["status"],
                "attempts": len(state["history"]),
                "qualified_for_live": False,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("usda", "previous", "continuous", "baseline", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--campaign", default="u1-account-chain")
    main(parser.parse_args())
