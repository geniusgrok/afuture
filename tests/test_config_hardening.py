from pathlib import Path

import pytest

from afuture.config import load_config


def _base_config(extra_execution: str = "", pair_extra: str = "") -> str:
    return f"""
[system]
mode = "replay"
initial_capital = 500000

[execution]
{extra_execution}

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
pair_id = "m_pair"
near_symbol = "m2609"
far_symbol = "m2701"
exchange = "DCE"
volume = 5
lookback = 20
entry_z = 2.0
exit_z = 0.5
stop_z = 4.0
{pair_extra}
"""


def test_config_rejects_negative_execution_safety_values(tmp_path: Path):
    path = tmp_path / "bad.toml"
    path.write_text(_base_config("latency_ticks = -1"), encoding="utf-8")
    with pytest.raises(ValueError, match="execution"):
        load_config(path)


def test_config_rejects_invalid_structural_and_holding_parameters(tmp_path: Path):
    path = tmp_path / "bad.toml"
    path.write_text(_base_config(pair_extra="max_holding_samples = -1"), encoding="utf-8")
    with pytest.raises(ValueError, match="max_holding_samples"):
        load_config(path)


def test_config_rejects_invalid_session_window(tmp_path: Path):
    path = tmp_path / "bad.toml"
    path.write_text(_base_config(pair_extra='session_windows = ["bad"]'), encoding="utf-8")
    with pytest.raises(ValueError, match="session window"):
        load_config(path)


def test_config_rejects_negative_fee(tmp_path: Path):
    text = _base_config().replace(
        "margin_rate_short = 0.12\n\n[[contracts]]",
        "margin_rate_short = 0.12\n[contracts.fee]\nopen_fixed = -1\n\n[[contracts]]",
        1,
    )
    path = tmp_path / "bad.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="fee"):
        load_config(path)
