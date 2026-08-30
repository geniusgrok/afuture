"""Cross-platform runtime path constants without POSIX locking dependencies."""

from pathlib import Path

PRODUCTION_ACCOUNT_RUNTIME_REGISTRY_PATH = Path("/var/lib/afuture/account-runtime-registry.json")
