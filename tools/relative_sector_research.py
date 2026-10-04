"""Fixed sector-relative revision of previously rejected broad momentum research."""

from __future__ import annotations

import numpy as np
import pandas as pd

# Preserve the four archived D groups; no classifications are fitted from returns.
SECTOR_GROUPS = {
    "agri_soft": tuple("A AP B C CF CJ CS LH M OI P PK RM SR Y".split()),
    "metals": tuple("AG AL AU BC CU NI PB SN ZN".split()),
    "energy_chem": tuple("BU EB EG FU L LU MA NR PG PP RU TA UR V".split()),
    "black_industrial": tuple("FG HC I J JM PF RB SA SF SM SP SS".split()),
}


def relative_sector_weights(prices: pd.DataFrame, *, pool: str = "full"):
    """Return raw daily targets and weekly ranking evidence, before OI/cost gates.

    Target D reads the63-session return ending D-1 and requires all64 actual
    completed prices. Each first observed trading session of a Monday-Sunday week
    selects the unique highest/lowest return within each inherited sector, at
    +0.25/-0.25. Other sessions retain those targets. Missing histories or tied
    extrema leave that group idle; exAG is removed before any ranking.

    This tests sector conditioning of an already failed broad momentum family.
    It does not assert that subsequent OI gates, costs, integer lots or risk actions
    preserve the raw portfolio's zero signed exposure.
    """
    if pool not in ("full", "exAG"):
        raise ValueError("pool must be full or exAG")
    if (
        not isinstance(prices.index, pd.DatetimeIndex)
        or prices.index.has_duplicates
        or not prices.index.is_monotonic_increasing
        or not prices.index.equals(prices.index.normalize())
        or prices.columns.has_duplicates
    ):
        raise ValueError("prices require unique ordered daily observations and product columns")
    products = {product for group in SECTOR_GROUPS.values() for product in group}
    if not set(prices.columns) <= products:
        raise ValueError("unclassified product in sector research")
    numeric = prices.astype(float)
    if pool == "exAG":
        numeric = numeric.drop(columns="AG", errors="ignore")
    observed = np.isfinite(numeric) & numeric.gt(0)
    valid = observed.rolling(64, min_periods=64).sum().eq(64)
    scores = (numeric.div(numeric.shift(63)) - 1.0).where(valid).shift(1)
    weeks = prices.index.to_period("W-SUN")
    output = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    current = pd.Series(0.0, index=prices.columns)
    audit = []
    for position, day in enumerate(prices.index):
        if position == 0 or weeks[position] != weeks[position - 1]:
            current[:] = 0.0
            for sector, group in SECTOR_GROUPS.items():
                eligible = scores.loc[day, scores.columns.intersection(group)].dropna()
                reason, long, short = "insufficient_complete_history", "", ""
                high = low = None
                if len(eligible) >= 2:
                    high, low = float(eligible.max()), float(eligible.min())
                    leaders = eligible.index[eligible == high]
                    laggards = eligible.index[eligible == low]
                    if high == low or len(leaders) != 1 or len(laggards) != 1:
                        reason = "tied_extreme"
                    else:
                        reason, long, short = "selected", leaders[0], laggards[0]
                        current[long], current[short] = 0.25, -0.25
                audit.append(
                    {
                        "target_day": day,
                        "source_end": prices.index[position - 1] if position else pd.NaT,
                        "source_start": prices.index[position - 64] if position >= 64 else pd.NaT,
                        "sector": sector,
                        "eligible_products": len(eligible),
                        "long": long,
                        "short": short,
                        "high_return": high,
                        "low_return": low,
                        "reason": reason,
                    }
                )
        output.loc[day] = current
    if not np.isfinite(output.to_numpy()).all() or (output.abs().sum(axis=1) > 2).any():
        raise ValueError("invalid sector target budget")
    return output, pd.DataFrame(audit)
