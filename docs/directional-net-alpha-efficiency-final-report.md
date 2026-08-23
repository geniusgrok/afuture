# Directional Net-Alpha Efficiency 最终报告

日期：2026-08-23

## 1. 最终结论

本阶段没有任何新增经济行为通过生产晋级门。最终候选**保持 PR #14 的 Alpha、风险权限、leverage、成本、margin proxy、数据时点和账户语义不变**；进入主线的新增价值只有行为中性的 Production PnL/turnover/capacity attribution、严格隔离 future labels 的 entry/exit 离线诊断、测试和可复现负证据。

固定 Production Before/After 因此完全相同：Base 109.0636% 年化，Stress 28.9559% 年化。没有为了接近 80% 而继续扫参数。

基线：`main@711633548af350155ede84e6bbd45e15447a5398`，固定输入 artifact `9473260618`；PR #14 fixed L3 run `32634296589` / artifact `9491959916`。

## 2. 为什么 Production Stress 只有 28.9559%

Task 1 的行为中性账本在 fixed L3 workflow `32642547947` / artifact `9494018121` 上精确复现 PR #14 经济结果，并给出：

```text
initial equity        500,000.000
+ gross signal PnL    700,245.000
- 15bp cost           385,377.435
= final equity        814,867.565
```

因此 15bp 直接成本占 observed gross signal PnL 的 **55.03%**。Stress 的 realized-path costless annualized proxy 为 **88.0480%**，实际成本对应 **59.0921 个百分点**的 pathwise annualized drag proxy。

容量端也显著压低可兑现 exposure：平均 raw target gross **1.928742x**，completed-return governor 后平均 target **1.715932x**，平均 realized gross 只有 **1.175349x**。这不是单一“保证金太紧”，而是 integer lots、35 手上限、不可用合约、margin fitting、governor、one-lot stabilization 与账户风险路径共同作用。

## 3. Float 58.1372% → Production 的口径纠偏

用户要求的 `Float Stress 58.1372% -> integer/contracts -> margin -> governor -> transaction cost -> circuits -> Production 28.9559%` 不能被诚实地画成可加 waterfall，原因有两点：

1. 归档 **58.1372% 本身已经包含 15bp one-way cost**，不能再扣一次 transaction cost；
2. 它来自较早 selection-biased Float 权重 lineage，而 PR #14 当前冻结权重的同口径 Float 15bp 是 **109.3145%**，不是同一 weight path。

所以保留两张表。

### 3.1 归档 headline gap

| 口径 | 年化 | 解释 |
|---|---:|---|
| 归档 Float Stress 15bp | 58.1372% | 较早权重 lineage，已含成本 |
| 当前 Production Stress | 28.9559% | PR #14 当前权重 + production mechanics |
| 原始差值 | -29.1813 pp | **不能归因**，lineage 不同 |

### 3.2 当前同 lineage 的可审计 bridge

当前冻结权重在同一 roll-safe next-open specific-contract 口径下：

| Stage / loss mechanism | 定量证据 | 年化影响口径 |
|---|---:|---:|
| Float 0bp | **236.4127%**；turnover ratio sum 608.4x | gross Float reference |
| Float 15bp | **109.3145%** | -127.0982 pp vs 0bp Float |
| raw target gross | avg **1.928742x** | exposure reference |
| governor | avg target **1.715932x**；70 天；denied 103.0 gross-ratio-days | 非可加 exposure effect |
| integer rounding | **86,822,426.49** notional-days / 483 天 | 非可加 exposure effect |
| max 35 lots clipping | **6,215,900.00** notional-days / 7 天 | 非可加 exposure effect |
| unavailable contract | **2,580,836.72** notional-days / 11 天 | 非可加 exposure effect |
| margin fitting | **39,374,570.00** notional-days / 264 天 | 非可加 exposure effect |
| one-lot stabilization | **1,751,885.00** notional-days / 28 天 | 非可加 exposure effect |
| realized gross | avg **1.175349x**；peak **1.668769x** | realized exposure |
| realized production path before cost | gross PnL 700,245；**88.0480%** annualized path proxy | costless realized-path reference |
| transaction cost | turnover **256,918,290**；cost **385,377.435** | **-59.0921 pp** pathwise drag proxy |
| daily circuit direct cost | 2 天；turnover 1,440,740；cost 2,161.11 | 0.2707 pp 已包含在 cost drag |
| Production Stress | **28.9559%** | final |

