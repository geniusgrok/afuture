"""Exact audit lineage for the opportunity-driven directional overlay."""
from __future__ import annotations

import pandas as pd

from .directional_lineage import build_weight_lineage
from .directional_opportunity import apply_opportunity_overlay, build_opportunity_score


def build_opportunity_weight_lineage(
    policy,
    open_prices: pd.DataFrame,
    close: pd.DataFrame,
    *,
    volume: pd.DataFrame | None = None,
    open_interest: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Scale exact raw template contributions by the product opportunity overlay."""
    raw_weights, meta, lineage = build_weight_lineage(
        policy.core,
        open_prices,
        close,
    )
    score = build_opportunity_score(close, volume, open_interest)
    adjusted = apply_opportunity_overlay(raw_weights, score)
    production = policy.weight_history(
        open_prices,
        close,
        volume=volume,
        open_interest=open_interest,
    )
    try:
        pd.testing.assert_frame_equal(adjusted, production)
    except AssertionError as exc:
        raise AssertionError(
            "opportunity lineage reconstruction diverged from production policy"
        ) from exc

    raw_long = raw_weights.stack(dropna=False).rename("raw_aggregate_weight")
    adjusted_long = adjusted.stack(dropna=False).rename("adjusted_aggregate_weight")
    scale = pd.concat([raw_long, adjusted_long], axis=1)
    denominator = scale["raw_aggregate_weight"].abs() > 1e-15
    scale["opportunity_scale"] = 1.0
    scale.loc[denominator, "opportunity_scale"] = (
        scale.loc[denominator, "adjusted_aggregate_weight"]
        / scale.loc[denominator, "raw_aggregate_weight"]
    )
    scale = scale.reset_index()
    scale.columns = [
        "date",
        "product",
        "raw_aggregate_weight",
        "adjusted_aggregate_weight",
        "opportunity_scale",
    ]
    scale["date"] = pd.to_datetime(scale["date"])
    scale["product"] = scale["product"].astype(str)

    result = lineage.copy()
    if not result.empty:
        result["date"] = pd.to_datetime(result["date"])
        result["product"] = result["product"].astype(str)
        result = result.drop(columns=["aggregate_weight"]).merge(
            scale[
                [
                    "date",
                    "product",
                    "adjusted_aggregate_weight",
                    "opportunity_scale",
                ]
            ],
            on=["date", "product"],
            how="left",
            validate="many_to_one",
        )
        result["opportunity_scale"] = result["opportunity_scale"].fillna(1.0)
        result["contribution_weight"] = (
            result["contribution_weight"].astype(float)
            * result["opportunity_scale"].astype(float)
        )
        result.rename(
            columns={"adjusted_aggregate_weight": "aggregate_weight"},
            inplace=True,
        )

    if not result.empty:
        reconstructed = (
            result.groupby(["date", "product"], sort=True)["contribution_weight"]
            .sum()
            .unstack("product")
            .reindex(index=adjusted.index, columns=adjusted.columns)
            .fillna(0.0)
        )
        try:
            pd.testing.assert_frame_equal(
                reconstructed,
                adjusted,
                check_names=False,
            )
        except AssertionError as exc:
            raise AssertionError(
                "opportunity template contributions do not close to product targets"
            ) from exc

    observed_scale = result.get("opportunity_scale", pd.Series(dtype=float)).dropna()
    if not observed_scale.empty and bool(
        ((observed_scale < 0.75 - 1e-12) | (observed_scale > 1.0 + 1e-12)).any()
    ):
        raise AssertionError("opportunity lineage scale escaped conservative bounds")
    return production, meta, result
