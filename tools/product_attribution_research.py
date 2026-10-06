"""Research-only product attribution of the frozen template proxy scores."""

import numpy as np
import pandas as pd

from afuture import execution_aligned_policy as policy


def product_attributed_targets(open_prices, close, paths):
    """Rank completed product PnL; never transfer another product's score."""
    scores = []
    weights = []
    identities = []
    for name, path in paths.items():
        # Reuse the original active-quote validation before decomposing its PnL.
        policy._intraday_proxy_stream(open_prices, close, path)
        intraday = close.reindex_like(path).div(open_prices.reindex_like(path)) - 1
        pnl = path * intraday.where(path.ne(0), 0)
        turnover = path.diff().abs()
        if len(path):
            turnover.iloc[0] = path.iloc[0].abs()
        score = policy._robust_trailing_scores(
            pnl - turnover * policy.BASE_COST_BPS / 10000,
            pnl - turnover * policy.STRESS_COST_BPS / 10000,
        )
        scores.append(score)
        weights.append(path.to_numpy(float))
        identities.extend((name, product) for product in path.columns)
    if not paths:
        raise ValueError("template paths are required")
    first = next(iter(paths.values()))
    if any(
        not p.index.equals(first.index) or not p.columns.equals(first.columns)
        for p in paths.values()
    ):
        raise ValueError("template axes must match")
    score = np.concatenate(scores, axis=1)
    weight = np.concatenate(weights, axis=1)
    result = pd.DataFrame(0.0, index=first.index, columns=first.columns)
    selected = []
    for i, day in enumerate(first.index):
        if i >= policy.META_LOOKBACK and (not selected or i % policy.META_REBALANCE == 0):
            valid = np.flatnonzero(np.isfinite(score[i]) & (np.abs(weight[i]) > 1e-15))
            selected = []
            products = set()
            for j in valid[np.argsort(-score[i, valid], kind="stable")]:
                product = identities[j][1]
                if product not in products:
                    selected.append(j)
                    products.add(product)
                if len(selected) == policy.META_COUNT:
                    break
        for j in selected:
            result.at[day, identities[j][1]] = weight[i, j] / policy.META_COUNT
    return result
