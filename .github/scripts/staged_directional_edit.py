# message: docs: synchronize promoted directional efficiency evidence
from pathlib import Path


def read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def replace_required(text: str, old: str, new: str, label: str, minimum: int = 1) -> str:
    count = text.count(old)
    if count < minimum:
        raise SystemExit(f"{label}: expected at least {minimum} matches, got {count}")
    return text.replace(old, new)


# 1) Code comment parity: the promoted adaptive envelope never expands above 30%.
path = "afuture/directional.py"
text = read(path)
old = '''    Missing completed-return evidence keeps the conservative 30%-equivalent envelope.
    With completed evidence, calm conditions can recover some capacity while the formula
    still reserves the configured 5% equity-loss budget plus an observed shock allowance.
'''
new = '''    Missing or calm completed-return evidence keeps the conservative 30%-equivalent
    envelope. Completed shocks above the volatility trigger contract that envelope
    further; this helper never expands normal target margin toward the 35% hard gate.
'''
text = replace_required(text, old, new, "adaptive margin docstring")
write(path, text)

# 2) Replace obsolete current-result numbers in the formal docs. Historical rejected
# experiment numbers are added explicitly below, so these replacements only redefine
# what each document calls the current/final promoted candidate.
docs = [
    "README.md",
    "docs/architecture.md",
    "docs/data-and-backtest.md",
    "docs/live-trading.md",
    "docs/production-checklist.md",
    "docs/directional-production-mechanics-evidence.md",
    "docs/research-final-evidence.md",
    "docs/return-target-100-evidence.md",
]
replacements = [
    ("108.8461%", "109.0636%"),
    ("311.4052%", "312.2285%"),
    ("17.8010%", "15.8529%"),
    ("38.8943%", "38.5851%"),
    ("2.0812", "2.0976"),
    ("2,057,025.78", "2,061,142.43"),
    ("20.4057%", "28.9559%"),
    ("42.8545%", "62.9735%"),
    ("27.9925%", "28.1152%"),
    ("31.2307%", "31.5242%"),
    ("0.7466", "0.9604"),
    ("714,272.26", "814,867.56"),
    ("472 / 484", "474 / 484"),
    ("472/484", "474/484"),
    ("1.684784x", "1.668769x"),
    ("workflow run `32624688557`", "workflow run `32634296589`"),
    ("run `32624688557`", "run `32634296589`"),
    ("artifact id `9489421243`", "artifact id `9491959916`"),
    ("artifact id              = 9489421243", "artifact id              = 9491959916"),
    ("PR head                  = 198cad7ee62e0e6892ddf5c725525fb46c7383e2", "PR head                  = 921bd8b4820a8c1efa9c804a8ea1a1b56c2d188f"),
    ("PR merge ref             = 7664851987a59c2d87d1084376ffbd9649863b2c", "PR merge ref             = 1ec387433ee5011e44bc214e4e4c83a28e72c93c"),
    ("stress-robustness-l3-7664851987a59c2d87d1084376ffbd9649863b2c", "stress-80-l3-1ec387433ee5011e44bc214e4e4c83a28e72c93c"),
    ("6a9abb9eb15a542eda2683200bbf5f001613dccd9546bde11f85dc4f0aa6add7", "e531f2874cbc26c3a54ff561e074f59b86ac887a1f509e33effd6908eaa3144d"),
]
for path in docs:
    text = read(path)
    for old, new in replacements:
        text = text.replace(old, new)
    # Final daily-state counts differ from PR #13.
    text = text.replace("daily circuit days | 4 | 4", "daily circuit days | 3 | 2")
    text = text.replace("daily circuit days | **4** | **4**", "daily circuit days | **3** | **2**")
    text = text.replace("defensive risk days | 78 | 70", "defensive risk days | 77 | 70")
    text = text.replace("defensive days | 78 | 70", "defensive days | 77 | 70")
    write(path, text)

