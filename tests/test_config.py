from pathlib import Path

import pytest

from afuture.config import load_config


def _write_config(tmp_path: Path, body: str, *, system_extra: str = "") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[system]
mode = "replay"
initial_capital = 500000
"""
        + system_extra
        + """
"""
        + body,
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("body", "section", "system_extra"),
    [
        ("", "system", "unknown = true\n"),
        ("\n[risk]\nunknown = 1\n", "risk", ""),
        (
            """

[[contracts]]
symbol = "m2609"
exchange = "DCE"
multiplier = 10
price_tick = 1
margin_rate_long = 0.12
margin_rate_short = 0.12
[contracts.fee]
unknown = 1
""",
            "fee",
            "",
        ),
    ],
)
def test_load_config_rejects_unknown_executable_fields(
    tmp_path: Path, body: str, section: str, system_extra: str
) -> None:
    """Removing strict key validation must make this test fail."""
    with pytest.raises(ValueError, match=section):
        load_config(_write_config(tmp_path, body, system_extra=system_extra))


@pytest.mark.parametrize(
    "body",
    [
        "\n[risk]\nmax_margin_ratio = nan\n",
        "\n[execution]\nmetadata_timeout_seconds = inf\n",
    ],
)
def test_load_config_rejects_non_finite_numeric_values(tmp_path: Path, body: str) -> None:
    """Removing finite-number validation must make this test fail."""
    with pytest.raises(ValueError, match="finite"):
        load_config(_write_config(tmp_path, body))


def test_load_config_rejects_lossy_integer_values(tmp_path: Path) -> None:
    """Replacing exact-integer validation with int() must make this test fail."""
    with pytest.raises(ValueError, match="slippage_ticks.*integer"):
        load_config(_write_config(tmp_path, "\n[execution]\nslippage_ticks = 1.5\n"))


def test_load_config_rejects_string_boolean_values(tmp_path: Path) -> None:
    """Replacing exact-boolean validation with bool() must make this test fail."""
    with pytest.raises(ValueError, match="auto_flatten_imbalance.*boolean"):
        load_config(
            _write_config(
                tmp_path,
                '\n[execution]\nauto_flatten_imbalance = "false"\n',
            )
        )
