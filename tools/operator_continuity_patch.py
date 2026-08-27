from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    if new and new in text:
        return
    if old not in text:
        raise SystemExit(f"patch anchor missing: {path}: {old[:80]!r}")
    file_path.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "afuture/directional.py",
    '    account_exclusive: bool = True\n\n    def validate(self) -> None:\n',
    '    account_exclusive: bool = True\n'
    '    account_continuity_mode: str = "strict"\n\n'
    '    def validate(self) -> None:\n',
)
replace_once(
    "afuture/directional.py",
    '        require_bool(self.account_exclusive, "directional.account_exclusive")\n'
    '        require_string(self.policy, "directional.policy")\n',
    '        require_bool(self.account_exclusive, "directional.account_exclusive")\n'
    '        require_string(self.policy, "directional.policy")\n'
    '        require_string(\n'
    '            self.account_continuity_mode, "directional.account_continuity_mode"\n'
    '        )\n'
    '        if self.account_continuity_mode not in {"strict", "operator_managed"}:\n'
    '            raise ValueError(\n'
    '                "directional.account_continuity_mode must be strict or operator_managed"\n'
    '            )\n',
)

replace_once(
    "afuture/config.py",
    '    config = DirectionalConfig(**cast(Any, values))\n'
    '    config.validate()\n'
    '    if config.enabled and mode == "live" and not config.policy:\n',
    '    config = DirectionalConfig(**cast(Any, values))\n'
    '    config.validate()\n'
    '    if config.account_continuity_mode == "operator_managed" and (\n'
    '        mode != "live"\n'
    '        or not config.enabled\n'
    '        or config.policy != "stress90"\n'
    '        or not config.account_exclusive\n'
    '    ):\n'
    '        raise ValueError(\n'
    '            "directional.account_continuity_mode=operator_managed requires "\n'
    '            "system.mode=live, directional.enabled=true, policy=stress90, "\n'
    '            "and account_exclusive=true"\n'
    '        )\n'
    '    if config.enabled and mode == "live" and not config.policy:\n',
)

replace_once(
    "afuture/account_runtime_registry.py",
    '        "stress90_crash_fill_recovery",\n        "legacy",\n',
    '        "stress90_crash_fill_recovery",\n'
    '        "operator_managed_continuity",\n'
    '        "legacy",\n',
)

