"""Production adapter for the opportunity-driven directional V2 policy."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd

from .execution_aligned_policy import MAX_GROSS_LEVERAGE
from .execution_aligned_runtime import (
    ExecutionAlignedDirectionalPortfolioManager,
    ExecutionAlignedSignalHistory,
    FROZEN_PRODUCTS,
)
from .opportunity_aligned_policy import OpportunityAlignedAggressivePolicy


@dataclass(frozen=True)
class OpportunitySignalHistory(ExecutionAlignedSignalHistory):
    volume: pd.DataFrame | None = None
    open_interest: pd.DataFrame | None = None


class SinaOpportunityOHLCVOIProvider:
    def __init__(self, max_workers: int = 8) -> None:
        self.max_workers = max(1, int(max_workers))

    @staticmethod
    def _load_one(product: str) -> pd.DataFrame:
        try:
            import akshare as ak
        except ImportError as exc:
            raise RuntimeError(
                "directional live mode requires the 'live' extra with akshare"
            ) from exc
        frame = ak.futures_zh_daily_sina(symbol=f"{product.upper()}0").copy()
        required = {"date", "open", "close"}
        if frame.empty or not required.issubset(frame.columns):
            raise RuntimeError(f"continuous OHLC history unavailable: {product}")
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        numeric = [column for column in ("open", "close", "volume", "hold") if column in frame]
        for column in numeric:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.dropna(subset=["date", "open", "close"])
        frame = frame[(frame["open"] > 0) & (frame["close"] > 0)]
        frame.drop_duplicates("date", keep="last", inplace=True)
        frame.sort_values("date", inplace=True)
        if frame.empty:
            raise RuntimeError(f"continuous OHLC history empty: {product}")
        columns = ["open", "close"]
        if "volume" in frame.columns:
            columns.append("volume")
        if "hold" in frame.columns:
            columns.append("hold")
        return frame.set_index("date")[columns]

    def load(self, products: tuple[str, ...]) -> OpportunitySignalHistory:
        unique = tuple(dict.fromkeys(item.upper() for item in products))
        frames: dict[str, pd.DataFrame] = {}
        errors: list[str] = []
        with ThreadPoolExecutor(
            max_workers=min(self.max_workers, max(len(unique), 1))
        ) as executor:
            futures = {
                executor.submit(self._load_one, product): product for product in unique
            }
            for future in as_completed(futures):
                product = futures[future]
                try:
                    frames[product] = future.result()
                except Exception as exc:
                    errors.append(f"{product}: {type(exc).__name__}: {exc}")
        if errors:
            raise RuntimeError(
                "directional signal refresh failed: " + "; ".join(sorted(errors))
            )
        open_prices = pd.concat(
            [frames[product]["open"].rename(product) for product in unique], axis=1
        ).sort_index()
        close = pd.concat(
            [frames[product]["close"].rename(product) for product in unique], axis=1
        ).sort_index()
        activity_complete = all(
            {"volume", "hold"}.issubset(frames[product].columns)
            for product in unique
        )
        volume = None
        open_interest = None
        if activity_complete:
            volume = pd.concat(
                [frames[product]["volume"].rename(product) for product in unique],
                axis=1,
            ).sort_index()
            open_interest = pd.concat(
                [frames[product]["hold"].rename(product) for product in unique],
                axis=1,
            ).sort_index()
        return OpportunitySignalHistory(
            open_prices,
            close,
            volume=volume,
            open_interest=open_interest,
        )


class OpportunityAlignedDirectionalPortfolioManager(
    ExecutionAlignedDirectionalPortfolioManager
):
    def __init__(
        self,
        config,
        broker,
        risk_manager,
        *,
        signal_provider=None,
        policy=None,
        **kwargs,
    ):
        if policy is None:
            configured = tuple(sorted({str(item).upper() for item in config.products}))
            if configured != FROZEN_PRODUCTS:
                raise ValueError(
                    "opportunity-aligned production requires the frozen 50-product universe"
                )
            policy = OpportunityAlignedAggressivePolicy(products=configured)
        super().__init__(
            config,
            broker,
            risk_manager,
            signal_provider=signal_provider or SinaOpportunityOHLCVOIProvider(),
            policy=policy,
            **kwargs,
        )

    def _normalize_history(
        self,
        history: OpportunitySignalHistory,
        *,
        max_date: date,
    ) -> OpportunitySignalHistory:
        close = self._normalize_frame(history.close, max_date)
        open_prices = self._normalize_frame(history.open, max_date)
        common = close.index.intersection(open_prices.index)
        close = close.reindex(common)
        open_prices = open_prices.reindex(index=common, columns=close.columns)
        if len(close) < 140:
            raise RuntimeError("directional signal history is shorter than 140 days")

        def normalize_optional(frame: pd.DataFrame | None) -> pd.DataFrame | None:
            if frame is None:
                return None
            result = self._normalize_frame(frame, max_date)
            return result.reindex(index=common, columns=close.columns)

        volume = normalize_optional(history.volume)
        open_interest = normalize_optional(history.open_interest)
        if volume is None or open_interest is None:
            volume = None
            open_interest = None
        return OpportunitySignalHistory(
            open_prices,
            close,
            volume=volume,
            open_interest=open_interest,
        )

    def _load_signal(
        self,
        now: datetime,
        required_signal_day: date | None = None,
    ) -> OpportunitySignalHistory:
        local = self._local(now)
        max_date = required_signal_day or local.date()
        refresh = (
            self._execution_signal_history is None
            or self._signal_refresh_date != local.date()
        )
        if refresh:
            try:
                raw = self.signal_provider.load(
                    tuple(item.upper() for item in self.config.products)
                )
            except Exception:
                if self._execution_signal_history is None:
                    raise
                cached = self._execution_signal_history
                if not isinstance(cached, OpportunitySignalHistory):
                    raise RuntimeError("cached opportunity signal history has wrong type")
                self._execution_signal_history = self._normalize_history(
                    cached,
                    max_date=max_date,
                )
            else:
                if not isinstance(raw, OpportunitySignalHistory):
                    raise RuntimeError(
                        "opportunity signal provider must return OHLCV/OI history"
                    )
                self._validate_activity_signal_alignment(raw, required_signal_day)
                self._execution_signal_history = self._normalize_history(
                    raw,
                    max_date=max_date,
                )
            self._signal_refresh_date = local.date()

        history = self._execution_signal_history
        if history is None or not isinstance(history, OpportunitySignalHistory):
            raise RuntimeError("opportunity-aligned signal history is unavailable")
        history = self._normalize_history(history, max_date=max_date)
        self._validate_signal_history(history, local, required_signal_day)
        self._execution_signal_history = history
        return OpportunitySignalHistory(
            history.open.copy(),
            history.close.copy(),
            volume=history.volume.copy() if history.volume is not None else None,
            open_interest=(
                history.open_interest.copy()
                if history.open_interest is not None
                else None
            ),
        )

    def _next_target_weights(
        self,
        history: OpportunitySignalHistory,
    ) -> dict[str, float]:
        close = history.close
        open_prices = history.open
        last = pd.Timestamp(close.index[-1])
        synthetic_index = last + pd.offsets.BDay(1)
        synthetic_close = close.iloc[[-1]].copy()
        synthetic_close.index = pd.DatetimeIndex([synthetic_index])
        synthetic_open = close.iloc[[-1]].copy()
        synthetic_open.index = pd.DatetimeIndex([synthetic_index])
        extended_open = pd.concat([open_prices, synthetic_open])
        extended_close = pd.concat([close, synthetic_close])

        volume = history.volume
        open_interest = history.open_interest
        if volume is not None and open_interest is not None:
            synthetic_volume = volume.iloc[[-1]].copy()
            synthetic_volume.index = pd.DatetimeIndex([synthetic_index])
            synthetic_oi = open_interest.iloc[[-1]].copy()
            synthetic_oi.index = pd.DatetimeIndex([synthetic_index])
            weights = self.policy.target_weights(
                extended_open,
                extended_close,
                volume=pd.concat([volume, synthetic_volume]),
                open_interest=pd.concat([open_interest, synthetic_oi]),
            )
        else:
            weights = self.policy.target_weights(extended_open, extended_close)

        gross = sum(abs(float(value)) for value in weights.values())
        if gross > min(self.config.max_gross_leverage, MAX_GROSS_LEVERAGE) + 1e-10:
            raise RuntimeError(
                f"directional signal exceeds configured gross leverage: {gross:.6f}"
            )
        return {str(key).upper(): float(value) for key, value in weights.items()}
