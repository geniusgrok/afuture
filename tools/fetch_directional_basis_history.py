"""Fetch one bounded historical spot/futures basis panel for Candidate C research."""
from __future__ import annotations

from pathlib import Path

import akshare as ak
import pandas as pd

START_DAY = "20220822"
END_DAY = "20260820"


def main() -> None:
    runtime = Path("runtime")
    source = runtime / "return_target_specific_contracts.csv"
    if not source.exists():
        raise SystemExit(f"frozen Production input missing: {source}")
    raw = pd.read_csv(source, usecols=["product"])
    universe = sorted(raw["product"].astype(str).str.upper().dropna().unique())
    basis = ak.futures_spot_price_daily(
        start_day=START_DAY,
        end_day=END_DAY,
        vars_list=universe,
    )
    if not isinstance(basis, pd.DataFrame) or basis.empty:
        raise SystemExit("historical basis fetch returned no rows")
    required = {"date", "symbol", "spot_price", "dominant_contract_price", "dom_basis_rate"}
    missing = required - set(basis.columns)
    if missing:
        raise SystemExit(f"historical basis fetch missing columns: {sorted(missing)}")
    frame = basis.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    frame["symbol"] = frame["symbol"].astype(str).str.upper()
    for column in ("spot_price", "dominant_contract_price", "dom_basis_rate"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame[
        frame["date"].notna()
        & frame["symbol"].isin(universe)
        & frame["spot_price"].gt(0.0)
        & frame["dominant_contract_price"].gt(0.0)
        & frame["dom_basis_rate"].notna()
    ].copy()
    frame.drop_duplicates(["date", "symbol"], keep="last", inplace=True)
    frame.sort_values(["date", "symbol"], inplace=True)
    if frame.empty:
        raise SystemExit("historical basis panel is empty after validation")
    output = runtime / "directional_basis_history.csv"
    frame.to_csv(output, index=False)
    print(
        {
            "rows": int(len(frame)),
            "first_date": str(frame["date"].min().date()),
            "last_date": str(frame["date"].max().date()),
            "products": int(frame["symbol"].nunique()),
            "akshare_version": getattr(ak, "__version__", "unknown"),
        }
    )


if __name__ == "__main__":
    main()
