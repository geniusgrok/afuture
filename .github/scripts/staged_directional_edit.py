# message: docs: finalize promoted L3 evidence consistency
# observable PR trigger: evidence-only sync; no economic behavior changes
from pathlib import Path

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

OLD_RUN = "32624688557"
FINAL_RUN = "32634296589"
OLD_HEAD = "198cad7ee62e0e6892ddf5c725525fb46c7383e2"
FINAL_HEAD = "921bd8b4820a8c1efa9c804a8ea1a1b56c2d188f"
OLD_MERGE = "7664851987a59c2d87d1084376ffbd9649863b2c"
FINAL_MERGE = "1ec387433ee5011e44bc214e4e4c83a28e72c93c"
OLD_ARTIFACT = "9489421243"
FINAL_ARTIFACT = "9491959916"
OLD_SHA = "6a9abb9eb15a542eda2683200bbf5f001613dccd9546bde11f85dc4f0aa6add7"
FINAL_SHA = "e531f2874cbc26c3a54ff561e074f59b86ac887a1f509e33effd6908eaa3144d"

for name in formal_docs:
    path = Path(name)
    body = path.read_text(encoding="utf-8")
    body = body.replace(OLD_RUN, FINAL_RUN)
    body = body.replace(OLD_HEAD, FINAL_HEAD)
    body = body.replace(OLD_MERGE, FINAL_MERGE)
    body = body.replace(OLD_ARTIFACT, FINAL_ARTIFACT)
    body = body.replace(OLD_SHA, FINAL_SHA)
    path.write_text(body, encoding="utf-8")

path = Path("docs/directional-production-mechanics-evidence.md")
body = path.read_text(encoding="utf-8")
old = '''Soft target share：

```text
hard_share = min(max_margin_ratio, 1 - min_available_ratio)
soft_target_margin_share = max(0, hard_share - max_daily_loss_ratio)
```

当前配置为：

```text
hard_share = min(35%, 75%) = 35%
soft target margin share = 35% - 5% = 30%
```

含义：正常目标不再故意贴着 35% hard margin boundary；保留现有 5% daily-loss budget 作为 mark-to-market 余量。**35% hard margin gate 没有变成 40%，也没有被绕过。**
'''
new = '''Soft target share：

```text
hard_share = min(max_margin_ratio, 1 - min_available_ratio)
conservative = max(0, hard_share - max_daily_loss_ratio)
shock = clamp(max(3%, abs(latest completed return), two-day sample volatility), 3%, 5%)
soft_target_margin_share = min(conservative, conservative * (1 - max(0, shock - 3%)))
```

当前配置中 `hard_share=35%`，无历史或平静 completed-return evidence 的 `conservative=30%`；当已完成收益绝对值或两日样本波动高于 3% 时，soft target 只会进一步收缩，永远不会向 35% hard margin gate 扩张。

含义：正常目标不再故意贴着 35% hard margin boundary，并在已完成账户证据变差时主动增加 headroom。**35% hard margin gate 没有变成 40%，也没有被绕过。**
'''
if body.count(old) != 1:
    raise SystemExit(f"production mechanics adaptive-margin block count={body.count(old)}")
path.write_text(body.replace(old, new, 1), encoding="utf-8")

prose_replacements = {
    "docs/research-final-evidence.md": (
        "**margin-aware target sizing**：当前 35% margin / 25% available / 5% daily-loss 配置下，正常 target margin share 为 30%；",
        "**adaptive margin-aware target sizing**：当前 35% margin / 25% available / 5% daily-loss 配置下，无历史/平静 target margin share 上限为 30%，completed shock 高于 3% 时只进一步收缩；",
    ),
    "docs/return-target-100-evidence.md": (
        "soft target margin share    = 30%",
        "calm soft target share      = 30% (completed shock can contract it)",
    ),
}
for name, (old, new) in prose_replacements.items():
    path = Path(name)
    body = path.read_text(encoding="utf-8")
    if body.count(old) != 1:
        raise SystemExit(f"{name}: expected one adaptive-margin prose match, got {body.count(old)}")
    path.write_text(body.replace(old, new, 1), encoding="utf-8")

for name in formal_docs:
    body = Path(name).read_text(encoding="utf-8")
    if "109.0636%" not in body or "28.9559%" not in body:
        raise SystemExit(f"{name}: final promoted metrics missing")
    for stale in (OLD_RUN, OLD_HEAD, OLD_MERGE, OLD_ARTIFACT, OLD_SHA):
        if stale in body:
            raise SystemExit(f"{name}: stale final evidence token remains: {stale}")

mechanics = Path("docs/directional-production-mechanics-evidence.md").read_text(encoding="utf-8")
if "soft_target_margin_share = max(0, hard_share - max_daily_loss_ratio)" in mechanics:
    raise SystemExit("production mechanics: obsolete fixed soft-margin formula remains")
if "completed shock" not in mechanics:
    raise SystemExit("production mechanics: adaptive margin contraction missing")

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
