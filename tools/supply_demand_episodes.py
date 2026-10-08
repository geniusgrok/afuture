"""Single-lot paper episodes for the frozen S1 cost estimator, never an account.

Use the existing causal concrete-contract selector. Missing marks invalidate the
whole episode; never fill them from a later close. All roll legs enter turnover.
Daily opening/closing prices share the disclosed historical execution proxy.
"""

from __future__ import annotations

from dataclasses import asdict

import numpy as np
import pandas as pd

from afuture.directional_acceptance import PRODUCT_MULTIPLIERS
from afuture.directional_concentration_freeze import (
    ExpandingMedianConcentrationFreezeDirectionalProductionAcceptance as Account,
)
from afuture.supply_demand_cost import SupplyEpisode


def build_supply_episodes(raw: pd.DataFrame, directions: pd.DataFrame) -> tuple[list, list]:
    selector = Account()
    context = selector.prepare_contracts(raw)
    active: dict[str, dict] = {}
    episodes, rejected = [], []
    for day, weights in directions.iterrows():
        prior = context.available_activity_days[context.available_activity_days < day]
        if not len(prior):
            raise ValueError("paper episodes need completed contract activity")
        selected = selector._select_contracts_from_snapshot(
            context.activity_by_day[prior[-1]],
            day,
            preferred_symbols={p: s["symbol"] for p, s in active.items()},
        )
        opening = (day.tz_localize("Asia/Shanghai") + pd.Timedelta(hours=9)).to_pydatetime()
        known = (day.tz_localize("Asia/Shanghai") + pd.Timedelta(hours=15)).to_pydatetime()
        products = set(active) | {p for p, v in weights.items() if v != 0}
        for product in sorted(products):
            direction = int(np.sign(weights.get(product, 0)))
            state = active.get(product)
            symbol = selected.get(product)
            row = context.by_day_symbol.get((day, symbol)) if symbol else None
            if state:
                old = context.by_day_symbol.get((day, state["symbol"]))
                if old is None:
                    state["invalid"] = True
                elif not state["invalid"]:
                    state["gross"] += (
                        state["direction"]
                        * PRODUCT_MULTIPLIERS[product]
                        * (float(old.open) - state["last_close"])
                    )
                if direction != state["direction"] or row is None:
                    if old is not None and not state["invalid"]:
                        state["turnover"] += float(old.open) * PRODUCT_MULTIPLIERS[product]
                        episodes.append(
                            SupplyEpisode(
                                product=product,
                                direction=state["direction"],
                                entered_at=state["entered_at"],
                                completed_at=opening,
                                known_at=known,
                                entry_notional=state["entry_notional"],
                                gross_pnl=state["gross"],
                                turnover_notional=state["turnover"],
                                episode_id=state["episode_id"],
                            )
                        )
                    else:
                        rejected.append(
                            dict(
                                product=product,
                                exit_day=str(day.date()),
                                reason="missing episode mark",
                            )
                        )
                    del active[product]
                    state = None
                elif symbol != state["symbol"] and not state["invalid"]:
                    state["turnover"] += (float(old.open) + float(row.open)) * (
                        PRODUCT_MULTIPLIERS[product]
                    )
                    state["symbol"] = symbol
            if state is None and direction and row is not None:
                notional = float(row.open) * PRODUCT_MULTIPLIERS[product]
                state = dict(
                    direction=direction,
                    symbol=symbol,
                    entered_at=opening,
                    entry_notional=notional,
                    turnover=notional,
                    gross=0.0,
                    invalid=False,
                    episode_id=f"{product}_{day.date()}_{direction}",
                )
                active[product] = state
            if state and row is not None and not state["invalid"]:
                state["gross"] += (
                    direction * PRODUCT_MULTIPLIERS[product] * (float(row.close) - float(row.open))
                )
                state["last_close"] = float(row.close)
    rejected.extend(
        dict(product=p, reason="unfinished episode", episode_id=s["episode_id"])
        for p, s in active.items()
    )
    return episodes, rejected


def episode_records(episodes: list[SupplyEpisode]) -> list[dict]:
    return [
        dict(
            asdict(e),
            entered_at=e.entered_at.isoformat(),
            completed_at=e.completed_at.isoformat(),
            known_at=e.known_at.isoformat(),
        )
        for e in episodes
    ]
