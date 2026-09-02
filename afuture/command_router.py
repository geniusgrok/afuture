"""Canonical CLI router for local provenance, recovery, and production supervision."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

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
        "prepare-session",
        "watchdog",
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
    elif command == "prepare-session":
        parser.add_argument("--config", required=True)
        parser.add_argument("--confirm-live", action="store_true")
        parser.add_argument("--output", required=True)
        parser.add_argument("--refresh-ohlc", action="store_true")
        parser.add_argument("--shadow-account", action="store_true")
    elif command == "watchdog":
        parser.add_argument("--config", required=True)
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--max-age-seconds", type=float, default=15.0)
    else:  # pragma: no cover - internal dispatch contract
        raise RuntimeError(f"unsupported routed command: {command}")
    return parser


def _is_stress90(config) -> bool:
    directional = getattr(config, "directional", None)
    return bool(
        getattr(directional, "enabled", False) and getattr(directional, "policy", "") == "stress90"
    )


def _configured_environment(config) -> str:
    return str(getattr(config, "ctp_environment", "")).lower()


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
        if command == "prepare-session":
            from .session_preflight import ProductionPreflightBackend, SessionPreflightRunner

            try:
                config = load_config(args.config, require_ctp_credentials=True)
                _require_stress90_config(config, command)
                backend = ProductionPreflightBackend(
                    config=config,
                    config_path=args.config,
                    confirm_live=bool(args.confirm_live),
                    shadow_account=bool(args.shadow_account),
                )
                code, payload = SessionPreflightRunner(
                    config=config,
                    config_path=args.config,
                    output_path=args.output,
                    confirm_live=bool(args.confirm_live),
                    refresh_ohlc=bool(args.refresh_ohlc),
                    shadow_account=bool(args.shadow_account),
                    backend=backend,
                ).run()
                _canonical_print(payload)
                return code
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
                return 3
        if command == "watchdog":
            from .heartbeat_config import heartbeat_settings
            from .watchdog import run_watchdog_once

            config = load_config(args.config, require_ctp_credentials=False)
            _require_stress90_config(config, command)
            settings = heartbeat_settings(config)
            result = run_watchdog_once(
                config=config,
                config_path=args.config,
                heartbeat_path=settings.path_for_state(config.state_path),
                max_age_seconds=float(args.max_age_seconds),
            )
            _canonical_print(result.to_dict())
            return 0 if result.passed else 2
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
        return None
    if not _is_stress90(config):
        return None
    production = _configured_environment(config) == "production"
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


def _configured_live_account_digest(config) -> str:
    credentials = getattr(config, "ctp", None)
    if credentials is None:
        raise RuntimeError("live account identity requires CTP configuration")
    broker_id = str(getattr(credentials, "broker_id", ""))
    user_id = str(getattr(credentials, "user_id", ""))
    environment = str(getattr(credentials, "environment", "")).lower()
    account_id = str(getattr(credentials, "account_id", ""))
    currency_id = str(getattr(credentials, "currency_id", ""))
    if not account_id or not currency_id:
        material = "\0".join(("afuture.ctp-account.v1", broker_id, user_id, environment))
    else:
        material = "\0".join(
            (
                "afuture.ctp-economic-account.v1",
                broker_id,
                environment,
                account_id,
                currency_id,
                str(getattr(credentials, "investor_id", "")),
                str(getattr(credentials, "invest_unit_id", "")),
            )
        )
    return sha256(material.encode()).hexdigest()


def _runtime_heartbeat_context(
    *,
    config,
    runtime: Path,
    role: str,
    deployment_digest: str,
    process_uuid: str | None,
):
    from .heartbeat_config import heartbeat_settings
    from .runtime_heartbeat import RuntimeHeartbeatContext, runtime_identity_digest
    from .stress90_risk_overlay import stress90_risk_overlay_digest

    settings = heartbeat_settings(config)
    live_account = _configured_live_account_digest(config)
    account_digest = (
        sha256(f"afuture.shadow-account.v1\0{live_account}".encode()).hexdigest()
        if role == "shadow"
        else live_account
    )
    heartbeat_path = (
        settings.path_for_state(config.state_path)
        if role == "live"
        else (runtime / settings.path.name).resolve(strict=False)
    )
    return RuntimeHeartbeatContext(
        path=str(heartbeat_path),
        interval_seconds=settings.interval_seconds,
        mode=role,
        policy=str(getattr(config.directional, "policy", "")),
        canonical_runtime_digest=runtime_identity_digest(
            runtime_dir=str(runtime.resolve(strict=False)),
            deployment_digest=deployment_digest,
            role=role,
        ),
        account_identity_digest=account_digest,
        deployment_identity_digest=deployment_digest,
        risk_overlay_digest=stress90_risk_overlay_digest(config.directional, config.risk),
        process_uuid=process_uuid,
    )


def _halt_unknown_process_run(config, *, reason: str) -> None:
    from .state import RuntimeState, StateStore
    from .stress90_activation_permit import Stress90ActivationPermitStore

    store = StateStore(config.state_path)
    current = store.load_record()
    state = RuntimeState() if current is None else current.state
    halted = replace(
        state,
        kill_switch=True,
        kill_reason=reason,
        runtime_mode="HALTED",
        reconciled=False,
    )
    store.save(
        halted,
        expected_sequence=0 if current is None else current.sequence,
        expected_checksum="" if current is None else current.checksum,
    )
    runtime = Path(config.state_path).resolve(strict=False).parent
    permit_path = runtime / "stress90_activation_permit.json"
    previous = permit_path.with_name(permit_path.name + ".prev")
    if (
        permit_path.exists()
        or permit_path.is_symlink()
        or previous.exists()
        or previous.is_symlink()
    ):
        Stress90ActivationPermitStore(permit_path).invalidate(reason)


def _run_live_with_process_fence(argv: list[str]) -> int:
    from .deployment_identity import DeploymentIdentityStore
    from .process_run import (
        PROCESS_FENCE_EXIT_CODE,
        ProcessRunStore,
        apply_unclean_restart_fence,
        set_current_process_uuid,
    )
    from .runtime_heartbeat import (
        configure_runtime_heartbeat,
        install_engine_heartbeat_hooks,
        stopped_heartbeat_succeeded,
    )
    from .state import StateStore

    config_path = _extract_option(argv, "--config")
    config = load_config(config_path, require_ctp_credentials=True)
    runtime = _runtime_for(config, shadow=False)
    deployment = DeploymentIdentityStore(runtime / "deployment_identity.json").load_required()
    state_store = StateStore(config.state_path)
    start_state = state_store.load_required_record()
    account_digest = _configured_live_account_digest(config)
    runtime_digest = sha256(
        "\0".join(
            (
                "afuture.process-runtime.v1",
                str(runtime.resolve(strict=False)),
                deployment.checksum,
            )
        ).encode()
    ).hexdigest()
    process_store = ProcessRunStore(runtime / "process_run.json")
    try:
        fence = apply_unclean_restart_fence(
            process_store=process_store,
            state_store=state_store,
            runtime_dir=runtime,
            deployment_digest=deployment.checksum,
            runtime_identity_digest=runtime_digest,
            account_identity_digest=account_digest,
        )
    except Exception as exc:
        reason = f"unclean restart fence: local fence failure ({type(exc).__name__})"
        try:
            _halt_unknown_process_run(config, reason=reason)
        except Exception:
            pass
        _canonical_print(
            {
                "passed": False,
                "command": "live",
                "restart_fence": True,
                "error": reason,
                "orders_sent": 0,
                "cancels_sent": 0,
            }
        )
        return PROCESS_FENCE_EXIT_CODE
    if fence.blocked:
        _canonical_print(
            {
                "passed": False,
                "command": "live",
                "restart_fence": True,
                "reason": fence.reason,
                "required_next_actions": [
                    "prepare-session",
                    "doctor",
                    "issue fresh activation permit",
                ],
                "orders_sent": 0,
                "cancels_sent": 0,
            }
        )
        return fence.exit_code

    record = process_store.begin(
        deployment_digest=deployment.checksum,
        runtime_identity_digest=runtime_digest,
        account_identity_digest=account_digest,
        start_state_checksum=start_state.checksum,
    )
    process_uuid = record.process_uuid
    set_current_process_uuid(process_uuid)
    context = _runtime_heartbeat_context(
        config=config,
        runtime=runtime,
        role="live",
        deployment_digest=deployment.checksum,
        process_uuid=process_uuid,
    )
    configure_runtime_heartbeat(context)
    install_engine_heartbeat_hooks()
    try:
        process_store.mark_phase(
            process_uuid,
            "broker_constructed",
            state_checksum=start_state.checksum,
        )
        from .cli import run_command

        code = run_command(argv)
        final_state = state_store.load_required_record()
        process_store.mark_phase(
            process_uuid,
            "shutdown_state_saved",
            state_checksum=final_state.checksum,
        )
        heartbeat_status = stopped_heartbeat_succeeded()
        if heartbeat_status is False:
            _canonical_print(
                {
                    "passed": False,
                    "command": "live",
                    "restart_fence": True,
                    "error": "final stopped heartbeat was not durably written",
                    "orders_sent": 0,
                    "cancels_sent": 0,
                }
            )
            return PROCESS_FENCE_EXIT_CODE
        if heartbeat_status is True:
            process_store.mark_phase(
                process_uuid,
                "heartbeat_stopped_pending_receipt",
                state_checksum=final_state.checksum,
            )
        process_store.finish_clean(
            process_uuid,
            stop_state_checksum=final_state.checksum,
        )
        return code
    except Exception as exc:
        _canonical_print(
            {
                "passed": False,
                "command": "live",
                "restart_fence": True,
                "error_category": type(exc).__name__,
                "orders_sent": 0,
                "cancels_sent": 0,
            }
        )
        return PROCESS_FENCE_EXIT_CODE
    finally:
        set_current_process_uuid(None)
        configure_runtime_heartbeat(None)


def _configure_shadow_heartbeat(argv: list[str]) -> None:
    from .deployment_identity import DeploymentIdentityStore
    from .runtime_heartbeat import configure_runtime_heartbeat, install_engine_heartbeat_hooks

    config_path = _extract_option(argv, "--config")
    config = load_config(config_path, require_ctp_credentials=True)
    if not _is_stress90(config):
        return
    runtime = _runtime_for(config, shadow=True)
    deployment = DeploymentIdentityStore(runtime / "deployment_identity.json").load_required()
    configure_runtime_heartbeat(
        _runtime_heartbeat_context(
            config=config,
            runtime=runtime,
            role="shadow",
            deployment_digest=deployment.checksum,
            process_uuid=None,
        )
    )
    install_engine_heartbeat_hooks()


def _print_combined_help() -> int:
    from .cli import build_parser

    parser = build_parser()
    parser.print_help()
    print("\nLocal provenance, recovery, and supervision commands:")
    for command in sorted(_NEW_COMMANDS):
        print(f"  {command}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args == ["--help"] or args == ["-h"]:
        if args:
            return _print_combined_help()
        from .cli import run_command

        return run_command(args)
    command = args[0]
    if command in _NEW_COMMANDS:
        return _run_new(command, args[1:])
    preflight = _deployment_preflight(command, args[1:])
    if preflight is not None:
        return preflight
    if command == "live":
        config_path = _extract_option(args[1:], "--config")
        if config_path:
            try:
                config = load_config(config_path, require_ctp_credentials=True)
            except Exception:
                from .cli import run_command

                return run_command(args)
            if _is_stress90(config) and _configured_environment(config) == "production":
                return _run_live_with_process_fence(args)
    if command == "shadow":
        try:
            _configure_shadow_heartbeat(args)
        except Exception as exc:
            _canonical_print(
                {
                    "passed": False,
                    "command": "shadow",
                    "error": str(exc),
                    "orders_sent": 0,
                    "cancels_sent": 0,
                }
            )
            return 2
    from .cli import run_command

    return run_command(args)
