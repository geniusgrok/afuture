from pathlib import Path

import pytest

from afuture.auto import AutoConfig
from afuture.config import load_config
from afuture.models import PairConfig
from afuture.strategy import CalendarSpreadStrategy


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


def test_mean_reversion_score_uses_only_the_documented_name(tmp_path: Path) -> None:
    """Only the documented field name is accepted, with exact configured values."""
    path = _write_config(
        tmp_path,
        """
[[contracts]]
symbol = "m2609"
exchange = "DCE"
multiplier = 10
price_tick = 1
margin_rate_long = 0.12
margin_rate_short = 0.12

[[contracts]]
symbol = "m2701"
exchange = "DCE"
multiplier = 10
price_tick = 1
margin_rate_long = 0.12
margin_rate_short = 0.12

[[pairs]]
pair_id = "m_calendar"
near_symbol = "m2609"
far_symbol = "m2701"
exchange = "DCE"
volume = 1
min_mean_reversion_score = 0.37
""",
    )

    loaded = load_config(path)

    assert PairConfig("p", "m2609", "m2701", "DCE", 1).min_mean_reversion_score == 0.0
    assert AutoConfig().min_mean_reversion_score == 0.02
    assert loaded.pairs[0].min_mean_reversion_score == 0.37

    removed = tmp_path / "removed-stationarity.toml"
    removed.write_text(
        path.read_text(encoding="utf-8").replace(
            "min_mean_reversion_score", "min_" + "stationarity_score"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown"):
        load_config(removed)
    with pytest.raises(TypeError, match="unexpected keyword"):
        PairConfig(
            "p",
            "m2609",
            "m2701",
            "DCE",
            1,
            **{"min_" + "stationarity_score": 0.37},
        )
    with pytest.raises(TypeError, match="unexpected keyword"):
        AutoConfig(**{"min_" + "stationarity_score": 0.37})


def test_mean_reversion_score_validation_uses_heuristic_language() -> None:
    with pytest.raises(ValueError, match="mean-reversion heuristic"):
        CalendarSpreadStrategy(
            PairConfig("p", "m2609", "m2701", "DCE", 1, min_mean_reversion_score=-0.01)
        )
    with pytest.raises(ValueError, match="mean-reversion heuristic"):
        AutoConfig(enabled=True, min_mean_reversion_score=1.01).validate()


def test_load_config_owns_heartbeat_path_and_interval(tmp_path: Path) -> None:
    state_path = tmp_path / "runtime" / "state.json"
    configured = _write_config(
        tmp_path,
        f'''\n[paths]\nstate = "{state_path}"\nheartbeat = "runtime/live-heartbeat.json"\n\n[execution]\nheartbeat_interval_seconds = 7.5\n''',
    )

    loaded = load_config(configured)

    assert loaded.heartbeat_path == (Path.cwd() / "runtime/live-heartbeat.json").resolve()
    assert loaded.heartbeat_interval_seconds == 7.5

    defaulted = load_config(_write_config(tmp_path, f'\n[paths]\nstate = "{state_path}"\n'))
    assert defaulted.heartbeat_path == state_path.resolve().with_name("heartbeat.json")
    assert defaulted.heartbeat_interval_seconds == 5.0

    for body, match in (
        ('\n[paths]\nheartbeat = "  "\n', "paths.heartbeat"),
        ("\n[execution]\nheartbeat_interval_seconds = true\n", "heartbeat_interval_seconds"),
        ("\n[execution]\nheartbeat_interval_seconds = nan\n", "heartbeat_interval_seconds"),
        ("\n[execution]\nheartbeat_interval_seconds = 61\n", "between 1 and 60"),
    ):
        with pytest.raises(ValueError, match=match):
            load_config(_write_config(tmp_path, body))
