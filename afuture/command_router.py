"""Canonical CLI router for local provenance/recovery commands and deployment gates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from .config import load_config

_NEW_COMMANDS = frozenset(
    {
        "stress90-bundle-create",
        "stress90-bundle-verify",
        "stress90-bundle-install",
        "deployment-seal",
        "deployment-verify",
        "backup-runtime",
        "verify-backup",
        "restore-runtime",
    }
)
_DEPLOYMENT_GATED_COMMANDS = frozenset({"doctor", "shadow", "live"})


def _canonical_print(payload: object) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def _parser(command: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"afuture {command}")
    if command == "stress90-bundle-create":
        parser.add_argument("--config", required=True)
        parser.add_argument("--runtime-dir", required=True)
        parser.add_argument("--output", required=True)
    elif command == "stress90-bundle-verify":
        parser.add_argument("--bundle", required=True)
    elif command == "stress90-bundle-install":
        parser.add_argument("--config", required=True)
        parser.add_argument("--bundle", required=True)
        parser.add_argument("--runtime-dir", required=True)
    elif command == "deployment-seal":
        parser.add_argument("--config", required=True)
        parser.add_argument("--bundle", required=True)
        parser.add_argument("--runtime-dir", default="")
        parser.add_argument("--shadow-account", action="store_true")
    elif command == "deployment-verify":
        parser.add_argument("--config", required=True)
        parser.add_argument("--runtime-dir", default="")
        parser.add_argument("--shadow-account", action="store_true")
    elif command == "backup-runtime":
        parser.add_argument("--config", required=True)
        parser.add_argument("--output", required=True)
    elif command == "verify-backup":
        parser.add_argument("--backup", required=True)
    elif command == "restore-runtime":
        parser.add_argument("--backup", required=True)
        parser.add_argument("--runtime-dir", required=True)
        parser.add_argument("--account-registry-path", required=True)
        parser.add_argument("--registry-staging-path", required=True)
    else:  # pragma: no cover - internal dispatch contract
        raise RuntimeError(f"unsupported routed command: {command}")
    return parser


def _is_stress90(config) -> bool:
    directional = getattr(config, "directional", None)
    return bool(
        getattr(directional, "enabled", False) and getattr(directional, "policy", "") == "stress90"
    )


def _raw_environment(config_path: str | Path) -> str:
    try:
        raw = tomllib.loads(Path(config_path).read_text(encoding="utf-8"))
        section = raw.get("ctp", {})
        if not isinstance(section, dict):
            return ""
        value = section.get("environment", "")
        return str(value).lower() if isinstance(value, str) else ""
    except (OSError, ValueError):
        return ""


def _runtime_for(config, *, shadow: bool, explicit: str = "") -> Path:
    if explicit:
        return Path(explicit).resolve(strict=False)
    base = Path(config.state_path).resolve(strict=False).parent
    return base / "shadow" if shadow else base


def _require_stress90_config(config, command: str) -> None:
    if not _is_stress90(config):
        raise RuntimeError(f"{command} requires directional.policy=stress90")


def _run_new(command: str, argv: list[str]) -> int:
    args = _parser(command).parse_args(argv)
    try:
        if command == "stress90-bundle-create":
            from .stress90_bootstrap_bundle import create_stress90_bundle

            config = load_config(args.config, require_ctp_credentials=False)
            _require_stress90_config(config, command)
            bundle_result = create_stress90_bundle(
                runtime_dir=args.runtime_dir,
                output_path=args.output,
            )
            _canonical_print(
                {
                    "created": True,
                    "production_ready": bundle_result.production_ready,
                    "bundle_digest": bundle_result.bundle_digest,
                    "archive_sha256": bundle_result.archive_sha256,
                    "orders_sent": 0,
                    "cancels_sent": 0,
                }
            )
            return 0
        if command == "stress90-bundle-verify":
            from .stress90_bootstrap_bundle import verify_stress90_bundle

            bundle_verification = verify_stress90_bundle(
                args.bundle,
                require_production=True,
            )
            _canonical_print(
                {
                    "passed": True,
                    "production_ready": bundle_verification.production_ready,
                    "bundle_digest": bundle_verification.bundle_digest,
                    "archive_sha256": bundle_verification.archive_sha256,
                    "orders_sent": 0,
                    "cancels_sent": 0,
                }
            )
            return 0
        if command == "stress90-bundle-install":
            from .stress90_bootstrap_bundle import install_stress90_bundle

            config = load_config(args.config, require_ctp_credentials=False)
            _require_stress90_config(config, command)
            _canonical_print(
                install_stress90_bundle(
                    bundle_path=args.bundle,
                    runtime_dir=args.runtime_dir,
                )
            )
            return 0
        if command == "deployment-seal":
            from .deployment_identity import seal_deployment

            config = load_config(args.config, require_ctp_credentials=False)
            _require_stress90_config(config, command)
            runtime = _runtime_for(
                config,
                shadow=bool(args.shadow_account),
                explicit=args.runtime_dir,
            )
            record = seal_deployment(
                config=config,
                config_path=args.config,
                bundle_path=args.bundle,
                runtime_dir=runtime,
                account_registry_path=config.account_registry_path,
                deployment_role="shadow" if args.shadow_account else "live",
            )
            _canonical_print(
                {
                    "sealed": True,
                    "sequence": record.sequence,
                    "parent_checksum": record.parent_checksum,
                    "checksum": record.checksum,
                    "runtime_dir": str(runtime.resolve(strict=False)),
                    "deployment_role": "shadow" if args.shadow_account else "live",
                    "activation_permit_granted": False,
                    "orders_sent": 0,
                    "cancels_sent": 0,
                }
            )
            return 0
        if command == "deployment-verify":
            from .deployment_identity import require_matching_deployment

            config = load_config(args.config, require_ctp_credentials=False)
            _require_stress90_config(config, command)
            runtime = _runtime_for(
                config,
                shadow=bool(args.shadow_account),
                explicit=args.runtime_dir,
            )
            deployment_result = require_matching_deployment(
                config=config,
                config_path=args.config,
                runtime_dir=runtime,
                account_registry_path=config.account_registry_path,
                expected_role="shadow" if args.shadow_account else "live",
            )
            _canonical_print(deployment_result.to_dict())
            return 0
        if command == "backup-runtime":
            from .runtime_backup import backup_runtime

            config = load_config(args.config, require_ctp_credentials=False)
            _require_stress90_config(config, command)
            backup_result = backup_runtime(
                config=config,
                config_path=args.config,
                output_path=args.output,
            )
            _canonical_print(backup_result.to_dict())
            return 0
        if command == "verify-backup":
            from .runtime_backup import verify_backup_archive

            _canonical_print(verify_backup_archive(args.backup).to_dict())
            return 0
        if command == "restore-runtime":
            from .runtime_backup import restore_runtime

            restore_result = restore_runtime(
                backup_path=args.backup,
                runtime_dir=args.runtime_dir,
                account_registry_path=args.account_registry_path,
                registry_staging_path=args.registry_staging_path,
            )
            _canonical_print(restore_result)
            return 0
        raise RuntimeError(f"unhandled routed command: {command}")
    except Exception as exc:
        _canonical_print(
            {
                "passed": False,
                "command": command,
                "error": str(exc),
                "orders_sent": 0,
                "cancels_sent": 0,
            }
        )
        return 2


def _extract_option(argv: list[str], option: str) -> str:
    try:
        index = argv.index(option)
    except ValueError:
        return ""
    return argv[index + 1] if index + 1 < len(argv) else ""


def _deployment_preflight(command: str, argv: list[str]) -> int | None:
    config_path = _extract_option(argv, "--config")
    if not config_path:
        return None
    require_credentials = command in _DEPLOYMENT_GATED_COMMANDS
    try:
        config = load_config(config_path, require_ctp_credentials=require_credentials)
    except Exception:
        return None  # Delegate canonical parser/config error semantics to the existing CLI.
    if not _is_stress90(config):
        return None
    production = _raw_environment(config_path) == "production"
    if command == "status":
        from .deployment_identity import verify_deployment
        from .operations import build_local_status

        local = build_local_status(config)
        deployment_payload: dict[str, object]
        deployment_passed = False
        try:
            verification = verify_deployment(
                config=config,
                config_path=config_path,
                runtime_dir=_runtime_for(config, shadow=False),
                account_registry_path=config.account_registry_path,
            )
            deployment_payload = verification.to_dict()
            deployment_passed = verification.passed
        except Exception as exc:
            deployment_payload = {
                "passed": False,
                "error": str(exc),
                "orders_sent": 0,
                "cancels_sent": 0,
            }
        payload = local.to_dict()
        payload["deployment"] = deployment_payload
        payload["deployment_required"] = production
        payload["passed"] = bool(local.passed and (deployment_passed or not production))
        _canonical_print(payload)
        return 0 if payload["passed"] else 2
    if command not in _DEPLOYMENT_GATED_COMMANDS or not production:
        return None
    try:
        from .deployment_identity import require_matching_deployment

        shadow = command == "shadow" or (command == "doctor" and "--shadow-account" in argv)
        require_matching_deployment(
            config=config,
            config_path=config_path,
            runtime_dir=_runtime_for(config, shadow=shadow),
            account_registry_path=config.account_registry_path,
            expected_role="shadow" if shadow else "live",
        )
    except Exception as exc:
        _canonical_print(
            {
                "passed": False,
                "command": command,
                "deployment_required": True,
                "error": str(exc),
                "orders_sent": 0,
                "cancels_sent": 0,
            }
        )
        return 2
    return None


def _print_combined_help() -> int:
    from .cli import build_parser

    parser = build_parser()
    parser.print_help()
    print("\nLocal provenance and recovery commands:")
    for command in sorted(_NEW_COMMANDS):
        print(f"  {command}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args == ["--help"] or args == ["-h"]:
        return _print_combined_help() if args else _delegate(args)
    command = args[0]
    if command in _NEW_COMMANDS:
        return _run_new(command, args[1:])
    preflight = _deployment_preflight(command, args[1:])
    if preflight is not None:
        return preflight
    return _delegate(args)


def _delegate(argv: list[str]) -> int:
    from .cli import main as legacy_main

    return legacy_main(argv)