# 3) README: describe the promoted mechanisms and explicit improvement over main.
path = "README.md"
text = read(path)
needle = "上一版本的 Stress 只有 `0.9249%` 年化、14/484 个活跃日并因 margin hard gate 永久 HALT。"
replacement = '''更早的 Stress 只有 `0.9249%` 年化、14/484 个活跃日并因 margin hard gate 永久 HALT；PR #13 已先修复到 `20.4057%` / 472 active days。本轮 execution-efficiency 收口进一步把最终 Stress 提升到 **28.9559%** / 474 active days，同时 Base 从 PR #13 的 108.8461% 提升到 **109.0636%**。'''
text = replace_required(text, needle, replacement, "README baseline paragraph")
old_margin = '''hard margin share = min(max_margin_ratio, 1 - min_available_ratio)
soft target share = max(0, hard margin share - max_daily_loss_ratio)
'''
new_margin = '''hard_share = min(max_margin_ratio, 1 - min_available_ratio)
conservative = max(0, hard_share - max_daily_loss_ratio)
shock = clamp(max(3%, abs(latest completed return), two-day sample volatility), 3%, 5%)
soft_share = min(conservative, conservative * (1 - max(0, shock - 3%)))
'''
text = replace_required(text, old_margin, new_margin, "README margin formula")
needle = "当前 35% margin / 25% available / 5% daily-loss 配置下，正常目标 margin budget 为 **30% equity**。"
replacement = '''当前 35% margin / 25% available / 5% daily-loss 配置下，无历史或平静 completed-return evidence 的正常目标 margin budget 为 **30% equity**；已完成收益绝对值或两日样本波动高于 3% 时只会进一步收缩，永远不会把 soft target 扩张到 35% hard gate。'''
text = replace_required(text, needle, replacement, "README adaptive margin")
needle = "CTP completed activity 选 D+1 concrete contracts"
replacement = "CTP completed activity 选 D+1 concrete contracts（eligible incumbent 默认保留；challenger 必须同时在 OI 和 volume 上更高才换月）"
text = replace_required(text, needle, replacement, "README roll semantics")
needle = "margin-aware soft sizing（当前 30% equity target margin budget）"
replacement = "adaptive margin-aware soft sizing（平静基线 30%，completed shock 只可继续收缩）\n        ↓\n同方向 +1 lot 低价值增仓可保持 incumbent；任何减仓/反转/风险动作不受抑制"
text = replace_required(text, needle, replacement, "README lot persistence")
text += '''\n\n## Execution-efficiency 最终处置（2026-08-23）\n\n本轮不是把所有“降换手”想法都塞进生产。固定 L3 逐项决定晋级：\n\n- **保留**：turnover attribution；eligible-incumbent contract-roll hysteresis；同方向 `+1 lot` 低价值增仓抑制；completed-return 驱动的 margin soft-envelope 收缩。\n- **拒绝并回退**：product replacement persistence（Base 约 58.41%、Stress 约 22.55%）；cost-aware meta hysteresis（Base 约 58.32%、Stress -13.03%、DD 超 30% 且 HALT）；same-direction weight resize hysteresis（未通过 L3 promotion gate）。\n- 更早已拒绝：Base/Stress 50/50 meta score（Base 约 94.59%）和只保留慢 rebalance templates（Base 约 69.78%）。\n\n最终 Stress 从 `20.4057%` 提升到 **28.9559%**，但仍没有达到 80%。高频 entry/exit 中包含真实 Alpha，不能把 turnover 本身当作错误并无限压低；继续在同一两年历史上追到 80% 会把研究目标变成 selection fitting。\n'''
write(path, text)

# 4) Architecture/data/live docs: replace fixed-margin wording and add promoted roll/lot semantics.
for path in ("docs/architecture.md", "docs/data-and-backtest.md", "docs/live-trading.md"):
    text = read(path)
    text = text.replace(
        "soft_target_share = max(0, hard_share - max_daily_loss_ratio)",
        "conservative = max(0, hard_share - max_daily_loss_ratio)\nshock = clamp(max(3%, abs(latest completed return), two-day sample volatility), 3%, 5%)\nsoft_target_share = min(conservative, conservative * (1 - max(0, shock - 3%)))",
    )
    text = text.replace(
        "当前 35% margin / 25% available / 5% daily-loss 配置得到 **30% equity** 的正常 target margin budget。",
        "当前 35% margin / 25% available / 5% daily-loss 配置的平静基线为 **30% equity**；completed shock 高于 3% 时只会进一步收缩，35% hard gate 不变。",
    )
    text = text.replace(
        "当前配置得到 `35% - 5% = 30% equity` 的正常 target margin budget。",
        "当前配置的无历史/平静基线为 `35% - 5% = 30% equity`；completed shock 高于 3% 时 soft target 进一步收缩。",
    )
    write(path, text)

