from pathlib import Path

path = Path("tests/test_stress90_operator_roll_forward.py")
text = path.read_text(encoding="utf-8")
old = '        account_registry_path=str(tmp_path / ".account-runtime-registry.json"),\n'
if old in text:
    path.write_text(text.replace(old, "", 1), encoding="utf-8")
