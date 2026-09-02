"""Adapt validated application configuration for runtime heartbeat wiring."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import AppConfig


@dataclass(frozen=True)
class RuntimeHeartbeatSettings:
    path: Path
    interval_seconds: float
    base_runtime_dir: Path

    def path_for_state(self, state_path: str | Path) -> Path:
        runtime = Path(state_path).resolve(strict=False).parent
        if runtime == self.base_runtime_dir:
            return self.path.resolve(strict=False)
        return (runtime / self.path.name).resolve(strict=False)


def heartbeat_settings(config: AppConfig) -> RuntimeHeartbeatSettings:
    """Derive runtime settings from the one already-validated AppConfig object."""

    return RuntimeHeartbeatSettings(
        path=Path(config.heartbeat_path).resolve(strict=False),
        interval_seconds=config.heartbeat_interval_seconds,
        base_runtime_dir=Path(config.state_path).resolve(strict=False).parent,
    )