path = "docs/architecture.md"
text = read(path)
needle = "→ D+1 concrete contract selection"
replacement = "→ D+1 concrete contract selection；eligible incumbent 保留，除非 challenger 同时拥有更高 OI 与 volume"
text = replace_required(text, needle, replacement, "architecture roll")
needle = "→ margin-aware target sizing"
replacement = "→ adaptive margin-aware target sizing\n→ 同方向 +1 lot 增仓 no-trade（仅当 incumbent 仍满足 soft margin / 2x gross）"
text = replace_required(text, needle, replacement, "architecture lot")
text += '''\n\n## 14. Execution-efficiency promotion 结果\n\n最终生产只保留通过固定 L3 的机制：turnover attribution、completed-activity roll hysteresis、`+1 lot` 同方向增仓抑制和 completed-return shock margin contraction。Product replacement、meta hysteresis、same-direction weight hysteresis 均实际实现并验证过，但因为明显损伤 Base/Stress 而回退。最终 full_recent 为 Base **109.0636% / 15.8529% DD**，Stress **28.9559% / 28.1152% DD**，两者 no-HALT；Stress 80% 仍未达到。\n'''
write(path, text)

path = "docs/data-and-backtest.md"
text = read(path)
text = text.replace(
    "OI → volume → expiry → symbol 排序。",
    "默认排序仍为 OI → volume → expiry → symbol；若已有持仓合约仍 eligible，则只有 challenger 在 completed-day OI 与 volume 两维都严格更高时才切换。",
)
text += '''\n\n## 11. Execution-efficiency 负证据与最终晋级\n\n固定输入 `9473260618` 上，本轮最终晋级候选把 Production Stress 从 **20.4057%** 提升到 **28.9559%**，Base 从 **108.8461%** 提升到 **109.0636%**。晋级来源是执行机械而不是新模板搜索：roll hysteresis、one-lot increase no-trade、adaptive margin contraction 与 turnover attribution。\n\n三类更激进的 signal 层换手抑制被固定 L3 否决并回退：product replacement persistence（Base 约 58.41%）、cost-aware meta hysteresis（Stress -13.03% 且 HALT）、same-direction weight resize hysteresis（未通过 promotion gate）。这说明 entry/exit turnover 中包含重要 Alpha；不能按“换手越低越好”继续拟合。\n'''
write(path, text)

path = "docs/live-trading.md"
text = read(path)
needle = "D+1 当前 Tick 仍用于 fresh quote、bid/ask、depth、limit、价格、margin sizing 和下单，但不能改变 D 已冻结主力。"
replacement = '''D+1 当前 Tick 仍用于 fresh quote、bid/ask、depth、limit、价格、margin sizing 和下单，但不能改变 D 已冻结 activity evidence。已有持仓合约若仍 eligible，会继续作为 incumbent；只有另一个合约在 D 日 completed OI **和** volume 两项都严格更高时才换月，expiry/listing/activity 失效则立即按确定性排名切换。'''
text = replace_required(text, needle, replacement, "live roll")
needle = "5. 生成 openings 后，原 `RiskManager.check_open_orders()` 仍重新检查 35% max margin 和 25% min available，并拥有最终否决权。"
replacement = needle + "\n6. margin fitting 后若只是同方向 `+1 lot` 增仓，且当前持仓本身仍在 soft margin 与 2x gross 内，可保持 incumbent lot；减仓、反转、换月、daily circuit 与 gross guard 不受该 no-trade 规则抑制。"
text = replace_required(text, needle, replacement, "live one-lot")
write(path, text)

# 5) Production checklist: current gates plus new live checks.
path = "docs/production-checklist.md"
text = read(path)
needle = "- [x] Production L3 使用 margin-aware target sizing，当前配置把正常 target margin 控制在约 30% equity，同时保留 35% hard margin gate。"
replacement = '''- [x] Production L3 使用 adaptive margin-aware target sizing：无历史/平静基线约 30% equity，completed shock 高于 3% 时只继续收缩，35% hard margin gate 不变。
- [x] Production L3 输出 turnover attribution，并验证每个 bucket 求和等于总 turnover。
- [x] completed-activity 选约保留仍 eligible 的 incumbent；challenger 只有 OI 和 volume 同时更高才切换。
- [x] margin-fitted 同方向 `+1 lot` 增仓可在 incumbent 仍满足 soft margin / 2x gross 时保持不动；减仓与风险动作不被抑制。'''
text = replace_required(text, needle, replacement, "checklist promoted mechanics")
text = text.replace(
    "- [x] margin-aware soft target share 当前为 30%，它只缩正常目标，不替代或放宽 35%/25% hard gates。",
    "- [x] adaptive soft target share 的平静上限为 30%，completed shock 只可进一步收缩；它不替代或放宽 35%/25% hard gates。",
)
text = text.replace(
    "- [ ] OI → volume → expiry → symbol 排序与离线重建一致。",
    "- [ ] incumbent eligibility + challenger OI/volume 双维 dominance 与离线重建一致；无 incumbent 时 OI → volume → expiry → symbol 排序一致。",
)
text = text.replace(
    "- [ ] 当前 35%/25%/5% 配置下正常 target margin share 约为 30%。",
    "- [ ] 当前 35%/25%/5% 配置下平静 target margin share 约为 30%，completed shock 高于 3% 时能因果收缩。",
)
text += '''\n\n## O. 本轮执行效率实验处置\n\n- [x] product replacement persistence 已实现并跑固定 L3；因 Base 降至约 58.41% 而拒绝并回退。\n- [x] cost-aware meta hysteresis 已实现并跑固定 L3；因 Base 约 58.32%、Stress -13.03%、DD 超 30% 且 HALT 而拒绝并回退。\n- [x] same-direction weight resize hysteresis 已实现并跑固定 L3；未通过 promotion gate，已回退。\n- [x] 最终晋级版本保持 Base ≥100%、Stress DD≤30%、no-HALT、gross≤2x、0 margin rejects。\n- [ ] Stress 80% 仍是研究方向，不作为放宽硬门或重复拟合同一历史的理由。\n'''
write(path, text)