recovery_tail = '''            return self._save_with_nonce_unlocked(current, tuple(bindings.values()), nonce_receipt)\n\n    def _is_exact_unchanged_binding_acknowledgement_retry(\n'''
operator_method = '''            return self._save_with_nonce_unlocked(current, tuple(bindings.values()), nonce_receipt)\n\n    def acknowledge_operator_continuity_operation(\n        self,\n        account_identity_digest: str,\n        runtime_dir: str | Path,\n        source_epoch: str,\n        operation_id: str,\n        request_digest: str,\n    ) -> AccountRuntimeRegistryRecord:\n        """Globally consume one semantic operator-continuity request nonce."""\n\n        account = _sha(account_identity_digest, "economic account identity")\n        runtime, runtime_digest = _canonical_runtime(runtime_dir)\n        source = _sha(source_epoch, "source account runtime epoch")\n        operation = _sha(operation_id, "operator continuity operation")\n        request = _sha(request_digest, "operator continuity request")\n        operation_receipt = _operation_receipt(\n            "operator_managed_continuity",\n            operation_id=operation,\n            request_digest=request,\n            account_identity_digest=account,\n            canonical_runtime=runtime,\n            account_epoch=source,\n        )\n        nonce_receipt = self._nonce_receipt_for(\n            operation=operation,\n            kind="operator_managed_continuity",\n            account=account,\n            runtime=runtime,\n            runtime_digest=runtime_digest,\n            epoch=source,\n            semantic_request_digest=operation_receipt,\n        )\n        with self._exclusive_lock() as lock:\n            current = self._load_unlocked(\n                required=True,\n                legacy_lock_evidence=lock.legacy_lock_evidence,\n            )\n            assert current is not None\n            self._require_schema3_nonce_ledger(current)\n            self._reconcile_nonce_pending_unlocked(current)\n            bindings = {item.account_identity_digest: item for item in current.bindings}\n            binding = bindings.get(account)\n            if binding is None:\n                raise AccountRuntimeRegistryError(\n                    "operator continuity registry binding is missing"\n                )\n            if (\n                binding.canonical_runtime != runtime\n                or binding.runtime_identity_digest != runtime_digest\n                or binding.account_epoch != source\n            ):\n                raise AccountRuntimeRegistryError(\n                    "operator continuity registry source CAS mismatch"\n                )\n            existing_nonce = self._nonce_ledger().lookup(current.nonce_root, operation)\n            if existing_nonce is not None:\n                if (\n                    binding.last_operation_id == operation\n                    and binding.operation_kinds[-1] == "operator_managed_continuity"\n                    and binding.last_operation_receipt_digest == operation_receipt\n                    and existing_nonce == nonce_receipt\n                ):\n                    return current\n                raise AccountRuntimeRegistryError(\n                    "operator continuity operation was already consumed for a different request"\n                )\n            operation_history, operation_kinds = _bound_operation_heads(\n                binding.operation_history,\n                binding.operation_kinds,\n                operation,\n                "operator_managed_continuity",\n            )\n            bindings[account] = _advance_binding_authority(\n                binding,\n                replace(\n                    binding,\n                    last_operation_id=operation,\n                    operation_history=operation_history,\n                    operation_kinds=operation_kinds,\n                    last_operation_receipt_digest=operation_receipt,\n                ),\n                operation_receipt=operation_receipt,\n            )\n            return self._save_with_nonce_unlocked(current, tuple(bindings.values()), nonce_receipt)\n\n    def _is_exact_unchanged_binding_acknowledgement_retry(\n'''
replace_once("afuture/account_runtime_registry.py", recovery_tail, operator_method)

replace_once(
    "afuture/trading_day_evidence.py",
    '            "stress90_to_execution_aligned",\n        } or status not in {"prepared", "committed"}:\n',
    '            "stress90_to_execution_aligned",\n'
    '            "settlement_roll_forward",\n'
    '        } or status not in {"prepared", "committed"}:\n',
)

replace_once(
    "tests/test_stress90_operator_continuity.py",
    'import json\nimport os\n',
    'import json\nimport os\nfrom hashlib import sha256\n',
)
replace_once(
    "tests/test_stress90_operator_continuity.py",
    '        canonical_runtime_digest=_SHA_1,\n',
    '        canonical_runtime_digest=sha256(\n'
    '            f"runtime:{runtime.resolve()}".encode()\n'
    '        ).hexdigest(),\n',
)
replace_once(
    "afuture/stress90_operator_continuity.py",
    "from typing import Any\n\n",
    "",
)
replace_once(
    "tests/test_stress90_operator_continuity.py",
    "import json\nimport os\nfrom hashlib import sha256\nfrom dataclasses import replace\n",
    "import json\nimport os\nfrom dataclasses import replace\nfrom hashlib import sha256\n",
)
replace_once(
    "tests/test_stress90_operator_continuity.py",
    "from afuture.account_runtime_registry import (\n    AccountRuntimeRegistry,\n    AccountRuntimeRegistryError,\n    ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,\n)\n",
    "from afuture.account_runtime_registry import (\n    ACCOUNT_RUNTIME_REGISTRY_INITIALIZE_CONFIRMATION,\n    AccountRuntimeRegistry,\n    AccountRuntimeRegistryError,\n)\n",
)
replace_once(
    "tests/test_stress90_operator_continuity.py",
    "    Stress90OperatorContinuityEvidence,\n    Stress90OperatorContinuityError,\n",
    "    Stress90OperatorContinuityError,\n    Stress90OperatorContinuityEvidence,\n",
)
