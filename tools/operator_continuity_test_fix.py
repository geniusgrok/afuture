from pathlib import Path

path = Path("tests/test_stress90_operator_roll_forward.py")
text = path.read_text(encoding="utf-8")
old = '        account_registry_path=str(tmp_path / ".account-runtime-registry.json"),\n'
if old in text:
    path.write_text(text.replace(old, "", 1), encoding="utf-8")

plan_path = Path("tests/test_stress90_operator_roll_forward_plan.py")
plan_text = plan_path.read_text(encoding="utf-8")
plan_text = plan_text.replace(
    '    assert plan.targets.generic_target.runtime_mode == "halted"\n',
    '    assert plan.targets.generic_target.runtime_mode == "HALTED"\n',
)
plan_path.write_text(plan_text, encoding="utf-8")
