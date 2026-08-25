import numpy as np
import pandas as pd
import pytest

from afuture.directional_data_validation import (
    validate_daily_index,
    validate_finite_columns,
    validate_unique_keys,
)


@pytest.mark.parametrize(
    ("index", "message"),
    [
        ([], "cannot be empty"),
        (["not-a-date"], "invalid date"),
        (["2026-01-02", "2026-01-02"], "duplicate daily date"),
        (
            ["2026-01-02 09:00", "2026-01-02 15:00"],
            "duplicate daily date",
        ),
        (["2026-01-03", "2026-01-02"], "monotonic increasing"),
    ],
)
def test_daily_index_rejects_invalid_ordering(
    index: list[str],
    message: str,
) -> None:
    frame = pd.DataFrame({"close": range(len(index))}, index=index)

    with pytest.raises(ValueError, match=message):
        validate_daily_index(frame, name="signals")


def test_daily_index_accepts_unique_monotonic_dates() -> None:
    frame = pd.DataFrame(
        {"close": [100.0, 101.0]},
        index=pd.to_datetime(["2026-01-02", "2026-01-05"]),
    )

    validate_daily_index(frame, name="signals")


@pytest.mark.parametrize(
    ("frame", "columns", "positive", "message"),
    [
        (pd.DataFrame({"open": [1.0]}), ["close"], [], "missing columns"),
        (
            pd.DataFrame({"close": [1.0, np.nan]}),
            ["close"],
            [],
            "finite",
        ),
        (
            pd.DataFrame({"close": [1.0, np.inf]}),
            ["close"],
            [],
            "finite",
        ),
        (
            pd.DataFrame({"close": [1.0, 0.0]}),
            ["close"],
            ["close"],
            "positive",
        ),
        (
            pd.DataFrame({"close": [1.0, -1.0]}),
            ["close"],
            ["close"],
            "positive",
        ),
    ],
)
def test_finite_columns_rejects_invalid_values(
    frame: pd.DataFrame,
    columns: list[str],
    positive: list[str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_finite_columns(
            frame,
            columns,
            name="signals",
            positive=positive,
        )


def test_finite_columns_accepts_zero_for_non_price_values() -> None:
    frame = pd.DataFrame({"return": [0.0, -0.1, 0.2]})

    validate_finite_columns(frame, ["return"], name="returns")


def test_unique_keys_rejects_duplicate_contract_observation() -> None:
    frame = pd.DataFrame(
        {
            "date": ["2026-01-02", "2026-01-02"],
            "symbol": ["CU2609", "CU2609"],
        }
    )

    with pytest.raises(ValueError, match="duplicate date/symbol"):
        validate_unique_keys(
            frame,
            ["date", "symbol"],
            name="contracts",
        )
