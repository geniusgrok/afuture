# message: docs: fix final L3 evidence reference
from pathlib import Path

README = Path("README.md")
text = README.read_text(encoding="utf-8")
old = "PR merge ref `7664851987a59c2d87d1084376ffbd9649863b2c`"
new = "PR merge ref `1ec387433ee5011e44bc214e4e4c83a28e72c93c`"
if text.count(old) != 1:
    raise SystemExit(f"README stale merge ref count={text.count(old)}")
README.write_text(text.replace(old, new, 1), encoding="utf-8")

formal_docs = [
    "README.md",
    "docs/architecture.md",
    "docs/data-and-backtest.md",
    "docs/live-trading.md",
    "docs/production-checklist.md",
    "docs/directional-production-mechanics-evidence.md",
    "docs/research-final-evidence.md",
    "docs/return-target-100-evidence.md",
]
for name in formal_docs:
    body = Path(name).read_text(encoding="utf-8")
    if "109.0636%" not in body or "28.9559%" not in body:
        raise SystemExit(f"{name}: final promoted metrics missing")
    if "7664851987a59c2d87d1084376ffbd9649863b2c" in body:
        raise SystemExit(f"{name}: stale final merge ref remains")

code = Path("afuture/directional.py").read_text(encoding="utf-8")
if "calm conditions can recover some capacity" in code:
    raise SystemExit("directional.py: obsolete adaptive-margin comment remains")
if "never expands normal target margin toward the 35% hard gate" not in code:
    raise SystemExit("directional.py: final adaptive-margin semantics missing")

spec = Path("docs/superpowers/specs/2026-08-23-directional-execution-efficiency-design.md").read_text(encoding="utf-8")
if "hard_share * (1-max_daily_loss_ratio) / (1+shock)" in spec:
    raise SystemExit("spec: rejected adaptive-margin formula remains")
if "Final disposition" not in spec:
    raise SystemExit("spec: final disposition missing")