# 6) Formal evidence docs: add final comparison, attribution and rejected experiments.
final_block = '''\n\n## 11. 2026-08-23 Execution-efficiency 最终晋级\n\n相对进入本轮前的 `main` commit `b6b2cdca0f04193c10e14f8b3ad61902d6e36769`：\n\n- Base 年化：108.8461% → **109.0636%**（+0.2175 个百分点）；最大回撤 17.8010% → **15.8529%**；\n- Stress 年化：20.4057% → **28.9559%**（+8.5502 个百分点）；最大回撤 27.9925% → **28.1152%**；\n- Stress active days：472 → **474 / 484**；margin rejects 仍为 **0**；no permanent HALT；\n- hard gates 保持 2x gross / 35% margin / 25% available / 5% daily loss / 30% DD / 35 lots。\n\n最终固定证据：workflow run `32634296589`，PR head `921bd8b4820a8c1efa9c804a8ea1a1b56c2d188f`，PR merge ref `1ec387433ee5011e44bc214e4e4c83a28e72c93c`，artifact id `9491959916`，artifact `stress-80-l3-1ec387433ee5011e44bc214e4e4c83a28e72c93c`，SHA-256 `e531f2874cbc26c3a54ff561e074f59b86ac887a1f509e33effd6908eaa3144d`。\n\nStress turnover attribution（notional）：entry/exit `189,920,240`、resize `51,722,155`、reversal `11,314,145`、roll `2,521,010`、daily circuit `1,440,740`，总计 `256,918,290`；所有 bucket 与总 turnover 精确闭合。\n\n最终**保留**：turnover attribution、completed-day contract-roll hysteresis、same-sign `+1 lot` increase no-trade、completed-return shock adaptive margin contraction。最终**拒绝并回退**：product replacement persistence、cost-aware meta hysteresis、same-direction weight resize hysteresis。前两者固定 L3 分别把 Base 压至约 58.41% 与 58.32%；meta hysteresis 还令 Stress 为 -13.03%、DD 超 30% 并 HALT。\n\n因此 28.9559% 仍不是 80%。本轮证据反而证明高换手中有相当部分是有效 Alpha 迁移，不能无限压低 turnover；在同一已反复观察历史上继续调整门槛直到得到 80% 会增加 selection bias，而不是提高实盘可信度。\n'''
for path in ("docs/directional-production-mechanics-evidence.md", "docs/research-final-evidence.md", "docs/return-target-100-evidence.md"):
    text = read(path)
    if "## 11. 2026-08-23 Execution-efficiency 最终晋级" not in text:
        text = text.rstrip() + final_block + "\n"
    write(path, text)

# Correct obsolete claim that the current round stopped before execution-efficiency work.
for path in ("docs/directional-production-mechanics-evidence.md", "docs/research-final-evidence.md", "docs/return-target-100-evidence.md"):
    text = read(path)
    text = text.replace(
        "继续在同一两年已观察历史上修改：",
        "本轮在不扫新模板/参数的前提下已逐项评估执行效率机制；仍不能通过以下方式继续在同一两年历史上追数：",
    )
    text = text.replace(
        "Stress 年化仍只有 28.9559%。继续围绕同一已观察历史扩展 template、meta、governor 或 margin 参数直到得到 80%，增加的是过拟合而不是新信息，因此停止。",
        "Stress 年化提升到 28.9559%，仍未达到 80%。本轮已经把可解释的执行效率方向逐项实现并用固定 L3 晋级/否决；继续围绕同一已观察历史扩展 template、meta、governor 或门槛直到得到 80%，增加的是过拟合而不是新信息，因此不再追加同历史参数搜索。",
    )
    write(path, text)

