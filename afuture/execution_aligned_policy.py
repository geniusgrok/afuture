"""Frozen 96-template execution-aligned directional portfolio policy.

This is the only production directional signal policy. The template pool was selected on
the already-observed 2024-08-21..2026-08-20 specific-contract next-open history. Daily
live rotation remains causal: template signals use the previous close and the meta
allocator ranks templates only from completed continuous-contract open->close returns.
Product ordering is frozen alphabetically. Gross target notional is capped at 2x.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

import numpy as np
import pandas as pd

MAX_GROSS_LEVERAGE = 2.0
MAX_ABS_DAILY_RETURN = 0.20
BASE_COST_BPS = 5.0
META_LOOKBACK = 11
META_REBALANCE = 3
META_COUNT = 3
META_ANNUALIZED_WEIGHT = 0.25
META_SHARPE_WEIGHT = 1.0
META_SCORE_SOURCE = "continuous_intraday_proxy"


@dataclass(frozen=True)
class _Template:
    family: str
    slow: int
    fast: int
    max_products: int
    rebalance: int
    gross_leverage: float = MAX_GROSS_LEVERAGE


def _parse_template_id(raw: str) -> _Template:
    match = re.fullmatch(r"(.+)_s(\d+)_f(\d+)_k(\d+)_r(\d+)_g([0-9.]+)", raw)
    if match is None:
        raise ValueError(f"invalid frozen directional template: {raw}")
    family, slow, fast, max_products, rebalance, gross = match.groups()
    value = float(gross)
    if value > MAX_GROSS_LEVERAGE:
        raise ValueError(f"frozen template exceeds gross cap: {raw}")
    return _Template(
        family,
        int(slow),
        int(fast),
        int(max_products),
        int(rebalance),
        value,
    )


def _rolling_log_return(returns: pd.DataFrame, window: int) -> pd.DataFrame:
    return np.log1p(returns.clip(lower=-0.99)).rolling(
        window, min_periods=window
    ).sum()


def _normalized_price(returns: pd.DataFrame) -> pd.DataFrame:
    return (1.0 + returns.fillna(0.0)).cumprod()


def _signal_scores(returns: pd.DataFrame, template: _Template) -> pd.DataFrame:
    vol20 = returns.rolling(20, min_periods=20).std().replace(0.0, np.nan)
    if template.family == "tsmom":
        momentum = _rolling_log_return(returns, template.slow)
        return momentum / (vol20 * np.sqrt(max(template.slow, 1)))
    if template.family == "momentum":
        return _rolling_log_return(returns, template.slow)
    if template.family == "reversal":
        momentum = _rolling_log_return(returns, template.fast)
        return -momentum / (vol20 * np.sqrt(max(template.fast, 1)))
    if template.family == "moving_average":
        price = _normalized_price(returns)
        average = price.rolling(template.slow, min_periods=template.slow).mean()
        deviation = price / average - 1.0
        return deviation / (vol20 * np.sqrt(max(template.slow, 1)))
    if template.family == "breakout":
        price = _normalized_price(returns)
        rolling_low = price.rolling(template.slow, min_periods=template.slow).min()
        rolling_high = price.rolling(template.slow, min_periods=template.slow).max()
        return (
            (price - rolling_low)
            / (rolling_high - rolling_low).replace(0.0, np.nan)
            - 0.5
        )
    if template.family == "acceleration":
        slow = _rolling_log_return(returns, template.slow)
        fast = _rolling_log_return(returns, template.fast)
        return (slow - fast) / (vol20 * np.sqrt(max(template.slow, 1)))
    raise ValueError(f"unknown frozen directional family: {template.family}")


def _template_weight_path(
    returns: pd.DataFrame, template: _Template
) -> pd.DataFrame:
    scores = _signal_scores(returns, template).to_numpy(float)
    current = np.zeros(returns.shape[1], dtype=float)
    audit = np.zeros(returns.shape, dtype=float)
    step = max(template.rebalance, 1)
    for position in range(1, len(returns)):
        if position % step == 0:
            lagged = scores[position - 1]
            valid = np.flatnonzero(
                np.isfinite(lagged) & (np.abs(lagged) > 1e-12)
            )
            next_weights = np.zeros(returns.shape[1], dtype=float)
            if valid.size:
                order = valid[np.argsort(-np.abs(lagged[valid]), kind="stable")]
                selected = order[: min(template.max_products, len(order))]
                if selected.size:
                    each = min(
                        template.gross_leverage, MAX_GROSS_LEVERAGE
                    ) / float(selected.size)
                    next_weights[selected] = np.sign(lagged[selected]) * each
            current = next_weights
        audit[position] = current
    return pd.DataFrame(audit, index=returns.index, columns=returns.columns)


def _trailing_scores(frame: pd.DataFrame, lookback: int = META_LOOKBACK) -> np.ndarray:
    values = frame.fillna(0.0).to_numpy(float)
    score = np.full_like(values, np.nan, dtype=float)
    for index in range(lookback, len(frame)):
        history = values[index - lookback : index]
        log_total = np.log1p(np.clip(history, -0.999999, None)).sum(axis=0)
        annualized = np.exp(log_total * (252.0 / lookback)) - 1.0
        mean = history.mean(axis=0)
        std = history.std(axis=0, ddof=1)
        sharpe = np.zeros(history.shape[1], dtype=float)
        valid = std > 1e-12
        sharpe[valid] = mean[valid] / std[valid] * np.sqrt(252.0)
        row = META_ANNUALIZED_WEIGHT * annualized + META_SHARPE_WEIGHT * sharpe
        row[annualized <= 0.0] = np.nan
        score[index] = row
    return score


_EXECUTION_TEMPLATE_IDS = (
    "breakout_s120_f0_k1_r1_g2",
    "tsmom_s40_f0_k1_r2_g2",
    "moving_average_s60_f0_k1_r5_g2",
    "breakout_s5_f0_k1_r2_g2",
    "breakout_s60_f0_k1_r1_g2",
    "moving_average_s60_f0_k1_r1_g2",
    "reversal_s0_f10_k1_r2_g2",
    "acceleration_s40_f5_k2_r2_g2",
    "tsmom_s40_f0_k2_r5_g2",
    "acceleration_s10_f3_k1_r10_g2",
    "moving_average_s60_f0_k1_r2_g2",
    "moving_average_s60_f0_k1_r10_g2",
    "tsmom_s40_f0_k2_r2_g2",
    "acceleration_s20_f5_k3_r10_g2",
    "breakout_s40_f0_k5_r10_g2",
    "breakout_s5_f0_k1_r5_g2",
    "acceleration_s40_f5_k2_r5_g2",
    "breakout_s40_f0_k1_r1_g2",
    "moving_average_s120_f0_k1_r2_g2",
    "reversal_s0_f1_k5_r10_g2",
    "breakout_s40_f0_k2_r1_g2",
    "tsmom_s40_f0_k3_r5_g2",
    "acceleration_s40_f5_k2_r1_g2",
    "moving_average_s120_f0_k2_r5_g2",
    "acceleration_s40_f5_k3_r2_g2",
    "breakout_s20_f0_k1_r5_g2",
    "moving_average_s120_f0_k1_r10_g2",
    "moving_average_s120_f0_k1_r1_g2",
    "reversal_s0_f5_k5_r10_g2",
    "acceleration_s20_f5_k1_r5_g2",
    "momentum_s20_f0_k1_r5_g2",
    "tsmom_s40_f0_k2_r1_g2",
    "moving_average_s120_f0_k2_r2_g2",
    "moving_average_s40_f0_k1_r1_g2",
    "tsmom_s40_f0_k2_r10_g2",
    "moving_average_s40_f0_k2_r10_g2",
    "acceleration_s40_f5_k2_r10_g2",
    "moving_average_s40_f0_k5_r5_g2",
    "breakout_s40_f0_k3_r1_g2",
    "acceleration_s10_f3_k2_r10_g2",
    "acceleration_s120_f10_k5_r1_g2",
    "moving_average_s60_f0_k3_r1_g2",
    "breakout_s60_f0_k5_r10_g2",
    "reversal_s0_f1_k1_r5_g2",
    "moving_average_s120_f0_k1_r5_g2",
    "reversal_s0_f5_k2_r10_g2",
    "moving_average_s60_f0_k2_r1_g2",
    "momentum_s20_f0_k1_r10_g2",
    "tsmom_s40_f0_k3_r2_g2",
    "reversal_s0_f1_k3_r10_g2",
    "reversal_s0_f1_k2_r5_g2",
    "breakout_s10_f0_k1_r2_g2",
    "tsmom_s40_f0_k1_r5_g2",
    "acceleration_s20_f5_k1_r10_g2",
    "breakout_s10_f0_k1_r5_g2",
    "tsmom_s40_f0_k1_r1_g2",
    "moving_average_s120_f0_k3_r5_g2",
    "moving_average_s120_f0_k2_r10_g2",
    "moving_average_s40_f0_k5_r10_g2",
    "acceleration_s20_f5_k2_r5_g2",
    "acceleration_s20_f5_k3_r5_g2",
    "moving_average_s120_f0_k2_r1_g2",
    "acceleration_s40_f5_k1_r1_g2",
    "breakout_s120_f0_k5_r10_g2",
    "reversal_s0_f1_k2_r10_g2",
    "momentum_s120_f0_k1_r10_g2",
    "tsmom_s40_f0_k3_r1_g2",
    "breakout_s20_f0_k5_r10_g2",
    "moving_average_s120_f0_k3_r1_g2",
    "acceleration_s40_f5_k3_r5_g2",
    "breakout_s40_f0_k5_r5_g2",
    "tsmom_s10_f0_k2_r10_g2",
    "acceleration_s40_f5_k3_r1_g2",
    "tsmom_s40_f0_k1_r10_g2",
    "tsmom_s40_f0_k3_r10_g2",
    "breakout_s40_f0_k5_r2_g2",
    "moving_average_s60_f0_k3_r2_g2",
    "breakout_s60_f0_k2_r1_g2",
    "acceleration_s120_f20_k5_r10_g2",
    "moving_average_s60_f0_k2_r10_g2",
    "acceleration_s40_f5_k3_r10_g2",
    "reversal_s0_f10_k1_r5_g2",
    "tsmom_s3_f0_k1_r10_g2",
    "acceleration_s20_f5_k2_r10_g2",
    "reversal_s0_f10_k1_r1_g2",
    "momentum_s120_f0_k2_r5_g2",
    "acceleration_s60_f10_k3_r1_g2",
    "breakout_s5_f0_k1_r1_g2",
    "breakout_s60_f0_k5_r2_g2",
    "breakout_s60_f0_k5_r1_g2",
    "reversal_s0_f1_k3_r5_g2",
    "moving_average_s60_f0_k2_r2_g2",
    "breakout_s20_f0_k3_r10_g2",
    "acceleration_s120_f10_k5_r5_g2",
    "breakout_s40_f0_k1_r5_g2",
    "reversal_s0_f5_k3_r10_g2",
)
_EXECUTION_TEMPLATES = tuple(
    _parse_template_id(item) for item in _EXECUTION_TEMPLATE_IDS
)


def _clean_prices(frame: pd.DataFrame, products: tuple[str, ...]) -> pd.DataFrame:
    result = frame.copy()
    result.columns = [str(item).upper() for item in result.columns]
    requested = sorted({str(item).upper() for item in products})
    missing = sorted(set(requested) - set(result.columns))
    if missing:
        raise ValueError(f"directional OHLC history missing products: {missing}")
    result = result[requested].astype(float).sort_index()
    return result.where(result > 0.0)


def _intraday_proxy_stream(
    open_prices: pd.DataFrame,
    close: pd.DataFrame,
    weights: pd.DataFrame,
    *,
    cost_bps: float = BASE_COST_BPS,
) -> pd.Series:
    intraday = close.div(open_prices) - 1.0
    intraday = intraday.mask(intraday.abs() > MAX_ABS_DAILY_RETURN).fillna(0.0)
    pnl = (weights * intraday).sum(axis=1)
    turnover = weights.diff().abs().sum(axis=1)
    if len(turnover):
        turnover.iloc[0] = float(weights.iloc[0].abs().sum())
    return pnl - turnover * float(cost_bps) / 10000.0


@dataclass(frozen=True)
class ExecutionAlignedAggressivePolicy:
    products: tuple[str, ...]
    meta_lookback: int = META_LOOKBACK
    meta_rebalance: int = META_REBALANCE
    meta_count: int = META_COUNT
    meta_score_source: str = META_SCORE_SOURCE
    template_ids: tuple[str, ...] = _EXECUTION_TEMPLATE_IDS

    def __post_init__(self) -> None:
        if not self.products:
            raise ValueError("execution-aligned policy products cannot be empty")
        if self.template_ids != _EXECUTION_TEMPLATE_IDS:
            raise ValueError("execution-aligned template pool is frozen")
        if (
            self.meta_lookback != META_LOOKBACK
            or self.meta_rebalance != META_REBALANCE
            or self.meta_count != META_COUNT
            or self.meta_score_source != META_SCORE_SOURCE
        ):
            raise ValueError("execution-aligned meta policy is frozen")

    def weight_history(
        self,
        open_prices: pd.DataFrame,
        close: pd.DataFrame,
    ) -> pd.DataFrame:
        close = _clean_prices(close, self.products)
        open_prices = _clean_prices(open_prices, self.products).reindex(close.index)
        returns = close.pct_change(fill_method=None)
        returns = returns.mask(returns.abs() > MAX_ABS_DAILY_RETURN)

        streams: dict[str, pd.Series] = {}
        paths: dict[str, pd.DataFrame] = {}
        for template_id, template in zip(self.template_ids, _EXECUTION_TEMPLATES):
            weights = _template_weight_path(returns, template)
            paths[template_id] = weights
            streams[template_id] = _intraday_proxy_stream(
                open_prices, close, weights
            )

        stream_frame = pd.DataFrame(streams).sort_index().fillna(0.0)
        scores = _trailing_scores(stream_frame, self.meta_lookback)
        names = list(stream_frame.columns)
        final = pd.DataFrame(0.0, index=close.index, columns=close.columns)
        selected: list[int] = []
        for position, timestamp in enumerate(close.index):
            if position >= self.meta_lookback and (
                not selected or position % self.meta_rebalance == 0
            ):
                row = scores[position]
                valid = np.flatnonzero(np.isfinite(row))
                selected = (
                    [
                        int(item)
                        for item in valid[
                            np.argsort(-row[valid], kind="stable")
                        ][: self.meta_count]
                    ]
                    if valid.size
                    else []
                )
            if selected:
                rows = [paths[names[item]].loc[timestamp] for item in selected]
                final.loc[timestamp] = pd.concat(rows, axis=1).mean(axis=1)

        gross = final.abs().sum(axis=1)
        if bool((gross > MAX_GROSS_LEVERAGE + 1e-10).any()):
            raise AssertionError("execution-aligned policy exceeded 2x gross")
        return final

    def target_weights(
        self,
        open_prices: pd.DataFrame,
        close: pd.DataFrame,
    ) -> dict[str, float]:
        history = self.weight_history(open_prices, close)
        if history.empty:
            return {}
        latest = history.iloc[-1]
        return {
            str(product): float(weight)
            for product, weight in latest.items()
            if abs(float(weight)) > 1e-15
        }
