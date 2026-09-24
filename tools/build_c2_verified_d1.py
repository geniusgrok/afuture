"""Verify official CZCE 2024 AP/CF rows and patch only absent D0 observations.

The two input text files are saved page text from the official annual TXT URLs;
their byte hashes and URLs are recorded alongside the reproducible D1 delta.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


GAPS = {("2024-01-30", "AP"), ("2024-01-30", "CF"),
        ("2024-03-01", "AP"), ("2024-03-01", "CF"),
        ("2024-05-10", "AP")}
FIELDS = ("open", "high", "low", "close", "volume", "hold", "settle")
URL = "https://www.czce.com.cn/cn/DFSStaticFiles/Future/2024/FutureDataAllHistory/{}FUTURES2024.txt"


def official_rows(file: Path, product: str) -> tuple[pd.DataFrame, dict]:
    source_bytes = file.read_bytes()
    parsed = []
    for number, line in enumerate(source_bytes.decode("utf-8").splitlines(), 1):
        columns = [part.strip().replace(",", "") for part in line.split("|")]
        if not re.fullmatch(r"2024-\d\d-\d\d", columns[0]):
            continue
        if len(columns) != 15 or not re.fullmatch(product + r"\d{3}", columns[1]):
            raise ValueError(f"unparseable official record at {file}:{number}")
        short = columns[1]
        symbol = product + "2" + short[-3:]
        record = {"date": columns[0], "symbol": symbol, "product": product,
                  "pre_settle": float(columns[2]), "open": float(columns[3]),
                  "high": float(columns[4]), "low": float(columns[5]),
                  "close": float(columns[6]), "settle": float(columns[7]),
                  "volume": int(columns[10]), "hold": int(columns[11])}
        if not (record["low"] <= min(record["open"], record["close"])
                <= max(record["open"], record["close"]) <= record["high"]):
            raise ValueError(f"bad OHLC at {file}:{number}")
        parsed.append(record)
    rows = pd.DataFrame(parsed)
    if rows.empty or rows.duplicated(["date", "symbol"]).any():
        raise ValueError(f"empty or duplicated official series {file}")
    provenance = {"url": URL.format(product), "saved_browser_text": file.name,
                  "saved_browser_text_sha256": hashlib.sha256(source_bytes).hexdigest(),
                  "parsed_rows": len(rows), "days": rows.date.nunique()}
    return rows, provenance


def build(inputs: Path, ap_file: Path, cf_file: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    original = pd.read_csv(inputs / "return_target_specific_contracts.csv")
    roots = pd.read_csv(inputs / "broad_daily_universe.csv")
    frames, sources = zip(*(official_rows(f, p) for f, p in
                            ((ap_file, "AP"), (cf_file, "CF"))), strict=True)
    official = pd.concat(frames, ignore_index=True)
    relevant = original.loc[original.date.str.startswith("2024")
                            & original["product"].isin(("AP", "CF"))]
    overlap = official.merge(relevant, on=["date", "symbol", "product"],
                             suffixes=("_official", "_D0"))
    # The historical provider and exchange revisions can disagree on old daily
    # bars. Report every discrepancy while preserving the immutable D0 rows.
    mismatches = {}
    for field in FIELDS:
        bad = overlap.loc[overlap[field + "_official"] != overlap[field + "_D0"]]
        mismatches[field] = len(bad)
    if len(overlap) < 2500:
        raise ValueError("insufficient official/D0 overlap for identity verification")
    specific_patch = []
    root_patch = []
    checks = []
    for day, product in sorted(GAPS):
        candidates = official.loc[(official.date == day) & (official["product"] == product)]
        if candidates.empty:
            raise ValueError(f"missing official day: {day} {product}")
        if not roots.loc[(roots.date == day) & (roots["product"] == product)].empty:
            raise ValueError(f"continuous root is already present: {day} {product}")
        around_root = roots.loc[(roots["product"] == product)
                                & roots.date.isin([str((pd.Timestamp(day) + pd.Timedelta(days=n)).date())
                                                       for n in range(-4, 5)])]
        before = around_root.loc[around_root.date < day].sort_values("date").tail(1)
        after = around_root.loc[around_root.date > day].sort_values("date").head(1)
        if before.empty or after.empty:
            raise ValueError(f"missing two-sided continuous identity: {day} {product}")
        nearby_symbols = []
        for neighbor in (before.iloc[0], after.iloc[0]):
            peers = original.loc[(original.date == neighbor.date)
                                 & (original["product"] == product)]
            equal = peers.loc[(peers[list(FIELDS)].to_numpy()
                               == neighbor[list(FIELDS)].to_numpy()).all(axis=1)]
            if len(equal) != 1:
                raise ValueError(f"continuous root has ambiguous contract: {neighbor.date} {product}")
            official_neighbor = official.loc[(official.date == neighbor.date)
                                             & (official.symbol == equal.iloc[0].symbol)]
            if len(official_neighbor) != 1 or any(
                official_neighbor.iloc[0][field] != neighbor[field]
                for field in ("open", "high", "low", "close", "settle")
            ):
                raise ValueError(f"official root price differs from adjacent D0: {neighbor.date} {product}")
            nearby_symbols.append(equal.iloc[0].symbol)
        if nearby_symbols[0] != nearby_symbols[1]:
            raise ValueError(f"continuous root switches across gap: {day} {product}")
        root_candidate = candidates.loc[candidates.symbol == nearby_symbols[0]]
        if len(root_candidate) != 1:
            raise ValueError(f"official root contract missing: {day} {product}")
        root_patch.append({"date": day, **root_candidate.iloc[0][list(FIELDS)].to_dict(),
                           "product": product, "symbol": product + "0"})
        for _, candidate in candidates.iterrows():
            symbol = candidate.symbol
            adjacent = original.loc[(original.symbol == symbol)
                                    & (original.date < day)].sort_values("date").tail(1)
            following = original.loc[(original.symbol == symbol)
                                     & (original.date > day)].sort_values("date").head(1)
            if adjacent.empty or following.empty:
                # A new or retired contract can appear in the annual file. Add
                # only contracts with two-sided frozen identity and delivery.
                continue
            if adjacent.iloc[0].delivery != following.iloc[0].delivery:
                raise ValueError(f"delivery mismatch: {day} {symbol}")
            previous_same_day = original.loc[(original.date == day) &
                                             (original.symbol == symbol)]
            if not previous_same_day.empty:
                continue
            # Settlement is an independent cross-day price check, when the
            # previous D0 trading date is the official prior trading date.
            official_prior = official.loc[(official.symbol == symbol) &
                                          (official.date < day)].sort_values("date").tail(1)
            if (not official_prior.empty and adjacent.iloc[0].date == official_prior.iloc[0].date
                    and candidate.pre_settle != adjacent.iloc[0].settle):
                # Zero settlement in D0 represents a vendor missing value on
                # some expired contracts; their official bar is still retained
                # in this supplemental patch with its explicit provenance.
                if adjacent.iloc[0].settle != 0:
                    raise ValueError(f"prior settlement mismatch: {day} {symbol}")
            specific_patch.append({"date": day, **candidate[list(FIELDS)].to_dict(),
                                   "symbol": symbol, "product": product,
                                   "exchange": "CZCE", "delivery": adjacent.iloc[0].delivery})
        checks.append({"date": day, "product": product,
                       "root_contract_from_both_neighbors": nearby_symbols[0],
                       "official_contract_rows": len(candidates),
                       "new_specific_rows": sum(x["date"] == day and x["product"] == product
                                                for x in specific_patch)})
    if len(root_patch) != len(GAPS) or len(specific_patch) < 20:
        raise ValueError("incomplete verified D1 delta")
    specific_new = pd.concat([original, pd.DataFrame(specific_patch)], ignore_index=True)
    roots_new = pd.concat([roots, pd.DataFrame(root_patch)], ignore_index=True)
    for name, data, keys in (("return_target_specific_contracts.csv", specific_new,
                             ["date", "symbol"]),
                            ("broad_daily_universe.csv", roots_new, ["date", "product"])):
        if data.duplicated(keys).any():
            raise ValueError(f"D1 duplicate: {name}")
        data.sort_values(keys).to_csv(output / name, index=False)
    pd.DataFrame(specific_patch).sort_values(["date", "symbol"]).to_csv(
        output / "D1_specific_patch.csv", index=False)
    pd.DataFrame(root_patch).sort_values(["date", "product"]).to_csv(
        output / "D1_continuous_patch.csv", index=False)
    for name in ("execution_aligned_weights.csv", "prior_two_year_broad_60m.csv",
                 "two_year_broad_60m.csv"):
        link = output / name
        target = (inputs / name).resolve()
        if link.is_symlink() and link.resolve() == target:
            continue
        if link.exists() or link.is_symlink():
            raise ValueError(f"refusing to replace unexpected D1 input: {link}")
        link.symlink_to(target)
    manifest = {"retrieved_utc": datetime.now(timezone.utc).isoformat(),
                "source": sources, "full_year_overlap_rows": len(overlap),
                "D0_official_overlapping_field_mismatch_count": mismatches,
                "checks": checks, "specific_patch_rows": len(specific_patch),
                "continuous_patch_rows": len(root_patch),
                "output_sha256": {name: hashlib.sha256((output / name).read_bytes()).hexdigest()
                                  for name in ("return_target_specific_contracts.csv",
                                               "broad_daily_universe.csv",
                                               "D1_specific_patch.csv", "D1_continuous_patch.csv")}}
    (output / "D1_provenance.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"specific": len(specific_patch), "root": len(root_patch),
                      "overlap": len(overlap)}, ensure_ascii=False))


if __name__ == "__main__":
    build(*(Path(arg).resolve() for arg in sys.argv[1:5]))
