from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    if new in text:
        return
    if old not in text:
        raise SystemExit(f"patch anchor missing: {path}: {old[:100]!r}")
    file_path.write_text(text.replace(old, new, 1), encoding="utf-8")


permit = Path("afuture/stress90_activation_permit.py")
text = permit.read_text(encoding="utf-8")
text = text.replace(
    "STRESS90_ACTIVATION_PERMIT_SCHEMA_VERSION = 4",
    "STRESS90_ACTIVATION_PERMIT_SCHEMA_VERSION = 5",
    1,
)
text = text.replace(
    "    active_order_count: int\n\n\n@dataclass(frozen=True)\nclass Stress90ActivationPermit:\n",
    "    active_order_count: int\n"
    '    account_continuity_mode: str = "strict"\n'
    '    operator_continuity_receipt_digest: str = ""\n\n\n'
    "@dataclass(frozen=True)\nclass Stress90ActivationPermit:\n",
    1,
)
validation_anchor = '''    if (\n        isinstance(evidence.active_order_count, bool)\n        or not isinstance(evidence.active_order_count, int)\n        or evidence.active_order_count < 0\n    ):\n        raise Stress90ActivationPermitIntegrityError(\n            "active order count must be a non-negative integer"\n        )\n    return asdict(evidence)\n'''
validation_new = '''    if (\n        isinstance(evidence.active_order_count, bool)\n        or not isinstance(evidence.active_order_count, int)\n        or evidence.active_order_count < 0\n    ):\n        raise Stress90ActivationPermitIntegrityError(\n            "active order count must be a non-negative integer"\n        )\n    if evidence.account_continuity_mode not in {"strict", "operator_managed"}:\n        raise Stress90ActivationPermitIntegrityError(\n            "activation evidence account continuity mode is invalid"\n        )\n    if evidence.account_continuity_mode == "strict":\n        if evidence.operator_continuity_receipt_digest != "":\n            raise Stress90ActivationPermitIntegrityError(\n                "strict activation evidence cannot bind an operator continuity receipt"\n            )\n    else:\n        _valid_sha256(\n            evidence.operator_continuity_receipt_digest,\n            name="operator continuity receipt digest",\n        )\n    return asdict(evidence)\n'''
if validation_new not in text:
    if validation_anchor not in text:
        raise SystemExit("permit evidence validation anchor missing")
    text = text.replace(validation_anchor, validation_new, 1)

signature_old = '''    catalog: list[ContractInfo],\n    account_registry_path: str | Path | None = None,\n) -> Stress90ActivationEvidence:\n'''
signature_new = '''    catalog: list[ContractInfo],\n    account_registry_path: str | Path | None = None,\n    account_continuity_mode: str = "strict",\n) -> Stress90ActivationEvidence:\n'''
if signature_new not in text:
    if signature_old not in text:
        raise SystemExit("collect evidence signature anchor missing")
    text = text.replace(signature_old, signature_new, 1)

registry_anchor = '''    registry_evidence = AccountRuntimeRegistry(registry_path).require_binding_evidence(\n        account_identity_digest,\n        runtime.resolve(strict=False),\n        policy.state.live_account_epoch,\n    )\n    prepared = policy.state.prepared_decision\n'''
registry_new = '''    registry_evidence = AccountRuntimeRegistry(registry_path).require_binding_evidence(\n        account_identity_digest,\n        runtime.resolve(strict=False),\n        policy.state.live_account_epoch,\n    )\n    if account_continuity_mode == "strict":\n        operator_continuity_receipt_digest = ""\n    elif account_continuity_mode == "operator_managed":\n        from .stress90_operator_continuity import Stress90OperatorContinuityStore\n\n        operator_receipt = Stress90OperatorContinuityStore(\n            runtime / "stress90_operator_continuity.json"\n        ).require_current_binding(\n            account_identity_digest=account_identity_digest,\n            account_epoch=policy.state.live_account_epoch,\n            canonical_runtime=runtime,\n            target_ctp_trading_day=day,\n        )\n        if (\n            operator_receipt.target_registry_receipt_digest\n            != registry_evidence.binding_receipt_digest\n        ):\n            raise Stress90ActivationPermitIntegrityError(\n                "operator continuity receipt is not bound to the current account registry receipt"\n            )\n        operator_continuity_receipt_digest = operator_receipt.checksum\n    else:\n        raise Stress90ActivationPermitIntegrityError(\n            "activation evidence account continuity mode is invalid"\n        )\n    prepared = policy.state.prepared_decision\n'''
if registry_new not in text:
    if registry_anchor not in text:
        raise SystemExit("permit registry anchor missing")
    text = text.replace(registry_anchor, registry_new, 1)