容量项不能强行转换成可加收益百分点：关闭任一机制都会改变后续 equity、integer lots、risk state 和后续交易，独立 PnL counterfactual 不是行为中性可观测量。报告宁可明确 `non-additive`，不制造伪精确数字。

Daily circuit 的**直接执行成本**可观测；“如果当日不 circuit 后面会赚/亏多少”的 opportunity PnL 需要改变后续账户状态，不能从 realized ledger 唯一识别，因此不报告伪精确机会收益。

## 4. Stress transaction cost / turnover attribution

| Action | Events | Affected days | Turnover | 15bp cost | Annualized drag proxy |
|---|---:|---:|---:|---:|---:|
| Entry | 415 | 238 | 97,057,695 | 145,586.5425 | 19.8080 pp |
| Exit | 411 | 227 | 92,862,545 | 139,293.8175 | 19.0010 pp |
| Resize | 264 | 206 | 51,722,155 | 77,583.2325 | 10.0825 pp |
| Reversal | 54 | 25 | 11,314,145 | 16,971.2175 | 2.0903 pp |
| Roll | 12 | 6 | 2,521,010 | 3,781.5150 | 0.4757 pp |
| Daily circuit | 2 | 2 | 1,440,740 | 2,161.1100 | 0.2707 pp |

Entry + exit 合计占 Stress turnover **73.92%**。但“换手大”不等于“坏交易”。

## 5. Alpha / product / holding-period attribution

Stress gross PnL **700,245**：long **460,515**，short **239,730**。

正贡献最大：AG **354,525**、LU **123,150**、JM **95,220**、EB **36,505**、UR **36,360**；负贡献最大：L **-33,835**、AP **-23,770**、SM **-22,800**、FU **-15,920**、HC **-12,630**。

AG 占 observed gross PnL **50.63%**，前三品种合计 **81.81%**，历史贡献集中度仍然明显。这也是不能把最近两年结果包装成稳定泛化证据的原因之一。

Stress production proxy 记录 **1,158 个执行事件 / 340 个受影响交易日**，平均持有期 **4.2164 sessions**。这是日线 integer-contract proxy event count，不是未来 live CTP fill count。

无法可靠重建 template/family PnL ownership：一个产品目标是多个 active templates 的平均，而 Production event artifact 没有保存每笔执行的 exact template ownership。这里明确标记“证据不足”，不伪造 family attribution。

## 6. Entry / exit 质量结论

离线诊断把 `feature_*` 限制为交易发生前已完成信息，把未来 1/3/5/10 session return、MFE/MAE、false breakout、temporary displacement 放在 `label_*`，production 不导入 future-label analyzer。

固定 full-recent 账本中，future-only 标签识别出 40 个 false-breakout entries（turnover 9,707,355）和 52 个 temporary-displacement exits（turnover 16,755,545），但它们不能直接成为生产规则。

可因果 cohort 的 5-session 15bp net return 在 prior1/prior2/train/validation/OOS 上不稳定。例如 rapid same-side re-entry 分别为 **-0.3232% / +0.1535% / +0.1886% / +2.7871% / -0.1479%**；full_recent 82 个 rapid re-entry / turnover 22,052,305 的 5-session net diagnostic 约 **+1.1214%**。机械禁止 re-entry 会删除真实 Alpha。

结论：**不存在跨窗口稳定负贡献、且能在交易前因果识别的 entry/exit cohort**。不晋级 hysteresis、minimum hold、entry persistence 或 exit persistence。

