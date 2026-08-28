"""Read the operational heartbeat extension without changing strategy configuration."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


@dataclass(frozen=True)
class RuntimeHeartbeatSettings:
    path: Path
    interval_seconds: float
    base_runtime_dir: Path
    explicitly_configured: bool

    def path_for_state(self, state_path: str | Path) -> Path:
        runtime = Path(state_path).resolve(strict=False).parent
        if runtime == self.base_runtime_dir:
            return self.path.resolve(strict=False)
        return (runtime / self.path.name).resolve(strict=False)


_ACTIVE_SETTINGS: RuntimeHeartbeatSettings | None = None


def load_heartbeat_settings(
    config_path: str | Path,
    *,
    state_path: str | Path,
) -> RuntimeHeartbeatSettings:
    raw = tomllib.loads(Path(config_path).read_text(encoding="utf-8"))
    paths = raw.get("paths", {})
    execution = raw.get("execution", {})
    if not isinstance(paths, dict) or not isinstance(execution, dict):
        raise ValueError("paths and execution must be TOML tables")
    default_path = Path(state_path).resolve(strict=False).with_name("heartbeat.json")
    raw_path = paths.get("heartbeat")
    explicit = raw_path is not None
    if raw_path is None:
        heartbeat_path = default_path
    elif not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("paths.heartbeat must be a non-empty string")
    else:
        heartbeat_path = Path(raw_path)
        if not heartbeat_path.is_absolute():
            heartbeat_path = Path.cwd() / heartbeat_path
        heartbeat_path = heartbeat_path.resolve(strict=False)
    raw_interval = execution.get("heartbeat_interval_seconds", 5)
    if isinstance(raw_interval, bool) or not isinstance(raw_interval, (int, float)):
        raise ValueError("execution.heartbeat_interval_seconds must be numeric")
    interval = float(raw_interval)
    if not math.isfinite(interval) or not 1.0 <= interval <= 60.0:
        raise ValueError("execution.heartbeat_interval_seconds must be between 1 and 60")
    return RuntimeHeartbeatSettings(
        path=heartbeat_path,
        interval_seconds=interval,
        base_runtime_dir=Path(state_path).resolve(strict=False).parent,
        explicitly_configured=explicit,
    )


def activate_heartbeat_settings(
    config_path: str | Path,
    *,
    state_path: str | Path,
) -> RuntimeHeartbeatSettings:
    global _ACTIVE_SETTINGS
    _ACTIVE_SETTINGS = load_heartbeat_settings(config_path, state_path=state_path)
    return _ACTIVE_SETTINGS


def active_heartbeat_settings(*, state_path: str | Path) -> RuntimeHeartbeatSettings:
    if _ACTIVE_SETTINGS is not None:
        return _ACTIVE_SETTINGS
    state = Path(state_path).resolve(strict=False)
    return RuntimeHeartbeatSettings(
        path=state.with_name("heartbeat.json"),
        interval_seconds=5.0,
        base_runtime_dir=state.parent,
        explicitly_configured=False,
    )
