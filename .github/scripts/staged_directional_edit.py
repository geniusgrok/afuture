# message: test: require same-direction resize turnover reduction
from pathlib import Path

path = Path("tests/test_directional_execution_efficiency.py")
text = path.read_text(encoding="utf-8")
old = "    assert candidate_turnover <= legacy_turnover + 1e-12\n"
new = "    assert candidate_turnover < legacy_turnover - 1e-12\n"
if text.count(old) != 1:
    raise SystemExit(f"expected one baseline turnover assertion, got {text.count(old)}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