## 7. Net-Edge-Aware qualification

只评估一个预声明、低自由度 candidate：product + direction + completed 20-session trend bucket，5-session outcome，只使用在当前决策前已经完整结束的历史 entry；expected gross edge 采用经验均值并向 0 shrink，再减固定 30bp round-trip hurdle。没有 threshold grid。

| Window | Qualified turnover share | Qualified net h5 | Rejected net h5 | Separation |
|---|---:|---:|---:|---:|
| prior1 | 0.39% | +2.0120% | -1.5864% | +3.5984% |
| prior2 | 28.43% | **-1.2529%** | **+0.4600%** | **-1.7128%** |
| train | 32.01% | -0.0134% | -0.3600% | +0.3466% |
| validation | 35.48% | +2.1307% | -0.1307% | +2.2614% |
| OOS | 27.84% | +1.3962% | +0.1334% | +1.2628% |

prior2 明确反向失效，因此 candidate 在进入 production 前即被拒绝；没有为它浪费固定 L3，更没有改 threshold 追结果。

## 8. 新 Alpha families

没有做 MA/lookback 大网格。固定评估：slow multi-horizon trend、all-horizon confirmed trend、cross-sectional momentum、breakout+range/trend confirmation、point-in-time carry、carry+trend、strength-ranked normalized trend。

15bp roll-safe next-open 年化：

| Family | prior1 | prior2 | train | validation | OOS | full_recent | DD | Decision |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Slow trend | -6.19% | -14.58% | -24.54% | +6.05% | -6.57% | -13.38% | -28.71% | reject |
| Confirmed trend | -8.93% | -7.13% | -22.72% | +22.00% | -8.73% | -9.80% | -30.43% | reject |
| X-sectional momentum | -6.01% | -18.62% | -33.58% | +26.91% | -13.72% | -16.77% | -35.05% | reject |
| Breakout confirmation | -15.61% | +2.23% | -37.02% | +26.01% | +4.34% | -15.09% | -42.22% | reject |
| Point-in-time carry | -4.29% | +2.17% | -6.82% | -11.06% | +16.26% | -2.55% | -23.75% | reject |
| Carry + trend | -10.41% | +1.06% | -17.96% | -13.75% | +9.81% | -10.55% | -25.98% | reject |
| Strength-ranked trend | -0.45% | -20.66% | -32.48% | +45.56% | -43.20% | -21.95% | -48.22% | reject |

没有一个 family 独立通过，因此 Task 5 **不把失败策略混合后碰运气**。组合权重搜索被停止。

## 9. Margin capacity 次级研究

唯一预声明 candidate 由硬门推导，不是手改 30%→33%：

```text
hard share = min(35% margin, 1 - 25% available) = 35%
adverse reserve = max(5% daily-loss gate, last two completed abs returns, two-day sample vol)
full-reversal cost reserve = 2 * 2x gross * 15bp = 0.60%
calm soft share = 35% * (1 - 5% - 0.60%) = 33.04%
```

固定 Stress screen workflow `32646232667`，artifact `9494951476`，SHA-256 `f2962bfd6feb2f267bd72046509fc0dc888618e2e0acdc5d98732ec2b786fd4a`：

| Metric | Baseline | Candidate |
|---|---:|---:|
| Annualized | 28.9559% | **4.7970%** |
| Total return | 62.9735% | **9.4165%** |
| Max DD | 28.1152% | **22.6644%** |
| Sharpe | 0.9604 | **0.3046** |
| Active | 474/484 | **366/484** |
| Max gross | 1.668769x | **1.806981x** |
| Margin rejects | 0 | **0** |
| Permanent HALT | false | **true** |

明确拒绝。之后没有继续试 32%、33%、不同 shock trigger 或 margin grid。

## 10. Before vs After

