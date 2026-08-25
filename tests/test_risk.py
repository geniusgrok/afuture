import pytest

from afuture.auto import AutoConfig
from afuture.directional import DirectionalConfig
from afuture.risk import RiskConfig, RiskManager


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_margin_ratio", float("nan")),
        ("max_quote_age_seconds", float("inf")),
        ("max_open_pairs", 1.5),
    ],
)
def test_risk_manager_rejects_non_finite_and_lossy_configuration(field: str, value: float) -> None:
    """Removing strict RiskConfig validation must make this test fail."""
    with pytest.raises(ValueError):
        RiskManager(RiskConfig(**{field: value}))


def test_auto_config_rejects_string_boolean() -> None:
    """Replacing exact-boolean validation with truthiness must make this test fail."""
    with pytest.raises(ValueError, match="enabled.*boolean"):
        AutoConfig(enabled="false").validate()  # type: ignore[arg-type]


def test_directional_config_rejects_string_boolean() -> None:
    """Replacing exact-boolean validation with truthiness must make this test fail."""
    with pytest.raises(ValueError, match="account_exclusive.*boolean"):
        DirectionalConfig(
            enabled=True,
            products=("A",),
            account_exclusive="false",  # type: ignore[arg-type]
        ).validate()