# 7) Spec/plan are historical design artifacts; append final disposition and correct the
# adaptive formula so future readers do not mistake a rejected design for production.
path = "docs/superpowers/specs/2026-08-23-directional-execution-efficiency-design.md"
text = read(path)
old = '''The 35% margin hard gate and 25% available hard gate remain authoritative. Normal target sizing uses a causal soft envelope derived from completed account-return risk evidence.

Let `hard_share = min(max_margin_ratio, 1-min_available_ratio)`. Let `shock` be the completed-history adverse-move proxy bounded to `[volatility_trigger, max_daily_loss_ratio]`, using the latest absolute completed return and two-day sample volatility. The safe target share is:

`hard_share * (1-max_daily_loss_ratio) / (1+shock)`

and is never allowed below the existing 30% conservative floor or above the hard share. Defensive governor scaling remains authoritative and naturally reduces exposure further.

This formula reserves capacity for a full 5% equity loss plus a completed-evidence mark expansion; it does not modify the hard gate.
'''
new = '''The 35% margin hard gate and 25% available hard gate remain authoritative. Normal target sizing uses a causal soft envelope derived from completed account-return risk evidence.

`hard_share = min(max_margin_ratio, 1-min_available_ratio)` and `conservative = hard_share - max_daily_loss_ratio`, which is 30% under the frozen configuration. `shock` is bounded to `[3%, 5%]` from the latest absolute completed return and two-day sample volatility. The promoted rule is:

`soft_share = min(conservative, conservative * (1 - max(0, shock - 3%)))`

so missing/calm evidence stays at 30%, while larger completed shocks can only contract the soft target. It never expands toward the 35% hard gate. Defensive governor scaling remains authoritative and naturally reduces exposure further.
'''
text = replace_required(text, old, new, "spec adaptive margin")
text += '''\n\n## Final disposition\n\nImplementation and fixed-L3 evaluation are complete. Promoted: turnover attribution, contract-roll hysteresis, one-lot same-sign increase suppression and adaptive margin contraction. Rejected after L3: cost-aware meta hysteresis, product-replacement persistence and same-direction weight hysteresis. The final promoted Production L3 is Base **109.0636% / 15.8529% DD** and Stress **28.9559% / 28.1152% DD**, no permanent HALT, with all hard gates unchanged. The 80% Stress objective remains unmet and is not a justification for further fitting of this observed window.\n'''
write(path, text)

path = "docs/superpowers/plans/2026-08-23-directional-execution-efficiency.md"
text = read(path)
marker = "**Spec:** `docs/superpowers/specs/2026-08-23-directional-execution-efficiency-design.md`\n"
status = '''**Final disposition (2026-08-23):** execution completed. Promoted: attribution, roll hysteresis, one-lot increase no-trade and adaptive margin contraction. Rejected/reverted after fixed L3: meta hysteresis, product replacement persistence and same-direction weight hysteresis. Final promoted L3: Base 109.0636%, Stress 28.9559%; 80% not achieved.\n\n'''
if status not in text:
    text = replace_required(text, marker, marker + "\n" + status, "plan status")
write(path, text)

# 8) Restore normal full CI. The next user-authored checkpoint will trigger it.
path = ".github/workflows/ci.yml"
text = read(path)
old = '''  test:
    if: (github.event_name != 'push' || github.ref != 'refs/heads/codex/afuture-stress-80-efficiency-20260823') && (github.event_name != 'pull_request' || github.head_ref != 'codex/afuture-stress-80-efficiency-20260823')
    strategy:
'''
new = '''  test:
    strategy:
'''
text = replace_required(text, old, new, "restore full CI")
text = text.replace(
    "# Final repository gate. The Stress-efficiency feature uses a narrow TDD workflow until\n# its final candidate; every other branch and PR still runs the complete repository gate.\n",
    "# Final repository gate. Feature development used impact-scoped validation; the final\n# candidate runs this complete Python 3.10/3.13 repository gate.\n",
)
write(path, text)

# Development-only workflows must not land on main.
for path in (
    ".github/workflows/stress-80-efficiency-targeted.yml",
    ".github/workflows/apply-staged-directional-edit.yml",
):
    p = Path(path)
    if p.exists():
        p.unlink()