| Metric | Before PR #15 | After final candidate |
|---|---:|---:|
| Base annualized | 109.0636% | **109.0636%** |
| Stress annualized | 28.9559% | **28.9559%** |
| Base DD | 15.8529% | **15.8529%** |
| Stress DD | 28.1152% | **28.1152%** |
| Stress Sharpe | 0.9604 | **0.9604** |
| Stress active | 474/484 | **474/484** |
| Stress gross peak | 1.668769x | **1.668769x** |
| Margin rejects | 0 | **0** |
| HALT | false | **false** |
| Stress turnover | baseline 未细分 | **256,918,290 已量化；经济行为不变** |
| Proxy execution events | baseline 未细分 | **1,158 已量化；经济行为不变** |
| Avg holding period | unavailable | **4.2164 sessions 已量化** |

## 11. 对最终 20 个问题的直接回答

1. **原 Stress 为什么低：** 15bp 成本吃掉 gross PnL 的 55.03%，同时 capacity/governor/integer mechanics 把平均 raw gross 1.9287x 压到 realized 1.1753x。
2. **Float 58.1372% → Production：** 不能做伪 additive waterfall；58.1372% 已含 15bp 且是旧 lineage。当前同 lineage bridge 已在第 3 节给出。
3. **Transaction cost：** 385,377.435；realized-path annualized drag proxy 59.0921pp。
4. **Margin capacity：** 39,374,570 notional-days / 264 天；独立 PnL 非可加。
5. **Governor/circuit/lot：** governor 103.0 denied gross-ratio-days / 70 天；rounding 86.822m notional-days；one-lot 1.752m；circuit 直接成本 2,161.11。
6. **Entry/exit 有价值部分：** rapid re-entry full_recent 5-session net diagnostic +1.1214%；不能把高 turnover 等同坏 churn。
7. **可因果识别低质量交易：** 没有跨 prior/OOS 稳定成立的 cohort，故无生产 filter。
8. **新增 Alpha：** 七类见第 8 节。
9. **各 Alpha 独立表现：** 全部表列，全部因 prior/OOS/15bp 不稳被拒。
10. **组合结果：** 不组合失败策略，因此无新增组合收益数字。
11. **Turnover：** Stress 256,918,290。
12. **Trade count：** 1,158 proxy execution events / 340 affected days。
13. **DD：** Base 15.8529%，Stress 28.1152%。
14. **Gross：** target/realized hard cap 2x 不变；Stress peak 1.668769x。
15. **Margin：** Stress 15% proxy ×1.25 buffer；35% hard margin / 25% available 不变；0 rejects。
16. **Sharpe：** Base 2.0976，Stress 0.9604。
17. **多窗口/regime：** entry cohort、net-edge、新 families 均覆盖 prior1/prior2/train/validation/OOS；无新规则稳定通过。
18. **拒绝实验：** PR #14 已拒的 score/hysteresis/persistence，加本阶段 net-edge、七个 family、shock-derived margin candidate；均有负证据。
19. **Selection bias / overfitting：** 明显存在；最近两年和 final OOS 都被观察过，任何结果都不称 pristine OOS。
20. **为什么应该进 main：** **不是因为收益提高**。只有行为中性 attribution、离线因果/未来标签边界、测试和可复现证据值得合并；Production Alpha/risk/economics 保持完全等价。最终 CI 若有行为回归则不应合并。

## 12. 停止规则

所有有经济依据、且没有重复 PR #14 负实验的方向已经评估。继续提升只能通过增加参数自由度、换 threshold、换 lookback/rebalance/fraction 或在同一已观察历史重新 selection。按照防过拟合要求，在此停止。

下一阶段真正有新增信息价值的输入是：**新发生的未见市场期、真实 CTP Shadow、实际 commission/slippage/margin、测试柜台 partial/reject/reconnect 和小资金 live execution**，而不是继续优化 2024-08-21~2026-08-20。

过程证据见 `docs/directional-net-alpha-efficiency-evidence.md`。