return_anchor = '''        session_activity_ownership_digest=session_activity_proof.ownership_digest,\n        active_order_count=0,\n    )\n'''
return_new = '''        session_activity_ownership_digest=session_activity_proof.ownership_digest,\n        active_order_count=0,\n        account_continuity_mode=account_continuity_mode,\n        operator_continuity_receipt_digest=operator_continuity_receipt_digest,\n    )\n'''
if return_new not in text:
    if return_anchor not in text:
        raise SystemExit("permit return anchor missing")
    text = text.replace(return_anchor, return_new, 1)

issue_sig_old = '''    strong_confirmation: str,\n    account_registry_path: str | Path | None = None,\n) -> Stress90ActivationPermitRecord:\n'''
issue_sig_new = '''    strong_confirmation: str,\n    account_registry_path: str | Path | None = None,\n    account_continuity_mode: str = "strict",\n) -> Stress90ActivationPermitRecord:\n'''
if issue_sig_new not in text:
    if issue_sig_old not in text:
        raise SystemExit("permit issue signature anchor missing")
    text = text.replace(issue_sig_old, issue_sig_new, 1)

issue_call_old = '''        catalog=catalog,\n        account_registry_path=account_registry_path,\n    )\n'''
issue_call_new = '''        catalog=catalog,\n        account_registry_path=account_registry_path,\n        account_continuity_mode=account_continuity_mode,\n    )\n'''
# First occurrence belongs to issue_stress90_doctor_permit's collect call after its definition.
issue_pos = text.index("def issue_stress90_doctor_permit(")
issue_tail = text[issue_pos:]
if "account_continuity_mode=account_continuity_mode" not in issue_tail.split("class Stress90TechnicalActivationAuthority", 1)[0]:
    if issue_call_old not in issue_tail:
        raise SystemExit("permit issue collect-call anchor missing")
    issue_tail = issue_tail.replace(issue_call_old, issue_call_new, 1)
    text = text[:issue_pos] + issue_tail

authority_sig_old = '''    def __init__(\n        self,\n        runtime_dir: str | Path,\n        *,\n        account_registry_path: str | Path | None = None,\n    ) -> None:\n        self.runtime_dir = Path(runtime_dir)\n        self.account_registry_path = account_registry_path\n'''
authority_sig_new = '''    def __init__(\n        self,\n        runtime_dir: str | Path,\n        *,\n        account_registry_path: str | Path | None = None,\n        account_continuity_mode: str = "strict",\n    ) -> None:\n        self.runtime_dir = Path(runtime_dir)\n        self.account_registry_path = account_registry_path\n        if account_continuity_mode not in {"strict", "operator_managed"}:\n            raise Stress90ActivationPermitIntegrityError(\n                "technical activation account continuity mode is invalid"\n            )\n        self.account_continuity_mode = account_continuity_mode\n'''
if authority_sig_new not in text:
    if authority_sig_old not in text:
        raise SystemExit("activation authority init anchor missing")
    text = text.replace(authority_sig_old, authority_sig_new, 1)

authority_pos = text.index("class Stress90TechnicalActivationAuthority")
authority_tail = text[authority_pos:]
activate_old = '''            catalog=broker.get_contract_catalog(),\n            account_registry_path=self.account_registry_path,\n        )\n'''
activate_new = '''            catalog=broker.get_contract_catalog(),\n            account_registry_path=self.account_registry_path,\n            account_continuity_mode=self.account_continuity_mode,\n        )\n'''
if activate_new not in authority_tail:
    if activate_old not in authority_tail:
        raise SystemExit("activation authority collect-call anchor missing")
    authority_tail = authority_tail.replace(activate_old, activate_new, 1)
    text = text[:authority_pos] + authority_tail
permit.write_text(text, encoding="utf-8")

runtime = Path("afuture/runtime_factory.py")
runtime_text = runtime.read_text(encoding="utf-8")
runtime_old = '''            common["technical_activation_authority"] = Stress90TechnicalActivationAuthority(\n                runtime_dir,\n                account_registry_path=config.account_registry_path,\n            )\n'''
runtime_new = '''            common["technical_activation_authority"] = Stress90TechnicalActivationAuthority(\n                runtime_dir,\n                account_registry_path=config.account_registry_path,\n                account_continuity_mode=config.directional.account_continuity_mode,\n            )\n'''
if runtime_new not in runtime_text:
    if runtime_old not in runtime_text:
        raise SystemExit("runtime factory authority anchor missing")
    runtime.write_text(runtime_text.replace(runtime_old, runtime_new, 1), encoding="utf-8")

cli = Path("afuture/cli.py")
cli_text = cli.read_text(encoding="utf-8")
needle = "            issue_stress90_doctor_permit(\n"
start = cli_text.index(needle)
end = cli_text.index("\n            )", start)
block = cli_text[start:end]
if "account_continuity_mode=" not in block:
    block += "\n                account_continuity_mode=config.directional.account_continuity_mode,"
    cli_text = cli_text[:start] + block + cli_text[end:]
cli.write_text(cli_text, encoding="utf-8")
